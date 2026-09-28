// Library tools panel: index / enrich / analyse / prune / stats jobs.
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
//   #library-log is a named region, not a live one, holding one block
//   element per line, so NVDA's browse mode reads it a line per Down
//   Arrow.  Lines are appended, never rebuilt, so a reader inside the log
//   keeps their reading cursor.

import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
  withDisabled,
} from "./api-client.js";
import { fmtDurationWords, fmtTime, setNoValue } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";

const _jobStatusState = new WeakMap();
const _logState = new WeakMap();
// Only an installed panel fetches on its own; applyLibraryJobState alone
// just renders what it is given.
const _installed = new WeakSet();
// Per panel: the latest job snapshot, which finished job has been
// followed up (full log, fresh stats), and which job's full log is shown.
const _jobs = new WeakMap();

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

// The runner's own lines: the command it started ("[autodj-jobs] $ ...")
// and its exit trailer ("[autodj-jobs] exit 1 (elapsed 1.0s)").  Neither
// says why a job failed; its spawn and read errors do, so they stay.
const RUNNER_NOISE = /^\[autodj-jobs\] (\$ |exit )/;

// The last line the job itself printed, which carries a failure's reason
// (a prune safety abort, "No beets_db in config").
function lastOutputLine(job) {
  const lines = job.lines || [];
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = String(lines[i]).trim();
    if (line && !RUNNER_NOISE.test(line)) return line;
  }
  return "";
}

function finishedText(job) {
  const took = fmtDurationWords(job.elapsed_seconds);
  if (job.exit_code === 0) return `${job.name} finished cleanly in ${took}.`;
  // A job the user stopped exits non-zero (1 on Windows), which is not
  // a failure worth an exit code.
  if (job.stopped) return `${job.name} stopped after ${took}.`;
  const reason = lastOutputLine(job);
  return `${job.name} exited with code ${job.exit_code} after ${took}.`
    + (reason ? ` ${reason}` : "");
}

// Shift+J: the job's state on demand.  A running job's progress is the
// percentage from its progress bar when it prints one; the bar itself
// (block characters, counts, "1477.44file/s") is noise when spoken.
export function libraryJobStatusText(job) {
  if (!job || !job.name) return "No library job has run.";
  if (job.running) {
    const percent = /(\d{1,3})(?:\.\d+)?%/.exec(lastOutputLine(job));
    return `${job.name} running, ${fmtDurationWords(job.elapsed_seconds)} elapsed`
      + (percent ? `, ${percent[1]} percent done.` : ".");
  }
  if (job.exit_code != null) return finishedText(job);
  return "No library job has run.";
}

function updateJobStatus(job, jobStatus, jobElapsed) {
  // Silent clock.  Never announced: the node is aria-live="off".
  if (jobElapsed) {
    const elapsed = job.running
      ? `${fmtTime(Number(job.elapsed_seconds) || 0)} elapsed`
      : "";
    if (jobElapsed.textContent !== elapsed) jobElapsed.textContent = elapsed;
  }
  if (!jobStatus) return;

  if (job.running) {
    setJobPhase(jobStatus, `running:${job.name}`, `${job.name} running.`);
    return;
  }
  if (job.exit_code != null) {
    setJobPhase(jobStatus, `finished:${job.name}:${job.exit_code}`, finishedText(job));
    return;
  }
  if (!job.name) setJobPhase(jobStatus, "idle", "Idle.");
}

function emptyLogNote(libLog) {
  const note = libLog.ownerDocument.createElement("p");
  note.className = "lib-log-empty";
  note.textContent = "No job has run yet.";
  return note;
}

function logLine(libLog, text) {
  const line = libLog.ownerDocument.createElement("div");
  line.textContent = text;
  return line;
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
  }
  for (let i = kept; i < lines.length; i++) {
    libLog.appendChild(logLine(libLog, lines[i]));
  }
  _logState.set(libLog, { lines: lines.slice() });
  if (pinned) libLog.scrollTop = libLog.scrollHeight;
}

// The websocket carries only the last 25 log lines, which cut a Stats
// report short.  Once a job has finished, fetch the log the server keeps
// (GET /api/library/job) and add the missing head in front of what is
// already shown, so a reader part-way down the log keeps their place.
function renderFullLog(libLog, lines) {
  const previous = _logState.get(libLog);
  const rendered = previous ? previous.lines : [];
  const head = lines.length - rendered.length;
  const isSuffix = rendered.length > 0 && head >= 0
    && rendered.every((line, i) => line === lines[head + i]);
  if (!isSuffix) {
    _logState.delete(libLog);
    renderLog(libLog, lines);
    return;
  }
  if (head === 0) return;
  const pinned = libLog.scrollTop + libLog.clientHeight
    >= libLog.scrollHeight - 4;
  const fragment = libLog.ownerDocument.createDocumentFragment();
  for (const line of lines.slice(0, head)) fragment.appendChild(logLine(libLog, line));
  libLog.insertBefore(fragment, libLog.firstChild);
  _logState.set(libLog, { lines: lines.slice() });
  if (pinned) libLog.scrollTop = libLog.scrollHeight;
}

async function followUpFinishedJob(els, job) {
  const epoch = captureAuthenticatedRequestEpoch();
  // Index stats change after index, enrich, analyse and prune.  Quiet: the
  // finish announcement is the one thing said.
  void refreshLibStats(els, null, { quiet: true });
  if (!els.libLog) return;
  let full;
  try {
    full = await requestJson("/api/library/job");
  } catch (_errorValue) {
    return;  // The last 25 lines stay on screen.
  }
  if (!isAuthenticatedRequestCurrent(epoch)) return;
  const current = _jobs.get(els);
  if (!full || full.running || full.started_at !== job.started_at
      || !current || current.job.started_at !== job.started_at) return;
  current.fullLogFor = job.started_at;
  renderFullLog(els.libLog, Array.isArray(full.lines) ? full.lines : []);
}

function runningJobName(els) {
  const current = _jobs.get(els);
  return current && current.job.running ? current.job.name : null;
}

// Run buttons stay enabled while a job runs (a disabled button drops
// focus and says nothing about why).  Pressing one says which job holds
// the single slot, through the page status region so #lib-job-status
// keeps its one-announcement-per-phase contract.
function reportBusy(els, name) {
  const doc = (els.jobStatus || els.libLog)?.ownerDocument || globalThis.document;
  announceStatus(doc.getElementById("sr-status"),
    `A job is already running: ${name}.  Wait for it to finish, or press Stop running job.`,
    { dwellMs: 5000, force: true });
}

export function installLibraryJobs(els) {
  const {
    runIndex, runEnrich, runAnalyse, runPrune, runStats, runStop,
    indexLimit, statsRefresh,
    statCount,
  } = els;
  _installed.add(els);

  if (runIndex) {
    runIndex.addEventListener("click", (event) => {
      const limit = parseInt(indexLimit && indexLimit.value, 10);
      const args = !isNaN(limit) && limit > 0 ? ["--limit", String(limit)] : [];
      void _run(els, "index", args, event.currentTarget);
    });
  }
  if (runEnrich) runEnrich.addEventListener("click", (event) => void _run(els, "enrich", [], event.currentTarget));
  if (runAnalyse) runAnalyse.addEventListener("click", (event) => void _run(els, "analyse", [], event.currentTarget));
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
  const running = runningJobName(els);
  if (running) {
    reportBusy(els, running);
    return;
  }
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
    // Another page (or the CLI) started a job since the last push.
    if (err.status === 409 && runningJobName(els)) {
      reportBusy(els, runningJobName(els));
      return;
    }
    reportJobError(jobStatus, `Error starting ${name}: ${err.message || err}`);
  }
}

async function refreshLibStats(els, control = null, { quiet = false } = {}) {
  const {
    statCount, statAvgBpm, statWithKey, statWithGenre, statWithEnergy,
  } = els;
  if (!statCount) return;
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    const s = await withDisabled(control, () => requestJson("/api/library/stats"));
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    statCount.textContent       = s.track_count;
    if (s.average_bpm) {
      statAvgBpm.textContent = `${s.average_bpm} (${s.tracks_with_bpm} tracks)`;
    } else {
      setNoValue(statAvgBpm, "None");
    }
    statWithKey.textContent     = s.tracks_with_key;
    statWithGenre.textContent   = s.tracks_with_genre;
    statWithEnergy.textContent  = s.tracks_with_energy;
    // Pressing Refresh stats when nothing changed rewrote identical
    // numbers, which is silent and looks like a dead button.  Confirm
    // every press (force), through the page-wide status region so the
    // job-phase region above keeps its own contract.
    if (control) {
      announceStatus(control.ownerDocument.getElementById("sr-status"),
        "Stats refreshed.", { dwellMs: 3000, force: true });
    }
  } catch (errorValue) {
    if (!isAuthenticatedRequestCurrent(epoch) || quiet) return;
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
  const previous = _jobs.get(els);
  const current = {
    job,
    finishedFor: previous ? previous.finishedFor : null,
    fullLogFor: previous ? previous.fullLogFor : null,
  };
  _jobs.set(els, current);
  updateJobStatus(job, jobStatus, jobElapsed);
  // The full log of this finished job is already shown; the websocket's
  // 25-line window would cut it back down.
  if (job.running || current.fullLogFor !== job.started_at) {
    renderLog(libLog, job.lines || []);
  }
  const finished = !job.running && job.exit_code != null && job.started_at != null;
  if (finished && current.finishedFor !== job.started_at && _installed.has(els)) {
    current.finishedFor = job.started_at;
    void followUpFinishedJob(els, job);
  }
}
