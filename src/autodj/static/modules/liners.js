// Voice liners -- Settings panel, file list, upload / delete / test,
// trigger evaluation, scheduler.
//
// Audio playback (Web Audio decode + duck the active deck) lives in
// the audio-engine module and is injected via deps.playLiner so this
// module stays free of AudioContext + decks state.

import { dbg } from "./dom-helpers.js";
import { clearLiveRegionLater } from "./live-region.js";
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

// Initialise lastFireAt lazily on first use so SSR / test environments
// without `performance` do not crash at module-eval time.
function _now() {
  return typeof performance !== "undefined" && performance.now
    ? performance.now()
    : Date.now();
}

function _intOrNull(el) {
  if (!el || el.value === "" || el.value == null) return null;
  const n = parseInt(el.value, 10);
  return isNaN(n) ? null : n;
}

function _floatOrNull(el) {
  if (!el || el.value === "" || el.value == null) return null;
  const n = parseFloat(el.value);
  return isNaN(n) ? null : n;
}

function _setStatus(els, msg) {
  if (els.lnStatus) {
    els.lnStatus.classList.remove("visually-hidden");
    els.lnStatus.textContent = msg;
    clearLiveRegionLater(els.lnStatus, 4000);
  }
}

export function renderLinerFileList(fileList, files, onDelete) {
  if (!fileList) return;
  fileList.replaceChildren();
  for (const name of files || []) {
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
    fileList.appendChild(li);
  }
}

async function _refreshLibrary(els) {
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
    const refreshed = await _refreshLibrary(els);
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (!refreshed) {
      const connected = (candidate) => candidate?.isConnected && !candidate.disabled;
      const next = controls.slice(deletedIndex + 1).find(connected);
      const previous = controls.slice(0, deletedIndex).reverse().find(connected);
      const stableTarget = connected(control)
        ? control
        : next || previous || (connected(els.lnUploadSubmit) ? els.lnUploadSubmit : null);
      if (stableTarget) {
        stableTarget.focus();
      } else if (els.lnFileList) {
        els.lnFileList.setAttribute("tabindex", "-1");
        els.lnFileList.focus();
      }
      return;
    }
    const remaining = els.lnFileList
      ? Array.from(els.lnFileList.querySelectorAll("button"))
      : [];
    const focusTarget = remaining.length
      ? remaining[Math.min(deletedIndex, remaining.length - 1)]
      : els.lnUploadSubmit;
    focusTarget?.focus();
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    _setStatus(els, `Delete failed: ${err.message}`);
    control?.focus();
  }
}

function _postConfig(els, postSettings, control) {
  void postSettings("/api/playback-settings", {
    liners_enabled:            !!(els.lnEnabled && els.lnEnabled.checked),
    liners_every_n_songs:      _intOrNull(els.lnEveryN),
    liners_every_minutes:      _floatOrNull(els.lnEveryMin),
    liners_random_min_minutes: _floatOrNull(els.lnRandMin),
    liners_random_max_minutes: _floatOrNull(els.lnRandMax),
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
  // weighted falls back to random in the browser since weights are
  // not persisted yet; matches LinerLibrary.pick fallback behaviour.
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

async function _playByName(els, deps, name) {
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    if (!deps.canPlay()) return;
    const buf = await requestBinary(`/api/liners/file/${encodeURIComponent(name)}`);
    if (!deps.canPlay() || !isAuthenticatedRequestCurrent(epoch)) return;
    const duckDb = (state.lib.config && state.lib.config.duck_db) || -12;
    const ok = await deps.playLiner(buf, duckDb, epoch);
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (!ok) {
      _setStatus(els, "Liner playback skipped (audio context not ready).");
      return;
    }
    state.lastFireAt   = _now();
    state.trackCount   = 0;
    state.randomTarget = _rollRandomTarget();
    _setStatus(els, `Liner playing: ${name}`);
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    _setStatus(els, `Liner playback failed: ${err.message}`);
  }
}

export function installLiners(els, deps) {
  state.lastFireAt = _now();
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
      _setStatus(els, `Uploading ${f.name}...`);
      const epoch = captureAuthenticatedRequestEpoch();
      try {
        await withDisabled(event.currentTarget, () => requestJson(
          "/api/liners/upload", { method: "POST", body: fd },
        ));
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        _setStatus(els, `Uploaded ${f.name}`);
        els.lnUpload.value = "";
        await _refreshLibrary(els);
      } catch (err) {
        if (!isAuthenticatedRequestCurrent(epoch)) return;
        _setStatus(els, `Upload failed: ${err.message}`);
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
      await withDisabled(event.currentTarget, () => _playByName(els, deps, name));
    });
  }

  // Periodic trigger evaluation -- once per second.
  setInterval(() => {
    if (!state.lib.config || !state.lib.config.enabled) return;
    if (!deps.canPlay()) return;
    const c = state.lib.config;
    const minsSince = (_now() - state.lastFireAt) / 60000;
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

export function getLinerTrackCountForTest() {
  return state.trackCount;
}

export function resetLinerStateForTest() {
  state.trackCount = 0;
  state.lastSeenPath = null;
}
