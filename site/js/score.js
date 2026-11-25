// SmoothRoute scoring engine: pure functions, no DOM. Used by app.js and tests.

export const R_KM = 6371;
export const MIN_SCORED_FL = 250;          // grids cover cruise levels only
// Felt-EDR thresholds for labels (m^2/3 s^-1). Heuristic; tune against pilot reports.
export const CATEGORIES = [
  { key: "smooth", label: "Smooth", max: 0.10 },
  { key: "light", label: "Light bumps", max: 0.18 },
  { key: "choppy", label: "Light to moderate", max: 0.28 },
  { key: "moderate", label: "Moderate", max: 0.40 },
  { key: "rough", label: "Rough", max: Infinity },
];
export const LIGHT = 0.10, MODERATE = 0.20;

const toRad = (d) => (d * Math.PI) / 180;
const toDeg = (r) => (r * 180) / Math.PI;

export function haversineKm(a, b) {
  const [la1, lo1, la2, lo2] = [a.lat, a.lon, b.lat, b.lon].map(toRad);
  const h = Math.sin((la2 - la1) / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin((lo2 - lo1) / 2) ** 2;
  return 2 * R_KM * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Points every ~stepKm along the great circle. Longitudes are unwrapped (continuous). */
export function greatCircle(a, b, stepKm = 50) {
  const total = haversineKm(a, b);
  const n = Math.max(2, Math.ceil(total / stepKm));
  const [la1, lo1, la2, lo2] = [a.lat, a.lon, b.lat, b.lon].map(toRad);
  const d = total / R_KM;
  const pts = [];
  let prevLon = null;
  for (let i = 0; i <= n; i++) {
    const f = i / n;
    let lat, lon;
    if (d < 1e-9) { lat = a.lat; lon = a.lon; } else {
      const A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
      const x = A * Math.cos(la1) * Math.cos(lo1) + B * Math.cos(la2) * Math.cos(lo2);
      const y = A * Math.cos(la1) * Math.sin(lo1) + B * Math.cos(la2) * Math.sin(lo2);
      const z = A * Math.sin(la1) + B * Math.sin(la2);
      lat = toDeg(Math.atan2(z, Math.hypot(x, y)));
      lon = toDeg(Math.atan2(y, x));
    }
    if (prevLon !== null) { while (lon - prevLon > 180) lon -= 360; while (lon - prevLon < -180) lon += 360; }
    prevLon = lon;
    pts.push({ lat, lon, d: f * total });
  }
  return pts;
}

/** Typical cruise flight level for a route length. */
export function cruiseFL(totalKm) {
  if (totalKm < 500) return 280;
  if (totalKm < 1200) return 330;
  if (totalKm < 3500) return 360;
  return 380;
}

/** Simple climb / cruise / descent profile (flight level at distance d). */
export function flAt(d, total, cruise) {
  const climb = Math.min(250, total * 0.3), desc = Math.min(300, total * 0.35);
  if (d < climb) return (cruise * d) / climb;
  if (d > total - desc) return Math.max(0, (cruise * (total - d)) / desc);
  return cruise;
}

/** Decode one grid file into per-layer Uint8Array views. */
export function decodeGrid(bytes, manifest) {
  const n = manifest.nlat * manifest.nlon;
  const L = manifest.levelsFL.length;
  if (bytes.length !== n * L) throw new Error(`grid size ${bytes.length} != ${n * L}`);
  return Array.from({ length: L }, (_, i) => bytes.subarray(i * n, (i + 1) * n));
}

/** Bilinear sample of one layer; ignores missing points. Returns EDR or NaN. */
export function sampleLayer(layer, m, lat, lon) {
  const fi = (m.lat0 - lat) / m.res;
  let fj = ((((lon - m.lon0) % 360) + 360) % 360) / m.res;
  const i0 = Math.max(0, Math.min(m.nlat - 1, Math.floor(fi)));
  const i1 = Math.min(m.nlat - 1, i0 + 1);
  const j0 = Math.floor(fj) % m.nlon, j1 = (j0 + 1) % m.nlon;
  const wi = Math.min(1, Math.max(0, fi - i0)), wj = fj - Math.floor(fj);
  let sum = 0, wsum = 0;
  for (const [i, j, w] of [[i0, j0, (1 - wi) * (1 - wj)], [i0, j1, (1 - wi) * wj], [i1, j0, wi * (1 - wj)], [i1, j1, wi * wj]]) {
    const v = layer[i * m.nlon + j];
    if (v !== m.missing) { sum += v * w; wsum += w; }
  }
  return wsum > 1e-6 ? (sum / wsum) * m.scale : NaN;
}

/** Vertical interpolation between the grid's flight levels. */
export function sampleLevels(layers, m, lat, lon, fl) {
  const fls = m.levelsFL;
  if (fl <= fls[0]) return sampleLayer(layers[0], m, lat, lon);
  if (fl >= fls[fls.length - 1]) return sampleLayer(layers[fls.length - 1], m, lat, lon);
  let k = 0;
  while (fl > fls[k + 1]) k++;
  const w = (fl - fls[k]) / (fls[k + 1] - fls[k]);
  const a = sampleLayer(layers[k], m, lat, lon), b = sampleLayer(layers[k + 1], m, lat, lon);
  if (Number.isNaN(a)) return b;
  if (Number.isNaN(b)) return a;
  return a * (1 - w) + b * w;
}

/**
 * EDR at (lat, lon, fl, tMs) using time-interpolation between loaded grids.
 * grids: [{tMs, layers}] sorted by time. Returns NaN if outside the forecast window.
 */
export function sampleEDR(grids, m, lat, lon, fl, tMs, slackMs = 90 * 60e3) {
  if (!grids.length) return NaN;
  if (tMs < grids[0].tMs - slackMs || tMs > grids[grids.length - 1].tMs + slackMs) return NaN;
  if (tMs <= grids[0].tMs) return sampleLevels(grids[0].layers, m, lat, lon, fl);
  const last = grids[grids.length - 1];
  if (tMs >= last.tMs) return sampleLevels(last.layers, m, lat, lon, fl);
  let k = 0;
  while (tMs > grids[k + 1].tMs) k++;
  const g0 = grids[k], g1 = grids[k + 1];
  const w = (tMs - g0.tMs) / (g1.tMs - g0.tMs);
  const a = sampleLevels(g0.layers, m, lat, lon, fl), b = sampleLevels(g1.layers, m, lat, lon, fl);
  if (Number.isNaN(a)) return b;
  if (Number.isNaN(b)) return a;
  return a * (1 - w) + b * w;
}

export function percentile(arr, p) {
  if (!arr.length) return 0;
  const s = [...arr].sort((x, y) => x - y);
  const idx = Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1));
  return s[idx];
}

export function categorize(peak) {
  return CATEGORIES.find((c) => peak < c.max) || CATEGORIES[CATEGORIES.length - 1];
}

/** Which grid valid times a flight needs (so the app only downloads those files). */
export function neededTimes(manifestTimesMs, depMs, arrMs) {
  const out = [];
  for (let i = 0; i < manifestTimesMs.length; i++) {
    const t = manifestTimesMs[i], next = manifestTimesMs[i + 1] ?? Infinity, prev = manifestTimesMs[i - 1] ?? -Infinity;
    if (next >= depMs && prev <= arrMs) out.push(t);
  }
  return out;
}

/**
 * Score one flight.
 * opts: {path, depMs, durMin, factor, edrAt(lat, lon, fl, tMs) -> EDR|NaN}
 * Returns metrics + per-point samples for maps/charts.
 */
export function scoreFlight({ path, depMs, durMin, factor = 1, edrAt }) {
  const total = path[path.length - 1].d || 1;
  const cruise = cruiseFL(total);
  const stepMin = durMin / Math.max(1, path.length - 1);
  const samples = [];
  let cruisePts = 0, covered = 0;
  for (const p of path) {
    const fl = flAt(p.d, total, cruise);
    const tMs = depMs + (p.d / total) * durMin * 60e3;
    let felt = null;
    if (fl >= MIN_SCORED_FL) {
      cruisePts++;
      const e = edrAt(p.lat, p.lon, fl, tMs);
      if (!Number.isNaN(e)) { covered++; felt = e * factor; }
    }
    samples.push({ lat: p.lat, lon: p.lon, fl, tMs, felt });
  }
  const vals = samples.filter((s) => s.felt !== null).map((s) => s.felt);
  const coverage = cruisePts ? covered / cruisePts : 0;
  const peak = vals.length >= 20 ? percentile(vals, 95) : Math.max(0, ...vals);
  const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
  const minLight = Math.round(vals.filter((v) => v >= LIGHT).length * stepMin);
  const minMod = Math.round(vals.filter((v) => v >= MODERATE).length * stepMin);
  // 0 (glass smooth) .. 100 (very rough): blend of peak, average and time in moderate+
  const index = Math.round(100 * Math.min(1, 0.5 * (peak / 0.4) + 0.3 * (mean / 0.2) + 0.2 * Math.min(1, minMod / 30)));
  return { index, peak, mean, minLight, minMod, coverage, cruiseFL: cruise, category: categorize(peak), samples };
}

/** Point in polygon (ring of [lon, lat]); longitude is shifted near the ring to handle wrap. */
export function pointInRing(lon, lat, ring) {
  const ref = ring[0][0];
  while (lon - ref > 180) lon -= 360;
  while (lon - ref < -180) lon += 360;
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

const toMs = (v) => (v == null ? null : typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : Date.parse(v));

/** Active warnings the flight passes through (time, altitude and area all overlap). */
export function sigmetHits(samples, sigmets) {
  const hits = [];
  for (const s of sigmets || []) {
    const from = toMs(s.from) ?? -Infinity, to = toMs(s.to) ?? Infinity;
    const base = (s.baseFt ?? 0) / 100, top = (s.topFt ?? 60000) / 100;
    const hit = samples.find((p) => p.tMs >= from && p.tMs <= to && p.fl >= base - 10 && p.fl <= top + 10 &&
      p.fl >= 100 && s.rings.some((r) => pointInRing(p.lon, p.lat, r)));
    if (hit) hits.push({ ...s, atMs: hit.tMs });
  }
  return hits;
}

/** Python weekday (Mon=0) for a UTC timestamp. */
export const pyWeekday = (ms) => (new Date(ms).getUTCDay() + 6) % 7;

/** Weekdays (Mon=0) covered by the history dates in a flights file, e.g. ["2026-11-22", ...]. */
export function coveredWeekdays(days) {
  return new Set((days || []).map((d) => pyWeekday(Date.parse(`${d}T00:00:00Z`))).filter((x) => !Number.isNaN(x)));
}

/**
 * Expand the weekly pattern into concrete departures within [startMs, endMs].
 * rows: [[callsign, flightNo, airline, [[wd, depUtcMin, durMin, type], ...]], ...]
 * covered: Set of weekdays present in the history. If a weekday has NO history yet
 * (e.g. fewer than 7 days collected), we borrow the nearest weekday's operation and
 * flag it as estimated. If the weekday IS covered and the flight is absent, it
 * genuinely didn't operate that day, so we skip it.
 */
export function upcomingDepartures(rows, startMs, endMs, covered = null) {
  const out = [];
  const s = new Date(startMs);
  const day0 = Date.UTC(s.getUTCFullYear(), s.getUTCMonth(), s.getUTCDate());
  const circ = (a, b) => Math.min((a - b + 7) % 7, (b - a + 7) % 7);
  for (const [callsign, flightNo, airline, occ] of rows || []) {
    if (!occ?.length) continue;
    for (let k = -1; k <= 3; k++) {
      const midnight = day0 + k * 86400e3;
      const wd = pyWeekday(midnight);
      let o = occ.find((x) => x[0] === wd), estimatedFrom = null;
      if (!o && covered && !covered.has(wd)) {
        o = [...occ].sort((a, b) => circ(a[0], wd) - circ(b[0], wd))[0];
        estimatedFrom = o[0];
      }
      if (!o) continue;
      const depMs = midnight + o[1] * 60e3;
      if (depMs < startMs || depMs > endMs) continue;
      out.push({ callsign, flightNo, airline, depMs, durMin: o[2], type: o[3] || "", arrMs: depMs + o[2] * 60e3, estimatedFrom });
    }
  }
  return out.sort((a, b) => a.depMs - b.depMs);
}

/** Aircraft factor + display info, with sensible defaults for unknown types. */
export function aircraftInfo(type, ac) {
  const t = ac?.types?.[type];
  if (t) return { code: type, ...t, known: true };
  return { code: type || "?", name: type ? `Aircraft type ${type}` : "Unknown aircraft", cls: "narrow",
           factor: ac?.classDefault?.narrow ?? 1, wing: [0.36, 0.56], known: false };
}
