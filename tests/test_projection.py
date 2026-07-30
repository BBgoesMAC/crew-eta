"""Regression tests for route projection on self-overlapping courses.

Nearest-point-by-air-distance snapping used to teleport the runner to whatever
route point was geometrically closest. On loop courses the finish sits metres
from the start, so a runner at the start line jumped straight to the finish;
on self-crossing courses a far-ahead VP got ticked at the crossing. The
projection is now gated by the distance the runner can plausibly have covered.

Run with:  python -m pytest tests/test_projection.py   (or execute directly)
"""
import math
import os
import sys

os.environ.setdefault("DATA_DIR", os.path.join(os.path.dirname(__file__), "_testdata"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import main  # noqa: E402

LAT0, LON0 = 38.65, -0.20
M2LAT = 1 / 111320.0
M2LON = 1 / (111320.0 * math.cos(math.radians(LAT0)))


def _gpx(coords):
    pts = "".join(
        f'<trkpt lat="{la:.7f}" lon="{lo:.7f}"><ele>{e}</ele></trkpt>'
        for la, lo, e in coords)
    return (f'<?xml version="1.0"?><gpx version="1.1" creator="t" '
            f'xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>{pts}'
            f'</trkseg></trk></gpx>').encode()


def _lollipop():
    """Out-and-back lollipop whose finish chute runs 8 m beside the start."""
    coords = []
    for m in range(0, 1000, 10):
        coords.append((LAT0 + m * M2LAT, LON0, 300))
    for k in range(0, 360, 4):
        a = math.radians(k)
        coords.append((LAT0 + (1300 + 300 * math.sin(a)) * M2LAT,
                       LON0 + (4 + 300 * math.cos(a)) * M2LON, 350))
    for m in range(1000, -1, -10):
        coords.append((LAT0 + m * M2LAT, LON0 + 8 * M2LON, 300))
    return main.build_route(_gpx(coords))


def _figure_eight():
    coords = []
    steps = 800
    for k in range(steps + 1):
        t = 2 * math.pi * k / steps
        coords.append((LAT0 + 600 * math.sin(2 * t) * M2LAT,
                       LON0 + 1000 * math.sin(t) * M2LON, 300))
    return main.build_route(_gpx(coords))


def test_start_does_not_snap_to_finish_on_a_loop():
    route = _lollipop()
    total_km = route["total_m"] / 1000.0
    # A first fix with a few metres of GPS scatter toward the finish chute must
    # stay at the start, not teleport to the finish.
    for off_m in (0, 4, 5, 8):
        idx = main.project(route, LAT0, LON0 + off_m * M2LON, 0, None)
        km = route["cum"][idx] / 1000.0
        assert km < 0.5, f"first fix +{off_m} m snapped to km {km:.2f} (total {total_km:.2f})"


def test_progress_is_monotonic_over_a_full_loop():
    route = _lollipop()
    total_km = route["total_m"] / 1000.0
    cfg = {"id": "a1b2c3", "name": "loop",
           "markers": [{"name": "VP1", "km": 1.5}, {"name": "VP2", "km": 3.5}],
           "simulate": False, "session_id": "x", "token": "y", "created": 0}
    tr = main.new_track(cfg, route)
    prev = 0.0
    t = 1000.0
    for i in range(0, len(route["lat"]), 3):
        main.ingest_points(tr, [{"t": t, "lat": route["lat"][i],
                                 "lon": route["lon"][i], "ele": route["ele"][i]}])
        km = route["cum"][tr["last_idx"]] / 1000.0
        assert km >= prev - main.BACK_SLACK_M / 1000.0, "runner jumped backwards"
        prev = km
        t += 60
    assert prev > total_km - 0.2, "runner never reached the finish"
    st = main.build_status(tr)
    passed = {vp["name"]: vp["passed"] for vp in st["vps"]}
    assert passed["VP1"] and passed["VP2"]


def test_far_vp_not_ticked_at_a_self_crossing():
    route = _figure_eight()
    total_km = route["total_m"] / 1000.0
    # middle self-crossing (~km 3.34) — the same spot is also the start (km 0)
    hits = sorted({round(route["cum"][i] / 1000.0, 2)
                   for i in range(len(route["lat"]))
                   if main.haversine(LAT0, LON0, route["lat"][i], route["lon"][i]) < 15})
    mid_km = next(k for k in hits if 1.0 < k < total_km - 1.0)
    vp_late = round(mid_km + 0.3, 2)
    cfg = {"id": "d4e5f6", "name": "eight",
           "markers": [{"name": "VP-late", "km": vp_late}],
           "simulate": False, "session_id": "x", "token": "y", "created": 0}
    tr = main.new_track(cfg, route)
    ticked_at = None
    t = 1000.0
    for i in range(0, len(route["lat"]), 2):
        main.ingest_points(tr, [{"t": t, "lat": route["lat"][i],
                                 "lon": route["lon"][i], "ele": route["ele"][i]}])
        if ticked_at is None and "VP-late" in tr["passed"]:
            ticked_at = route["cum"][tr["last_idx"]] / 1000.0
        t += 60
    assert ticked_at is not None, "VP-late was never ticked"
    assert ticked_at > vp_late - 0.4, (
        f"VP-late ticked at km {ticked_at:.2f}, expected near {vp_late:.2f} "
        "(ticked early at the crossing)")


if __name__ == "__main__":
    test_start_does_not_snap_to_finish_on_a_loop()
    test_progress_is_monotonic_over_a_full_loop()
    test_far_vp_not_ticked_at_a_self_crossing()
    print("all projection regression tests passed")
