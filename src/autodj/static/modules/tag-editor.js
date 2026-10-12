// Track info, Tags: edit a recording's title, artist, album, genre, year,
// BPM, key and ISRC (#53).
//
// Save tags sends only the fields that changed.  A refused field is marked
// aria-invalid, its error text is linked to it with aria-describedby and the
// first one takes focus, which reads the error, so nothing else speaks.
// Any other result is said once in #tags-status.  Focus stays on Save tags
// throughout: the button is never disabled while the save runs.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { announceStatus } from "./live-region.js";

const TAG_FIELDS = ["title", "artist", "album", "genre", "year", "bpm", "key", "isrc"];
const TEXT_FIELDS = new Set(["title", "artist", "album", "genre"]);
const LABELS = { title: "Title", artist: "Artist", album: "Album", genre: "Genre" };
const MAX_TEXT = 500;
const CAMELOT = /^(?:1[0-2]|[1-9])[AB]$/i;
const LETTER_KEY = /^[A-Ga-g][#b]?(?:m| ?minor| ?major)?$/;

// The error for one field's text, or "" when the server will take it.  The
// server checks the same rules again and names the field it refuses.
export function tagFieldError(field, value) {
  const text = String(value ?? "").trim();
  if (TEXT_FIELDS.has(field)) {
    if (field === "title" && !text) return "Enter a title.";
    if (text.length > MAX_TEXT) return `${LABELS[field]} must be ${MAX_TEXT} characters or fewer.`;
    // eslint-disable-next-line no-control-regex
    if (/[\u0000-\u001f\u007f]/.test(text)) return `${LABELS[field]} cannot contain line breaks or tabs.`;
    return "";
  }
  if (!text) return "";
  if (field === "year" && (!/^\d{4}$/.test(text) || Number(text) < 1000)) {
    return "Enter the year as four digits, such as 1999, or leave it empty.";
  }
  if (field === "bpm" && (!/^\d{1,3}(?:\.\d{1,2})?$/.test(text) || Number(text) < 1 || Number(text) > 400)) {
    return "Enter BPM as a number from 1 to 400, such as 128 or 127.5, or leave it empty.";
  }
  if (field === "key" && !CAMELOT.test(text) && !LETTER_KEY.test(text)) {
    return "Enter a key such as Am, F#m, Db or C major, or a Camelot key such as 8A, or leave it empty.";
  }
  if (field === "isrc" && !/^[A-Z]{2}[A-Z0-9]{3}\d{7}$/.test(text.replace(/[\s-]/g, "").toUpperCase())) {
    return "Enter an ISRC of 12 characters: 2 letters, 3 letters or digits, then 7 digits, such as AB1CD2600001. Hyphens and spaces are ignored. Or leave it empty.";
  }
  return "";
}

const NO_EDITOR = { load() {}, clear() {}, dirty: () => false, save: async () => true, destroy() {} };

export function installTagEditor({ document: doc = document, request = requestJson, onSaved = () => {}, isCurrent = () => true } = {}) {
  const section = doc.getElementById("tags-editor");
  const form = doc.getElementById("tags-form");
  if (!section || !form) return NO_EDITOR;
  const status = doc.getElementById("tags-status");
  const unavailable = doc.getElementById("tags-unavailable");
  const beetsLabel = doc.getElementById("tags-beets-label");
  const beets = doc.getElementById("tags-beets");
  const save = doc.getElementById("tags-save");
  const inputs = Object.fromEntries(TAG_FIELDS.map((field) => [field, doc.getElementById(`tag-${field}`)]));
  const errors = Object.fromEntries(TAG_FIELDS.map((field) => [field, doc.getElementById(`tag-${field}-error`)]));
  let path = null;
  let loaded = {};
  let saving = null;  // the save in flight, if any
  let generation = 0;

  function linkError(field, linked) {
    const input = inputs[field];
    const error = errors[field];
    const ids = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== error.id);
    if (linked) ids.push(error.id);
    if (ids.length) input.setAttribute("aria-describedby", ids.join(" "));
    else input.removeAttribute("aria-describedby");
  }
  function clearError(field) {
    inputs[field].removeAttribute("aria-invalid");
    errors[field].textContent = "";
    errors[field].hidden = true;
    linkError(field, false);
  }
  function showError(field, message) {
    inputs[field].setAttribute("aria-invalid", "true");
    errors[field].textContent = message;
    errors[field].hidden = false;
    linkError(field, true);
  }
  function clearErrors() { for (const field of TAG_FIELDS) clearError(field); }
  function say(message, tone = "info") { announceStatus(status, message, { force: true, tone }); }

  function changes() {
    const changed = {};
    for (const field of TAG_FIELDS) {
      const value = inputs[field].value.trim();
      if (value !== (loaded[field] ?? "")) changed[field] = value;
    }
    return changed;
  }

  function load(record) {
    generation += 1;
    path = record?.path || null;
    loaded = { ...(record?.tags || {}) };
    clearErrors();
    status.textContent = "";
    for (const field of TAG_FIELDS) inputs[field].value = loaded[field] ?? "";
    const writable = !!path && record?.tags_writable !== false && !!record?.tags;
    form.hidden = !writable;
    if (unavailable) unavailable.hidden = writable || !path;
    if (beetsLabel) beetsLabel.hidden = !record?.beets_writeback;
    section.hidden = !path;
  }

  function clear() {
    generation += 1;
    saving = null;
    path = null;
    loaded = {};
    clearErrors();
    status.textContent = "";
    for (const field of TAG_FIELDS) inputs[field].value = "";
    section.hidden = true;
  }

  // Resolves true once saved (or nothing to save).  errorTarget, from the
  // Save or Discard dialog, receives a message that is not about a field.
  async function saveTags(errorTarget = null) {
    if (saving || !path || form.hidden) return false;
    clearErrors();
    const changed = changes();
    const invalid = Object.keys(changed).map((field) => [field, tagFieldError(field, changed[field])]).filter(([, message]) => message);
    if (invalid.length) {
      for (const [field, message] of invalid) showError(field, message);
      if (errorTarget) errorTarget.textContent = "Some tags need correcting. Press Escape to return to editing.";
      else inputs[invalid[0][0]].focus();
      return false;
    }
    if (!Object.keys(changed).length) {
      if (!errorTarget) say("No tags changed.");
      return true;
    }
    const token = {};
    saving = token;
    const mine = generation;
    const epoch = captureAuthenticatedRequestEpoch();
    // The page's own preview may be reading the file; let it go first.
    const preview = doc.getElementById("points-preview");
    const previewSrc = preview?.getAttribute("src") || "";
    if (previewSrc) { preview.pause(); preview.removeAttribute("src"); preview.load(); }
    try {
      const record = await request("/api/library/tags", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, tags: changed, beets: !!(beets && !beetsLabel?.hidden && beets.checked) }),
      });
      if (mine !== generation || !isAuthenticatedRequestCurrent(epoch) || !isCurrent()) return false;
      const keepBeets = beets?.checked;
      load(record);
      if (beets) beets.checked = keepBeets;
      onSaved(record);
      const name = record.tags?.title || record.title || "this track";
      const message = {
        updated: `Saved the tags of ${name} and updated beets.`,
        not_found: `Saved the tags of ${name}. Beets has no entry for this file, so beets did not change.`,
        failed: `Saved the tags of ${name}, but beets could not be updated: ${record.beets_error || "unknown error"}.`,
      }[record.beets] || `Saved the tags of ${name}.`;
      say(message, record.beets === "failed" ? "error" : "info");
      return true;
    } catch (error) {
      if (mine !== generation || !isAuthenticatedRequestCurrent(epoch)) return false;
      if (error.field && inputs[error.field]) {
        showError(error.field, error.message);
        if (errorTarget) errorTarget.textContent = "Some tags need correcting. Press Escape to return to editing.";
        else inputs[error.field].focus();
      } else {
        const message = `Could not save the tags: ${error.message}`;
        if (errorTarget) errorTarget.textContent = `${message} Press Escape to return to editing.`;
        else say(message, "error");
      }
      return false;
    } finally {
      if (saving === token) saving = null;
      if (previewSrc && preview && !preview.getAttribute("src")) preview.src = previewSrc;
    }
  }

  const onSubmit = (event) => { event.preventDefault(); void saveTags(); };
  form.addEventListener("submit", onSubmit);
  const onInput = (event) => {
    const field = event.target?.dataset?.tag;
    if (field && inputs[field]?.getAttribute("aria-invalid") === "true") clearError(field);
  };
  form.addEventListener("input", onInput);
  save?.setAttribute("type", "submit");

  return {
    load,
    clear,
    dirty: () => !!path && !form.hidden && Object.keys(changes()).length > 0,
    save: saveTags,
    destroy() { form.removeEventListener("submit", onSubmit); form.removeEventListener("input", onInput); clear(); },
  };
}
