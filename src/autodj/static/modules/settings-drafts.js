// One draft per Settings tab; server configuration and browser preferences keep their owners.
import { requestJson, captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";
import { currentStation, stationName } from "./stations.js";
import { announceStatus, sayIfStillPending } from "./live-region.js";
import { chooseAction } from "./confirm-dialog.js";
import { clearFieldError, showFieldError } from "./dom-helpers.js";

const editors = new WeakMap();
const clone = (value) => JSON.parse(JSON.stringify(value));
const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function editorFor(doc) {
  if (editors.has(doc)) return editors.get(doc);
  const editor = { doc, panels: new Map(), revision: null, values: {}, loading: null, generation: 0 };
  editors.set(doc, editor);
  doc.defaultView?.addEventListener("beforeunload", (event) => {
    if (![...editor.panels.values()].some(dirtyPanel)) return;
    event.preventDefault();
    event.returnValue = "";
  });
  return editor;
}

function dirty(part) {
  return part.baseline !== null && !equal(part.read(), part.baseline);
}

function dirtyPanel(panel) {
  return panel.parts.some(dirty);
}

// aria-disabled, not disabled: Save or Discard is usually the focused
// control when its press empties the draft, and disabling it dropped focus
// to the page body, so NVDA lost the result.  While a save is out the
// buttons keep their state, or NVDA calls the pressed button unavailable
// (#331); aria-busy and .is-pending say it is busy.  The click handlers
// honour the attribute.
function unavailable(button) {
  return button.getAttribute("aria-disabled") === "true";
}

function refresh(panel) {
  const changed = dirtyPanel(panel);
  for (const button of [panel.save, panel.discard]) {
    if (!panel.saving) button.setAttribute("aria-disabled", String(!changed));
    button.classList.toggle("is-pending", panel.saving);
  }
  if (panel.saving) panel.save.setAttribute("aria-busy", "true");
  else panel.save.removeAttribute("aria-busy");
  panel.note.textContent = panel.saving ? "Saving changes…" : changed ? "Unsaved changes" : "No unsaved changes";
  const scopes = [];
  if (panel.parts.some((part) => part.group)) scopes.push(`Station: ${stationName()}`);
  if (panel.parts.some((part) => !part.group)) scopes.push("this browser");
  if (panel.parts.some((part) => part.group === "time")) scopes.push("shared server time display");
  panel.scope.textContent = scopes.join(" · ");
  for (const part of panel.parts) {
    for (const control of controls(part)) control.classList.toggle("settings-dirty", dirty(part));
  }
}

function controls(part) {
  return (typeof part.controls === "function" ? part.controls() : part.controls).filter(Boolean);
}

function say(panel, text, error = false) {
  announceStatus(panel.status, text, { force: true, tone: error ? "error" : "info" });
}

function accept(part, value) {
  if (value == null) return;
  if (part.echoUntil > Date.now()) {
    const matches = Object.entries(part.baseline).every(([key, item]) => equal(value[key], item));
    // Ignore state pushes sent before the save until the server echoes it.
    if (!matches) return;
  }
  part.echoUntil = 0;
  part.latest = clone(value);
  if (!dirty(part)) {
    const unchanged = part.baseline !== null && Object.entries(part.baseline).every(([key, item]) => equal(value[key], item));
    if (!unchanged) part.write(value);
    part.baseline = clone(part.read());
  }
}

export function applySettingsDrafts(settings, doc = document) {
  if (!settings?.values || !settings.revision) return;
  const editor = editorFor(doc);
  const pending = [...editor.panels.values()].some(dirtyPanel);
  editor.values = clone(settings.values);
  if (!pending || !editor.revision) editor.revision = settings.revision;
  for (const panel of editor.panels.values()) {
    for (const part of panel.parts) if (part.group) accept(part, settings.values[part.group]);
    refresh(panel);
  }
}

export async function loadSettingsDrafts(doc = document) {
  const editor = editorFor(doc);
  if (editor.loading) return editor.loading;
  const generation = editor.generation;
  editor.loading = requestJson("/api/settings").then((settings) => {
    if (generation === editor.generation) applySettingsDrafts(settings, doc);
  }).catch((error) => {
    if (generation !== editor.generation) return;
    for (const panel of editor.panels.values()) {
      if (!panel.element.hidden) say(panel, `Could not load settings: ${error.message}`, true);
    }
  }).finally(() => { if (generation === editor.generation) editor.loading = null; });
  return editor.loading;
}

export function registerSettingsGroup(spec) {
  const element = spec.panel;
  if (!element) return { accept() {}, dirty: () => false };
  const editor = editorFor(element.ownerDocument);
  let panel = editor.panels.get(element);
  if (!panel) {
    const doc = editor.doc;
    const bar = doc.createElement("div");
    bar.className = "settings-save-bar";
    const note = doc.createElement("p");
    const scope = doc.createElement("p");
    const actions = doc.createElement("div");
    actions.className = "dialog-actions";
    const save = doc.createElement("button");
    save.type = "button";
    save.textContent = "Save changes";
    save.dataset.settingsSave = "";
    const discard = doc.createElement("button");
    discard.type = "button";
    discard.textContent = "Discard changes";
    discard.dataset.settingsDiscard = "";
    const status = doc.createElement("p");
    status.setAttribute("role", "status");
    status.className = "settings-save-status";
    const error = doc.createElement("p");
    error.id = `${element.id}-save-error`;
    error.className = "field-error";
    error.hidden = true;
    actions.append(discard, save);
    bar.append(scope, note, actions, error, status);
    element.append(bar);
    panel = { element, parts: [], save, discard, status, error, note, scope, saving: false };
    editor.panels.set(element, panel);
    save.addEventListener("click", () => { if (!unavailable(save)) void saveSettingsPanel(element); });
    discard.addEventListener("click", () => { if (!unavailable(discard)) discardSettingsPanel(element); });
    for (const type of ["input", "change"]) element.addEventListener(type, (event) => {
      if ((event.target.getAttribute?.("aria-describedby") || "").split(/\s+/).includes(panel.error.id)) clearFieldError(event.target, panel.error);
      for (const part of panel.parts) {
        if (part.baseline === null && controls(part).includes(event.target)) part.baseline = clone(part.initial);
      }
      refresh(panel);
    });
    element.addEventListener("toggle", () => { if (element.open) void loadSettingsDrafts(doc); });
  }
  const part = { ...spec, baseline: null, latest: null, initial: clone(spec.read()), echoUntil: 0 };
  panel.parts.push(part);
  if (part.group) accept(part, editor.values[part.group]);
  else {
    part.baseline = clone(part.read());
    part.latest = clone(part.baseline);
  }
  refresh(panel);
  return {
    accept(value) { accept(part, value); refresh(panel); },
    dirty: () => dirty(part),
  };
}

export function settingsDraftHolds(control) {
  if (!control) return false;
  const editor = editors.get(control.ownerDocument);
  if (!editor) return false;
  for (const panel of editor.panels.values()) {
    for (const part of panel.parts) {
      if ((panel.saving || dirty(part) || part.echoUntil > Date.now()) && controls(part).some((item) => item === control || item.contains(control))) return true;
    }
  }
  return false;
}

export function discardSettingsPanel(element) {
  const editor = editorFor(element.ownerDocument);
  const panel = editor.panels.get(element);
  if (!panel || panel.saving) return;
  for (const part of panel.parts) {
    if (part.latest === null) continue;
    if (part.persisted) part.latest = clone(part.persisted());
    part.echoUntil = 0;
    part.write(part.latest);
    part.baseline = clone(part.read());
    for (const control of controls(part)) clearFieldError(control, panel.error);
  }
  refresh(panel);
  say(panel, "Changes discarded.");
  void loadSettingsDrafts(editor.doc);
}

export async function saveSettingsPanel(element) {
  const editor = editorFor(element.ownerDocument);
  const panel = editor.panels.get(element);
  if (!panel || panel.saving) return false;
  const parts = panel.parts.filter(dirty);
  if (!parts.length) return true;
  const changes = {};
  const submitted = new Map();
  // What each part's save did, in words, for parts that say so.
  const described = new Map();
  try {
    for (const part of parts) {
      if (part.persisted && !equal(part.persisted(), part.baseline)) {
        throw new Error("Browser settings changed in another tab. Discard your draft and try again.");
      }
      if (part.validate?.() === false) return false;
      for (const control of controls(part)) {
        if (control.disabled || !control.checkValidity || control.checkValidity()) continue;
        showFieldError(control, control.validationMessage, panel.error, panel.status);
        return false;
      }
      const value = clone(part.read());
      submitted.set(part, value);
      const words = part.describe?.(value, part.baseline);
      if (words) described.set(part, words);
      if (!part.group) continue;
      const patch = changes[part.group] ||= {};
      for (const [key, item] of Object.entries(value)) {
        if (!equal(item, part.baseline[key])) patch[key] = item;
      }
    }
  } catch (error) {
    say(panel, error.message, true);
    return false;
  }
  const station = currentStation();
  const epoch = captureAuthenticatedRequestEpoch();
  const generation = editor.generation;
  const current = () => generation === editor.generation && station === currentStation() && isAuthenticatedRequestCurrent(epoch);
  panel.saving = true;
  refresh(panel);
  const stopPending = sayIfStillPending((text) => say(panel, text), "Saving changes…");
  try {
    let result = { applied: [], failed: {} };
    if (Object.keys(changes).length) {
      if (!editor.revision) throw new Error("Reload settings before saving.");
      result = await requestJson("/api/settings", {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision: editor.revision, changes }),
      });
      if (!current()) return false;
      editor.revision = result.revision;
      editor.values = clone(result.settings.values);
    }
    for (const [part, value] of submitted) {
      if (part.group && !result.applied.includes(part.group)) continue;
      if (!part.group && await part.commit(value) === false) throw new Error("Browser settings could not be applied.");
      if (!current()) return false;
      const unchanged = equal(part.read(), value);
      part.latest = clone(part.group ? editor.values[part.group] : value);
      part.baseline = value;
      if (unchanged) {
        part.write(part.latest);
        part.baseline = clone(part.read());
      }
      // shortcut: legacy pushes have no revision; hold old echoes for five seconds.
      if (part.group) part.echoUntil = Date.now() + 5000;
    }
    if (Object.keys(result.failed || {}).length) {
      const saved = result.applied.length || parts.some((part) => !part.group);
      say(panel, `${saved ? "Some settings were saved." : "No settings were saved."} ${Object.values(result.failed).join(" ")} Unsaved changes remain.`, true);
      return false;
    }
    for (const other of editor.panels.values()) {
      for (const part of other.parts) if (part.group && !submitted.has(part)) accept(part, editor.values[part.group]);
      refresh(other);
    }
    const scope = parts.some((part) => part.group) ? `for ${stationName()}` : "in this browser";
    const done = [...described].filter(([part]) => !part.group || result.applied.includes(part.group))
      .map(([, words]) => words);
    say(panel, [`Settings saved ${scope}.`, ...done, result.message || ""].join(" ").trim());
    return true;
  } catch (error) {
    if (current()) {
      const part = error.field && parts.find((item) => Object.hasOwn(item.read(), error.field));
      if (part) {
        const index = Object.keys(part.read()).indexOf(error.field);
        showFieldError(controls(part)[index] || controls(part)[0], error.message, panel.error, panel.status);
      } else say(panel, error.status === 409
        ? "Settings changed elsewhere. Your draft is kept. Discard to load the current settings, then edit again."
        : `Could not save all settings: ${error.message} Unsaved changes remain.`, true);
    }
    return false;
  } finally {
    stopPending();
    if (generation === editor.generation) { panel.saving = false; refresh(panel); }
  }
}

export function settingsHaveChanges(doc = document) {
  return [...editorFor(doc).panels.values()].some(dirtyPanel);
}

export async function confirmSettingsNavigation(doc = document) {
  const editor = editorFor(doc);
  const changed = [...editor.panels.values()].filter(dirtyPanel);
  if (!changed.length) return true;
  if (changed.some((panel) => panel.saving)) return false;
  const answer = await chooseAction(doc, {
    title: "Unsaved settings", message: "Save your settings changes before leaving, or discard them.",
    choices: ["Save changes", "Discard changes", "Stay"],
  });
  if (answer === "accept") {
    for (const panel of changed) if (!await saveSettingsPanel(panel.element)) return false;
    return !settingsHaveChanges(doc);
  }
  if (answer === "alternate") {
    for (const panel of changed) discardSettingsPanel(panel.element);
    return true;
  }
  return false;
}

export function resetSettingsDrafts(doc = document) {
  const editor = editorFor(doc);
  editor.generation += 1;
  editor.revision = null;
  editor.values = {};
  editor.loading = null;
  for (const panel of editor.panels.values()) {
    panel.saving = false;
    for (const part of panel.parts) if (part.group) { part.baseline = null; part.latest = null; part.echoUntil = 0; }
    refresh(panel);
  }
}
