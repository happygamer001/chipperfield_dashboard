/* Fab Shop views shared by Calvin's dashboard (Fab Shop page) and Jerron's
   portal (Evaluation of the Fab Shop): value graph with the tap-a-day panel,
   Daily Log, Weekly Value, Absences & PTO. Needs shared/log-cards.js and Chart.js.

   mountFabshopViews(rootEl, {
     tabs:  [{ key, label }, ...]  // built-in keys: summary, daily, weekly, absences
     panes: { key: element }       // extra tabs (e.g. Leads, Trends) - the element is moved in
     api:   { summary, weekly, impact, dayLog, pto }  // endpoints (defaults: Calvin's)
     onShow(key)                   // called when a tab is opened
   }) */

let FS_API = { summary: "/api/fabshop-summary", weekly: "/api/fabshop-weekly", impact: "/api/fabshop-impact",
               dayLog: "/api/fabshop-day-log", pto: "/api/fabshop-pto" };

// ---------- Fab Shop: Summary (line graph) ----------
let fwChart = null;
let fwRange = "week";
let fwWeek = null;
let fwDays = [];
let fwFilter = "all";
let fwOpen = {};   // date -> true when an older/collapsed day is expanded
let fwFull = {};   // date -> true when "View full tab" is open


async function loadFabshopSummary(range) {
  fwRange = range;
  document.querySelectorAll("#fw-range .fw-chip").forEach(b => b.classList.toggle("active", b.getAttribute("data-range") === range));
  const statusEl = document.getElementById("fabshop-status");
  try {
    const res = await fetch(FS_API.summary + "?range=" + encodeURIComponent(range));
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || "Failed to load");
    if (!data.has_data) {
      statusEl.textContent = "No Fab Shop data yet";
      document.getElementById("fw-stats").innerHTML = "";
      document.getElementById("fw-gaps").innerHTML = "";
      document.getElementById("fw-explain").textContent = "Nothing on file yet.";
      if (fwChart) { fwChart.destroy(); fwChart = null; }
      return;
    }
    statusEl.textContent = "Data on file through " + fwDay(data.last_data_date, true);
    renderFabshopSummary(data);
  } catch (err) {
    statusEl.textContent = "Could not load Fab Shop summary";
    document.getElementById("fw-explain").textContent = err.message;
  }
}

function renderFabshopSummary(data) {
  const isWeek = data.range === "week";
  document.getElementById("fw-chart-title").textContent = isWeek ? "Value of work completed, by day" : "Value of work completed, by week";
  const css = getComputedStyle(document.documentElement);
  const green = css.getPropertyValue("--green-dark").trim() || "#4d7a2f";
  const grid = css.getPropertyValue("--line").trim() || "#e2e3dc";
  const soft = css.getPropertyValue("--ink-soft").trim() || "#5b5f56";

  if (fwChart) fwChart.destroy();
  fwChart = new Chart(document.getElementById("fw-chart"), {
    type: "line",
    data: {
      labels: data.points.map(p => p.label),
      datasets: [{
        data: data.points.map(p => p.value),
        borderColor: green, backgroundColor: green, borderWidth: 3,
        pointRadius: isWeek ? 5 : 3, pointHoverRadius: 7, tension: 0.25,
        spanGaps: false, // a missing day/week is a break in the line, never a zero
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      // Tap anywhere above a day (or week) to open it - no need to hit the dot exactly
      interaction: { mode: "index", intersect: false },
      onClick: (evt, _els, chart) => {
        const hit = chart.getElementsAtEventForMode(evt, "index", { intersect: false }, true);
        if (hit.length) fsOpenPanel(data.points[hit[0].index], data.range);
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => c.parsed.y == null ? (data.points[c.dataIndex].pending ? "No dollar value" : "No data") : fwMoney(c.parsed.y) } },
      },
      scales: {
        x: { ticks: { color: soft, maxRotation: 0, autoSkip: true, maxTicksLimit: isWeek ? 5 : 7 }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { color: soft, callback: v => "$" + Number(v).toLocaleString() }, grid: { color: grid } },
      },
    },
  });

  const unit = isWeek ? "day" : "week";
  document.getElementById("fw-stats").innerHTML = `
    <div class="fw-box"><div class="fw-tile-lbl">${isWeek ? "Value this week" : "Value, 6 months"}</div><div class="fw-tile-val">${fwMoney(data.total_value)}</div></div>
    <div class="fw-box"><div class="fw-tile-lbl">Average per ${unit}</div><div class="fw-tile-val">${data.average_value == null ? "—" : fwMoney(data.average_value)}</div></div>`;

  document.getElementById("fw-gaps").innerHTML = (data.gaps || []).map(g =>
    `<div class="fw-gap">No data on file for ${g.weeks} weeks (${fwDay(g.from, false).replace(/^\w+ /, "")} to ${fwDay(g.to, false).replace(/^\w+ /, "")}). The line breaks there; it does not mean $0.</div>`
  ).join("");

  fsSummary = data;
  document.getElementById("fs-hint").textContent = isWeek
    ? "Tap a point (or a day below) to see that day's log and value."
    : "Tap a point to see that week's logs and value.";
  document.getElementById("fs-daychips").innerHTML = isWeek ? data.points.map((p, i) =>
    `<button class="fw-chip" data-fs-point="${i}">${fwEsc(p.label)} · ${p.value == null ? (p.pending ? "no $" : "—") : fwMoney(p.value)}</button>`).join("") : "";
  if (fsPanelKey && !data.points.some(p => p.key === fsPanelKey)) fsClosePanel();

  document.getElementById("fw-explain").textContent = isWeek
    ? "Each point is one day's estimated value of work completed, from the Daily Tab. Days with no tab on file, or with no dollar value written, are left blank."
    : "Each point is one week's estimated value, starting from earlier history and the Daily Tab. Earlier history is still being added, so gaps show where no data is on file yet. Days with no dollar value are left out, so a week with several can read low.";
}


// ---------- Fab Shop: what a chart point holds (day panel) ----------
let fsSummary = null;
let fsPanelKey = null;


function fsClosePanel() {
  fsPanelKey = null;
  document.getElementById("fs-panel-slot").innerHTML = "";
  document.querySelectorAll("#fs-daychips .fw-chip").forEach(c => c.classList.remove("active"));
}

function fsAddDays(iso, n) {
  const d = new Date(iso + "T12:00:00");
  d.setDate(d.getDate() + n);
  return fsIso(d);
}
function fsIso(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// Turns one day from /api/fabshop-day-log into cards for renderLogItems().
function fsDayEntries(day) {
  const out = [];
  const t = day.tab;
  if (t) {
    const lines = [];
    if (t.description) lines.push(t.description);
    if (t.problems) lines.push("Problems: " + t.problems);
    if (t.new_leads) lines.push("New lead: " + t.new_leads);
    const pills = []
      .concat(t.people.map(p => `<span class="fw-pill wo" style="background:var(--page);color:var(--ink);">${fwEsc(p.name)} ${fwEsc(p.total_hm)}</span>`))
      .concat(t.wo_tags.map(w => `<span class="fw-pill wo">${fwEsc(w)}</span>`))
      .concat(t.elsewhere.map(x => `<span class="fw-pill ${x.kind === "mci" ? "mci" : "other"}">→ ${fwEsc(x.label)}</span>`))
      .concat((t.absences || []).filter(a => a.name !== "Entire fab shop").map(a =>
        `<span class="fw-pill ${a.type === "left_early" ? "early" : "out"}">${fwEsc(a.name)} ${a.type === "left_early" ? "left early" : "out"}${a.reason && a.reason !== "not listed" ? " (" + fwEsc(a.reason) + ")" : ""}</span>`));
    // Where the tab sheet came from: the online form, a paper sheet typed in, or the old workbook upload
    const by = t.completed_by ? " · " + t.completed_by : "";
    const origin = t.source === "excel" ? { title: "Uploaded tab sheet (workbook)", tag: "Uploaded" }
      : t.source === "import" ? { title: "Paper tab sheet" + by, tag: "Paper sheet" }
      : { title: "Daily Tab" + by, tag: "Online form" };
    out.push({
      title: origin.title,
      subtitle: `${fwDay(day.date, true)} · ${t.total_hm} hrs · Value ${fwValueLabel(t)}`,
      summary: lines.join("\n\n"),
      tagClass: "ok", tagText: origin.tag,
      extraHtml: (t.shop_closed ? '<div class="fw-closed" style="margin-top:8px;">Entire fab shop out</div>' : "")
        + (pills.length ? `<div class="fw-pills" style="margin-top:8px;">${pills.join("")}</div>` : ""),
      pdfPath: null, photoPaths: [], notionUrl: null,
      manualNotes: (t.notes || []).map(n => ({ author: n.author, text: n.text })),
    });
  }
  (day.logs || []).forEach(l => {
    const mgmt = l.job_or_wo === "Daily Management Log";
    out.push({
      title: l.name + (mgmt ? "" : " | Fab Shop Daily Log"),
      subtitle: `${fwDay(l.date, true)} · ${l.photo_count || 0} photo(s)`,
      summary: l.summary || "",
      tagClass: "ok", tagText: mgmt ? "Management log" : "Typeform log",
      pdfPath: l.dropbox_pdf_path || null, photoPaths: l.photo_paths || [], notionUrl: null,
      manualNotes: l.manual_notes || [],
    });
  });
  return out;
}

function fsDayHeadline(day) {
  const t = day.tab;
  const bits = [];
  if (t) { bits.push("Value " + fwValueLabel(t)); bits.push(t.total_hm + " hrs"); }
  else bits.push("No Daily Tab on file");
  if (day.logs.length) bits.push(day.logs.length + " Typeform log" + (day.logs.length === 1 ? "" : "s"));
  if (day.holiday) bits.push("Holiday: " + day.holiday);
  return bits.join(" · ");
}

async function fsOpenPanel(point, range) {
  if (!point) return;
  const isWeek = range === "week";
  const start = point.key, end = isWeek ? point.key : fsAddDays(point.key, 6);
  fsPanelKey = point.key;
  document.querySelectorAll("#fs-daychips .fw-chip").forEach(c =>
    c.classList.toggle("active", fsSummary && fsSummary.points[+c.dataset.fsPoint].key === point.key));
  const slot = document.getElementById("fs-panel-slot");
  const title = isWeek
    ? fwDay(start, true)
    : "Week of " + new Date(start + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });
  slot.innerHTML = `<div class="cal-panel fs-panel" id="fs-panel" role="region" aria-label="Details for ${fwEsc(title)}">
    <button class="cal-close" aria-label="Close">✕</button><h3>${fwEsc(title)}</h3><div class="cal-muted">Loading…</div></div>`;
  slot.querySelector(".cal-close").addEventListener("click", fsClosePanel);
  slot.scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const res = await fetch(`${FS_API.dayLog}?start=${start}&end=${end}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || "Failed to load");
    if (fsPanelKey !== point.key) return; // another point was tapped meanwhile
    const value = point.value == null ? (point.pending ? "No dollar value written" : "Nothing on file") : fwMoney(point.value);
    const days = data.days || [];
    const sections = days.map(day => {
      const entries = fsDayEntries(day);
      const body = entries.map(r => {
        const idx = logFileRegister(r);
        return `<div class="fs-log">
          <div class="row" style="display:flex;justify-content:space-between;gap:8px;align-items:baseline;">
            <strong style="font-size:14px;">${fwEsc(r.title)}</strong><span class="tag ok">${fwEsc(r.tagText)}</span></div>
          ${r.extraHtml || ""}
          ${r.summary ? `<p class="fw-text" style="margin-top:6px;">${fwEsc(fwSnippet(r.summary).text)}</p>` : '<p class="cal-muted" style="margin:6px 0 0;">No details captured.</p>'}
          ${r.photoPaths.length ? `<div class="log-thumbs" data-fs-thumbs="${idx}"></div>` : ""}
          <div class="lf-links"><a href="#" data-preview="${idx}">Preview</a>${r.pdfPath ? `<a href="#" data-open-path="${logFileAttr(r.pdfPath)}" data-open-mode="view">Open PDF ↗</a>` : ""}</div>
        </div>`;
      }).join("");
      return `<div class="cal-sec"><div class="cal-lbl">${fwEsc(isWeek ? "On file" : fwDay(day.date, true))} · ${fwEsc(fsDayHeadline(day))}</div>${body}</div>`;
    }).join("");
    const panel = document.getElementById("fs-panel");
    panel.innerHTML = `<button class="cal-close" aria-label="Close">✕</button>
      <h3>${fwEsc(title)}</h3>
      <div class="cal-lbl" style="margin:8px 0 0;">Estimated value${isWeek ? "" : " (week)"}</div>
      <div class="fs-val">${fwEsc(value)}</div>
      ${data.logs_error ? `<div class="cal-muted">Typeform logs couldn't load: ${fwEsc(data.logs_error)}</div>` : ""}
      ${sections || `<div class="cal-sec"><div class="cal-muted">No Daily Tab or Typeform log on file for ${isWeek ? "this day" : "this week"}.</div></div>`}
      <div class="fs-panel-actions"><button class="fw-chip" id="fs-panel-open">Open in Daily Log</button></div>`;
    panel.querySelector(".cal-close").addEventListener("click", fsClosePanel);
    panel.querySelector("#fs-panel-open").addEventListener("click", () => {
      document.getElementById("fs-date-from").value = start;
      document.getElementById("fs-date-to").value = end;
      showFabshopTab("daily");
      applyFsDateFilter();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    panel.querySelectorAll("[data-fs-thumbs]").forEach(t => fillLogThumbs(t, LOG_PREVIEWS[+t.dataset.fsThumbs].photoPaths));
  } catch (err) {
    const panel = document.getElementById("fs-panel");
    if (panel) panel.querySelector(".cal-muted").textContent = "Could not load this day: " + err.message;
  }
}

// ---------- Fab Shop: Daily Log tab ----------
let fsDateFilter = { start: null, end: null };

function fsRangeLabel() {
  if (!fsDateFilter.start) return "the last 7 days";
  const f = d => fwDay(d, true);
  return fsDateFilter.start === fsDateFilter.end ? f(fsDateFilter.start) : `${f(fsDateFilter.start)} – ${f(fsDateFilter.end)}`;
}

function applyFsDateFilter() {
  let start = document.getElementById("fs-date-from").value;
  let end = document.getElementById("fs-date-to").value;
  if (!start && !end) { fsDateFilter = { start: null, end: null }; loadFabshopDayLog(); return; }
  start = start || end; end = end || start;
  if (start > end) [start, end] = [end, start];
  if ((new Date(end) - new Date(start)) / 86400000 > 92) {
    document.getElementById("fs-date-showing").textContent = "Pick a range of 3 months or less.";
    return;
  }
  document.getElementById("fs-date-from").value = start;
  document.getElementById("fs-date-to").value = end;
  fsDateFilter = { start, end };
  loadFabshopDayLog();
}

async function loadFabshopDayLog() {
  const el = document.getElementById("fs-daylog");
  const showing = document.getElementById("fs-date-showing");
  showing.textContent = "Showing " + fsRangeLabel();
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const q = fsDateFilter.start ? `start=${fsDateFilter.start}&end=${fsDateFilter.end}` : "";
    const res = await fetch(FS_API.dayLog + (q ? "?" + q : ""));
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || "Failed to load");
    const days = data.days || [];
    showing.textContent = `Showing ${fsRangeLabel()} · ${data.tab_count} Daily Tab(s), ${data.log_count} Typeform log(s)`
      + (data.logs_error ? " · Typeform logs couldn't load" : "");
    if (!days.length) { el.innerHTML = `<div class="empty">Nothing logged for ${fsRangeLabel()}.</div>`; return; }
    el.innerHTML = days.map(d => `<div class="fs-day">
      <div class="fs-dayhead"><strong>${fwEsc(fwDay(d.date, true))}</strong><span>${fwEsc(fsDayHeadline(d))}</span></div>
      <div id="fs-day-${d.date}"></div></div>`).join("");
    days.forEach(d => renderLogItems(document.getElementById("fs-day-" + d.date), fsDayEntries(d), "Nothing on file."));
  } catch (err) {
    el.innerHTML = '<div class="empty">Could not load the Fab Shop log: ' + fwEsc(err.message) + '</div>';
  }
}

// ---------- Fab Shop: time off requests ----------
function fsDateRange(start, end) {
  const f = iso => new Date(iso + "T12:00:00").toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  return start === end ? f(start) : `${f(start)} – ${f(end)}`;
}
async function loadFabshopPto() {
  const el = document.getElementById("fs-pto");
  try {
    const res = await fetch(FS_API.pto);
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || "Failed to load");
    const items = data.items || [];
    const row = p => `<div class="fs-pto-row"><span><span class="pill">✈ ${fwEsc(p.name)}</span> ${fwEsc(p.label)}${p.hours ? " · " + fwEsc(p.hours) + " hrs" : ""}</span>
      <span class="sub">${fwEsc(fsDateRange(p.start, p.end))}${p.url ? ` · <a href="${fwEsc(p.url)}" target="_blank" rel="noopener" style="color:var(--green-dark);">Notion ↗</a>` : ""}</span></div>`;
    const group = (label, list) => list.length ? `<div class="fs-pto-group"><h3>${label}</h3>${list.map(row).join("")}</div>` : "";
    const now = items.filter(i => i.when === "now"), up = items.filter(i => i.when === "upcoming"),
      past = items.filter(i => i.when === "past").reverse();
    el.innerHTML = (group("Out today", now) + group("Coming up (next 90 days)", up) + group("Recent (last 30 days)", past))
      || '<div class="empty">No time off requested by the fab shop crew in the last 30 or next 90 days.</div>';
    el.insertAdjacentHTML("beforeend", `<p class="fw-explain">From the PTO request forms in Notion, matched by first name to the fab shop crew list (${fwEsc((data.roster || []).join(", "))}).</p>`);
  } catch (err) {
    el.innerHTML = '<div class="empty">Could not load time off requests: ' + fwEsc(err.message) + '</div>';
  }
}

// ---------- Fab Shop: holiday + absence impact ----------
async function loadFabshopImpact() {
  const el = document.getElementById("fw-impact");
  try {
    const res = await fetch(FS_API.impact);
    const d = await res.json();
    if (!res.ok || !d.has_data) { el.innerHTML = ""; return; }
    const a = d.absences, h = d.holidays, c = d.shop_closed;
    const rows = a.by_reason.map(r => `<tr><td>${fwEsc(r.reason)}</td><td>${r.days}</td><td>${r.est_hours}</td></tr>`).join("");
    const ppl = a.by_person.map(p => `<tr><td>${fwEsc(p.name)}</td><td>${p.out}</td><td>${p.left_early}</td><td>${p.est_hours}</td></tr>`).join("");
    const closedTxt = c.days.map(x => `${fwDay(x.date, true)}${x.note ? " (“" + fwEsc(x.note) + "”)" : ""}`).join("; ");
    const hol = h.days.map(x => `<div class="fw-offday holiday"><span>${fwDay(x.date, true)} · ${fwEsc(x.name)}</span><span>${x.worked ? "worked" : "closed"}</span></div>`).join("");
    const recent = d.recent_events.slice(0, 12).map(e =>
      `<div class="fw-offday"><span>${fwDay(e.date, true)} · ${fwEsc(e.name)} ${e.type === "left_early" ? "left early" : "out"}</span><span>${fwEsc(e.reason || "no reason written")}</span></div>`).join("");
    el.innerHTML = `
    <p class="section-heading" style="margin-top:20px;">Holidays &amp; absences</p>
    <p class="section-sub">Since ${fwDay(d.coverage_from, true)}, when daily hours by person began. Hours and dollars lost are estimates.</p>
    <div class="fw-stats">
      <div class="fw-box"><div class="fw-tile-lbl">Est. hours lost</div><div class="fw-tile-val">${Math.round(d.total_est_hours).toLocaleString()}</div></div>
      <div class="fw-box"><div class="fw-tile-lbl">Est. value of that time</div><div class="fw-tile-val">${fwMoney(d.total_est_value)}</div></div>
    </div>
    <div class="fw-box fw-impact"><h3>Entire fab shop out</h3>
      <p class="fw-text">${c.count} day${c.count === 1 ? "" : "s"}: ${closedTxt || "none"}. About ${c.est_hours} hours, ${fwMoney(c.est_value)}.</p></div>
    <div class="fw-box fw-impact"><h3>Absences</h3>
      <p class="fw-text">${a.person_days_out} person-days out and ${a.person_days_left_early} left early or short. About ${a.est_hours} hours, ${fwMoney(a.est_value)}.</p>
      <table><tr><th>Reason</th><th>Days</th><th>Est. hrs</th></tr>${rows}</table>
      <table style="margin-top:12px;"><tr><th>Person</th><th>Out</th><th>Early</th><th>Est. hrs</th></tr>${ppl}</table></div>
    <div class="fw-box fw-impact"><h3>Holidays</h3>
      <p class="fw-text">${h.count} holiday weekdays closed: about ${h.est_hours} crew hours, ${fwMoney(h.est_value)}.</p>
      <div style="display:grid;gap:6px;">${hol}</div></div>
    <div class="fw-box fw-impact"><h3>Most recent</h3><div style="display:grid;gap:6px;">${recent}</div></div>
    <p class="fw-explain">${fwEsc(d.estimate_note)} Caleb W. is part-time and is not counted as absent. ${d.no_tab_days.count} other weekdays have no sheet on file; those are not counted as absences.</p>`;
  } catch (err) { el.innerHTML = ""; }
}

// ---------- Fab Shop: Weekly Breakdown ----------
async function loadFabshopWeekly(week) {
  const el = document.getElementById("fabshop-weekly-display");
  el.innerHTML = '<div class="empty">Loading…</div>';
  try {
    const res = await fetch(FS_API.weekly + (week ? "?week=" + encodeURIComponent(week) : ""));
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || "Failed to load");
    if (!data.has_data) { el.innerHTML = '<div class="empty">No Fab Shop days on file yet.</div>'; return; }
    fwWeek = data; fwDays = data.days; fwFilter = "all"; fwOpen = {}; fwFull = {};
    renderFabshopWeekly();
  } catch (err) {
    el.innerHTML = '<div class="empty">Could not load the weekly breakdown: ' + fwEsc(err.message) + '</div>';
  }
}

function fwSnippet(text) {
  const t = (text || "").trim();
  if (t.length <= 130) return { text: t, cut: false };
  const cut = t.slice(0, 130).replace(/\s+\S*$/, "");
  return { text: cut + "…", cut: true };
}

function fwCategoryBar(day, cls) {
  const sp = day.split;
  if (!(sp.billable + sp.non_billable + sp.mgt_design > 0)) return "";
  return `<div class="fw-bar ${cls || ""}" role="img" aria-label="Billable ${day.billable_hm}, non-billable ${day.non_billable_hm}, management and design ${day.mgt_design_hm}">
    <div class="fw-seg-b" style="width:${sp.billable}%"></div><div class="fw-seg-n" style="width:${sp.non_billable}%"></div><div class="fw-seg-m" style="width:${sp.mgt_design}%"></div></div>`;
}

function fwValueLabel(day) {
  if (!day.value_pending) return fwMoney(day.value);
  return day.value_note ? "“" + day.value_note + "”" : "not written";
}

function fwDayCard(day) {
  const pills = []
    .concat(day.wo_tags.map(t => `<span class="fw-pill wo">${fwEsc(t)}</span>`))
    .concat(day.elsewhere.map(e => `<span class="fw-pill ${e.kind === "mci" ? "mci" : "other"}">→ ${fwEsc(e.label)}</span>`))
    .concat(day.new_leads ? ['<span class="fw-pill lead">New lead</span>'] : [])
    .concat(day.holiday ? [`<span class="fw-pill holiday">Holiday: ${fwEsc(day.holiday)}</span>`] : [])
    .concat((day.absences || []).filter(a => a.name !== "Entire fab shop").map(a =>
      `<span class="fw-pill ${a.type === "left_early" ? "early" : "out"}">${fwEsc(a.name)} ${a.type === "left_early" ? "left early" : "out"}${a.reason && a.reason !== "not listed" ? " (" + fwEsc(a.reason) + ")" : ""}</span>`))
    .concat(day.value_pending && day.value_note ? [`<span class="fw-pill pending">Written note: ${fwEsc(day.value_note)}</span>`] : []);
  const full = !!fwFull[day.date];
  const snip = fwSnippet(day.description);
  const people = day.people.map(p => `${fwEsc(p.name)} ${p.total_hm}`).join(" · ");
  const woText = day.wo_tags.length ? day.wo_tags.join(", ") : "";
  const notes = (day.notes || []).map(n => `<div class="fw-notes">${fwEsc(n.author)}: ${fwEsc(n.text)}</div>`).join("");
  const excelOnly = day.source === "excel";
  return `
  <div class="fw-day" data-date="${day.date}">
    <div class="fw-day-top"><div class="fw-day-name">${fwDay(day.date, true)}</div><div class="fw-day-total"><strong>${day.total_hm}</strong> total</div></div>
    ${day.shop_closed ? '<div class="fw-closed">Entire fab shop out</div>' : ""}
    ${people ? `<div class="fw-people">${people}</div>` : ""}
    ${day.shop_closed ? "" : fwCategoryBar(day, "sm")}
    ${full ? `<div class="fw-people">Billable ${day.billable_hm} · Non-billable ${day.non_billable_hm} · Mgt/design ${day.mgt_design_hm}</div>` : ""}
    ${pills.length ? `<div class="fw-pills">${pills.join("")}</div>` : ""}
    ${day.description ? `<p class="fw-text">${fwEsc(full ? day.description : snip.text)}</p>` : ""}
    ${day.no_notes ? '<p class="fw-text" style="color:var(--ink-soft);">No notes available</p>' : ""}
    ${(snip.cut || day.problems || day.notes.length || day.new_leads.length > 0) && !excelOnly ? `<button class="fw-more" data-fw-full="${day.date}">${full ? "Hide full tab" : "View full tab"}</button>` : ""}
    ${day.new_leads ? `<div class="fw-leadbox"><strong>Lead:</strong> ${fwEsc(day.new_leads)}</div>` : ""}
    ${full && day.problems ? `<div class="fw-probbox"><strong>Problems:</strong> ${fwEsc(day.problems)}</div>` : ""}
    ${full ? notes : ""}
    <div class="fw-foot">
      <span>Value <strong>${fwEsc(fwValueLabel(day))}</strong>${woText ? ` <span class="muted">· ${fwEsc(woText)}</span>` : ""}</span>
      ${day.completed_by ? `<span class="muted">by ${fwEsc(day.completed_by)}</span>` : ""}
    </div>
  </div>`;
}

function fwCollapsedRow(day) {
  const bits = [day.total_hm, fwValueLabel(day)];
  if (day.wo_tags.length) bits.push(day.wo_tags.join(", "));
  return `<button class="fw-row" data-fw-open="${day.date}"><span><strong>${fwDay(day.date, true)}</strong> <span class="muted">· ${fwEsc(bits.join(" · "))}</span></span><span class="chev">›</span></button>`;
}

function renderFabshopWeekly() {
  const el = document.getElementById("fabshop-weekly-display");
  const w = fwWeek, sm = w.summary, sp = sm.split;
  const filters = {
    all: d => true,
    elsewhere: d => d.elsewhere.length > 0,
    leads: d => !!d.new_leads,
    pending: d => d.value_pending,
  };
  const labels = { all: "All days", elsewhere: "Hours charged elsewhere", leads: "New leads", pending: "No dollar value" };
  const shown = fwDays.filter(filters[fwFilter]);

  const items = shown.map((d, i) => ({ date: d.date, html: (i < 3 || fwOpen[d.date]) ? fwDayCard(d) : fwCollapsedRow(d) }));
  if (fwFilter === "all") {
    (w.off_days || []).forEach(o => items.push({ date: o.date, html:
      `<div class="fw-offday ${o.holiday ? "holiday" : ""}"><span><strong>${fwDay(o.date, true)}</strong></span><span>${fwEsc(o.label)}</span></div>` }));
    items.sort((x, y) => y.date.localeCompare(x.date));
  }
  const dayHtml = items.map(x => x.html).join("") || '<div class="empty">No days match this filter.</div>';

  el.innerHTML = `
    <div class="fw-weeknav">
      <button data-fw-week="${w.prev_week || ""}" ${w.prev_week ? "" : "disabled"} aria-label="Previous week">‹</button>
      <div class="lbl">Week of ${fwEsc(w.week_label)}</div>
      <button data-fw-week="${w.next_week || ""}" ${w.next_week ? "" : "disabled"} aria-label="Next week">›</button>
    </div>
    <div class="fw-tiles">
      <div class="fw-box"><div class="fw-tile-lbl">Hours logged</div><div class="fw-tile-val">${sm.total_hm}</div></div>
      <div class="fw-box"><div class="fw-tile-lbl">Value priced</div><div class="fw-tile-val">${fwMoney(sm.value)}</div></div>
    </div>
    <div class="fw-box fw-split">
      <h3>Where the week's hours went</h3>
      <div class="fw-bar" role="img" aria-label="Billable ${sm.billable_hm}, non-billable ${sm.non_billable_hm}, management and design ${sm.mgt_design_hm}">
        <div class="fw-seg-b" style="width:${sp.billable}%"></div><div class="fw-seg-n" style="width:${sp.non_billable}%"></div><div class="fw-seg-m" style="width:${sp.mgt_design}%"></div>
      </div>
      <div class="fw-legend">
        <div><span><span class="fw-dot" style="background:#3f6a24"></span>Billable</span><span><strong>${sm.billable_hm}</strong> · ${sp.billable}%</span></div>
        <div><span><span class="fw-dot" style="background:#d9d4b0;border:1px solid #b5ae7c"></span>Non-billable</span><span><strong>${sm.non_billable_hm}</strong> · ${sp.non_billable}%</span></div>
        <div><span><span class="fw-dot" style="background:#6f93ab"></span>Management / design</span><span><strong>${sm.mgt_design_hm}</strong> · ${sp.mgt_design}%</span></div>
      </div>
    </div>
    <div class="fw-chips" id="fw-filters">
      ${Object.keys(labels).map(k => `<button class="fw-chip ${fwFilter === k ? "active" : ""}" data-fw-filter="${k}">${labels[k]}</button>`).join("")}
    </div>
    ${dayHtml}
    <div class="fw-foot-note">Hours are not split between jobs. Tags come from the WO numbers on the value line and from places named in the notes (MCI, gravel pit). Days without a dollar value show what was written beside the value line, and are left out of "Value priced". Days with no tab on file are shown as holidays or "No tab on file". Caleb W. is part-time.</div>`;
}



let FABSHOP_TAB_PANES = { summary: "fabshop-summary-display", daily: "fabshop-daily-display", weekly: "fabshop-weekly-display",
                          absences: "fabshop-absences-display" };
let FS_ON_SHOW = null;
function showFabshopTab(tab) {
  Object.entries(FABSHOP_TAB_PANES).forEach(([t, id]) => {
    document.getElementById(id).style.display = (t === tab) ? "block" : "none";
  });
  document.querySelectorAll("#fabshop-tabs .lookup-tab").forEach(el => {
    const on = el.getAttribute("data-tab") === tab;
    el.classList.toggle("active", on);
    el.setAttribute("aria-selected", on ? "true" : "false");
  });
  if (tab === "summary" && fwChart) fwChart.resize();
  if (FS_ON_SHOW) FS_ON_SHOW(tab);
}


function fabshopViewsHtml(tabList) {
  const tabsHtml = tabList.map((t, i) =>
    `<div class="lookup-tab${i === 0 ? " active" : ""}" data-tab="${t.key}" role="tab" tabindex="0" aria-selected="${i === 0}">${fwEsc(t.label)}</div>`).join("");
  return `<p class="section-sub" id="fabshop-status">Loading…</p>
    <div class="lookup-tabs" id="fabshop-tabs" role="tablist">${tabsHtml}</div>
    <div id="fabshop-summary-display">
      <div class="fw-range" id="fw-range">
        <button class="fw-chip active" data-range="week">Past week</button>
        <button class="fw-chip" data-range="6m">6 months</button>
      </div>
      <div class="fw-box">
        <div class="fw-tile-lbl" id="fw-chart-title">Value of work completed</div>
        <div class="fw-chartwrap"><canvas id="fw-chart" role="img" aria-label="Line graph of the value of work completed"></canvas></div>
        <p class="fs-hint" id="fs-hint">Tap a point to see that day's log and value.</p>
        <div class="fs-daychips" id="fs-daychips"></div>
      </div>
      <div id="fs-panel-slot"></div>
      <div class="fw-stats" id="fw-stats"></div>
      <div id="fw-gaps"></div>
      <p class="fw-explain" id="fw-explain"></p>
    </div>

    <div id="fabshop-daily-display" style="display:none;">
      <p class="section-heading">Daily Log</p>
      <p class="section-sub">The shop's Daily Tab (online form) and its Typeform logs, by day.</p>
      <div class="log-date-filter">
        <label>From <input type="date" id="fs-date-from"></label>
        <label>To <input type="date" id="fs-date-to"></label>
        <button class="fw-chip primary" id="fs-date-apply">Show</button>
        <button class="fw-chip" id="fs-date-yesterday">Yesterday</button>
        <button class="fw-chip" id="fs-date-reset">Last 7 days</button>
      </div>
      <p class="log-date-showing" id="fs-date-showing" aria-live="polite">Showing the last 7 days</p>
      <div id="fs-daylog"></div>
    </div>

    <div id="fabshop-absences-display" style="display:none;">
      <p class="section-heading">Time off requests</p>
      <p class="section-sub">PTO, time off and sick time requested by the fab shop crew.</p>
      <div id="fs-pto"><div class="empty">Loading…</div></div>
      <div id="fw-impact"></div>
    </div>

    <div id="fabshop-weekly-display" style="display:none;"></div>
`;
}

// Redraws the value graph in the current theme's colors (call after a light/dark switch).
function fabshopRecolor() { if (fsSummary) renderFabshopSummary(fsSummary); }

function mountFabshopViews(root, cfg) {
  cfg = cfg || {};
  FS_API = Object.assign({}, FS_API, cfg.api || {});
  FS_ON_SHOW = cfg.onShow || null;
  const tabList = cfg.tabs || [{ key: "summary", label: "Overview" }, { key: "daily", label: "Daily Log" },
    { key: "weekly", label: "Weekly Value" }, { key: "absences", label: "Absences & PTO" }];
  root.innerHTML = fabshopViewsHtml(tabList);
  Object.entries(cfg.panes || {}).forEach(([key, el]) => {
    if (!el.id) el.id = "fabshop-pane-" + key;
    root.appendChild(el);
    FABSHOP_TAB_PANES[key] = el.id;
  });
  ["daily", "weekly", "absences"].forEach(k => {
    if (!tabList.some(t => t.key === k)) document.getElementById(FABSHOP_TAB_PANES[k]).remove();
  });
  Object.keys(FABSHOP_TAB_PANES).forEach(k => { if (!tabList.some(t => t.key === k)) delete FABSHOP_TAB_PANES[k]; });

  document.querySelectorAll("#fw-range .fw-chip").forEach(btn => {
    btn.addEventListener("click", () => loadFabshopSummary(btn.getAttribute("data-range")));
  });

  document.getElementById("fs-daychips").addEventListener("click", e => {
    const b = e.target.closest("[data-fs-point]");
    if (b && fsSummary) fsOpenPanel(fsSummary.points[+b.dataset.fsPoint], fsSummary.range);
  });

  document.getElementById("fs-date-apply").addEventListener("click", applyFsDateFilter);
  document.getElementById("fs-date-yesterday").addEventListener("click", () => {
    const y = fsAddDays(fsIso(new Date()), -1);
    document.getElementById("fs-date-from").value = y;
    document.getElementById("fs-date-to").value = y;
    applyFsDateFilter();
  });
  document.getElementById("fs-date-reset").addEventListener("click", () => {
    document.getElementById("fs-date-from").value = "";
    document.getElementById("fs-date-to").value = "";
    applyFsDateFilter();
  });
  ["fs-date-from", "fs-date-to"].forEach(id =>
    document.getElementById(id).addEventListener("keydown", e => { if (e.key === "Enter") applyFsDateFilter(); }));

  document.getElementById("fabshop-weekly-display").addEventListener("click", (e) => {
    const t = e.target.closest("[data-fw-week],[data-fw-filter],[data-fw-open],[data-fw-full]");
    if (!t) return;
    if (t.hasAttribute("data-fw-week")) { const wk = t.getAttribute("data-fw-week"); if (wk) loadFabshopWeekly(wk); return; }
    if (t.hasAttribute("data-fw-filter")) { fwFilter = t.getAttribute("data-fw-filter"); renderFabshopWeekly(); return; }
    if (t.hasAttribute("data-fw-open")) { fwOpen[t.getAttribute("data-fw-open")] = true; renderFabshopWeekly(); return; }
    if (t.hasAttribute("data-fw-full")) { const d = t.getAttribute("data-fw-full"); fwFull[d] = !fwFull[d]; renderFabshopWeekly(); }
  });

  document.querySelectorAll("#fabshop-tabs .lookup-tab").forEach(el => {
    el.addEventListener("click", () => showFabshopTab(el.getAttribute("data-tab")));
    el.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); showFabshopTab(el.getAttribute("data-tab")); }
    });
  });


  showFabshopTab(tabList[0].key);
  loadFabshopSummary("week");
  if (FABSHOP_TAB_PANES.absences) { loadFabshopImpact(); loadFabshopPto(); }
  if (FABSHOP_TAB_PANES.weekly) loadFabshopWeekly(null);
  if (FABSHOP_TAB_PANES.daily) loadFabshopDayLog();
}
