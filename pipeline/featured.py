"""Extra-thorough flight lookups for a short list of "featured" routes.

The global /flights/all feed sometimes misses flights on a route (e.g. OpenSky could
not estimate the arrival airport, or a long ocean crossing was split into two
pieces). For the routes in ``featured_routes.json`` we additionally query
OpenSky's per-airport endpoints:

* departures from the origin whose estimated arrival is the destination,
* arrivals at the destination whose estimated departure is the origin, and
* "stitched" flights: same callsign seen leaving the origin (arrival unknown) and
  arriving at the destination (departure unknown) a few hours later.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

from .common import CACHE, ROOT, ensure_dir, log, read_json, write_json
from .opensky import OpenSkyLimit

DEPARTURE_URL = "https://opensky-network.org/api/flights/departure"
ARRIVAL_URL = "https://opensky-network.org/api/flights/arrival"
AIRPORT_DIR = CACHE / "opensky_airports"
ROUTES_FILE = ROOT / "featured_routes.json"
MAX_STITCH_S = 18 * 3600


def load_routes(airports: dict, path: Path = ROUTES_FILE) -> list[tuple[str, str]]:
    """Read featured routes (ICAO or IATA codes) -> [(originICAO, destICAO)] known to `airports`."""
    by_iata = {v[0]: k for k, v in airports.items() if v and v[0]}
    resolve = lambda c: c if c in airports else by_iata.get(c)
    out = []
    for pair in (read_json(path, {}) or {}).get("routes", []):
        o, d = (resolve(str(c).strip().upper()) for c in pair[:2])
        if o and d and o != d and (o, d) not in out:
            out.append((o, d))
        elif not (o and d):
            log.warning("Featured route %s: unknown airport code", pair)
    return out


def compact_partial(flights: list[dict]) -> list[list]:
    """Like flights.compact but keeps rows whose dep/arr airport is unknown ("")."""
    from .flights import AIRLINE_CALLSIGN
    out = []
    for f in flights:
        cs = (f.get("callsign") or "").strip().upper()
        first, last = f.get("firstSeen"), f.get("lastSeen")
        if not (AIRLINE_CALLSIGN.match(cs) and first and last and last >= first):
            continue
        out.append([(f.get("icao24") or "").lower(), cs, f.get("estDepartureAirport") or "",
                    f.get("estArrivalAirport") or "", int(first), int(last)])
    return out


def _day_of(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date().isoformat()


def fetch_airport_days(client, kind: str, icao: str, days: list[dt.date]) -> list[list]:
    """All rows for airport `icao` ('dep' or 'arr') over `days`, cached per UTC day.

    Missing days are requested in 2-day windows (OpenSky's maximum), which cost the
    same credits as a single day. Rows are filed under the day of their departure
    ('dep') or arrival ('arr') time, matching how OpenSky filters these endpoints."""
    ensure_dir(AIRPORT_DIR)
    path = lambda d: AIRPORT_DIR / f"{kind}_{icao}_{d.isoformat()}.json.gz"
    missing = sorted(d for d in days if not path(d).exists())
    url = DEPARTURE_URL if kind == "dep" else ARRIVAL_URL
    tcol = 4 if kind == "dep" else 5
    i = 0
    while i < len(missing):
        d0 = missing[i]
        span = 2 if i + 1 < len(missing) and missing[i + 1] == d0 + dt.timedelta(days=1) else 1
        begin = int(dt.datetime(d0.year, d0.month, d0.day, tzinfo=dt.timezone.utc).timestamp())
        rows = compact_partial(client.get(url, {"airport": icao, "begin": begin, "end": begin + span * 86400}))
        for k in range(span):
            d = d0 + dt.timedelta(days=k)
            mine = [r for r in rows if _day_of(r[tcol]) == d.isoformat()]
            if mine:  # never cache an empty day: it may simply not be processed yet
                write_json(path(d), mine, gz=True)
        i += span
        time.sleep(float(os.environ.get("OPENSKY_SLEEP", "2")))
    keep = {path(d).name for d in days}
    for p in AIRPORT_DIR.glob(f"{kind}_{icao}_*.json.gz"):
        if p.name not in keep:
            p.unlink()
    return [r for d in sorted(days) for r in read_json(path(d), [])]


def match_route(o: str, d: str, deps: list[list], arrs: list[list]) -> list[list]:
    """Flights o->d from origin departures + destination arrivals (+ stitched halves)."""
    found = [r for r in deps if r[2] == o and r[3] == d] + [r for r in arrs if r[2] == o and r[3] == d]
    open_deps = [r for r in deps if r[2] == o and not r[3]]   # left o, arrival unknown
    open_arrs = [r for r in arrs if r[3] == d and not r[2]]   # reached d, departure unknown
    for a in open_arrs:
        best = None
        for p in open_deps:
            if p[1] == a[1] and p[4] < a[5] and a[5] - p[4] <= MAX_STITCH_S and (best is None or p[4] > best[4]):
                best = p
        if best:
            found.append([best[0] or a[0], a[1], o, d, best[4], a[5]])
    out, seen = [], []
    for r in sorted(found, key=lambda r: r[4]):
        r = [r[0], r[1], o, d, r[4], r[5]]
        if not (15 * 60 <= r[5] - r[4] <= 20 * 3600):
            continue
        if any(s[1] == r[1] and abs(s[4] - r[4]) < 3 * 3600 for s in seen):
            continue  # same flight reported by both endpoints
        seen.append(r)
        out.append(r)
    return out


def collect(client, routes: list[tuple[str, str]], days: list[dt.date]) -> tuple[list[list], dict]:
    """Returns (rows, per-route flight counts). Stops quietly when OpenSky credits run out."""
    rows, counts = [], {}
    if not routes or not days:
        return rows, counts
    data: dict = {}
    for kind, icao in [("dep", o) for o, _ in routes] + [("arr", d) for _, d in routes]:
        if (kind, icao) in data:
            continue
        try:
            data[(kind, icao)] = fetch_airport_days(client, kind, icao, days)
        except OpenSkyLimit as e:
            log.warning("Featured routes: %s; using cached data only", e)
            data[(kind, icao)] = _cached(kind, icao, days)
        except Exception as e:
            log.warning("OpenSky %s %s failed: %s", kind, icao, e)
            data[(kind, icao)] = _cached(kind, icao, days)
    for o, d in routes:
        got = match_route(o, d, data[("dep", o)], data[("arr", d)])
        rows += got
        counts[f"{o}-{d}"] = len(got)
        log.info("Featured route %s->%s: %d flights over %d days", o, d, len(got), len(days))
    return rows, counts


def _cached(kind: str, icao: str, days: list[dt.date]) -> list[list]:
    return [r for d in sorted(days) for r in read_json(AIRPORT_DIR / f"{kind}_{icao}_{d.isoformat()}.json.gz", [])]
