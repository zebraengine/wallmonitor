/* Wall Connector Monitor — single-file frontend. No external dependencies.
   All timestamps arrive as UTC epoch seconds and are rendered in the browser's
   local timezone by one shared formatter, so every view agrees on the clock. */
"use strict";

/* ---------------- utilities ---------------- */

const $ = (sel, root) => (root || document).querySelector(sel);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

const pad = (num) => String(num).padStart(2, "0");
function fmtDT(ts) {
  if (ts == null) return "—";
  const date = new Date(ts * 1000);
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
         `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function fmtT(ts) {
  const date = new Date(ts * 1000);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
function fmtDur(seconds) {
  if (seconds == null) return "—";
  seconds = Math.round(seconds);
  const hours = Math.floor(seconds / 3600), mins = Math.floor((seconds % 3600) / 60), secs = seconds % 60;
  if (hours) return `${hours}h ${pad(mins)}m`;
  if (mins) return `${mins}m ${pad(secs)}s`;
  return `${secs}s`;
}
function fmtNum(value, digits = 1) {
  if (value == null || Number.isNaN(value)) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
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

// Tesla doesn't document evse_state. Labels below are verified against this
// monitor's own telemetry on firmware 26.18.0 (cross-tab of a week of samples
// vs vehicle_connected/contactor_closed, ~132k samples):
//   1  — vehicle_connected false in all 35,785 samples; every unplug lands
//        here. Verified.
//   4  — vehicle_connected true in all 31,009 samples, contactor always open.
//        Pilot swings ±12 V (J1772 state B: vehicle present, not ready), vs
//        state 9's low pilot levels — consistent with the car asleep, hence
//        "idle", but the asleep reading itself is inference. Verified as
//        connected-not-charging.
//   9  — contactor open at ~0 W in all 53,148 samples. Verified. (Community
//        lists 9 as "Charging" and 11 as "Charging paused", i.e. swapped.)
//   11 — contactor closed, full power, in all 12,021 samples. Verified.
// Contradicted community labels: 7 ("Error") appeared only as a benign 5-6 s
// plug-in transient (1 → 7 → 11, charging started normally) — same handshake
// slot where 2 and 3 also occur; too few samples to verify a meaning, but not
// an error state. 5 ("Scheduled charging") never appeared even with a
// vehicle-side scheduled charge armed overnight — the charger just sat in
// 9/4; the schedule lives in the car. 8 never appeared even during a real
// thermal derate (the charger stayed in 11 and signaled via alert 40 only).
// evse_not_ready_reasons are likewise undocumented; observed correlations:
// [4,8] when no vehicle, narrowing to [4] during the plug-in handshake, then
// [1] whenever a vehicle is connected (including while charging) — meanings
// unverified, so the UI shows the raw codes.
const EVSE_STATES = {
  0: "Booting", 1: "Standby — no vehicle", 2: "Vehicle detected", 3: "Ready",
  4: "Connected, idle", 5: "Scheduled charging", 6: "Negotiating", 7: "Plug-in transition",
  8: "Charging (de-rated)", 9: "Connected, not charging", 10: "Charging finished", 11: "Charging",
};
const EVSE_VERIFIED = new Set([1, 4, 9, 11]);
const evseLabel = (state) => state == null ? "—" : `${EVSE_STATES[state] || "State"} (${state})`;

const EVENT_META = {
  session_start: ["Session started", "good"],
  session_end: ["Session ended", "muted"],
  charging_start: ["Charging started", "good"],
  charging_stop: ["Charging stopped", "muted"],
  alert_raised: ["Alert raised", "critical"],
  alert_cleared: ["Alert cleared", "good"],
  evse_state_change: ["EVSE state change", "muted"],
  evse_not_ready_change: ["Not-ready reasons changed", "muted"],
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
  monitor_gap: ["Monitoring gap — service was off", "warning"],
  thermal_drift: ["Handle heat rise increasing vs baseline", "serious"],
  thermal_drift_cleared: ["Handle heat rise back to baseline", "good"],
  derate_warning: ["Derate predicted — lower charge current", "serious"],
  derate_warning_cleared: ["Derate no longer predicted", "good"],
};
const eventLabel = (kind) => (EVENT_META[kind] || [kind, "muted"])[0];
const eventSeverity = (kind) => (EVENT_META[kind] || [kind, "muted"])[1];

/* ---------------- alert decoding ---------------- */

// Loaded once from /api/alert-codes. Tesla doesn't document the numeric codes
// the charger reports in current_alerts, so only verified entries are labeled;
// everything else renders honestly as an undocumented code.
let alertCodes = null;
async function loadAlertCodes() {
  if (alertCodes) return alertCodes;
  try { alertCodes = await getJSON("/api/alert-codes"); } catch { alertCodes = { codes: {}, categories: [] }; }
  return alertCodes;
}

const UNDOCUMENTED_HINT =
  "undocumented code — open the Tesla app while this alert is active to see its name, then add it to alert_codes.json";

function alertDisplay(alertStr, source) {
  const alertText = String(alertStr);
  const known = alertCodes && alertCodes.codes && alertCodes.codes[alertText];
  if (known) {
    return {
      label: known.label,
      sub: `code ${alertText} — ${known.description}${known.verified ? "" : " (community-reported, unverified)"}`,
    };
  }
  const numeric = /^\d+$/.test(alertText);
  if (numeric) return { label: `Alert code ${alertText}`, sub: UNDOCUMENTED_HINT };
  // Monitor/wifi alerts (and any string alerts) are already human-readable.
  return { label: alertText, sub: source === "device" ? UNDOCUMENTED_HINT : null };
}

/* ------------- unified vitals sample (DB row or SSE message) ------------- */

// 255 (0xFF) is the device's "sensor read invalid" sentinel for temperatures.
const realTemp = (value) => (value != null && value >= 255 ? null : value);

// Community-observed (not Tesla-published) circuit-board temperature at which
// the Gen 3 begins throttling charge current. Shown as a reference line so
// headroom is visible; ambient-driven foldback in hot installs is the usual
// cause of a real event. See the Alerts page notes.
const PCBA_THROTTLE_C = 95;
// Handle temperature that raises alert 40 and halves charge current
// (observed on firmware 26.18.0 — see alert_codes.json).
const HANDLE_TRIP_C = 65;

function fromDbRow(row) {
  return {
    ts: row.ts, power: row.total_power_w, maxPower: row.max_power_w,
    iv: row.vehicle_current_a,
    ia: row.current_a_a, ib: row.current_b_a, ic: row.current_c_a,
    va: row.voltage_a_v, vb: row.voltage_b_v, vc: row.voltage_c_v,
    gridV: row.grid_v, gridHz: row.grid_hz,
    tPcba: realTemp(row.pcba_temp_c), tHandle: realTemp(row.handle_temp_c), tMcu: realTemp(row.mcu_temp_c),
    energy: row.session_energy_wh, connected: !!row.vehicle_connected,
    charging: !!row.contactor_closed, evse: row.evse_state, sessionId: row.session_id,
    pilotHigh: row.pilot_high_v, pilotLow: row.pilot_low_v, prox: row.prox_v,
    relayK1: row.relay_k1_v, relayK2: row.relay_k2_v,
  };
}
function fromSse(msg) {
  const data = msg.data || {};
  return {
    ts: msg.ts, power: msg.total_power_w,
    iv: data.vehicle_current_a,
    ia: data.currentA_a, ib: data.currentB_a, ic: data.currentC_a,
    va: data.voltageA_v, vb: data.voltageB_v, vc: data.voltageC_v,
    gridV: data.grid_v, gridHz: data.grid_hz,
    tPcba: realTemp(data.pcba_temp_c), tHandle: realTemp(data.handle_temp_c), tMcu: realTemp(data.mcu_temp_c),
    energy: data.session_energy_wh, connected: !!data.vehicle_connected,
    charging: !!data.contactor_closed, evse: data.evse_state, sessionId: msg.session_id,
    sessionS: data.session_s, alerts: data.current_alerts || [],
    notReady: data.evse_not_ready_reasons || [],
    pilotHigh: data.pilot_high_v, pilotLow: data.pilot_low_v, prox: data.prox_v,
    relayK1: data.relay_k1_v ?? data.relay_coil_v, relayK2: data.relay_k2_v,
  };
}

/* ---------------- chart engine (SVG, crosshair tooltip) ---------------- */

const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, value);
  return node;
}

function niceTicks(min, max, count) {
  if (!(isFinite(min) && isFinite(max))) return { ticks: [0, 1], min: 0, max: 1 };
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((mult) => mult * mag).find((candidate) => span / candidate <= count + 0.5) || 10 * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let value = lo; value <= hi + step / 2; value += step) ticks.push(Math.round(value * 1e6) / 1e6);
  return { ticks, min: lo, max: hi };
}

function timeTickFormat(ts, spanS) {
  if (spanS > 36 * 3600) {
    const date = new Date(ts * 1000);
    return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
  const date = new Date(ts * 1000);
  return spanS <= 900 ? fmtT(ts) : `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/* scatterChart(box, {series: [{name, color, points: [[x, y, title?]]}],
   xLabel, height, digits}) — numeric x axis (not time), one dot per point.
   Used where the relationship between two measured quantities is the story
   (e.g. fitted heat rise vs window ambient), not their history. */
function scatterChart(box, opts) {
  box.style.minHeight = `${opts.height || 210}px`;
  box.textContent = "";
  box.classList.add("chart-box");
  const series = (opts.series || []).map((dataset) => (
    { ...dataset, points: dataset.points.filter((point) => point[0] != null && point[1] != null && isFinite(point[0]) && isFinite(point[1])) }
  ));
  const height = opts.height || 210;
  const width = Math.max(320, box.clientWidth || 800);
  const margin = { left: 48, right: 14, top: 12, bottom: 34 };
  const pw = width - margin.left - margin.right, ph = height - margin.top - margin.bottom;
  const allPts = series.flatMap((dataset) => dataset.points);
  if (allPts.length < 2) {
    box.append(el("div", { class: "empty" }, "Not enough fits yet — each completed charge adds a point"));
    return;
  }
  const xt = niceTicks(Math.min(...allPts.map((p) => p[0])), Math.max(...allPts.map((p) => p[0])), 6);
  const yt = niceTicks(Math.min(...allPts.map((p) => p[1])), Math.max(...allPts.map((p) => p[1])), 4);
  const xOf = (value) => margin.left + ((value - xt.min) / (xt.max - xt.min || 1)) * pw;
  const yOf = (value) => margin.top + ph - ((value - yt.min) / (yt.max - yt.min || 1)) * ph;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height });
  for (const value of yt.ticks) {
    root.append(svg("line", { x1: margin.left, x2: width - margin.right, y1: yOf(value), y2: yOf(value), stroke: "var(--grid)", "stroke-width": 1 }));
    const label = svg("text", { x: margin.left - 6, y: yOf(value) + 3.5, "text-anchor": "end", class: "axis-text" });
    label.textContent = fmtNum(value, 0);
    root.append(label);
  }
  for (const value of xt.ticks) {
    const label = svg("text", { x: xOf(value), y: height - 18, "text-anchor": "middle", class: "axis-text" });
    label.textContent = fmtNum(value, 0);
    root.append(label);
  }
  if (opts.xLabel) {
    const label = svg("text", { x: margin.left + pw / 2, y: height - 4, "text-anchor": "middle", class: "axis-text" });
    label.textContent = opts.xLabel;
    root.append(label);
  }
  root.append(svg("line", { x1: margin.left, x2: width - margin.right, y1: margin.top + ph, y2: margin.top + ph, stroke: "var(--baseline)", "stroke-width": 1 }));
  for (const dataset of series) {
    for (const point of dataset.points) {
      const dot = svg("circle", { cx: xOf(point[0]).toFixed(1), cy: yOf(point[1]).toFixed(1), r: 4.5, fill: dataset.color, "fill-opacity": 0.85 });
      if (point[2]) {
        const title = svg("title", {});
        title.textContent = point[2];
        dot.append(title);
      }
      root.append(dot);
    }
  }
  box.append(root);
  if (series.length > 1) {
    const legend = el("div", { class: "note" });
    for (const dataset of series) {
      const swatch = el("span", {}, "● ");
      swatch.style.color = dataset.color;
      legend.append(swatch, `${dataset.name} (${dataset.points.length})  `);
    }
    box.append(legend);
  }
}

/* lineChart(box, {series, height, unit, digits, area, zeroBase, xFrom, xTo}) */
function lineChart(box, opts) {
  // Reserve the chart's height before clearing so a live re-render never
  // collapses the page and yanks the scroll position around.
  box.style.minHeight = `${opts.height || 210}px`;
  box.textContent = "";
  box.classList.add("chart-box");
  const series = (opts.series || []).map((dataset) => (
    { ...dataset, points: dataset.points.filter((point) => point[1] != null && isFinite(point[1])) }
  ));
  const height = opts.height || 210;
  const width = Math.max(320, box.clientWidth || 800);
  const margin = { left: 48, right: 14, top: 12, bottom: 24 };
  const pw = width - margin.left - margin.right, ph = height - margin.top - margin.bottom;

  const allPts = series.flatMap((dataset) => dataset.points);
  if (!allPts.length) {
    box.append(el("div", { class: "empty" }, "No data in this range"));
    return;
  }
  const xFrom = opts.xFrom ?? Math.min(...allPts.map((point) => point[0]));
  const xTo = opts.xTo ?? Math.max(...allPts.map((point) => point[0]));
  const spanS = Math.max(1, xTo - xFrom);
  let yMin = Math.min(...allPts.map((point) => point[1]));
  let yMax = Math.max(...allPts.map((point) => point[1]));
  if (opts.zeroBase) yMin = Math.min(0, yMin);
  // Reference lines (e.g. a thermal-foldback threshold) are drawn in-frame so
  // the headroom between the live value and the limit is visible at a glance.
  const refLines = opts.refLines || [];
  for (const refLine of refLines) {
    if (refLine.value > yMax) yMax = refLine.value;
    if (refLine.value < yMin) yMin = refLine.value;
  }
  const yt = niceTicks(yMin, yMax, 4);
  const xOf = (ts) => margin.left + ((ts - xFrom) / spanS) * pw;
  const yOf = (value) => margin.top + ph - ((value - yt.min) / (yt.max - yt.min || 1)) * ph;

  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height });

  // Label precision follows the tick step, so ticks never collapse into
  // duplicate rounded values (e.g. 0.5 steps labelled "1, 1, 0, -1, -1").
  const yStep = yt.ticks.length > 1 ? yt.ticks[1] - yt.ticks[0] : 1;
  const tickDigits = Math.max(0, Math.min(3, -Math.floor(Math.log10(yStep) + 1e-9)));
  for (const value of yt.ticks) {
    root.append(svg("line", { x1: margin.left, x2: width - margin.right, y1: yOf(value), y2: yOf(value), stroke: "var(--grid)", "stroke-width": 1 }));
    const label = svg("text", { x: margin.left - 6, y: yOf(value) + 3.5, "text-anchor": "end", class: "axis-text" });
    label.textContent = fmtNum(value, tickDigits);
    label.classList.add("axis-text");
    root.append(label);
  }
  root.append(svg("line", { x1: margin.left, x2: width - margin.right, y1: margin.top + ph, y2: margin.top + ph, stroke: "var(--baseline)", "stroke-width": 1 }));

  const nXTicks = Math.max(2, Math.min(6, Math.floor(pw / 110)));
  for (let i = 0; i <= nXTicks; i++) {
    const ts = xFrom + (spanS * i) / nXTicks;
    const label = svg("text", { x: xOf(ts), y: height - 6, "text-anchor": i === 0 ? "start" : i === nXTicks ? "end" : "middle", class: "axis-text" });
    label.textContent = timeTickFormat(ts, spanS);
    root.append(label);
  }

  for (const dataset of series) {
    if (!dataset.points.length) continue;
    const pathData = dataset.points.map((point, idx) =>
      `${idx ? "L" : "M"}${xOf(point[0]).toFixed(1)},${yOf(point[1]).toFixed(1)}`).join("");
    if (opts.area && series.length === 1) {
      const areaPath = pathData + `L${xOf(dataset.points[dataset.points.length - 1][0]).toFixed(1)},${yOf(yt.min).toFixed(1)}` +
        `L${xOf(dataset.points[0][0]).toFixed(1)},${yOf(yt.min).toFixed(1)}Z`;
      root.append(svg("path", { d: areaPath, fill: dataset.color, "fill-opacity": 0.1, stroke: "none" }));
    }
    root.append(svg("path", { d: pathData, fill: "none", stroke: dataset.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  }

  // Reference/threshold lines sit above the data, below the crosshair: a thin
  // warning-toned solid line with a right-anchored label.
  for (const refLine of refLines) {
    const y = yOf(refLine.value);
    if (y < margin.top || y > margin.top + ph) continue;
    root.append(svg("line", {
      x1: margin.left, x2: width - margin.right, y1: y, y2: y,
      stroke: "var(--status-warning)", "stroke-width": 1, "stroke-opacity": 0.7,
    }));
    const refLabel = svg("text", { x: width - margin.right, y: y - 4, "text-anchor": "end", class: "axis-text" });
    refLabel.textContent = refLine.label;
    refLabel.setAttribute("fill", "var(--status-warning)");
    root.append(refLabel);
  }

  const cross = svg("line", { x1: 0, x2: 0, y1: margin.top, y2: margin.top + ph, stroke: "var(--baseline)", "stroke-width": 1, visibility: "hidden" });
  root.append(cross);
  const dots = series.map((dataset) => {
    const dot = svg("circle", { r: 4, fill: dataset.color, stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden" });
    root.append(dot);
    return dot;
  });

  box.append(root);

  const tip = el("div", { class: "tooltip" });
  box.append(tip);

  const tsUnion = [...new Set(allPts.map((point) => point[0]))].sort((tsA, tsB) => tsA - tsB);
  const hit = svg("rect", { x: margin.left, y: margin.top, width: pw, height: ph, fill: "transparent" });
  root.append(hit);

  function nearest(arr, target) {
    let lo = 0, hi = arr.length - 1;
    while (hi - lo > 1) { const mid = (hi + lo) >> 1; (arr[mid] < target ? (lo = mid) : (hi = mid)); }
    return target - arr[lo] < arr[hi] - target ? arr[lo] : arr[hi];
  }

  function onMove(ev) {
    const rect = root.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * width;
    const ts = nearest(tsUnion, xFrom + ((px - margin.left) / pw) * spanS);
    const xPos = xOf(ts);
    cross.setAttribute("x1", xPos); cross.setAttribute("x2", xPos);
    cross.setAttribute("visibility", "visible");
    tip.textContent = "";
    const timeRow = el("div", { class: "tt-time" }, fmtDT(ts));
    tip.append(timeRow);
    series.forEach((dataset, idx) => {
      let best = null;
      for (const point of dataset.points) {
        if (best === null || Math.abs(point[0] - ts) < Math.abs(best[0] - ts)) best = point;
      }
      if (!best || Math.abs(best[0] - ts) > spanS / 8) { dots[idx].setAttribute("visibility", "hidden"); return; }
      dots[idx].setAttribute("cx", xOf(best[0])); dots[idx].setAttribute("cy", yOf(best[1]));
      dots[idx].setAttribute("visibility", "visible");
      const row = el("div", { class: "tt-row" });
      const swatch = el("span", { class: "swatch" }); swatch.style.background = dataset.color;
      row.append(swatch, el("span", { class: "tt-name" }, dataset.name),
        el("span", { class: "tt-val" }, `${fmtNum(best[1], opts.digits ?? 1)}${opts.unit ? " " + opts.unit : ""}`));
      tip.append(row);
    });
    tip.style.display = "block";
    const boxWidth = box.clientWidth;
    const leftPct = (xPos / width) * boxWidth;
    tip.style.left = `${Math.min(boxWidth - 150, Math.max(0, leftPct + 12))}px`;
    tip.style.top = "8px";
  }
  hit.addEventListener("pointermove", onMove);
  hit.addEventListener("pointerleave", () => {
    tip.style.display = "none";
    cross.setAttribute("visibility", "hidden");
    dots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
  });

  if (series.length >= 2) {
    const legend = el("div", { class: "legend" });
    for (const dataset of series) {
      const swatch = el("span", { class: "swatch" }); swatch.style.background = dataset.color;
      legend.append(el("span", { class: "lkey" }, swatch, dataset.name));
    }
    box.append(legend);
  }
}

/* barChart(box, {bars: [{label, value}], unit, digits, height}) — columns with
   rounded caps, square baseline, per-bar hover tooltip. */
function barChart(box, opts) {
  box.style.minHeight = `${opts.height || 220}px`;
  box.textContent = "";
  box.classList.add("chart-box");
  const bars = opts.bars || [];
  if (!bars.length) {
    box.append(el("div", { class: "empty" }, "No data in this range yet — the chart fills in as history accumulates"));
    return;
  }
  const height = opts.height || 220;
  const width = Math.max(320, box.clientWidth || 800);
  const margin = { left: 48, right: 14, top: 12, bottom: 24 };
  const pw = width - margin.left - margin.right, ph = height - margin.top - margin.bottom;
  const yt = niceTicks(0, Math.max(...bars.map((bar) => bar.value), 0.001), 4);
  const yOf = (value) => margin.top + ph - (value / (yt.max || 1)) * ph;
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height });

  const yStep = yt.ticks.length > 1 ? yt.ticks[1] - yt.ticks[0] : 1;
  const tickDigits = Math.max(0, Math.min(3, -Math.floor(Math.log10(yStep) + 1e-9)));
  for (const value of yt.ticks) {
    if (value < 0) continue;
    root.append(svg("line", { x1: margin.left, x2: width - margin.right, y1: yOf(value), y2: yOf(value), stroke: "var(--grid)", "stroke-width": 1 }));
    const lbl = svg("text", { x: margin.left - 6, y: yOf(value) + 3.5, "text-anchor": "end", class: "axis-text" });
    lbl.textContent = fmtNum(value, tickDigits);
    root.append(lbl);
  }
  root.append(svg("line", { x1: margin.left, x2: width - margin.right, y1: margin.top + ph, y2: margin.top + ph, stroke: "var(--baseline)", "stroke-width": 1 }));

  const slot = pw / bars.length;
  const barWidth = Math.min(24, Math.max(3, slot - 2));
  const color = COLORS().s1;
  const tip = el("div", { class: "tooltip" });
  const baseY = margin.top + ph;
  const labelEvery = Math.max(1, Math.ceil(bars.length / Math.floor(pw / 60)));

  bars.forEach((bar, idx) => {
    const xPos = margin.left + slot * idx + (slot - barWidth) / 2;
    const topY = yOf(bar.value);
    const barHeight = Math.max(0, baseY - topY);
    const cornerRadius = Math.min(4, barWidth / 2, barHeight);
    const pathData = barHeight <= 0
      ? ""
      : `M${xPos},${baseY} L${xPos},${topY + cornerRadius} Q${xPos},${topY} ${xPos + cornerRadius},${topY} L${xPos + barWidth - cornerRadius},${topY} ` +
        `Q${xPos + barWidth},${topY} ${xPos + barWidth},${topY + cornerRadius} L${xPos + barWidth},${baseY} Z`;
    const rect = pathData ? svg("path", { d: pathData, fill: color }) : null;
    if (rect) root.append(rect);
    if (idx % labelEvery === 0) {
      const xLabel = svg("text", { x: xPos + barWidth / 2, y: height - 6, "text-anchor": "middle", class: "axis-text" });
      xLabel.textContent = bar.label;
      root.append(xLabel);
    }
    // Hit target spans the full slot height, wider than the mark.
    const hit = svg("rect", { x: margin.left + slot * idx, y: margin.top, width: slot, height: ph, fill: "transparent" });
    hit.addEventListener("pointermove", () => {
      if (rect) rect.setAttribute("fill-opacity", "0.8");
      tip.textContent = "";
      tip.append(el("div", { class: "tt-time" }, bar.label));
      const row = el("div", { class: "tt-row" });
      const swatch = el("span", { class: "swatch" }); swatch.style.background = color;
      row.append(swatch, el("span", { class: "tt-name" }, opts.seriesName || ""),
        el("span", { class: "tt-val" }, `${fmtNum(bar.value, opts.digits ?? 1)}${opts.unit ? " " + opts.unit : ""}`));
      tip.append(row);
      tip.style.display = "block";
      const boxWidth = box.clientWidth;
      tip.style.left = `${Math.min(boxWidth - 150, Math.max(0, ((xPos + barWidth / 2) / width) * boxWidth + 10))}px`;
      tip.style.top = "8px";
    });
    hit.addEventListener("pointerleave", () => {
      if (rect) rect.removeAttribute("fill-opacity");
      tip.style.display = "none";
    });
    root.append(hit);
  });

  box.append(root, tip);
}

/* timeBrush(box, {samples, range, win, onChange}) — an overview strip of the
   session's power with a draggable selection window. Drag the handles to
   resize, drag the middle to pan, drag on empty track to select fresh.
   onChange(win|null) fires as the user drags; null = full range. */
function timeBrush(box, opts) {
  const HEIGHT = 64;
  box.textContent = "";
  box.classList.add("chart-box");
  box.style.minHeight = `${HEIGHT}px`;
  const width = Math.max(320, box.clientWidth || 800);
  const margin = { left: 48, right: 14 };
  const pw = width - margin.left - margin.right;
  const [tFrom, tTo] = opts.range;
  const span = Math.max(1, tTo - tFrom);
  const xOf = (ts) => margin.left + ((ts - tFrom) / span) * pw;
  const tsOf = (xPos) => tFrom + ((xPos - margin.left) / pw) * span;
  const clampT = (ts) => Math.min(tTo, Math.max(tFrom, ts));
  const MIN_WIN = Math.min(15, span / 4);
  const color = COLORS().s1;

  const root = svg("svg", { viewBox: `0 0 ${width} ${HEIGHT}`, width, height: HEIGHT, style: "touch-action:none" });
  root.append(svg("rect", { x: margin.left, y: 4, width: pw, height: HEIGHT - 8, fill: "transparent", stroke: "var(--grid)", "stroke-width": 1 }));

  // overview: total power as a compact area
  const pts = (opts.samples || []).map((sample) => [sample.ts, sample.power])
    .filter((point) => point[1] != null && isFinite(point[1]));
  if (pts.length > 1) {
    const vMax = Math.max(...pts.map((point) => point[1]), 1);
    const yOf = (value) => (HEIGHT - 10) - (Math.max(0, value) / vMax) * (HEIGHT - 18);
    const pathData = pts.map((point, idx) =>
      `${idx ? "L" : "M"}${xOf(point[0]).toFixed(1)},${yOf(point[1]).toFixed(1)}`).join("");
    root.append(svg("path", {
      d: pathData + `L${xOf(pts[pts.length - 1][0]).toFixed(1)},${HEIGHT - 10}L${xOf(pts[0][0]).toFixed(1)},${HEIGHT - 10}Z`,
      fill: color, "fill-opacity": 0.12, stroke: "none",
    }));
    root.append(svg("path", { d: pathData, fill: "none", stroke: color, "stroke-width": 1.5, "stroke-linejoin": "round" }));
  }

  // selection window
  const winRect = svg("rect", { y: 4, height: HEIGHT - 8, fill: color, "fill-opacity": 0.14, stroke: color, "stroke-width": 1.5, rx: 3 });
  const handleLeft = svg("rect", { y: HEIGHT / 2 - 12, width: 7, height: 24, rx: 3, fill: color, style: "cursor:ew-resize" });
  const handleRight = svg("rect", { y: HEIGHT / 2 - 12, width: 7, height: 24, rx: 3, fill: color, style: "cursor:ew-resize" });
  root.append(winRect, handleLeft, handleRight);

  let win = opts.win ? [...opts.win] : null;
  function draw() {
    const [winStart, winEnd] = win || [tFrom, tTo];
    const xStart = xOf(winStart), xEnd = xOf(winEnd);
    winRect.setAttribute("x", xStart);
    winRect.setAttribute("width", Math.max(1, xEnd - xStart));
    winRect.setAttribute("stroke-opacity", win ? 1 : 0.35);
    winRect.setAttribute("fill-opacity", win ? 0.14 : 0.04);
    winRect.style.cursor = win ? "grab" : "crosshair";
    handleLeft.setAttribute("x", xStart - 3.5);
    handleRight.setAttribute("x", xEnd - 3.5);
    handleLeft.style.display = handleRight.style.display = win ? "" : "none";
  }
  draw();

  let mode = null, grabT = 0, winAtGrab = null;
  function pxT(ev) {
    const rect = root.getBoundingClientRect();
    return tsOf(((ev.clientX - rect.left) / rect.width) * width);
  }
  root.addEventListener("pointerdown", (ev) => {
    const ts = pxT(ev);
    root.setPointerCapture(ev.pointerId);
    if (win && ev.target === handleLeft) mode = "l";
    else if (win && ev.target === handleRight) mode = "r";
    else if (win && ts > win[0] && ts < win[1]) { mode = "move"; grabT = ts; winAtGrab = [...win]; }
    else { mode = "new"; grabT = clampT(ts); win = [grabT, grabT]; }
    ev.preventDefault();
  });
  root.addEventListener("pointermove", (ev) => {
    if (!mode) return;
    const ts = clampT(pxT(ev));
    if (mode === "l") win[0] = Math.min(ts, win[1] - MIN_WIN);
    else if (mode === "r") win[1] = Math.max(ts, win[0] + MIN_WIN);
    else if (mode === "move") {
      const dt = ts - grabT;
      const winSpan = winAtGrab[1] - winAtGrab[0];
      let newStart = winAtGrab[0] + dt;
      newStart = Math.min(Math.max(newStart, tFrom), tTo - winSpan);
      win = [newStart, newStart + winSpan];
    } else if (mode === "new") {
      win = grabT < ts ? [grabT, ts] : [ts, grabT];
    }
    draw();
  });
  root.addEventListener("pointerup", () => {
    if (!mode) return;
    if (mode === "new" && win && win[1] - win[0] < MIN_WIN) win = null; // a click clears
    mode = null;
    draw();
    opts.onChange(win ? [...win] : null);
  });

  box.append(root);
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
  for (const preset of presets) {
    row.append(el("button", { class: "chip" + (preset.key === activeKey ? " active" : ""), onclick: () => onPick(preset.key) }, preset.label));
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
  const dot = $("#conn-dot"), textEl = $("#conn-text");
  dot.className = "dot " + (ok ? "ok" : "bad");
  textEl.textContent = text;
}

async function refreshStatus() {
  try {
    const st = await getJSON("/api/status");
    live.status = st;
    const poller = st.poller || {};
    const stale = st.vitals ? (st.server_ts - st.vitals.ts) : null;
    if (poller.offline) setConnDot(false, `charger unreachable (${poller.last_poll_error || "no response"})`);
    else if (st.vitals) setConnDot(true, `charger online — last sample ${stale < 2 ? "now" : fmtDur(stale) + " ago"}`);
    else setConnDot(false, "no data yet");
    renderBanner(st.active_alerts || []);
    const counts = st.counts || {};
    $("#foot-info").textContent =
      `${fmtNum(counts.vitals_samples, 0)} vitals samples · ${fmtNum(counts.sessions, 0)} sessions · ` +
      `${fmtNum(counts.events, 0)} events recorded` + (poller.host ? ` · watching ${poller.host}` : "");
  } catch {
    setConnDot(false, "monitor unreachable");
  }
}

function renderBanner(alerts) {
  const holder = $("#alert-banner");
  holder.textContent = "";
  for (const alertItem of alerts) {
    const sev = alertItem.source === "device" ? "critical" : alertItem.source === "wifi" ? "serious" : "critical";
    const disp = alertDisplay(alertItem.alert, alertItem.source);
    const label = el("span", {}, disp.label);
    if (disp.sub) label.title = disp.sub;
    holder.append(el("div", { class: "banner " + sev },
      el("span", { class: "icon" }, "⚠"),
      label,
      el("span", { class: "when" }, `active since ${fmtDT(alertItem.first_ts)}`)));
  }
}

/* ---------------- views ---------------- */

let cleanup = null;

async function viewLive(root) {
  const colors = COLORS();
  const tiles = el("div", { class: "cards" });
  const sessionCard = el("div", {});
  const thermalCard = el("div", {});
  const power = chartCard("Power", "Total power drawn by the vehicle — live");
  const currents = chartCard("Phase currents", "Per-phase current at the charger — live");
  root.append(tiles, sessionCard, thermalCard, power.card, currents.card);

  // Seed the rolling buffer with the last 15 minutes from the DB.
  const now = Date.now() / 1000;
  let buf = [];
  try {
    const hist = await getJSON(`/api/vitals?from=${now - 900}&to=${now}&points=900`);
    buf = hist.samples.map(fromDbRow);
  } catch { /* fresh DB */ }

  function renderTiles(sample) {
    tiles.textContent = "";
    const st = live.status || {};
    tiles.append(
      statTile("Power", fmtNum((sample.power ?? 0) / 1000, 2), "kW", sample.charging ? "charging" : sample.connected ? "connected, not charging" : "idle"),
      statTile("Vehicle current", fmtNum(sample.iv ?? (sample.ia != null ? Math.max(sample.ia, sample.ib ?? 0, sample.ic ?? 0) : null), 1), "A"),
      statTile("Grid", `${fmtNum(sample.gridV, 1)}`, "V", `${fmtNum(sample.gridHz, 3)} Hz`),
      statTile("Session energy", fmtNum((sample.energy ?? 0) / 1000, 2), "kWh", sample.sessionId ? `session #${sample.sessionId}` : "no session"),
      statTile("Plug handle temp", fmtNum(sample.tHandle, 1), "°C",
        `circuit board ${fmtNum(sample.tPcba, 1)} °C · processor ${fmtNum(sample.tMcu, 1)} °C`),
      statTile("EVSE state", "", null, ""),
    );
    const evseTile = tiles.lastChild;
    evseTile.querySelector(".tile-value").textContent = evseLabel(sample.evse);
    evseTile.querySelector(".tile-value").style.fontSize = "16px";
    let notReady = sample.notReady;
    if (notReady == null && st.vitals && st.vitals.raw) {
      try { notReady = JSON.parse(st.vitals.raw).evse_not_ready_reasons || []; } catch { notReady = []; }
    }
    // This firmware reports evse_not_ready_reasons [1] persistently — on
    // every connected sample, including while actively charging — so the
    // codes only carry meaning when no power is flowing. Suppress them during
    // charging rather than caption "Charging" with "not-ready reasons".
    const showNotReady = !sample.charging && notReady && notReady.length;
    evseTile.querySelector(".tile-sub").textContent =
      (showNotReady ? `not-ready reason codes: ${notReady.join(", ")} — ` : "") +
      (EVSE_VERIFIED.has(sample.evse)
        ? "label verified from this charger's own telemetry; (n) is the charger's raw value"
        : "label is community-reported, unverified — Tesla doesn't document these codes; (n) is the charger's raw value");
    if (st.version) {
      tiles.append(statTile("Firmware", "", null, ""));
      const tile = tiles.lastChild;
      tile.querySelector(".tile-value").textContent = st.version.firmware_version || "—";
      tile.querySelector(".tile-value").style.fontSize = "15px";
      tile.querySelector(".tile-sub").textContent = `S/N ${st.version.serial_number || "?"}`;
    }
  }

  function renderSessionCard(sample) {
    sessionCard.textContent = "";
    if (!sample.sessionId) return;
    const started = buf.find((entry) => entry.sessionId === sample.sessionId);
    const startTs = (live.status && live.status.active_session && live.status.active_session.start_ts) || (started && started.ts);
    sessionCard.append(el("div", { class: "chart-card" },
      el("div", { class: "chart-title" }, `Live charging session #${sample.sessionId}`),
      el("dl", { class: "kv" },
        el("dt", {}, "Plugged in"), el("dd", {}, startTs ? `${fmtDT(startTs)} (${fmtDur(sample.ts - startTs)} ago)` : "—"),
        el("dt", {}, "Energy this session"), el("dd", {}, `${fmtNum((sample.energy ?? 0) / 1000, 2)} kWh`),
        el("dt", {}, "State"), el("dd", {}, evseLabel(sample.evse)),
        el("dt", {}, "Device alerts"), el("dd", {},
          (sample.alerts && sample.alerts.length) ? sample.alerts.map((alertId) => alertDisplay(alertId, "device").label).join(", ") : "none")),
      el("div", { class: "note" }, "Full history for this session appears under Sessions once it ends — or open it live: "),
      el("a", { href: `#/sessions/${sample.sessionId}` }, "open session detail")));
  }

  function renderCharts() {
    const xTo = Date.now() / 1000;
    const xFrom = xTo - 900;
    const pts = buf.filter((sample) => sample.ts >= xFrom);
    lineChart(power.box, {
      series: [{ name: "Power (W)", color: colors.s1, points: pts.map((sample) => [sample.ts, sample.power]) }],
      unit: "W", digits: 0, area: true, zeroBase: true, xFrom, xTo, height: 230,
    });
    lineChart(currents.box, {
      series: [
        { name: "Phase A", color: colors.s1, points: pts.map((sample) => [sample.ts, sample.ia]) },
        { name: "Phase B", color: colors.s2, points: pts.map((sample) => [sample.ts, sample.ib]) },
        { name: "Phase C", color: colors.s3, points: pts.map((sample) => [sample.ts, sample.ic]) },
      ],
      unit: "A", digits: 1, zeroBase: true, xFrom, xTo, height: 200,
    });
  }

  const cToF = (celsius) => (celsius * 9) / 5 + 32;

  function renderThermal(data) {
    thermalCard.textContent = "";
    const forecast = data.forecast, model = data.model || {};
    let chip = null;
    const lines = [];
    if (data.state === "charging" && forecast && forecast.will_trip != null) {
      if (forecast.will_trip && forecast.minutes_to_trip <= 0.5) {
        chip = chipFor("critical", "at the derate threshold now");
      } else if (forecast.will_trip) {
        chip = chipFor("warning", `derate expected ≈ ${fmtT(forecast.trip_ts).slice(0, 5)} (in ~${fmtNum(forecast.minutes_to_trip, 0)} min)`);
        lines.push(`Handle is at ${fmtNum(data.handle_c, 1)} °C and heading to ~${fmtNum(forecast.steady_state_c, 1)} °C — ` +
          `alert 40 raises at ${fmtNum(model.trip_c, 0)} °C and halves the charge current for the rest of the session.`);
        if (forecast.suggested_max_a) {
          lines.push(`Capping the vehicle's charge current at ~${fmtNum(forecast.suggested_max_a, 0)} A now would stay under the limit — ` +
            `a faster charge overall than riding it into the 50% foldback.`);
        }
      } else {
        chip = chipFor("good", "no derate expected");
        lines.push(`Handle is at ${fmtNum(data.handle_c, 1)} °C, settling near ~${fmtNum(forecast.steady_state_c, 1)} °C — ` +
          `below the ${fmtNum(model.trip_c, 0)} °C alert-40 threshold.`);
      }
      lines.push(forecast.basis === "trajectory"
        ? "Based on the handle's temperature trajectory over the last few minutes."
        : forecast.ambient_source === "recent_trajectory"
          ? "Based on ambient inferred from the last steady stretch of charging — switches to the live trajectory once the current holds steady for a couple of minutes."
          : "Based on pre-session ambient and charge current — refines as the session warms up.");
    } else if (data.state === "charging") {
      lines.push(forecast && forecast.reason === "current_changed"
        ? "Charge current changed a moment ago — the forecast rebuilds after a couple of minutes at the new rate."
        : "Charging just started — the forecast needs a few minutes of steady data.");
    } else if (data.state === "idle" && forecast) {
      const amb = `${fmtNum(data.ambient_c, 1)} °C (${fmtNum(cToF(data.ambient_c), 0)} °F)`;
      if (forecast.will_trip) {
        chip = chipFor("warning", "hot enough to derate");
        lines.push(`Ambient at the charger ≈ ${amb}. A full-rate (${fmtNum(model.ref_current_a, 0)} A) charge started now ` +
          `would hit the ${fmtNum(model.trip_c, 0)} °C handle limit in ~${fmtNum(forecast.minutes_to_trip, 0)} min and drop to half current.`);
        if (forecast.suggested_max_a) {
          lines.push(`Setting the vehicle to ~${fmtNum(forecast.suggested_max_a, 0)} A before plugging in would avoid the derate ` +
            `and beat a full-rate start that folds back to ${fmtNum(model.ref_current_a / 2, 0)} A.`);
        }
      } else {
        chip = chipFor("good", "full-rate charging safe");
        lines.push(`Ambient at the charger ≈ ${amb}. A full-rate (${fmtNum(model.ref_current_a, 0)} A) charge would settle ` +
          `near ~${fmtNum(forecast.steady_state_c, 1)} °C, below the ${fmtNum(model.trip_c, 0)} °C limit — derates start above ` +
          `~${fmtNum(forecast.safe_ambient_max_c, 0)} °C (${fmtNum(cToF(forecast.safe_ambient_max_c), 0)} °F) ambient.`);
      }
      if (data.ambient_stable === false) lines.push("Handle is still cooling from recent charging, so the ambient estimate reads high.");
    } else if (data.state === "connected") {
      lines.push("Vehicle connected but not charging — the forecast resumes when current flows.");
    } else {
      lines.push("No recent samples to forecast from.");
    }
    // Degradation watch: same fits, watched over time. Rising heat at the
    // same current means added resistance somewhere in the current path.
    const drift = data.drift;
    let driftLine = null;
    if (drift && drift.drifting) {
      driftLine = el("div", { class: "note" },
        chipFor("serious", "heat rise increasing"),
        ` Recent sessions average +${fmtNum(drift.recent_rise_c, 1)} °C at ${fmtNum(model.ref_current_a, 0)} A vs a ` +
        `+${fmtNum(drift.baseline_rise_c, 1)} °C baseline (Δ ${fmtNum(drift.delta_c, 1)} °C). More heat at the same current ` +
        `means added resistance — inspect the handle and charge-port pins, and have the terminal torque checked.` +
        (drift.off_current_n ? ` (${drift.off_current_n} session${drift.off_current_n === 1 ? "" : "s"} away from the usual ` +
        `~${fmtNum(drift.typical_current_a, 0)} A excluded from the comparison.)` : ""));
    }
    const modelNote = `Model: τ ≈ ${fmtNum(model.tau_min, 1)} min, +${fmtNum(model.rise_ref_c, 0)} °C at ${fmtNum(model.ref_current_a, 0)} A — ` +
      (model.fitted ? `fitted from ${model.tau_fits} recorded session ramp${model.tau_fits === 1 ? "" : "s"}.`
                : "defaults from the verified alert-40 event; refits automatically as sessions accumulate.") +
      (drift && !drift.drifting ? ` Heat rise stable across the last ${drift.recent_n + drift.baseline_n} fitted sessions` +
        `${drift.off_current_n ? ` (${drift.off_current_n} off-current session${drift.off_current_n === 1 ? "" : "s"} excluded)` : ""}.` : "");
    thermalCard.append(el("div", { class: "chart-card" },
      el("div", { class: "chart-title" }, "Thermal derate forecast", chip ? " " : null, chip),
      ...lines.map((text) => el("div", { class: "note" }, text)),
      driftLine,
      el("div", { class: "chart-sub" }, modelNote)));
  }

  async function loadThermal() {
    try { renderThermal(await getJSON("/api/thermal")); } catch { /* keep last card */ }
  }
  loadThermal();
  const thermalTimer = setInterval(loadThermal, 60000);

  if (buf.length) { renderTiles(buf[buf.length - 1]); renderSessionCard(buf[buf.length - 1]); }
  renderCharts();

  let lastCharging = null;
  const onMsg = (msg) => {
    if (msg.type !== "vitals") return;
    const sample = fromSse(msg);
    buf.push(sample);
    const cutoff = Date.now() / 1000 - 960;
    while (buf.length && buf[0].ts < cutoff) buf.shift();
    renderTiles(sample);
    renderSessionCard(sample);
    renderCharts();
    // Refresh the forecast immediately when charging starts or stops rather
    // than waiting out the poll interval.
    if (lastCharging !== null && sample.charging !== lastCharging) loadThermal();
    lastCharging = sample.charging;
  };
  live.listeners.add(onMsg);
  return () => { live.listeners.delete(onMsg); clearInterval(thermalTimer); };
}

const RANGE_PRESETS = [
  { key: "1h", label: "Last hour", seconds: 3600 },
  { key: "6h", label: "6 hours", seconds: 6 * 3600 },
  { key: "24h", label: "24 hours", seconds: 24 * 3600 },
  { key: "7d", label: "7 days", seconds: 7 * 24 * 3600 },
  { key: "30d", label: "30 days", seconds: 30 * 24 * 3600 },
  { key: "90d", label: "90 days", seconds: 90 * 24 * 3600 },
];
const rangeSeconds = (key) => (RANGE_PRESETS.find((preset) => preset.key === key) || RANGE_PRESETS[3]).seconds;

async function viewSessions(root, rangeKey = "30d") {
  const now = Date.now() / 1000;
  const from = now - rangeSeconds(rangeKey);
  root.append(el("h2", {}, "Charging sessions"));
  root.append(presetRow(RANGE_PRESETS.slice(2), rangeKey, (key) => { render("sessions", key); }));

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
    for (const session of data.sessions) {
      const ongoing = session.end_ts == null;
      const row = el("tr", { class: "clickable", onclick: () => { location.hash = `#/sessions/${session.id}`; } },
        el("td", {}, `#${session.id}`),
        el("td", {}, fmtDT(session.start_ts)),
        el("td", {}, ongoing ? "ongoing" : fmtDT(session.end_ts)),
        el("td", { class: "num" }, fmtDur((ongoing ? now : session.end_ts) - session.start_ts)),
        el("td", { class: "num" }, fmtDur(session.charging_s)),
        el("td", { class: "num" }, fmtNum((session.energy_wh ?? 0) / 1000, 2)),
        el("td", { class: "num" }, fmtNum((session.max_power_w ?? 0) / 1000, 2)),
        el("td", { class: "num" }, fmtNum(session.sample_count, 0)),
        el("td", {}, chipFor(ongoing ? "good" : "muted", ongoing ? "live" : (session.end_reason || "ended").replaceAll("_", " "))));
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

async function viewEnergy(root, rangeKey = "30d") {
  const now = Date.now() / 1000;
  const from = now - rangeSeconds(rangeKey);
  root.append(el("h2", {}, "Charger lifetime"));

  const st = live.status || await getJSON("/api/status");
  const lifetime = st.lifetime;
  const tiles = el("div", { class: "cards" });
  if (lifetime) {
    tiles.append(
      statTile("Energy delivered", fmtNum(lifetime.energy_wh / 1e6, 2), "MWh", "over the charger's whole life"),
      statTile("Charge sessions", fmtNum(lifetime.charge_starts, 0), null, `${fmtNum(lifetime.connector_cycles, 0)} plug-in cycles`),
      statTile("Time charging", fmtNum(lifetime.charging_time_s / 3600, 0), "h", `uptime ${fmtNum(lifetime.uptime_s / 86400, 0)} days`),
      statTile("Thermal foldbacks", fmtNum(lifetime.thermal_foldbacks, 0), null, "times charging was slowed by heat"),
      statTile("Lifetime alerts", fmtNum(lifetime.alert_count, 0), null),
      statTile("Contactor cycles", fmtNum(lifetime.contactor_cycles, 0), null, `${fmtNum(lifetime.contactor_cycles_loaded, 0)} under load`),
    );
  } else {
    tiles.append(el("div", { class: "card" }, el("div", { class: "empty" }, "No lifetime data recorded yet.")));
  }
  root.append(tiles);

  root.append(el("h2", {}, "Energy per day"));
  root.append(presetRow(RANGE_PRESETS.slice(3), rangeKey, (key) => render("energy", key)));
  const daily = chartCard("Daily energy delivered",
    "From the charger's cumulative counter, sampled every minute — days before monitoring began can't be reconstructed");
  root.append(daily.card);

  const data = await getJSON(`/api/lifetime?from=${from}&to=${now}`);
  const byDay = new Map();
  for (const sample of data.samples) {
    if (sample.energy_wh == null) continue;
    const key = fmtDT(sample.ts).slice(0, 10);
    const cur = byDay.get(key);
    if (!cur) byDay.set(key, { min: sample.energy_wh, max: sample.energy_wh });
    else { cur.min = Math.min(cur.min, sample.energy_wh); cur.max = Math.max(cur.max, sample.energy_wh); }
  }
  const bars = [...byDay.entries()].map(([day, range]) => ({ label: day.slice(5), value: (range.max - range.min) / 1000 }));
  barChart(daily.box, { bars, unit: "kWh", digits: 1, seriesName: "Energy", height: 240 });
  const total = bars.reduce((sum, bar) => sum + bar.value, 0);
  if (bars.length) {
    root.append(el("div", { class: "note" },
      `${fmtNum(total, 1)} kWh across ${bars.length} recorded day${bars.length > 1 ? "s" : ""} ` +
      `(avg ${fmtNum(total / bars.length, 1)} kWh/day)`));
  }
}

async function viewSessionDetail(root, id) {
  const colors = COLORS();
  // The skeleton is built once; live refreshes fetch first and then update
  // content in place, so the page never blanks or flashes mid-update.
  const heading = el("h2", {}, `Session #${id}`);
  const tiles = el("div", { class: "cards" });
  const power = chartCard("Power", "Total power over the session");
  const cur = chartCard("Phase currents", "Per-phase current");
  const volt = chartCard("Phase voltages", "Per-phase voltage");
  const temp = chartCard("Temperatures",
    `Plug handle, charger circuit board (PCBA), and processor (MCU). At ${HANDLE_TRIP_C}°C on the handle, alert 40 raises ` +
    `and current is halved (observed on this firmware); the ≈${PCBA_THROTTLE_C}°C line is the community-observed PCBA throttle point.`);
  const pilot = chartCard("Pilot & proximity", "J1772 handshake signals — flaky values here often precede charging errors");
  const relay = chartCard("Relay voltages", "Contactor coil drive");
  const eventsWrap = el("div", {});
  const brush = chartCard("Time window",
    "Drag across the strip to zoom every chart and the event list into that window — drag the edges to resize, the middle to pan, click once to reset");
  const brushInfo = el("div", { class: "note" });
  brush.card.append(brushInfo);
  root.append(
    heading,
    el("div", { class: "filters" }, el("a", { class: "chip", href: "#/sessions" }, "← all sessions")),
    tiles,
    brush.card,
    power.card, el("div", { class: "grid-2" }, cur.card, volt.card), temp.card,
    el("div", { class: "grid-2" }, pilot.card, relay.card),
    el("h2", {}, "Events during this session"),
    eventsWrap);

  let ongoing = false;
  // Time-window state: win = [t0, t1] or null (full session). Windowing is
  // pure view filtering — nothing is deleted — and a windowed view re-fetches
  // that range at high resolution for a deeper dive than the whole-session
  // downsample can show.
  let win = null;
  let fullSamples = [];
  let fullEvents = [];
  let sessionRange = [0, 1];
  let winSeq = 0;
  let winTimer = null;

  function renderCharts(samples, xFrom, xTo) {
    lineChart(power.box, {
      series: [{ name: "Power (W)", color: colors.s1, points: samples.map((sample) => [sample.ts, sample.power]) }],
      unit: "W", digits: 0, area: true, zeroBase: true, xFrom, xTo, height: 240,
    });
    lineChart(cur.box, {
      series: [
        { name: "Phase A", color: colors.s1, points: samples.map((sample) => [sample.ts, sample.ia]) },
        { name: "Phase B", color: colors.s2, points: samples.map((sample) => [sample.ts, sample.ib]) },
        { name: "Phase C", color: colors.s3, points: samples.map((sample) => [sample.ts, sample.ic]) },
      ], unit: "A", digits: 1, zeroBase: true, xFrom, xTo, height: 190,
    });
    lineChart(volt.box, {
      series: [
        { name: "Phase A", color: colors.s1, points: samples.map((sample) => [sample.ts, sample.va]) },
        { name: "Phase B", color: colors.s2, points: samples.map((sample) => [sample.ts, sample.vb]) },
        { name: "Phase C", color: colors.s3, points: samples.map((sample) => [sample.ts, sample.vc]) },
      ], unit: "V", digits: 1, xFrom, xTo, height: 190,
    });
    lineChart(temp.box, {
      series: [
        { name: "Circuit board (PCBA)", color: colors.s1, points: samples.map((sample) => [sample.ts, sample.tPcba]) },
        { name: "Plug handle", color: colors.s2, points: samples.map((sample) => [sample.ts, sample.tHandle]) },
        { name: "Processor (MCU)", color: colors.s3, points: samples.map((sample) => [sample.ts, sample.tMcu]) },
      ], unit: "°C", digits: 1, xFrom, xTo, height: 190,
      refLines: [
        { value: HANDLE_TRIP_C, label: `${HANDLE_TRIP_C}°C handle → alert 40 derate` },
        { value: PCBA_THROTTLE_C, label: `≈${PCBA_THROTTLE_C}°C PCBA throttle (approx.)` },
      ],
    });
    lineChart(pilot.box, {
      series: [
        { name: "Pilot high", color: colors.s1, points: samples.map((sample) => [sample.ts, sample.pilotHigh]) },
        { name: "Pilot low", color: colors.s2, points: samples.map((sample) => [sample.ts, sample.pilotLow]) },
        { name: "Proximity", color: colors.s3, points: samples.map((sample) => [sample.ts, sample.prox]) },
      ], unit: "V", digits: 1, xFrom, xTo, height: 190,
    });
    lineChart(relay.box, {
      series: [
        { name: "Relay K1", color: colors.s1, points: samples.map((sample) => [sample.ts, sample.relayK1]) },
        { name: "Relay K2", color: colors.s2, points: samples.map((sample) => [sample.ts, sample.relayK2]) },
      ], unit: "V", digits: 1, zeroBase: true, xFrom, xTo, height: 190,
    });
  }

  function renderEvents() {
    const evs = win ? fullEvents.filter((event) => event.ts >= win[0] && event.ts <= win[1]) : fullEvents;
    eventsWrap.replaceChildren(eventsTable(evs));
  }

  function updateBrushInfo() {
    brushInfo.textContent = win
      ? `Window: ${fmtDT(win[0])} → ${fmtT(win[1])} (${fmtDur(win[1] - win[0])}) — full-resolution data for this range`
      : "Showing the full session";
  }

  function renderBrush() {
    timeBrush(brush.box, { samples: fullSamples, range: sessionRange, win, onChange: onBrushChange });
    updateBrushInfo();
  }

  async function applyWindow() {
    const seq = ++winSeq;
    if (!win) {
      renderCharts(fullSamples, sessionRange[0], sessionRange[1]);
      renderEvents();
      return;
    }
    // Immediate feedback from data already in hand…
    renderCharts(fullSamples.filter((sample) => sample.ts >= win[0] && sample.ts <= win[1]), win[0], win[1]);
    renderEvents();
    // …then upgrade to full-resolution samples for the window.
    try {
      const data = await getJSON(`/api/vitals?from=${win[0]}&to=${win[1]}&points=1500`);
      if (seq !== winSeq || !win) return;
      renderCharts(data.samples.map(fromDbRow), win[0], win[1]);
    } catch { /* keep the client-filtered render */ }
  }

  function onBrushChange(newWin) {
    win = newWin;
    updateBrushInfo();
    clearTimeout(winTimer);
    winTimer = setTimeout(applyWindow, 200);
  }

  async function refresh() {
    const data = await getJSON(`/api/sessions/${id}`); // fetch completes before any DOM change
    const session = data.session;
    ongoing = session.end_ts == null;
    const now = Date.now() / 1000;
    const samples = data.samples.map(fromDbRow);

    heading.replaceChildren(`Session #${session.id} `, ongoing ? chipFor("good", "live") : chipFor("muted", "ended"));

    const energyKwh = (session.energy_wh ?? (samples.length ? samples[samples.length - 1].energy : 0) ?? 0) / 1000;
    const maxP = session.max_power_w ?? Math.max(0, ...samples.map((sample) => sample.power || 0));
    // The plug-in time can predate monitoring (derived from the charger's own
    // session timer); charts still start at the first observed sample.
    const firstSeen = samples.length ? samples[0].ts : session.start_ts;
    const backdated = firstSeen - session.start_ts > 120;
    tiles.replaceChildren(
      statTile("Plugged in", fmtDT(session.start_ts).slice(11), null,
        fmtDT(session.start_ts).slice(0, 10) + (backdated ? ` — from charger's timer; monitoring since ${fmtT(firstSeen)}` : "")),
      ongoing
        ? statTile("Status", "Plugged in", null, "session in progress")
        : statTile("Unplugged", fmtDT(session.end_ts).slice(11), null, fmtDT(session.end_ts).slice(0, 10)),
      statTile("Duration", fmtDur((ongoing ? now : session.end_ts) - session.start_ts), null,
        session.charging_s != null ? `charging ${fmtDur(session.charging_s)}` : "charging time totals when the session ends"),
      statTile("Energy", fmtNum(energyKwh, 2), "kWh"),
      statTile("Peak power", fmtNum(maxP / 1000, 2), "kW", session.avg_power_w != null ? `avg ${fmtNum(session.avg_power_w / 1000, 2)} kW` : null),
      statTile("Samples", fmtNum(session.sample_count ?? samples.length, 0), null, "full fidelity retained"));

    fullSamples = samples;
    fullEvents = data.events;
    sessionRange = [backdated ? firstSeen : session.start_ts, ongoing ? now : session.end_ts];
    renderBrush();
    await applyWindow();
  }

  await refresh();

  if (ongoing) {
    // Refresh in place at most every 5s while live; stop once the session ends.
    let timer = null;
    const throttled = (msg) => {
      if (msg.type !== "vitals" && msg.type !== "event") return;
      if (timer) return;
      timer = setTimeout(async () => {
        timer = null;
        try { await refresh(); } catch { /* transient fetch failure; next tick retries */ }
        if (!ongoing) { live.listeners.delete(throttled); }
      }, 5000);
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
        const detailObj = JSON.parse(ev.detail);
        if (ev.kind === "evse_state_change") detail = `${evseLabel(detailObj.from)} → ${evseLabel(detailObj.to)}`;
        else if (ev.kind === "derate_warning")
          detail = `~${fmtNum(detailObj.minutes_to_trip, 0)} min to 65 °C at ${fmtNum(detailObj.current_a, 0)} A` +
            (detailObj.suggested_max_a ? ` — cap vehicle charge current at ${fmtNum(detailObj.suggested_max_a, 0)} A to keep charging` : "");
        else if (ev.kind === "monitor_gap") detail = `no data since ${fmtDT(detailObj.offline_since)} (${fmtDur(detailObj.gap_s)})`;
        else if (ev.kind === "evse_not_ready_change")
          detail = `codes [${(detailObj.from || []).join(", ")}] → [${(detailObj.to || []).join(", ")}] (undocumented)`;
        else if ((ev.kind === "alert_raised" || ev.kind === "alert_cleared") && detailObj.alert != null) {
          const disp = alertDisplay(detailObj.alert, "device");
          detail = disp.sub ? `${disp.label} · ${disp.sub}` : disp.label;
        }
        else if (ev.kind === "session_start" && detailObj.backdated_s)
          detail = `session_id: ${detailObj.session_id} · start backdated ${fmtDur(detailObj.backdated_s)} from the charger's session timer`;
        else detail = Object.entries(detailObj).map(([key, value]) => `${key}: ${value}`).join(" · ");
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
  const colors = COLORS();
  const now = Date.now() / 1000;
  const from = now - rangeSeconds(rangeKey);
  root.append(el("h2", {}, "Wi-Fi health"));
  root.append(presetRow(RANGE_PRESETS.slice(0, 4), rangeKey, (key) => render("wifi", key)));

  const [data, evData] = await Promise.all([
    getJSON(`/api/wifi?from=${from}&to=${now}`),
    getJSON(`/api/events?from=${from}&to=${now}&kinds=wifi_disconnected,wifi_reconnected,internet_lost,internet_restored,poll_error,poll_recovered`),
  ]);
  const st = live.status || await getJSON("/api/status");
  const wifi = st.wifi;

  const tiles = el("div", { class: "cards" });
  if (wifi) {
    tiles.append(
      statTile("Charger Wi-Fi", "", null, ""),
      statTile("Internet (charger → Tesla)", "", null, ""),
      statTile("Signal", fmtNum(wifi.signal_strength, 0), "%", `RSSI ${fmtNum(wifi.rssi, 0)} dBm · SNR ${fmtNum(wifi.snr, 0)} dB`),
      statTile("Network", "", null, ""));
    const [wifiTile, netTile, , netInfo] = tiles.children;
    wifiTile.querySelector(".tile-value").replaceChildren(chipFor(wifi.connected ? "good" : "critical", wifi.connected ? "connected" : "disconnected"));
    wifiTile.querySelector(".tile-sub").textContent = `as of ${fmtDT(wifi.ts)}`;
    netTile.querySelector(".tile-value").replaceChildren(chipFor(wifi.internet ? "good" : "warning", wifi.internet ? "reachable" : "unreachable"));
    netTile.querySelector(".tile-sub").textContent = "the charger's own cloud link — the monitor never uses it";
    netInfo.querySelector(".tile-value").textContent = wifi.ssid || "—";
    netInfo.querySelector(".tile-value").style.fontSize = "16px";
    netInfo.querySelector(".tile-sub").textContent = `${wifi.infra_ip || "?"} · ${wifi.mac || "?"}`;
  } else {
    tiles.append(el("div", { class: "card" }, el("div", { class: "empty" }, "No Wi-Fi data recorded yet.")));
  }
  root.append(tiles);

  const pts = data.samples;
  const rssi = chartCard("RSSI", "Received signal strength, dBm — higher (less negative) is better");
  const snr = chartCard("Signal-to-noise ratio", "dB — above ~20 dB is healthy");
  const sig = chartCard("Signal strength", "Charger-reported quality, %");
  root.append(el("div", { class: "grid-3" }, rssi.card, snr.card, sig.card));
  lineChart(rssi.box, { series: [{ name: "RSSI", color: colors.s1, points: pts.map((sample) => [sample.ts, sample.rssi]) }], unit: "dBm", digits: 0, xFrom: from, xTo: now, height: 170 });
  lineChart(snr.box, { series: [{ name: "SNR", color: colors.s1, points: pts.map((sample) => [sample.ts, sample.snr]) }], unit: "dB", digits: 0, xFrom: from, xTo: now, height: 170 });
  lineChart(sig.box, { series: [{ name: "Signal", color: colors.s1, points: pts.map((sample) => [sample.ts, sample.signal_strength]) }], unit: "%", digits: 0, zeroBase: true, xFrom: from, xTo: now, height: 170 });

  root.append(el("h2", {}, "Connectivity events"));
  root.append(eventsTable(evData.events));
}

async function viewAlerts(root, rangeKey = "7d") {
  const colors = COLORS();
  const now = Date.now() / 1000;
  const from = now - rangeSeconds(rangeKey);
  root.append(el("h2", {}, "Alerts"));
  const [data, thermalData] = await Promise.all([
    getJSON(`/api/alerts?from=${from}&to=${now}`),
    getJSON("/api/thermal").catch(() => null),
    loadAlertCodes(),
  ]);

  const activeWrap = el("div", { class: "cards" });
  if (!data.active.length) {
    activeWrap.append(el("div", { class: "card" },
      el("div", { class: "tile-label" }, "Active alerts"),
      el("div", { class: "tile-value" }, chipFor("good", "none")),
      el("div", { class: "tile-sub" }, "the charger reports no active alerts")));
  } else {
    for (const alertItem of data.active) {
      const disp = alertDisplay(alertItem.alert, alertItem.source);
      // Verified descriptions can run to a paragraph; inline only the first
      // sentence (the official alert text) and keep the full description in
      // the hover tooltip, like the banner and history table. The card spans
      // the grid row so a lone active alert doesn't render as a tall column.
      const firstSentence = disp.sub ? disp.sub.split(". ")[0] : null;
      const card = el("div", { class: "card wide" },
        el("div", { class: "tile-label" }, `${alertItem.source} alert`),
        el("div", { class: "tile-value" }, chipFor(alertItem.source === "wifi" ? "serious" : "critical", disp.label)),
        el("div", { class: "tile-sub" },
          `since ${fmtDT(alertItem.first_ts)} (${fmtDur(now - alertItem.first_ts)})` + (firstSentence ? ` · ${firstSentence}` : "")));
      if (disp.sub) card.title = disp.sub;
      activeWrap.append(card);
    }
  }
  root.append(activeWrap);

  // Degradation watch: fitted heat rise per charging segment at reference
  // current. Prediction uses the rolling median, which would silently follow
  // a slow increase; this trend is where a contact/wiring problem shows.
  const fits = ((thermalData && thermalData.session_fits) || []).filter((fit) => fit.rise_ref_c != null);
  if (fits.length >= 2) {
    const drift = thermalData.drift;
    const rise = chartCard("Handle heat rise per charging segment",
      `Fitted steady-state rise above ambient, normalized to ${fmtNum(thermalData.model.ref_current_a, 0)} A. ` +
      "A sustained climb at the same current means added resistance in the current path — inspect before it becomes heat.");
    root.append(rise.card);
    lineChart(rise.box, {
      series: [{ name: "Rise (°C)", color: colors.s1, points: fits.map((fit) => [fit.start_ts, fit.rise_ref_c]) }],
      unit: "°C", digits: 1, height: 180,
    });
    if (drift) {
      // The verdict carries its own uncertainty — a delta from a handful of
      // fits is a lead, not a conviction, and the note must show which.
      const [ciLo, ciHi] = drift.delta_ci95_c || [null, null];
      const sureness = ciLo == null ? "" :
        ` · 95% CI ${fmtNum(ciLo, 1)}..${fmtNum(ciHi, 1)} °C from n=${drift.baseline_n}+${drift.recent_n}` +
        (drift.confident ? "" : " — not yet statistically confirmed; more sessions will tighten this");
      const pooled = drift.cross_current_n
        ? ` (${drift.cross_current_n} ambient-bracketed fit${drift.cross_current_n > 1 ? "s" : ""} pooled from other charge currents)`
        : "";
      rise.card.append(el("div", { class: "note" },
        (drift.drifting
          ? `Recent median +${fmtNum(drift.recent_rise_c, 1)} °C vs baseline +${fmtNum(drift.baseline_rise_c, 1)} °C ` +
            `(Δ ${fmtNum(drift.delta_c, 1)} °C ≥ ${fmtNum(drift.threshold_c, 1)} °C threshold) — a monitor alert is active`
          : `Stable: recent median +${fmtNum(drift.recent_rise_c, 1)} °C vs baseline +${fmtNum(drift.baseline_rise_c, 1)} °C ` +
            `(alert threshold Δ ≥ ${fmtNum(drift.threshold_c, 1)} °C)`) + sureness + pooled + "."));
    }
    // Ambient bracketing: fits that read ambient at both ends of the load
    // window are de-trended for weather that moved during the charge — the
    // difference between "the garage warmed 3 °C" and "the connector is
    // going bad". Start-only fits carry that ambiguity; say so.
    const bracketed = fits.filter((fit) => fit.ambient_drift_c != null);
    if (bracketed.length) {
      const maxDrift = bracketed.reduce(
        (acc, fit) => Math.abs(fit.ambient_drift_c) > Math.abs(acc) ? fit.ambient_drift_c : acc, 0);
      rise.card.append(el("div", { class: "note" },
        `Ambient bracketing: ${bracketed.length} of ${fits.length} fits read ambient at both ends of ` +
        `their load window and are corrected for in-window ambient drift ` +
        `(largest ${maxDrift > 0 ? "+" : ""}${fmtNum(maxDrift, 1)} °C); the rest assume it held still.`));
    }
    // Verified-baseline anchor: without one, "baseline" only means "the
    // first charges the monitor happened to see". After a hardware
    // inspection, anchoring here makes the comparison mean "vs verified
    // healthy" — the claim the alert text is actually making.
    const anchorTs = thermalData.baseline_anchor_ts;
    const anchorRow = el("div", { class: "filters" },
      el("span", { class: "flabel" }, "Baseline"),
      el("span", { class: "note" }, anchorTs
        ? `anchored at ${fmtDT(anchorTs)} — fits before it sit out the drift comparison. `
        : "all recorded fits (hardware never verified). After an inspection, anchor here so the " +
          "baseline means “verified healthy”. "));
    const anchorBtn = el("button", { class: "chip", onclick: async () => {
      const verb = anchorTs ? "Clear the verified-baseline anchor?" :
        "Anchor the baseline now? Only fits recorded from this moment on will form the drift baseline. " +
        "Do this right after the hardware has been inspected and verified (or fixed).";
      if (!window.confirm(verb)) return;
      await fetch("/api/thermal/baseline-anchor", { method: anchorTs ? "DELETE" : "POST",
        headers: { "Content-Type": "application/json" },
        body: anchorTs ? undefined : JSON.stringify({}) });
      render("alerts", rangeKey);
    } }, anchorTs ? "Clear anchor" : "Mark hardware verified — anchor baseline now");
    anchorRow.append(anchorBtn);
    rise.card.append(anchorRow);

    // Rise vs ambient: the confounder detector. Healthy hardware with a
    // complete thermal model shows a flat cloud — the fitted rise should not
    // care what the garage temperature was, because ambient is already
    // subtracted out. A cloud that still slopes upward with ambient means
    // the model is missing an environment term (heat-soaked cable/structure
    // in an uninsulated garage, not the weather at fit time); a flat cloud
    // sitting higher than the old fits at the same ambient is hardware.
    const scatter = chartCard("Heat rise vs ambient",
      "Each completed charge adds a point: fitted rise (48 A-normalized) against the ambient measured for " +
      "that load window. Flat cloud = model complete, ambient truly removed. Upward slope = residual " +
      "environment effect (e.g. multi-day heat soak) still masquerading as rise. Elevated-but-flat = " +
      "genuine added resistance. Hover a point for its session.");
    root.append(scatter.card);
    const fitPoint = (fit) => {
      const ambient = fit.ambient_end_c != null ? (fit.ambient_c + fit.ambient_end_c) / 2 : fit.ambient_c;
      return [ambient, fit.rise_ref_c,
        `session ${fit.session_id} · ${fmtDT(fit.start_ts)} · ${fmtNum(fit.current_a, 1)} A · ` +
        `rise ${fmtNum(fit.rise_ref_c, 1)} °C @ ambient ${fmtNum(ambient, 1)} °C`];
    };
    scatterChart(scatter.box, {
      xLabel: "window ambient, °C",
      series: [
        { name: "ambient-bracketed", color: colors.s1,
          points: fits.filter((fit) => fit.ambient_drift_c != null && fit.ambient_c != null).map(fitPoint) },
        { name: "start-only ambient", color: colors.s3,
          points: fits.filter((fit) => fit.ambient_drift_c == null && fit.ambient_c != null).map(fitPoint) },
      ],
      height: 210,
    });
  }

  root.append(el("h2", {}, "Alert history"));
  root.append(presetRow(RANGE_PRESETS.slice(2), rangeKey, (key) => render("alerts", key)));
  const wrap = el("div", { class: "tbl-wrap" });
  if (!data.history.length) {
    wrap.append(el("div", { class: "empty" }, "No alerts recorded in this range."));
  } else {
    const tbl = el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Alert"), el("th", {}, "Source"), el("th", {}, "First seen"),
        el("th", {}, "Cleared"), el("th", { class: "num" }, "Duration"), el("th", {}, "Status"))));
    const tbody = el("tbody", {});
    for (const alertItem of data.history) {
      const disp = alertDisplay(alertItem.alert, alertItem.source);
      const cell = el("td", {}, disp.label);
      if (disp.sub) cell.title = disp.sub;
      tbody.append(el("tr", {},
        cell, el("td", {}, alertItem.source),
        el("td", {}, fmtDT(alertItem.first_ts)),
        el("td", {}, alertItem.cleared_ts ? fmtDT(alertItem.cleared_ts) : "—"),
        el("td", { class: "num" }, fmtDur((alertItem.cleared_ts ?? now) - alertItem.first_ts)),
        el("td", {}, alertItem.active ? chipFor("critical", "active") : chipFor("good", "cleared"))));
    }
    tbl.append(tbody);
    wrap.append(tbl);
  }
  root.append(wrap);

  // Official fault categories (from Tesla's Gen 3 manual) as a reference —
  // the numeric API codes themselves are undocumented by Tesla, so an
  // unmapped code can be cross-read against the charger's LED blink pattern.
  const cats = (alertCodes && alertCodes.categories) || [];
  if (cats.length) {
    root.append(el("h2", {}, "Alert reference — official fault categories"),
      el("div", { class: "note" },
        "Tesla documents Wall Connector faults by LED blink pattern, not by the numeric codes the local API reports. " +
        "If an undocumented code appears here, check the charger's LED and the Tesla app (it names active alerts), " +
        "then teach the monitor by adding the code to alert_codes.json."));
    const rtbl = el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "LED"), el("th", {}, "Fault"), el("th", {}, "Meaning / action"))));
    const rbody = el("tbody", {});
    for (const category of cats) {
      rbody.append(el("tr", {},
        el("td", {}, category.led),
        el("td", {}, category.label),
        el("td", { style: "white-space:normal" }, category.description)));
    }
    rtbl.append(rbody);
    root.append(el("div", { class: "tbl-wrap" }, rtbl));
  }

  root.append(el("h2", {}, "Event timeline"),
    el("div", { class: "note" },
      "Every recorded state change with its exact local timestamp — use this to reconstruct the operating conditions around an error, " +
      "then open the matching session for the telemetry at that moment."));

  // The timeline grows without bound, so it gets its own range, category
  // filters, and incremental paging — independent of the page-level range
  // so narrowing the timeline doesn't refetch the alert history above.
  const EVENT_GROUPS = [
    { key: "charging", label: "Charging & sessions",
      kinds: ["session_start", "session_end", "charging_start", "charging_stop"] },
    { key: "evse", label: "EVSE state",
      kinds: ["evse_state_change", "evse_not_ready_change"] },
    { key: "alerts", label: "Alerts & thermal",
      kinds: ["alert_raised", "alert_cleared", "thermal_drift", "thermal_drift_cleared",
              "derate_warning", "derate_warning_cleared", "charger_reboot"] },
    { key: "conn", label: "Connectivity",
      kinds: ["poll_error", "poll_recovered", "wifi_disconnected", "wifi_reconnected",
              "internet_lost", "internet_restored"] },
    { key: "monitor", label: "Monitor",
      kinds: ["monitor_start", "monitor_stop", "monitor_gap", "firmware_changed"] },
  ];
  const TL_PAGE = 100;
  const tl = { rangeKey, groups: new Set(EVENT_GROUPS.map((group) => group.key)), events: [], shown: TL_PAGE };
  const tlControls = el("div", {});
  const tlInfo = el("div", { class: "note" });
  const tlBody = el("div", {});
  const tlPager = el("div", { class: "filters" });
  root.append(tlControls, tlInfo, tlBody, tlPager);

  function tlVisibleEvents() {
    // With every category on, pass everything through — including any event
    // kind added later that no group lists yet.
    if (tl.groups.size === EVENT_GROUPS.length) return tl.events;
    const kinds = new Set(EVENT_GROUPS.filter((group) => tl.groups.has(group.key))
      .flatMap((group) => group.kinds));
    return tl.events.filter((event) => kinds.has(event.kind));
  }

  function tlRender() {
    const evs = tlVisibleEvents();
    const visible = evs.slice(0, tl.shown);
    tlBody.replaceChildren(eventsTable(visible));
    const filtered = tl.groups.size < EVENT_GROUPS.length;
    tlInfo.textContent =
      `${evs.length}${tl.events.length >= 2000 ? "+" : ""} event${evs.length === 1 ? "" : "s"} in range` +
      (filtered ? ` (filtered from ${tl.events.length})` : "") +
      (visible.length < evs.length ? ` — showing the newest ${visible.length}` : "") +
      (tl.events.length >= 2000 ? " · range capped at the most recent 2000; narrow the range for older events" : "");
    tlPager.replaceChildren();
    if (visible.length < evs.length) {
      tlPager.append(
        el("button", { class: "chip", onclick: () => { tl.shown += TL_PAGE; tlRender(); } },
          `Show ${Math.min(TL_PAGE, evs.length - visible.length)} more`),
        el("button", { class: "chip", onclick: () => { tl.shown = evs.length; tlRender(); } },
          `Show all ${evs.length}`));
    }
  }

  function tlControlsRender() {
    const rangeRow = presetRow(RANGE_PRESETS, tl.rangeKey, (key) => {
      tl.rangeKey = key;
      tl.shown = TL_PAGE;
      tlFetch();
    });
    const filterRow = el("div", { class: "filters" }, el("span", { class: "flabel" }, "Show"));
    for (const group of EVENT_GROUPS) {
      filterRow.append(el("button", {
        class: "chip" + (tl.groups.has(group.key) ? " active" : ""),
        onclick: () => {
          if (tl.groups.has(group.key)) tl.groups.delete(group.key);
          else tl.groups.add(group.key);
          tl.shown = TL_PAGE;
          tlControlsRender();
          tlRender();
        },
      }, group.label));
    }
    tlControls.replaceChildren(rangeRow, filterRow);
  }

  async function tlFetch() {
    tlControlsRender();
    tlInfo.textContent = "Loading events…";
    try {
      const nowTs = Date.now() / 1000;
      const data = await getJSON(`/api/events?from=${nowTs - rangeSeconds(tl.rangeKey)}&to=${nowTs}`);
      tl.events = data.events; // newest first from the API
    } catch {
      tlInfo.textContent = "Could not load events.";
      return;
    }
    tlRender();
  }

  await tlFetch();
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
  document.querySelectorAll(".tabs a").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.view === (view === "session" ? "sessions" : view));
  });
  try {
    if (view === "live") cleanup = await viewLive(root);
    else if (view === "sessions") await viewSessions(root, arg);
    else if (view === "session") cleanup = await viewSessionDetail(root, arg);
    else if (view === "energy") await viewEnergy(root, arg);
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
  else if (["live", "sessions", "energy", "wifi", "alerts"].includes(parts[0])) render(parts[0]);
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

/* ---------------- local browser notifications ----------------
 * Actionable warnings only, fired from the SSE stream while the dashboard
 * is open in any tab. Entirely local: the Notification API here involves no
 * push service and no traffic beyond this page — matching the project's
 * nothing-phones-home rule. (For warnings with the dashboard closed, point
 * WM_NOTIFY_URL at a LAN endpoint instead.)
 */
const NOTIFY_KEY = "wm-notify";
const NOTIFY_BUILDERS = {
  derate_warning: (d) => ["Thermal derate predicted",
    `~${fmtNum((d || {}).minutes_to_trip, 0)} min until the handle hits 65 °C and charging folds back to 50%. ` +
    ((d || {}).suggested_max_a
      ? `Set the vehicle's charge current to ≤${fmtNum(d.suggested_max_a, 0)} A to keep a sustained rate.`
      : "Reduce the vehicle's charge current.")],
  alert_raised: (d) => {
    const disp = alertDisplay(String((d || {}).alert), "device");
    return ["Wall Connector alert", disp.sub ? `${disp.label} — ${disp.sub}` : disp.label];
  },
  thermal_drift: (d) => ["Heat rise climbing vs baseline",
    `Recent sessions run +${fmtNum((d || {}).recent_rise_c, 1)} °C vs a +${fmtNum((d || {}).baseline_rise_c, 1)} °C baseline ` +
    "at the same current — inspect the handle and charge-port pins, and have the terminal torque checked."],
  poll_error: () => ["Charger unreachable",
    "Repeated polls failed — check the breaker, Wi-Fi, or the charger itself."],
};

function initNotifications() {
  const btn = $("#notif-btn");
  if (!btn || !("Notification" in window)) return;
  btn.hidden = false;
  // Browsers only grant notification permission on secure origins (HTTPS or
  // localhost). Served over plain http:// on a LAN IP — the normal deploy —
  // the permission prompt silently auto-denies, so a clickable toggle would
  // be a lie. Say so instead, and point at the paths that do work.
  if (!window.isSecureContext) {
    btn.disabled = true;
    btn.textContent = "\u{1F515} warnings need HTTPS";
    btn.title =
      "Browsers only allow notifications on HTTPS or localhost. Options: open the dashboard " +
      "through an SSH tunnel (ssh -L 8480:localhost:8480 <host>, then http://localhost:8480), " +
      "serve it behind HTTPS, or use the WM_NOTIFY_URL webhook — server-side delivery that " +
      "doesn't involve the browser at all.";
    return;
  }
  const enabled = () => localStorage.getItem(NOTIFY_KEY) === "1" && Notification.permission === "granted";
  const paint = () => {
    if (Notification.permission === "denied") {
      btn.textContent = "\u{1F515} warnings blocked";
      btn.title = "Notifications are blocked for this site in the browser's settings — " +
        "re-allow them there, then click again.";
      btn.classList.remove("active");
      return;
    }
    btn.textContent = enabled() ? "\u{1F514} warnings on" : "\u{1F515} warnings off";
    btn.classList.toggle("active", enabled());
  };
  btn.onclick = async () => {
    if (enabled()) localStorage.setItem(NOTIFY_KEY, "0");
    else if ((await Notification.requestPermission()) === "granted") localStorage.setItem(NOTIFY_KEY, "1");
    paint();
  };
  paint();
  live.listeners.add((msg) => {
    if (msg.type !== "event" || !enabled()) return;
    const build = NOTIFY_BUILDERS[msg.kind];
    if (!build) return;
    // The dashboard in front of the user already shows it; notify when the
    // tab is hidden or unfocused (other window, phone in a pocket).
    if (document.visibilityState === "visible" && document.hasFocus()) return;
    try {
      const [title, body] = build(msg.detail);
      new Notification(title, { body, tag: `wm-${msg.kind}` });
    } catch { /* denied or unsupported at fire time — nothing to do */ }
  });
}

window.addEventListener("hashchange", route);
connectSSE();
initNotifications();
loadAlertCodes().finally(refreshStatus);
setInterval(refreshStatus, 10000);
setInterval(tickClock, 1000);
tickClock();
tzNote();
route();
