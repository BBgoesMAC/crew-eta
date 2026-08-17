"""
crew-eta: Garmin LiveTrack -> ETA dashboards for a support crew.

Multi-track model:
- /tracking/admin  : password-protected; create / edit / delete tracks
- /tracking/       : list of all tracks; a track without a Garmin link shows an
                     inline "enter link" field that the first visitor fills in
- /tracking/t/<id> : one track's dashboard (map + ETA), incl. a button to
                     change the Garmin link

Per-VP ETA: grade-adjusted equivalent distance (Minetti-style), a blend of a
rolling window and overall pace, plus a small fatigue drift.
"""

import asyncio
import base64
import json
import math
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import gpxpy
import httpx
from fastapi import (Depends, FastAPI, File, Form, Header, HTTPException,
                     UploadFile)
from fastapi.responses import HTMLResponse, JSONResponse

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
ROLLING_WINDOW_MIN = float(os.environ.get("ROLLING_WINDOW_MIN", "30"))
DRIFT_PER_HOUR = float(os.environ.get("DRIFT_PER_HOUR", "0.03"))
DEFAULT_ADMIN_PASSWORD = "changeme-in-env"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
OFFROUTE_MAX_M = 400.0
MAX_TRACKPOINTS = 60000
# Projection is progress-gated: between two fixes the runner may advance at most
# this far along the route. Nearest-point-by-air-distance alone snaps to the
# finish on loop courses (finish sits right next to the start), so a plausible
# along-route speed cap plus a fixed slack keeps the projection honest.
MAX_ALONG_SPEED = 8.0       # m/s (~28.8 km/h) — above any trail runner incl. fast downhills
FWD_SLACK_M = 400.0         # extra forward allowance per fix (sparse/late points)
BACK_SLACK_M = 150.0        # allow a small backward correction (GPS jitter / switchbacks)
# Where the route runs over itself (a crossing, an out-and-back sharing a
# trail), two very different route positions sit on the same spot but point in
# different directions. We disambiguate with the runner's heading, taken from
# recent GPS fixes: when the nearest point runs against the runner but an almost
# co-located point runs with them, we take the aligned one. Distance still leads;
# heading only breaks the tie, so ordinary trail and switchbacks are untouched.
DIR_COLOC_M = 8.0           # two legs within this are literally "the same spot" (a real
                            # overlap); switchback legs are metres apart and stay unaffected
DIR_MAX_DIFF = math.pi / 2  # a leg pointing >90° off the runner's heading is "wrong way"
# The heading is smoothed over a baseline of recent travel, NOT a single step:
# on short legs a per-fix bearing is mostly GPS noise and would fight the very
# leg the runner is on. We look back until the runner has moved this far, so the
# signal (real displacement) dominates the scatter.
HEADING_BASELINE_M = 45.0
RECENT_GPS_MAX = 40         # how many accepted coords to retain for that lookback
# Hard safety cap on the heading override: it may only re-seat the projection
# onto an overlapping leg within this along-route distance of the current
# progress — i.e. still "where the runner plausibly is". This is what makes the
# direction logic incapable of ever flinging the projection to a far leg (the
# failure mode a naive heading check causes in switchback corners).
OVERRIDE_MAX_M = 200.0
# After a GPS/Internet dropout in the mountains the next fix arrives with a long
# dt, so the reachable band would balloon and could snap to a far, unrelated leg
# purely by proximity. Cap how far a *single* re-acquisition may jump forward;
# with the heading check this keeps re-connects on the correct leg.
REACQUIRE_MAX_M = 3000.0    # max forward jump on one fix after a long gap

# Security / abuse limits
MAX_ROUTE_POINTS = 200000          # cap parsed GPX route size (CPU/RAM DoS)
MAX_GPX_BYTES = 10 * 1024 * 1024   # 10 MB upload cap
MAX_NAME_LEN = 200
MAX_MARKERS_LEN = 20000
MAX_MARKERS = 1000
MAX_LIVETRACK_LEN = 2000
_LIVETRACK_MAX_REDIRECTS = 6       # cap redirect hops when resolving gar.mn links
# Track ids are secrets.token_hex(4); accept only hex so ids can never
# escape DATA_DIR when used to build file paths.
_TID_RE = re.compile(r"[0-9a-fA-F]{6,64}\Z")

DATA_DIR.mkdir(parents=True, exist_ok=True)
META_FILE = DATA_DIR / "tracks.json"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        # Map tiles: Esri World Imagery (satellite + labels hybrid, key-less) and
        # Google hybrid, plus the CARTO dark basemap as a night option.
        "img-src 'self' data: https://unpkg.com https://*.basemaps.cartocdn.com "
        "https://server.arcgisonline.com https://*.arcgisonline.com "
        "https://*.google.com https://*.googleapis.com https://*.ggpht.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://unpkg.com; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "font-src https://fonts.gstatic.com; "
        "connect-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'"
    )
    return resp

# STATE["tracks"][id] = {
#   cfg: {id,name,markers,simulate,livetrack_url,session_id,token,created},
#   route, track[], history[], last_idx, passed{}, poll_error, last_poll_ok }
STATE = {"tracks": {}}
LOCK = asyncio.Lock()

# ----------------------------------------------------------------------------
# Geometry & grade-adjusted pace
# ----------------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2):
    """Compass bearing in radians from point 1 to point 2 (0 = north)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.atan2(y, x)


def route_bearing(route, i):
    """Direction the route heads at index i (radians)."""
    n = len(route["lat"])
    a = max(0, i - 1)
    b = min(n - 1, i + 1)
    if a == b:
        return 0.0
    return bearing(route["lat"][a], route["lon"][a],
                   route["lat"][b], route["lon"][b])


def smoothed_heading(recent, lat, lon):
    """Runner bearing (radians) from recent GPS coords, or ``None`` if unknown.

    ``recent`` is the tail of accepted (lat, lon) fixes, oldest first. We step
    back through it until the runner has moved at least ``HEADING_BASELINE_M``,
    so the heading reflects real travel rather than the jitter of one fix. A
    runner who has barely moved yields ``None`` (direction stays as it was)."""
    for rlat, rlon in reversed(recent):
        if haversine(rlat, rlon, lat, lon) >= HEADING_BASELINE_M:
            return bearing(rlat, rlon, lat, lon)
    return None


def grade_factor(g):
    """Pace multiplier vs. flat (Minetti-style, tuned for trail).
    +10% uphill ~1.4x, +20% ~2.1x, -10% ~0.9x, steep downhill slow again."""
    g = max(-0.35, min(0.35, g))
    m = 1.0 + 2.6 * g + 15.0 * g * g
    return max(0.7, min(4.0, m))


def build_route(gpx_bytes):
    if len(gpx_bytes) > MAX_GPX_BYTES:
        raise ValueError("GPX file too large")
    text = gpx_bytes.decode("utf-8", errors="replace")
    # Reject DOCTYPE / entity declarations: defends against XXE (local file
    # disclosure via SYSTEM entities) and "billion laughs" expansion DoS,
    # independent of the XML backend gpxpy uses.
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low or "<!element" in low:
        raise ValueError("GPX must not contain DOCTYPE or entity declarations")
    try:
        gpx = gpxpy.parse(text)
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - gpxpy raises various XML/GPX errors
        raise ValueError("File is not valid GPX")
    pts = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            pts.extend(seg.points)
    if not pts:
        for rte in gpx.routes:
            pts.extend(rte.points)
    if len(pts) < 2:
        raise ValueError("GPX contains no route")
    if len(pts) > MAX_ROUTE_POINTS:
        raise ValueError(f"GPX route too large (max {MAX_ROUTE_POINTS} points)")

    lat = [p.latitude for p in pts]
    lon = [p.longitude for p in pts]
    ele_raw = [p.elevation if p.elevation is not None else 0.0 for p in pts]

    # Smooth elevation (moving average) so the gradient doesn't jitter
    w = 7
    ele = []
    for i in range(len(ele_raw)):
        a, b = max(0, i - w // 2), min(len(ele_raw), i + w // 2 + 1)
        ele.append(sum(ele_raw[a:b]) / (b - a))

    cum = [0.0]
    eq = [0.0]
    for i in range(1, len(lat)):
        d = haversine(lat[i - 1], lon[i - 1], lat[i], lon[i])
        if d < 0.01:
            d = 0.01
        g = (ele[i] - ele[i - 1]) / d
        cum.append(cum[-1] + d)
        eq.append(eq[-1] + d * grade_factor(g))
    return {"lat": lat, "lon": lon, "ele": ele, "cum": cum, "eq": eq,
            "total_m": cum[-1], "total_eq": eq[-1]}


def idx_at_km(route, km):
    target = km * 1000.0
    cum = route["cum"]
    lo, hi = 0, len(cum) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _angle_diff(a, b):
    """Smallest absolute difference between two bearings (radians), 0..pi."""
    d = abs(a - b) % (2 * math.pi)
    return d if d <= math.pi else 2 * math.pi - d


def _best_in_range(route, lat, lon, lo, hi, heading, cur_m):
    """Pick the on-route point in [lo, hi) that best matches the fix.

    Distance decides by default: the result is the nearest on-route point, which
    is correct for ordinary trail, switchbacks and loops alike, and can never run
    away from the runner. Heading intervenes only in one specific situation — the
    nearest point runs *against* the runner while an essentially co-located point
    (within DIR_COLOC_M) runs *with* them. That is precisely a crossing or an
    overlapping out-and-back, where two route positions share the same spot but
    opposite directions; there we take the aligned one. Two guards keep this from
    ever misfiring: the co-location cap (legs a switchback apart are not "the
    same spot"), and OVERRIDE_MAX_M — the aligned point must still be near the
    current progress ``cur_m``, so the override can never fling the projection to
    a distant leg. Returns (index, distance) or (None, inf).
    """
    cands = []
    d_min, i_min = 1e18, None
    for i in range(lo, hi):
        d = haversine(lat, lon, route["lat"][i], route["lon"][i])
        if d <= OFFROUTE_MAX_M:
            cands.append((d, i))
            if d < d_min:
                d_min, i_min = d, i
    if i_min is None:
        return None, 1e18
    if heading is None:
        return i_min, d_min
    if _angle_diff(route_bearing(route, i_min), heading) <= DIR_MAX_DIFF:
        return i_min, d_min  # nearest already goes the runner's way — keep it
    # Nearest runs against us: prefer an aligned point on the same spot that is
    # still a plausible step from where the runner already was.
    cum = route["cum"]
    best_i, best_d = None, 1e18
    for d, i in cands:
        if d <= d_min + DIR_COLOC_M \
                and abs(cum[i] - cur_m) <= OVERRIDE_MAX_M \
                and _angle_diff(route_bearing(route, i), heading) <= DIR_MAX_DIFF \
                and d < best_d:
            best_d, best_i = d, i
    return (best_i, best_d) if best_i is not None else (i_min, d_min)


def project(route, lat, lon, last_idx, dt, heading=None):
    """Project a position onto the route, constrained by the distance the runner
    can plausibly have covered AND the direction they are travelling — not just
    air-line proximity.

    ``dt`` is the seconds since the last accepted fix (``None`` for the very
    first one); ``heading`` is the runner's recent bearing in radians, or
    ``None`` when unknown. Pure nearest-neighbour snapping fails wherever the
    course runs close to itself: on a loop the finish sits beside the start, and
    at a crossing / out-and-back the nearest point may belong to the *opposite*
    leg — so the runner teleports to the finish or appears to run backwards. We
    therefore search only a window that starts just behind the current progress
    and reaches forward as far as speed*dt allows, and inside it prefer the leg
    whose direction matches the runner's heading.

    Returns the new route index (never far ahead of real progress) or ``None``
    when the point is off-route / implausible, in which case the caller keeps
    the previous progress and the point is treated as a gap.
    """
    cum = route["cum"]
    n = len(cum)
    if dt is None:
        # First fix: bind to the EARLIEST on-route point, not the nearest. On a
        # loop the finish is metres from the start; earliest keeps us at km 0.
        best_i, best_d = None, 1e18
        for i in range(n):
            d = haversine(lat, lon, route["lat"][i], route["lon"][i])
            if d < best_d:
                best_d, best_i = d, i
                if best_d <= OFFROUTE_MAX_M:
                    break  # first point inside the corridor wins -> earliest
        if best_i is None or best_d > OFFROUTE_MAX_M:
            return None
        return best_i
    # Subsequent fixes: search only the reachable band around current progress.
    # A long gap (mountains) widens the band, but the single-fix forward jump is
    # capped so a re-connect can't leap across the map; the heading check then
    # keeps it on the correct leg.
    cur_m = cum[last_idx]
    fwd = min(MAX_ALONG_SPEED * max(dt, 0.0) + FWD_SLACK_M, REACQUIRE_MAX_M)
    lo = idx_at_km(route, max(0.0, cur_m - BACK_SLACK_M) / 1000.0)
    hi = min(n, idx_at_km(route, (cur_m + fwd) / 1000.0) + 1)
    best_i, best_d = _best_in_range(route, lat, lon, lo, hi, heading, cur_m)
    if best_i is None or best_d > OFFROUTE_MAX_M:
        return None  # off-route / gap: keep previous progress, retry next fix
    return best_i


# ----------------------------------------------------------------------------
# LiveTrack
# ----------------------------------------------------------------------------
def parse_livetrack_url(url):
    # Grab whole path segments (stop at the next / ? #) so a session id or token
    # is never silently truncated if Garmin changes their character set.
    m = re.search(r"livetrack\.garmin\.com/session/([^/?#]+)", url)
    if not m:
        raise ValueError("Not a valid LiveTrack link (expected .../session/<id>/...)")
    sid = m.group(1)
    tok = None
    mt = re.search(r"/token/([^/?#]+)", url)
    if mt:
        tok = mt.group(1)
    else:
        mq = re.search(r"[?&]token=([A-Za-z0-9_-]+)", url)
        if mq:
            tok = mq.group(1)
    return sid, tok


def _is_garmin_host(host):
    """True only for Garmin-owned hosts we trust to resolve/fetch links from.

    Guards the short-link resolver against SSRF: an attacker can point a link
    anywhere, but we refuse to make requests to anything outside Garmin.
    """
    host = (host or "").lower()
    return (host in ("gar.mn", "www.gar.mn")
            or host == "garmin.com" or host.endswith(".garmin.com"))


async def _follow_garmin_redirects(url):
    """Follow HTTP redirects from a Garmin short link (gar.mn/...) until the
    final livetrack.garmin.com/session/... URL is reached.

    SSRF-safe: every hop must stay on a Garmin host, only http(s) is allowed,
    redirects are capped and there is a short timeout.
    """
    headers = {"user-agent": "Mozilla/5.0 (crew-eta-dashboard)"}
    current = url
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        for _ in range(_LIVETRACK_MAX_REDIRECTS):
            p = urlparse(current)
            if p.scheme not in ("http", "https") or not _is_garmin_host(p.hostname):
                raise ValueError("Refusing to follow a non-Garmin redirect")
            if (p.hostname or "").lower().endswith("livetrack.garmin.com") \
                    and "/session/" in (p.path or ""):
                return current
            r = await client.get(current, headers=headers)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                if not loc:
                    raise ValueError(
                        "LiveTrack short link redirected without a target")
                current = urljoin(current, loc)
                continue
            # Not a redirect: trust the final resolved URL of the response.
            return str(r.url)
    raise ValueError("Too many redirects while resolving the LiveTrack link")


async def resolve_livetrack(raw):
    """Turn a pasted LiveTrack link into (session_id, token, canonical_url).

    Accepts either a full livetrack.garmin.com/session/... URL (parsed with no
    network call) or a Garmin short link such as https://gar.mn/XXXX, which is
    resolved by following redirects within Garmin domains only.
    """
    p = urlparse(raw)
    if not _is_garmin_host(p.hostname):
        raise ValueError(
            "Not a Garmin LiveTrack link "
            "(expected livetrack.garmin.com/... or gar.mn/...)")
    if (p.hostname or "").lower().endswith("livetrack.garmin.com") \
            and "/session/" in (p.path or ""):
        sid, tok = parse_livetrack_url(raw)
        return sid, tok, raw
    final = await _follow_garmin_redirects(raw)
    sid, tok = parse_livetrack_url(final)
    return sid, tok, final


# Garmin's LiveTrack is now a Cloudflare-fronted SPA whose trackpoints API is
# guarded by a double-submit CSRF token. We load the session page first (it sets
# cookies and embeds <meta name="csrf-token" ...>), then call the API with that
# token in the Livetrack-Csrf-Token header, reusing the page's cookies. Both the
# cookies and the header are required; the User-Agent is not checked.
_CSRF_RE = re.compile(r'name="csrf-token"\s+content="([^"]+)"')


async def fetch_trackpoints(sid, tok):
    if not tok:
        raise RuntimeError("LiveTrack token missing")
    page_url = f"https://livetrack.garmin.com/session/{sid}/token/{tok}"
    api_url = (f"https://livetrack.garmin.com/api/sessions/{sid}"
               f"/track-points/common?token={tok}")
    ua = {"user-agent": "Mozilla/5.0 (crew-eta-dashboard)"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # 1) Load the session page: sets cookies + carries the CSRF token.
        page = await client.get(page_url, headers=ua)
        m = _CSRF_RE.search(page.text)
        if not m:
            raise RuntimeError(f"CSRF token not found (HTTP {page.status_code})")
        csrf = m.group(1)
        # 2) Fetch trackpoints with the CSRF header + the cookies just set.
        r = await client.get(api_url, headers={**ua, "accept": "application/json",
                                               "Livetrack-Csrf-Token": csrf})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.json()
        tps = data.get("trackPoints") or data.get("trackpoints") or []
        out = []
        for tp in tps:
            pos = tp.get("position") or {}
            la, lo = pos.get("lat"), pos.get("lon")
            ts = tp.get("dateTime") or tp.get("timestamp")
            if la is None or lo is None or ts is None:
                continue
            try:
                t = datetime.fromisoformat(
                    str(ts).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            out.append({"t": t, "lat": la, "lon": lo,
                        "ele": tp.get("altitude")})
        return out


# ----------------------------------------------------------------------------
# ETA engine (per track)
# ----------------------------------------------------------------------------
def new_track(cfg, route):
    return {"cfg": cfg, "route": route, "track": [], "history": [],
            "last_idx": 0, "passed": {}, "poll_error": None, "last_poll_ok": None,
            # recent accepted GPS coords + the smoothed heading derived from
            # them, used to keep the projection on the correct leg at crossings,
            # switchbacks and on re-connect after a signal gap.
            "recent": [], "heading": None}


def recompute_passed(tr):
    tr["passed"] = {}
    route = tr["route"]
    for mk in tr["cfg"]["markers"]:
        for (t, i) in tr["history"]:
            if route["cum"][i] / 1000.0 >= mk["km"] - 0.05:
                tr["passed"][mk["name"]] = t
                break


def ingest_points(tr, points):
    known = {round(p["t"], 1) for p in tr["track"]}
    added = [p for p in sorted(points, key=lambda p: p["t"])
             if round(p["t"], 1) not in known]
    route = tr["route"]
    for p in added:
        tr["track"].append(p)
        # dt is measured against the last ACCEPTED fix; None for the first one.
        # A skipped (off-route) point widens dt for the next, so the reachable
        # band grows and tracking re-acquires after a signal gap on its own.
        dt = (p["t"] - tr["history"][-1][0]) if tr["history"] else None
        # Heading smoothed over recent travel; keep the last one while the
        # runner is basically stationary so the direction can't spin on jitter.
        heading = smoothed_heading(tr["recent"], p["lat"], p["lon"])
        if heading is None:
            heading = tr["heading"]
        idx = project(route, p["lat"], p["lon"], tr["last_idx"], dt, heading)
        if idx is not None:
            tr["last_idx"] = idx
            tr["history"].append((p["t"], idx))
            tr["heading"] = heading
            tr["recent"].append((p["lat"], p["lon"]))
            if len(tr["recent"]) > RECENT_GPS_MAX:
                tr["recent"] = tr["recent"][-RECENT_GPS_MAX:]
    if len(tr["track"]) > MAX_TRACKPOINTS:
        tr["track"] = tr["track"][-MAX_TRACKPOINTS:]
    if tr["history"]:
        cur_km = route["cum"][tr["history"][-1][1]] / 1000.0
        for mk in tr["cfg"]["markers"]:
            if mk["name"] not in tr["passed"] and cur_km >= mk["km"] - 0.05:
                t_pass = tr["history"][-1][0]
                for (t, i) in tr["history"]:
                    if route["cum"][i] / 1000.0 >= mk["km"] - 0.05:
                        t_pass = t
                        break
                tr["passed"][mk["name"]] = t_pass
    return len(added)


def eq_speed_now(tr):
    """Equivalent speed (eq-m/s): blend of rolling window and overall pace."""
    hist = tr["history"]
    route = tr["route"]
    if len(hist) < 2:
        return None, None, None
    t_now, i_now = hist[-1]
    t0, i0 = hist[0]
    total_span = t_now - t0
    if total_span < 60:
        return None, None, None
    v_overall = (route["eq"][i_now] - route["eq"][i0]) / total_span

    win = ROLLING_WINDOW_MIN * 60.0
    t_ref = t_now - win
    ref = hist[0]
    for h in hist:
        if h[0] <= t_ref:
            ref = h
        else:
            break
    span = t_now - ref[0]
    v_roll = (route["eq"][i_now] - route["eq"][ref[1]]) / span if span > 60 else v_overall

    w = min(1.0, span / win) * 0.75
    v = w * v_roll + (1 - w) * v_overall
    if v < 0.05:  # essentially stopped (VP break) -> anchor to overall pace
        v = max(v_overall, 0.05)
    return v, v_roll, v_overall


def build_status(tr):
    route = tr["route"]
    cfg = tr["cfg"]
    now = time.time()
    out = {"configured": True, "id": cfg["id"], "name": cfg["name"],
           "needs_link": not (cfg.get("session_id") or cfg.get("simulate")),
           "total_km": round(route["total_m"] / 1000.0, 1),
           "poll_interval": POLL_INTERVAL, "simulate": bool(cfg.get("simulate")),
           "poll_error": tr["poll_error"], "vps": []}

    runner = None
    if tr["history"]:
        t_last, i_last = tr["history"][-1]
        runner = {"lat": route["lat"][i_last], "lon": route["lon"][i_last],
                  "km": round(route["cum"][i_last] / 1000.0, 2),
                  "ele": round(route["ele"][i_last]), "t": t_last,
                  "stale_min": round((now - t_last) / 60.0, 1)}
    out["runner"] = runner

    v, v_roll, v_all = eq_speed_now(tr)
    out["pace"] = None
    if v_roll:
        out["pace"] = {
            "rolling_min_per_eqkm": round(1000.0 / v_roll / 60.0, 1) if v_roll > 0.05 else None,
            "overall_min_per_eqkm": round(1000.0 / v_all / 60.0, 1) if v_all and v_all > 0.05 else None}

    markers = list(cfg["markers"])
    if not any(abs(m["km"] * 1000 - route["total_m"]) < 200 for m in markers):
        markers.append({"name": "Finish", "km": round(route["total_m"] / 1000.0, 1)})

    i_now = tr["history"][-1][1] if tr["history"] else 0
    eq_now = route["eq"][i_now]
    for mk in sorted(markers, key=lambda m: m["km"]):
        vp_idx = idx_at_km(route, mk["km"])
        entry = {"name": mk["name"], "km": mk["km"],
                 "lat": route["lat"][vp_idx], "lon": route["lon"][vp_idx],
                 "ele": round(route["ele"][vp_idx])}
        if mk["name"] in tr["passed"]:
            entry["passed"] = True
            entry["passed_at"] = tr["passed"][mk["name"]]
        elif v and runner:
            rem_eq = max(0.0, route["eq"][vp_idx] - eq_now)
            t_sec = rem_eq / v
            t_sec *= 1.0 + DRIFT_PER_HOUR * (t_sec / 3600.0) / 2.0
            age = now - runner["t"]
            entry["passed"] = False
            entry["remaining_km"] = round(
                max(0.0, (route["cum"][vp_idx] - route["cum"][i_now]) / 1000.0), 1)
            entry["eta_epoch"] = runner["t"] + t_sec
            entry["eta_in_s"] = max(0, int(t_sec - age))
        else:
            entry["passed"] = False
        out["vps"].append(entry)
    return out


def build_summary(tr):
    """Compact status for the overview list."""
    cfg = tr["cfg"]
    s = {"id": cfg["id"], "name": cfg["name"],
         "needs_link": not (cfg.get("session_id") or cfg.get("simulate")),
         "simulate": bool(cfg.get("simulate")),
         "total_km": round(tr["route"]["total_m"] / 1000.0, 1),
         "poll_error": tr["poll_error"]}
    if tr["history"]:
        st = build_status(tr)
        s["runner_km"] = st["runner"]["km"] if st["runner"] else None
        s["stale_min"] = st["runner"]["stale_min"] if st["runner"] else None
        nxt = next((v for v in st["vps"] if not v["passed"] and v.get("eta_epoch")), None)
        if nxt:
            s["next"] = {"name": nxt["name"], "eta_epoch": nxt["eta_epoch"],
                         "eta_in_s": nxt["eta_in_s"]}
    return s


# ----------------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------------
def _check_tid(tid):
    """Reject any track id that isn't a plain hex token, so it can never be
    used to traverse outside DATA_DIR."""
    if not isinstance(tid, str) or not _TID_RE.fullmatch(tid):
        raise HTTPException(404, "Track not found")
    return tid


def gpx_path(tid):
    return DATA_DIR / f"{_check_tid(tid)}.gpx"


def live_path(tid):
    return DATA_DIR / f"{_check_tid(tid)}.track.json"


def save_meta():
    META_FILE.write_text(json.dumps(
        {"tracks": {tid: tr["cfg"] for tid, tr in STATE["tracks"].items()}}))


def save_live(tr):
    live_path(tr["cfg"]["id"]).write_text(json.dumps(
        {"track": tr["track"][-MAX_TRACKPOINTS:], "passed": tr["passed"]}))


def load_all():
    if not META_FILE.exists():
        return
    meta = json.loads(META_FILE.read_text())
    for tid, cfg in meta.get("tracks", {}).items():
        gp = gpx_path(tid)
        if not gp.exists():
            continue
        route = build_route(gp.read_bytes())
        tr = new_track(cfg, route)
        lp = live_path(tid)
        if lp.exists():
            saved = json.loads(lp.read_text())
            ingest_points(tr, saved.get("track", []))
            recompute_passed(tr)
        STATE["tracks"][tid] = tr


# ----------------------------------------------------------------------------
# Poller (+ simulator)
# ----------------------------------------------------------------------------
def simulate_points(tr):
    """Fake runner: ~9 min/eq-km from setup time, one point per poll."""
    route = tr["route"]
    t0 = tr["cfg"]["created"]
    now = time.time()
    v_eq = 1000.0 / (9.0 * 60.0)
    eq_arr = route["eq"]
    pts = []
    t = tr["track"][-1]["t"] + POLL_INTERVAL if tr["track"] else t0
    while t <= now:
        eq_target = (t - t0) * v_eq
        lo, hi = 0, len(eq_arr) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if eq_arr[mid] < eq_target:
                lo = mid + 1
            else:
                hi = mid
        pts.append({"t": t, "lat": route["lat"][lo], "lon": route["lon"][lo],
                    "ele": route["ele"][lo]})
        t += POLL_INTERVAL
    return pts


async def poller():
    while True:
        async with LOCK:
            items = list(STATE["tracks"].items())
        for tid, tr in items:
            cfg = tr["cfg"]
            if not (cfg.get("session_id") or cfg.get("simulate")):
                continue
            try:
                if cfg.get("simulate"):
                    pts = simulate_points(tr)
                else:
                    pts = await fetch_trackpoints(cfg["session_id"], cfg.get("token"))
                async with LOCK:
                    ingest_points(tr, pts)
                    tr["poll_error"] = None
                    tr["last_poll_ok"] = time.time()
                    save_live(tr)
            except Exception as e:  # noqa: BLE001
                async with LOCK:
                    # Keep it short and single-line; this value is served publicly.
                    msg = (str(e) or e.__class__.__name__).splitlines()[0]
                    tr["poll_error"] = msg[:200]
        await asyncio.sleep(POLL_INTERVAL)


@app.on_event("startup")
async def _startup():
    try:
        load_all()
    except Exception:  # noqa: BLE001
        pass
    asyncio.create_task(poller())


# ----------------------------------------------------------------------------
# Auth (admin, HTTP Basic)
# ----------------------------------------------------------------------------
async def require_admin(authorization: str = Header(None)):
    # Refuse to expose admin at all while the well-known default password is in
    # place — forces operators to set a real ADMIN_PASSWORD before use.
    if not ADMIN_PASSWORD or ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD:
        raise HTTPException(
            503, detail="Admin disabled: set a strong ADMIN_PASSWORD env var")
    unauth = HTTPException(
        401, detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="crew-eta admin"'})
    if not authorization or not authorization.lower().startswith("basic "):
        raise unauth
    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
    except Exception:  # noqa: BLE001
        await asyncio.sleep(0.5)  # slow down credential guessing
        raise unauth
    pw = decoded.split(":", 1)[1] if ":" in decoded else decoded
    if not secrets.compare_digest(pw, ADMIN_PASSWORD):
        await asyncio.sleep(0.5)  # slow down credential guessing
        raise unauth
    return True


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def parse_markers(text):
    markers = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"[,;]\s*|\s+", line, maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"Cannot read line: '{line}' (format: VP1, 12.0)")
        name, km_s = parts[0], parts[1]
        try:
            km = float(km_s.replace(",", "."))
        except ValueError:
            try:  # maybe swapped: "12.0 VP1"
                km = float(name.replace(",", "."))
                name = km_s
            except ValueError:
                raise ValueError(f"Cannot read km value in: '{line}'")
        markers.append({"name": name.strip(), "km": km})
    if not markers:
        raise ValueError("No markers given")
    if len(markers) > MAX_MARKERS:
        raise ValueError(f"Too many markers (max {MAX_MARKERS})")
    return sorted(markers, key=lambda m: m["km"])


_ALLOWED_GPX_CT = {"", "application/gpx+xml", "application/xml", "text/xml",
                   "application/octet-stream", "text/plain"}


async def read_gpx_upload(upload):
    """Read an uploaded GPX safely: enforce extension, content-type and a hard
    size cap while streaming so a huge upload can't exhaust memory."""
    filename = upload.filename or ""
    if not filename.lower().endswith(".gpx"):
        raise HTTPException(400, "Only .gpx files are allowed")
    if (upload.content_type or "").lower() not in _ALLOWED_GPX_CT:
        raise HTTPException(400, "Unexpected content type for a GPX file")
    data = bytearray()
    while True:
        chunk = await upload.read(65536)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_GPX_BYTES:
            raise HTTPException(
                413, f"GPX file too large (max {MAX_GPX_BYTES // (1024 * 1024)} MB)")
    if not data:
        raise HTTPException(400, "Empty GPX file")
    return bytes(data)


def get_track(tid):
    _check_tid(tid)
    tr = STATE["tracks"].get(tid)
    if not tr:
        raise HTTPException(404, "Track not found")
    return tr


# ----------------------------------------------------------------------------
# Admin API (Basic auth via Depends)
# ----------------------------------------------------------------------------
@app.post("/tracking/admin/api/create")
async def admin_create(
    _ok: bool = Depends(require_admin),
    name: str = Form(...),
    markers: str = Form(...),
    simulate: str = Form(""),
    gpx: UploadFile = File(...),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name is required")
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(400, "Name is too long")
    if len(markers) > MAX_MARKERS_LEN:
        raise HTTPException(400, "Markers text is too long")
    sim = simulate in ("1", "on", "true")
    gpx_bytes = await read_gpx_upload(gpx)
    try:
        route = build_route(gpx_bytes)
        mks = parse_markers(markers)
    except ValueError as e:
        raise HTTPException(400, str(e))
    total_km = route["total_m"] / 1000.0
    for m in mks:
        if m["km"] > total_km + 1:
            raise HTTPException(400, f"{m['name']} at km {m['km']} is past the "
                                     f"end of the route ({total_km:.1f} km)")
    tid = secrets.token_hex(4)
    cfg = {"id": tid, "name": name, "markers": mks, "simulate": sim,
           "livetrack_url": None, "session_id": None, "token": None,
           "created": time.time()}
    async with LOCK:
        gpx_path(tid).write_bytes(gpx_bytes)
        STATE["tracks"][tid] = new_track(cfg, route)
        save_meta()
    return {"ok": True, "id": tid, "total_km": round(total_km, 1),
            "markers": mks}


@app.post("/tracking/admin/api/update")
async def admin_update(
    _ok: bool = Depends(require_admin),
    id: str = Form(...),
    name: str = Form(...),
    markers: str = Form(...),
    simulate: str = Form(""),
    gpx: UploadFile = File(None),
):
    _check_tid(id)
    if len(name) > MAX_NAME_LEN:
        raise HTTPException(400, "Name is too long")
    if len(markers) > MAX_MARKERS_LEN:
        raise HTTPException(400, "Markers text is too long")
    new_gpx = None
    if gpx is not None and (gpx.filename or "").strip():
        new_gpx = await read_gpx_upload(gpx)
    async with LOCK:
        tr = STATE["tracks"].get(id)
        if not tr:
            raise HTTPException(404, "Track not found")
        try:
            mks = parse_markers(markers)
        except ValueError as e:
            raise HTTPException(400, str(e))
        name = name.strip()
        if not name:
            raise HTTPException(400, "Name is required")
        if new_gpx:
            try:
                route = build_route(new_gpx)
            except ValueError as e:
                raise HTTPException(400, str(e))
            gpx_path(id).write_bytes(new_gpx)
            # route changed -> discard live state (projection invalid)
            tr = new_track(tr["cfg"], route)
            STATE["tracks"][id] = tr
        total_km = tr["route"]["total_m"] / 1000.0
        for m in mks:
            if m["km"] > total_km + 1:
                raise HTTPException(400, f"{m['name']} at km {m['km']} is past the "
                                         f"end of the route ({total_km:.1f} km)")
        tr["cfg"]["name"] = name
        tr["cfg"]["markers"] = mks
        tr["cfg"]["simulate"] = simulate in ("1", "on", "true")
        recompute_passed(tr)
        save_meta()
        save_live(tr)
    return {"ok": True, "id": id, "total_km": round(total_km, 1)}


@app.post("/tracking/admin/api/delete")
async def admin_delete(_ok: bool = Depends(require_admin), id: str = Form(...)):
    _check_tid(id)
    async with LOCK:
        if id in STATE["tracks"]:
            del STATE["tracks"][id]
        gpx_path(id).unlink(missing_ok=True)
        live_path(id).unlink(missing_ok=True)
        save_meta()
    return {"ok": True}


@app.get("/tracking/admin/api/tracks")
async def admin_tracks(_ok: bool = Depends(require_admin)):
    async with LOCK:
        out = []
        for tr in STATE["tracks"].values():
            c = tr["cfg"]
            out.append({"id": c["id"], "name": c["name"], "markers": c["markers"],
                        "simulate": bool(c.get("simulate")),
                        "has_link": bool(c.get("session_id")),
                        "livetrack_url": c.get("livetrack_url"),
                        "total_km": round(tr["route"]["total_m"] / 1000.0, 1)})
        out.sort(key=lambda t: t["name"].lower())
    return {"tracks": out}


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------
@app.get("/tracking/api/tracks")
async def api_tracks():
    async with LOCK:
        out = [build_summary(tr) for tr in STATE["tracks"].values()]
        out.sort(key=lambda t: t["name"].lower())
    return {"tracks": out}


@app.post("/tracking/api/link")
async def api_link(id: str = Form(...), livetrack: str = Form(...)):
    """Set/change the Garmin link — intentionally open (first visitor fills it)."""
    _check_tid(id)
    if len(livetrack) > MAX_LIVETRACK_LEN:
        raise HTTPException(400, "LiveTrack link is too long")
    try:
        sid, tok, canonical = await resolve_livetrack(livetrack)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach Garmin to resolve the LiveTrack link")
    async with LOCK:
        tr = STATE["tracks"].get(id)
        if not tr:
            raise HTTPException(404, "Track not found")
        changed = tr["cfg"].get("session_id") != sid
        tr["cfg"]["livetrack_url"] = canonical
        tr["cfg"]["session_id"] = sid
        tr["cfg"]["token"] = tok
        if changed:  # different link -> reset live data
            tr["track"] = []
            tr["history"] = []
            tr["last_idx"] = 0
            tr["passed"] = {}
            tr["poll_error"] = None
            live_path(id).unlink(missing_ok=True)
        save_meta()
    return {"ok": True}


@app.get("/tracking/api/status/{tid}")
async def api_status(tid: str):
    async with LOCK:
        tr = get_track(tid)
        return JSONResponse(build_status(tr))


@app.get("/tracking/api/route/{tid}")
async def api_route(tid: str):
    async with LOCK:
        tr = get_track(tid)
        route = tr["route"]
        n = len(route["lat"])
        step = max(1, n // 1500)
        line = [[round(route["lat"][i], 5), round(route["lon"][i], 5)]
                for i in range(0, n, step)]
        last = [round(route["lat"][-1], 5), round(route["lon"][-1], 5)]
        if line[-1] != last:
            line.append(last)
        return JSONResponse({"line": line})


@app.get("/tracking/healthz")
async def healthz():
    return {"ok": True}


# ----------------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------------
@app.get("/tracking", response_class=HTMLResponse)
@app.get("/tracking/", response_class=HTMLResponse)
async def page_list():
    return HTMLResponse(LIST_HTML)


@app.get("/tracking/t/{tid}", response_class=HTMLResponse)
async def page_dashboard(tid: str):
    if tid not in STATE["tracks"]:
        return HTMLResponse(NOTFOUND_HTML, status_code=404)
    return HTMLResponse(DASH_HTML)


@app.get("/tracking/admin", response_class=HTMLResponse)
async def page_admin(_ok: bool = Depends(require_admin)):
    return HTMLResponse(ADMIN_HTML)


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
COMMON_CSS = """
:root{
  --bg:#101418;--panel:#181e24;--line:#232b33;--txt:#e8e4da;--dim:#8a939c;
  --amber:#f5a623;--green:#59c27a;--red:#e05c5c;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);
  font-family:'Barlow',system-ui,sans-serif;min-height:100vh}
a{color:var(--amber);text-decoration:none}
.mono{font-family:'IBM Plex Mono',ui-monospace,monospace}
header{padding:14px 16px;border-bottom:1px solid var(--line);
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
header h1{font-size:15px;letter-spacing:.14em;text-transform:uppercase;font-weight:600}
header h1 a{color:var(--txt)}
header .sub{color:var(--dim);font-size:12px}
.wrap{max-width:880px;margin:0 auto;padding:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:14px;margin-bottom:14px}
label{display:block;font-size:12px;color:var(--dim);text-transform:uppercase;
  letter-spacing:.08em;margin:12px 0 4px}
input,textarea{width:100%;background:#0c1013;border:1px solid var(--line);
  border-radius:6px;color:var(--txt);padding:9px;font-size:15px}
textarea{min-height:96px;font-family:ui-monospace,monospace}
button{background:var(--amber);color:#151005;border:0;border-radius:6px;
  padding:10px 16px;font-size:15px;font-weight:700;cursor:pointer}
button.ghost{background:#2a3138;color:var(--txt)}
button.danger{background:#3a2226;color:var(--red)}
.msg{margin-top:10px;font-size:14px}
.msg.err{color:var(--red)}.msg.ok{color:var(--green)}
.dim{color:var(--dim)}
/* Pulsing runner beacon on the map */
.runner-beacon{position:relative}
.runner-beacon .core{position:absolute;left:50%;top:50%;width:16px;height:16px;
  margin:-8px 0 0 -8px;border-radius:50%;background:#ff3b30;
  border:3px solid #fff;box-shadow:0 0 8px 2px rgba(255,59,48,.9);z-index:2}
.runner-beacon .pulse{position:absolute;left:50%;top:50%;width:16px;height:16px;
  margin:-8px 0 0 -8px;border-radius:50%;background:rgba(255,59,48,.55);z-index:1;
  animation:runnerPulse 1.5s ease-out infinite}
.runner-beacon .pulse.d{animation-delay:.75s}
@keyframes runnerPulse{0%{transform:scale(1);opacity:.8}
  100%{transform:scale(4.5);opacity:0}}
@media (prefers-reduced-motion:reduce){.runner-beacon .pulse{animation:none;opacity:0}}
"""

# ---------- List ----------
LIST_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>crew-eta</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet">
<style>""" + COMMON_CSS + """
.trk{display:flex;justify-content:space-between;align-items:center;gap:12px;
  padding:14px 4px;border-top:1px solid var(--line);flex-wrap:wrap}
.trk:first-child{border-top:0}
.trk .name{font-size:18px;font-weight:600}
.trk .meta{color:var(--dim);font-size:13px;margin-top:2px}
.trk .go{white-space:nowrap}
.linkbox{display:flex;gap:8px;width:100%;margin-top:8px}
.linkbox input{flex:1}
.empty{color:var(--dim);padding:24px 4px}
</style></head><body>
<header><h1>crew-eta</h1><span class="sub">live tracking</span></header>
<div class="wrap"><div class="card" id="list"><div class="empty">loading …</div></div>
<div class="dim" style="font-size:12px">Create tracks in the <a href="/tracking/admin">admin area</a></div>
</div>
<script>
const fmtClock=e=>new Date(e*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
async function load(){
  let d;try{d=await(await fetch('/tracking/api/tracks')).json()}catch{return}
  const box=document.getElementById('list');
  if(!d.tracks.length){box.innerHTML='<div class="empty">No tracks yet. Create one in the <a href="/tracking/admin">admin area</a>.</div>';return}
  box.innerHTML='';
  for(const t of d.tracks){
    const row=document.createElement('div');row.className='trk';
    if(t.needs_link){
      row.innerHTML=`<div style="width:100%">
        <div class="name">${esc(t.name)}</div>
        <div class="meta">${t.total_km} km · Garmin LiveTrack link missing</div>
        <div class="linkbox">
          <input placeholder="Paste Garmin LiveTrack link here" data-id="${t.id}">
          <button data-set="${t.id}">Save</button>
        </div>
        <div class="msg" data-msg="${t.id}"></div>
      </div>`;
    }else{
      let meta=`${t.total_km} km`;
      if(t.simulate)meta+=' · SIMULATION';
      if(t.runner_km!=null)meta+=` · at km ${t.runner_km.toFixed(1)}`;
      if(t.next)meta+=` · ${esc(t.next.name)} ~${fmtClock(t.next.eta_epoch)}`;
      if(t.poll_error)meta+=` · ⚠ ${esc(t.poll_error)}`;
      row.innerHTML=`<div>
        <div class="name"><a href="/tracking/t/${t.id}">${esc(t.name)}</a></div>
        <div class="meta">${meta}</div></div>
        <div class="go"><a href="/tracking/t/${t.id}"><button>open →</button></a></div>`;
    }
    box.appendChild(row);
  }
  box.querySelectorAll('[data-set]').forEach(b=>b.addEventListener('click',async()=>{
    const id=b.getAttribute('data-set');
    const inp=box.querySelector(`input[data-id="${id}"]`);
    const msg=box.querySelector(`[data-msg="${id}"]`);
    const fd=new FormData();fd.set('id',id);fd.set('livetrack',inp.value.trim());
    const r=await fetch('/tracking/api/link',{method:'POST',body:fd});
    if(r.ok){location.href='/tracking/t/'+id}
    else{const e=await r.json().catch(()=>({}));msg.textContent=e.detail||'Error';msg.className='msg err'}
  }));
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
load();setInterval(load,60000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load()});
</script></body></html>"""

# ---------- Dashboard ----------
DASH_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>crew-eta</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>""" + COMMON_CSS + """
#map{height:44vh;min-height:280px;border-radius:10px;border:1px solid var(--line)}
.status{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--dim);padding:10px 2px}
.status b{color:var(--txt);font-weight:600}
.status .warn{color:var(--red)}
.next{border-color:var(--amber)}
.next .eta{font-size:44px;line-height:1.05;color:var(--amber)}
.next .nm{color:var(--amber)}
table{width:100%;border-collapse:collapse}
td,th{padding:10px 8px;text-align:left;border-top:1px solid var(--line);font-size:15px}
th{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;border-top:0}
td.eta-cell{font-size:20px}
tr.passed td{color:var(--dim)}
tr.passed .nm::after{content:" ✓";color:var(--green)}
.cd{color:var(--dim);font-size:13px}
.nm{font-weight:600}
.linkrow{display:flex;gap:8px;margin-top:10px}.linkrow input{flex:1}
.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
</style></head><body>
<header><h1><a href="/tracking/">crew-eta</a></h1><span class="sub" id="hdr">loading …</span></header>
<div class="wrap">
  <div id="needlink" class="card" style="display:none">
    <b>Garmin LiveTrack link missing.</b>
    <div class="dim" style="font-size:13px;margin-top:4px">Paste the link from Garmin (available once the activity has started) — tracking then runs automatically.</div>
    <div class="linkrow"><input id="link1" placeholder="https://gar.mn/… or livetrack.garmin.com/session/…">
      <button id="save1">Save</button></div>
    <div class="msg" id="msg1"></div>
  </div>
  <div id="live" style="display:none">
    <div id="map"></div>
    <div class="status" id="stat"></div>
    <div class="card next" id="nextcard" style="display:none">
      <div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)">Next point</div>
      <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-top:6px">
        <span class="nm" id="n_name" style="font-size:22px"></span>
        <span class="eta mono" id="n_eta"></span>
        <span class="cd" id="n_cd"></span>
      </div>
    </div>
    <div class="card">
      <table><thead><tr><th>Point</th><th>km</th><th>remaining</th><th>ETA</th></tr></thead>
      <tbody id="rows"></tbody></table>
      <div class="toolbar">
        <button class="ghost" id="editlink">Change Garmin link</button>
        <div id="editbox" style="display:none;flex:1;min-width:240px">
          <div class="linkrow"><input id="link2" placeholder="New LiveTrack link">
            <button id="save2">Apply</button></div>
          <div class="msg" id="msg2"></div>
        </div>
      </div>
    </div>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const TID=location.pathname.split('/').filter(Boolean).pop();
const fmtClock=e=>new Date(e*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
const fmtCd=s=>{if(s<0)s=0;const h=Math.floor(s/3600),m=Math.round(s%3600/60);
  return h>0?`in ${h} h ${m} min`:`in ${m} min`};
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

async function saveLink(inputEl,msgEl){
  const fd=new FormData();fd.set('id',TID);fd.set('livetrack',inputEl.value.trim());
  const r=await fetch('/tracking/api/link',{method:'POST',body:fd});
  if(r.ok){msgEl.textContent='Saved';msgEl.className='msg ok';
    mapReady=false;if(map){map.remove();map=null;runnerMarker=null}
    setTimeout(refresh,300)}
  else{const e=await r.json().catch(()=>({}));msgEl.textContent=e.detail||'Error';msgEl.className='msg err'}
}
document.getElementById('save1').addEventListener('click',()=>
  saveLink(document.getElementById('link1'),document.getElementById('msg1')));
document.getElementById('save2').addEventListener('click',()=>
  saveLink(document.getElementById('link2'),document.getElementById('msg2')));
document.getElementById('editlink').addEventListener('click',()=>{
  const b=document.getElementById('editbox');b.style.display=b.style.display==='none'?'block':'none'});

let map,runnerMarker,vpLayer,mapReady=false;
async function initMap(){
  let r;try{r=await(await fetch('/tracking/api/route/'+TID)).json()}catch{return}
  if(!r.line)return;
  map=L.map('map',{zoomControl:true});
  // Satellite/terrain you can actually read: imagery with roads, place names,
  // forests and buildings — like Google's hybrid view. Esri World Imagery +
  // reference labels needs no API key; a Google hybrid layer and the dark
  // street map are offered in the layer switcher (top-right).
  const esriSat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {attribution:'&copy; Esri, Maxar, Earthstar Geographics',maxZoom:19});
  const esriLabels=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
    {maxZoom:19});
  const esriRoads=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}',
    {maxZoom:19});
  const hybrid=L.layerGroup([esriSat,esriRoads,esriLabels]);
  const google=L.tileLayer('https://mt{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
    {subdomains:['0','1','2','3'],attribution:'&copy; Google',maxZoom:20});
  const dark=L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    {attribution:'&copy; OpenStreetMap &copy; CARTO',maxZoom:18});
  hybrid.addTo(map);
  L.control.layers({'Satellite (hybrid)':hybrid,'Google hybrid':google,'Dark street':dark},
    null,{position:'topright'}).addTo(map);
  // Draw a dark halo first, then the bright route on top, so the line stays
  // legible over imagery (drawing order = stacking; no bringToBack, which throws
  // before the map has a view).
  L.polyline(r.line,{color:'#000',weight:7,opacity:.35}).addTo(map);
  const line=L.polyline(r.line,{color:'#ffd21e',weight:4,opacity:.95}).addTo(map);
  const fit=()=>map.fitBounds(line.getBounds(),{padding:[20,20],maxZoom:16});
  fit();
  // On mobile the container is often sized a tick after we build the map, which
  // would otherwise leave it zoomed to a corner — re-measure and re-fit once.
  setTimeout(()=>{try{map.invalidateSize();fit()}catch(e){}},250);
  vpLayer=L.layerGroup().addTo(map);mapReady=true;
}
function vpIcon(p){return L.divIcon({className:'',iconSize:[16,16],
  html:`<div style="width:12px;height:12px;border-radius:50%;border:2px solid #000;box-shadow:0 0 0 1px ${p?'#59c27a':'#fff'};background:${p?'#59c27a':'#fff'}"></div>`})}
// Runner: a loud, pulsing beacon so the eye locks onto it instantly.
const runnerIcon=L.divIcon({className:'runner-beacon',iconSize:[28,28],iconAnchor:[14,14],
  html:'<span class="pulse"></span><span class="pulse d"></span><span class="core"></span>'});

async function refresh(){
  let d;try{const rr=await fetch('/tracking/api/status/'+TID);if(rr.status===404){document.getElementById('hdr').textContent='Track not found';return}d=await rr.json()}catch{return}
  document.getElementById('hdr').textContent=`${esc(d.name)} · ${d.total_km} km${d.simulate?' · SIMULATION':''}`;
  document.title=d.name+' · crew-eta';
  if(d.needs_link){
    document.getElementById('needlink').style.display='block';
    document.getElementById('live').style.display='none';return;
  }
  document.getElementById('needlink').style.display='none';
  document.getElementById('live').style.display='block';
  if(!mapReady){await initMap()}

  const st=document.getElementById('stat');let s='';
  if(d.runner){
    s+=`<span>Position <b>km ${d.runner.km.toFixed(1)}</b> · ${d.runner.ele} m</span>`;
    s+=`<span>Updated <b>${d.runner.stale_min<1?'&lt;1':Math.round(d.runner.stale_min)} min</b> ago</span>`;
    if(d.pace&&d.pace.rolling_min_per_eqkm)s+=`<span>GAP pace <b>${d.pace.rolling_min_per_eqkm} min/km</b></span>`;
    if(d.runner.stale_min>10)s+=`<span class="warn">⚠ No signal for ${Math.round(d.runner.stale_min)} min</span>`;
  }else s+='<span>Waiting for first LiveTrack data …</span>';
  if(d.poll_error)s+=`<span class="warn">⚠ ${esc(d.poll_error)}</span>`;
  st.innerHTML=s;

  const rows=document.getElementById('rows');rows.innerHTML='';
  let nextVp=null;
  if(vpLayer)vpLayer.clearLayers();
  for(const vp of d.vps){
    if(vpLayer)L.marker([vp.lat,vp.lon],{icon:vpIcon(vp.passed)}).bindTooltip(`${esc(vp.name)} · km ${vp.km}`).addTo(vpLayer);
    const tr=document.createElement('tr');
    if(vp.passed){tr.className='passed';
      tr.innerHTML=`<td class="nm">${esc(vp.name)}</td><td>${vp.km}</td><td>—</td><td class="mono">${vp.passed_at?fmtClock(vp.passed_at):''}</td>`;}
    else if(vp.eta_epoch){if(!nextVp)nextVp=vp;
      tr.innerHTML=`<td class="nm">${esc(vp.name)}</td><td>${vp.km}</td><td>${vp.remaining_km} km</td>
        <td class="eta-cell mono">${fmtClock(vp.eta_epoch)} <span class="cd">${fmtCd(vp.eta_in_s)}</span></td>`;}
    else{tr.innerHTML=`<td class="nm">${esc(vp.name)}</td><td>${vp.km}</td><td>—</td><td>—</td>`;}
    rows.appendChild(tr);
  }
  const nc=document.getElementById('nextcard');
  if(nextVp){nc.style.display='block';
    document.getElementById('n_name').textContent=`${nextVp.name} · km ${nextVp.km}`;
    document.getElementById('n_eta').textContent=fmtClock(nextVp.eta_epoch);
    document.getElementById('n_cd').textContent=`${fmtCd(nextVp.eta_in_s)} · ${nextVp.remaining_km} km to go`;
  }else nc.style.display='none';
  if(map&&d.runner){
    if(!runnerMarker)runnerMarker=L.marker([d.runner.lat,d.runner.lon],{icon:runnerIcon}).addTo(map);
    else runnerMarker.setLatLng([d.runner.lat,d.runner.lon]);
  }
}
refresh();
setInterval(refresh,60000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh()});
</script></body></html>"""

# ---------- Admin ----------
ADMIN_HTML = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>crew-eta · admin</title>
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;600;700&family=IBM+Plex+Mono:wght@500&display=swap" rel="stylesheet">
<style>""" + COMMON_CSS + """
.trk{border-top:1px solid var(--line);padding:12px 2px}
.trk:first-child{border-top:0}
.trk h3{font-size:16px;display:flex;align-items:center;gap:8px}
.tag{font-size:11px;padding:2px 7px;border-radius:20px;background:#0c1013;color:var(--dim);border:1px solid var(--line)}
.tag.ok{color:var(--green);border-color:#204a30}
.tag.warn{color:var(--amber);border-color:#4a3a10}
.trk .meta{color:var(--dim);font-size:13px;margin:4px 0}
.trk .acts{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.edit{margin-top:10px;display:none}
</style></head><body>
<header><h1><a href="/tracking/">crew-eta</a> · admin</h1></header>
<div class="wrap">
  <div class="card">
    <h2 style="font-size:15px;margin-bottom:6px">Create a track</h2>
    <form id="create">
      <label>Name (shown in the list)</label>
      <input name="name" placeholder="e.g. GTCB Finestrat 102K" required>
      <label>GPX route</label>
      <input type="file" name="gpx" accept=".gpx" required>
      <label>Aid stations — one per line: name, km</label>
      <textarea name="markers" placeholder="VP1, 12&#10;VP2, 33.3&#10;VP3, 58.5"></textarea>
      <label style="display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0">
        <input type="checkbox" name="simulate" value="1" style="width:auto">
        Simulation (test run without a Garmin link)
      </label>
      <button type="submit" style="margin-top:14px">Create</button>
      <div class="msg" id="cmsg"></div>
    </form>
  </div>
  <div class="card">
    <h2 style="font-size:15px;margin-bottom:6px">Existing tracks</h2>
    <div id="list"><div class="dim">loading …</div></div>
  </div>
</div>
<script>
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function mkText(mks){return mks.map(m=>`${m.name}, ${m.km}`).join('\\n')}

document.getElementById('create').addEventListener('submit',async e=>{
  e.preventDefault();const f=e.target,msg=document.getElementById('cmsg');
  msg.textContent='Loading…';msg.className='msg';
  const r=await fetch('/tracking/admin/api/create',{method:'POST',body:new FormData(f)});
  const d=await r.json().catch(()=>({detail:'Server error'}));
  if(r.ok){msg.textContent=`Created — ${d.total_km} km, ${d.markers.length} markers`;
    msg.className='msg ok';f.reset();load()}
  else{msg.textContent=d.detail||'Error';msg.className='msg err'}
});

async function load(){
  let d;try{d=await(await fetch('/tracking/admin/api/tracks')).json()}catch{return}
  const box=document.getElementById('list');
  if(!d.tracks.length){box.innerHTML='<div class="dim">No tracks yet.</div>';return}
  box.innerHTML='';
  for(const t of d.tracks){
    const el=document.createElement('div');el.className='trk';
    const tag=t.simulate?'<span class="tag">SIM</span>':
      (t.has_link?'<span class="tag ok">link active</span>':'<span class="tag warn">waiting for link</span>');
    el.innerHTML=`
      <h3>${esc(t.name)} ${tag}</h3>
      <div class="meta">${t.total_km} km · ${t.markers.length} markers · <a href="/tracking/t/${t.id}">open</a></div>
      <div class="acts">
        <button class="ghost" data-edit="${t.id}">Edit</button>
        <button class="danger" data-del="${t.id}" data-name="${esc(t.name)}">Delete</button>
      </div>
      <div class="edit" id="edit-${t.id}">
        <label>Name</label><input id="n-${t.id}" value="${esc(t.name)}">
        <label>Markers</label><textarea id="m-${t.id}">${esc(mkText(t.markers))}</textarea>
        <label>Replace GPX (optional)</label><input type="file" id="g-${t.id}" accept=".gpx">
        <label style="display:flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0">
          <input type="checkbox" id="s-${t.id}" ${t.simulate?'checked':''} style="width:auto"> Simulation</label>
        <button data-save="${t.id}" style="margin-top:12px">Save</button>
        <div class="msg" id="em-${t.id}"></div>
      </div>`;
    box.appendChild(el);
  }
  box.querySelectorAll('[data-edit]').forEach(b=>b.addEventListener('click',()=>{
    const e=document.getElementById('edit-'+b.getAttribute('data-edit'));
    e.style.display=e.style.display==='block'?'none':'block'}));
  box.querySelectorAll('[data-del]').forEach(b=>b.addEventListener('click',async()=>{
    const id=b.getAttribute('data-del');
    if(!confirm(`Delete track "${b.getAttribute('data-name')}"?`))return;
    const fd=new FormData();fd.set('id',id);
    const r=await fetch('/tracking/admin/api/delete',{method:'POST',body:fd});
    if(r.ok)load()}));
  box.querySelectorAll('[data-save]').forEach(b=>b.addEventListener('click',async()=>{
    const id=b.getAttribute('data-save'),msg=document.getElementById('em-'+id);
    const fd=new FormData();
    fd.set('id',id);
    fd.set('name',document.getElementById('n-'+id).value);
    fd.set('markers',document.getElementById('m-'+id).value);
    if(document.getElementById('s-'+id).checked)fd.set('simulate','1');
    const gf=document.getElementById('g-'+id).files[0];if(gf)fd.set('gpx',gf);
    msg.textContent='Loading…';msg.className='msg';
    const r=await fetch('/tracking/admin/api/update',{method:'POST',body:fd});
    const d=await r.json().catch(()=>({detail:'Server error'}));
    if(r.ok){msg.textContent='Saved';msg.className='msg ok';load()}
    else{msg.textContent=d.detail||'Error';msg.className='msg err'}
  }));
}
load();
</script></body></html>"""

NOTFOUND_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Not found</title>
<style>""" + COMMON_CSS + """.wrap{padding-top:60px;text-align:center}</style></head><body>
<div class="wrap"><h1 style="font-size:20px;margin-bottom:10px">Track not found</h1>
<p class="dim">It may have been deleted in the admin area.</p>
<p style="margin-top:16px"><a href="/tracking/">← back to overview</a></p></div></body></html>"""
