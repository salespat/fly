"""Synthetic sample data so the site can be previewed offline. Clearly flagged as demo."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np

from . import turbulence as T
from .common import write_json

AIRPORTS = {  # icao: iata, name, city, lat, lon, tz
    "CYYZ": ["YYZ", "Toronto Pearson International Airport", "Toronto", 43.6772, -79.6306, "America/Toronto"],
    "CYVR": ["YVR", "Vancouver International Airport", "Vancouver", 49.1939, -123.1844, "America/Vancouver"],
    "CYUL": ["YUL", "Montreal-Trudeau International Airport", "Montreal", 45.4706, -73.7408, "America/Toronto"],
    "CYYC": ["YYC", "Calgary International Airport", "Calgary", 51.1225, -114.0133, "America/Edmonton"],
    "KJFK": ["JFK", "John F. Kennedy International Airport", "New York", 40.6398, -73.7789, "America/New_York"],
    "KLAX": ["LAX", "Los Angeles International Airport", "Los Angeles", 33.9425, -118.4081, "America/Los_Angeles"],
    "KORD": ["ORD", "Chicago O'Hare International Airport", "Chicago", 41.9786, -87.9048, "America/Chicago"],
    "EHAM": ["AMS", "Amsterdam Airport Schiphol", "Amsterdam", 52.3086, 4.7639, "Europe/Amsterdam"],
    "EGLL": ["LHR", "London Heathrow Airport", "London", 51.4706, -0.4619, "Europe/London"],
}
# (origin, dest, callsign, flightNo, airline, depUtcMin, durMin, type)
# Deliberately fictional airline and flight numbers so demo data can never be mistaken for real schedules.
FLIGHTS = [
    ("CYYZ", "CYVR", "DEMO107", "DEMO 107", "Demo Air (sample data)", 12 * 60 + 30, 290, "B789"),
    ("CYYZ", "CYVR", "DEMO114", "DEMO 114", "Demo Air (sample data)", 17 * 60 + 15, 295, "A321"),
    ("CYYZ", "CYVR", "DEMO121", "DEMO 121", "Demo Air (sample data)", 22 * 60 + 40, 290, "B77W"),
    ("CYYZ", "CYVR", "DEMO128", "DEMO 128", "Demo Air (sample data)", 14 * 60 + 5, 300, "B38M"),
    ("CYYZ", "CYVR", "DEMO135", "DEMO 135", "Demo Air (sample data)", 23 * 60 + 50, 305, "B738"),
    ("CYYZ", "CYVR", "DEMO142", "DEMO 142", "Demo Air (sample data)", 1 * 60 + 20, 300, "B38M"),
    ("CYYZ", "CYYC", "DEMO149", "DEMO 149", "Demo Air (sample data)", 13 * 60 + 0, 245, "A20N"),
    ("CYYZ", "CYYC", "DEMO156", "DEMO 156", "Demo Air (sample data)", 19 * 60 + 30, 250, "B38M"),
    ("CYYZ", "CYUL", "DEMO163", "DEMO 163", "Demo Air (sample data)", 15 * 60 + 0, 65, "BCS3"),
    ("CYYZ", "CYUL", "DEMO170", "DEMO 170", "Demo Air (sample data)", 21 * 60 + 0, 70, "CRJ9"),
    ("CYYZ", "KJFK", "DEMO177", "DEMO 177", "Demo Air (sample data)", 12 * 60 + 45, 85, "E75L"),
    ("CYYZ", "EGLL", "DEMO184", "DEMO 184", "Demo Air (sample data)", 23 * 60 + 55, 410, "B789"),
    ("CYYZ", "EGLL", "DEMO191", "DEMO 191", "Demo Air (sample data)", 0 * 60 + 30, 405, "B77W"),
    ("CYYZ", "KLAX", "DEMO198", "DEMO 198", "Demo Air (sample data)", 15 * 60 + 30, 315, "A321"),
    ("CYYZ", "EHAM", "DEMO205", "DEMO 205", "Demo Air (sample data)", 23 * 60 + 30, 435, "B789"),
    ("CYYZ", "EHAM", "DEMO212", "DEMO 212", "Demo Air (sample data)", 1 * 60 + 15, 430, "B772"),
    ("CYYC", "CYYZ", "DEMO219", "DEMO 219", "Demo Air (sample data)", 14 * 60 + 0, 215, "A20N"),
    ("EHAM", "CYYZ", "DEMO226", "DEMO 226", "Demo Air (sample data)", 13 * 60 + 30, 520, "B772"),
    ("CYVR", "CYYZ", "DEMO233", "DEMO 233", "Demo Air (sample data)", 15 * 60 + 0, 255, "B789"),
    ("CYVR", "CYYZ", "DEMO240", "DEMO 240", "Demo Air (sample data)", 18 * 60 + 30, 260, "B38M"),
    ("KJFK", "KLAX", "DEMO247", "DEMO 247", "Demo Air (sample data)", 13 * 60 + 0, 355, "A321"),
    ("KJFK", "KLAX", "DEMO254", "DEMO 254", "Demo Air (sample data)", 15 * 60 + 15, 350, "B752"),
    ("KJFK", "KLAX", "DEMO261", "DEMO 261", "Demo Air (sample data)", 17 * 60 + 0, 360, "A320"),
    ("KORD", "KLAX", "DEMO268", "DEMO 268", "Demo Air (sample data)", 14 * 60 + 0, 260, "B739"),
]


def demo_turbulence(t_hours: float) -> np.ndarray:
    """A wavy jet-stream band + a few blobs, drifting east with time."""
    lat = (90 - np.arange(T.NLAT) * T.RES)[:, None]
    lon = (np.arange(T.NLON) * T.RES)[None, :]
    lon_w = ((lon + 180) % 360) - 180
    drift = 1.5 * t_hours
    axis = 46 + 6 * np.sin(np.deg2rad((lon_w - drift) * 3))
    band = np.exp(-((lat - axis) / 3.0) ** 2)
    blobs = np.zeros_like(band)
    for clat, clon, amp, r in [(50, -112, 0.38, 4), (44, -85, 0.25, 3.5), (55, -40, 0.30, 6), (38, -100, 0.2, 4)]:
        d2 = ((lat - clat) / r) ** 2 + ((lon_w - (clon + drift)) / (r * 1.6)) ** 2
        blobs += amp * np.exp(-d2)
    base = 0.03 + 0.14 * band + blobs
    layers = [np.clip(base * f, 0, 0.6).astype(np.float32) for f in (0.8, 1.0, 1.15)]
    return np.stack(layers)


def build_demo(out) -> dict:
    now = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - dt.timedelta(hours=now.hour % 3)
    grids = {start + dt.timedelta(hours=h): demo_turbulence(h) for h in range(-3, 40, 3)}
    T.write_grids(out, grids, {"source": "DEMO synthetic turbulence (not a forecast)", "sourceId": "demo",
                               "demo": True})
    # flights: same schedule every weekday, aircraft type varies on a couple of days
    index: dict = {}
    for o, d, cs, fno, airline, dep, dur, typ in FLIGHTS:
        occ = [[wd, dep, dur, ("A333" if typ == "B789" and wd == 5 else typ)] for wd in range(7)]
        index.setdefault(o, {}).setdefault(d, []).append([cs, fno, airline, occ])
    days = [(now.date() - dt.timedelta(days=i)).isoformat() for i in range(1, 8)]
    for o, dests in index.items():
        write_json(out / "flights" / f"{o}.json", {"origin": o, "days": days, "dest": dests})
    used = set(index) | {d for v in index.values() for d in v}
    write_json(out / "airports.json", [[k, *AIRPORTS[k], 1 if k in index else 0] for k in sorted(used)])
    featured = [("CYYZ", "CYYC"), ("CYYZ", "EHAM"), ("CYYC", "CYYZ"), ("EHAM", "CYYZ")]
    write_json(out / "featured.json", [{"from": o, "to": d, "flights": len(index.get(o, {}).get(d, []))}
                                       for o, d in featured])
    ts = lambda h: (now + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(out / "sigmets.json", {"features": [{
        "hazard": "TURB", "severity": "SEV", "baseFt": 28000, "topFt": 38000,
        "from": ts(-1), "to": ts(14), "fir": "CZEG (demo)",
        "rings": [[[-118, 48], [-108, 48], [-106, 53], [-116, 54], [-118, 48]]]}]})
    return {"demo": True, "errors": [], "turbulence": {"source": "demo", "times": len(grids)},
            "flights": {"origins": len(index), "flights": len(FLIGHTS)}, "sigmets": 1}
