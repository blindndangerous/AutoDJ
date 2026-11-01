// Voice liners -- Settings panel, file list, upload / delete / test,
// trigger evaluation, scheduler.
//
// Audio playback (Web Audio decode + duck the active deck) lives in
// app.js and is injected via deps.playLiner so this module stays free
// of AudioContext + decks state.

import { createCollectionTable } from "./collection-table.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { createEditorDialog } from "./editor-dialog.js";
import { applyServerFieldError, clearFieldError, dbg, sentences, showFieldError, stationDay } from "./dom-helpers.js";
import { tableFocusTarget } from "./table-row.js";
import { makeSay } from "./live-region.js";
import { parseSettingValue, refusalText } from "./mix-settings.js";
import { registerSettingsGroup, settingsDraftHolds } from "./settings-drafts.js";
import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  isStaleStationReply,
  requestBinary,
  requestJson,
  failureText,
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
// Bumped by a station switch: a load started before it writes nothing.
let libraryGeneration = 0;

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

const DUCK_DB = { label: "Music duck", min: 0, max: 30, integer: false };

function _setStatus(els, msg, { force = false, tone = "info", region = els.lnStatus } = {}) {
  if (!region) return;
  region.classList.remove("visually-hidden");
  makeSay(region, { dwellMs: 4000, errorDwellMs: 6000, force })(msg, tone);
}

// One liner in words: its name, its play dates, and why it is skipped now.
function linerText(liner) {
  const parts = [liner.name];
  if (liner.dates_text) parts.push(liner.dates_text);
  if (liner.playable === false) parts.push(`Skipped now: ${liner.unplayable_text}`);
  return parts.join(". ");
}

const linerCollections = new WeakMap();

export function renderLinerFileList(fileList, files, onDelete, onEdit = (path, kind, opener) => {
  fileList.ownerDocument.dispatchEvent(new CustomEvent("autodj:edit-points", {
    detail: { path, kind, opener },
  }));
}, onDates = (liner, opener) => {
  fileList.ownerDocument.dispatchEvent(new CustomEvent("autodj:liner-dates", {
    detail: { liner, opener },
  }));
}, { status, announceFocus = true } = {}) {
  if (!fileList) return;
  let entry = linerCollections.get(fileList);
  if (!entry) {
    entry = {};
    entry.table = createCollectionTable({
      host: fileList, label: "Liners", status, fallback: _listHeading(fileList),
      emptyText: "No liner files yet.", keyOf: (liner) => liner.id, nameOf: (liner) => liner.name,
      columns: [
        { key: "name", label: "Name", value: (liner) => liner.name },
        { key: "details", label: "Details", value: (liner) => sentences(liner.dates_text || "Any day",
          liner.playable === false && `Skipped now: ${liner.unplayable_text}`) },
      ],
      actions: (liner) => [
        { id: "dates", label: "Play dates",
          disabledReason: liner.legacy ? "Play dates are unavailable for this liner." : "",
          run: (button) => entry.onDates(liner, button) },
        { id: "info", label: "Track info",
          disabledReason: !liner.path || liner.editable === false ? "Track info is unavailable for this liner." : "",
          run: (button) => entry.onEdit(liner.path, "liner", button) },
        { id: "delete", label: "Delete", run: (button) => liner.legacy
          ? entry.onDelete(liner.id, button) : entry.onDelete(liner.id, button, liner.name) },
      ],
    });
    linerCollections.set(fileList, entry);
  }
  Object.assign(entry, { onDelete, onEdit, onDates });
  const items = (files || []).map((item) => typeof item === "string"
    ? { id: item, name: item, path: null, legacy: true } : item);
  return entry.table.update(items, { announceFocus });
}

// Where focus goes when the list has no liner left: the list's heading,
// which takes focus from script only; the list itself without one.
function _listHeading(list) {
  const id = list.getAttribute("aria-labelledby")
    || list.closest("section")?.getAttribute("aria-labelledby");
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
  state.lib = body;
  renderLinerFileList(els.lnFileList, body.items?.length ? body.items : body.files,
    (id, button, label) => void _deleteLiner(els, id, button, label), undefined, undefined,
    { status: els.lnBrowseStatus || els.lnStatus });
}

// Loads the liner folder and config into the page.  False when the
// sign-in changed meanwhile; a failed request throws.
async function _loadLibrary(els, epoch) {
  const mine = libraryGeneration;
  const body = await requestJson("/api/liners");
  if (!isAuthenticatedRequestCurrent(epoch) || mine !== libraryGeneration) return false;
  state.lib = body;
  if (els.lnFolderDisplay) {
    els.lnFolderDisplay.textContent = "Folder: " + (body.folder || "—");
  }
  renderLinerFileList(
    els.lnFileList,
    body.items?.length ? body.items : body.files,
    (id, button, label) => void _deleteLiner(els, id, button, label), undefined, undefined,
    { status: els.lnBrowseStatus || els.lnStatus },
  );
  // Sync config inputs from server payload, leaving fields the user
  // is currently editing untouched.
  const c = body.config || {};
  const sync = (el, v) => {
    if (el && document.activeElement !== el && !settingsDraftHolds(el)) el.value = v;
  };
  if (els.lnEnabled && document.activeElement !== els.lnEnabled && !settingsDraftHolds(els.lnEnabled)) {
    els.lnEnabled.checked = !!c.enabled;
  }
  sync(els.lnEveryN,    c.every_n_songs        != null ? c.every_n_songs        : "");
  sync(els.lnEveryMin,  c.every_minutes        != null ? c.every_minutes        : "");
  sync(els.lnRandMin,   c.random_min_minutes   != null ? c.random_min_minutes   : "");
  sync(els.lnRandMax,   c.random_max_minutes   != null ? c.random_max_minutes   : "");
  sync(els.lnPickMode,  c.pick_mode || "random");
  sync(els.lnDuckDb,    c.duck_db != null ? Math.abs(c.duck_db) : 3);
  return true;
}

// A station switch (#70): the liners, their settings and the cadence were
// the old station's, so none of them shows or fires under the new one.
function _clearLibrary(els) {
  libraryGeneration += 1;
  state.lib = { folder: "", files: [], config: {} };
  state.trackCount = 0;
  state.randomTarget = null;
  state.seqCursor = 0;
  state.lastSeenPath = null;
  if (els.lnFileList) {
    linerCollections.get(els.lnFileList)?.table.clear();
    linerCollections.delete(els.lnFileList);
    els.lnFileList.replaceChildren();
  }
  for (const region of [els.lnFolderDisplay, els.lnStatus, els.lnBrowseStatus]) {
    if (region) region.textContent = "";
  }
}

async function _refreshLibrary(els) {
  const epoch = captureAuthenticatedRequestEpoch();
  try {
    await _loadLibrary(els, epoch);
  } catch (err) {
    // A reply for the station the page has just left says nothing (#70).
    if (!isAuthenticatedRequestCurrent(epoch) || isStaleStationReply(err)) return;
    dbg("liner refresh failed:", err);
    _setStatus(els, failureText("load liners", err));
  }
}

// The delete runs while the confirmation is still open, so the list is
// already updated when it closes and focus goes straight to the next
// row's Actions cell, or the list itself when none is left (see confirmAction).
// The result is said after the dialog has closed, in the status region
// beside the list (Queue & Search), since the Settings one is hidden there.
async function _deleteLiner(els, name, button, label = name) {
  const list = els.lnFileList;
  const region = els.lnBrowseStatus || els.lnStatus;
  const collection = linerCollections.get(list)?.table;
  const index = [...collection.body.children].indexOf(button.closest("tr"));
  const epoch = captureAuthenticatedRequestEpoch();
  let result = null;
  const confirmed = await deleteCollectionItem(button.ownerDocument, {
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
        return tableFocusTarget(button);
      }
      if (!isAuthenticatedRequestCurrent(epoch)) return null;
      try {
        if (!(await _loadLibrary(els, epoch))) return null;
      } catch (err) {
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        result = `Deleted ${label}, but could not load liners: ${err.message}`;
        return tableFocusTarget(button);
      }
      // A delete whose play dates could not be forgotten says so.
      result = reply?.message ? `Deleted ${label}. ${reply.message}.` : `Deleted ${label}`;
      const rows = [...collection.body.querySelectorAll("tr[data-row-key]")];
      const next = rows[Math.min(index, rows.length - 1)];
      return collection.target(next?.dataset.rowKey, "options") || _listHeading(list);
    },
  });
  if (!confirmed) {
    tableFocusTarget(button).focus();
    return;
  }
  if (result && isAuthenticatedRequestCurrent(epoch)) _setStatus(els, result, { region });
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
    const duckDb = state.lib.config?.duck_db ?? -3;
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
  const region = () => editor.region(els.lnBrowseStatus || els.lnStatus);
  const say = makeSay(region, { dwellMs: 4000, errorDwellMs: 6000 });
  const clearError = () => clearFieldError(Object.values(fields), errorText);
  function showError(field, message) {
    clearError();
    showFieldError(fields[field] || notBefore, message, errorText, editor.status);
  }
  function close() {
    editor.close({ restore: true });
    editing = null;
    clearError();
  }
  const editor = createEditorDialog({
    form, heading: legend, cancel: doc.getElementById("ln-dates-cancel"),
    fallback: _listHeading(els.lnFileList), save: saveDates, pendingText: null,
    onCancel: () => {
      editing = null;
      clearError();
      say("Cancelled. The play dates did not change.");
    },
  });

  doc.addEventListener("autodj:liner-dates", (event) => {
    const { liner, opener: from } = event.detail;
    if (!form.isConnected || !els.lnFileList.contains(from)) return;
    editing = liner;
    clearError();
    legend.textContent = `Play dates for ${liner.name}`;
    notBefore.value = liner.not_before || "";
    notAfter.value = liner.not_after || "";
    editor.open(notBefore, from);
  });

  async function saveDates(_event, current) {
    if (!editing) return;
    clearError();
    // A half-typed date reads as empty, and a five-digit year reaches the server.
    for (const [field, input, label] of [["not_before", notBefore, "Not before"], ["not_after", notAfter, "Do not play after"]]) {
      if (input.validity?.badInput || (input.value && !/^\d{4}-\d{2}-\d{2}$/.test(input.value))) {
        showError(field, `Enter ${label} as a full date, or leave it empty.`);
        return;
      }
    }
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
      if (!current()) return;
      if (!applyServerFieldError(err, fields, errorText, editor.status)) {
        say(failureText(`save the play dates of ${liner.name}`, err), "error");
      }
      return;
    }
    if (!current()) return;
    try {
      await _loadLibrary(els, epoch);
    } catch (err) {
      dbg("liner refresh failed:", err);
    }
    if (!current()) return;
    close();
    say(saved.dates_text
      ? `Saved: ${linerText({ ...saved, name: liner.name })}.`
      : `Saved: ${liner.name} plays on any day.`);
  }

  for (const control of Object.values(fields)) {
    control.addEventListener("input", () => clearFieldError(control, errorText));
  }
  return editor;
}

export function installLiners(els, deps) {
  state.lastFireAt = performance.now();
  let scheduledPlayback = null;
  const controls = [els.lnEnabled, els.lnMatchedIntroOnlyWithoutLiner,
    els.lnEveryN, els.lnEveryMin, els.lnRandMin, els.lnRandMax,
    els.lnPickMode, els.lnDuckDb, els.lnTalkUp].filter(Boolean);
  const panel = controls[0]?.closest("#spanel-liners");
  const read = () => ({
    liners_enabled: Boolean(els.lnEnabled?.checked),
    matched_intro_only_without_liner: Boolean(els.lnMatchedIntroOnlyWithoutLiner?.checked),
    liners_every_n_songs: _intOrOff(els.lnEveryN),
    liners_every_minutes: _floatOrOff(els.lnEveryMin),
    liners_random_min_minutes: _floatOrOff(els.lnRandMin),
    liners_random_max_minutes: _floatOrOff(els.lnRandMax),
    liners_pick_mode: els.lnPickMode?.value || "random",
    liners_duck_db: _floatOrNull(els.lnDuckDb) === null ? null : -_floatOrNull(els.lnDuckDb),
    liners_talk_up: Boolean(els.lnTalkUp?.checked),
  });
  registerSettingsGroup({
    panel, group: "playback", controls, read,
    fields: {
      liners_enabled: els.lnEnabled, matched_intro_only_without_liner: els.lnMatchedIntroOnlyWithoutLiner,
      liners_every_n_songs: els.lnEveryN, liners_every_minutes: els.lnEveryMin,
      liners_random_min_minutes: els.lnRandMin, liners_random_max_minutes: els.lnRandMax,
      liners_pick_mode: els.lnPickMode, liners_duck_db: els.lnDuckDb, liners_talk_up: els.lnTalkUp,
    },
    write(value) {
      const c = state.lib.config || (state.lib.config = {});
      const fields = [
        ["liners_enabled", els.lnEnabled, "checked", "enabled"],
        ["matched_intro_only_without_liner", els.lnMatchedIntroOnlyWithoutLiner, "checked", null],
        ["liners_every_n_songs", els.lnEveryN, "value", "every_n_songs"],
        ["liners_every_minutes", els.lnEveryMin, "value", "every_minutes"],
        ["liners_random_min_minutes", els.lnRandMin, "value", "random_min_minutes"],
        ["liners_random_max_minutes", els.lnRandMax, "value", "random_max_minutes"],
        ["liners_pick_mode", els.lnPickMode, "value", "pick_mode"],
        ["liners_duck_db", els.lnDuckDb, "value", "duck_db"],
        ["liners_talk_up", els.lnTalkUp, "checked", null],
      ];
      for (const [key, control, prop, configKey] of fields) {
        if (!(key in value) || !control) continue;
        const item = value[key];
        control[prop] = prop === "value" && key === "liners_duck_db"
          ? String(Math.abs(item ?? -3)) : prop === "value" ? String(item ?? "") : Boolean(item);
        if (configKey) c[configKey] = item;
      }
      c.enabled = value.liners_enabled ?? c.enabled;
      c.every_n_songs = value.liners_every_n_songs ?? c.every_n_songs;
      c.every_minutes = value.liners_every_minutes ?? c.every_minutes;
      c.random_min_minutes = value.liners_random_min_minutes ?? c.random_min_minutes;
      c.random_max_minutes = value.liners_random_max_minutes ?? c.random_max_minutes;
      c.pick_mode = value.liners_pick_mode ?? c.pick_mode;
      if ("liners_duck_db" in value) c.duck_db = value.liners_duck_db;
    },
    validate() {
      const error = controls[0]?.ownerDocument.getElementById("ln-settings-error");
      clearFieldError(controls, error);
      const fields = [
        [els.lnEveryN, { label: "Songs between liners", min: 0, max: 500, integer: true }],
        [els.lnEveryMin, { label: "Minutes between liners", min: 0, max: 240 }],
        [els.lnRandMin, { label: "Shortest random interval", min: 0, max: 240 }],
        [els.lnRandMax, { label: "Longest random interval", min: 0, max: 240 }],
        [els.lnDuckDb, DUCK_DB],
      ];
      for (const [control, spec] of fields) {
        if (!control || control.value === "") continue;
        if (parseSettingValue(control.value, spec) !== null) continue;
        showFieldError(control, refusalText(spec, control.value), error, els.lnStatus);
        return false;
      }
      if (_floatOrNull(els.lnRandMin) != null && _floatOrNull(els.lnRandMax) != null
          && _floatOrNull(els.lnRandMin) > _floatOrNull(els.lnRandMax)) {
        showFieldError(els.lnRandMax, "Longest random interval must be at least the shortest interval.", error, els.lnStatus);
        return false;
      }
      return true;
    },
  });
  for (const control of controls) {
    control.addEventListener("input", () => clearFieldError(control, control.ownerDocument.getElementById("ln-settings-error")));
  }
  const datesEditor = els.lnFileList && _installDatesForm(els, els.lnFileList.ownerDocument);

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
        // A file that is not audio is refused with the server's own sentence
        // ("broken.mp3 is not an audio file AutoDJ can play."), said again
        // when the same file is tried again.
        if (err.status === 422) {
          _setStatus(els, err.message, { force: true, tone: "error" });
          return;
        }
        _setStatus(els, err.status === 409 && !replace
          ? `Upload failed: a liner named ${f.name} already exists.  Tick Replace existing file to overwrite it.`
          : `Upload failed: ${err.message}`);
      }
    });
  }

  els.lnDuckDb?.addEventListener("input", () => clearFieldError(els.lnDuckDb, els.lnDuckDb.ownerDocument.getElementById("ln-duck-db-error")));
  for (const el of [
    els.lnEnabled, els.lnEveryN, els.lnEveryMin,
    els.lnRandMin, els.lnRandMax, els.lnPickMode, els.lnDuckDb,
  ]) {
    if (!el) continue;
    el.addEventListener("change", () => {
      // change fires as Tab leaves the field: the error is linked and said,
      // but focus stays where the reader moved it.
      if (el === els.lnDuckDb && parseSettingValue(el.value, DUCK_DB) === null) {
        showFieldError(el, refusalText(DUCK_DB, el.value), el.ownerDocument.getElementById("ln-duck-db-error"), els.lnStatus, { focus: false });
      }
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
  // After a station switch (#70): each station has its own liners.
  return { refresh: () => _refreshLibrary(els), clear() {
    datesEditor?.close();
    _clearLibrary(els);
  } };
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
