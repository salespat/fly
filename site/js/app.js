import * as S from "./score.js";

const DATA = "data/";
const COLORS = { smooth: "--smooth", light: "--light", choppy: "--choppy", moderate: "--moderate", rough: "--rough" };
const $ = (id) => document.getElementById(id);
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const state = {
  manifest: null, airports: new Map(), byCode: new Map(), aircraft: null, sigmets: [], status: null,
  grids: new Map(), flightFiles: new Map(), results: [], filter: "all", map: null, mapLayer: null, route: null,
};

// ------------------------------------------------------------------ utils
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
const colorFor = (felt) => (felt == null ? cssVar("--muted") : cssVar(COLORS[S.categorize(felt).key]));

function fmt(ms, tz, opts) {
  try { return new Intl.DateTimeFormat(undefined, { timeZone: tz || "UTC", ...opts }).format(ms); }
  catch { return new Intl.DateTimeFormat(undefined, { timeZone: "UTC", ...opts }).format(ms) + " UTC"; }
}
const fmtTime = (ms, tz) => fmt(ms, tz, { hour: "numeric", minute: "2-digit" });
const fmtDay = (ms, tz) => fmt(ms, tz, { weekday: "short", month: "short", day: "numeric" });
const localDateKey = (ms, tz) => {   // sortable YYYY-MM-DD in the airport's time zone
  try { return new Intl.DateTimeFormat("en-CA", { timeZone: tz || "UTC", year: "numeric", month: "2-digit", day: "2-digit" }).format(ms); }
  catch { return new Date(ms).toISOString().slice(0, 10); }
};
const durText = (min) => `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, "0")}m`;
function ago(ms) {
  const h = (Date.now() - ms) / 3600e3;
  return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : `${Math.round(h)} h ago`;
}

async function getJSON(path) {
  const r = await fetch(DATA + path, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}
async function getBytes(path) {
  const r = await fetch(DATA + path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf[0] === 0x1f && buf[1] === 0x8b) {           // not auto-decoded by the server
    if (typeof DecompressionStream === "undefined") throw new Error("Your browser is too old to read forecast files.");
    const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }
  return buf;
}

// ------------------------------------------------------------------ airports
function airportLabel(a) { return `${a.iata || a.icao} – ${a.city || a.name}`; }
function resolveAirport(text) {
  const t = (text || "").trim();
  if (!t) return null;
  const code = t.split(/\s+[–-]\s+/)[0].trim().toUpperCase();
  if (state.byCode.has(code)) return state.airports.get(state.byCode.get(code));
  const q = t.toLowerCase();
  const hits = [...state.airports.values()].filter((a) => a.city.toLowerCase() === q || a.name.toLowerCase().includes(q));
  const origins = hits.filter((a) => a.isOrigin);
  return (origins.length === 1 ? origins[0] : hits.length === 1 ? hits[0] : null);
}
function fillList(el, airports) {
  el.innerHTML = airports.map((a) => `<option value="${esc(airportLabel(a))}">${esc(a.name)}</option>`).join("");
}
async function flightFile(icao) {
  if (!state.flightFiles.has(icao)) {
    state.flightFiles.set(icao, getJSON(`flights/${icao}.json`).catch(() => null));
  }
  return state.flightFiles.get(icao);
}
async function onFromChange() {
  const o = resolveAirport($("from").value);
  if (!o) return;
  const f = await flightFile(o.icao);
  const dests = f ? Object.keys(f.dest).map((k) => state.airports.get(k)).filter(Boolean) : [];
  fillList($("to-list"), dests.sort((a, b) => (a.city || a.name).localeCompare(b.city || b.name)));
}

// ------------------------------------------------------------------ grids
function gridFor(tMs) {
  if (!state.grids.has(tMs)) {
    const entry = state.manifest.times.find((t) => Date.parse(t.t) === tMs);
    state.grids.set(tMs, getBytes(entry.file).then((b) => ({ tMs, layers: S.decodeGrid(b, state.manifest) })));
  }
  return state.grids.get(tMs);
}

// ------------------------------------------------------------------ search
async function search(ev) {
  ev?.preventDefault();
  const o = resolveAirport($("from").value), d = resolveAirport($("to").value);
  const res = $("results");
  $("detail").hidden = true;
  if (!o || !d) { res.innerHTML = `<div class="empty">Pick both airports from the suggestions (type a code like YYZ or a city).</div>`; return; }
  if (o.icao === d.icao) { res.innerHTML = `<div class="empty">Origin and destination are the same.</div>`; return; }
  if (!state.manifest) { res.innerHTML = `<div class="empty">Turbulence forecast isn't available right now. Please try again later.</div>`; return; }
  history.replaceState(null, "", `#${o.iata || o.icao}-${d.iata || d.icao}`);
  const btn = document.querySelector(".go");
  btn.disabled = true;
  res.innerHTML = `<div class="empty"><span class="spinner"></span>Checking flights and forecast…</div>`;
  try {
    const file = await flightFile(o.icao);
    const rows = file?.dest?.[d.icao] || [];
    if (!rows.length) {
      res.innerHTML = `<div class="empty">No airline flights from ${esc(airportLabel(o))} to ${esc(airportLabel(d))} were seen in the last 7 days of tracking data.
        This can happen on routes with patchy ADS-B coverage, or with connecting itineraries. Try a nearby airport.</div>`;
      return;
    }
    const timesMs = state.manifest.times.map((t) => Date.parse(t.t)).sort((a, b) => a - b);
    const now = Date.now();
    const covered = S.coveredWeekdays(file.days);
    const deps = S.upcomingDepartures(rows, now, timesMs.at(-1), covered);
    if (!deps.length) {
      res.innerHTML = `<div class="empty">No departures on this route fall inside the current forecast window (until ${esc(fmtDay(timesMs.at(-1), o.tz))} ${esc(fmtTime(timesMs.at(-1), o.tz))}).
        Schedule data currently covers ${covered.size} of 7 weekdays (${esc((file.days || []).join(", ") || "none")}). Check back later.</div>`;
      return;
    }
    const need = new Set(deps.flatMap((f) => S.neededTimes(timesMs, f.depMs, f.arrMs)));
    const grids = (await Promise.all([...need].map((t) => gridFor(t).catch(() => null)))).filter(Boolean).sort((a, b) => a.tMs - b.tMs);
    const path = S.greatCircle(o, d, 50);
    state.route = { o, d, path };
    state.results = deps.map((f) => {
      const ac = S.aircraftInfo(f.type, state.aircraft);
      const sc = S.scoreFlight({ path, depMs: f.depMs, durMin: f.durMin, factor: ac.factor,
        edrAt: (lat, lon, fl, t) => S.sampleEDR(grids, state.manifest, lat, lon, fl, t) });
      const hits = S.sigmetHits(sc.samples, state.sigmets);
      if (hits.length) sc.index = Math.min(100, sc.index + 15);
      return { ...f, ac, ...sc, hits, dayKey: localDateKey(f.depMs, o.tz) };
    });
    state.filter = "all";
    render();
  } catch (e) {
    console.error(e);
    res.innerHTML = `<div class="empty">Something went wrong loading data: ${esc(e.message)}</div>`;
  } finally { btn.disabled = false; }
}

// ------------------------------------------------------------------ rendering
function spark(samples, w = 150, h = 40, maxE = 0.5) {
  const n = samples.length;
  const segs = [];
  let cur = [];
  samples.forEach((s, i) => {
    if (s.felt == null) { if (cur.length) segs.push(cur); cur = []; return; }
    cur.push(`${((i / (n - 1)) * w).toFixed(1)},${(h - Math.min(1, s.felt / maxE) * (h - 2) - 1).toFixed(1)}`);
  });
  if (cur.length) segs.push(cur);
  const y = (e) => (h - (e / maxE) * (h - 2) - 1).toFixed(1);
  const peak = Math.max(0, ...samples.map((s) => s.felt ?? 0));
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <line x1="0" x2="${w}" y1="${y(S.LIGHT)}" y2="${y(S.LIGHT)}" stroke="${cssVar("--line")}" stroke-dasharray="3 3"/>
    <line x1="0" x2="${w}" y1="${y(S.MODERATE)}" y2="${y(S.MODERATE)}" stroke="${cssVar("--line")}" stroke-dasharray="3 3"/>
    ${segs.map((p) => `<polyline fill="none" stroke="${colorFor(peak)}" stroke-width="2" points="${p.join(" ")}"/>`).join("")}
  </svg>`;
}

function badge(r) {
  if (r.coverage < 0.8) return `<span class="badge b-na">Forecast not ready</span>`;
  return `<span class="badge b-${r.category.key}">${esc(r.category.label)}</span>`;
}

function card(r, rank) {
  const { o, d } = state.route;
  const name = r.flightNo || r.callsign;
  const extra = r.coverage < 0.8 ? `forecast covers ${Math.round(r.coverage * 100)}% of cruise`
    : r.minMod ? `≈ ${r.minMod} min of moderate bumps` : r.minLight ? `≈ ${r.minLight} min of light bumps` : "smooth at cruise";
  return `<button class="card ${rank === 1 ? "best" : ""} ${r.coverage < 0.8 ? "dim" : ""}" data-i="${state.results.indexOf(r)}">
    <div class="rank">${r.coverage < 0.8 ? "–" : rank}</div>
    <div class="c2"><div class="fl">${esc(name)} <span class="sub">· ${esc(r.airline || "Unknown airline")}</span></div>
      <div class="sub">${esc(fmtDay(r.depMs, o.tz))} · ${esc(fmtTime(r.depMs, o.tz))} → ${esc(fmtTime(r.arrMs, d.tz))} · ${durText(r.durMin)}${r.flightNo ? "" : ` · callsign ${esc(r.callsign)}`}</div></div>
    <div class="c3">${badge(r)} ${r.hits.length ? `<span class="badge b-rough" title="Active turbulence/storm warning on route">⚠ warning</span>` : ""}
      <div class="sub">${esc(r.ac.name)}${r.type ? " (usually)" : ""} · ${esc(extra)}</div>
      ${r.estimatedFrom != null ? `<div class="sub">Time estimated from its ${WEEKDAYS[r.estimatedFrom]} flight (no data for this weekday yet)</div>` : ""}</div>
    <div class="c4">${spark(r.samples)}</div>
  </button>`;
}

function render() {
  const { o } = state.route;
  const res = $("results");
  const days = [...new Set(state.results.map((r) => r.dayKey))].sort();
  const pick = state.results.filter((r) => state.filter === "all" || r.dayKey === state.filter);
  const ranked = pick.filter((r) => r.coverage >= 0.8).sort((a, b) => a.index - b.index || a.peak - b.peak);
  const partial = pick.filter((r) => r.coverage < 0.8).sort((a, b) => a.depMs - b.depMs);
  const tabs = days.length > 1 ? `<div class="sub">Show: ${["all", ...days].map((k) => {
    const label = k === "all" ? "All" : fmtDay(state.results.find((r) => r.dayKey === k).depMs, o.tz);
    return `<a href="#" data-day="${k}" style="${k === state.filter ? "font-weight:700" : ""}">${esc(label)}</a>`;
  }).join(" · ")}</div>` : "";
  let html = tabs;
  if (ranked.length) html += `<div class="group-title">Ranked smoothest first</div>` + ranked.map((r, i) => card(r, i + 1)).join("");
  else html += `<div class="empty">None of these flights is fully inside the forecast window yet.</div>`;
  if (partial.length) html += `<div class="group-title">Later flights (forecast not fully available yet)</div>` + partial.map((r) => card(r, 0)).join("");
  res.innerHTML = html;
  res.querySelectorAll(".card").forEach((el) => el.addEventListener("click", () => showDetail(state.results[+el.dataset.i], el)));
  res.querySelectorAll("[data-day]").forEach((el) => el.addEventListener("click", (e) => { e.preventDefault(); state.filter = el.dataset.day; render(); }));
  if (ranked.length) showDetail(ranked[0], res.querySelector(".card"));
}

function profileChart(r) {
  const w = 420, h = 150, pad = { l: 34, r: 8, t: 8, b: 22 }, maxE = 0.5;
  const { o } = state.route;
  const X = (t) => pad.l + ((t - r.depMs) / (r.arrMs - r.depMs)) * (w - pad.l - pad.r);
  const Y = (e) => pad.t + (1 - Math.min(1, e / maxE)) * (h - pad.t - pad.b);
  let bands = "", prev = 0;
  for (const c of S.CATEGORIES) {
    const top = Math.min(c.max, maxE);
    bands += `<rect x="${pad.l}" y="${Y(top)}" width="${w - pad.l - pad.r}" height="${Y(prev) - Y(top)}" fill="${cssVar(COLORS[c.key])}" opacity="0.12"/>`;
    prev = top;
    if (top >= maxE) break;
  }
  const pts = r.samples.filter((s) => s.felt != null).map((s) => `${X(s.tMs).toFixed(1)},${Y(s.felt).toFixed(1)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const t = r.depMs + f * (r.arrMs - r.depMs);
    return `<text x="${X(t)}" y="${h - 6}" font-size="10" text-anchor="middle" fill="${cssVar("--muted")}">${esc(fmtTime(t, o.tz))}</text>`;
  }).join("");
  const yt = [0, 0.1, 0.2, 0.4].map((e) => `<text x="${pad.l - 4}" y="${Y(e) + 3}" font-size="10" text-anchor="end" fill="${cssVar("--muted")}">${e.toFixed(1)}</text>`).join("");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="Forecast turbulence over the flight">
    ${bands}${yt}${ticks}<polyline fill="none" stroke="${cssVar("--text")}" stroke-width="1.8" points="${pts}"/></svg>
    <div class="sub">Felt turbulence intensity (EDR, adjusted for ${esc(r.ac.name)}) vs. time (${esc(o.city || o.iata)} time). Peak ${r.peak.toFixed(2)}, average ${r.mean.toFixed(2)}. Times shown in departure-city time; climb and descent not scored.</div>`;
}

function seatPanel(r) {
  const ac = r.ac, tip = state.aircraft?.tips?.[ac.cls] || "";
  const [ws, we] = ac.wing || [0.36, 0.56];
  const w = 420, h = 110, x0 = 30, x1 = 400, cab = (f) => x0 + 20 + f * (x1 - x0 - 50);
  const smoother = ac.factor < 1 ? `Rides roughly ${Math.round((1 - ac.factor) * 100)}% smoother than a typical A320/737 in the same air.`
    : ac.factor > 1 ? `Feels roughly ${Math.round((ac.factor - 1) * 100)}% bumpier than a typical A320/737 in the same air.` : "Typical single-aisle ride.";
  return `<div class="seatmap"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Cabin diagram showing smoothest seats over the wing">
      <polygon points="${cab(ws)},50 ${cab(we)},50 ${cab(we) + 40},104 ${cab(ws) + 18},104" fill="${cssVar("--line")}"/>
      <polygon points="${cab(ws)},46 ${cab(we)},46 ${cab(we) + 40},6 ${cab(ws) + 18},6" fill="${cssVar("--line")}"/>
      <rect x="${x0}" y="34" width="${x1 - x0}" height="28" rx="14" fill="${cssVar("--card")}" stroke="${cssVar("--muted")}"/>
      <rect x="${cab(ws)}" y="36" width="${cab(we) - cab(ws)}" height="24" fill="${cssVar("--smooth")}" opacity="0.55"/>
      <rect x="${cab(0.82)}" y="36" width="${cab(1) - cab(0.82)}" height="24" fill="${cssVar("--rough")}" opacity="0.35"/>
      <text x="${x0 + 6}" y="52" font-size="10" fill="${cssVar("--muted")}">nose</text>
      <text x="${(cab(ws) + cab(we)) / 2}" y="52" font-size="11" text-anchor="middle" font-weight="700" fill="${cssVar("--text")}">smoothest</text>
      <text x="${(cab(0.82) + cab(1)) / 2}" y="52" font-size="10" text-anchor="middle" fill="${cssVar("--text")}">bumpiest</text>
    </svg></div>
    <p class="tip"><b>${esc(ac.name)}:</b> ${esc(tip)} ${esc(smoother)}</p>
    <p class="sub">Wing position is approximate. Check your airline's seat map for exact row numbers.${ac.known ? "" : " (Aircraft type not in our table; using single-aisle defaults.)"}</p>`;
}

function shiftRingNear(ring, lonRef) {
  const mean = ring.reduce((a, p) => a + p[0], 0) / ring.length;
  const k = Math.round((lonRef - mean) / 360);
  return ring.map(([x, y]) => [y, x + 360 * k]);
}

function drawMap(r) {
  const el = $("map");
  if (!window.L) { el.innerHTML = `<div class="empty">Map unavailable (map library failed to load).</div>`; return; }
  if (!state.map) {
    state.map = L.map(el, { worldCopyJump: false, scrollWheelZoom: false });
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    L.tileLayer(`https://{s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png`, {
      attribution: "© OpenStreetMap contributors © CARTO", subdomains: "abcd", maxZoom: 10 }).addTo(state.map);
  }
  state.mapLayer?.remove();
  const g = L.layerGroup().addTo(state.map);
  state.mapLayer = g;
  const s = r.samples;
  for (let i = 0; i < s.length - 1; i++) {
    L.polyline([[s[i].lat, s[i].lon], [s[i + 1].lat, s[i + 1].lon]], {
      color: colorFor(s[i].felt), weight: s[i].felt == null ? 3 : 6, opacity: 0.95, dashArray: s[i].felt == null ? "4 6" : null,
    }).addTo(g);
  }
  const { o, d } = state.route;
  const midLon = s[Math.floor(s.length / 2)].lon;
  for (const sig of state.sigmets) {
    const hit = r.hits.includes(sig) || r.hits.some((h) => h.rings === sig.rings);
    for (const ring of sig.rings) {
      const latlngs = shiftRingNear(ring, midLon);
      L.polygon(latlngs, { color: cssVar("--rough"), weight: hit ? 2 : 1, fillOpacity: hit ? 0.18 : 0.05, opacity: hit ? 0.9 : 0.35 })
        .bindTooltip(`${esc(sig.severity)} ${sig.hazard === "TURB" ? "turbulence" : "thunderstorm"} warning ${sig.fir ? `(${esc(sig.fir)})` : ""}`).addTo(g);
    }
  }
  L.circleMarker([s[0].lat, s[0].lon], { radius: 5, color: cssVar("--text") }).bindTooltip(o.iata || o.icao, { permanent: true, direction: "left" }).addTo(g);
  L.circleMarker([s.at(-1).lat, s.at(-1).lon], { radius: 5, color: cssVar("--text") }).bindTooltip(d.iata || d.icao, { permanent: true, direction: "right" }).addTo(g);
  state.map.fitBounds(L.latLngBounds(s.map((p) => [p.lat, p.lon])).pad(0.15));
  setTimeout(() => state.map.invalidateSize(), 50);
}

function showDetail(r, el) {
  if (!r) return;
  document.querySelectorAll(".card.active").forEach((c) => c.classList.remove("active"));
  el?.classList.add("active");
  const { o, d } = state.route;
  $("detail").hidden = false;
  $("detail-title").textContent = `${r.flightNo || r.callsign} · ${o.iata || o.icao} → ${d.iata || d.icao} · ${fmtDay(r.depMs, o.tz)} ${fmtTime(r.depMs, o.tz)}`;
  $("legend").innerHTML = S.CATEGORIES.map((c) => `<span><i style="background:${cssVar(COLORS[c.key])}"></i>${esc(c.label)}</span>`).join("") +
    `<span><i style="background:${cssVar("--muted")}"></i>Climb/descent or no forecast</span>`;
  $("profile").innerHTML = profileChart(r);
  $("seats").innerHTML = seatPanel(r);
  $("warnings").innerHTML = r.hits.map((h) => `<div class="warn">⚠ Active ${esc(h.severity)} ${h.hazard === "TURB" ? "turbulence" : "thunderstorm"} warning${h.fir ? ` (${esc(h.fir)})` : ""}
    ${h.baseFt != null ? `FL${Math.round(h.baseFt / 100)}–FL${Math.round((h.topFt ?? 0) / 100)}` : ""} on the route around ${esc(fmtTime(h.atMs, o.tz))}. Pilots usually route around these.</div>`).join("");
  drawMap(r);
}

// ------------------------------------------------------------------ init
async function init() {
  const [manifest, airports, aircraft, sigmets, status] = await Promise.all([
    getJSON("turb/manifest.json").catch(() => null), getJSON("airports.json").catch(() => []),
    getJSON("aircraft.json").catch(() => ({})), getJSON("sigmets.json").catch(() => ({ features: [] })),
    getJSON("status.json").catch(() => null)]);
  Object.assign(state, { manifest, aircraft, sigmets: sigmets.features || [], status });
  for (const [icao, iata, name, city, lat, lon, tz, isOrigin] of airports) {
    const a = { icao, iata, name, city, lat, lon, tz, isOrigin: !!isOrigin };
    state.airports.set(icao, a);
    state.byCode.set(icao, icao);
    if (iata && !state.byCode.has(iata)) state.byCode.set(iata, icao);
  }
  const all = [...state.airports.values()].sort((a, b) => (a.city || a.name).localeCompare(b.city || b.name));
  fillList($("from-list"), all.filter((a) => a.isOrigin));
  fillList($("to-list"), all);

  const banner = [];
  if (manifest?.demo || status?.demo) banner.push("<b>Demo mode:</b> the flights and turbulence shown are synthetic sample data, not a real forecast.");
  if (!manifest) banner.push("The turbulence forecast is temporarily unavailable.");
  else if (Date.now() - Date.parse(manifest.generated) > 9 * 3600e3) banner.push(`The forecast data is older than usual (updated ${ago(Date.parse(manifest.generated))}).`);
  if (banner.length) { $("banner").innerHTML = banner.join("<br>"); $("banner").hidden = false; }
  if (manifest) {
    const last = Date.parse(manifest.times.at(-1).t);
    $("meta").textContent = `Forecast: ${manifest.source} · updated ${ago(Date.parse(manifest.generated))} · covers until ${fmtDay(last)} ${fmtTime(last)} UTC` +
      (state.status?.flights?.days?.length ? ` · schedules from ${state.status.flights.days.length} days of tracking data` : "");
  }

  $("search").addEventListener("submit", search);
  $("from").addEventListener("change", onFromChange);
  $("swap").addEventListener("click", () => { const f = $("from").value; $("from").value = $("to").value; $("to").value = f; onFromChange(); });

  const m = location.hash.match(/^#([A-Z0-9]{3,4})-([A-Z0-9]{3,4})$/i);
  if (m) {
    const o = resolveAirport(m[1]), d = resolveAirport(m[2]);
    if (o) $("from").value = airportLabel(o);
    if (d) $("to").value = airportLabel(d);
    if (o) await onFromChange();
    if (o && d) search();
  }
}
init();
