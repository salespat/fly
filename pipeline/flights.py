"""Build a "who flies where" index from OpenSky's free /flights/all endpoint.

How it works
------------
* OpenSky only returns *past* flights, at most 2 hours per request.
* Each run we fetch any missing UTC days among the last ``HISTORY_DAYS`` days
  (12 requests per day) and cache them as compact per-day files.
* From the most recent 7 days we build, per origin airport, the list of airline
  flights to each destination with the weekday, takeoff time (UTC), duration and
  aircraft type seen on that weekday. The website assumes the same flights operate
  on the same weekday this week ("usually operated by...").
"""
from __future__ import annotations

import datetime as dt
import os
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

from .common import CACHE, ensure_dir, http_get, log, read_json, write_json

TOKEN_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
FLIGHTS_ALL_URL = "https://opensky-network.org/api/flights/all"
CHUNK_S = 2 * 3600            # OpenSky limit: interval must not exceed two hours
HISTORY_DAYS = 7              # one of each weekday
LAG_DAYS = 1                  # OpenSky flight tables are batch-processed; skip "today"
READY_AFTER_H = 6             # only cache a UTC day once it ended at least this long ago
AIRLINE_CALLSIGN = re.compile(r"^([A-Z]{3})(\d{1,4})([A-Z]{0,2})$")
DAYS_DIR = CACHE / "opensky_days"


class OpenSkyClient:
    """Minimal OAuth2 client-credentials client (tokens last ~30 min)."""

    def __init__(self, client_id: str | None, client_secret: str | None):
        self.client_id, self.client_secret = client_id, client_secret
        self._token, self._exp = None, 0.0

    def _headers(self) -> dict:
        if not (self.client_id and self.client_secret):
            return {}  # anonymous: far fewer credits, may be rejected for /flights/all
        if time.time() > self._exp - 60:
            import requests
            r = requests.post(TOKEN_URL, timeout=30, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id, "client_secret": self.client_secret})
            r.raise_for_status()
            j = r.json()
            self._token, self._exp = j["access_token"], time.time() + j.get("expires_in", 1800)
        return {"Authorization": f"Bearer {self._token}"}

    def flights_all(self, begin: int, end: int) -> list[dict]:
        r = http_get(FLIGHTS_ALL_URL, params={"begin": begin, "end": end},
                     headers=self._headers(), timeout=120, ok_404=True)
        return [] if r is None else (r.json() or [])


def compact(flights: list[dict]) -> list[list]:
    """Keep only airline-looking flights with both airports: [icao24, cs, dep, arr, first, last]."""
    out = []
    for f in flights:
        cs = (f.get("callsign") or "").strip().upper()
        dep, arr = f.get("estDepartureAirport"), f.get("estArrivalAirport")
        if not (dep and arr and dep != arr and AIRLINE_CALLSIGN.match(cs)):
            continue
        first, last = f.get("firstSeen"), f.get("lastSeen")
        if not (first and last and 15 * 60 <= last - first <= 20 * 3600):
            continue
        out.append([(f.get("icao24") or "").lower(), cs, dep, arr, int(first), int(last)])
    return out


def fetch_day(client: OpenSkyClient, day: dt.date) -> list[list]:
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    rows: list[list] = []
    for b in range(start, start + 86400, CHUNK_S):
        raw = client.flights_all(b, b + CHUNK_S)
        if not raw:  # worldwide traffic is never zero: this part isn't processed yet
            raise RuntimeError(f"empty 2-hour block at {dt.datetime.fromtimestamp(b, dt.timezone.utc):%H:%M} UTC; will retry next run")
        rows += compact(raw)
        time.sleep(float(os.environ.get("OPENSKY_SLEEP", "2")))
    # de-duplicate (a flight can straddle a chunk boundary)
    seen, uniq = set(), []
    for r in rows:
        k = (r[0], r[1], r[4])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def window_days(today: dt.date | None = None) -> tuple[list[dt.date], list[dt.date]]:
    """(wanted days newest first, the subset that OpenSky should have finished processing)."""
    now = dt.datetime.now(dt.timezone.utc)
    today = today or now.date()
    wanted = [today - dt.timedelta(days=LAG_DAYS + i) for i in range(HISTORY_DAYS)]
    ready = [d for d in wanted
             if now >= dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=1, hours=READY_AFTER_H)]
    return wanted, ready


def update_history(client: OpenSkyClient, today: dt.date | None = None,
                   max_new_days: int = 7) -> list[dt.date]:
    """Fetch missing days; returns the list of days available in the cache window."""
    ensure_dir(DAYS_DIR)
    wanted, ready_days = window_days(today)
    ready = lambda d: d in ready_days
    fetched = 0
    for day in wanted:  # newest first, so a fresh install gets recent data first
        path = DAYS_DIR / f"{day.isoformat()}.json.gz"
        if path.exists() or fetched >= max_new_days or not ready(day):
            continue
        log.info("OpenSky: fetching %s", day)
        try:
            rows = fetch_day(client, day)
        except Exception as e:
            log.warning("OpenSky fetch for %s failed: %s", day, e)
            continue
        if rows:
            write_json(path, rows, gz=True)
            fetched += 1
            log.info("OpenSky: %s -> %d airline flights", day, len(rows))
        else:
            log.warning("OpenSky returned no flights for %s (not processed yet?)", day)
    # prune old files
    keep = {f"{d.isoformat()}.json.gz" for d in wanted}
    for p in DAYS_DIR.glob("*.json.gz"):
        if p.name not in keep:
            p.unlink()
    return sorted(d for d in wanted if (DAYS_DIR / f"{d.isoformat()}.json.gz").exists())


def flight_number(callsign: str, airlines: dict[str, list]) -> tuple[str, str]:
    """ACA123 -> ('AC123', 'Air Canada'). Alphanumeric callsigns can't be mapped reliably."""
    m = AIRLINE_CALLSIGN.match(callsign)
    if not m:
        return "", ""
    prefix, num, suffix = m.groups()
    iata, name = (airlines.get(prefix) or ["", ""])[:2]
    if iata and not suffix:
        return f"{iata}{int(num)}", name
    return "", name


def build_index(days: list[dt.date], airports: dict, airlines: dict, types: dict,
                days_dir: Path = DAYS_DIR, extra_rows: list[list] | None = None) -> dict[str, dict]:
    """origin ICAO -> {dest ICAO -> [[callsign, flightNo, airline, [[wd, depMin, durMin, type], ...]]]}"""
    occ: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    rows = [r for day in sorted(days) for r in read_json(days_dir / f"{day.isoformat()}.json.gz", [])]
    rows += sorted(extra_rows or [], key=lambda r: r[4])
    for icao24, cs, dep, arr, first, last in rows:  # older first so newer days overwrite the same weekday
        if dep not in airports or arr not in airports:
            continue
        t = dt.datetime.fromtimestamp(first, dt.timezone.utc)
        wd = t.weekday()
        occ[dep][arr][cs][wd] = [wd, t.hour * 60 + t.minute, round((last - first) / 60),
                                 types.get(icao24, "")]
    index: dict[str, dict] = {}
    for dep, dests in occ.items():
        index[dep] = {}
        for arr, by_cs in dests.items():
            rows = []
            for cs, by_wd in sorted(by_cs.items()):
                fno, airline = flight_number(cs, airlines)
                rows.append([cs, fno, airline, [by_wd[w] for w in sorted(by_wd)]])
            index[dep][arr] = rows
    return index


def write_index(index: dict, airports: dict, out_dir: Path, days: list[dt.date],
                featured: list[tuple[str, str]] | None = None) -> dict:
    """Writes data/flights/<ICAO>.json, data/airports.json and data/featured.json; returns stats."""
    fdir = ensure_dir(out_dir / "flights")
    for p in fdir.glob("*.json"):
        p.unlink()
    featured = [(o, d) for o, d in (featured or []) if o in airports and d in airports]
    used = set(index) | {c for pair in featured for c in pair}
    for dep, dests in index.items():
        used.update(dests)
        write_json(fdir / f"{dep}.json", {"origin": dep, "days": [d.isoformat() for d in days], "dest": dests})
    # Airport list for autocomplete: [icao, iata, name, city, lat, lon, tz, isOrigin]
    origins = set(index) | {o for o, _ in featured}
    alist = [[icao, *airports[icao], 1 if icao in origins else 0] for icao in sorted(used) if icao in airports]
    write_json(out_dir / "airports.json", alist)
    write_json(out_dir / "featured.json", [
        {"from": o, "to": d, "flights": len(index.get(o, {}).get(d, []))} for o, d in featured])
    n_routes = sum(len(d) for d in index.values())
    n_flights = sum(len(r) for d in index.values() for r in d.values())
    return {"origins": len(index), "routes": n_routes, "flights": n_flights, "airports": len(alist)}


def run(out_dir: Path, airports: dict, airlines: dict, types: dict) -> dict:
    client = OpenSkyClient(os.environ.get("OPENSKY_CLIENT_ID"), os.environ.get("OPENSKY_CLIENT_SECRET"))
    days = update_history(client, max_new_days=int(os.environ.get("OPENSKY_MAX_NEW_DAYS", "7")))
    from . import featured
    routes = featured.load_routes(airports)
    _, ready = window_days()
    extra, counts = featured.collect(client, routes, ready)
    if not days and not extra:
        log.warning("No OpenSky history available; keeping previous flight index if any")
        return {"days": [], "featured": counts}
    index = build_index(days, airports, airlines, types, extra_rows=extra)
    stats = write_index(index, airports, out_dir, days or ready, featured=routes)
    stats["days"] = [d.isoformat() for d in days]
    stats["featured"] = {f"{o}-{d}": len(index.get(o, {}).get(d, [])) for o, d in routes}
    log.info("Flight index: %s", stats)
    return stats
