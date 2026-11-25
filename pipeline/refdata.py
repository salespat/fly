"""Reference data: airports (+time zones), airlines, aircraft types by ICAO24 address.

All sources are free/open:
  * OurAirports (public domain)      – airport names, codes, coordinates
  * OpenFlights airports/airlines (ODbL) – IANA time zones, airline IATA codes/names
  * Aircraft registry (OpenSky / ADS-B Exchange) – icao24 -> ICAO type code
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os

from .common import CACHE, file_age_days, http_get, log, read_json, write_json

OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OPENFLIGHTS_AIRPORTS_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
OPENFLIGHTS_AIRLINES_URL = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"

# Tried in order; override with AIRCRAFT_DB_URL if these move.
AIRCRAFT_DB_URLS = [u for u in [
    os.environ.get("AIRCRAFT_DB_URL"),
    "https://opensky-network.org/datasets/metadata/aircraftDatabase.csv",
    "https://s3.opensky-network.org/data-samples/metadata/aircraftDatabase.csv",
    "https://downloads.adsbexchange.com/downloads/basic-ac-db.json.gz",
] if u]


# ---------------------------------------------------------------- airports
def parse_openflights_tz(text: str) -> dict[str, str]:
    """ICAO -> IANA tz from OpenFlights airports.dat."""
    tz = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) > 11 and row[5] not in ("", "\\N") and row[11] not in ("", "\\N"):
            tz[row[5]] = row[11]
    return tz


def parse_ourairports(text: str, tz: dict[str, str]) -> dict[str, list]:
    """ICAO -> [iata, name, city, lat, lon, tz]. Keeps airports with scheduled service
    or a large/medium type (OpenSky occasionally reports others; they are added later)."""
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        icao = (r.get("icao_code") or r.get("gps_code") or r.get("ident") or "").strip()
        if len(icao) != 4:
            continue
        try:
            lat, lon = round(float(r["latitude_deg"]), 4), round(float(r["longitude_deg"]), 4)
        except (KeyError, ValueError):
            continue
        out[icao] = [r.get("iata_code") or "", r.get("name") or icao,
                     r.get("municipality") or "", lat, lon, tz.get(icao, "")]
    return out


def load_airports(max_age_days=7) -> dict[str, list]:
    path = CACHE / "airports.json.gz"
    if file_age_days(path) < max_age_days:
        return read_json(path)
    log.info("Downloading airport reference data")
    tz = parse_openflights_tz(http_get(OPENFLIGHTS_AIRPORTS_URL).text)
    airports = parse_ourairports(http_get(OURAIRPORTS_URL).text, tz)
    write_json(path, airports, gz=True)
    return airports


# ---------------------------------------------------------------- airlines
def parse_airlines(text: str) -> dict[str, list]:
    """ICAO airline code -> [IATA, name]; active airlines win over defunct ones."""
    out, active = {}, {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 8:
            continue
        icao, iata, name, is_active = row[4], row[3], row[1], row[7] == "Y"
        if len(icao) != 3 or icao == "\\N":
            continue
        iata = iata if len(iata) == 2 and iata != "\\N" else ""
        if icao not in out or (is_active and not active.get(icao)):
            out[icao], active[icao] = [iata, name], is_active
    return out


def load_airlines(max_age_days=30) -> dict[str, list]:
    path = CACHE / "airlines.json.gz"
    if file_age_days(path) < max_age_days:
        return read_json(path)
    log.info("Downloading airline reference data")
    airlines = parse_airlines(http_get(OPENFLIGHTS_AIRLINES_URL).text)
    write_json(path, airlines, gz=True)
    return airlines


# ---------------------------------------------------------------- aircraft
def parse_aircraft_db(raw: bytes) -> dict[str, str]:
    """Accepts OpenSky CSV (double or single quoted) or ADS-B Exchange JSON-lines."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    out: dict[str, str] = {}
    first = text.lstrip()[:1]
    if first == "{":  # ADS-B Exchange basic-ac-db: one JSON object per line
        for line in text.splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            icao, t = (o.get("icao") or "").lower(), (o.get("icaotype") or "").upper()
            if icao and t:
                out[icao] = t
        return out
    header = text.split("\n", 1)[0]
    quote = "'" if "'icao24'" in header else '"'
    for r in csv.DictReader(io.StringIO(text), quotechar=quote):
        icao = (r.get("icao24") or "").strip().lower()
        t = (r.get("typecode") or "").strip().upper()
        if icao and t:
            out[icao] = t
    return out


def load_aircraft_types(max_age_days=30) -> dict[str, str]:
    path = CACHE / "aircraft_types.json.gz"
    if file_age_days(path) < max_age_days:
        return read_json(path)
    for url in AIRCRAFT_DB_URLS:
        try:
            log.info("Downloading aircraft database: %s", url)
            r = http_get(url, timeout=300, retries=2, ok_404=True)
            if r is None:
                continue
            types = parse_aircraft_db(r.content)
            if len(types) > 10_000:
                write_json(path, types, gz=True)
                return types
            log.warning("Aircraft DB at %s looked empty (%d rows)", url, len(types))
        except Exception as e:  # try the next mirror
            log.warning("Aircraft DB %s failed: %s", url, e)
    log.warning("No aircraft DB available; aircraft types will be unknown")
    return read_json(path, default={})
