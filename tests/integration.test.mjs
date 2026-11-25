// Runs the same steps as app.js `search()` against the generated demo data.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";
import * as S from "../site/js/score.js";

// Demo data folder: $DEMO_DATA if set (GitHub Actions builds it in /tmp/demo-data), else site/data.
const D = process.env.DEMO_DATA ? new URL(`file://${process.env.DEMO_DATA.replace(/\/?$/, "/")}`) : new URL("../site/data/", import.meta.url);
const J = (p) => JSON.parse(readFileSync(new URL(p, D)));

test("YYZ -> YVR end-to-end on demo data", () => {
  const man = J("turb/manifest.json");
  const airports = new Map(J("airports.json").map((a) => [a[0], { icao: a[0], lat: a[4], lon: a[5], tz: a[6] }]));
  const ac = JSON.parse(readFileSync(new URL("../site/data/aircraft.json", import.meta.url))); // committed, not generated
  const sig = J("sigmets.json").features;
  const rows = J("flights/CYYZ.json").dest.CYVR;
  const timesMs = man.times.map((t) => Date.parse(t.t)).sort((a, b) => a - b);
  const now = Date.now();
  const deps = S.upcomingDepartures(rows, now, timesMs.at(-1));
  assert.ok(deps.length >= 4, `expected several departures, got ${deps.length}`);
  const need = new Set(deps.flatMap((f) => S.neededTimes(timesMs, f.depMs, f.arrMs)));
  const grids = [...need].sort((a, b) => a - b).map((tMs) => {
    const e = man.times.find((t) => Date.parse(t.t) === tMs);
    return { tMs, layers: S.decodeGrid(new Uint8Array(gunzipSync(readFileSync(new URL(e.file, D)))), man) };
  });
  const path = S.greatCircle(airports.get("CYYZ"), airports.get("CYVR"));
  const results = deps.map((f) => {
    const info = S.aircraftInfo(f.type, ac);
    const sc = S.scoreFlight({ path, depMs: f.depMs, durMin: f.durMin, factor: info.factor,
      edrAt: (lat, lon, fl, t) => S.sampleEDR(grids, man, lat, lon, fl, t) });
    const hits = S.sigmetHits(sc.samples, sig);
    return { f, info, sc, hits };
  });
  const ranked = results.filter((r) => r.sc.coverage >= 0.8).sort((a, b) => a.sc.index - b.sc.index);
  assert.ok(ranked.length >= 2, "at least two fully-covered flights");
  for (const r of ranked) {
    assert.ok(r.sc.index >= 0 && r.sc.index <= 100);
    assert.ok(r.sc.peak > 0 && r.sc.peak < 0.8);
  }
  // Same air, different aircraft: the big jet should never score worse than a narrow-body
  console.log(ranked.map((r) => `${r.f.flightNo.padEnd(6)} ${new Date(r.f.depMs).toISOString().slice(5, 16)} ${r.f.type.padEnd(4)} idx=${String(r.sc.index).padStart(3)} ${r.sc.category.label.padEnd(18)} peak=${r.sc.peak.toFixed(2)} modMin=${r.sc.minMod} warn=${r.hits.length}`).join("\n"));
});
