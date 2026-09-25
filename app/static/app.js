let ITEMS = [];
let CURRENT_FILTER = "all";

// Grid order, remembered per browser. Default: title, A-Z.
const SORT_FIELDS = ["title", "score", "released"];
const SORT_STORAGE_KEY = "wokearr.sort";
let SORT = loadSort();

function loadSort() {
  try {
    const saved = JSON.parse(localStorage.getItem(SORT_STORAGE_KEY));
    if (saved && SORT_FIELDS.includes(saved.field) && ["asc", "desc"].includes(saved.dir)) return saved;
  } catch (e) {
    // Storage blocked or garbage in it - just use the default
  }
  return { field: "title", dir: "asc" };
}

function saveSort() {
  try {
    localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(SORT));
  } catch (e) {
    // Not remembered then - sorting itself still works
  }
}

// Titles and error messages come from outside (Plex, isitwokeornot.com) and
// end up in innerHTML - never unescaped.
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

const TITLE_COLLATOR = new Intl.Collator(window.__LANGUAGE__ || undefined, { sensitivity: "base", numeric: true });

function releaseKey(item) {
  if (item.released) return item.released;
  return item.year ? `${String(item.year).padStart(4, "0")}-01-01` : null;
}

function sortItems(items) {
  const dir = SORT.dir === "desc" ? -1 : 1;
  const byTitle = (a, b) => TITLE_COLLATOR.compare(a.sortTitle || a.title, b.sortTitle || b.title);
  return [...items].sort((a, b) => {
    let cmp;
    if (SORT.field === "score") {
      cmp = a.score - b.score;
    } else if (SORT.field === "released") {
      const ra = releaseKey(a), rb = releaseKey(b);
      // Titles without any date go last, whichever direction
      if (!ra || !rb) return ra ? -1 : rb ? 1 : byTitle(a, b);
      cmp = ra < rb ? -1 : ra > rb ? 1 : 0;
    } else {
      cmp = byTitle(a, b);
    }
    // Ties (same score, same day) always A-Z, so the order never jumps around
    return cmp * dir || byTitle(a, b);
  });
}

function updateSortControls() {
  document.getElementById("sort-field").value = SORT.field;
  const dirBtn = document.getElementById("sort-dir");
  const label = t(SORT.dir === "asc" ? "sort.asc" : "sort.desc");
  dirBtn.textContent = SORT.dir === "asc" ? "↑" : "↓";
  dirBtn.title = label;
  dirBtn.setAttribute("aria-label", label);
}

// Every API call goes through here: the X-Requested-With header is what the
// server checks for requests that change something when a login is active
// (CSRF protection, see auth.py), and a 401 with the login page (forms) means
// the session ended - back to the login instead of failing silently.
async function apiFetch(url, options = {}) {
  const headers = { ...(options.headers || {}), "X-Requested-With": "wokearr" };
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401 && window.__AUTH_METHOD__ === "forms") {
    window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    throw new Error("login required");
  }
  return res;
}

function t(key, vars) {
  let template = (window.__I18N__ && window.__I18N__[key]) || key;
  if (vars) {
    for (const k in vars) template = template.split(`{${k}}`).join(vars[k]);
  }
  return template;
}

// Official bands from isitwokeornot.com. Keys match data-filter / count-<key>
// in the template; CSS classes use the kebab-case variant (see bandClass).
const BANDS = [
  { key: "not_woke", max: 19 },
  { key: "slightly_woke", max: 39 },
  { key: "woke", max: 59 },
  { key: "very_woke", max: 79 },
  { key: "super_woke", max: 100 },
];

function scoreBand(score) {
  const band = BANDS.find(b => score <= b.max);
  return band ? band.key : BANDS[BANDS.length - 1].key;
}

function bandClass(band) {
  return band.replace(/_/g, "-");
}

function scoreLabel(score) {
  return window.__BADGE_LABEL_STYLE__ === "woke" ? `${score}% woke` : `${score}%`;
}

function render() {
  const grid = document.getElementById("grid");
  const emptyState = document.getElementById("empty-state");
  const filtered = sortItems(CURRENT_FILTER === "all" ? ITEMS : ITEMS.filter(i => scoreBand(i.score) === CURRENT_FILTER));

  grid.innerHTML = "";
  emptyState.style.display = ITEMS.length === 0 ? "block" : "none";

  for (const item of filtered) {
    const band = scoreBand(item.score);
    const card = document.createElement("div");
    card.className = "card";
    const sourceUrl = item.sourceUrl || "https://isitwokeornot.com/";
    const key = encodeURIComponent(item.ratingKey);
    card.innerHTML = `
      <img src="/api/poster/${key}" alt="${escapeHtml(item.title)}" loading="lazy">
      <div class="badge badge-${bandClass(band)}">${escapeHtml(scoreLabel(item.score))}</div>
      <div class="card-overlay">
        <div class="card-title">${escapeHtml(item.title)}</div>
        <div class="card-year">${escapeHtml(item.year || "")}</div>
        <div class="card-actions">
          <button class="card-apply" data-key="${escapeHtml(item.ratingKey)}">${escapeHtml(t("card.push"))}</button>
          <a class="source-link" href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(t("card.source_title"))}">
            <img class="source-icon" src="https://isitwokeornot.com/favicon.ico" alt="isitwokeornot.com">
          </a>
        </div>
      </div>
    `;
    grid.appendChild(card);

    const sourceIcon = card.querySelector(".source-icon");
    sourceIcon.addEventListener("error", () => {
      sourceIcon.outerHTML = '<span class="source-icon-fallback">&#8599;</span>';
    }, { once: true });
  }

  grid.querySelectorAll(".card-apply").forEach(btn => {
    btn.addEventListener("click", () => applyBadges([btn.dataset.key], btn));
  });

  document.getElementById("count-all").textContent = ITEMS.length;
  for (const { key } of BANDS) {
    document.getElementById(`count-${key}`).textContent = ITEMS.filter(i => scoreBand(i.score) === key).length;
  }
}

function showToast(text, progress) {
  const toast = document.getElementById("toast");
  const bar = document.getElementById("toast-bar-fill");
  toast.style.display = "block";
  document.getElementById("toast-text").textContent = text;
  if (progress != null) bar.style.width = `${progress}%`;
}
function hideToast() {
  document.getElementById("toast").style.display = "none";
}

async function pollJob(jobId, labelPrefix) {
  while (true) {
    const res = await apiFetch(`/api/job/${jobId}`);
    const job = await res.json();
    const [done, total] = job.progress || [0, 0];
    const pct = total ? Math.round((done / total) * 100) : 0;
    showToast(`${labelPrefix}: ${done}/${total}`, pct);
    if (job.state === "done" || job.state === "error") {
      if (job.state === "done") {
        showToast(`${labelPrefix}: ${t("toast.suffix_done")}`, 100);
        setTimeout(hideToast, 3000);
      } else {
        // The reason, not just "error" - it stays up a little longer to be read
        showToast(job.error || `${labelPrefix}: ${t("toast.suffix_error")}`, 100);
        setTimeout(hideToast, 8000);
      }
      // Every job writes a protocol entry - refresh it here so all four
      // buttons get it without each handler having to remember.
      loadStatus();
      return job;
    }
    await new Promise(r => setTimeout(r, 700));
  }
}

// Which run-stat keys are worth showing, in display order. Zero values are
// skipped so a quiet run stays a short line instead of a wall of zeros.
const RUN_STATS = [
  "scores_updated",
  "originals_fetched",
  "rendered",
  "pushed",
  "orphans_removed",
  "plex_posters_removed",
];

function formatWhen(iso) {
  const d = new Date(iso);
  // Formatted in the configured UI language, not the browser's - otherwise a
  // German UI would show US-formatted timestamps.
  return isNaN(d) ? iso : d.toLocaleString(window.__LANGUAGE__ || undefined);
}

function formatDuration(seconds) {
  if (seconds == null) return "";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const mins = Math.floor(seconds / 60);
  return `${mins}m ${Math.round(seconds % 60)}s`;
}

function errorSummary(error) {
  const reason = t(`error.${error.kind || "other"}`, { target: error.target || "?", status: error.status || "?" });
  return t("status.stage_failed", { stage: t(`status.trigger.${error.stage}`), reason });
}

function runErrors(run) {
  return Array.isArray(run.errors) ? run.errors : [];
}

// The delta in matched titles compares against the last run that counted
// them - a score-database run in between (which doesn't) mustn't hide it.
function previousMatched(runs, index) {
  const earlier = runs.slice(index + 1).find(r => r.matched != null);
  return earlier ? earlier.matched : null;
}

function runChanges(run, prevMatched) {
  const parts = [];
  if (run.matched != null) {
    let label = `${run.matched} ${t("status.stat.matched")}`;
    if (prevMatched != null) {
      const delta = run.matched - prevMatched;
      if (delta !== 0) label += ` (${delta > 0 ? "+" : ""}${delta})`;
    }
    parts.push(label);
  }
  for (const key of RUN_STATS) {
    if (run[key]) parts.push(`${run[key]} ${t(`status.stat.${key}`)}`);
  }
  if (run.score_changes && run.score_changes.length) {
    parts.push(`${run.score_changes.length} ${t("status.stat.score_changes")}`);
  }
  if (run.push_failed) parts.push(`${run.push_failed} ${t("status.stat.errors")}`);
  const errors = runErrors(run);
  if (errors.length) {
    for (const error of errors) parts.push(errorSummary(error));
  } else if (run.error) {
    // Protocol entries from before errors were recorded in detail
    parts.push(t("status.failed"));
  }
  return parts.length ? parts.join(", ") : t("status.no_changes");
}

// The lines behind a run's summary: what changed by name, and the raw error
// messages. Only shown when the row is expanded.
function runDetails(run) {
  const lines = [];
  if (run.new_titles && run.new_titles.length) {
    lines.push(t("status.detail.new_titles", { titles: run.new_titles.join(", ") }));
  }
  for (const change of run.score_changes || []) {
    lines.push(t("status.detail.score_change", { title: change.title, old: change.old, new: change.new }));
  }
  for (const error of runErrors(run)) {
    if (error.message) lines.push(`${t(`status.trigger.${error.stage}`)}: ${error.message}`);
  }
  return lines;
}

function changesCell(run, prevMatched) {
  const summary = escapeHtml(runChanges(run, prevMatched));
  const details = runDetails(run);
  if (!details.length) return summary;
  const items = details.map(line => `<li>${escapeHtml(line)}</li>`).join("");
  return `<details class="run-details"><summary>${summary}</summary><ul>${items}</ul></details>`;
}

function formatNextRun(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const lang = window.__LANGUAGE__ || undefined;
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString(lang, { dateStyle: "short", timeStyle: "short" });
}

function renderStatus(data) {
  const versionEl = document.getElementById("status-version");
  versionEl.innerHTML = "";
  if (data.version) {
    const link = document.createElement("a");
    link.href = data.version_url || "https://github.com/Jake-double-one/wokearr";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = `Wokearr ${data.version}`;
    versionEl.appendChild(link);
  }
  if (data.build_date) {
    versionEl.append(` · ${t("status.build", { date: formatWhen(data.build_date) })}`);
  }

  const nextEl = document.getElementById("status-next-run");
  nextEl.textContent = data.next_run ? t("status.next_run", { when: formatNextRun(data.next_run) }) : "";
  nextEl.hidden = !data.next_run;

  const runs = data.runs || [];
  const lastRunEl = document.getElementById("status-last-run");
  if (!runs.length) {
    lastRunEl.textContent = t("status.never");
    lastRunEl.classList.remove("status-error");
  } else {
    // runs[] is newest first
    lastRunEl.textContent =
      t("status.last_run", { when: formatWhen(runs[0].started_at) }) + " · " + runChanges(runs[0], previousMatched(runs, 0));
    lastRunEl.classList.toggle("status-error", Boolean(runs[0].error));
  }

  const history = document.getElementById("status-history");
  if (!runs.length) {
    history.innerHTML = "";
    return;
  }
  const rows = runs.map((run, i) => `
    <tr>
      <td>${escapeHtml(formatWhen(run.started_at))}</td>
      <td>${escapeHtml(t(`status.trigger.${run.trigger}`))}</td>
      <td class="status-changes${run.error ? " status-error" : ""}">${changesCell(run, previousMatched(runs, i))}</td>
      <td>${escapeHtml(formatDuration(run.duration_seconds))}</td>
    </tr>`).join("");
  history.innerHTML = `
    <table>
      <thead><tr>
        <th>${escapeHtml(t("status.col.when"))}</th>
        <th>${escapeHtml(t("status.col.trigger"))}</th>
        <th>${escapeHtml(t("status.col.changes"))}</th>
        <th>${escapeHtml(t("status.col.duration"))}</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function loadStatus() {
  try {
    const res = await apiFetch("/api/status");
    renderStatus(await res.json());
  } catch (e) {
    // The protocol is a nice-to-have - never let it break the main view
  }
}

async function loadLibrary() {
  const res = await apiFetch("/api/library");
  const data = await res.json();
  ITEMS = data.items;
  document.getElementById("demo-flag").style.display = data.demo ? "inline-block" : "none";
  render();
}

async function applyBadges(ratingKeys, btn) {
  if (btn) { btn.disabled = true; btn.textContent = t("card.loading"); }
  const res = await apiFetch("/api/apply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ratingKeys }),
  });
  const data = await res.json();
  if (data.error) {
    showToast(data.error);
    setTimeout(hideToast, 4000);
    if (btn) { btn.disabled = false; btn.textContent = t("card.push"); }
    return;
  }
  const job = await pollJob(data.job_id, t("toast.uploading_posters"));
  if (btn) { btn.textContent = t("card.push_done"); }
}

function warnAboutMissingOriginals(job) {
  const titles = job.missing_originals;
  if (!titles || !titles.length) return;
  alert(t("alert.missing_originals", { titles: titles.join(", ") }));
}

async function triggerRebuild(full) {
  const res = await apiFetch("/api/rebuild-cache", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ full }),
  });
  const data = await res.json();
  if (data.error) {
    showToast(data.error);
    setTimeout(hideToast, 5000);
    return;
  }
  await pollJob(data.job_id, full ? t("topbar.btn_rebuild_full.label") : t("toast.building_cache"));
  loadLibrary();
}

document.getElementById("btn-sync-library").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  const res = await apiFetch("/api/sync-library", { method: "POST" });
  const data = await res.json();
  if (data.error) {
    showToast(data.error);
    setTimeout(hideToast, 5000);
    btn.disabled = false;
    return;
  }
  const job = await pollJob(data.job_id, t("toast.syncing_with_plex"));
  btn.disabled = false;
  warnAboutMissingOriginals(job);
  loadLibrary();
});

document.getElementById("btn-rebuild").addEventListener("click", () => triggerRebuild(false));

document.getElementById("btn-rebuild-full").addEventListener("click", () => {
  if (confirm(t("confirm.full_rebuild"))) {
    triggerRebuild(true);
  }
});

document.getElementById("btn-apply-all").addEventListener("click", () => {
  applyBadges(ITEMS.map(i => i.ratingKey));
});

document.getElementById("btn-cleanup-posters").addEventListener("click", async () => {
  const ok = confirm(t("confirm.cleanup_posters"));
  if (!ok) return;
  const res = await apiFetch("/api/cleanup-posters", { method: "POST" });
  const data = await res.json();
  if (data.error) {
    showToast(data.error);
    setTimeout(hideToast, 5000);
    return;
  }
  await pollJob(data.job_id, t("toast.cleaning_posters"));
});

document.getElementById("status-toggle").addEventListener("click", (e) => {
  const btn = e.currentTarget;
  const history = document.getElementById("status-history");
  const open = history.hidden;
  history.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  btn.title = open ? t("status.hide_history") : t("status.show_history");
});

document.querySelectorAll(".chip").forEach(chip => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach(c => c.classList.remove("chip-active"));
    chip.classList.add("chip-active");
    CURRENT_FILTER = chip.dataset.filter;
    render();
  });
});

document.getElementById("sort-field").addEventListener("change", (e) => {
  SORT.field = e.currentTarget.value;
  // Score and release date read most naturally highest/newest first
  SORT.dir = SORT.field === "title" ? "asc" : "desc";
  saveSort();
  updateSortControls();
  render();
});

document.getElementById("sort-dir").addEventListener("click", () => {
  SORT.dir = SORT.dir === "asc" ? "desc" : "asc";
  saveSort();
  updateSortControls();
  render();
});

document.getElementById("status-toggle").title = t("status.show_history");
updateSortControls();

loadLibrary();
loadStatus();
