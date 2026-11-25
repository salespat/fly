"""Shared helpers: paths, HTTP with retries, gzip JSON I/O."""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("smoothroute")

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.environ.get("SMOOTHROUTE_CACHE", ROOT / "pipeline" / ".cache"))
UA = {"User-Agent": "SmoothRoute/1.0 (+https://github.com/; hobby turbulence site)"}


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def http_get(url: str, *, params=None, headers=None, timeout=90, retries=3,
             ok_404=False, backoff=5.0):
    """GET with simple retry/backoff. Returns None on 404 when ok_404=True."""
    import requests  # lazy: demo mode and tests run without it
    h = dict(UA, **(headers or {}))
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=timeout)
            if r.status_code == 404 and ok_404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", backoff * (attempt + 1)))
                log.warning("HTTP %s for %s, retrying in %.0fs", r.status_code, url, wait)
                time.sleep(min(wait, 120))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:  # network hiccup
            last_exc = e
            time.sleep(backoff * (attempt + 1))
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Giving up on {url} after {retries} attempts")


def write_json(path: Path, obj, *, gz=False, compact=True) -> None:
    ensure_dir(path.parent)
    data = json.dumps(obj, separators=(",", ":") if compact else None,
                      indent=None if compact else 2).encode()
    if gz:
        data = gzip.compress(data, mtime=0)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def file_age_days(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400 if path.exists() else 1e9
