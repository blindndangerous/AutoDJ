// Library tools panel: index / enrich / prune / stats jobs.
// All controls are no-ops on pages that don't include the library
// section markup, so this module is safe to wire unconditionally.
//
// Live-region contract (NVDA repeat fix):
//   #lib-job-status is the ONLY announcing node in this panel.  It is
//   keyed on the job *phase* (idle / running / finished), never on the
//   clock, so a job that runs for two hours speaks exactly once when it
//   starts and once when it ends.  The ticking elapsed counter lives in
//   the sibling #lib-job-elapsed, which is aria-live="off" and therefore
//   silent no matter how often it changes.
//   #library-log is a plain <pre>: it is appended to, never rebuilt, so
//   a reader who has tabbed into it keeps their reading cursor.

import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";

const _jobStatusState = new WeakMap();
const _logState = new WeakMap();

// Write `text` into the live region only when the phase actually
// changed.  Repeated ticks carrying the same phase are silent.
function setJobPhase(jobStatus, phase, text) {
  if (!jobStatus) return;
  const previous = _jobStatusState.get(jobStatus);
  if (previous && previous.phase === phase) return;
  _jobStatusState.set(jobStatus, { phase });
  if (jobStatus.textContent !== text) jobStatus.textContent = text;
}

// Errors are user-visible failures of a specific action, not a job
// phase.  They must always land, and they must not be wiped by the very
// next websocket tick — so they clear the phase memory instead of
// setting one, and the following tick re-establishes the real phase.
function reportJobError(jobStatus, text) {
  if (!jobStatus) return;
  _jobStatusState.delete(jobStatus);
  if (jobStatus.textContent !== text) jobStatus.textContent = text;
}

function updateJobStatus(job, jobStatus, jobElapsed) {
  // Visual-only clock.  Never announced: the node is aria-live="off".
  if (jobElapsed) {
    const elapsed = job.running
      ? `${Math.round(Number(job.elapsed_seconds) || 0)}s elapsed`
      : "";
    if (jobElapsed.textContent !== elapsed) jobElapsed.textContent = elapsed;
  }
  if (!jobStatus) return;

  if (job.running) {
    setJobPhase(jobStatus, `running:${job.name}`, `${job.name} running.`);
    return;
  }
  if (job.exit_code != null) {
    const seconds = Math.round(Number(job.elapsed_seconds) || 0);
    setJobPhase(
      jobStatus,
      `finished:${job.name}:${job.exit_code}`,
      job.exit_code === 0
        ? `${job.name} finished cleanly in ${seconds} seconds.`
        : `${job.name} exited with code ${job.exit_code} after ${seconds} seconds.`,
    );
    return;
  }
  if (!job.name) setJobPhase(jobStatus, "idle", "Idle.");
}

function emptyLogNote(libLog) {
  const note = libLog.ownerDocument.createElement("em");
  note.className = "lib-log-empty";
  note.textContent = "No job has run yet.";
  return note;
}

// Index of the first rendered line inside `lines`, or -1 when the two
// windows do not overlap (a brand-new job, or a reset log).
function slideOffset(rendered, lines) {
  for (let offset = 0; offset < rendered.length; offset++) {
    const tail = rendered.length - offset;
    if (tail > lines.length) continue;
    let matches = true;
    for (let i = 0; i < tail; i++) {
      if (rendered[offset + i] !== lines[i]) { matches = false; break; }
    }
    if (matches) return offset;
  }
  return -1;
}

// Append-only render.  The server sends a sliding 25-line window, so the
// head may fall off; drop exactly the vanished nodes and append exactly
// the new ones rather than replacing the whole subtree every second.
function renderLog(libLog, lines) {
  const previous = _logState.get(libLog);
  const rendered = previous ? previous.lines : null;

  if (lines.length === 0) {
    if (!rendered || rendered.length !== 0) {
      libLog.replaceChildren(emptyLogNote(libLog));
      _logState.set(libLog, { lines: [] });
    }
    return;
  }

  const pinned = libLog.scrollTop + libLog.clientHeight
    >= libLog.scrollHeight - 4;
  const offset = rendered && rendered.length ? slideOffset(rendered, lines) : -1;
  let kept = 0;
  if (offset < 0) {
    libLog.replaceChildren();
  } else {
    kept = rendered.length - offset;
    for (let i = 0; i < offset; i++) {
      if (libLog.firstChild) libLog.removeChild(libLog.firstChild);
    }
    // The surviving first node still carries the separator newline that
    // joined it to the node just removed.
    const first = libLog.firstChild;
    if (first && typeof first.data === "string" && first.data.startsWith("\n")) {
      first.data = first.data.slice(1);
    }
  }
  for (let i = kept; i < lines.length; i++) {
    libLog.appendChild(
      libLog.ownerDocument.createTextNode((i === 0 ? "" : "\n") + lines[i]),
    );
  }
  _logState.set(libLog, { lines: lines.slice() });
  if (pinned) libLog.scrollTop = libLog.scrollHeight;
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
        reportJobError(
          els.jobStatus,
          `Could not stop library job: ${errorValue.message}`,
        );
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
    // Seed the phase so the websocket tick that lands a fraction of a
    // second later does not speak the same thing again.
    setJobPhase(jobStatus, `running:${name}`, `${name} started.`);
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    reportJobError(jobStatus, `Error starting ${name}: ${err.message || err}`);
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
    reportJobError(
      els.jobStatus,
      `Could not load library stats: ${errorValue.message}`,
    );
  }
}

export function applyLibraryJobState(s, els) {
  const { libLog, jobStatus, jobElapsed } = els;
  const job = s && s.library_job;
  if (!job || !libLog) return;
  updateJobStatus(job, jobStatus, jobElapsed);
  renderLog(libLog, job.lines || []);
}
