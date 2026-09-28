// Voice liners -- Settings panel, file list, upload / delete / test,
// trigger evaluation, scheduler.
//
// Audio playback (Web Audio decode + duck the active deck) lives in
// the audio-engine module and is injected via deps.playLiner so this
// module stays free of AudioContext + decks state.

import { dbg, replaceRows } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { applyShowWhen } from "./show-when.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestBinary,
  requestJson,
  withDisabled,
} from "./api-client.js";

const state = {
  lib: { folder: "", files: [], config: {} },
  lastFireAt: 0,
  trackCount: 0,
  randomTarget: null,
  seqCursor: 0,
  lastSeenPath: null,
};

function _intOrNull(el) {
  if (!el || el.value === "" || el.value == null) return null;
  const n = parseInt(el.value, 10);
  return isNaN(n) ? null : n;
}

// A trigger field left blank means "off", the same as 0.  Sending null
// would mean "leave unchanged" to the server, so clearing the field could
// never switch a trigger off.
function _intOrOff(el) {
  return _intOrNull(el) ?? 0;
}

function _floatOrOff(el) {
  return _floatOrNull(el) ?? 0;
}

function _floatOrNull(el) {
  if (!el || el.value === "" || el.value == null) return null;
  const n = parseFloat(el.value);
  return isNaN(n) ? null : n;
}

function _setStatus(els, msg, { force = false } = {}) {
  if (!els.lnStatus) return;
  els.lnStatus.classList.remove("visually-hidden");
  announceStatus(els.lnStatus, msg, { dwellMs: 4000, force });
}

// `focus` comes from a delete: see replaceRows.
export function renderLinerFileList(fileList, files, onDelete, focus = null) {
  if (!fileList) return;
  if (!files || files.length === 0) {
    const empty = document.createElement("li");
    empty.className = "no-results";
    empty.textContent = "No liner files yet.";
    replaceRows(fileList, [empty], focus);
    return;
  }
  const rows = [];
  for (const name of files) {
    const li = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = name;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Delete";
    button.setAttribute("aria-label", `Delete ${name}`);
    button.addEventListener("click", () => onDelete(name, button));
    li.appendChild(text);
    li.appendChild(document.createTextNode(" "));
    li.appendChild(button);
    rows.push(li);
  }
  replaceRows(fileList, rows, focus);
}

async function _refreshLibrary(els, focus = null) {
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    const body = await requestJson("/api/liners");
    if (!isAuthenticatedRequestCurrent(epoch)) return false;
    state.lib = body;
    if (els.lnFolderDisplay) {
      els.lnFolderDisplay.textContent = "Folder: " + (body.folder || "—");
    }
    renderLinerFileList(
      els.lnFileList,
      body.files,
      (name, button) => void _deleteLiner(els, name, button),
      focus,
    );
    // Sync config inputs from server payload, leaving fields the user
    // is currently editing untouched.
    const c = body.config || {};
    const sync = (el, v) => {
      if (el && document.activeElement !== el) el.value = v;
    };
    if (els.lnEnabled && document.activeElement !== els.lnEnabled) {
      els.lnEnabled.checked = !!c.enabled;
    }
    sync(els.lnEveryN,    c.every_n_songs        != null ? c.every_n_songs        : "");
    sync(els.lnEveryMin,  c.every_minutes        != null ? c.every_minutes        : "");
    sync(els.lnRandMin,   c.random_min_minutes   != null ? c.random_min_minutes   : "");
    sync(els.lnRandMax,   c.random_max_minutes   != null ? c.random_max_minutes   : "");
    sync(els.lnPickMode,  c.pick_mode || "random");
    sync(els.lnDuckDb,    c.duck_db != null ? c.duck_db : -12);
    applyShowWhen();
    return true;
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return false;
    dbg("liner refresh failed:", err);
    _setStatus(els, `Could not load liners: ${err.message}`);
    return false;
  }
}

async function _deleteLiner(els, name, control) {
  if (!confirm(`Delete liner "${name}"?`)) return;
  const controls = els.lnFileList
    ? Array.from(els.lnFileList.querySelectorAll("button"))
    : [];
  const deletedIndex = Math.max(0, controls.indexOf(control));
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    await withDisabled(control, () => requestJson(
      `/api/liners/file/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ));
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    _setStatus(els, `Deleted ${name}`);
    await _refreshLibrary(els, {
      from: control, selector: "button", index: deletedIndex, fallback: els.lnUploadSubmit,
    });
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    _setStatus(els, `Delete failed: ${err.message}`);
  }
}

function _postConfig(els, postSettings, control) {
  void postSettings("/api/playback-settings", {
    liners_enabled:            !!(els.lnEnabled && els.lnEnabled.checked),
    liners_every_n_songs:      _intOrOff(els.lnEveryN),
    liners_every_minutes:      _floatOrOff(els.lnEveryMin),
    liners_random_min_minutes: _floatOrOff(els.lnRandMin),
    liners_random_max_minutes: _floatOrOff(els.lnRandMax),
    liners_pick_mode:          els.lnPickMode ? els.lnPickMode.value : "random",
    liners_duck_db:            _floatOrNull(els.lnDuckDb),
  }, control);
}

function _pickLiner() {
  if (!state.lib.files || state.lib.files.length === 0) return null;
  const mode = (state.lib.config && state.lib.config.pick_mode) || "random";
  if (mode === "sequential") {
    const i = (state.seqCursor++) % state.lib.files.length;
    return state.lib.files[i];
  }
  const i = Math.floor(Math.random() * state.lib.files.length);
  return state.lib.files[i];
}

function _rollRandomTarget() {
  const c = state.lib.config || {};
  const lo = c.random_min_minutes;
  const hi = c.random_max_minutes;
  if (lo == null || hi == null || lo > hi || hi <= 0) return null;
  return lo + Math.random() * (hi - lo);
}

// `ready` says whether the liner may still play: the schedule's canPlay,
// or Test's own check.
async function _playByName(els, deps, name, ready = deps.canPlay) {
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    if (!ready()) return;
    const buf = await requestBinary(`/api/liners/file/${encodeURIComponent(name)}`);
    if (!ready() || !isAuthenticatedRequestCurrent(epoch)) return;
    const duckDb = (state.lib.config && state.lib.config.duck_db) || -12;
    const ok = await deps.playLiner(buf, duckDb, epoch);
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (!ok) {
      _setStatus(els, "Liner playback skipped (audio context not ready).");
      return;
    }
    state.lastFireAt   = performance.now();
    state.trackCount   = 0;
    state.randomTarget = _rollRandomTarget();
    _setStatus(els, `Liner playing: ${name}`);
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    _setStatus(els, `Liner playback failed: ${err.message}`);
  }
}

// Stream mode or --server-audio: the server mixes, so the liner has to go
// into that mix (every listener hears it) instead of this browser's
// speakers.  The result
// is forced: a second press of Test must be reported again, not silenced
// because the region still holds the same sentence.  When the server says
// why nothing played (the station is idle or paused), that reason is shown.
async function _testOnServer(els, name) {
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    const body = await requestJson("/api/liners/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    _setStatus(
      els,
      body && body.played
        ? `Liner playing: ${body.played}`
        : (body && body.message) || `Could not play ${name}.`,
      { force: true },
    );
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    _setStatus(els, `Liner playback failed: ${err.message}`, { force: true });
  }
}

export function installLiners(els, deps) {
  state.lastFireAt = performance.now();
  let scheduledPlayback = null;

  if (els.lnUploadSubmit) {
    els.lnUploadSubmit.addEventListener("click", async (event) => {
      if (!els.lnUpload || !els.lnUpload.files || els.lnUpload.files.length === 0) {
        _setStatus(els, "Pick a file first.");
        return;
      }
      const f = els.lnUpload.files[0];
      const fd = new FormData();
      fd.append("file", f, f.name);
      const replace = Boolean(els.lnUploadReplace && els.lnUploadReplace.checked);
      _setStatus(els, `Uploading ${f.name}...`);
      const epoch = captureAuthenticatedRequestEpoch();
      try {
        await withDisabled(event.currentTarget, () => requestJson(
          `/api/liners/upload${replace ? "?replace=true" : ""}`,
          { method: "POST", body: fd },
        ));
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        _setStatus(els, replace ? `Uploaded ${f.name}, replacing any file with that name.` : `Uploaded ${f.name}`);
        els.lnUpload.value = "";
        await _refreshLibrary(els);
      } catch (err) {
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        _setStatus(els, err.status === 409 && !replace
          ? `Upload failed: a liner named ${f.name} already exists.  Tick Replace existing file to overwrite it.`
          : `Upload failed: ${err.message}`);
      }
    });
  }

  for (const el of [
    els.lnEnabled, els.lnEveryN, els.lnEveryMin,
    els.lnRandMin, els.lnRandMax, els.lnPickMode, els.lnDuckDb,
  ]) {
    if (!el) continue;
    el.addEventListener("change", (event) => {
      _postConfig(els, deps.postSettings, event.currentTarget);
    });
  }

  if (els.lnTestBtn) {
    els.lnTestBtn.addEventListener("click", async (event) => {
      const name = _pickLiner();
      if (!name) {
        _setStatus(els, "No liner files in folder.");
        return;
      }
      if (deps.testOnServer()) {
        await withDisabled(event.currentTarget, () => _testOnServer(els, name));
        return;
      }
      // Browser playback: Test previews the liner on this page, over the
      // music or on its own.  prepareTest says why it cannot (Mute, for
      // one), and runs inside the click so the page's audio can start.
      const refusal = deps.prepareTest();
      if (refusal) {
        _setStatus(els, refusal, { force: true });
        return;
      }
      await withDisabled(event.currentTarget,
        () => _playByName(els, deps, name, () => deps.prepareTest() === null));
    });
  }

  // Periodic trigger evaluation -- once per second.
  setInterval(() => {
    if (!state.lib.config || !state.lib.config.enabled) return;
    if (!deps.canPlay()) return;
    const c = state.lib.config;
    const minsSince = (performance.now() - state.lastFireAt) / 60000;
    let fire = false;
    if (c.every_n_songs && state.trackCount >= c.every_n_songs) fire = true;
    if (c.every_minutes && minsSince >= c.every_minutes) fire = true;
    if (state.randomTarget != null && minsSince >= state.randomTarget) fire = true;
    if (fire && !scheduledPlayback) {
      const name = _pickLiner();
      if (name) {
        scheduledPlayback = _playByName(els, deps, name).finally(() => {
          scheduledPlayback = null;
        });
      }
    }
  }, 1000);

  // Initial fetch + reapply hidden state on load.
  void _refreshLibrary(els);
}

// Bumps the every_n_songs counter when the WS state surfaces a new
// current_track path.  One hook covers every advance route.
export function bumpLinerTrackCount(s) {
  const cur = (s && s.current_track && s.current_track.path) || null;
  if (!cur || cur === state.lastSeenPath) return false;
  const hadBaseline = state.lastSeenPath !== null;
  state.lastSeenPath = cur;
  if (!hadBaseline) return false;
  state.trackCount += 1;
  return true;
}
