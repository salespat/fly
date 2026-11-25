import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import * as S from "../site/js/score.js";

// Demo data folder: $DEMO_DATA if set (GitHub Actions builds it in /tmp/demo-data), else site/data.
const DEMO = process.env.DEMO_DATA ? new URL(`file://${process.env.DEMO_DATA.replace(/\/?$/, "/")}`) : new URL("../site/data/", import.meta.url);

const YYZ = { lat: 43.6772, lon: -79.6306 }, YVR = { lat: 49.1939, lon: -123.1844 };
const NRT = { lat: 35.772, lon: 140.393 };

test("haversine YYZ-YVR ~ 3350 km", () => {
  const d = S.haversineKm(YYZ, YVR);
  assert.ok(d > 3300 && d < 3400, d);
});

test("great circle endpoints, spacing and antimeridian unwrap", () => {
  const p = S.greatCircle(YYZ, YVR, 50);
  assert.ok(Math.abs(p[0].lat - YYZ.lat) < 1e-6 && Math.abs(p.at(-1).lon - YVR.lon) < 1e-6);
  assert.ok(p.length > 60 && p.length < 75);
  const q = S.greatCircle(YVR, NRT, 100);       // crosses 180
  for (let i = 1; i < q.length; i++) assert.ok(Math.abs(q[i].lon - q[i - 1].lon) < 10);
  assert.ok(q.at(-1).lon < -180);               // unwrapped westwards, so NRT appears at ~-219.6
});

test("flight profile climbs, cruises, descends", () => {
  assert.equal(S.flAt(0, 3000, 360), 0);
  assert.equal(S.flAt(1500, 3000, 360), 360);
  assert.equal(S.flAt(3000, 3000, 360), 0);
});

const M = { levelsFL: [300, 340, 390], nlat: 361, nlon: 720, lat0: 90, lon0: 0, res: 0.5, scale: 0.01, missing: 255 };
function uniformGrid(vals) {
  return vals.map((v) => new Uint8Array(361 * 720).fill(v));
}

test("bilinear and vertical sampling", () => {
  const L = uniformGrid([10, 20, 40]);
  assert.equal(S.sampleLayer(L[0], M, 43.2, -79.7).toFixed(3), "0.100");
  assert.equal(S.sampleLevels(L, M, 10, 10, 320).toFixed(3), "0.150");
  assert.equal(S.sampleLevels(L, M, 10, 10, 450).toFixed(3), "0.400");
  // missing values ignored
  const g = new Uint8Array(361 * 720).fill(255);
  assert.ok(Number.isNaN(S.sampleLayer(g, M, 0, 0)));
  // antimeridian: column 719 (359.5E) and column 0 interpolate
  const h = new Uint8Array(361 * 720).fill(0);
  for (let i = 0; i < 361; i++) h[i * 720 + 719] = 100;
  assert.equal(S.sampleLayer(h, M, 0, -0.25).toFixed(2), "0.50");
});

test("time interpolation and window", () => {
  const t0 = Date.UTC(2026, 10, 25, 0), t1 = t0 + 3 * 3600e3;
  const grids = [{ tMs: t0, layers: uniformGrid([10, 10, 10]) }, { tMs: t1, layers: uniformGrid([30, 30, 30]) }];
  assert.equal(S.sampleEDR(grids, M, 0, 0, 340, t0 + 1.5 * 3600e3).toFixed(2), "0.20");
  assert.ok(Number.isNaN(S.sampleEDR(grids, M, 0, 0, 340, t1 + 3 * 3600e3)));
});

test("scoring ranks a smoother flight lower and respects aircraft factor", () => {
  const path = S.greatCircle(YYZ, YVR, 50);
  const calm = S.scoreFlight({ path, depMs: 0, durMin: 290, factor: 1, edrAt: () => 0.05 });
  const bumpy = S.scoreFlight({ path, depMs: 0, durMin: 290, factor: 1, edrAt: (lat, lon) => (lon < -105 ? 0.3 : 0.05) });
  const bumpyBig = S.scoreFlight({ path, depMs: 0, durMin: 290, factor: 0.85, edrAt: (lat, lon) => (lon < -105 ? 0.3 : 0.05) });
  assert.equal(calm.category.key, "smooth");
  assert.ok(bumpy.index > calm.index);
  assert.ok(bumpyBig.index < bumpy.index);
  assert.ok(bumpy.minMod > 30);
  assert.equal(calm.coverage, 1);
  const none = S.scoreFlight({ path, depMs: 0, durMin: 290, edrAt: () => NaN });
  assert.equal(none.coverage, 0);
});

test("point in polygon incl. wrap", () => {
  const ring = [[-118, 48], [-108, 48], [-106, 53], [-116, 54], [-118, 48]];
  assert.ok(S.pointInRing(-112, 50, ring));
  assert.ok(S.pointInRing(-112 + 360, 50, ring));
  assert.ok(!S.pointInRing(-100, 50, ring));
});

test("sigmet hits need time, altitude and area overlap", () => {
  const samples = [{ lat: 50, lon: -112, fl: 360, tMs: Date.parse("2026-11-25T03:00:00Z") }];
  const sig = { rings: [[[-118, 48], [-108, 48], [-106, 53], [-116, 54], [-118, 48]]], baseFt: 28000, topFt: 38000,
                from: "2026-11-25T00:00:00Z", to: "2026-11-25T06:00:00Z" };
  assert.equal(S.sigmetHits(samples, [sig]).length, 1);
  assert.equal(S.sigmetHits(samples, [{ ...sig, topFt: 30000 }]).length, 0);
  assert.equal(S.sigmetHits(samples, [{ ...sig, to: "2026-11-25T01:00:00Z" }]).length, 0);
  assert.equal(S.sigmetHits(samples, [{ ...sig, from: 1795564800, to: 1795586400 }]).length, 1); // epoch seconds
});

test("weekly pattern -> concrete departures", () => {
  // 2026-11-25 is a Wednesday (python weekday 2)
  const start = Date.UTC(2026, 10, 25, 10), end = start + 36 * 3600e3;
  assert.equal(S.pyWeekday(start), 2);
  const rows = [["ACA101", "AC101", "Air Canada", [[2, 12 * 60, 290, "B789"], [3, 12 * 60, 290, "A333"]]],
                ["WJA709", "WS709", "WestJet", [[2, 9 * 60, 300, "B738"]]]];
  const deps = S.upcomingDepartures(rows, start, end);
  assert.deepEqual(deps.map((d) => [d.flightNo, d.type, new Date(d.depMs).toISOString()]), [
    ["AC101", "B789", "2026-11-25T12:00:00.000Z"],
    ["AC101", "A333", "2026-11-26T12:00:00.000Z"],
  ]); // WS709 at 09:00 Wed is before the window and doesn't fly Thu
});

test("needed grid times cover the flight", () => {
  const h = 3600e3, ts = [0, 3, 6, 9, 12].map((x) => x * h);
  assert.deepEqual(S.neededTimes(ts, 4 * h, 8 * h).map((t) => t / h), [3, 6, 9]);
});

test("demo grid files decode with the manifest", () => {
  const man = JSON.parse(readFileSync(new URL("turb/manifest.json", DEMO)));
  const raw = gunzipSync(readFileSync(new URL(man.times[0].file, DEMO)));
  const layers = S.decodeGrid(new Uint8Array(raw), man);
  assert.equal(layers.length, 3);
  const v = S.sampleLevels(layers, man, 47, -100, 360);
  assert.ok(v > 0 && v < 0.7, v);
});

test("missing weekdays fall back to nearest operation, covered weekdays don't", () => {
  // History only has Sun (6) + Mon (0); window is Wed 25 -> Thu 26 Nov 2026
  const covered = S.coveredWeekdays(["2026-11-22", "2026-11-23"]);
  assert.deepEqual([...covered].sort(), [0, 6]);
  const start = Date.UTC(2026, 10, 25, 10), end = start + 36 * 3600e3;
  const rows = [["ACA101", "AC101", "Air Canada", [[0, 12 * 60, 290, "B789"], [6, 13 * 60, 295, "A333"]]]];
  const deps = S.upcomingDepartures(rows, start, end, covered);
  assert.deepEqual(deps.map((d) => [new Date(d.depMs).toISOString(), d.type, d.estimatedFrom]), [
    ["2026-11-25T12:00:00.000Z", "B789", 0],   // Wed borrows Mon
    ["2026-11-26T12:00:00.000Z", "B789", 0],   // Thu: Mon (3 days) is nearer than Sun (4 days)
  ]);
  // Full week of history: Wed is covered and the flight isn't listed, so it's skipped
  const all = new Set([0, 1, 2, 3, 4, 5, 6]);
  assert.equal(S.upcomingDepartures(rows, start, end, all).length, 0);
});
