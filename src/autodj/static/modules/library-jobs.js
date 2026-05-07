// Library tools panel: index / enrich / prune / stats jobs.
// All controls are no-ops on pages that don't include the library
// section markup, so this module is safe to wire unconditionally.

let _lastLogKey = "";

export function installLibraryJobs(els) {
  const {
    runIndex, runEnrich, runPrune, runStats, runStop,
    indexLimit, statsRefresh,
    statCount,
  } = els;

  if (runIndex) {
    runIndex.addEventListener("click", () => {
      const limit = parseInt(indexLimit && indexLimit.value, 10);
      const args = !isNaN(limit) && limit > 0 ? ["--limit", String(limit)] : [];
      _run(els, "index", args);
    });
  }
  if (runEnrich) runEnrich.addEventListener("click", () => _run(els, "enrich"));
  if (runPrune)  runPrune.addEventListener("click",  () => _run(els, "prune"));
  if (runStats)  runStats.addEventListener("click",  () => _run(els, "stats"));
  if (runStop) {
    runStop.addEventListener("click", async () => {
      try { await fetch("/api/library/stop", { method: "POST" }); } catch (_) {}
    });
  }
  if (statsRefresh) statsRefresh.addEventListener("click", () => refreshLibStats(els));
  if (statCount)    refreshLibStats(els);
}

async function _run(els, name, args = []) {
  const { jobStatus } = els;
  try {
    const r = await fetch("/api/library/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, args }),
    });
    if (!r.ok) {
      const txt = await r.text();
      if (jobStatus) jobStatus.textContent = `Could not start ${name}: ${txt}`;
      return;
    }
    if (jobStatus) jobStatus.textContent = `${name} started…`;
  } catch (err) {
    if (jobStatus) jobStatus.textContent = `Error starting ${name}: ${err.message || err}`;
  }
}

export async function refreshLibStats(els) {
  const {
    statCount, statAvgBpm, statWithKey, statWithGenre, statWithEnergy,
  } = els;
  if (!statCount) return;
  try {
    const r = await fetch("/api/library/stats");
    if (!r.ok) return;
    const s = await r.json();
    statCount.textContent       = s.track_count;
    statAvgBpm.textContent      = s.average_bpm
      ? `${s.average_bpm} (${s.tracks_with_bpm} tracks)` : "—";
    statWithKey.textContent     = s.tracks_with_key;
    statWithGenre.textContent   = s.tracks_with_genre;
    statWithEnergy.textContent  = s.tracks_with_energy;
  } catch (_) {}
}

export function applyLibraryJobState(s, els) {
  const { libLog, jobStatus } = els;
  const job = s && s.library_job;
  if (!job || !libLog) return;
  if (jobStatus) {
    if (job.running) {
      jobStatus.textContent = `${job.name} running for ${job.elapsed_seconds}s…`;
    } else if (job.exit_code != null) {
      const ok = job.exit_code === 0;
      jobStatus.textContent = ok
        ? `${job.name} finished cleanly in ${job.elapsed_seconds}s.`
        : `${job.name} exited with code ${job.exit_code} after ${job.elapsed_seconds}s.`;
    } else if (!job.name) {
      jobStatus.textContent = "Idle.";
    }
  }
  // Append-only log render -- only re-render when payload changed.
  const lines = job.lines || [];
  const key = lines.length + "@" + (lines[lines.length - 1] || "");
  if (key === _lastLogKey) return;
  _lastLogKey = key;
  if (lines.length === 0) {
    libLog.innerHTML = '<em style="color:var(--text-dim)">No job has run yet.</em>';
  } else {
    libLog.textContent = lines.join("\n");
    libLog.scrollTop = libLog.scrollHeight;
  }
}
