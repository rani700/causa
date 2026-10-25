"use strict";

// Causa web UI: a thin client over the HTTP API. Every number on screen comes from the engines.

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const SVG = "http://www.w3.org/2000/svg";
const SEVERITY_ORDER = ["critical", "high", "medium", "low"];
const KIND_ICON = {
  source: "⇣", pipeline: "⚙", deployment: "⚑", dashboard: "▦", scheduled_report: "✉",
  reverse_etl: "⇄", spreadsheet: "▤", ad_hoc: "⌕",
};
const KIND_LABEL = {
  source: "Connector", pipeline: "Pipeline", deployment: "Deployment", dashboard: "Dashboard",
  scheduled_report: "Scheduled report", reverse_etl: "Reverse ETL", spreadsheet: "Spreadsheet", ad_hoc: "Ad-hoc SQL",
};

const state = {
  status: null,
  metric: null,
  weekEnd: null, // last day of the selected week (ISO)
  series: null,
  cases: null,
  map: null,
  run: 0, // guards against out-of-order investigation responses
  conversation: null,
};

// -- helpers ---------------------------------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

const day = (iso) => new Date(iso + "T00:00:00Z");
const iso = (d) => d.toISOString().slice(0, 10);
const addDays = (isoDate, n) => iso(new Date(day(isoDate).getTime() + n * 86400000));
const daysBetween = (a, b) => Math.round((day(b) - day(a)) / 86400000);
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fmtDay = (isoDate) => { const d = day(isoDate); return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`; };
const fmtRange = (a, b) => `${fmtDay(a)} – ${fmtDay(b)} ${day(b).getUTCFullYear()}`;

function money(v, unit, signed = true) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const sign = v < 0 ? "−" : signed && v > 0 ? "+" : "";
  const a = Math.abs(v);
  let num;
  if (a === 0) num = "0";
  else if (a >= 1e9) num = (a / 1e9).toFixed(2) + "B";
  else if (a >= 1e6) num = (a / 1e6).toFixed(2) + "M";
  else if (a >= 1e3) num = (a / 1e3).toFixed(1) + "K";
  else num = a.toFixed(a >= 100 ? 0 : 1);
  const symbol = unit === "EUR" ? "€" : unit === "USD" ? "$" : "";
  return symbol ? `${sign}${symbol}${num}` : `${sign}${num}${unit ? " " + unit : ""}`;
}

const pct = (v, signed = true) =>
  v === null || v === undefined ? "–" : `${v < 0 ? "−" : signed && v > 0 ? "+" : ""}${Math.abs(v * 100).toFixed(1)}%`;

function el(tag, attrs = {}, html = "") {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  node.innerHTML = html;
  return node;
}

function svg(tag, attrs = {}, parent = null) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function loading(text, steps = "") {
  return `<div class="card loading"><div class="spinner"></div><div><div>${esc(text)}</div>${steps ? `<div class="steps">${esc(steps)}</div>` : ""}</div></div>`;
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 1800);
}

const tooltip = {
  show(html, x, y) {
    const t = $("#tooltip");
    t.innerHTML = html;
    t.hidden = false;
    const r = t.getBoundingClientRect();
    t.style.left = Math.min(x + 14, window.innerWidth - r.width - 8) + "px";
    t.style.top = Math.max(8, y - r.height - 12) + "px";
  },
  hide() { $("#tooltip").hidden = true; },
};

// -- routing & URL state ---------------------------------------------------------------------

function readUrl() {
  const p = new URLSearchParams(location.search);
  return { view: p.get("view") || "overview", metric: p.get("metric"), week: p.get("week") };
}

function writeUrl(view) {
  const p = new URLSearchParams();
  p.set("view", view || currentView());
  if (state.metric) p.set("metric", state.metric);
  if (state.weekEnd) p.set("week", state.weekEnd);
  history.replaceState(null, "", "?" + p.toString());
}

const currentView = () => $(".tabs [aria-selected=true]")?.dataset.view || "overview";

function show(view) {
  for (const b of $$(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.view === view));
  for (const s of $$(".view")) s.hidden = s.id !== "view-" + view;
  writeUrl(view);
  if (view === "incidents") loadIncidents();
  if (view === "lineage") loadLineage();
  if (view === "ask") initAsk();
}

// -- boot ------------------------------------------------------------------------------------

async function boot() {
  for (const b of $$(".tabs button")) b.addEventListener("click", () => show(b.dataset.view));
  try {
    state.status = await api("/status");
  } catch (e) {
    $("main").innerHTML = `<div class="card error">Could not reach the Causa API: ${esc(e.message)}</div>`;
    return;
  }
  const s = state.status;
  const backend = s.backend === "exasol" ? "Exasol" : s.backend === "duckdb" ? "DuckDB (dev)" : s.backend;
  $("#env").innerHTML = `
    <span class="pill"><span class="dot"></span>${esc(backend)}</span>
    <span class="pill" title="Where lineage and actors come from">${esc(s.query_log)}</span>
    <span class="pill" title="The latest day the query log has seen">Today ${esc(fmtDay(s.today))}</span>`;

  const url = readUrl();
  const select = $("#metric");
  select.innerHTML = s.metrics.map((m) => `<option value="${esc(m.name)}">${esc(m.label)}</option>`).join("");
  state.metric = s.metrics.some((m) => m.name === url.metric) ? url.metric : s.metrics[0]?.name;
  select.value = state.metric;
  state.weekEnd = clampWeek(url.week || addDays(s.today, -1));

  select.addEventListener("change", () => { state.metric = select.value; state.series = null; refreshOverview(); });
  $("#prev-week").addEventListener("click", () => setWeek(addDays(state.weekEnd, -7)));
  $("#next-week").addEventListener("click", () => setWeek(addDays(state.weekEnd, 7)));
  $("#min-severity").addEventListener("change", () => { state.cases = null; loadIncidents(true); });

  show(url.view);
  refreshOverview();
}

function clampWeek(end) {
  const s = state.status;
  const last = addDays(s.today, -1);
  const first = addDays(s.history_start || s.today, 13); // need a full comparison week before it
  if (end > last) return last;
  if (end < first) return first;
  return end;
}

function setWeek(end) {
  const next = clampWeek(end);
  if (next === state.weekEnd) return;
  state.weekEnd = next;
  writeUrl();
  refreshOverview();
}

const metricDef = () => state.status.metrics.find((m) => m.name === state.metric);
const weekStart = () => addDays(state.weekEnd, -6);
const baseline = () => [addDays(state.weekEnd, -13), addDays(state.weekEnd, -7)];

// -- overview --------------------------------------------------------------------------------

async function refreshOverview() {
  const m = metricDef();
  const [b0, b1] = baseline();
  $("#week-current").textContent = fmtRange(weekStart(), state.weekEnd);
  $("#week-baseline").textContent = `compared with ${fmtRange(b0, b1)}`;
  $("#prev-week").disabled = clampWeek(addDays(state.weekEnd, -7)) === state.weekEnd;
  $("#next-week").disabled = clampWeek(addDays(state.weekEnd, 7)) === state.weekEnd;
  $("#chart-title").textContent = `${m.label}, daily`;

  const run = ++state.run;
  const s = state.status;
  if (!state.series) {
    $("#chart").innerHTML = `<div class="loading"><div class="spinner"></div>Loading history…</div>`;
    state.series = await api(`/metrics/${m.name}/series?since=${s.history_start}&until=${s.today}`);
  }
  drawChart();
  if (!state.cases) loadCases().then(drawChart).catch(() => {});

  $("#investigation").innerHTML = loading(
    "Investigating this week…",
    "attribution · health checks · business-vs-data verdict · counterfactual replay of the pipeline · recall",
  );
  try {
    const data = await api(`/metrics/${m.name}/investigation?current=${weekStart()}..${state.weekEnd}`);
    if (run !== state.run) return;
    renderInvestigation(data, m);
  } catch (e) {
    if (run !== state.run) return;
    $("#investigation").innerHTML = `<div class="card error">Investigation failed: ${esc(e.message)}</div>`;
  }
}

async function loadCases() {
  const s = state.status;
  const since = addDays(s.history_start, 21); // detectors need a few weeks of history as baseline
  const min = $("#min-severity").value;
  state.cases = await api(`/health/cases?since=${since}&until=${s.today}&min_severity=${min}`);
  return state.cases;
}

function drawChart() {
  const host = $("#chart");
  const points = state.series.points;
  const unit = state.series.unit;
  host.innerHTML = "";
  if (!points.length) { host.innerHTML = `<div class="empty">No data</div>`; return; }
  const W = host.clientWidth || 900, H = host.clientHeight || 260;
  const M = { l: 58, r: 12, t: 14, b: 24 };
  const first = points[0][0], last = points[points.length - 1][0];
  const span = Math.max(1, daysBetween(first, last) + 1);
  const x = (d) => M.l + (daysBetween(first, d) / span) * (W - M.l - M.r);
  const dayW = (W - M.l - M.r) / span;
  const max = Math.max(...points.map((p) => p[1])) * 1.1 || 1;
  const y = (v) => H - M.b - (v / max) * (H - M.t - M.b);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Daily metric chart" }, host);

  // grid + y labels
  const grid = svg("g", { class: "grid" }, root);
  const axis = svg("g", { class: "axis" }, root);
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    svg("line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v) }, grid);
    svg("text", { x: M.l - 8, y: y(v) + 4, "text-anchor": "end" }, axis).textContent = money(v, unit, false);
  }
  // month labels
  for (let d = first; d <= last; d = addDays(d, 1)) {
    if (d.endsWith("-01")) {
      svg("text", { x: x(d) + 2, y: H - 6 }, axis).textContent = MONTHS[day(d).getUTCMonth()];
      svg("line", { x1: x(d), x2: x(d), y1: M.t, y2: H - M.b }, grid);
    }
  }
  // incident bands
  const cases = state.cases?.cases || [];
  for (const c of cases) {
    const x0 = x(c.start), x1 = x(addDays(c.end, 1));
    svg("rect", { x: x0, y: M.t, width: Math.max(2, x1 - x0), height: H - M.t - M.b, class: `band-${c.severity}` }, root);
    svg("path", { d: `M${x0 - 4},${M.t - 2} L${x0 + 4},${M.t - 2} L${x0},${M.t + 5} Z`, class: `marker-${c.severity}` }, root);
  }
  // selected week and its comparison week
  const [b0] = baseline();
  svg("rect", { x: x(b0), y: M.t, width: dayW * 7, height: H - M.t - M.b, class: "band-base", rx: 3 }, root);
  svg("rect", { x: x(weekStart()), y: M.t, width: dayW * 7, height: H - M.t - M.b, class: "band-week", rx: 3 }, root);

  // series
  const pts = points.map(([d, v]) => [x(d) + dayW / 2, y(v)]);
  const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join("");
  svg("path", { d: `${line}L${pts[pts.length - 1][0]},${H - M.b}L${pts[0][0]},${H - M.b}Z`, class: "area" }, root);
  svg("path", { d: line, class: "series" }, root);

  // hover + click
  const hoverLine = svg("line", { class: "hover-line", y1: M.t, y2: H - M.b, visibility: "hidden" }, root);
  const hoverDot = svg("circle", { class: "hover-dot", r: 4, visibility: "hidden" }, root);
  const byDay = new Map(points);
  const dayAt = (evt) => {
    const r = root.getBoundingClientRect();
    const px = ((evt.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(span - 1, Math.floor(((px - M.l) / (W - M.l - M.r)) * span)));
    return addDays(first, i);
  };
  root.addEventListener("mousemove", (evt) => {
    const d = dayAt(evt);
    const v = byDay.get(d);
    const cx = x(d) + dayW / 2;
    hoverLine.setAttribute("x1", cx); hoverLine.setAttribute("x2", cx); hoverLine.setAttribute("visibility", "visible");
    if (v !== undefined) { hoverDot.setAttribute("cx", cx); hoverDot.setAttribute("cy", y(v)); hoverDot.setAttribute("visibility", "visible"); }
    const hits = cases.filter((c) => c.start <= d && d <= c.end);
    tooltip.show(
      `<div class="muted">${esc(fmtDay(d))} ${day(d).getUTCFullYear()}</div><div class="t-value">${esc(money(v ?? 0, unit, false))}</div>` +
        hits.map((c) => `<div class="t-case"><span class="badge ${c.severity}">${c.severity}</span><span>${esc(c.headline.slice(0, 120))}</span></div>`).join(""),
      evt.clientX, evt.clientY,
    );
  });
  root.addEventListener("mouseleave", () => { tooltip.hide(); hoverLine.setAttribute("visibility", "hidden"); hoverDot.setAttribute("visibility", "hidden"); });
  root.addEventListener("click", (evt) => setWeek(dayAt(evt)));
}

window.addEventListener("resize", () => {
  clearTimeout(drawChart.t);
  drawChart.t = setTimeout(() => { if (state.series && currentView() === "overview") drawChart(); }, 120);
});

function renderInvestigation(data, m) {
  const v = data.verdict;
  const h = v.headline;
  const t = h.total;
  const unit = m.unit;
  const findings = v.findings.filter((f) => f.counted_as_data_incident);
  const host = $("#investigation");
  host.innerHTML = "";

  // KPIs
  const bizUnusual = v.business_change_z !== null && Math.abs(v.business_change_z) >= 2;
  const dataShare = t.change ? v.data_incident_impact / t.change : null;
  host.appendChild(el("div", { class: "card verdict-hero" }, `
    <div class="kpi total">
      <div class="kpi-label"><span class="dot"></span>Reported change</div>
      <div class="kpi-value">${esc(money(t.change, unit))}</div>
      <div class="kpi-sub">${esc(pct(t.change_pct))} · ${esc(money(t.current, unit, false))} vs ${esc(money(t.baseline, unit, false))}
        ${h.unusual === null ? "" : `<span class="badge ${h.unusual ? "unusual" : "normal"}">${h.unusual ? "unusual" : "normal"}</span>`}</div>
      <div class="kpi-sub faint">a typical week moves ±${esc((h.typical_change_pct * 100).toFixed(1))}%</div>
    </div>
    <div class="kpi data">
      <div class="kpi-label"><span class="dot"></span>Broken data</div>
      <div class="kpi-value">${findings.length ? esc(money(v.data_incident_impact, unit)) : "none"}</div>
      <div class="kpi-sub">${findings.length
        ? `${findings.length} data incident${findings.length > 1 ? "s" : ""}${dataShare !== null && dataShare > 0 && dataShare <= 1.5 ? ` · ${Math.round(dataShare * 100)}% of the change` : ""}`
        : "no data incident touches this change"}</div>
    </div>
    <div class="kpi biz">
      <div class="kpi-label"><span class="dot"></span>Real business change</div>
      <div class="kpi-value">${esc(money(v.business_change, unit))}</div>
      <div class="kpi-sub">${esc(pct(v.business_change_pct))}
        ${v.business_change_z === null ? "" : `<span class="badge ${bizUnusual ? "unusual" : "normal"}">${bizUnusual ? "unusual" : "within normal variation"}</span>`}</div>
    </div>`));

  // One sentence a finance lead can forward.
  const verb = t.change < 0 ? "drop" : "rise";
  let story;
  if (!findings.length) {
    story = `No data incident touches this week: the ${verb} of <strong>${esc(money(Math.abs(t.change), unit, false))}</strong> is <strong class="biz">real business movement</strong>.`;
  } else {
    const causes = findings.map((f) => esc(f.cause)).join("; and ");
    story = `Of the ${verb} of <strong>${esc(money(Math.abs(t.change), unit, false))}</strong>, <strong class="data">${esc(money(v.data_incident_impact, unit))}</strong> is broken data: ${causes}. ` +
      `The <strong class="biz">real business change is ${esc(money(v.business_change, unit))} (${esc(pct(v.business_change_pct))})</strong>, ${bizUnusual ? "which is unusual" : "within normal variation"}.`;
  }
  host.appendChild(el("div", { class: "card story" }, story));
  host.appendChild(bridge(t.baseline, v.data_incident_impact, v.business_change, t.current, unit));

  // Data incidents
  if (v.findings.length) {
    host.appendChild(el("div", { class: "section-title" }, `<h2>Data incidents</h2><span class="muted">what broke, by how much, and how sure Causa is</span>`));
    for (const f of v.findings) host.appendChild(findingCard(f, unit));
  }

  // Business movement + recall side by side
  const row = el("div", { class: "grid-2" });
  row.appendChild(businessCard(v, unit));
  row.appendChild(recallCard(data.recalls, unit));
  host.appendChild(row);
}

function bridge(base, dataImpact, biz, current, unit) {
  const card = el("div", { class: "card bridge" });
  const W = 640, H = 150, top = 22, bottom = 26;
  const steps = [
    { label: "Compared with", value: base, from: 0, to: base, cls: "bar-total" },
    { label: "Broken data", value: dataImpact, from: base, to: base + dataImpact, cls: "bar-data" },
    { label: "Real business", value: biz, from: base + dataImpact, to: base + dataImpact + biz, cls: "bar-biz" },
    { label: "Reported", value: current, from: 0, to: current, cls: "bar-total" },
  ];
  const hi = Math.max(...steps.map((s) => Math.max(s.from, s.to))) * 1.02;
  const lo = Math.min(0, ...steps.map((s) => Math.min(s.from, s.to)));
  // zoom the axis so that small steps are visible, without hiding the totals entirely
  const floor = Math.max(lo, Math.min(...steps.slice(1, 3).map((s) => Math.min(s.from, s.to))) - (hi - lo) * 0.25);
  const y = (v) => top + (1 - (v - floor) / (hi - floor)) * (H - top - bottom);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}` }, card);
  const bw = 90, gap = (W - bw * 4) / 4;
  steps.forEach((s, i) => {
    const x0 = gap / 2 + i * (bw + gap);
    const yTop = y(Math.max(s.from, s.to)), yBot = y(Math.max(floor, Math.min(s.from, s.to)));
    svg("rect", { x: x0, y: yTop, width: bw, height: Math.max(2, yBot - yTop), rx: 3, class: s.cls }, root);
    const label = svg("text", { x: x0 + bw / 2, y: H - 8, "text-anchor": "middle" }, root);
    label.textContent = s.label;
    const val = svg("text", { x: x0 + bw / 2, y: yTop - 6, "text-anchor": "middle", class: "val" }, root);
    val.textContent = i === 0 || i === 3 ? money(s.value, unit, false) : money(s.value, unit);
    if (i < 3) {
      const nx = gap / 2 + (i + 1) * (bw + gap);
      svg("line", { x1: x0 + bw, x2: nx, y1: y(s.to), y2: y(s.to), class: "connector" }, root);
    }
  });
  return card;
}

function findingCard(f, unit) {
  const replayed = f.method.startsWith("counterfactual");
  const card = el("div", { class: "card finding" });
  if (!f.counted_as_data_incident) card.style.borderLeftColor = "var(--border)";
  const maxLo = Math.max(3, ...f.evidence.map((e) => Math.abs(e.log_odds)));
  const lands = f.lands_in.map((l) => `${esc(l.driver)}${l.share_of_affected_value < 0.99 ? ` (${Math.round(l.share_of_affected_value * 100)}%)` : ""}`).join(", ");
  card.innerHTML = `
    <div class="finding-head">
      <div>
        <div class="finding-cause">${esc(f.cause)}</div>
        <div class="finding-meta" style="margin-top:6px">
          <span class="badge replayed">${replayed ? "replayed on repaired data" : esc(f.method.split(":")[0])}</span>
          <span class="confidence">confidence <span class="meter"><i style="width:${Math.round(f.confidence * 100)}%"></i></span>${Math.round(f.confidence * 100)}%</span>
          <span class="case-id">${esc(f.case_id)}</span>
          ${f.duplicate_of ? `<span class="badge">same incident as ${esc(f.duplicate_of)}</span>` : ""}
          ${!f.counted_as_data_incident && !f.duplicate_of ? `<span class="badge">not counted</span>` : ""}
        </div>
      </div>
      <div class="finding-impact" title="Effect on the week's change">${esc(money(f.impact_on_change, unit))}</div>
    </div>
    ${lands ? `<div class="method">Affected rows: <code>${esc(f.affected_rows)}</code> · lands in ${lands}</div>` : ""}
    <div class="days">
      ${f.affected_current_days.map((d) => `<span class="day-chip" title="this week">${esc(fmtDay(d))}</span>`).join("")}
      ${f.affected_baseline_days.map((d) => `<span class="day-chip base" title="comparison week">${esc(fmtDay(d))}</span>`).join("")}
    </div>
    <details>
      <summary>Why Causa believes this is broken data</summary>
      <ul class="evidence">
        ${f.evidence.map((e) => `<li><span class="lo ${e.log_odds < 0 ? "neg" : ""}" style="width:${Math.max(6, (Math.abs(e.log_odds) / maxLo) * 70)}px" title="log-odds ${e.log_odds}"></span><span>${esc(e.reason)}</span></li>`).join("")}
      </ul>
    </details>
    <details>
      <summary>How the impact was measured</summary>
      <div class="method">${esc(f.method)}</div>
      ${f.earlier_estimate !== null && f.earlier_estimate !== undefined ? `<div class="method">Statistical estimate before replay: ${esc(money(f.earlier_estimate, unit))}</div>` : ""}
      ${f.impact_band_95 ? `<div class="method">95% band: ${esc(money(f.impact_band_95[0], unit))} to ${esc(money(f.impact_band_95[1], unit))}</div>` : ""}
      ${f.not_replayed_because ? `<div class="method">Not replayed: ${esc(f.not_replayed_because)}</div>` : ""}
      ${f.notes.map((n) => `<div class="method">${esc(n)}</div>`).join("")}
      <div class="method">Health check: ${esc(f.health_summary)}</div>
    </details>`;
  return card;
}

function segmentLabel(seg) {
  return esc(seg).replaceAll(" &amp; ", " › ");
}

function businessCard(v, unit) {
  const card = el("div", { class: "card" });
  const drivers = v.business_drivers_on_clean_days || [];
  const affected = (v.days_excluded_as_affected || []).filter((d) => d >= weekStart() && d <= state.weekEnd);
  const clean = 7 - affected.length;
  card.innerHTML = `<div class="chart-head"><h2>Real business movement</h2>
    <span class="muted" style="font-size:12.5px">${affected.length ? `on days without data incidents (${clean} of 7)` : "whole week"}</span></div>`;
  if (!drivers.length) {
    card.appendChild(el("div", { class: "empty" }, clean < 3
      ? `<div class="big">Not enough clean days</div>Data incidents cover most of the week, so the business movement is read from the total only.`
      : `<div class="big">No single segment stands out</div>The change is spread across the business.`));
    return card;
  }
  const rows = [];
  for (const d of drivers.slice(0, 5)) {
    rows.push({ seg: d.segment, change: d.change, child: false, vanished: d.vanished });
    for (const c of (d.drill_down || []).slice(0, 3)) rows.push({ seg: c.segment.split(" & ").slice(-1)[0], change: c.change, child: true, vanished: c.vanished });
  }
  const max = Math.max(...rows.map((r) => Math.abs(r.change))) || 1;
  const list = el("div", { class: "drivers" });
  for (const r of rows) {
    const w = (Math.abs(r.change) / max) * 50;
    list.appendChild(el("div", { class: `driver${r.child ? " child" : ""}` }, `
      <span class="seg" title="${esc(r.seg)}">${segmentLabel(r.seg)}${r.vanished ? ' <span class="badge unusual">stopped</span>' : ""}</span>
      <span class="bar"><i class="${r.change >= 0 ? "pos" : "neg"}" style="width:${w}%"></i></span>
      <span class="amt">${esc(money(r.change, unit))}</span>`));
  }
  card.appendChild(list);
  return card;
}

function recallCard(recalls, unit) {
  const card = el("div", { class: "card" });
  card.innerHTML = `<div class="chart-head"><h2>Who saw wrong numbers</h2></div>`;
  if (!recalls.length) {
    card.appendChild(el("div", { class: "empty" }, `<div class="big">Nobody</div>No report, dashboard or sync read data affected by an incident this week.`));
    return card;
  }
  for (const r of recalls) {
    const open = r.exposure[1] === "still open";
    card.appendChild(el("div", { class: "exposure" }, `
      <span class="badge ${open ? "unusual" : "normal"}">${open ? "still wrong in the warehouse" : "healed " + esc(fmtDay(r.exposure[1]))}</span>
      <span>misstated by <strong>${esc(money(r.misstatement_total, unit))}</strong> · since ${esc(fmtDay(r.exposure[0]))} · <span class="case-id">${esc(r.case_id)}</span></span>`));
    const table = el("table", { class: "consumers" });
    table.innerHTML = `<thead><tr><th>Consumer</th><th class="num">Reads</th><th class="num hide-sm">Last read</th><th></th></tr></thead>`;
    const body = el("tbody");
    r.consumers.forEach((c, i) => {
      const tr = el("tr", {}, `
        <td><span class="kind"><span class="kind-icon" title="${esc(KIND_LABEL[c.kind] || c.kind)}">${KIND_ICON[c.kind] || "•"}</span>
          <span><strong>${esc(c.name)}</strong><br><span class="faint">${esc(c.tool)}${c.attributes?.recipients ? ` · ${esc(c.attributes.recipients)} recipients` : ""}</span></span></span>
          <div class="notice" hidden>${esc(c.draft_notice)}</div></td>
        <td class="num">${c.reads || !c.rebuilt.length ? c.reads : `<span title="${esc(c.rebuilt.join(", "))}">rebuilt ${c.rebuilt.length}</span>`}<br><span class="faint">${c.active_days} day${c.active_days === 1 ? "" : "s"}</span></td>
        <td class="num hide-sm faint">${c.last_read ? esc(fmtDay(c.last_read.slice(0, 10))) + " " + esc(c.last_read.slice(11, 16)) : "–"}</td>
        <td><button class="ghost" data-i="${i}">Notice</button></td>`);
      const btn = $("button", tr);
      btn.addEventListener("click", async () => {
        const notice = $(".notice", tr);
        notice.hidden = !notice.hidden;
        try { await navigator.clipboard.writeText(c.draft_notice); toast("Correction notice copied"); } catch { /* clipboard blocked */ }
      });
      body.appendChild(tr);
    });
    table.appendChild(body);
    card.appendChild(table);
  }
  return card;
}

// -- incidents -------------------------------------------------------------------------------

async function loadIncidents(force = false) {
  const s = state.status;
  if (!state.cases || force) {
    $("#timeline").innerHTML = `<div class="loading"><div class="spinner"></div>Scanning data health…</div>`;
    $("#cases").innerHTML = "";
    try { await loadCases(); } catch (e) {
      $("#timeline").innerHTML = `<div class="error">Health scan failed: ${esc(e.message)}</div>`;
      return;
    }
    if (currentView() === "overview" && state.series) drawChart();
  }
  const { cases, monitored_tables } = state.cases;
  $("#incidents-hint").textContent = `${cases.length} case${cases.length === 1 ? "" : "s"} · ${monitored_tables.length} tables monitored automatically`;
  drawTimeline(cases, addDays(s.history_start, 21), s.today);
  const host = $("#cases");
  host.innerHTML = "";
  if (!cases.length) { host.appendChild(el("div", { class: "card empty" }, `<div class="big">All clear</div>No incidents at this severity.`)); return; }
  for (const c of [...cases].sort((a, b) => (a.start < b.start ? 1 : -1))) {
    const days = daysBetween(c.start, c.end) + 1;
    const card = el("div", { class: "card case", id: "case-" + c.case_id }, `
      <div class="case-when"><span class="badge ${c.severity}">${c.severity}</span>
        <span>${esc(fmtDay(c.start))}${days > 1 ? " – " + esc(fmtDay(c.end)) : ""}</span><span class="faint">${days} day${days > 1 ? "s" : ""}</span></div>
      <div>
        <div class="case-head">${esc(c.headline)}</div>
        ${c.other_incidents.length ? `<ul>${c.other_incidents.slice(0, 4).map((i) => `<li>${esc(i)}</li>`).join("")}${c.other_incidents.length > 4 ? `<li class="faint">and ${c.other_incidents.length - 4} more</li>` : ""}</ul>` : ""}
        <div class="tables">${c.tables.map((t) => `<button class="table-chip" data-table="${esc(t)}">${esc(t)}</button>`).join("")}</div>
      </div>
      <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">
        <span class="case-id">${esc(c.case_id)}</span>
        <button class="ghost" data-act="week">Open week</button>
        <button class="ghost" data-act="blast">Blast radius</button>
      </div>`);
    $$(".table-chip", card).forEach((b) => b.addEventListener("click", () => openLineage(b.dataset.table)));
    $("[data-act=week]", card).addEventListener("click", () => { setWeek(addDays(c.start, 3)); show("overview"); });
    $("[data-act=blast]", card).addEventListener("click", () => openLineage(c.tables, true, c));
    host.appendChild(card);
  }
}

function drawTimeline(cases, first, last) {
  const host = $("#timeline");
  host.innerHTML = "";
  const W = host.clientWidth - 36 || 900;
  const lanes = [];
  const placed = [...cases].sort((a, b) => (a.start < b.start ? -1 : 1)).map((c) => {
    let lane = lanes.findIndex((end) => end < c.start);
    if (lane < 0) { lane = lanes.length; lanes.push(""); }
    lanes[lane] = addDays(c.end, 2);
    return { c, lane };
  });
  const H = 30 + Math.max(1, lanes.length) * 14;
  const span = daysBetween(first, last) + 1;
  const x = (d) => (Math.max(0, daysBetween(first, d)) / span) * W;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, height: H }, host);
  svg("line", { x1: 0, x2: W, y1: H - 18, y2: H - 18, class: "tl-axis" }, root);
  for (let d = first; d <= last; d = addDays(d, 1)) {
    if (d.endsWith("-01")) svg("text", { x: x(d), y: H - 4 }, root).textContent = MONTHS[day(d).getUTCMonth()];
  }
  for (const { c, lane } of placed) {
    const r = svg("rect", { x: x(c.start), y: 6 + lane * 14, width: Math.max(6, x(addDays(c.end, 1)) - x(c.start)), height: 9, rx: 3, class: `tl-bar tl-${c.severity}` }, root);
    r.addEventListener("mousemove", (e) => tooltip.show(`<span class="badge ${c.severity}">${c.severity}</span> ${esc(fmtDay(c.start))}<div>${esc(c.headline.slice(0, 160))}</div>`, e.clientX, e.clientY));
    r.addEventListener("mouseleave", tooltip.hide);
    r.addEventListener("click", () => {
      $$(".case.selected").forEach((n) => n.classList.remove("selected"));
      const card = document.getElementById("case-" + c.case_id);
      card?.classList.add("selected");
      card?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }
}

// -- lineage ---------------------------------------------------------------------------------

async function loadLineage() {
  if (state.map) return;
  $("#lineage").innerHTML = `<div class="loading"><div class="spinner"></div>Reconstructing lineage…</div>`;
  try {
    state.map = await api("/lineage-map");
  } catch (e) {
    $("#lineage").innerHTML = `<div class="error">${esc(e.message)}</div>`;
    return;
  }
  drawLineage();
  if (state.pendingLineage) { const p = state.pendingLineage; state.pendingLineage = null; openLineage(...p); }
}

function layoutLineage(map) {
  const nodes = new Map(map.nodes.map((n) => [n.id, { ...n, in: [], out: [] }]));
  const edges = map.edges.filter((e) => nodes.has(e.source) && nodes.has(e.target));
  for (const e of edges) { nodes.get(e.source).out.push(e); nodes.get(e.target).in.push(e); }
  // columns: connectors | tables by depth | consumers
  const depth = new Map();
  const tableDepth = (id, seen = new Set()) => {
    if (depth.has(id)) return depth.get(id);
    if (seen.has(id)) return 1;
    seen.add(id);
    const feeders = nodes.get(id).in.filter((e) => e.kind === "feeds").map((e) => e.source);
    const d = feeders.length ? 1 + Math.max(...feeders.map((f) => tableDepth(f, seen))) : 1;
    depth.set(id, d);
    return d;
  };
  for (const n of nodes.values()) if (n.type === "table") n.col = tableDepth(n.id);
  const maxCol = Math.max(1, ...[...nodes.values()].filter((n) => n.type === "table").map((n) => n.col));
  for (const n of nodes.values()) {
    if (n.type !== "actor") continue;
    const loads = n.out.some((e) => e.kind === "loads");
    n.col = loads ? 0 : maxCol + 1;
  }
  const cols = [];
  for (const n of nodes.values()) (cols[n.col] ||= []).push(n);
  for (const c of cols) c?.sort((a, b) => (a.group + a.name).localeCompare(b.group + b.name));
  // barycentre ordering, a few sweeps in both directions
  const pos = new Map();
  const index = () => cols.forEach((c) => c?.forEach((n, i) => pos.set(n.id, i)));
  index();
  for (let sweep = 0; sweep < 6; sweep++) {
    const forward = sweep % 2 === 0;
    const order = forward ? cols.keys() : [...cols.keys()].reverse();
    for (const ci of order) {
      const col = cols[ci];
      if (!col) continue;
      const score = (n) => {
        const nb = forward ? n.in.map((e) => e.source) : n.out.map((e) => e.target);
        const ps = nb.filter((id) => pos.has(id)).map((id) => pos.get(id));
        return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : pos.get(n.id);
      };
      col.sort((a, b) => score(a) - score(b));
      col.forEach((n, i) => pos.set(n.id, i));
    }
  }
  return { nodes, edges, cols };
}

function drawLineage() {
  const { nodes, edges, cols } = layoutLineage(state.map);
  const NW = 168, NH = 38, GX = 46, GY = 12, TOP = 30;
  const tallest = Math.max(...Array.from(cols, (c) => c?.length || 0));
  const W = cols.length * NW + (cols.length - 1) * GX + 8;
  const H = TOP + tallest * (NH + GY) + 8;
  const drifted = new Set(state.map.schema_changes.map((c) => "table:" + c.table));
  const host = $("#lineage");
  host.innerHTML = "";
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}` }, host);
  cols.forEach((col, ci) => {
    if (!col) return;
    const x = ci * (NW + GX) + 4;
    const offset = ((tallest - col.length) * (NH + GY)) / 2;
    col.forEach((n, i) => { n.x = x; n.y = TOP + offset + i * (NH + GY); });
    const groups = [...new Set(col.map((n) => (n.type === "table" ? n.group : null)).filter(Boolean))];
    const title = ci === 0 ? "Sources" : ci === cols.length - 1 ? "Consumers" : groups.join(" · ");
    svg("text", { x: x + 2, y: 14, class: "col-title" }, root).textContent = title;
  });
  const edgeLayer = svg("g", {}, root);
  for (const e of edges) {
    const a = nodes.get(e.source), b = nodes.get(e.target);
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
    const mx = (x1 + x2) / 2;
    e.el = svg("path", { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, class: `edge ${e.kind}` }, edgeLayer);
  }
  for (const n of nodes.values()) {
    const g = svg("g", { class: `node ${n.type}${drifted.has(n.id) ? " drifted" : ""}`, transform: `translate(${n.x},${n.y})` }, root);
    svg("rect", { width: NW, height: NH, rx: 8 }, g);
    const icon = n.type === "actor" ? (KIND_ICON[n.group] || "•") + " " : "";
    const label = svg("text", { x: 10, y: 16 }, g);
    const text = icon + n.label;
    label.textContent = text.length > 23 ? text.slice(0, 22) + "…" : text;
    if (text.length > 23) svg("title", {}, g).textContent = n.name;
    const sub = svg("text", { x: 10, y: 30, class: "sub" }, g);
    sub.textContent = n.type === "table"
      ? `${n.group} · ${n.object_type}${drifted.has(n.id) ? " · schema changed" : ""}`
      : `${n.tool} · ${KIND_LABEL[n.group] || n.group}`.slice(0, 30);
    n.el = g;
    g.addEventListener("click", () => selectNode(n.id));
  }
  state.layout = { nodes, edges };
}

function walk(start, step) {
  const seen = new Set();
  const stack = [start];
  while (stack.length) for (const next of step(stack.pop())) if (!seen.has(next)) { seen.add(next); stack.push(next); }
  seen.delete(start);
  return seen;
}

function clearLineage() {
  const { nodes, edges } = state.layout;
  $("#lineage").classList.remove("focus");
  for (const n of nodes.values()) n.el.classList.remove("on", "sel", "up", "down", "hit");
  for (const e of edges) e.el.classList.remove("on", "up", "down", "hit");
}

function selectNode(id) {
  const { nodes, edges } = state.layout;
  clearLineage();
  const up = walk(id, (n) => nodes.get(n).in.map((e) => e.source));
  const down = walk(id, (n) => nodes.get(n).out.map((e) => e.target));
  $("#lineage").classList.add("focus");
  nodes.get(id).el.classList.add("on", "sel");
  for (const u of up) nodes.get(u).el.classList.add("on", "up");
  for (const d of down) nodes.get(d).el.classList.add("on", "down");
  for (const e of edges) {
    if ((up.has(e.source) || e.source === id) && (up.has(e.target) || e.target === id)) e.el.classList.add("on", "up");
    if ((down.has(e.target) || e.target === id) && (down.has(e.source) || e.source === id)) e.el.classList.add("on", "down");
  }
  showSide(nodes.get(id));
}

function openSide(html) {
  const side = $("#lineage-side");
  side.hidden = false;
  side.innerHTML = `<button class="close" aria-label="Close" title="Close">×</button>${html}`;
  $(".close", side).addEventListener("click", () => { side.hidden = true; clearLineage(); });
  side.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return side;
}

async function showSide(n) {
  if (n.type === "actor") {
    const reads = n.in.filter((e) => e.kind === "reads");
    const loads = n.out.filter((e) => e.kind === "loads");
    openSide(`
      <div class="head"><div><span class="badge">${esc(KIND_LABEL[n.group] || n.group)}</span></div>
      <h3>${esc(n.name)}</h3></div>
      <div class="kv"><span class="kv-label">Tool</span><span>${esc(n.tool)}</span></div>
      ${Object.keys(n.attributes || {}).length ? `<div class="kv"><span class="kv-label">Identified from the client string</span><ul>${Object.entries(n.attributes).map(([k, v]) => `<li><code>${esc(k)}=${esc(v)}</code></li>`).join("")}</ul></div>` : ""}
      ${loads.length ? `<div class="kv"><span class="kv-label">Loads</span><ul>${loads.map((e) => `<li><code>${esc(e.target.slice(6))}</code> <span class="faint">· ${e.statements} loads</span></li>`).join("")}</ul></div>` : ""}
      ${reads.length ? `<div class="kv"><span class="kv-label">Reads</span><ul>${reads.map((e) => `<li><code>${esc(e.source.slice(6))}</code> <span class="faint">· ${e.statements} queries</span></li>`).join("")}</ul></div>` : ""}`);
    return;
  }
  openSide(`<div class="head"><h3>${esc(n.name)}</h3></div><div class="loading"><div class="spinner"></div>Loading…</div>`);
  try {
    const info = await api(`/lineage/${encodeURIComponent(n.name)}`);
    const gating = Object.entries(info.row_gating_columns);
    openSide(`
      <div class="head"><div><span class="badge">${esc(n.object_type)}</span> <span class="badge">${esc(n.group)}</span>
        <button class="ghost" id="blast-btn" style="margin-left:8px">If this table broke: blast radius</button></div>
      <h3>${esc(n.name)}</h3></div>
      ${n.built_by.length ? `<div class="kv"><span class="kv-label">Written by</span><ul>${n.built_by.map((b) => `<li>${esc(b)}</li>`).join("")}</ul></div>` : ""}
      <div class="kv"><span class="kv-label">Built from (${info.built_from.length})</span>${info.built_from.length ? `<ul>${info.built_from.map((t) => `<li><code>${esc(t)}</code></li>`).join("")}</ul>` : `<span class="faint">a source table</span>`}</div>
      <div class="kv"><span class="kv-label">Feeds (${info.feeds.length})</span>${info.feeds.length ? `<ul>${info.feeds.map((t) => `<li><code>${esc(t)}</code></li>`).join("")}</ul>` : `<span class="faint">nothing downstream</span>`}</div>
      ${gating.length ? `<details><summary>Columns that decide which rows survive (${gating.reduce((a, [, c]) => a + c.length, 0)})</summary>${gating.map(([t, cs]) => `<div class="kv" style="margin-bottom:6px"><span class="faint">into <code>${esc(t)}</code></span><ul>${cs.map((c) => `<li><code>${esc(c)}</code></li>`).join("")}</ul></div>`).join("")}</details>` : ""}
      ${info.schema_changes.length ? `<div class="kv"><span class="kv-label">Schema changes</span>${info.schema_changes.map((c) => `<div class="ddl">${esc(c.ddl)}<br><span class="faint">${esc(c.by)} · ${esc(c.at.slice(0, 16))}</span></div>`).join("")}</div>` : ""}
      <div id="blast"></div>`);
    $("#blast-btn").addEventListener("click", () => blastRadius([n.name]));
  } catch (e) {
    openSide(`<div class="error">${esc(e.message)}</div>`);
  }
}

async function blastRadius(tables, kase = null) {
  const { nodes, edges } = state.layout;
  // start from the most upstream table of the set
  const ids = tables.map((t) => "table:" + t).filter((id) => nodes.has(id));
  if (!ids.length) return;
  const origin = ids.reduce((a, b) => (nodes.get(b).col < nodes.get(a).col ? b : a));
  const name = origin.slice(6);
  const params = kase ? `?since=${kase.start}&until=${state.status.today}` : "";
  const target = $("#blast") || openSide("");
  try {
    const impact = await api(`/lineage/${encodeURIComponent(name)}/impact${params}`);
    clearLineage();
    $("#lineage").classList.add("focus");
    const hit = new Set([...impact.tables.map((t) => "table:" + t), ...impact.actors.map((a) => a.node)]);
    for (const id of hit) nodes.get(id)?.el.classList.add("on", "hit");
    for (const e of edges) if (hit.has(e.source) && hit.has(e.target)) e.el.classList.add("on", "hit");
    const consumers = impact.actors.filter((a) => a.reads > 0);
    const html = `
      <div class="kv blast-list"><span class="kv-label">Blast radius of ${esc(name)}${kase ? ` since ${esc(fmtDay(kase.start))}` : ""}</span>
        <span>${impact.tables.length} tables · ${consumers.length} consumers</span>
      <ul>${consumers.sort((a, b) => b.reads - a.reads).map((a) => `<li>${KIND_ICON[a.kind] || "•"} <strong>${esc(a.name)}</strong> <span class="faint">· ${a.reads} reads on ${a.active_days} days</span></li>`).join("")}</ul></div>`;
    if (kase) {
      openSide(`<div class="head"><div><span class="badge ${kase.severity}">${kase.severity}</span> <span class="case-id">${esc(kase.case_id)}</span></div><div class="case-head">${esc(kase.headline)}</div></div>${html}`);
    } else {
      target.innerHTML = html;
    }
  } catch (e) {
    target.innerHTML = `<div class="error">${esc(e.message)}</div>`;
  }
}

function openLineage(tables, blast = false, kase = null) {
  show("lineage");
  if (!state.layout) { state.pendingLineage = [tables, blast, kase]; return; }
  const list = Array.isArray(tables) ? tables : [tables];
  if (blast) blastRadius(list, kase);
  else if (state.layout.nodes.has("table:" + list[0])) selectNode("table:" + list[0]);
}

// -- ask -------------------------------------------------------------------------------------

function initAsk() {
  if (initAsk.done) return;
  initAsk.done = true;
  const s = state.status;
  const m = metricDef();
  if (!s.agent_available) {
    $("#messages").innerHTML = `<div class="empty"><div class="big">The investigator agent is off</div>
      Add <code>OPENAI_API_KEY</code> or <code>ANTHROPIC_API_KEY</code> to <code>.env</code> and restart the server to ask questions in plain language.<br>
      Everything else in Causa works without it: the agent only calls the same engines you see in the other tabs.</div>`;
    $("#ask-form").hidden = true;
    return;
  }
  const label = m.label.toLowerCase();
  const questions = [
    `Why did ${label} change last week? Is it real?`,
    `Which dashboards and reports showed wrong ${label} this month?`,
    `What broke in the data pipeline in the last 30 days?`,
    `Which segment drove ${label} in the last 4 weeks?`,
  ];
  $("#messages").innerHTML = `<div class="empty"><div class="big">Ask Causa</div>Answers come from the engines, with every tool call shown underneath.</div>`;
  $("#suggestions").innerHTML = questions.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  $$("#suggestions button").forEach((b) => b.addEventListener("click", () => { $("#question").value = b.textContent; ask(); }));
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask(); });
  $("#question").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } });
}

function markdown(text) {
  // Small, safe subset: escape first, then paragraphs, bullet lists, bold and inline code.
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = [];
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) { (list ||= []).push(`<li>${inline(item[1])}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    const heading = line.match(/^#{1,4}\s+(.*)$/);
    if (heading) out.push(`<p><strong>${inline(heading[1])}</strong></p>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("");
}

async function ask() {
  const q = $("#question").value.trim();
  if (!q) return;
  const box = $("#messages");
  if (!state.conversation) box.innerHTML = "";
  $("#suggestions").hidden = true;
  box.appendChild(el("div", { class: "msg user" }, esc(q)));
  const pending = el("div", { class: "msg bot" }, `<div class="loading"><div class="spinner"></div>Investigating…</div>`);
  box.appendChild(pending);
  pending.scrollIntoView({ behavior: "smooth", block: "end" });
  $("#question").value = "";
  $("#ask-btn").disabled = true;
  try {
    const r = await api("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, conversation_id: state.conversation }),
    });
    state.conversation = r.conversation_id;
    pending.innerHTML = `<div class="body">${markdown(r.answer)}</div>
      ${r.tool_calls.length ? `<div class="trace">${r.tool_calls.map((c) => `<span class="badge ${c.error ? "unusual" : ""}" title="${esc(JSON.stringify(c.input))}">${esc(c.tool)} · ${c.seconds}s</span>`).join("")}</div>` : ""}
      <div class="usage">${esc(r.model || "")} · ${r.input_tokens.toLocaleString()} in (${r.cache_read_tokens.toLocaleString()} cached) · ${r.output_tokens.toLocaleString()} out</div>`;
  } catch (e) {
    pending.innerHTML = `<div class="card error">${esc(e.message)}</div>`;
  } finally {
    $("#ask-btn").disabled = false;
  }
}

boot();
