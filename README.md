# crew-eta

**Self-hosted live ETA dashboard for ultra and trail crews.** The runner tracks via Garmin LiveTrack — the crew sees the live position on a map plus a continuously recalculated estimated time of arrival (ETA) for each aid station and the finish, grade-adjusted for elevation.

No third-party account, no cloud, no subscription: one Docker container on **your own** server, and your data stays yours.

---

## ⚠️ Read this first — scope of use

This project reads position data from an **unofficial, undocumented Garmin LiveTrack endpoint**. That has consequences you need to understand before using it:

- **Private / own-crew use only.** It is meant for tracking **your own** LiveTrack link and sharing it with **your own** crew. Do **not** run this as a central service that collects other people's runner data.
- **No commercial use.** Tapping the endpoint for a paid or ad-funded product violates Garmin's terms of service (among other things: sharing end-user data with unapproved third parties, and circumventing protection mechanisms). Anyone who builds a commercial service on it does so at their own legal risk. The official route would be Garmin's **Connect Developer Program** (enterprise, approval-based) — which, however, does not expose a real-time LiveTrack stream.
- **It can break at any time.** Garmin may change the endpoint's format or paths without notice. Tracking then stops working until the endpoint is updated in the code. Test in simulation mode before any important race.
- **Privacy.** Location data is sensitive. If you share it with others, get consent from the person concerned (usually yourself), and don't run this publicly with someone else's data.
- **Trademarks.** *Garmin* and *LiveTrack* are trademarks of Garmin Ltd. This project is **not** affiliated with, endorsed by, or reviewed by Garmin.

In short: a private community tool, at your own risk — not a product.

---

## Features

- **Multiple tracks in parallel**, created in a password-protected admin area.
- **The crew enters the Garmin link itself:** a newly created track has no LiveTrack link yet (it only exists once the activity has started). On the overview, the first visitor pastes it — after that, tracking runs automatically for everyone. A button lets anyone fix a wrong link.
- **Grade-adjusted ETA** (GAP): uphill/downhill feed into the remaining time via a Minetti-style factor.
- **Adaptive:** a blend of a rolling pace window and overall race pace, plus a small fatigue drift — the closer a point, the sharper its ETA.
- **Dark map** (night-friendly for long races); aid-station markers turn green once passed.
- **Simulation mode** for testing without a real Garmin link.

## Tech

FastAPI (Python) · Leaflet · Docker. No external database — configuration and live data are stored as JSON in the mounted `data/` volume.

## Quick start (local, with simulation)

```bash
git clone https://github.com/BBgoesMAC/crew-eta.git
cd crew-eta
cp .env.example .env
# open .env and set ADMIN_PASSWORD to your own password!
docker compose up -d --build
```

Then open `http://127.0.0.1:8010/tracking/admin`, sign in with your password, create a track and tick **Simulation** — after 2–3 minutes the first ETAs appear at `http://127.0.0.1:8010/tracking/`.

> The container listens on `127.0.0.1` only. To reach it from outside, put a reverse proxy (e.g. Nginx) in front — see below.

## The three pages

| Path | Access | Purpose |
|---|---|---|
| `/tracking/` | open | overview of all tracks; link entry for tracks without a Garmin link |
| `/tracking/t/<id>` | open | one track's dashboard: map + per-aid-station ETA |
| `/tracking/admin` | password | create / edit / delete tracks |

The admin area is protected with HTTP Basic auth (password = `ADMIN_PASSWORD`, any username). Entering the Garmin link on the overview is intentionally open, so the crew can do it without credentials.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_PASSWORD` | `changeme-in-env` | password for the admin area — **change this** |
| `POLL_INTERVAL` | `60` | seconds between LiveTrack polls |
| `ROLLING_WINDOW_MIN` | `30` | window for the current pace |
| `DRIFT_PER_HOUR` | `0.03` | fatigue markup on remaining time, per hour |

## Entering aid-station markers

In the admin area, one line per aid station, format `name, km`:

```
VP1-Polop, 12
VP2-Benimantell, 33.3
VP3-Confrides, 58.5
```

Use names without spaces (hyphen instead of a space), since the parser splits on the first space/comma. The finish is appended to the end of the route automatically.

## Running behind Nginx

```nginx
location /tracking/ {
    proxy_pass http://127.0.0.1:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 20m;   # GPX upload
}
```

Nginx matches the longest prefix, so `/tracking/` coexists with an existing site served at `/`.

## How the ETA is computed

1. GPX → grade-adjusted equivalent distance (Minetti-style gradient factor).
2. The live position is projected monotonically onto the route (robust against crossings and GPS outliers).
3. Pace = 75% rolling 30-min window + 25% overall pace → near aid stations follow current form, far ones stay conservative.
4. Fatigue drift (+3%/h) on the remaining time.

Aid-station stops don't break the ETA: the window smooths them, and when stationary the calculation falls back to overall pace. **The crew must mentally add any planned long aid-station stops.**

## When the endpoint breaks

Start LiveTrack, inspect the network requests of the share page in your browser, grab the current trackpoints URL, and adjust it in `app/main.py` (`fetch_trackpoints`). Pull requests welcome.

## Contributing

Issues and PRs welcome — especially for endpoint fixes, ETA calibration, and translations. Please respect the scope of use above.

## License

[MIT](LICENSE) — do what you want, no warranty. The scope of use above is a strong recommendation, not an additional license term.
