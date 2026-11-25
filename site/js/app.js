import * as S from "./score.js";

const DATA = "data/";
const COLORS = { smooth: "--smooth", light: "--light", choppy: "--choppy", moderate: "--moderate", rough: "--rough" };
const $ = (id) => document.getElementById(id);
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const state = {
  manifest: null, airports: new Map(), byCode: new Map(), aircraft: null, sigmets: [], status: null,
  grids: new Map(), flightFiles: new Map(), results: [], filter: "all", map: null, mapLayer: null, route: null,
  featured: [],
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
  // destinations seen in tracking data + featured routes from this origin (always offered)
  const codes = new Set([...Object.keys(f?.dest || {}), ...state.featured.filter((r) => r.from === o.icao).map((r) => r.to)]);
  let dests = [...codes].map((k) => state.airports.get(k)).filter(Boolean);
  if (!dests.length) dests = [...state.airports.values()].filter((a) => a.icao !== o.icao);
  fillList($("to-list"), dests.sort((a, b) => (a.city || a.name).localeCompare(b.city || b.name)));
}

function renderFeatured() {
  const routes = state.featured.filter((r) => state.airports.has(r.from) && state.airports.has(r.to));
  if (!routes.length) return;
  const name = (a) => a.city || a.name;
  $("featured").innerHTML = `<span class="featured-label">Popular routes</span>` + routes.map((r) => {
    const o = state.airports.get(r.from), d = state.airports.get(r.to);
    return `<button type="button" class="chip-route" data-from="${esc(r.from)}" data-to="${esc(r.to)}">
      ${esc(name(o))} <span aria-hidden="true">→</span><span class="sr">to</span> ${esc(name(d))}
      <small>${esc(o.iata || o.icao)}–${esc(d.iata || d.icao)}</small></button>`;
  }).join("");
  $("featured").hidden = false;
  for (const b of $("featured").querySelectorAll(".chip-route")) {
    b.addEventListener("click", async () => {
      $("from").value = airportLabel(state.airports.get(b.dataset.from));
      $("to").value = airportLabel(state.airports.get(b.dataset.to));
      await onFromChange();
      search();
    });
  }
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
  res.innerHTML = skeleton(3);
  try {
    const file = await flightFile(o.icao);
    const rows = file?.dest?.[d.icao] || [];
    if (!rows.length) {
      const isFeatured = state.featured.some((r) => r.from === o.icao && r.to === d.icao);
      res.innerHTML = isFeatured
        ? `<div class="empty">Flight data for ${esc(airportLabel(o))} → ${esc(airportLabel(d))} is still being collected.
            Schedules are learned from the last 7 days of flight tracking, so check back after the next data update (every few hours).</div>`
        : `<div class="empty">No airline flights from ${esc(airportLabel(o))} to ${esc(airportLabel(d))} were seen in the last 7 days of tracking data.
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
const PLANE = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg>`;
let gradId = 0;

/** Smooth area sparkline of felt turbulence along the flight. */
function spark(samples, w = 170, h = 40, maxE = 0.45) {
  const n = samples.length, id = `g${++gradId}`;
  const peak = Math.max(0, ...samples.map((s) => s.felt ?? 0));
  const col = colorFor(peak);
  const X = (i) => ((i / (n - 1)) * w).toFixed(1);
  const Y = (e) => (h - Math.min(1, e / maxE) * (h - 4) - 2).toFixed(1);
  const segs = [];
  let cur = [];
  samples.forEach((s, i) => { if (s.felt == null) { if (cur.length) segs.push(cur); cur = []; } else cur.push([X(i), Y(s.felt)]); });
  if (cur.length) segs.push(cur);
  const paths = segs.map((p) => {
    const line = p.map(([x, y], k) => `${k ? "L" : "M"}${x},${y}`).join("");
    return `<path d="${line}L${p.at(-1)[0]},${h}L${p[0][0]},${h}Z" fill="url(#${id})"/><path d="${line}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  }).join("");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".28"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    <line x1="0" x2="${w}" y1="${Y(S.MODERATE)}" y2="${Y(S.MODERATE)}" stroke="#e4edf6" stroke-dasharray="3 4"/>
    ${paths}</svg>`;
}

function pill(r) {
  if (r.coverage < 0.8) return `<span class="pill p-na">Forecast not ready</span>`;
  return `<span class="pill p-${r.category.key}">${esc(r.category.label)}</span>`;
}

function card(r, rank) {
  const { o, d } = state.route;
  const name = r.flightNo || r.callsign;
  const bumps = r.coverage < 0.8 ? `Forecast covers ${Math.round(r.coverage * 100)}% of cruise so far`
    : r.minMod ? `About <b>${r.minMod} min</b> of moderate bumps` : r.minLight ? `About <b>${r.minLight} min</b> of light bumps` : `<b>Smooth</b> at cruise`;
  const notes = [];
  if (r.estimatedFrom != null) notes.push(`Time estimated from its ${WEEKDAYS[r.estimatedFrom]} flight (no data for this weekday yet)`);
  if (!r.flightNo) notes.push(`Callsign ${esc(r.callsign)}`);
  const best = rank === 1;
  return `<button class="card ${best ? "best" : ""} ${r.coverage < 0.8 ? "dim" : ""}" data-i="${state.results.indexOf(r)}">
    ${best ? `<span class="best-tag">Smoothest pick</span>` : ""}
    <div class="card-top">
      <div class="rank">${r.coverage < 0.8 ? "·" : rank}</div>
      <div class="airline"><span class="fno">${esc(name)}</span><span class="aname">${esc(r.airline || "Unknown airline")}</span></div>
      <div class="pills">${r.hits.length ? `<span class="pill p-warn" title="Active turbulence or storm warning on this route">⚠ Warning on route</span>` : ""}${pill(r)}</div>
    </div>
    <div class="times">
      <div class="t"><b>${esc(fmtTime(r.depMs, o.tz))}</b><small>${esc(o.iata || o.icao)} · ${esc(fmtDay(r.depMs, o.tz))}</small></div>
      <div class="track"><span>${PLANE}${durText(r.durMin)}</span></div>
      <div class="t right"><b>${esc(fmtTime(r.arrMs, d.tz))}</b><small>${esc(d.iata || d.icao)} · local time</small></div>
    </div>
    <div class="card-bottom">
      <div><div class="facts"><b>${esc(r.ac.name)}</b>${r.type ? " (usually)" : ""} · ${bumps}</div>
        ${notes.map((n) => `<div class="note">${n}</div>`).join("")}</div>
      ${spark(r.samples)}
    </div>
  </button>`;
}

function skeleton(n = 3) { return Array.from({ length: n }, () => `<div class="skeleton"></div>`).join(""); }

function render() {
  const { o, d } = state.route;
  const res = $("results");
  const days = [...new Set(state.results.map((r) => r.dayKey))].sort();
  const pick = state.results.filter((r) => state.filter === "all" || r.dayKey === state.filter);
  const ranked = pick.filter((r) => r.coverage >= 0.8).sort((a, b) => a.index - b.index || a.peak - b.peak);
  const partial = pick.filter((r) => r.coverage < 0.8).sort((a, b) => a.depMs - b.depMs);
  const seg = days.length > 1 ? `<div class="seg" role="tablist">${["all", ...days].map((k) => {
    const label = k === "all" ? "All" : fmt(state.results.find((r) => r.dayKey === k).depMs, o.tz, { weekday: "short", day: "numeric" });
    return `<button type="button" role="tab" data-day="${k}" class="${k === state.filter ? "on" : ""}">${esc(label)}</button>`;
  }).join("")}</div>` : "";
  let html = `<div class="results-head"><h2>${esc(o.city || o.iata)} → ${esc(d.city || d.iata)}<span class="count">${ranked.length} flight${ranked.length === 1 ? "" : "s"}, smoothest first</span></h2>${seg}</div>`;
  if (ranked.length) html += ranked.map((r, i) => card(r, i + 1)).join("");
  else html += `<div class="empty">None of these flights is fully inside the forecast window yet.</div>`;
  if (partial.length) html += `<div class="section-label">Later flights · forecast not fully available yet</div>` + partial.map((r) => card(r, 0)).join("");
  res.innerHTML = html;
  res.querySelectorAll(".card").forEach((el) => el.addEventListener("click", () => {
    showDetail(state.results[+el.dataset.i], el);
    $("detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  res.querySelectorAll("[data-day]").forEach((el) => el.addEventListener("click", () => { state.filter = el.dataset.day; render(); }));
  if (ranked.length) showDetail(ranked[0], res.querySelector(".card"));
}

function profileChart(r) {
  const w = 420, h = 160, pad = { l: 30, r: 6, t: 10, b: 24 }, maxE = 0.45;
  const { o } = state.route;
  const X = (t) => pad.l + ((t - r.depMs) / (r.arrMs - r.depMs)) * (w - pad.l - pad.r);
  const Y = (e) => pad.t + (1 - Math.min(1, e / maxE)) * (h - pad.t - pad.b);
  const col = colorFor(r.peak);
  const pts = r.samples.filter((s) => s.felt != null);
  const line = pts.map((s, k) => `${k ? "L" : "M"}${X(s.tMs).toFixed(1)},${Y(s.felt).toFixed(1)}`).join("");
  const area = pts.length ? `${line}L${X(pts.at(-1).tMs).toFixed(1)},${Y(0)}L${X(pts[0].tMs).toFixed(1)},${Y(0)}Z` : "";
  const grid = [[0.1, "Light"], [0.2, "Moderate"], [0.4, "Rough"]].map(([e, lbl]) =>
    `<line x1="${pad.l}" x2="${w - pad.r}" y1="${Y(e)}" y2="${Y(e)}" stroke="#e4edf6" stroke-dasharray="3 4"/>
     <text x="${w - pad.r}" y="${Y(e) - 4}" font-size="10" text-anchor="end" fill="#9db0c3">${lbl}</text>`).join("");
  const ticks = [0, 0.5, 1].map((f) => {
    const t = r.depMs + f * (r.arrMs - r.depMs);
    return `<text x="${X(t)}" y="${h - 6}" font-size="10.5" text-anchor="${f === 0 ? "start" : f === 1 ? "end" : "middle"}" fill="#6f87a0">${esc(fmtTime(t, o.tz))}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" role="img" aria-label="Forecast turbulence over the flight">
    <defs><linearGradient id="pf" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".32"/><stop offset="1" stop-color="${col}" stop-opacity=".02"/></linearGradient></defs>
    ${grid}<line x1="${pad.l}" x2="${w - pad.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="#d5e2ef"/>
    <path d="${area}" fill="url(#pf)"/><path d="${line}" fill="none" stroke="${col}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>
    ${ticks}</svg>
    <div class="chart-note">Felt turbulence for a ${esc(r.ac.name)}, in ${esc(o.city || o.iata)} time. Peak ${r.peak.toFixed(2)} · average ${r.mean.toFixed(2)} EDR. Climb and descent not scored.</div>`;
}

function seatPanel(r) {
  const ac = r.ac, tip = state.aircraft?.tips?.[ac.cls] || "";
  const [ws, we] = ac.wing || [0.36, 0.56];
  const w = 420, h = 120, x0 = 26, x1 = 404, cab = (f) => x0 + 30 + f * (x1 - x0 - 60);
  const smoother = ac.factor < 1 ? `Rides roughly ${Math.round((1 - ac.factor) * 100)}% smoother than a typical A320/737 in the same air.`
    : ac.factor > 1 ? `Feels roughly ${Math.round((ac.factor - 1) * 100)}% bumpier than a typical A320/737 in the same air.` : "A typical single-aisle ride.";
  const smooth = cssVar("--smooth"), rough = cssVar("--rough");
  return `<div class="seatmap"><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Cabin diagram: smoothest seats are over the wing">
      <path d="M${cab(ws)},52 L${cab(we)},52 L${cab(we) + 44},112 L${cab(ws) + 20},112 Z" fill="#e6effa"/>
      <path d="M${cab(ws)},68 L${cab(we)},68 L${cab(we) + 44},8 L${cab(ws) + 20},8 Z" fill="#e6effa"/>
      <path d="M${x1 - 30},60 L${x1 + 8},38 L${x1 + 8},82 Z" fill="#e6effa"/>
      <rect x="${x0}" y="44" width="${x1 - x0}" height="32" rx="16" fill="#fff" stroke="#cfdcea" stroke-width="1.5"/>
      <rect x="${cab(ws)}" y="47" width="${cab(we) - cab(ws)}" height="26" rx="6" fill="${smooth}" opacity=".22"/>
      <rect x="${cab(0.82)}" y="47" width="${cab(1) - cab(0.82)}" height="26" rx="6" fill="${rough}" opacity=".16"/>
      ${Array.from({ length: 22 }, (_, i) => `<circle cx="${cab(i / 21)}" cy="60" r="2.2" fill="#c5d4e4"/>`).join("")}
      <text x="${(cab(ws) + cab(we)) / 2}" y="40" font-size="11.5" text-anchor="middle" font-weight="700" fill="${smooth}">Smoothest</text>
      <text x="${(cab(0.82) + cab(1)) / 2}" y="40" font-size="11" text-anchor="middle" font-weight="600" fill="${rough}">Bumpiest</text>
      <text x="${x0 + 6}" y="96" font-size="10" fill="#9db0c3">Front</text>
    </svg></div>
    <p class="tip"><b>${esc(ac.name)}.</b> ${esc(tip)} ${esc(smoother)}</p>
    <p class="sub">Wing position is approximate. Check your airline's seat map for exact rows.${ac.known ? "" : " (Aircraft not in our table, so single-aisle defaults are used.)"}</p>`;
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
    state.map = L.map(el, { worldCopyJump: false, scrollWheelZoom: false, zoomControl: true });
    L.tileLayer("https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png", {
      attribution: "© OpenStreetMap contributors © CARTO", subdomains: "abcd", maxZoom: 10 }).addTo(state.map);
  }
  state.mapLayer?.remove();
  const g = L.layerGroup().addTo(state.map);
  state.mapLayer = g;
  const s = r.samples;
  L.polyline(s.map((p) => [p.lat, p.lon]), { color: "#ffffff", weight: 11, opacity: 0.9 }).addTo(g);  // soft halo
  for (let i = 0; i < s.length - 1; i++) {
    L.polyline([[s[i].lat, s[i].lon], [s[i + 1].lat, s[i + 1].lon]], {
      color: s[i].felt == null ? "#9db0c3" : colorFor(s[i].felt), weight: s[i].felt == null ? 3 : 6, opacity: 1,
      dashArray: s[i].felt == null ? "2 7" : null, lineCap: "round",
    }).addTo(g);
  }
  const { o, d } = state.route;
  const midLon = s[Math.floor(s.length / 2)].lon;
  for (const sig of state.sigmets) {
    const hit = r.hits.some((h) => h.rings === sig.rings);
    for (const ring of sig.rings) {
      L.polygon(shiftRingNear(ring, midLon), { color: cssVar("--rough"), weight: hit ? 1.5 : 1, fillOpacity: hit ? 0.14 : 0.05, opacity: hit ? 0.8 : 0.3, dashArray: "4 4" })
        .bindTooltip(`${esc(sig.severity)} ${sig.hazard === "TURB" ? "turbulence" : "thunderstorm"} warning ${sig.fir ? `(${esc(sig.fir)})` : ""}`).addTo(g);
    }
  }
  const pin = { radius: 6, color: "#fff", weight: 3, fillColor: cssVar("--accent"), fillOpacity: 1 };
  L.circleMarker([s[0].lat, s[0].lon], pin).bindTooltip(o.iata || o.icao, { permanent: true, direction: "left" }).addTo(g);
  L.circleMarker([s.at(-1).lat, s.at(-1).lon], pin).bindTooltip(d.iata || d.icao, { permanent: true, direction: "right" }).addTo(g);
  state.map.fitBounds(L.latLngBounds(s.map((p) => [p.lat, p.lon])).pad(0.18));
  setTimeout(() => state.map.invalidateSize(), 50);
}

function showDetail(r, el) {
  if (!r) return;
  document.querySelectorAll(".card.active").forEach((c) => c.classList.remove("active"));
  el?.classList.add("active");
  const { o, d } = state.route;
  $("detail").hidden = false;
  $("detail-title").textContent = `${r.flightNo || r.callsign} · ${o.iata || o.icao} → ${d.iata || d.icao} · ${fmtDay(r.depMs, o.tz)}, ${fmtTime(r.depMs, o.tz)}`;
  $("legend").innerHTML = S.CATEGORIES.map((c) => `<span><i style="background:${cssVar(COLORS[c.key])}"></i>${esc(c.label)}</span>`).join("") +
    `<span><i style="background:#9db0c3"></i>Climb, descent or no forecast</span>`;
  $("profile").innerHTML = profileChart(r);
  $("seats").innerHTML = seatPanel(r);
  $("warnings").innerHTML = r.hits.map((h) => `<div class="warn">⚠ Active ${esc(h.severity)} ${h.hazard === "TURB" ? "turbulence" : "thunderstorm"} warning${h.fir ? ` (${esc(h.fir)})` : ""}
    ${h.baseFt != null ? `FL${Math.round(h.baseFt / 100)}–FL${Math.round((h.topFt ?? 0) / 100)}` : ""} on the route around ${esc(fmtTime(h.atMs, o.tz))}. Pilots usually route around these.</div>`).join("");
  drawMap(r);
}

// ------------------------------------------------------------------ init
async function init() {
  const [manifest, airports, aircraft, sigmets, status, featured] = await Promise.all([
    getJSON("turb/manifest.json").catch(() => null), getJSON("airports.json").catch(() => []),
    getJSON("aircraft.json").catch(() => ({})), getJSON("sigmets.json").catch(() => ({ features: [] })),
    getJSON("status.json").catch(() => null), getJSON("featured.json").catch(() => [])]);
  Object.assign(state, { manifest, aircraft, sigmets: sigmets.features || [], status,
    featured: Array.isArray(featured) ? featured : [] });
  for (const [icao, iata, name, city, lat, lon, tz, isOrigin] of airports) {
    const a = { icao, iata, name, city, lat, lon, tz, isOrigin: !!isOrigin };
    state.airports.set(icao, a);
    state.byCode.set(icao, icao);
    if (iata && !state.byCode.has(iata)) state.byCode.set(iata, icao);
  }
  const all = [...state.airports.values()].sort((a, b) => (a.city || a.name).localeCompare(b.city || b.name));
  fillList($("from-list"), all.filter((a) => a.isOrigin));
  fillList($("to-list"), all);
  renderFeatured();

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
