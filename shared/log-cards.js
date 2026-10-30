/* Shared by Calvin's dashboard and Jerron's Fab Shop portal:
   small formatting helpers, log cards (renderLogItems), photo thumbnails,
   and opening log files / the Preview window.
   Set window.LOG_LINKS_URL before this loads to use a different link endpoint. */

function fwEsc(t) {
  return String(t == null ? "" : t).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fwMoney(n) { return "$" + Math.round(n || 0).toLocaleString(); }
function fwDay(dateStr, long) {
  const d = new Date(dateStr + "T12:00:00");
  return d.toLocaleDateString("en-US", long ? { weekday: "short", month: "short", day: "numeric" } : { weekday: "short", month: "numeric", day: "numeric" });
}


// Fills a .log-thumbs box with up to 6 photo thumbnails (+N tile for the rest).
async function fillLogThumbs(target, photoPaths) {
  const paths = (photoPaths || []).slice(0, 6);
  if (!paths.length) return;
  try {
    const links = await logFileLinks(paths, "temp");
    target.innerHTML = paths.map((p, n) => links[p] && links[p].url
      ? `<img src="${links[p].url}" alt="Site photo ${n + 1}" loading="lazy" data-open-path="${logFileAttr(p)}" data-open-mode="view">`
      : "").join("")
      + (photoPaths.length > 6 ? `<span class="more-ph" data-open-path="${logFileAttr(photoPaths[6])}" data-open-mode="view">+${photoPaths.length - 6}</span>` : "");
  } catch (err) {
    target.innerHTML = `<span class="item-sub">Photos couldn't load (${fwEsc(err.message)}). Use the Photo links below.</span>`;
  }
}

function logSnippet(text, sentenceCount = 2) {
  if (!text) return "";
  // Split on sentence-ending punctuation, keep the punctuation with each piece
  const sentences = text.match(/[^.!?]+[.!?]+(\s|$)/g) || [text];
  const preview = sentences.slice(0, sentenceCount).join("").trim();
  return sentences.length > sentenceCount ? preview + "…" : preview;
}

// One card per log (Chipperfield daily reports and the Fab Shop Daily Log).
function renderLogItems(el, list, emptyMsg) {
  if (list.length === 0) {
    el.innerHTML = `<div class="empty">${emptyMsg}</div>`;
    return;
  }
  el.innerHTML = list.map((r, i) => `
    <div class="item">
      <div class="item-top">
        <div>
          <p class="item-title">${fwEsc(r.title)}</p>
          <p class="item-sub">${fwEsc(r.subtitle)}</p>
        </div>
        <span class="tag ${r.tagClass}">${fwEsc(r.tagText)}</span>
      </div>
      ${r.extraHtml || ""}
      ${r.summary
        ? `<p class="summary" id="summary-${el.id}-${i}">${fwEsc(logSnippet(r.summary))}</p>
           <a href="#" class="toggle-details" data-toggle-idx="${i}" data-expanded="false" style="font-size:12px;color:var(--green-dark);">View full report</a>`
        : `<p class="summary" style="color:var(--ink-soft);font-style:italic;">No details captured for this entry.</p>`}
      ${(r.photoPaths || []).length ? `<div class="log-thumbs" data-thumbs="${i}" aria-label="${r.photoPaths.length} site photo(s)">${r.photoPaths.slice(0, 6).map(() => '<span class="ph"></span>').join("")}</div>` : ""}
      ${logFileLinksHtml(r)}
      ${r.notionUrl ? `<p style="margin:6px 0 0;"><a href="${r.notionUrl}" target="_blank" rel="noopener" style="font-size:12px;color:var(--green-dark);">View in Notion ↗</a></p>` : ''}
      ${(r.manualNotes || []).map(n => `<div style="font-size:12px;padding:6px 0;border-top:1px solid var(--line);"><strong>${fwEsc(n.author)}:</strong> ${fwEsc(n.text)}</div>`).join("")}
      ${r.submittedBy !== undefined || r.footerLeft ? `<div class="lc-foot"><span>${r.footerLeft || ""}</span>
        ${r.submittedBy !== undefined ? `<span class="lc-by">Submitted by <strong>${fwEsc(r.submittedBy || "not recorded")}</strong>${r.footerNote ? " · " + fwEsc(r.footerNote) : ""}</span>` : ""}</div>` : ""}
    </div>
  `).join("");

  // Photo thumbnails load when the card scrolls into view (links are made on demand).
  const thumbObs = new IntersectionObserver(entries => entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    thumbObs.unobserve(entry.target);
    fillLogThumbs(entry.target, list[+entry.target.dataset.thumbs].photoPaths);
  }), { rootMargin: "200px" });
  el.querySelectorAll("[data-thumbs]").forEach(t => thumbObs.observe(t));

  el.querySelectorAll("[data-toggle-idx]").forEach(link => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const idx = parseInt(link.getAttribute("data-toggle-idx"), 10);
      const r = list[idx];
      const p = document.getElementById(`summary-${el.id}-${idx}`);
      const expanded = link.getAttribute("data-expanded") === "true";
      if (expanded) {
        p.textContent = logSnippet(r.summary);
        link.textContent = "View full report";
        link.setAttribute("data-expanded", "false");
      } else {
        p.textContent = r.summary;
        link.textContent = "Show less";
        link.setAttribute("data-expanded", "true");
      }
    });
  });
}


/* ---- Opening Daily Job Log files (PDFs, photos) on click ----
   Links are made when someone taps, not when the page loads. The file then
   comes straight from Dropbox in a new tab; nothing passes through Vercel. */
const LOG_PREVIEWS = [];
function logFileRegister(entry) { LOG_PREVIEWS.push(entry); return LOG_PREVIEWS.length - 1; }
function logFileAttr(path) { return encodeURIComponent(path); }

async function logFileLinks(paths, mode) {
  const res = await fetch(window.LOG_LINKS_URL || "/api/log-links", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths, mode })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.status !== "ok") throw new Error(data.message || ("Server error (" + res.status + ")"));
  return data.links || {};
}

async function openLogFile(path, mode) {
  // Open the tab right away (inside the tap) so phone pop-up blockers allow it.
  const w = window.open("", "_blank");
  if (w) { try { w.document.write("<p style='font-family:sans-serif;padding:24px;color:#555'>Opening file…</p>"); } catch (e) {} }
  try {
    const links = await logFileLinks([path], mode || "view");
    const link = links[path];
    if (!link || !link.url) throw new Error("No link returned");
    if (w) { w.location.href = link.url; } else { window.location.href = link.url; }
  } catch (err) {
    if (w) w.close();
    alert("Couldn't open that file right now (" + err.message + "). Please try again in a moment.");
  }
}

function closeLogPreview() { const o = document.getElementById("lf-overlay"); if (o) o.remove(); }

async function showLogPreview(idx) {
  const e = LOG_PREVIEWS[idx];
  if (!e) return;
  closeLogPreview();
  const overlay = document.createElement("div");
  overlay.id = "lf-overlay"; overlay.className = "lf-overlay";
  const modal = document.createElement("div"); modal.className = "lf-modal";
  const h = document.createElement("h3"); h.textContent = e.title || "";
  const sub = document.createElement("p"); sub.className = "lf-sub"; sub.textContent = e.subtitle || "";
  const txt = document.createElement("p"); txt.className = "lf-text";
  txt.textContent = e.summary || "No details captured for this entry.";
  const photos = document.createElement("div"); photos.className = "lf-photos";
  const actions = document.createElement("div"); actions.className = "lf-actions";
  const mk = (label, cls, fn) => { const b = document.createElement("button"); b.textContent = label; if (cls) b.className = cls; b.addEventListener("click", fn); actions.appendChild(b); };
  if (e.pdfPath) {
    mk("Open PDF", "primary", () => openLogFile(e.pdfPath, "view"));
    mk("Download", "", () => openLogFile(e.pdfPath, "download"));
  }
  mk("Close", "", closeLogPreview);
  modal.append(h, sub, txt, photos, actions);
  overlay.appendChild(modal);
  overlay.addEventListener("click", ev => { if (ev.target === overlay) closeLogPreview(); });
  document.body.appendChild(overlay);

  if ((e.photoPaths || []).length) {
    try {
      const links = await logFileLinks(e.photoPaths, "temp");
      e.photoPaths.forEach(p => {
        const l = links[p]; if (!l || !l.url) return;
        const img = document.createElement("img"); img.src = l.url; img.loading = "lazy"; img.alt = "Site photo";
        img.addEventListener("click", () => window.open(l.url, "_blank"));
        photos.appendChild(img);
      });
    } catch (err) { /* photos are optional in the preview */ }
  }
}


function logFileLinksHtml(entry) {
  // Preview / Open PDF / Download for one log entry (needs entry.pdfPath, entry.photoPaths).
  if (!entry.pdfPath && !(entry.photoPaths || []).length) return "";
  const idx = logFileRegister(entry);
  const parts = [`<a href="#" data-preview="${idx}">Preview</a>`];
  if (entry.pdfPath) {
    parts.push(`<a href="#" data-open-path="${logFileAttr(entry.pdfPath)}" data-open-mode="view">Open PDF ↗</a>`);
    parts.push(`<a href="#" data-open-path="${logFileAttr(entry.pdfPath)}" data-open-mode="download">Download</a>`);
  }
  (entry.photoPaths || []).forEach((p, i) => {
    parts.push(`<a href="#" data-open-path="${logFileAttr(p)}" data-open-mode="view">Photo ${i + 1} ↗</a>`);
  });
  return `<div class="lf-links">${parts.join("")}</div>`;
}

document.addEventListener("click", ev => {
  const open = ev.target.closest("[data-open-path]");
  if (open) { ev.preventDefault(); openLogFile(decodeURIComponent(open.dataset.openPath), open.dataset.openMode || "view"); return; }
  const prev = ev.target.closest("[data-preview]");
  if (prev) { ev.preventDefault(); showLogPreview(parseInt(prev.dataset.preview, 10)); }
});
document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeLogPreview(); });
