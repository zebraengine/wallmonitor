/* Wall Connector Monitor — single-file frontend. No external dependencies.
   All timestamps arrive as UTC epoch seconds and are rendered in the browser's
   local timezone by one shared formatter, so every view agrees on the clock. */
"use strict";

/* ---------------- utilities ---------------- */

const $ = (sel, root) => (root || document).querySelector(sel);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c == null) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

const pad = (n) => String(n).padStart(2, "0");
function fmtDT(ts) {
  if (ts == null) return "—";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtT(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtDur(s) {
  if (s == null) return "—";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${pad(m)}m`;
  if (m) return `${m}m ${pad(sec)}s`;
  return `${sec}s`;
}
function fmtNum(v, digits = 1) {
  if (v == null || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return res.json();
}

const CSSVAR = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const COLORS = () => ({
  s1: CSSVAR("--series-1"), s2: CSSVAR("--series-2"), s3: CSSVAR("--series-3"), s5: CSSVAR("--series-5"),
});

const EVSE_STATES = {
  0: "Booting", 1: "Standby — no vehicle", 2: "Vehicle detected", 3: "Ready",
  4: "Vehicle connected", 5: "Scheduled charging", 6: "Negotiating", 7: "Error",
  8: "Charging (de-rated)", 9: "Charging", 10: "Charging finished", 11: "Charging paused",
};
const evseLabel = (v) => v == null ? "—" : `${EVSE_STATES[v] || "State"} (${v})`;

const EVENT_META = {
  session_start: ["Session started", "good"],
  session_end: ["Session ended", "muted"],
  charging_start: ["Charging started", "good"],
  charging_stop: ["Charging stopped", "muted"],
  alert_raised: ["Alert raised", "critical"],
  alert_cleared: ["Alert cleared", "good"],
  evse_state_change: ["EVSE state change", "muted"],
  charger_reboot: ["Charger rebooted", "serious"],
  poll_error: ["Charger unreachable", "critical"],
  poll_recovered: ["Charger reachable again", "good"],
  wifi_disconnected: ["Wi-Fi disconnected", "serious"],
  wifi_reconnected: ["Wi-Fi reconnected", "good"],
  internet_lost: ["Internet connectivity lost", "warning"],
  internet_restored: ["Internet connectivity restored", "good"],
  firmware_changed: ["Firmware version changed", "warning"],
  monitor_start: ["Monitor started", "muted"],
  monitor_stop: ["Monitor stopped", "muted"],
};
const eventLabel = (k) => (EVENT_META[k] || [k, "muted"])[0];
const eventSeverity = (k) => (EVENT_META[k] || [k, "muted"])[1];

/* ------------- unified vitals sample (DB row or SSE message) ------------- */

function fromDbRow(r) {
  return {
    ts: r.ts, power: r.total_power_w, maxPower: r.max_power_w,
    iv: r.vehicle_current_a,
    ia: r.current_a_a, ib: r.current_b_a, ic: r.current_c_a,
    va: r.voltage_a_v, vb: r.voltage_b_v, vc: r.voltage_c_v,
    gridV: r.grid_v, gridHz: r.grid_hz,
    tPcba: r.pcba_temp_c, tHandle: r.handle_temp_c, tMcu: r.mcu_temp_c,
    energy: r.session_energy_wh, connected: !!r.vehicle_connected,
    charging: !!r.contactor_closed, evse: r.evse_state, sessionId: r.session_id,
  };
}
function fromSse(m) {
  const d = m.data || {};
  return {
    ts: m.ts, power: m.total_power_w,
    iv: d.vehicle_current_a,
    ia: d.currentA_a, ib: d.currentB_a, ic: d.currentC_a,
    va: d.voltageA_v, vb: d.voltageB_v, vc: d.voltageC_v,
    gridV: d.grid_v, gridHz: d.grid_hz,
    tPcba: d.pcba_temp_c, tHandle: d.handle_temp_c, tMcu: d.mcu_temp_c,
    energy: d.session_energy_wh, connected: !!d.vehicle_connected,
    charging: !!d.contactor_closed, evse: d.evse_state, sessionId: m.session_id,
    sessionS: d.session_s, alerts: d.current_alerts || [],
  };
}

/* ---------------- chart engine (SVG, crosshair tooltip) ---------------- */

const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

function niceTicks(min, max, count) {
  if (!(isFinite(min) && isFinite(max))) return { ticks: [0, 1], min: 0, max: 1 };
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count + 0.5) || 10 * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return { ticks, min: lo, max: hi };
}

function timeTickFormat(t, spanS) {
  if (spanS > 36 * 3600) {
    const d = new Date(t * 1000);
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  const d = new Date(t * 1000);
  return spanS <= 900 ? fmtT(t) : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* lineChart(box, {series, height, unit, digits, area, zeroBase, xFrom, xTo}) */
function lineChart(box, opts) {
  // Reserve the chart's height before clearing so a live re-render never
  // collapses the page and yanks the scroll position around.
  box.style.minHeight = `${opts.height || 210}px`;
  box.textContent = "";
  box.classList.add("chart-box");
  const series = (opts.series || []).map((s) => ({ ...s, points: s.points.filter((p) => p[1] != null && isFinite(p[1])) }));
  const height = opts.height || 210;
  const width = Math.max(320, box.clientWidth || 800);
  const M = { l: 48, r: 14, t: 12, b: 24 };
  const pw = width - M.l - M.r, ph = height - M.t - M.b;

  const allPts = series.flatMap((s) => s.points);
  if (!allPts.length) {
    box.append(el("div", { class: "empty" }, "No data in this range"));
    return;
  }
  const xFrom = opts.xFrom ?? Math.min(...allPts.map((p) => p[0]));
  const xTo = opts.xTo ?? Math.max(...allPts.map((p) => p[0]));
  const spanS = Math.max(1, xTo - xFrom);
  let yMin = Math.min(...allPts.map((p) => p[1]));
  let yMax = Math.max(...allPts.map((p) => p[1]));
  if (opts.zeroBase) yMin = Math.min(0, yMin);
  const yt = niceTicks(yMin, yMax, 4);
  const X = (t) => M.l + ((t - xFrom) / spanS) * pw;
  const Y = (v) => M.t + ph - ((v - yt.min) / (yt.max - yt.min || 1)) * ph;

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height });

  // Label precision follows the tick step, so ticks never collapse into
  // duplicate rounded values (e.g. 0.5 steps labelled "1, 1, 0, -1, -1").
  const yStep = yt.ticks.length > 1 ? yt.ticks[1] - yt.ticks[0] : 1;
  const tickDigits = Math.max(0, Math.min(3, -Math.floor(Math.log10(yStep) + 1e-9)));
  for (const v of yt.ticks) {
    root.append(svg("line", { x1: M.l, x2: width - M.r, y1: Y(v), y2: Y(v), stroke: "var(--grid)", "stroke-width": 1 }));
    const label = svg("text", { x: M.l - 6, y: Y(v) + 3.5, "text-anchor": "end", class: "axis-text" });
    label.textContent = fmtNum(v, tickDigits);
    label.classList.add("axis-text");
    root.append(label);
  }
  root.append(svg("line", { x1: M.l, x2: width - M.r, y1: M.t + ph, y2: M.t + ph, stroke: "var(--baseline)", "stroke-width": 1 }));

  const nXTicks = Math.max(2, Math.min(6, Math.floor(pw / 110)));
  for (let i = 0; i <= nXTicks; i++) {
    const t = xFrom + (spanS * i) / nXTicks;
    const label = svg("text", { x: X(t), y: height - 6, "text-anchor": i === 0 ? "start" : i === nXTicks ? "end" : "middle", class: "axis-text" });
    label.textContent = timeTickFormat(t, spanS);
    root.append(label);
  }

  for (const s of series) {
    if (!s.points.length) continue;
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    if (opts.area && series.length === 1) {
      const areaD = d + `L${X(s.points[s.points.length - 1][0]).toFixed(1)},${Y(yt.min).toFixed(1)}` +
        `L${X(s.points[0][0]).toFixed(1)},${Y(yt.min).toFixed(1)}Z`;
      root.append(svg("path", { d: areaD, fill: s.color, "fill-opacity": 0.1, stroke: "none" }));
    }
    root.append(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  }

  const cross = svg("line", { x1: 0, x2: 0, y1: M.t, y2: M.t + ph, stroke: "var(--baseline)", "stroke-width": 1, visibility: "hidden" });
  root.append(cross);
  const dots = series.map((s) => {
    const dot = svg("circle", { r: 4, fill: s.color, stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden" });
    root.append(dot);
    return dot;
  });

  box.append(root);

  const tip = el("div", { class: "tooltip" });
  box.append(tip);

  const xsUnion = [...new Set(allPts.map((p) => p[0]))].sort((a, b) => a - b);
  const hit = svg("rect", { x: M.l, y: M.t, width: pw, height: ph, fill: "transparent" });
  root.append(hit);

  function nearest(arr, t) {
    let lo = 0, hi = arr.length - 1;
    while (hi - lo > 1) { const mid = (hi + lo) >> 1; (arr[mid] < t ? (lo = mid) : (hi = mid)); }
    return t - arr[lo] < arr[hi] - t ? arr[lo] : arr[hi];
  }

  function onMove(ev) {
    const rect = root.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * width;
    const t = nearest(xsUnion, xFrom + ((px - M.l) / pw) * spanS);
    const x = X(t);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x);
    cross.setAttribute("visibility", "visible");
    tip.textContent = "";
    const timeRow = el("div", { class: "tt-time" }, fmtDT(t));
    tip.append(timeRow);
    series.forEach((s, i) => {
      let best = null;
      for (const p of s.points) if (best === null || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      if (!best || Math.abs(best[0] - t) > spanS / 8) { dots[i].setAttribute("visibility", "hidden"); return; }
      dots[i].setAttribute("cx", X(best[0])); dots[i].setAttribute("cy", Y(best[1]));
      dots[i].setAttribute("visibility", "visible");
      const row = el("div", { class: "tt-row" });
      const sw = el("span", { class: "swatch" }); sw.style.background = s.color;
      row.append(sw, el("span", { class: "tt-name" }, s.name),
        el("span", { class: "tt-val" }, `${fmtNum(best[1], opts.digits ?? 1)}${opts.unit ? " " + opts.unit : ""}`));
      tip.append(row);
    });
    tip.style.display = "block";
    const bw = box.clientWidth;
    const leftPct = (x / width) * bw;
    tip.style.left = `${Math.min(bw - 150, Math.max(0, leftPct + 12))}px`;
    tip.style.top = "8px";
  }
  hit.addEventListener("pointermove", onMove);
  hit.addEventListener("pointerleave", () => {
    tip.style.display = "none";
    cross.setAttribute("visibility", "hidden");
    dots.forEach((d) => d.setAttribute("visibility", "hidden"));
  });

  if (series.length >= 2) {
    const legend = el("div", { class: "legend" });
    for (const s of series) {
      const sw = el("span", { class: "swatch" }); sw.style.background = s.color;
      legend.append(el("span", { class: "lkey" }, sw, s.name));
    }
    box.append(legend);
  }
}

function chartCard(title, sub) {
  const box = el("div", { class: "chart-box" });
  const card = el("div", { class: "chart-card" },
    el("div", { class: "chart-title" }, title),
    sub ? el("div", { class: "chart-sub" }, sub) : null,
    box);
  return { card, box };
}

function statTile(label, value, unit, sub) {
  // Always render the sub element so callers can patch it after the fact.
  return el("div", { class: "card" },
    el("div", { class: "tile-label" }, label),
    el("div", { class: "tile-value" }, value, unit ? el("span", { class: "unit" }, unit) : null),
    el("div", { class: "tile-sub" }, sub ?? ""));
}

function presetRow(presets, activeKey, onPick) {
  const row = el("div", { class: "filters" }, el("span", { class: "flabel" }, "Range"));
  for (const p of presets) {
    row.append(el("button", { class: "chip" + (p.key === activeKey ? " active" : ""), onclick: () => onPick(p.key) }, p.label));
  }
  return row;
}

/* ---------------- live connection state ---------------- */

const live = {
  es: null,
  listeners: new Set(),
  lastMsgTs: 0,
  status: null,
};

function connectSSE() {
  if (live.es) return;
  const es = new EventSource("/api/stream");
  live.es = es;
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    live.lastMsgTs = Date.now() / 1000;
    for (const fn of live.listeners) fn(msg);
    if (msg.type === "event" || (msg.type === "vitals" && msg.data && (msg.data.current_alerts || []).length)) refreshStatus();
  };
  es.onerror = () => setConnDot(false, "stream reconnecting…");
}

function setConnDot(ok, text) {
  const dot = $("#conn-dot"), t = $("#conn-text");
  dot.className = "dot " + (ok ? "ok" : "bad");
  t.textContent = text;
}

async function refreshStatus() {
  try {
    const st = await getJSON("/api/status");
    live.status = st;
    const p = st.poller || {};
    const stale = st.vitals ? (st.server_ts - st.vitals.ts) : null;
    if (p.offline) setConnDot(false, `charger unreachable (${p.last_poll_error || "no response"})`);
    else if (st.vitals) setConnDot(true, `charger online — last sample ${stale < 2 ? "now" : fmtDur(stale) + " ago"}`);
    else setConnDot(false, "no data yet");
    renderBanner(st.active_alerts || []);
    const counts = st.counts || {};
    $("#foot-info").textContent =
      `${fmtNum(counts.vitals_samples, 0)} vitals samples · ${fmtNum(counts.sessions, 0)} sessions · ` +
      `${fmtNum(counts.events, 0)} events recorded` + (p.host ? ` · watching ${p.host}` : "");
  } catch {
    setConnDot(false, "monitor unreachable");
  }
}

function renderBanner(alerts) {
  const holder = $("#alert-banner");
  holder.textContent = "";
  for (const a of alerts) {
    const sev = a.source === "device" ? "critical" : a.source === "wifi" ? "serious" : "critical";
    holder.append(el("div", { class: "banner " + sev },
      el("span", { class: "icon" }, "⚠"),
      el("span", {}, `${a.alert}`),
      el("span", { class: "when" }, `active since ${fmtDT(a.first_ts)}`)));
  }
}

/* ---------------- views ---------------- */

let cleanup = null;

async function viewLive(root) {
  const C = COLORS();
  const tiles = el("div", { class: "cards" });
  const sessionCard = el("div", {});
  const power = chartCard("Power", "Total power drawn by the vehicle — live");
  const currents = chartCard("Phase currents", "Per-phase current at the charger — live");
  root.append(tiles, sessionCard, power.card, currents.card);

  // Seed the rolling buffer with the last 15 minutes from the DB.
  const now = Date.now() / 1000;
  let buf = [];
  try {
    const hist = await getJSON(`/api/vitals?from=${now - 900}&to=${now}&points=900`);
    buf = hist.samples.map(fromDbRow);
  } catch { /* fresh DB */ }

  function renderTiles(s) {
    tiles.textContent = "";
    const st = live.status || {};
    tiles.append(
      statTile("Power", fmtNum((s.power ?? 0) / 1000, 2), "kW", s.charging ? "charging" : s.connected ? "connected, not charging" : "idle"),
      statTile("Vehicle current", fmtNum(s.iv ?? (s.ia != null ? Math.max(s.ia, s.ib ?? 0, s.ic ?? 0) : null), 1), "A"),
      statTile("Grid", `${fmtNum(s.gridV, 1)}`, "V", `${fmtNum(s.gridHz, 3)} Hz`),
      statTile("Session energy", fmtNum((s.energy ?? 0) / 1000, 2), "kWh", s.sessionId ? `session #${s.sessionId}` : "no session"),
      statTile("Plug handle temp", fmtNum(s.tHandle, 1), "°C",
        `circuit board ${fmtNum(s.tPcba, 1)} °C · processor ${fmtNum(s.tMcu, 1)} °C`),
      statTile("EVSE state", "", null, ""),
    );
    const evseTile = tiles.lastChild;
    evseTile.querySelector(".tile-value").textContent = evseLabel(s.evse);
    evseTile.querySelector(".tile-value").style.fontSize = "16px";
    evseTile.querySelector(".tile-sub").textContent = "community-reported meaning; raw value in parentheses";
    if (st.version) {
      tiles.append(statTile("Firmware", "", null, ""));
      const t = tiles.lastChild;
      t.querySelector(".tile-value").textContent = st.version.firmware_version || "—";
      t.querySelector(".tile-value").style.fontSize = "15px";
      t.querySelector(".tile-sub").textContent = `S/N ${st.version.serial_number || "?"}`;
    }
  }

  function renderSessionCard(s) {
    sessionCard.textContent = "";
    if (!s.sessionId) return;
    const started = buf.find((p) => p.sessionId === s.sessionId);
    const startTs = (live.status && live.status.active_session && live.status.active_session.start_ts) || (started && started.ts);
    sessionCard.append(el("div", { class: "chart-card" },
      el("div", { class: "chart-title" }, `Live charging session #${s.sessionId}`),
      el("dl", { class: "kv" },
        el("dt", {}, "Plugged in"), el("dd", {}, startTs ? `${fmtDT(startTs)} (${fmtDur(s.ts - startTs)} ago)` : "—"),
        el("dt", {}, "Energy this session"), el("dd", {}, `${fmtNum((s.energy ?? 0) / 1000, 2)} kWh`),
        el("dt", {}, "State"), el("dd", {}, evseLabel(s.evse)),
        el("dt", {}, "Device alerts"), el("dd", {}, (s.alerts && s.alerts.length) ? s.alerts.join(", ") : "none")),
      el("div", { class: "note" }, "Full history for this session appears under Sessions once it ends — or open it live: "),
      el("a", { href: `#/sessions/${s.sessionId}` }, "open session detail")));
  }

  function renderCharts() {
    const xTo = Date.now() / 1000;
    const xFrom = xTo - 900;
    const pts = buf.filter((p) => p.ts >= xFrom);
    lineChart(power.box, {
      series: [{ name: "Power (W)", color: C.s1, points: pts.map((p) => [p.ts, p.power]) }],
      unit: "W", digits: 0, area: true, zeroBase: true, xFrom, xTo, height: 230,
    });
    lineChart(currents.box, {
      series: [
        { name: "Phase A", color: C.s1, points: pts.map((p) => [p.ts, p.ia]) },
        { name: "Phase B", color: C.s2, points: pts.map((p) => [p.ts, p.ib]) },
        { name: "Phase C", color: C.s3, points: pts.map((p) => [p.ts, p.ic]) },
      ],
      unit: "A", digits: 1, zeroBase: true, xFrom, xTo, height: 200,
    });
  }

  if (buf.length) { renderTiles(buf[buf.length - 1]); renderSessionCard(buf[buf.length - 1]); }
  renderCharts();

  const onMsg = (msg) => {
    if (msg.type !== "vitals") return;
    const s = fromSse(msg);
    buf.push(s);
    const cutoff = Date.now() / 1000 - 960;
    while (buf.length && buf[0].ts < cutoff) buf.shift();
    renderTiles(s);
    renderSessionCard(s);
    renderCharts();
  };
  live.listeners.add(onMsg);
  return () => live.listeners.delete(onMsg);
}

const RANGE_PRESETS = [
  { key: "1h", label: "Last hour", s: 3600 },
  { key: "6h", label: "6 hours", s: 6 * 3600 },
  { key: "24h", label: "24 hours", s: 24 * 3600 },
  { key: "7d", label: "7 days", s: 7 * 24 * 3600 },
  { key: "30d", label: "30 days", s: 30 * 24 * 3600 },
  { key: "90d", label: "90 days", s: 90 * 24 * 3600 },
];
const rangeSeconds = (key) => (RANGE_PRESETS.find((p) => p.key === key) || RANGE_PRESETS[3]).s;

async function viewSessions(root, rangeKey = "30d") {
  const now = Date.now() / 1000;
  const from = now - rangeSeconds(rangeKey);
  root.append(el("h2", {}, "Charging sessions"));
  root.append(presetRow(RANGE_PRESETS.slice(2), rangeKey, (k) => { render("sessions", k); }));

  const data = await getJSON(`/api/sessions?from=${from}&to=${now}`);
  const wrap = el("div", { class: "tbl-wrap" });
  if (!data.sessions.length) {
    wrap.append(el("div", { class: "empty" }, "No sessions recorded in this range yet. Plug in a vehicle and it will appear here."));
  } else {
    const tbl = el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "#"), el("th", {}, "Plugged in"), el("th", {}, "Unplugged"),
        el("th", { class: "num" }, "Duration"), el("th", { class: "num" }, "Charging time"),
        el("th", { class: "num" }, "Energy (kWh)"), el("th", { class: "num" }, "Peak power (kW)"),
        el("th", { class: "num" }, "Samples"), el("th", {}, "Status"))));
    const tbody = el("tbody", {});
    for (const s of data.sessions) {
      const ongoing = s.end_ts == null;
      const row = el("tr", { class: "clickable", onclick: () => { location.hash = `#/sessions/${s.id}`; } },
        el("td", {}, `#${s.id}`),
        el("td", {}, fmtDT(s.start_ts)),
        el("td", {}, ongoing ? "ongoing" : fmtDT(s.end_ts)),
        el("td", { class: "num" }, fmtDur((ongoing ? now : s.end_ts) - s.start_ts)),
        el("td", { class: "num" }, fmtDur(s.charging_s)),
        el("td", { class: "num" }, fmtNum((s.energy_wh ?? 0) / 1000, 2)),
        el("td", { class: "num" }, fmtNum((s.max_power_w ?? 0) / 1000, 2)),
        el("td", { class: "num" }, fmtNum(s.sample_count, 0)),
        el("td", {}, chipFor(ongoing ? "good" : "muted", ongoing ? "live" : (s.end_reason || "ended").replaceAll("_", " "))));
      tbody.append(row);
    }
    tbl.append(tbody);
    wrap.append(tbl);
  }
  root.append(wrap);
  root.append(el("div", { class: "note" }, "Click a session to review its full recorded telemetry."));
}

function chipFor(sev, text) {
  return el("span", { class: `schip ${sev}` }, el("span", { class: "sdot" }), text);
}

async function viewSessionDetail(root, id) {
  const C = COLORS();
  const data = await getJSON(`/api/sessions/${id}`);
  const s = data.session;
  const ongoing = s.end_ts == null;
  const now = Date.now() / 1000;
  const samples = data.samples.map(fromDbRow);

  root.append(el("h2", {}, `Session #${s.id} `, ongoing ? chipFor("good", "live") : chipFor("muted", "ended")));
  root.append(el("div", { class: "filters" },
    el("a", { class: "chip", href: "#/sessions" }, "← all sessions")));

  const energyKwh = (s.energy_wh ?? (samples.length ? samples[samples.length - 1].energy : 0) ?? 0) / 1000;
  const maxP = s.max_power_w ?? Math.max(0, ...samples.map((p) => p.power || 0));
  root.append(el("div", { class: "cards" },
    statTile("Plugged in", fmtDT(s.start_ts).slice(11), null, fmtDT(s.start_ts).slice(0, 10)),
    ongoing
      ? statTile("Status", "Plugged in", null, "session in progress")
      : statTile("Unplugged", fmtDT(s.end_ts).slice(11), null, fmtDT(s.end_ts).slice(0, 10)),
    statTile("Duration", fmtDur((ongoing ? now : s.end_ts) - s.start_ts), null,
      s.charging_s != null ? `charging ${fmtDur(s.charging_s)}` : "charging time totals when the session ends"),
    statTile("Energy", fmtNum(energyKwh, 2), "kWh"),
    statTile("Peak power", fmtNum(maxP / 1000, 2), "kW", s.avg_power_w != null ? `avg ${fmtNum(s.avg_power_w / 1000, 2)} kW` : null),
    statTile("Samples", fmtNum(s.sample_count ?? samples.length, 0), null, "full fidelity retained")));

  const power = chartCard("Power", "Total power over the session");
  const cur = chartCard("Phase currents", "Per-phase current");
  const volt = chartCard("Phase voltages", "Per-phase voltage");
  const temp = chartCard("Temperatures", "Plug handle, charger circuit board (PCBA), and processor (MCU)");
  root.append(power.card, el("div", { class: "grid-2" }, cur.card, volt.card), temp.card);

  const xFrom = s.start_ts, xTo = ongoing ? now : s.end_ts;
  lineChart(power.box, {
    series: [{ name: "Power (W)", color: C.s1, points: samples.map((p) => [p.ts, p.power]) }],
    unit: "W", digits: 0, area: true, zeroBase: true, xFrom, xTo, height: 240,
  });
  lineChart(cur.box, {
    series: [
      { name: "Phase A", color: C.s1, points: samples.map((p) => [p.ts, p.ia]) },
      { name: "Phase B", color: C.s2, points: samples.map((p) => [p.ts, p.ib]) },
      { name: "Phase C", color: C.s3, points: samples.map((p) => [p.ts, p.ic]) },
    ], unit: "A", digits: 1, zeroBase: true, xFrom, xTo, height: 190,
  });
  lineChart(volt.box, {
    series: [
      { name: "Phase A", color: C.s1, points: samples.map((p) => [p.ts, p.va]) },
      { name: "Phase B", color: C.s2, points: samples.map((p) => [p.ts, p.vb]) },
      { name: "Phase C", color: C.s3, points: samples.map((p) => [p.ts, p.vc]) },
    ], unit: "V", digits: 1, xFrom, xTo, height: 190,
  });
  lineChart(temp.box, {
    series: [
      { name: "Circuit board (PCBA)", color: C.s1, points: samples.map((p) => [p.ts, p.tPcba]) },
      { name: "Plug handle", color: C.s2, points: samples.map((p) => [p.ts, p.tHandle]) },
      { name: "Processor (MCU)", color: C.s3, points: samples.map((p) => [p.ts, p.tMcu]) },
    ], unit: "°C", digits: 1, xFrom, xTo, height: 190,
  });

  root.append(el("h2", {}, "Events during this session"));
  root.append(eventsTable(data.events));

  if (ongoing) {
    const onMsg = (msg) => { if (msg.type === "vitals" || msg.type === "event") render("session", id); };
    // Re-render at most every 5s while live.
    let timer = null;
    const throttled = (msg) => {
      if (msg.type !== "vitals" && msg.type !== "event") return;
      if (timer) return;
      timer = setTimeout(() => { timer = null; render("session", id); }, 5000);
    };
    live.listeners.add(throttled);
    return () => { live.listeners.delete(throttled); if (timer) clearTimeout(timer); };
  }
}

function eventsTable(events) {
  const wrap = el("div", { class: "tbl-wrap" });
  if (!events || !events.length) {
    wrap.append(el("div", { class: "empty" }, "No events in this range."));
    return wrap;
  }
  const tbl = el("table", {},
    el("thead", {}, el("tr", {},
      el("th", {}, "Time (local)"), el("th", {}, "Event"), el("th", {}, "Detail"))));
  const tbody = el("tbody", {});
  for (const ev of events) {
    let detail = "";
    if (ev.detail) {
      try {
        const d = JSON.parse(ev.detail);
        if (ev.kind === "evse_state_change") detail = `${evseLabel(d.from)} → ${evseLabel(d.to)}`;
        else detail = Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(" · ");
      } catch { detail = String(ev.detail); }
    }
    tbody.append(el("tr", {},
      el("td", {}, fmtDT(ev.ts)),
      el("td", {}, chipFor(eventSeverity(ev.kind), eventLabel(ev.kind))),
      el("td", {}, el("code", { class: "mono" }, detail))));
  }
  tbl.append(tbody);
  wrap.append(tbl);
  return wrap;
}

async function viewWifi(root, rangeKey = "24h") {
  const C = COLORS();
  const now = Date.now() / 1000;
  const from = now - rangeSeconds(rangeKey);
  root.append(el("h2", {}, "Wi-Fi health"));
  root.append(presetRow(RANGE_PRESETS.slice(0, 4), rangeKey, (k) => render("wifi", k)));

  const [data, evData] = await Promise.all([
    getJSON(`/api/wifi?from=${from}&to=${now}`),
    getJSON(`/api/events?from=${from}&to=${now}&kinds=wifi_disconnected,wifi_reconnected,internet_lost,internet_restored,poll_error,poll_recovered`),
  ]);
  const st = live.status || await getJSON("/api/status");
  const w = st.wifi;

  const tiles = el("div", { class: "cards" });
  if (w) {
    tiles.append(
      statTile("Charger Wi-Fi", "", null, ""),
      statTile("Internet (charger → Tesla)", "", null, ""),
      statTile("Signal", fmtNum(w.signal_strength, 0), "%", `RSSI ${fmtNum(w.rssi, 0)} dBm · SNR ${fmtNum(w.snr, 0)} dB`),
      statTile("Network", "", null, ""));
    const [wifiTile, netTile, , netInfo] = tiles.children;
    wifiTile.querySelector(".tile-value").replaceChildren(chipFor(w.connected ? "good" : "critical", w.connected ? "connected" : "disconnected"));
    wifiTile.querySelector(".tile-sub").textContent = `as of ${fmtDT(w.ts)}`;
    netTile.querySelector(".tile-value").replaceChildren(chipFor(w.internet ? "good" : "warning", w.internet ? "reachable" : "unreachable"));
    netTile.querySelector(".tile-sub").textContent = "the charger's own cloud link — the monitor never uses it";
    netInfo.querySelector(".tile-value").textContent = w.ssid || "—";
    netInfo.querySelector(".tile-value").style.fontSize = "16px";
    netInfo.querySelector(".tile-sub").textContent = `${w.infra_ip || "?"} · ${w.mac || "?"}`;
  } else {
    tiles.append(el("div", { class: "card" }, el("div", { class: "empty" }, "No Wi-Fi data recorded yet.")));
  }
  root.append(tiles);

  const pts = data.samples;
  const rssi = chartCard("RSSI", "Received signal strength, dBm — higher (less negative) is better");
  const snr = chartCard("Signal-to-noise ratio", "dB — above ~20 dB is healthy");
  const sig = chartCard("Signal strength", "Charger-reported quality, %");
  root.append(el("div", { class: "grid-3" }, rssi.card, snr.card, sig.card));
  lineChart(rssi.box, { series: [{ name: "RSSI", color: C.s1, points: pts.map((p) => [p.ts, p.rssi]) }], unit: "dBm", digits: 0, xFrom: from, xTo: now, height: 170 });
  lineChart(snr.box, { series: [{ name: "SNR", color: C.s1, points: pts.map((p) => [p.ts, p.snr]) }], unit: "dB", digits: 0, xFrom: from, xTo: now, height: 170 });
  lineChart(sig.box, { series: [{ name: "Signal", color: C.s1, points: pts.map((p) => [p.ts, p.signal_strength]) }], unit: "%", digits: 0, zeroBase: true, xFrom: from, xTo: now, height: 170 });

  root.append(el("h2", {}, "Connectivity events"));
  root.append(eventsTable(evData.events));
}

async function viewAlerts(root, rangeKey = "7d") {
  const now = Date.now() / 1000;
  const from = now - rangeSeconds(rangeKey);
  root.append(el("h2", {}, "Alerts"));
  const data = await getJSON(`/api/alerts?from=${from}&to=${now}`);

  const activeWrap = el("div", { class: "cards" });
  if (!data.active.length) {
    activeWrap.append(el("div", { class: "card" },
      el("div", { class: "tile-label" }, "Active alerts"),
      el("div", { class: "tile-value" }, chipFor("good", "none")),
      el("div", { class: "tile-sub" }, "the charger reports no active alerts")));
  } else {
    for (const a of data.active) {
      activeWrap.append(el("div", { class: "card" },
        el("div", { class: "tile-label" }, `${a.source} alert`),
        el("div", { class: "tile-value" }, chipFor(a.source === "wifi" ? "serious" : "critical", a.alert)),
        el("div", { class: "tile-sub" }, `since ${fmtDT(a.first_ts)} (${fmtDur(now - a.first_ts)})`)));
    }
  }
  root.append(activeWrap);

  root.append(el("h2", {}, "Alert history"));
  root.append(presetRow(RANGE_PRESETS.slice(2), rangeKey, (k) => render("alerts", k)));
  const wrap = el("div", { class: "tbl-wrap" });
  if (!data.history.length) {
    wrap.append(el("div", { class: "empty" }, "No alerts recorded in this range."));
  } else {
    const tbl = el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Alert"), el("th", {}, "Source"), el("th", {}, "First seen"),
        el("th", {}, "Cleared"), el("th", { class: "num" }, "Duration"), el("th", {}, "Status"))));
    const tbody = el("tbody", {});
    for (const a of data.history) {
      tbody.append(el("tr", {},
        el("td", {}, a.alert), el("td", {}, a.source),
        el("td", {}, fmtDT(a.first_ts)),
        el("td", {}, a.cleared_ts ? fmtDT(a.cleared_ts) : "—"),
        el("td", { class: "num" }, fmtDur((a.cleared_ts ?? now) - a.first_ts)),
        el("td", {}, a.active ? chipFor("critical", "active") : chipFor("good", "cleared"))));
    }
    tbl.append(tbody);
    wrap.append(tbl);
  }
  root.append(wrap);

  root.append(el("h2", {}, "Event timeline"),
    el("div", { class: "note" },
      "Every recorded state change with its exact local timestamp — use this to reconstruct the operating conditions around an error, " +
      "then open the matching session for the telemetry at that moment."));
  const evData = await getJSON(`/api/events?from=${from}&to=${now}`);
  root.append(eventsTable(evData.events));
}

/* ---------------- router ---------------- */

let lastViewKey = "";

async function render(view, arg) {
  if (cleanup) { try { cleanup(); } catch { /* noop */ } cleanup = null; }
  // Re-rendering the same view (live session refresh) must not move the
  // reader: hold the page height during the rebuild and restore the scroll
  // position afterwards, so the browser never clamps the scroll to the top.
  const viewKey = `${view}:${arg ?? ""}`;
  const sameView = viewKey === lastViewKey;
  lastViewKey = viewKey;
  const scrollY = window.scrollY;
  const root = $("#view");
  root.style.minHeight = sameView ? `${root.offsetHeight}px` : "";
  root.textContent = "";
  document.querySelectorAll(".tabs a").forEach((a) => {
    a.classList.toggle("active", a.dataset.view === (view === "session" ? "sessions" : view));
  });
  try {
    if (view === "live") cleanup = await viewLive(root);
    else if (view === "sessions") await viewSessions(root, arg);
    else if (view === "session") cleanup = await viewSessionDetail(root, arg);
    else if (view === "wifi") await viewWifi(root, arg);
    else if (view === "alerts") await viewAlerts(root, arg);
  } catch (ex) {
    root.append(el("div", { class: "empty" }, `Failed to load: ${ex.message}`));
  }
  if (sameView) window.scrollTo(0, scrollY);
  root.style.minHeight = "";
}

function route() {
  const hash = location.hash || "#/live";
  const parts = hash.replace(/^#\//, "").split("/");
  if (parts[0] === "sessions" && parts[1]) render("session", parts[1]);
  else if (["live", "sessions", "wifi", "alerts"].includes(parts[0])) render(parts[0]);
  else render("live");
}

/* ---------------- boot ---------------- */

function tickClock() {
  const now = new Date();
  $("#clock").textContent = fmtDT(now.getTime() / 1000);
}

function tzNote() {
  const offMin = -new Date().getTimezoneOffset();
  const sign = offMin >= 0 ? "+" : "-";
  const abs = Math.abs(offMin);
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
  $("#tz-note").textContent =
    `All timestamps are shown in ${tz} (UTC${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}); ` +
    "recorded internally as UTC from a single host clock.";
}

window.addEventListener("hashchange", route);
connectSSE();
refreshStatus();
setInterval(refreshStatus, 10000);
setInterval(tickClock, 1000);
tickClock();
tzNote();
route();
