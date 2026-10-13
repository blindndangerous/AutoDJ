// Voice liners -- Settings panel, file list, upload / delete / test,
// trigger evaluation, scheduler.
//
// Audio playback (Web Audio decode + duck the active deck) lives in
// app.js and is injected via deps.playLiner so this module stays free
// of AudioContext + decks state.

import { confirmAction } from "./confirm-dialog.js";
import { dbg, listRow, nextRowControl, rowButton, stationDay } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { parseSettingValue, refusalText } from "./mix-settings.js";
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

// The range the server accepts for liners_duck_db: 0 is no drop, minus
// 30 nearly silent.
const DUCK_DB = { label: "Duck depth", min: -30, max: 0, integer: false };

function _setStatus(els, msg, { force = false, tone = "info", region = els.lnStatus } = {}) {
  if (!region) return;
  region.classList.remove("visually-hidden");
  announceStatus(region, msg, { dwellMs: tone === "error" ? 6000 : 4000, force, tone });
}

// One liner in words: its name, its play dates, and why it is skipped now.
function linerText(liner) {
  const parts = [liner.name];
  if (liner.dates_text) parts.push(liner.dates_text);
  if (liner.playable === false) parts.push(`Skipped now: ${liner.unplayable_text}`);
  return parts.join(". ");
}

export function renderLinerFileList(fileList, files, onDelete, onEdit = (path, kind, opener) => {
  fileList.ownerDocument.dispatchEvent(new CustomEvent("autodj:edit-points", {
    detail: { path, kind, opener },
  }));
}, onDates = (liner, opener) => {
  fileList.ownerDocument.dispatchEvent(new CustomEvent("autodj:liner-dates", {
    detail: { liner, opener },
  }));
}) {
  if (!fileList) return;
  if (!files || files.length === 0) {
    const empty = document.createElement("li");
    empty.className = "no-results";
    empty.textContent = "No liner files yet.";
    fileList.replaceChildren(empty);
    return;
  }
  fileList.replaceChildren(...files.map((item) => {
    const legacyName = typeof item === "string";
    const liner = legacyName ? { id: item, name: item, path: null } : item;
    const buttons = [rowButton(document, "Delete", `Delete ${liner.name}`, (button) =>
      legacyName ? onDelete(liner.id, button) : onDelete(liner.id, button, liner.name))];
    if (liner.path && liner.editable !== false) buttons.push(rowButton(document, "Track info", `Track info for ${liner.name}`, (button) => onEdit(liner.path, "liner", button)));
    if (!legacyName) {
      const dates = rowButton(document, "Play dates", `Play dates for ${liner.name}`, (button) => onDates(liner, button));
      dates.dataset.linerDates = liner.id;
      buttons.push(dates);
    }
    const row = listRow(document, legacyName ? liner.name : linerText(liner), "", ...buttons);
    row.dataset.linerId = liner.id;
    return row;
  }));
}

// Where focus goes when the list has no liner left: the list's heading,
// which takes focus from script only; the list itself without one.
function _listHeading(list) {
  const id = list.closest("section")?.getAttribute("aria-labelledby");
  const heading = id ? list.ownerDocument.getElementById(id) : null;
  if (!heading) return list;
  if (!heading.hasAttribute("tabindex")) heading.tabIndex = -1;
  return heading;
}

// The station's day changed while the page was open: read the list again
// and change only the words of each row ("Skipped now: expired after ..."),
// so focus stays where it is and nothing is announced.  A list whose liners
// changed meanwhile is drawn again, as a refresh draws it.
async function _followStationDay(els, now = new Date()) {
  const day = stationDay(state.lib.timezone, now);
  if (!day || !state.lib.today || day === state.lib.today || !els.lnFileList) return;
  // Asked once per day change, even when the request fails.
  state.lib.today = day;
  const epoch = captureAuthenticatedRequestEpoch();
  let body;
  try {
    body = await requestJson("/api/liners");
  } catch (err) {
    dbg("liner day refresh failed:", err);
    return;
  }
  if (!isAuthenticatedRequestCurrent(epoch)) return;
  const items = body.items || [];
  const rows = [...els.lnFileList.querySelectorAll(":scope > li[data-liner-id]")];
  const same = rows.length === items.length && rows.every((row, i) => row.dataset.linerId === items[i].id);
  if (!same) {
    // The liners changed meanwhile: draw the list again, then put focus back
    // on the same button of the same liner (or the next row's), so a
    // midnight redraw never drops it.
    const focused = els.lnFileList.contains(document.activeElement) ? document.activeElement : null;
    const row = focused?.closest("li[data-liner-id]");
    const label = focused?.getAttribute("aria-label");
    const index = focused ? [...els.lnFileList.querySelectorAll("button")].indexOf(focused) : -1;
    await _loadLibrary(els, epoch);
    if (!focused || focused.isConnected) return;
    const again = row && [...els.lnFileList.querySelectorAll(":scope > li[data-liner-id]")]
      .find((item) => item.dataset.linerId === row.dataset.linerId);
    const target = (again && [...again.querySelectorAll("button")].find((b) => b.getAttribute("aria-label") === label))
      || nextRowControl(els.lnFileList, "button", Math.max(index, 0), null)
      || _listHeading(els.lnFileList);
    target?.focus();
    return;
  }
  state.lib = body;
  rows.forEach((row, i) => {
    const text = linerText(items[i]);
    const span = row.firstElementChild;
    if (span && span.textContent !== text) span.textContent = text;
  });
}

// Loads the liner folder and config into the page.  False when the
// sign-in changed meanwhile; a failed request throws.
async function _loadLibrary(els, epoch) {
  const body = await requestJson("/api/liners");
  if (!isAuthenticatedRequestCurrent(epoch)) return false;
  state.lib = body;
  if (els.lnFolderDisplay) {
    els.lnFolderDisplay.textContent = "Folder: " + (body.folder || "—");
  }
  renderLinerFileList(
    els.lnFileList,
    body.items?.length ? body.items : body.files,
    (id, button, label) => void _deleteLiner(els, id, button, label),
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
  return true;
}

async function _refreshLibrary(els) {
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    await _loadLibrary(els, epoch);
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    dbg("liner refresh failed:", err);
    _setStatus(els, `Could not load liners: ${err.message}`);
  }
}

// The delete runs while the confirmation is still open, so the list is
// already updated when it closes and focus goes straight to the next
// row's Delete, or the list itself when none is left (see confirmAction).
// The result is said after the dialog has closed, in the status region
// beside the list (Queue & Search), since the Settings one is hidden there.
async function _deleteLiner(els, name, button, label = name) {
  const list = els.lnFileList;
  const region = els.lnBrowseStatus || els.lnStatus;
  const index = Array.from(list.querySelectorAll("button")).indexOf(button);
  const epoch = captureAuthenticatedRequestEpoch();
  let result = null;
  const confirmed = await confirmAction(button.ownerDocument, {
    title: `Delete liner ${label}?`,
    message: "The file is removed from the liners folder.",
    confirmLabel: "Delete liner",
    onConfirm: async () => {
      let reply = null;
      try {
        reply = await requestJson(`/api/liners/file/${encodeURIComponent(name)}`, { method: "DELETE" });
      } catch (err) {
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        result = `Delete failed: ${err.message}`;
        return button;
      }
      if (!isAuthenticatedRequestCurrent(epoch)) return null;
      try {
        if (!(await _loadLibrary(els, epoch))) return null;
      } catch (err) {
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        result = `Deleted ${label}, but could not load liners: ${err.message}`;
        return button;
      }
      // A delete whose play dates could not be forgotten says so.
      result = reply?.message ? `Deleted ${label}. ${reply.message}.` : `Deleted ${label}`;
      return nextRowControl(list, "button", index, list);
    },
  });
  if (!confirmed) {
    button.focus();
    return;
  }
  if (result && isAuthenticatedRequestCurrent(epoch)) _setStatus(els, result, { region });
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

// Whether a liner may play on *day* (ISO dates compare as text); without a
// day, the server's word when the list was loaded.
function linerPlayable(item, day) {
  if (!day) return item.playable !== false;
  if (item.not_after && day > item.not_after) return false;
  return !(item.not_before && day < item.not_before);
}

function _pickLiner() {
  // A liner outside its play dates is skipped, as the server skips it.
  const day = stationDay(state.lib.timezone);
  const items = state.lib.items?.length
    ? state.lib.items.filter((item) => linerPlayable(item, day))
    : (state.lib.files || []).map((name) => ({ id: name, name }));
  if (items.length === 0) return null;
  const mode = (state.lib.config && state.lib.config.pick_mode) || "random";
  if (mode === "sequential") {
    const i = (state.seqCursor++) % items.length;
    return items[i].id;
  }
  const i = Math.floor(Math.random() * items.length);
  return items[i].id;
}

function _linerLabel(id) {
  return state.lib.items?.find((item) => item.id === id)?.name || id;
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
    const duckDb = state.lib.config?.duck_db ?? -12;
    const ok = await deps.playLiner(buf, duckDb, epoch);
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (!ok) {
      _setStatus(els, "Liner playback skipped (audio context not ready).");
      return;
    }
    state.lastFireAt   = performance.now();
    state.trackCount   = 0;
    state.randomTarget = _rollRandomTarget();
    _setStatus(els, `Liner playing: ${_linerLabel(name)}`);
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
        ? `Liner playing: ${_linerLabel(body.played)}`
        : (body && body.message) || `Could not play ${_linerLabel(name)}.`,
      { force: true },
    );
  } catch (err) {
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    _setStatus(els, `Liner playback failed: ${err.message}`, { force: true });
  }
}

// The Play dates form: one liner's "not before" and "do not play after".
function _installDatesForm(els, doc = document) {
  const form = doc.getElementById("ln-dates-form");
  if (!form) return;
  const legend = doc.getElementById("ln-dates-legend");
  const errorText = doc.getElementById("ln-dates-error");
  const notBefore = doc.getElementById("ln-not-before");
  const notAfter = doc.getElementById("ln-not-after");
  const fields = { not_before: notBefore, not_after: notAfter };
  let editing = null;
  let opener = null;
  const region = () => els.lnBrowseStatus || els.lnStatus;
  const say = (message, tone = "info") => _setStatus(els, message, { force: true, tone, region: region() });

  // The error text joins or leaves a field's own description.
  function linkError(control, linked) {
    const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== errorText.id);
    if (linked) ids.push(errorText.id);
    control.setAttribute("aria-describedby", ids.join(" "));
  }

  function clearError() {
    errorText.hidden = true;
    errorText.textContent = "";
    for (const control of Object.values(fields)) {
      control.removeAttribute("aria-invalid");
      linkError(control, false);
    }
  }

  function showError(field, message) {
    clearError();
    const control = fields[field] || notBefore;
    errorText.textContent = message;
    errorText.hidden = false;
    control.setAttribute("aria-invalid", "true");
    linkError(control, true);
    control.focus();
    say(message, "error");
  }

  function datesButton(id) {
    return [...els.lnFileList.querySelectorAll("[data-liner-dates]")]
      .find((button) => button.dataset.linerDates === id) || null;
  }

  function close(focusId) {
    form.hidden = true;
    editing = null;
    clearError();
    const back = (focusId && datesButton(focusId)) || (opener?.isConnected ? opener : els.lnFileList);
    back?.focus();
  }

  doc.addEventListener("autodj:liner-dates", (event) => {
    const { liner, opener: from } = event.detail;
    editing = liner;
    opener = from;
    clearError();
    legend.textContent = `Play dates for ${liner.name}`;
    notBefore.value = liner.not_before || "";
    notAfter.value = liner.not_after || "";
    form.hidden = false;
    notBefore.focus();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!editing) return;
    clearError();
    if (notBefore.value && notAfter.value && notAfter.value < notBefore.value) {
      showError("not_after", "Do not play after must be on or after the Not before date");
      return;
    }
    const liner = editing;
    const epoch = captureAuthenticatedRequestEpoch();
    let saved;
    try {
      saved = await requestJson(`/api/liners/dates/${encodeURIComponent(liner.id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ not_before: notBefore.value || null, not_after: notAfter.value || null }),
      });
    } catch (err) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      if (err.field && fields[err.field]) showError(err.field, err.message);
      else say(`Could not save the play dates of ${liner.name}: ${err.message}`, "error");
      return;
    }
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    try {
      await _loadLibrary(els, epoch);
    } catch (err) {
      dbg("liner refresh failed:", err);
    }
    close(liner.id);
    say(saved.dates_text
      ? `Saved: ${linerText({ ...saved, name: liner.name })}.`
      : `Saved: ${liner.name} plays on any day.`);
  });

  doc.getElementById("ln-dates-cancel")?.addEventListener("click", () => {
    const id = editing?.id;
    close(id);
    say("Cancelled. The play dates did not change.");
  });
  for (const control of Object.values(fields)) {
    control.addEventListener("input", () => {
      control.removeAttribute("aria-invalid");
      linkError(control, false);
    });
  }
}

export function installLiners(els, deps) {
  state.lastFireAt = performance.now();
  let scheduledPlayback = null;
  if (els.lnFileList) _installDatesForm(els, els.lnFileList.ownerDocument);

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
      // A depth outside the range goes back to the saved one, and says so.
      if (el === els.lnDuckDb && parseSettingValue(el.value, DUCK_DB) === null) {
        el.value = String(state.lib.config?.duck_db ?? -12);
        _setStatus(els, refusalText(DUCK_DB, el.value), { force: true, tone: "error" });
        return;
      }
      _postConfig(els, deps.postSettings, event.currentTarget);
    });
  }

  if (els.lnTestBtn) {
    els.lnTestBtn.addEventListener("click", async (event) => {
      const name = _pickLiner();
      if (!name) {
        _setStatus(els, state.lib.items?.length
          ? "Every liner is outside its play dates, so Test has nothing to play."
          : "No liner files in folder.");
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

  // "Skipped now" follows the station's day on a page left open past midnight.
  setInterval(() => void _followStationDay(els), 60_000);

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
