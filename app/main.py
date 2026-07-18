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
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme-in-env")
OFFROUTE_MAX_M = 400.0
MAX_TRACKPOINTS = 60000

DATA_DIR.mkdir(parents=True, exist_ok=True)
META_FILE = DATA_DIR / "tracks.json"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

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


def grade_factor(g):
    """Pace multiplier vs. flat (Minetti-style, tuned for trail).
    +10% uphill ~1.4x, +20% ~2.1x, -10% ~0.9x, steep downhill slow again."""
    g = max(-0.35, min(0.35, g))
    m = 1.0 + 2.6 * g + 15.0 * g * g
    return max(0.7, min(4.0, m))


def build_route(gpx_bytes):
    gpx = gpxpy.parse(gpx_bytes.decode("utf-8", errors="replace"))
    pts = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            pts.extend(seg.points)
    if not pts:
        for rte in gpx.routes:
            pts.extend(rte.points)
    if len(pts) < 2:
        raise ValueError("GPX contains no route")

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


def project(route, lat, lon, last_idx):
    """Project a position onto the route. Searches forward from the last known
    index so crossings/switchbacks don't snap backwards."""
    n = len(route["lat"])
    start = max(0, last_idx - 150)
    end = min(n, last_idx + 4000)
    best_i, best_d = None, 1e18
    for i in range(start, end):
        d = haversine(lat, lon, route["lat"][i], route["lon"][i])
        if d < best_d:
            best_d, best_i = d, i
    if best_d > OFFROUTE_MAX_M:
        # global fallback search (e.g. restart mid-race)
        for i in range(0, n, 5):
            d = haversine(lat, lon, route["lat"][i], route["lon"][i])
            if d < best_d:
                best_d, best_i = d, i
        if best_d > OFFROUTE_MAX_M:
            return None
    return max(best_i, last_idx)  # keep progress monotonic


# ----------------------------------------------------------------------------
# LiveTrack
# ----------------------------------------------------------------------------
def parse_livetrack_url(url):
    m = re.search(r"livetrack\.garmin\.com/session/([0-9a-fA-F-]+)", url)
    if not m:
        raise ValueError("Not a valid LiveTrack link (expected .../session/<id>/...)")
    sid = m.group(1)
    tok = None
    mt = re.search(r"/token/([A-Za-z0-9_-]+)", url)
    if mt:
        tok = mt.group(1)
    else:
        mq = re.search(r"[?&]token=([A-Za-z0-9_-]+)", url)
        if mq:
            tok = mq.group(1)
    return sid, tok


async def fetch_trackpoints(sid, tok):
    ms = int(time.time() * 1000)
    candidates = []
    if tok:
        candidates.append(
            f"https://livetrack.garmin.com/services/session/{sid}/token/{tok}/trackpoints?requestTime={ms}")
        candidates.append(
            f"https://livetrack.garmin.com/services/session/{sid}/trackpoints?requestTime={ms}&token={tok}")
    candidates.append(
        f"https://livetrack.garmin.com/services/session/{sid}/trackpoints?requestTime={ms}")

    headers = {"accept": "application/json",
               "user-agent": "Mozilla/5.0 (crew-eta-dashboard)"}
    last_err = None
    async with httpx.AsyncClient(timeout=20) as client:
        for url in candidates:
            try:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
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
                last_err = f"HTTP {r.status_code}"
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
    raise RuntimeError(last_err or "LiveTrack unreachable")


# ----------------------------------------------------------------------------
# ETA engine (per track)
# ----------------------------------------------------------------------------
def new_track(cfg, route):
    return {"cfg": cfg, "route": route, "track": [], "history": [],
            "last_idx": 0, "passed": {}, "poll_error": None, "last_poll_ok": None}


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
        idx = project(route, p["lat"], p["lon"], tr["last_idx"])
        if idx is not None:
            tr["last_idx"] = idx
            tr["history"].append((p["t"], idx))
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
def gpx_path(tid):
    return DATA_DIR / f"{tid}.gpx"


def live_path(tid):
    return DATA_DIR / f"{tid}.track.json"


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
                    tr["poll_error"] = str(e)
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
def require_admin(authorization: str = Header(None)):
    unauth = HTTPException(
        401, detail="Authentication required",
        headers={"WWW-Authenticate": 'Basic realm="crew-eta admin"'})
    if not authorization or not authorization.lower().startswith("basic "):
        raise unauth
    try:
        decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
    except Exception:  # noqa: BLE001
        raise unauth
    pw = decoded.split(":", 1)[1] if ":" in decoded else decoded
    if not secrets.compare_digest(pw, ADMIN_PASSWORD):
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
    return sorted(markers, key=lambda m: m["km"])


def get_track(tid):
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
    sim = simulate in ("1", "on", "true")
    gpx_bytes = await gpx.read()
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
        new_gpx = None
        if gpx is not None:
            new_gpx = await gpx.read()
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
    try:
        sid, tok = parse_livetrack_url(livetrack)
    except ValueError as e:
        raise HTTPException(400, str(e))
    async with LOCK:
        tr = STATE["tracks"].get(id)
        if not tr:
            raise HTTPException(404, "Track not found")
        changed = tr["cfg"].get("session_id") != sid
        tr["cfg"]["livetrack_url"] = livetrack
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
    <div class="linkrow"><input id="link1" placeholder="https://livetrack.garmin.com/session/…/token/…">
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
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    {attribution:'&copy; OpenStreetMap &copy; CARTO',maxZoom:18}).addTo(map);
  const line=L.polyline(r.line,{color:'#f5a623',weight:3,opacity:.85}).addTo(map);
  map.fitBounds(line.getBounds(),{padding:[20,20]});
  vpLayer=L.layerGroup().addTo(map);mapReady=true;
}
function vpIcon(p){return L.divIcon({className:'',iconSize:[14,14],
  html:`<div style="width:12px;height:12px;border-radius:50%;border:2px solid ${p?'#59c27a':'#e8e4da'};background:${p?'#59c27a':'#101418'}"></div>`})}
const runnerIcon=L.divIcon({className:'',iconSize:[18,18],
  html:'<div style="width:16px;height:16px;border-radius:50%;background:#f5a623;border:3px solid #101418;box-shadow:0 0 10px #f5a623"></div>'});

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
