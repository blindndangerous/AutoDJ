// Clockwheels: a list of saved clockwheels and an editor for one of them.
//
// The server keeps each clockwheel as a native JSON file.  This module
// opens one into a working copy, edits its steps in the page, and sends it
// back only when Save clockwheel is pressed; nothing is saved on its own.
//
// The <ol> is a layout grid with a row header and a cell for each native
// button. The step name is the Edit button; Up/Down moves between names,
// Left/Right moves among row actions, and Alt+Up/Down moves the focused step. After
// every change focus stays with the step that moved, or the nearest one,
// and the result is said once through #clock-status.
//
// Folders are chosen from the registered library folders by ID, with an
// optional path inside the folder; the browser never sends a full path.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";

const NAME = /^[A-Za-z0-9 _-]{1,64}$/;
const RESERVED = /^(con|prn|aux|nul|com\d|lpt\d)$/i;
const JSON_HEADERS = { "Content-Type": "application/json" };
const copy = (value) => JSON.parse(JSON.stringify(value));
const FOLDER_TYPES = new Set(["music", "liner"]);
// Field wrappers shown for each step type; the rest are hidden.
const FIELDS_FOR = {
  music: ["source", "folder", "recursive", "selection", "enforce-rules"],
  liner: ["source", "folder", "recursive", "selection", "enforce-rules", "require-following"],
  track: ["source", "path"],
  settings: ["preset"],
};
const ALL_FIELDS = ["source", "folder", "path", "preset", "recursive", "selection", "enforce-rules", "require-following"];
// Row buttons, in tab order.
const ROW_ACTIONS = [["edit", "Edit"], ["move-up", "Move up"], ["move-down", "Move down"], ["duplicate", "Duplicate"], ["delete", "Delete"]];

export function parseResyncMinutes(text) {
  if (!String(text).trim()) return [];
  const parts = String(text).split(",").map((part) => part.trim());
  if (parts.some((part) => !/^\d+$/.test(part))) throw new Error("Enter comma-separated whole minutes from 0 to 59.");
  const values = parts.map(Number);
  if (values.some((value) => value > 59)) throw new Error("Each restart minute must be from 0 to 59.");
  return [...new Set(values)].sort((a, b) => a - b);
}

function lastPart(path) {
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || String(path);
}

// "music from Rock, at random": what one step does, for its list item.
export function describeStep(step, sources = []) {
  if (FOLDER_TYPES.has(step.type)) {
    const source = sources.find((item) => item.id === step.source);
    const where = step.folder || (source ? lastPart(source.path) : "a folder that is no longer registered");
    const extras = [];
    if (step.recursive) extras.push("with subfolders");
    if (step.selection === "random") extras.push("at random");
    if (step.enforce_rules === false) extras.push("ignoring repeat rules");
    if (step.type === "liner" && step.require_following) extras.push("only if the following track fits before the next reset");
    return [`${step.type} from ${where}`, ...extras].join(", ");
  }
  if (step.type === "track") return `track ${step.path}`;
  if (step.type === "settings") return `switch to preset ${step.preset}`;
  return `${String(step.type).replaceAll("_", " ")} step`;
}

export function installClockwheelPanel({ document: doc = document, request = requestJson } = {}) {
  const el = (id) => doc.getElementById(id);
  const panel = el("panel-library");
  const list = el("clock-list");
  const steps = el("clock-slots");
  const nameInput = el("clock-name");
  const resync = el("clock-resync-minutes");
  const status = el("clock-status");
  const typeSelect = el("clock-step-type");
  const addButton = el("clock-add-step");
  const cancelEdit = el("clock-cancel-edit");
  const legend = el("clock-step-legend");
  if (!panel || !list || !steps || !nameInput || !resync || !status || !typeSelect || !addButton) {
    return { refresh: async () => {}, clearProtectedData() {}, resume() {}, destroy() {} };
  }

  const empty = () => ({ names: [], active: null, sources: [], presets: [], cursor: 0, notice: null });
  let data = empty();
  let clock = null; // the working copy, or null when none is open
  let isNew = false; // created in the page and never saved
  let dirty = false;
  let editing = null; // index of the step in the form, or null when adding
  let listSignature = "";
  let optionsSignature = "";
  let lastNotice = null;
  let timer = null;
  let generation = 0;
  let suspended = false;

  const current = (epoch, mine) => !suspended && generation === mine && isAuthenticatedRequestCurrent(epoch);
  const say = (message, error = false) => announceStatus(status, message, { force: true, tone: error ? "error" : "info" });
  const clockUrl = (name) => `/api/library/clock/${encodeURIComponent(name)}`;
  const send = (url, method, body) => request(url, body === undefined ? { method } : { method, headers: JSON_HEADERS, body: JSON.stringify(body) });
  const count = (n) => `${n} ${n === 1 ? "step" : "steps"}`;
  const position = (index) => `Step ${index + 1} of ${clock.steps.length}, ${describeStep(clock.steps[index], data.sources)}`;

  function typedName() {
    const value = nameInput.value.trim();
    if (!NAME.test(value) || RESERVED.test(value)) {
      nameInput.setAttribute("aria-invalid", "true");
      nameInput.focus();
      throw new Error("A clockwheel name needs 1 to 64 letters, digits, spaces, dashes or underscores.");
    }
    nameInput.removeAttribute("aria-invalid");
    return value;
  }

  // ---- Saved clockwheels ---------------------------------------------
  function renderList() {
    const signature = JSON.stringify([data.names, data.active, clock?.name ?? null, isNew]);
    if (signature === listSignature) return;
    listSignature = signature;
    const focused = list.contains(doc.activeElement) ? doc.activeElement.dataset.name : null;
    if (!data.names.length) {
      const none = doc.createElement("li");
      none.textContent = "No saved clockwheels.";
      list.replaceChildren(none);
      return;
    }
    list.replaceChildren(...data.names.map((name) => {
      const item = doc.createElement("li");
      const button = doc.createElement("button");
      button.type = "button";
      button.dataset.name = name;
      button.textContent = name === data.active ? `${name}, active` : name;
      if (!isNew && clock?.name === name) button.setAttribute("aria-current", "true");
      button.addEventListener("click", () => void openClock(name));
      item.append(button);
      return item;
    }));
    if (focused) [...list.querySelectorAll("button")].find((button) => button.dataset.name === focused)?.focus();
  }

  function renderOptions() {
    const signature = JSON.stringify([data.sources, data.presets, typeSelect.value]);
    if (signature === optionsSignature) return;
    optionsSignature = signature;
    const fill = (select, items) => {
      if (!select) return;
      const chosen = select.value;
      select.replaceChildren(...items.map(([value, label]) => {
        const option = doc.createElement("option");
        option.value = value;
        option.textContent = label;
        return option;
      }));
      if (items.some(([value]) => value === chosen)) select.value = chosen;
    };
    const kind = typeSelect.value === "liner" ? "liner" : "music";
    fill(el("clock-step-source"), data.sources.filter((source) => source.kind === kind).map((source) => [source.id, source.path]));
    fill(el("clock-step-preset"), data.presets.map((name) => [name, name]));
  }

  function renderNotice() {
    const notice = el("clock-notice");
    if (notice) {
      notice.textContent = data.notice || "";
      notice.hidden = !data.notice;
    }
    if (data.notice && data.notice !== lastNotice) say(data.notice, true);
    lastNotice = data.notice;
  }

  // ---- The open clockwheel -------------------------------------------
  function renderHeading() {
    const title = el("clock-open-name");
    if (title) title.textContent = clock ? `${clock.name}${isNew ? ", not saved yet" : ""}` : "no clockwheel open";
  }

  function renderNext() {
    const next = el("clock-next");
    if (!next) return;
    if (!clock || isNew) next.textContent = "";
    else if (clock.name !== data.active) next.textContent = `${clock.name} is not the active clockwheel.`;
    else if (dirty) next.textContent = "Save to play these changes.";
    else if (!clock.steps.length) next.textContent = "";
    else next.textContent = `Plays next: ${position((data.cursor || 0) % clock.steps.length)}.`;
  }

  function renderSteps(focusIndex = null, action = "edit") {
    if (!clock || !clock.steps.length) {
      const none = doc.createElement("li");
      none.setAttribute("role", "row");
      const cell = doc.createElement("div");
      cell.setAttribute("role", "gridcell");
      cell.textContent = clock ? "No steps yet. Add one with the form below." : "Open or create a clockwheel to see its steps.";
      none.append(cell);
      steps.replaceChildren(none);
    } else {
      steps.replaceChildren(...clock.steps.map((step, index) => {
        const item = doc.createElement("li");
        item.dataset.index = String(index);
        item.setAttribute("role", "row");
        const number = doc.createElement("span");
        number.setAttribute("role", "rowheader");
        number.textContent = String(index + 1);
        item.append(number);
        for (const [key, title] of ROW_ACTIONS) {
          const cell = doc.createElement("span");
          cell.setAttribute("role", "gridcell");
          if (key === "edit") {
            const button = doc.createElement("button");
            button.type = "button";
            button.dataset.action = key;
            button.tabIndex = index === 0 ? 0 : -1;
            button.textContent = describeStep(step, data.sources);
            cell.append(button);
            item.append(cell);
            continue;
          }
          const button = doc.createElement("button");
          button.type = "button";
          button.dataset.action = key;
          button.tabIndex = -1;
          // The visible words start the name; the hidden number says which step.
          const hidden = doc.createElement("span");
          hidden.className = "visually-hidden";
          hidden.textContent = ` step ${index + 1}`;
          button.append(title, hidden);
          button.disabled = (key === "move-up" && index === 0) || (key === "move-down" && index === clock.steps.length - 1);
          cell.append(button);
          item.append(" ", cell);
        }
        return item;
      }));
    }
    renderHeading();
    renderNext();
    if (focusIndex !== null) focusRow(focusIndex, action);
  }

  function focusRow(index, action) {
    const row = steps.querySelector(`li[data-index="${index}"]`);
    if (!row) return;
    const wanted = row.querySelector(`button[data-action="${action}"]`);
    const other = { "move-up": "move-down", "move-down": "move-up" }[action] || "edit";
    const target = wanted && !wanted.disabled ? wanted : row.querySelector(`button[data-action="${other}"]:not([disabled])`)
      || row.querySelector('button[data-action="edit"]');
    steps.querySelectorAll("button[data-action]").forEach((button) => { button.tabIndex = button === target ? 0 : -1; });
    target?.focus();
  }

  function changed(message, focusIndex, action) {
    dirty = true;
    renderSteps(focusIndex, action);
    say(`${message} Not saved yet.`);
  }

  function move(index, by, action) {
    const to = index + by;
    if (to < 0 || to >= clock.steps.length) {
      say(by < 0 ? "This is already the first step." : "This is already the last step.");
      return;
    }
    const [step] = clock.steps.splice(index, 1);
    clock.steps.splice(to, 0, step);
    if (editing === index) editing = to;
    else if (editing === to) editing = index;
    changed(`Moved. ${position(to)}.`, to, action);
  }

  function remove(index) {
    const gone = describeStep(clock.steps[index], data.sources);
    clock.steps.splice(index, 1);
    if (editing !== null) resetForm();
    const left = clock.steps.length;
    const message = `Deleted step ${index + 1}, ${gone}.`;
    if (left) {
      changed(`${message} ${left === 1 ? "1 step remains" : `${left} steps remain`}.`, Math.min(index, left - 1), "edit");
    } else {
      changed(`${message} No steps are left.`, null);
      typeSelect.focus();
    }
  }

  function rowAction(action, index) {
    if (!clock) return;
    if (action === "edit") startEdit(index);
    else if (action === "move-up") move(index, -1, action);
    else if (action === "move-down") move(index, 1, action);
    else if (action === "duplicate") {
      clock.steps.splice(index + 1, 0, copy(clock.steps[index]));
      changed(`Duplicated as ${position(index + 1)}.`, index + 1, action);
    } else if (action === "delete") remove(index);
  }

  steps.addEventListener("click", (event) => {
    const button = event.target.closest?.("button[data-action]");
    const row = button?.closest("li[data-index]");
    if (row) {
      steps.querySelectorAll("button[data-action]").forEach((item) => { item.tabIndex = item === button ? 0 : -1; });
      rowAction(button.dataset.action, Number(row.dataset.index));
    }
  });
  steps.addEventListener("keydown", (event) => {
    const row = event.target.closest?.("li[data-index]");
    if (!row || !clock) return;
    if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
      event.preventDefault();
      move(Number(row.dataset.index), event.key === "ArrowUp" ? -1 : 1, event.target.dataset?.action || "edit");
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
      const next = Number(row.dataset.index) + (event.key === "ArrowUp" ? -1 : 1);
      event.preventDefault();
      if (next >= 0 && next < clock.steps.length) {
        focusRow(next, "edit");
      }
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && (event.key === "ArrowLeft" || event.key === "ArrowRight")) {
      const buttons = [...row.querySelectorAll('button[data-action]:not([disabled])')];
      const at = buttons.indexOf(event.target.closest?.("button[data-action]"));
      const next = at + (event.key === "ArrowLeft" ? -1 : 1);
      event.preventDefault();
      if (next >= 0 && next < buttons.length) focusRow(Number(row.dataset.index), buttons[next].dataset.action);
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && (event.key === "Home" || event.key === "End")) {
      const buttons = [...row.querySelectorAll('button[data-action]:not([disabled])')];
      event.preventDefault();
      focusRow(Number(row.dataset.index), buttons[event.key === "Home" ? 0 : buttons.length - 1].dataset.action);
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.key === "Delete") {
      event.preventDefault();
      if (!event.repeat) rowAction("delete", Number(row.dataset.index));
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && event.key === "Enter" && event.target.dataset?.action === "edit") {
      event.preventDefault();
      rowAction("edit", Number(row.dataset.index));
    }
  });

  // ---- Step form ------------------------------------------------------
  function showFields() {
    const visible = new Set(FIELDS_FOR[typeSelect.value] || []);
    for (const id of ALL_FIELDS) el(`clock-step-${id}`)?.closest(".clock-step-field")?.toggleAttribute("hidden", !visible.has(id));
    renderOptions();
  }

  function resetForm() {
    editing = null;
    el("clock-step-require-following").checked = false;
    if (legend) legend.textContent = "Add a step";
    addButton.textContent = "Add step";
    if (cancelEdit) cancelEdit.hidden = true;
  }

  function startEdit(index) {
    const step = clock.steps[index];
    editing = index;
    typeSelect.value = step.type in FIELDS_FOR ? step.type : "music";
    showFields();
    const set = (id, value) => { const field = el(id); if (field) field.value = value; };
    set("clock-step-source", step.source || "");
    set("clock-step-folder", step.folder || "");
    set("clock-step-path", step.path || "");
    set("clock-step-preset", step.preset || "");
    set("clock-step-selection", step.selection || "least_recent");
    const check = (id, value) => { const field = el(id); if (field) field.checked = value; };
    check("clock-step-recursive", Boolean(step.recursive));
    check("clock-step-enforce-rules", step.enforce_rules !== false);
    check("clock-step-require-following", Boolean(step.require_following));
    if (legend) legend.textContent = `Edit step ${index + 1}`;
    addButton.textContent = `Update step ${index + 1}`;
    if (cancelEdit) cancelEdit.hidden = false;
    typeSelect.focus();
    say(`Editing ${position(index)}.`);
  }

  function relative(id, label, optional) {
    const field = el(id);
    const value = (field?.value || "").trim().replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
    const bad = value.split("/").some((part) => !part.trim() || part === "." || part === "..") || value.includes(":");
    if (value ? bad : !optional) {
      field?.setAttribute("aria-invalid", "true");
      field?.focus();
      throw new Error(`${label} must be a path inside the library folder, such as Rock/Live, with no "..".`);
    }
    field?.removeAttribute("aria-invalid");
    return value;
  }

  function formStep() {
    const type = typeSelect.value;
    if (FOLDER_TYPES.has(type) || type === "track") {
      const source = el("clock-step-source")?.value || "";
      if (!source) {
        el("clock-step-source")?.focus();
        throw new Error(`Add a ${type === "liner" ? "voice liner" : "music"} folder under Music folders and Voice liner folders first.`);
      }
      if (type === "track") return { type, source, path: relative("clock-step-path", "The track", false) };
      const step = {
        type, source, folder: relative("clock-step-folder", "The subfolder", true),
        recursive: el("clock-step-recursive").checked,
        selection: el("clock-step-selection").value,
        enforce_rules: el("clock-step-enforce-rules").checked,
      };
      if (type === "liner") step.require_following = el("clock-step-require-following").checked;
      return step;
    }
    if (type === "settings") {
      const preset = el("clock-step-preset")?.value || "";
      if (!preset) throw new Error("Choose a preset.");
      return { type, preset };
    }
    throw new Error(`The editor cannot make ${type} steps yet.`);
  }

  typeSelect.addEventListener("change", showFields);
  addButton.addEventListener("click", () => {
    if (!clock) { say("Open or create a clockwheel first.", true); return; }
    let step;
    try { step = formStep(); } catch (error) { say(error.message, true); return; }
    if (editing !== null) {
      const index = editing;
      clock.steps[index] = step;
      resetForm();
      changed(`Updated ${position(index)}.`, index, "edit");
    } else {
      clock.steps.push(step);
      resetForm();
      dirty = true;
      renderSteps();
      say(`Added ${position(clock.steps.length - 1)}. Not saved yet.`);
    }
  });
  cancelEdit?.addEventListener("click", () => {
    const index = editing;
    resetForm();
    if (index !== null) focusRow(index, "edit");
    say("Edit cancelled.");
  });
  resync.addEventListener("input", () => { if (clock) { dirty = true; renderNext(); } });

  // ---- Opening, saving and the clockwheel buttons ---------------------
  function discardChanges() {
    if (!dirty || !clock) return Promise.resolve(true);
    return confirmAction(doc, {
      title: `Discard changes to ${clock.name}?`,
      message: "The changes to its steps have not been saved.",
      confirmLabel: "Discard changes",
    });
  }

  function load(value, { fresh = false } = {}) {
    clock = copy(value);
    isNew = fresh;
    dirty = fresh;
    resetForm();
    nameInput.value = clock.name;
    resync.value = clock.resync_minutes.join(", ");
    resync.removeAttribute("aria-invalid");
    listSignature = "";
    renderList();
    renderSteps();
  }

  function close() {
    clock = null; isNew = false; dirty = false;
    resetForm();
    listSignature = "";
    renderList();
    renderSteps();
  }

  async function openClock(name, { quiet = false } = {}) {
    if (!(await discardChanges())) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const value = await request(clockUrl(name));
      if (!current(epoch, mine)) return;
      load(value);
      if (!quiet) say(`Opened ${name}, ${count(value.steps.length)}${name === data.active ? ", active" : ""}.`);
    } catch (error) { if (current(epoch, mine)) say(`Could not open ${name}: ${error.message}`, true); }
  }

  async function refresh() {
    if (!panel || panel.hidden || doc.hidden || suspended) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const value = await request("/api/library/clock");
      if (!current(epoch, mine)) return;
      data = { ...empty(), ...value };
      renderList();
      renderOptions();
      renderNotice();
      // The first look at the panel opens the active clockwheel, silently.
      if (!clock && data.active && data.names.includes(data.active)) await openClock(data.active, { quiet: true });
      else renderNext();
    } catch (error) { if (current(epoch, mine)) say(`Could not load clockwheels: ${error.message}`, true); }
  }

  // Runs one change on the server, applies its result and reloads the list.
  // Returns the result, or null after saying what went wrong.
  async function command(run, after = null) {
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const result = await run();
      if (!current(epoch, mine)) return null;
      if (after) after(result);
      await refresh();
      return current(epoch, mine) ? result : null;
    } catch (error) {
      if (current(epoch, mine)) {
        say(error.message, true);
        const step = /^Step (\d+):/.exec(error.message);
        if (step && clock) focusRow(Number(step[1]) - 1, "edit");
      }
      return null;
    }
  }

  function needSaved(verb) {
    if (!clock || isNew) { say(`Open a saved clockwheel to ${verb} it.`, true); return false; }
    if (dirty) { say(`Save the changes to ${clock.name}, or open it again to drop them, before you ${verb} it.`, true); return false; }
    return true;
  }

  function newName(verb) {
    const name = typedName();
    if (clock && name === clock.name) {
      nameInput.focus();
      throw new Error(`Type a new name in Name to ${verb}.`);
    }
    return name;
  }

  el("clock-create")?.addEventListener("click", async () => {
    let name;
    try { name = typedName(); } catch (error) { say(error.message, true); return; }
    if (data.names.some((item) => item.toLowerCase() === name.toLowerCase())) {
      nameInput.focus();
      say(`A clockwheel named ${name} already exists. Type another name.`, true);
      return;
    }
    if (!(await discardChanges())) return;
    load({ version: 1, name, steps: [], resync_minutes: [0] }, { fresh: true });
    typeSelect.focus();
    say(`New clockwheel ${name}. Add its steps, then press Save clockwheel.`);
  });

  el("clock-save")?.addEventListener("click", async () => {
    if (!clock) { say("Open or create a clockwheel first.", true); return; }
    let minutes;
    try { minutes = parseResyncMinutes(resync.value); } catch (error) {
      resync.setAttribute("aria-invalid", "true");
      resync.focus();
      say(error.message, true);
      return;
    }
    resync.removeAttribute("aria-invalid");
    const body = { version: 1, name: clock.name, steps: clock.steps, resync_minutes: minutes };
    const saved = await command(() => send(isNew ? "/api/library/clock" : clockUrl(clock.name), isNew ? "POST" : "PUT", body),
      (result) => load(result));
    if (saved) say(`Saved ${saved.name}, ${count(saved.steps.length)}.`);
  });

  el("clock-duplicate")?.addEventListener("click", async () => {
    if (!needSaved("copy")) return;
    let name;
    try { name = newName("copy it"); } catch (error) { say(error.message, true); return; }
    const from = clock.name;
    const copy = await command(() => send(`${clockUrl(from)}/duplicate`, "POST", { name }), (result) => load(result));
    if (copy) say(`Copied ${from} as ${copy.name}, and opened the copy.`);
  });

  el("clock-rename")?.addEventListener("click", async () => {
    if (!needSaved("rename")) return;
    let name;
    try { name = newName("rename it"); } catch (error) { say(error.message, true); return; }
    const old = clock.name;
    const renamed = await command(() => send(`${clockUrl(old)}/rename`, "POST", { name }), (result) => load(result));
    if (renamed) say(`Renamed ${old} to ${renamed.name}.`);
  });

  el("clock-delete")?.addEventListener("click", async () => {
    if (!clock) { say("Open a clockwheel to delete it.", true); return; }
    const name = clock.name;
    if (isNew) {
      close();
      say(`Dropped the new clockwheel ${name}; it was never saved.`);
      return;
    }
    let deleted = null;
    // The request runs while the dialog is open; the result is said after it closes.
    await confirmAction(doc, {
      title: `Delete clockwheel ${name}?`,
      message: name === data.active
        ? "It is the active clockwheel, so no clockwheel plays until you activate another."
        : "Its file is removed. Backups made earlier still hold it.",
      confirmLabel: "Delete clockwheel",
      onConfirm: async () => {
        deleted = await command(() => send(clockUrl(name), "DELETE"), () => close());
        return deleted ? list.querySelector("button") || el("clock-create") : null;
      },
    });
    if (deleted) say(`Deleted ${name}.`);
  });

  el("clock-activate")?.addEventListener("click", async () => {
    if (!needSaved("activate")) return;
    const name = clock.name;
    if (await command(() => send("/api/library/clock/active", "POST", { name }))) {
      say(`${name} is the active clockwheel. It plays when Pick mode is set to Clockwheel.`);
    }
  });

  el("clock-deactivate")?.addEventListener("click", async () => {
    if (await command(() => send("/api/library/clock/active", "POST", { name: null }))) say("No clockwheel is active now.");
  });

  el("clock-import")?.addEventListener("click", async () => {
    const fileInput = el("clock-pal-file");
    const file = fileInput?.files?.[0];
    if (!file) { fileInput?.focus(); say("Choose a .PAL file first.", true); return; }
    if (file.size > 256 * 1024) { say(".PAL files must be 256 KB or smaller.", true); return; }
    let name, minutes;
    try { name = typedName(); minutes = parseResyncMinutes(resync.value); } catch (error) { say(error.message, true); return; }
    if (!(await discardChanges())) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    let text;
    try { text = await file.text(); } catch (error) {
      if (current(epoch, mine)) say(`Could not read ${file.name}: ${error.message}`, true);
      return;
    }
    if (!current(epoch, mine)) return;
    const imported = await command(() => send("/api/library/clock/import", "POST", { text, name, resync_minutes: minutes }),
      (result) => load(result));
    if (imported) say(`Imported ${imported.name} from ${file.name}, ${count(imported.steps.length)}. Press Activate to use it.`);
  });

  // ---- Polling and sign-out -------------------------------------------
  function updatePolling() {
    if (suspended || panel.hidden || doc.hidden) {
      if (timer !== null) clearInterval(timer);
      timer = null;
    } else if (timer === null) {
      void refresh();
      timer = setInterval(refresh, 5000);
    }
  }
  window.addEventListener("hashchange", updatePolling);
  doc.addEventListener("visibilitychange", updatePolling);
  showFields();
  renderSteps();
  updatePolling();

  function clearProtectedData() {
    suspended = true;
    generation += 1;
    if (timer !== null) clearInterval(timer);
    timer = null;
    data = empty();
    clock = null; isNew = false; dirty = false;
    listSignature = ""; optionsSignature = ""; lastNotice = null;
    list.replaceChildren();
    nameInput.value = "";
    resync.value = "0";
    const file = el("clock-pal-file");
    if (file) file.value = "";
    resetForm();
    renderOptions();
    renderSteps();
    const notice = el("clock-notice");
    if (notice) { notice.textContent = ""; notice.hidden = true; }
    status.textContent = "";
    const mine = generation;
    setTimeout(() => { if (suspended && generation === mine) status.textContent = ""; }, 0);
  }
  return {
    refresh,
    clearProtectedData,
    resume() { suspended = false; updatePolling(); },
    destroy() {
      clearProtectedData();
      window.removeEventListener("hashchange", updatePolling);
      doc.removeEventListener("visibilitychange", updatePolling);
    },
  };
}
