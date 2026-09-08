let ITEMS = [];
let CURRENT_FILTER = "all";

function scoreBand(score) {
  if (score <= 33) return "green";
  if (score <= 66) return "yellow";
  return "red";
}

function scoreLabel(score) {
  return window.__BADGE_LABEL_STYLE__ === "woke" ? `${score}% woke` : `${score}%`;
}

function render() {
  const grid = document.getElementById("grid");
  const emptyState = document.getElementById("empty-state");
  const filtered = CURRENT_FILTER === "all" ? ITEMS : ITEMS.filter(i => scoreBand(i.score) === CURRENT_FILTER);

  grid.innerHTML = "";
  emptyState.style.display = ITEMS.length === 0 ? "block" : "none";

  for (const item of filtered) {
    const band = scoreBand(item.score);
    const card = document.createElement("div");
    card.className = "card";
    const sourceUrl = item.sourceUrl || "https://isitwokeornot.com/";
    card.innerHTML = `
      <img src="/api/poster/${item.ratingKey}" alt="${item.title}" loading="lazy">
      <div class="badge badge-${band}">${scoreLabel(item.score)}</div>
      <div class="card-overlay">
        <div class="card-title">${item.title}</div>
        <div class="card-year">${item.year || ""}</div>
        <div class="card-actions">
          <button class="card-apply" data-key="${item.ratingKey}">Anwenden</button>
          <a class="source-link" href="${sourceUrl}" target="_blank" rel="noopener noreferrer" title="Quelle: isitwokeornot.com">
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
  for (const band of ["red", "yellow", "green"]) {
    document.getElementById(`count-${band}`).textContent = ITEMS.filter(i => scoreBand(i.score) === band).length;
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
    const res = await fetch(`/api/job/${jobId}`);
    const job = await res.json();
    const [done, total] = job.progress || [0, 0];
    const pct = total ? Math.round((done / total) * 100) : 0;
    showToast(`${labelPrefix}: ${done}/${total}`, pct);
    if (job.state === "done" || job.state === "error") {
      showToast(job.state === "done" ? `${labelPrefix}: fertig` : `${labelPrefix}: Fehler`, 100);
      setTimeout(hideToast, 3000);
      return job;
    }
    await new Promise(r => setTimeout(r, 700));
  }
}

async function loadLibrary() {
  const res = await fetch("/api/library");
  const data = await res.json();
  ITEMS = data.items;
  document.getElementById("demo-flag").style.display = data.demo ? "inline-block" : "none";
  render();
}

async function applyBadges(ratingKeys, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "..."; }
  const res = await fetch("/api/apply", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ratingKeys }),
  });
  const data = await res.json();
  if (data.error) {
    showToast(data.error);
    setTimeout(hideToast, 4000);
    if (btn) { btn.disabled = false; btn.textContent = "Anwenden"; }
    return;
  }
  const job = await pollJob(data.job_id, "Poster werden aktualisiert");
  if (btn) { btn.textContent = "Erledigt"; }
  warnAboutMissingOriginals(job);
}

function warnAboutMissingOriginals(job) {
  const warnings = (job.log || []).filter(l => l.includes("kein TMDb-Original in Plex gefunden"));
  if (warnings.length === 0) return;
  const titles = warnings.map(l => l.replace(/^OK \(mit Warnung\): /, "").split(" - ")[0]);
  alert(
    `Bei ${titles.length} Titel(n) hat Plex kein TMDb-Original-Poster mehr gefunden - ` +
    `dort könnte der Badge weiterhin doppelt sein:\n\n${titles.join("\n")}\n\n` +
    `Fix: In Plex bei diesen Titeln "Metadaten aktualisieren" ausführen, danach hier erneut anwenden.`
  );
}

async function triggerRebuild(full) {
  const res = await fetch("/api/rebuild-cache", {
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
  await pollJob(data.job_id, full ? "Kompletter Neuaufbau" : "Cache wird aufgebaut");
  loadLibrary();
}

document.getElementById("btn-rebuild").addEventListener("click", () => triggerRebuild(false));

document.getElementById("btn-rebuild-full").addEventListener("click", () => {
  if (confirm("Kompletten Neuaufbau starten? Das fragt alle Titel erneut ab und dauert deutlich länger als ein normales Update.")) {
    triggerRebuild(true);
  }
});

document.getElementById("btn-apply-all").addEventListener("click", () => {
  applyBadges(ITEMS.map(i => i.ratingKey));
});

document.getElementById("btn-cleanup-posters").addEventListener("click", async () => {
  const ok = confirm(
    "Alte, selbst hochgeladene Poster-Versionen in der gesamten Plex-Bibliothek löschen?\n\n" +
    "Die aktuell ausgewählten Poster bleiben unangetastet, nur ungenutzte ältere " +
    "Versionen werden entfernt. Original-Poster von TMDb & Co. werden nicht angerührt."
  );
  if (!ok) return;
  const res = await fetch("/api/cleanup-posters", { method: "POST" });
  const data = await res.json();
  if (data.error) {
    showToast(data.error);
    setTimeout(hideToast, 5000);
    return;
  }
  await pollJob(data.job_id, "Plex-Poster werden aufgeräumt");
});

document.querySelectorAll(".chip").forEach(chip => {
  chip.addEventListener("click", () => {
    document.querySelectorAll(".chip").forEach(c => c.classList.remove("chip-active"));
    chip.classList.add("chip-active");
    CURRENT_FILTER = chip.dataset.filter;
    render();
  });
});

loadLibrary();
