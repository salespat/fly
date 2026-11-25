"""Build all static data for the site.

    python -m pipeline.build                 # real data (needs network + OpenSky API client)
    python -m pipeline.build --demo          # synthetic sample data, no network
    python -m pipeline.build --skip-flights  # only refresh turbulence + warnings
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import traceback
from pathlib import Path

from .common import ROOT, log, write_json


def build_real(out: Path, skip_flights: bool) -> dict:
    from . import flights, refdata, sigmets, turbulence
    status: dict = {"demo": False, "errors": []}
    try:
        m = turbulence.run(out)
        status["turbulence"] = {"source": m["source"], "times": len(m["times"])}
    except Exception as e:
        traceback.print_exc()
        status["errors"].append(f"turbulence: {e}")
    if not skip_flights:
        try:
            airports = refdata.load_airports()
            airlines = refdata.load_airlines()
            types = refdata.load_aircraft_types()
            status["flights"] = flights.run(out, airports, airlines, types)
        except Exception as e:
            traceback.print_exc()
            status["errors"].append(f"flights: {e}")
    try:
        status.update(sigmets.run(out))
    except Exception as e:
        status["errors"].append(f"sigmets: {e}")
    return status


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "site" / "data"))
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--skip-flights", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = Path(a.out)
    if a.demo:
        from .demo import build_demo
        status = build_demo(out)
    else:
        status = build_real(out, a.skip_flights)
    status["built"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    write_json(out / "status.json", status, compact=False)
    log.info("Build status: %s", status)
    # Fail (so the previous deployment stays live) if there is no turbulence data.
    if not (out / "turb" / "manifest.json").exists() or any(e.startswith("turbulence") for e in status.get("errors", [])):
        log.error("No turbulence data produced; failing build")
        return 1
    if not (out / "airports.json").exists():
        log.error("No flight index available; failing build")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
