"""Active turbulence / thunderstorm warnings (SIGMETs) from aviationweather.gov.

International SIGMETs cover the world; US SIGMETs cover US airspace. Both are free,
no key required. We keep only turbulence and convective warnings and simplify them
to {hazard, severity, baseFt, topFt, from, to, rings:[[[lon,lat],...]]}.
"""
from __future__ import annotations

from pathlib import Path

from .common import http_get, log, write_json

ENDPOINTS = [
    "https://aviationweather.gov/api/data/isigmet",
    "https://aviationweather.gov/api/data/airsigmet",
]
KEEP = ("TURB", "TS", "CONVECTIVE", "MTW", "TC")


def _first(props: dict, *keys):
    for k in keys:
        v = props.get(k)
        if v not in (None, ""):
            return v
    return None


def simplify(feature: dict) -> dict | None:
    props = feature.get("properties") or {}
    hazard = str(_first(props, "hazard", "airSigmetType", "phenomenon") or "").upper()
    if not any(k in hazard for k in KEEP):
        return None
    geom = feature.get("geometry") or {}
    gtype, coords = geom.get("type"), geom.get("coordinates") or []
    if gtype == "Polygon":
        rings = [coords[0]] if coords else []
    elif gtype == "MultiPolygon":
        rings = [poly[0] for poly in coords if poly]
    else:
        return None
    if not rings:
        return None

    def ft(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    base = ft(_first(props, "base", "altitudeLow1", "altitudeLow", "bottom"))
    top = ft(_first(props, "top", "altitudeHi1", "altitudeHi", "altitudeHigh1"))
    return {
        "hazard": "TURB" if "TURB" in hazard or "MTW" in hazard else "TS",
        "severity": str(_first(props, "qualifier", "severity") or ""),
        "baseFt": base, "topFt": top,
        "from": _first(props, "validTimeFrom", "validFrom"),
        "to": _first(props, "validTimeTo", "validTo"),
        "fir": _first(props, "firName", "firId", "icaoId") or "",
        "rings": [[[round(float(x), 3), round(float(y), 3)] for x, y, *_ in ring] for ring in rings],
    }


def run(out_dir: Path) -> dict:
    feats = []
    for url in ENDPOINTS:
        try:
            r = http_get(url, params={"format": "geojson"}, timeout=60)
            for f in (r.json().get("features") or []):
                s = simplify(f)
                if s:
                    feats.append(s)
        except Exception as e:
            log.warning("SIGMET fetch %s failed: %s", url, e)
    write_json(out_dir / "sigmets.json", {"features": feats})
    log.info("SIGMETs: %d turbulence/convective warnings", len(feats))
    return {"sigmets": len(feats)}
