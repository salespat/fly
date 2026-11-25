import datetime as dt
import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

os.environ.setdefault("SMOOTHROUTE_CACHE", tempfile.mkdtemp())
from pipeline import flights, refdata, sigmets, turbulence as T  # noqa: E402
from pipeline.common import write_json  # noqa: E402


class FlightsTest(unittest.TestCase):
    def test_compact_filters_non_airline_and_bad_rows(self):
        raw = [
            {"icao24": "C0FFEE", "callsign": "ACA101  ", "estDepartureAirport": "CYYZ", "estArrivalAirport": "CYVR",
             "firstSeen": 1000, "lastSeen": 1000 + 4 * 3600},
            {"icao24": "a1", "callsign": "N123AB", "estDepartureAirport": "KJFK", "estArrivalAirport": "KBOS",
             "firstSeen": 0, "lastSeen": 3600},                      # private registration
            {"icao24": "a2", "callsign": "DAL5", "estDepartureAirport": None, "estArrivalAirport": "KLAX",
             "firstSeen": 0, "lastSeen": 3600},                      # unknown origin
            {"icao24": "a3", "callsign": "UAL9", "estDepartureAirport": "KORD", "estArrivalAirport": "KORD",
             "firstSeen": 0, "lastSeen": 3600},                      # same airport
            {"icao24": "a4", "callsign": "WJA1", "estDepartureAirport": "CYYC", "estArrivalAirport": "CYVR",
             "firstSeen": 0, "lastSeen": 300},                       # too short
        ]
        self.assertEqual(flights.compact(raw), [["c0ffee", "ACA101", "CYYZ", "CYVR", 1000, 1000 + 4 * 3600]])

    def test_flight_number(self):
        al = {"ACA": ["AC", "Air Canada"], "RYR": ["FR", "Ryanair"], "XXX": ["", "Mystery"]}
        self.assertEqual(flights.flight_number("ACA0101", al), ("AC101", "Air Canada"))
        self.assertEqual(flights.flight_number("RYR12AB", al), ("", "Ryanair"))   # alphanumeric callsign
        self.assertEqual(flights.flight_number("XXX12", al), ("", "Mystery"))
        self.assertEqual(flights.flight_number("ZZZ1", al), ("", ""))

    def test_build_and_write_index(self):
        tmp = Path(tempfile.mkdtemp())
        ddir = tmp / "days"
        # 2026-11-18 is a Wednesday (weekday 2); 2026-11-19 Thursday
        wed = int(dt.datetime(2026, 11, 18, 12, 30, tzinfo=dt.timezone.utc).timestamp())
        thu = wed + 86400
        write_json(ddir / "2026-11-18.json.gz", [["abc123", "ACA101", "CYYZ", "CYVR", wed, wed + 290 * 60],
                                                 ["fff000", "ACA101", "CYYZ", "LFPG", wed, wed + 400 * 60]], gz=True)
        write_json(ddir / "2026-11-19.json.gz", [["def456", "ACA101", "CYYZ", "CYVR", thu + 600, thu + 600 + 295 * 60]], gz=True)
        airports = {"CYYZ": ["YYZ", "Pearson", "Toronto", 43.6, -79.6, "America/Toronto"],
                    "CYVR": ["YVR", "Vancouver Intl", "Vancouver", 49.2, -123.2, "America/Vancouver"]}
        idx = flights.build_index([dt.date(2026, 11, 18), dt.date(2026, 11, 19)], airports,
                                  {"ACA": ["AC", "Air Canada"]}, {"abc123": "B789", "def456": "A333"}, days_dir=ddir)
        self.assertEqual(list(idx), ["CYYZ"])
        self.assertNotIn("LFPG", idx["CYYZ"])  # unknown airport dropped
        cs, fno, airline, occ = idx["CYYZ"]["CYVR"][0]
        self.assertEqual((cs, fno, airline), ("ACA101", "AC101", "Air Canada"))
        self.assertEqual(occ, [[2, 750, 290, "B789"], [3, 760, 295, "A333"]])
        stats = flights.write_index(idx, airports, tmp / "out", [dt.date(2026, 11, 18)])
        self.assertEqual(stats["routes"], 1)
        alist = json.loads((tmp / "out" / "airports.json").read_text())
        self.assertEqual([a[0] for a in alist], ["CYVR", "CYYZ"])
        self.assertEqual(alist[1][-1], 1)  # CYYZ is an origin
        self.assertTrue((tmp / "out" / "flights" / "CYYZ.json").exists())


class RefdataTest(unittest.TestCase):
    def test_airports_and_tz(self):
        tz = refdata.parse_openflights_tz('193,"Lester B. Pearson","Toronto","Canada","YYZ","CYYZ",43.67,-79.63,569,-5,"A","America/Toronto","airport","OurAirports"\n')
        self.assertEqual(tz, {"CYYZ": "America/Toronto"})
        csv_text = ("id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,"
                    "municipality,scheduled_service,icao_code,iata_code,gps_code,local_code\n"
                    '1,CYYZ,large_airport,"Toronto Pearson",43.6772,-79.6306,569,NA,CA,CA-ON,Toronto,yes,CYYZ,YYZ,CYYZ,\n'
                    '2,00A,heliport,"Some Heli",40.0,-74.0,11,NA,US,US-PA,Bensalem,no,,,00A,00A\n')
        ap = refdata.parse_ourairports(csv_text, tz)
        self.assertEqual(ap, {"CYYZ": ["YYZ", "Toronto Pearson", "Toronto", 43.6772, -79.6306, "America/Toronto"]})

    def test_aircraft_db_formats(self):
        os_csv = b"'icao24','registration','typecode'\n'c0ffee','C-FAKE','B789'\n'abc','X','\n"
        self.assertEqual(refdata.parse_aircraft_db(os_csv), {"c0ffee": "B789"})
        dq = b'"icao24","typecode"\n"ABC123","a20n"\n'
        self.assertEqual(refdata.parse_aircraft_db(dq), {"abc123": "A20N"})
        jl = gzip.compress(b'{"icao":"C0FFEE","icaotype":"B38M"}\n{"icao":"x"}\n')
        self.assertEqual(refdata.parse_aircraft_db(jl), {"c0ffee": "B38M"})

    def test_airlines(self):
        txt = ('1,"Old Air","",AC,ACA,"OLD","Canada","N"\n'
               '2,"Air Canada","",AC,ACA,"AIR CANADA","Canada","Y"\n'
               '3,"No ICAO","",ZZ,\\N,"","X","Y"\n')
        self.assertEqual(refdata.parse_airlines(txt), {"ACA": ["AC", "Air Canada"]})


class TurbulenceTest(unittest.TestCase):
    def test_pressure_altitude(self):
        self.assertAlmostEqual(T.hpa_to_ft(250) / 100, 340, delta=1.5)
        self.assertAlmostEqual(T.hpa_to_ft(T.ft_to_hpa(39000)), 39000, delta=1)

    def test_message_fl(self):
        self.assertAlmostEqual(T.message_fl({"levType": 100, "levScaledValue": 25000, "levScale": 0}), 340, delta=1.5)
        self.assertAlmostEqual(T.message_fl({"levType": 100, "level": 250, "levScaledValue": None}), 340, delta=1.5)
        self.assertAlmostEqual(T.message_fl({"levType": 102, "levScaledValue": 10363, "levScale": 0}), 340, delta=0.5)
        self.assertIsNone(T.message_fl({"levType": 1, "levScaledValue": 0, "levScale": 0}))

    def test_encode(self):
        e = T.encode_edr(np.array([0.0, 0.123, 5.0, np.nan], dtype=np.float32))
        self.assertEqual(e.tolist(), [0, 12, 250, 255])

    def test_regrid_quarter_degree_south_first_and_rolled(self):
        a = np.zeros((721, 1440), dtype=np.float32)   # south->north, starting at -180E
        # spike at lat 50N, lon 10E -> row 560 (from -90), col (10+180)/0.25 = 760
        a[560, 760] = 0.4
        out = T.to_half_degree(a, -90, 90, -180, 0.25)
        self.assertEqual(out.shape, (361, 720))
        self.assertAlmostEqual(float(out[80, 20]), 0.4)     # row (90-50)/0.5, col 10/0.5
        # max-pooling keeps the spike within a neighbouring cell too, but nothing far away
        self.assertEqual(float(out[200, 400]), 0.0)

    def test_regrid_half_degree_identity(self):
        a = np.random.rand(361, 720).astype(np.float32)
        self.assertTrue(np.array_equal(T.to_half_degree(a, 90, -90, 0, 0.5), a))

    def test_ellrod_detects_shear_zone(self):
        lat = (90 - np.arange(361) * 0.5)[:, None] * np.ones((1, 720))
        jet = lambda p, k: (k * np.exp(-((lat - 45) / 4) ** 2)).astype(np.float32)
        u = {p: jet(p, {150: 30, 200: 55, 250: 60, 300: 45, 350: 30}[p]) for p in T.GFS_LEVELS}
        v = {p: np.zeros((361, 720), np.float32) for p in T.GFS_LEVELS}
        e = T.ellrod_edr(u, v)
        self.assertEqual(e.shape, (3, 361, 720))
        near_jet_flank, far = e[1, 2 * 45 - 8 * 2 + 0, 0], e[1, 2 * 90 - 20, 0]
        self.assertGreater(float(e[1].max()), 0.05)
        self.assertGreaterEqual(float(near_jet_flank), float(far))
        self.assertTrue(np.all(e >= 0) and np.all(e <= 0.5))

    def test_write_grids_roundtrip(self):
        tmp = Path(tempfile.mkdtemp())
        t = dt.datetime(2026, 11, 25, 6, tzinfo=dt.timezone.utc)
        g = np.full((3, 361, 720), 0.1, np.float32)
        g[2, 0, 0] = np.nan
        man = T.write_grids(tmp, {t: g}, {"source": "test"})
        self.assertEqual(man["times"], [{"t": "2026-11-25T06:00Z", "file": "turb/2026112506.bin.gz"}])
        raw = np.frombuffer(gzip.decompress((tmp / man["times"][0]["file"]).read_bytes()), np.uint8)
        self.assertEqual(raw.size, 3 * 361 * 720)
        self.assertEqual(raw[0], 10)
        self.assertEqual(raw[2 * 361 * 720], 255)


class SigmetTest(unittest.TestCase):
    def test_simplify(self):
        f = {"type": "Feature", "properties": {"hazard": "TURB", "qualifier": "SEV", "base": 30000, "top": 39000,
                                               "validTimeFrom": "2026-11-25T00:00:00Z", "validTimeTo": "2026-11-25T04:00:00Z",
                                               "firName": "GANDER OCEANIC"},
             "geometry": {"type": "Polygon", "coordinates": [[[-50, 50], [-40, 50], [-40, 55], [-50, 50]]]}}
        s = sigmets.simplify(f)
        self.assertEqual((s["hazard"], s["severity"], s["baseFt"], s["topFt"]), ("TURB", "SEV", 30000, 39000))
        self.assertEqual(len(s["rings"][0]), 4)
        self.assertIsNone(sigmets.simplify({"properties": {"hazard": "ICE"}, "geometry": f["geometry"]}))
        self.assertIsNone(sigmets.simplify({"properties": {"hazard": "TS"}, "geometry": {"type": "Point", "coordinates": [0, 0]}}))


if __name__ == "__main__":
    unittest.main()


class FeaturedRoutesTest(unittest.TestCase):
    AIRPORTS = {"CYYZ": ["YYZ", "Toronto Pearson", "Toronto", 43.68, -79.63, "America/Toronto"],
                "CYYC": ["YYC", "Calgary Intl", "Calgary", 51.12, -114.01, "America/Edmonton"],
                "EHAM": ["AMS", "Schiphol", "Amsterdam", 52.31, 4.76, "Europe/Amsterdam"]}

    def test_load_routes_accepts_iata_and_skips_unknown(self):
        from pipeline import featured
        p = Path(tempfile.mkdtemp()) / "r.json"
        p.write_text(json.dumps({"routes": [["YYZ", "YYC"], ["CYYZ", "EHAM"], ["YYZ", "XXX"], ["YYZ", "YYC"]]}))
        self.assertEqual(featured.load_routes(self.AIRPORTS, p), [("CYYZ", "CYYC"), ("CYYZ", "EHAM")])

    def test_match_route_merges_endpoints_dedupes_and_stitches(self):
        from pipeline import featured
        h = 3600
        deps = [["a", "ACA131", "CYYZ", "CYYC", 10 * h, 14 * h],      # direct hit
                ["b", "KLM692", "CYYZ", "", 1 * h, 3 * h],            # arrival unknown (ocean gap)
                ["c", "ACA405", "CYYZ", "CYUL", 5 * h, 6 * h]]        # other route
        arrs = [["a", "ACA131", "CYYZ", "CYYC", 10 * h + 60, 14 * h],  # same flight seen at destination
                ["b", "KLM692", "", "EHAM", 6 * h, 8 * h + 1800],     # second half of KLM692
                ["d", "WJA663", "CYYZ", "CYYC", 19 * h, 23 * h]]      # only seen at destination
        self.assertEqual([r[1] for r in featured.match_route("CYYZ", "CYYC", deps, arrs)], ["ACA131", "WJA663"])
        ams = featured.match_route("CYYZ", "EHAM", deps, arrs)
        self.assertEqual(ams, [["b", "KLM692", "CYYZ", "EHAM", 1 * h, 8 * h + 1800]])

    def test_empty_two_hour_block_means_day_is_not_cached(self):
        class Client:
            calls = 0
            def flights_all(self, b, e):
                Client.calls += 1
                return [] if Client.calls == 5 else [{"icao24": "x", "callsign": "ACA1", "estDepartureAirport": "CYYZ",
                                                      "estArrivalAirport": "CYYC", "firstSeen": b, "lastSeen": b + 4 * 3600}]
        os.environ["OPENSKY_SLEEP"] = "0"
        with self.assertRaises(RuntimeError):
            flights.fetch_day(Client(), dt.date(2026, 11, 20))

    def test_featured_airports_listed_even_without_flights(self):
        out = Path(tempfile.mkdtemp())
        stats = flights.write_index({}, self.AIRPORTS, out, [dt.date(2026, 11, 20)],
                                    featured=[("CYYZ", "CYYC"), ("CYYZ", "EHAM")])
        codes = {a[0]: a[-1] for a in json.loads((out / "airports.json").read_text())}
        self.assertEqual(codes, {"CYYZ": 1, "CYYC": 0, "EHAM": 0})
        self.assertEqual(json.loads((out / "featured.json").read_text())[1], {"from": "CYYZ", "to": "EHAM", "flights": 0})
        self.assertEqual(stats["airports"], 3)
