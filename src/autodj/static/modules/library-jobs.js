// Library tools panel: index / enrich / prune / stats jobs.
// All controls are no-ops on pages that don't include the library
// section markup, so this module is safe to wire unconditionally.

import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";

let _lastLogKey = "";
const _jobStatusState = new WeakMap();

function updateJobStatus(job, jobStatus) {
  let key;
  let text;
  if (job.running) {
    const elapsedBucket = Math.floor(Number(job.elapsed_seconds || 0) / 10);
    key = `running:${job.name}:${elapsedBucket}`;
    text = `${job.name} running for ${job.elapsed_seconds}s…`;
  } else if (job.exit_code != null) {
    key = `finished:${job.name}:${job.exit_code}:${job.elapsed_seconds}`;
    text = job.exit_code === 0
      ? `${job.name} finished cleanly in ${job.elapsed_seconds}s.`
      : `${job.name} exited with code ${job.exit_code} after ${job.elapsed_seconds}s.`;
  } else if (!job.name) {
    key = "idle";
    text = "Idle.";
  } else {
    return;
  }

  const previous = _jobStatusState.get(jobStatus);
  if (previous?.key === key && jobStatus.textContent === previous.text) return;
  if (jobStatus.textContent !== text) jobStatus.textContent = text;
  _jobStatusState.set(jobStatus, { key, text });
}

export function installLibraryJobs(els) {
  const {
    runIndex, runEnrich, runPrune, runStats, runStop,
    indexLimit, statsRefresh,
    statCount,
  } = els;

  if (runIndex) {
    runIndex.addEventListener("click", (event) => {
      const limit = parseInt(indexLimit && indexLimit.value, 10);
      const args = !isNaN(limit) && limit > 0 ? ["--limit", String(limit)] : [];
      void _run(els, "index", args, event.currentTarget);
    });
  }
  if (runEnrich) runEnrich.addEventListener("click", (event) => void _run(els, "enrich", [], event.currentTarget));
  if (runPrune)  runPrune.addEventListener("click",  (event) => void _run(els, "prune", [], event.currentTarget));
  if (runStats)  runStats.addEventListener("click",  (event) => void _run(els, "stats", [], event.currentTarget));
  if (runStop) {
    runStop.addEventListener("click", (event) => {
      const control = event.currentTarget;
      const epoch = captureAuthenticatedRequestEpoch();
      void withDisabled(control, () => requestJson(
        "/api/library/stop", { method: "POST" },
      )).catch((errorValue) => {
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        if (els.jobStatus) {
          els.jobStatus.textContent = `Could not stop library job: ${errorValue.message}`;
        }
      });
    });
  }
  if (statsRefresh) statsRefresh.addEventListener("click", (event) => {
    void refreshLibStats(els, event.currentTarget);
  });
  if (statCount) void refreshLibStats(els);
}

async function _run(els, name, args = [], control = null) {
  const { jobStatus } = els;
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    await withDisabled(control, () => requestJson("/api/library/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, args }),
    }));
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (jobStatus) jobStatus.textContent = `${name} started…`;
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (jobStatus) jobStatus.textContent = `Error starting ${name}: ${err.message || err}`;
  }
}

async function refreshLibStats(els, control = null) {
  const {
    statCount, statAvgBpm, statWithKey, statWithGenre, statWithEnergy,
  } = els;
  if (!statCount) return;
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    const s = await withDisabled(control, () => requestJson("/api/library/stats"));
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    statCount.textContent       = s.track_count;
    statAvgBpm.textContent      = s.average_bpm
      ? `${s.average_bpm} (${s.tracks_with_bpm} tracks)` : "—";
    statWithKey.textContent     = s.tracks_with_key;
    statWithGenre.textContent   = s.tracks_with_genre;
    statWithEnergy.textContent  = s.tracks_with_energy;
  } catch (errorValue) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (els.jobStatus) {
      els.jobStatus.textContent = `Could not load library stats: ${errorValue.message}`;
    }
  }
}

export function applyLibraryJobState(s, els) {
  const { libLog, jobStatus } = els;
  const job = s && s.library_job;
  if (!job || !libLog) return;
  if (jobStatus) updateJobStatus(job, jobStatus);
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
