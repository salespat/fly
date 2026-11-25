"""Turbulence forecast grids for the website.

Output (site/data/turb/):
  manifest.json            – grid spec, source, list of valid times
  <YYYYMMDDHH>.bin.gz      – uint8 grid, layers concatenated: [FL300, FL340, FL390]
                             value = EDR x 100 (0.01 m^2/3 s^-1 steps), 255 = missing
Grid: 0.5 deg, rows north->south from 90N, columns east from 0E (361 x 720).

Sources (tried in order):
  1. NOAA WAFS harmonized turbulence (EDR), 0.25 deg, via the WIFS API.
     The WIFS service is aimed at approved users; set WIFS_USERNAME/WIFS_PASSWORD if
     you have an account. If access is refused we fall back automatically.
  2. NOAA GFS 0.5 deg upper-level winds (public NOMADS) -> Ellrod clear-air
     turbulence index, mapped to an approximate EDR scale.
"""
from __future__ import annotations

import datetime as dt
import gzip
import os
import re
import time
from pathlib import Path

import numpy as np

from .common import CACHE, ensure_dir, http_get, log, write_json

FLS = [300, 340, 390]
NLAT, NLON, RES = 361, 720, 0.5
MISSING = 255
WINDOW_BEFORE_H, WINDOW_AFTER_H = 3, 42

WIFS_BASE = "https://aviationweather.gov/wifs/api/collections/kwbc_wafshzds_blended_turb_0p25"
WIFS_ITEM_RE = re.compile(r"\b(Y[A-Z]{3}G(\d{3})_(\d{3})FLALL)\b")
NOMADS_FILTER = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p50.pl"
NOMADS_DIR = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"


# ------------------------------------------------------------------ helpers
def hpa_to_ft(p_hpa: float) -> float:
    """ICAO standard atmosphere pressure altitude (troposphere formula; fine to FL450)."""
    return (1 - (p_hpa / 1013.25) ** 0.190263) * 145366.45


def ft_to_hpa(ft: float) -> float:
    return 1013.25 * (1 - ft / 145366.45) ** (1 / 0.190263)


def encode_edr(edr: np.ndarray) -> np.ndarray:
    """EDR float grid -> uint8 (x100), NaN -> 255."""
    out = np.clip(np.rint(np.nan_to_num(edr, nan=-1) * 100), 0, 250).astype(np.uint8)
    out[~np.isfinite(edr)] = MISSING
    return out


def to_half_degree(values: np.ndarray, lat_first: float, lat_last: float,
                   lon_first: float, dlon: float) -> np.ndarray:
    """Regular lat/lon grid (any orientation, 0.25/0.5/other spacing) -> our 361x720 grid.
    Finer grids are max-pooled so narrow turbulence bands are not lost."""
    a = np.asarray(values, dtype=np.float32)
    if lat_first < lat_last:            # make rows north -> south
        a = a[::-1]
    nlat, nlon = a.shape
    dlat = 180.0 / (nlat - 1) if nlat > 1 else RES
    # roll columns so the first one is 0E
    shift = int(round((lon_first % 360) / dlon))
    a = np.roll(a, shift, axis=1)
    if abs(dlon - 0.5) < 1e-6 and nlat == NLAT:
        return a
    if abs(dlon - 0.25) < 1e-6 and nlat == 721:
        # max-pool each 0.5 deg cell over the surrounding 0.25 deg points
        rows = [np.fmax.reduce(a[max(0, 2 * i - 1):2 * i + 2], axis=0) for i in range(NLAT)]
        b = np.stack(rows)
        return np.fmax.reduce([b[:, 0::2], b[:, 1::2], np.roll(b, 1, axis=1)[:, 0::2]])
    # generic nearest-neighbour fallback
    lat_idx = np.clip(np.rint(np.arange(NLAT) * RES / dlat).astype(int), 0, nlat - 1)
    lon_idx = np.rint(np.arange(NLON) * RES / dlon).astype(int) % nlon
    return a[np.ix_(lat_idx, lon_idx)]


def write_grids(out_dir: Path, grids: dict[dt.datetime, np.ndarray], meta: dict) -> dict:
    """grids: valid time -> float array (len(FLS), 361, 720) of EDR."""
    tdir = ensure_dir(out_dir / "turb")
    for p in tdir.glob("*.bin.gz"):
        p.unlink()
    times = []
    for t in sorted(grids):
        name = t.strftime("%Y%m%d%H") + ".bin.gz"
        raw = np.concatenate([encode_edr(layer).ravel() for layer in grids[t]]).tobytes()
        (tdir / name).write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
        times.append({"t": t.strftime("%Y-%m-%dT%H:%MZ"), "file": f"turb/{name}"})
    manifest = {
        "levelsFL": FLS, "nlat": NLAT, "nlon": NLON, "lat0": 90, "lon0": 0, "res": RES,
        "scale": 0.01, "missing": MISSING, "times": times,
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), **meta,
    }
    write_json(tdir / "manifest.json", manifest, compact=False)
    return manifest


def in_window(t: dt.datetime, now: dt.datetime) -> bool:
    return now - dt.timedelta(hours=WINDOW_BEFORE_H) <= t <= now + dt.timedelta(hours=WINDOW_AFTER_H)


# ------------------------------------------------------------------ GRIB decoding
def iter_grib(path: Path):
    """Yield dicts with keys we need from every GRIB2 message in a file."""
    import eccodes  # imported lazily so tests/demo run without it
    with open(path, "rb") as f:
        while True:
            gid = eccodes.codes_grib_new_from_file(f)
            if gid is None:
                break
            try:
                g = lambda k, d=None: (eccodes.codes_get(gid, k) if eccodes.codes_is_defined(gid, k) else d)
                ni, nj = g("Ni"), g("Nj")
                vals = eccodes.codes_get_values(gid).astype(np.float32)
                miss = g("missingValue", 9999)
                if g("bitmapPresent", 0):
                    vals[vals == miss] = np.nan
                vd, vt = g("validityDate"), g("validityTime")
                yield {
                    "shortName": g("shortName", ""), "name": str(g("name", "")),
                    "cat": g("parameterCategory"), "num": g("parameterNumber"),
                    "levType": g("typeOfFirstFixedSurface"),
                    "levScaledValue": g("scaledValueOfFirstFixedSurface"),
                    "levScale": g("scaleFactorOfFirstFixedSurface", 0),
                    "level": g("level"), "typeOfLevel": g("typeOfLevel", ""),
                    "stat": g("typeOfStatisticalProcessing"),
                    "valid": dt.datetime.strptime(f"{vd}{int(vt):04d}", "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc),
                    "lat1": g("latitudeOfFirstGridPointInDegrees"), "lat2": g("latitudeOfLastGridPointInDegrees"),
                    "lon1": g("longitudeOfFirstGridPointInDegrees"), "dlon": g("iDirectionIncrementInDegrees"),
                    "values": vals.reshape(nj, ni),
                }
            finally:
                eccodes.codes_release(gid)


def message_fl(m: dict) -> float | None:
    """Flight level (hundreds of ft) of a GRIB message, or None if not an altitude level."""
    lt = m.get("levType")
    if m.get("levScaledValue") is not None:
        val = m["levScaledValue"] / (10 ** (m.get("levScale") or 0))
    else:
        val = m.get("level")
    if val is None:
        return None
    if lt == 100 or m.get("typeOfLevel") == "isobaricInhPa":
        p_hpa = val / 100 if val > 2000 else val       # Pa vs hPa
        return hpa_to_ft(p_hpa) / 100
    if lt in (102, 103) or m.get("typeOfLevel") in ("heightAboveSea",):
        return val * 3.28084 / 100
    return None


def is_edr(m: dict) -> bool:
    n = m["name"].lower()
    return ("eddy" in n or "turb" in n or m["shortName"].lower() in ("edr", "cat", "turb")
            or (m.get("cat") == 19 and m.get("num") in (22, 28, 29, 30)))


# ------------------------------------------------------------------ source 1: WAFS
def wafs_grids(now: dt.datetime, work: Path) -> dict[dt.datetime, np.ndarray]:
    auth = None
    if os.environ.get("WIFS_USERNAME"):
        auth = (os.environ["WIFS_USERNAME"], os.environ.get("WIFS_PASSWORD", ""))
    import requests
    s = requests.Session()
    s.auth = auth
    r = s.get(f"{WIFS_BASE}/items", params={"f": "json"}, timeout=60)
    if r.status_code in (401, 403):
        raise PermissionError(f"WIFS refused access (HTTP {r.status_code})")
    r.raise_for_status()
    items = sorted({(m[0], int(m[1]), int(m[2])) for m in WIFS_ITEM_RE.findall(r.text)}, key=lambda x: x[1])
    items = [it for it in items if it[1] <= 48]
    if not items:
        raise RuntimeError("WIFS listing contained no global turbulence items")
    want_stat = {"mean": 0, "max": 2}.get(os.environ.get("TURB_STAT", "mean"), 0)
    per_time: dict[dt.datetime, dict[float, dict[int | None, np.ndarray]]] = {}
    for item_id, h0, h1 in items:
        path = work / item_id
        log.info("WAFS: downloading %s (T+%d..%d)", item_id, h0, h1)
        rr = s.get(f"{WIFS_BASE}/items/{item_id}", timeout=600)
        if rr.status_code in (401, 403):
            raise PermissionError(f"WIFS refused download (HTTP {rr.status_code})")
        rr.raise_for_status()
        path.write_bytes(rr.content)
        for m in iter_grib(path):
            if not is_edr(m) or not in_window(m["valid"], now):
                continue
            fl = message_fl(m)
            if fl is None:
                continue
            grid = to_half_degree(m["values"], m["lat1"], m["lat2"], m["lon1"], m["dlon"])
            per_time.setdefault(m["valid"], {}).setdefault(round(fl), {})[m.get("stat")] = grid
        path.unlink(missing_ok=True)
        time.sleep(float(os.environ.get("WIFS_SLEEP", "10")))  # be polite, as NOAA's examples do
    out = {}
    for t, levels in per_time.items():
        avail = sorted(levels)
        layers = []
        for fl in FLS:
            near = min(avail, key=lambda x: abs(x - fl))
            if abs(near - fl) > 25:
                break
            by_stat = levels[near]
            layers.append(by_stat.get(want_stat, next(iter(by_stat.values()))))
        if len(layers) == len(FLS):
            out[t] = np.stack(layers)
    return out


# ------------------------------------------------------------------ source 2: GFS + Ellrod
GFS_LEVELS = [150, 200, 250, 300, 350]
FL_TO_CENTER = {300: (300, 350, 250), 340: (250, 300, 200), 390: (200, 250, 150)}
# Ellrod TI1 (x1e-7 s^-2) -> approximate EDR. Heuristic; tune against PIREPs.
TI_TO_EDR = ([0, 2, 4, 8, 12, 20], [0.0, 0.05, 0.12, 0.22, 0.35, 0.5])


def ellrod_edr(u: dict[int, np.ndarray], v: dict[int, np.ndarray]) -> np.ndarray:
    """Ellrod TI1 = VWS x (DEF + CVG) on the 0.5 deg grid for each FL in FLS, mapped to EDR."""
    R = 6.371e6
    lats = np.deg2rad(90 - np.arange(NLAT) * RES)
    coslat = np.maximum(np.cos(lats), 0.05)[:, None]
    dx = R * coslat * np.deg2rad(RES)
    dy = R * np.deg2rad(RES)

    def ddx(f):
        return (np.roll(f, -1, axis=1) - np.roll(f, 1, axis=1)) / (2 * dx)

    def ddy(f):  # rows run north -> south, so north neighbour is i-1
        g = np.empty_like(f)
        g[1:-1] = (f[:-2] - f[2:]) / (2 * dy)
        g[0], g[-1] = g[1], g[-2]
        return g

    def z(p):  # standard-atmosphere height (m)
        return 44330.77 * (1 - (p / 1013.25) ** 0.190263)

    layers = []
    for fl in FLS:
        pc, plo, phi = FL_TO_CENTER[fl]
        du, dv = u[phi] - u[plo], v[phi] - v[plo]
        vws = np.hypot(du, dv) / (z(phi) - z(plo))
        uc, vc = u[pc], v[pc]
        dst = ddx(uc) - ddy(vc)
        dsh = ddx(vc) + ddy(uc)
        cvg = -(ddx(uc) + ddy(vc))
        ti = np.clip(vws * (np.hypot(dst, dsh) + cvg), 0, None) * 1e7
        layers.append(np.interp(ti, *TI_TO_EDR).astype(np.float32))
    return np.stack(layers)


def latest_gfs_cycle(now: dt.datetime, last_fh: int) -> tuple[str, str] | None:
    base = now.replace(minute=0, second=0, microsecond=0)
    for back in range(3, 30):
        c = base - dt.timedelta(hours=back)
        if c.hour % 6:
            continue
        d, h = c.strftime("%Y%m%d"), f"{c.hour:02d}"
        url = f"{NOMADS_DIR}/gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2full.0p50.f{last_fh:03d}.idx"
        try:
            if http_get(url, ok_404=True, retries=1, timeout=30) is not None:
                return d, h
        except Exception:
            continue
    return None


def gfs_grids(now: dt.datetime, work: Path) -> tuple[dict[dt.datetime, np.ndarray], str]:
    last_fh = 48
    cyc = latest_gfs_cycle(now, last_fh)
    if not cyc:
        raise RuntimeError("No complete GFS cycle found on NOMADS")
    d, h = cyc
    run = dt.datetime.strptime(d + h, "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    out = {}
    for fh in range(0, last_fh + 1, 3):
        valid = run + dt.timedelta(hours=fh)
        if not in_window(valid, now):
            continue
        params = {"file": f"gfs.t{h}z.pgrb2full.0p50.f{fh:03d}", "dir": f"/gfs.{d}/{h}/atmos",
                  "var_UGRD": "on", "var_VGRD": "on", **{f"lev_{p}_mb": "on" for p in GFS_LEVELS}}
        path = work / f"gfs_{fh:03d}.grib2"
        path.write_bytes(http_get(NOMADS_FILTER, params=params, timeout=180).content)
        u, v = {}, {}
        for m in iter_grib(path):
            p = m["level"]
            if p not in GFS_LEVELS:
                continue
            grid = to_half_degree(m["values"], m["lat1"], m["lat2"], m["lon1"], m["dlon"])
            (u if m["shortName"] == "u" else v if m["shortName"] == "v" else {})[p] = grid
        path.unlink(missing_ok=True)
        if len(u) == len(v) == len(GFS_LEVELS):
            out[valid] = ellrod_edr(u, v)
        time.sleep(float(os.environ.get("NOMADS_SLEEP", "2")))
    return out, run.strftime("%Y-%m-%dT%H:%MZ")


# ------------------------------------------------------------------ entry point
def run(out_dir: Path, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    work = ensure_dir(CACHE / "grib_tmp")
    source = os.environ.get("TURB_SOURCE", "auto")   # auto | wafs | gfs
    if source in ("auto", "wafs"):
        try:
            grids = wafs_grids(now, work)
            if grids:
                return write_grids(out_dir, grids, {"source": "NOAA WAFS harmonized turbulence (EDR)",
                                                    "sourceId": "wafs"})
            log.warning("WAFS produced no usable grids")
        except Exception as e:
            log.warning("WAFS unavailable (%s); falling back to GFS", e)
            if source == "wafs":
                raise
    grids, run_t = gfs_grids(now, work)
    if not grids:
        raise RuntimeError("No turbulence grids could be produced")
    return write_grids(out_dir, grids, {"source": "NOAA GFS winds, Ellrod CAT index (approx. EDR)",
                                        "sourceId": "gfs", "run": run_t})
