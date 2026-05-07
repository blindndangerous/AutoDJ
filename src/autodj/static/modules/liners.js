// Voice liners -- Settings panel, file list, upload / delete / test,
// trigger evaluation, scheduler.
//
// Audio playback (Web Audio decode + duck the active deck) lives in
// the audio-engine module and is injected via deps.playLiner so this
// module stays free of AudioContext + decks state.

import { escHtml, dbg } from "./dom-helpers.js";
import { clearLiveRegionLater } from "./live-region.js";
import { applyShowWhen } from "./show-when.js";

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

async function _refreshLibrary(els) {
  try {
    const resp = await fetch("/api/liners");
    if (!resp.ok) return;
    const body = await resp.json();
    state.lib = body;
    if (els.lnFolderDisplay) {
      els.lnFolderDisplay.textContent = "Folder: " + (body.folder || "—");
    }
    if (els.lnFileList) {
      els.lnFileList.innerHTML = "";
      for (const name of body.files || []) {
        const li = document.createElement("li");
        const text = document.createElement("span");
        text.textContent = name;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.innerHTML = '<span aria-hidden="true">Delete</span>' +
          `<span class="visually-hidden"> ${escHtml(name)}</span>`;
        btn.addEventListener("click", () => _deleteLiner(els, name));
        li.appendChild(text);
        li.appendChild(document.createTextNode(" "));
        li.appendChild(btn);
        els.lnFileList.appendChild(li);
      }
    }
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
  } catch (err) {
    dbg("liner refresh failed:", err);
  }
}

async function _deleteLiner(els, name) {
  if (!confirm(`Delete liner "${name}"?`)) return;
  try {
    const resp = await fetch(
      `/api/liners/file/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    if (!resp.ok) {
      _setStatus(els, `Delete failed: HTTP ${resp.status}`);
      return;
    }
    _setStatus(els, `Deleted ${name}`);
    await _refreshLibrary(els);
  } catch (err) {
    _setStatus(els, `Delete failed: ${err.message}`);
  }
}

function _postConfig(els, postSettings) {
  postSettings("/api/playback-settings", {
    liners_enabled:            !!(els.lnEnabled && els.lnEnabled.checked),
    liners_every_n_songs:      _intOrNull(els.lnEveryN),
    liners_every_minutes:      _floatOrNull(els.lnEveryMin),
    liners_random_min_minutes: _floatOrNull(els.lnRandMin),
    liners_random_max_minutes: _floatOrNull(els.lnRandMax),
    liners_pick_mode:          els.lnPickMode ? els.lnPickMode.value : "random",
    liners_duck_db:            _floatOrNull(els.lnDuckDb),
  });
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
  try {
    const resp = await fetch(`/api/liners/file/${encodeURIComponent(name)}`);
    if (!resp.ok) {
      _setStatus(els, `Liner fetch failed: HTTP ${resp.status}`);
      return;
    }
    const buf = await resp.arrayBuffer();
    const duckDb = (state.lib.config && state.lib.config.duck_db) || -12;
    const ok = await deps.playLiner(buf, duckDb);
    if (!ok) {
      _setStatus(els, "Liner playback skipped (audio context not ready).");
      return;
    }
    state.lastFireAt   = _now();
    state.trackCount   = 0;
    state.randomTarget = _rollRandomTarget();
    _setStatus(els, `Liner playing: ${name}`);
  } catch (err) {
    _setStatus(els, `Liner playback failed: ${err.message}`);
  }
}

export function installLiners(els, deps) {
  state.lastFireAt = _now();

  if (els.lnUploadSubmit) {
    els.lnUploadSubmit.addEventListener("click", async () => {
      if (!els.lnUpload || !els.lnUpload.files || els.lnUpload.files.length === 0) {
        _setStatus(els, "Pick a file first.");
        return;
      }
      const f = els.lnUpload.files[0];
      const fd = new FormData();
      fd.append("file", f, f.name);
      _setStatus(els, `Uploading ${f.name}...`);
      try {
        const resp = await fetch("/api/liners/upload", { method: "POST", body: fd });
        if (!resp.ok) {
          const detail = await resp.text();
          _setStatus(els, `Upload failed: ${detail}`);
          return;
        }
        _setStatus(els, `Uploaded ${f.name}`);
        els.lnUpload.value = "";
        await _refreshLibrary(els);
      } catch (err) {
        _setStatus(els, `Upload failed: ${err.message}`);
      }
    });
  }

  for (const el of [
    els.lnEnabled, els.lnEveryN, els.lnEveryMin,
    els.lnRandMin, els.lnRandMax, els.lnPickMode, els.lnDuckDb,
  ]) {
    if (!el) continue;
    el.addEventListener("change", () => _postConfig(els, deps.postSettings));
  }

  if (els.lnTestBtn) {
    els.lnTestBtn.addEventListener("click", async () => {
      const name = _pickLiner();
      if (!name) {
        _setStatus(els, "No liner files in folder.");
        return;
      }
      await _playByName(els, deps, name);
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
    if (fire) {
      const name = _pickLiner();
      if (name) _playByName(els, deps, name);
    }
  }, 1000);

  // Initial fetch + reapply hidden state on load.
  _refreshLibrary(els);
}

// Bumps the every_n_songs counter when the WS state surfaces a new
// current_track path.  One hook covers every advance route.
export function bumpLinerTrackCount(s) {
  const cur = (s && s.current_track && s.current_track.path) || null;
  if (cur && cur !== state.lastSeenPath) {
    if (state.lastSeenPath !== null) state.trackCount += 1;
    state.lastSeenPath = cur;
  }
}
