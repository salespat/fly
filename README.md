# Fly

Pick a route (e.g. **YYZ → YVR**). Fly ranks every flight on that route in the next ~36 hours by forecast turbulence, adjusted for the aircraft type, and shows the smoothest seats.

* Fully static: **GitHub Actions** builds the data every 3 hours, **GitHub Pages** hosts it. No server, no database.
* Free data only: NOAA (turbulence, warnings), OpenSky Network (flights), OurAirports/OpenFlights (reference data).

```
GitHub Actions (every 3 h)                         Browser (per search)
├─ turbulence grids  → site/data/turb/*.bin.gz     ├─ load data/flights/<ORIGIN>.json
├─ OpenSky last 7 d  → site/data/flights/*.json    ├─ keep flights to <DEST> on matching weekdays
├─ SIGMET warnings   → site/data/sigmets.json      ├─ download only the forecast hours needed
└─ deploy site/ to GitHub Pages                    └─ score, rank, map, seat tips
```

## Quick start (local preview with demo data)

```bash
pip install numpy                     # demo mode needs only numpy
python -m pipeline.build --demo       # writes synthetic data to site/data/
python -m http.server -d site 8000    # open http://localhost:8000/#YYZ-YVR
```

Demo mode shows a yellow "Demo mode" banner. Its flights and turbulence are made up.

## Deploy (about 15 minutes)

1. **Create a public GitHub repo** and push this folder to `main`. (Free GitHub Pages requires a public repo.)
2. **OpenSky API client (free):** create an account at opensky-network.org → *Account* → create an **API client**. Copy the `client_id` and `client_secret`.
3. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   * `OPENSKY_CLIENT_ID`
   * `OPENSKY_CLIENT_SECRET`
4. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
5. **Actions → Build & deploy → Run workflow.** The first run backfills 7 days of flights (84 OpenSky requests) and takes the longest.
6. Open `https://<you>.github.io/<repo>/`. To use your own domain, add it under Settings → Pages → Custom domain.

Tip: to put the site online before your OpenSky client is set up, add the repository **variable** `DEMO_MODE=true`.

### Optional settings (Settings → Secrets and variables → Actions → Variables)

| Variable | Default | Meaning |
|---|---|---|
| `DEMO_MODE` | `false` | `true` = deploy synthetic demo data |
| `TURB_SOURCE` | `auto` | `auto` (WAFS, fall back to GFS), `wafs`, or `gfs` |
| `OPENSKY_MAX_NEW_DAYS` | `7` | Max history days to fetch per run. Lower it (e.g. `2`) if you hit OpenSky credit limits; history fills in over a few days |

Optional secrets: `WIFS_USERNAME` / `WIFS_PASSWORD`, if you have NOAA WIFS credentials.

## How the turbulence data works

1. **NOAA WAFS harmonized turbulence (EDR), 0.25°** via the WIFS API. NOAA describes WIFS as a service for approved users, so it may refuse anonymous requests. If it does, the build falls back automatically to:
2. **NOAA GFS 0.5° upper-level winds** (public NOMADS). The pipeline computes the **Ellrod clear-air turbulence index** and maps it to an approximate EDR scale (`TI_TO_EDR` in `pipeline/turbulence.py`).

The page header always shows which source is live. Both sources are converted to the same compact grid: 0.5°, layers FL300/FL340/FL390, every 3 hours, roughly 3 hours back to 42 hours ahead.

## How flights work

OpenSky only records past flights. Each day the pipeline fetches yesterday's flights (12 two-hour requests), keeps a rolling 7 days, and builds one file per origin airport. The site assumes the same callsigns fly again on the same weekday, usually with the same aircraft type, and labels them that way.

### Featured routes (always shown)

`featured_routes.json` lists routes that always appear as one-click "Popular routes" chips and are always offered in the airport dropdowns. Currently: Toronto–Calgary and Toronto–Amsterdam, in both directions. For these routes the pipeline also queries OpenSky's per-airport departure and arrival lists, and joins the two halves of flights that OpenSky splits on long ocean crossings. This makes them much less likely to be missing. To change them, edit the file (IATA codes such as `YYZ` work) and push. The next build picks up the change.

### OpenSky credits and HTTP 429

OpenSky gives each account a daily allowance of credits for flight data: 400 without login, 4,000 with a standard account and 8,000 for active feeders. Requests for past days cost about 30 credits each, so a full first-time download of 7 days uses most of a standard allowance. When the allowance runs out, OpenSky answers **HTTP 429**.

The pipeline (`pipeline/opensky.py`) handles this:
- it keeps its own estimated daily budget (3,500 credits by default, or 350 without API credentials) and stops before going over it;
- on a 429 it stops calling OpenSky at once and doesn't try again until OpenSky's "retry after" time has passed, even across builds;
- every 2-hour block is saved as soon as it arrives, so an interrupted day resumes where it stopped instead of starting over;
- featured routes are fetched first, in 2-day windows (the same price as 1 day).

A 429 no longer breaks anything: the site is rebuilt from the data already downloaded, and missing days are filled in on later runs. `site/data/status.json` → `flights.opensky` shows credits used, and whether downloads were stopped and why. If you are an OpenSky feeder (8,000 credits), set the repository variable `OPENSKY_DAILY_CREDITS` to `7500`.

A day of tracking data is only saved when all twelve 2-hour blocks came back. Empty blocks (not processed yet by OpenSky) are retried on the next run, so no flights are silently lost.

Known gaps: routes with weak ADS-B coverage (oceans, parts of Africa and Asia) may be missing. Alphanumeric callsigns (e.g. `RYR5UK`) can't be mapped to flight numbers, so the site shows the callsign instead.

## Scoring (site/js/score.js)

* Great-circle path every 50 km, with a climb/cruise/descent profile (cruise FL by distance). Only cruise (FL250+) is scored.
* At each point: EDR interpolated in space, altitude and time, × aircraft factor (`site/data/aircraft.json`).
* Score 0–100 = 50% peak (95th percentile) + 30% average + 20% minutes in moderate+, plus 15 if the route crosses an active turbulence or thunderstorm SIGMET.
* Labels: Smooth < 0.10, Light < 0.18, Light-to-moderate < 0.28, Moderate < 0.40, Rough ≥ 0.40 (felt EDR).

**All thresholds, aircraft factors and the Ellrod→EDR mapping are starting heuristics.** Tune them against pilot reports (aviationweather.gov PIREPs) once you have real data.

## Tests

```bash
python -m unittest discover -s tests -t . -p "test_*.py"
python -m pipeline.build --demo && node --test tests/score.test.mjs tests/integration.test.mjs
```

The workflow runs the unit tests before every build. If the real-data build fails, the deploy is skipped and the previous version of the site stays live.

## Verify on your first real run

This project was written and tested offline against synthetic data. When your first live run completes, check the Actions log for:

- [ ] `WAFS unavailable … falling back to GFS`: expected without WIFS credentials. Make sure the GFS path then succeeds.
- [ ] OpenSky: `fetching <day> -> N airline flights` with N in the tens of thousands per day. If you see 401/403 errors, check your secrets. If you see 429 errors, lower `OPENSKY_MAX_NEW_DAYS`.
- [ ] `Downloading aircraft database`: if every mirror fails, set the variable `AIRCRAFT_DB_URL` to a current OpenSky aircraft-database CSV.
- [ ] Load a busy route (e.g. `#YYZ-YVR`) and check the flight times against an airline schedule.

## Data licences and attribution

* NOAA/NWS data: US government, public domain.
* OpenSky Network: free for **non-commercial / research** use. Check their terms before monetising, and keep the attribution.
* OurAirports: public domain. OpenFlights airports/airlines: ODbL (attribution).
* Map tiles: © OpenStreetMap contributors, © CARTO (free tier; heavier or commercial use needs a CARTO plan).
* GitHub Pages isn't meant for commercial services. If you monetise, move hosting (e.g. Cloudflare Pages). The workflow stays the same apart from the deploy step.

Comfort-planning tool only, not for flight safety.

## Layout

```
pipeline/   build.py (entry) · turbulence.py · flights.py · sigmets.py · refdata.py · demo.py
site/       index.html · css/style.css · js/app.js (UI) · js/score.js (scoring) · data/
tests/      Python unit tests · Node tests (scoring + end-to-end on demo data)
.github/workflows/build.yml
```
