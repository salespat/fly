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

from .common import CACHE, ROOT, ensure_dir, http_get, log, read_json, write_json

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


def _fetch(client, url: str, icao: str, begin: int, end: int) -> list[dict]:
    r = http_get(url, params={"airport": icao, "begin": begin, "end": end},
                 headers=client._headers(), timeout=120, ok_404=True)
    return [] if r is None else (r.json() or [])


def fetch_airport_day(client, kind: str, icao: str, day: dt.date) -> list[list]:
    """kind: 'dep' or 'arr'. Cached per airport/day. One request per day (2h chunks as fallback)."""
    ensure_dir(AIRPORT_DIR)
    path = AIRPORT_DIR / f"{kind}_{icao}_{day.isoformat()}.json.gz"
    if path.exists():
        return read_json(path, [])
    url = DEPARTURE_URL if kind == "dep" else ARRIVAL_URL
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    try:
        raw = _fetch(client, url, icao, start, start + 86400)
    except Exception as e:  # interval too long or transient error: retry in 2-hour pieces
        log.info("OpenSky %s %s %s: full-day request failed (%s); using 2h chunks", kind, icao, day, e)
        raw = []
        for b in range(start, start + 86400, 7200):
            raw += _fetch(client, url, icao, b, b + 7200)
            time.sleep(float(os.environ.get("OPENSKY_SLEEP", "2")))
    rows = compact_partial(raw)
    if rows:  # never cache an empty day: it may simply not be processed yet
        write_json(path, rows, gz=True)
    time.sleep(float(os.environ.get("OPENSKY_SLEEP", "2")))
    return rows


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
    """Returns (rows, per-route flight counts)."""
    rows, counts = [], {}
    if not routes:
        return rows, counts
    cache: dict = {}
    get = lambda kind, icao, day: cache.setdefault((kind, icao, day), _safe(client, kind, icao, day))
    for o, d in routes:
        n = 0
        for day in days:
            got = match_route(o, d, get("dep", o, day), get("arr", d, day))
            rows += got
            n += len(got)
        counts[f"{o}-{d}"] = n
        log.info("Featured route %s->%s: %d flights over %d days", o, d, n, len(days))
    return rows, counts


def _safe(client, kind, icao, day):
    try:
        return fetch_airport_day(client, kind, icao, day)
    except Exception as e:
        log.warning("OpenSky %s %s %s failed: %s", kind, icao, day, e)
        return []
