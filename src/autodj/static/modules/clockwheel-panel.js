// Clockwheels: a list of saved clockwheels and an editor for one of them.
//
// The server keeps each clockwheel as a native JSON file.  This module
// opens one into a working copy, edits its steps in the page, and sends it
// back only when Save clockwheel is pressed; nothing is saved on its own.
//
// Saved wheels and steps use the shared collection table. Cell focus retains
// the native table keyboard model; each row's Options menu has its actions.
// A step is one cell, its numbered sentence, as before #707 (#712).
// Alt+Up/Down moves the focused step, Delete removes it, and Ctrl+Z undoes
// step changes. Edits remain in the working copy until Save clockwheel.
//
// Folders are chosen from the registered library folders by ID, with an
// optional path inside the folder; the browser never sends a full path.
// Category conditions name a saved category (Categories, above) by its ID.
import { createCollectionTable } from "./collection-table.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { failureText, captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { createEditorDialog } from "./editor-dialog.js";
import { clearFieldError, clearFormErrors, showFieldError } from "./dom-helpers.js";
import { makeSay } from "./live-region.js";
import { parsePointTime } from "./library-folders.js";
import { createConditionRow, readConditionRows, renumberConditionRows, updateConditionCategories } from "./condition-editor.js";
import { describeConditions, describeStep } from "./clockwheel-step.js";
import { addBrowseButton, libraryFolderSource } from "./path-picker.js";
export { describeStep } from "./clockwheel-step.js";
import { fillStepHours, installStepHours, readStepHours } from "./step-hours.js";
import { checkStationName, isStationNameError } from "./station-name-field.js";

const NAME = /^[A-Za-z0-9 _-]{1,64}$/;
const RESERVED = /^(con|prn|aux|nul|com\d|lpt\d)$/i;
const JSON_HEADERS = { "Content-Type": "application/json" };
const copy = (value) => JSON.parse(JSON.stringify(value));
const FOLDER_TYPES = new Set(["music", "liner"]);
// Field wrappers shown for each step type; the rest are hidden.
const FIELDS_FOR = {
  music: ["conditions", "selection", "enforce-rules"],
  liner: ["conditions", "selection", "enforce-rules", "require-following"],
  track: ["source", "path"],
  settings: ["preset"],
  ad_break: ["ad-spots"],
};
const ALL_FIELDS = ["conditions", "source", "path", "preset", "ad-spots", "selection", "enforce-rules", "require-following"];
const AD_ERROR = "clock-step-ad-error";
const AD_FIELDS = ["clock-step-ad-spots", "clock-step-ad-seconds", "clock-step-ad-advertiser", "clock-step-ad-category"];
// A step's menu, in order.
const ROW_ACTIONS = [["edit", "Edit"], ["move-up", "Move up"], ["move-down", "Move down"], ["duplicate", "Duplicate"], ["delete", "Delete"]];

export function parseResyncMinutes(text) {
  if (!String(text).trim()) return [];
  const parts = String(text).split(",").map((part) => part.trim());
  if (parts.some((part) => !/^\d+$/.test(part))) throw new Error("Enter comma-separated whole minutes from 0 to 59.");
  const values = parts.map(Number);
  if (values.some((value) => value > 59)) throw new Error("Each restart minute must be from 0 to 59.");
  return [...new Set(values)].sort((a, b) => a - b);
}

export function installClockwheelPanel({ document: doc = document, request = requestJson, createCategory } = {}) {
  const el = (id) => doc.getElementById(id);
  const panel = el("panel-library");
  const list = el("clock-list");
  const steps = el("clock-slots");
  const undoButton = el("clock-undo");
  const editor = steps?.closest(".card");
  const nameInput = el("clock-name");
  const resync = el("clock-resync-minutes");
  // The station name while this wheel is active (#472), and its error.
  const stationName = el("clock-station-name");
  const stationNameError = el("clock-station-name-error");
  const status = el("clock-status");
  const typeSelect = el("clock-step-type");
  const newStep = el("clock-new-step");
  const addButton = el("clock-add-step");
  const cancelEdit = el("clock-cancel-edit");
  const stepDialog = el("clock-step-dialog");
  const stepError = el("clock-step-form-error");
  const legend = el("clock-step-legend");
  if (!panel || !list || !steps || !nameInput || !resync || !status || !typeSelect || !addButton) {
    return { refresh: async () => {}, clearProtectedData() {}, hasUnsavedWork: () => false, resume() {}, destroy() {} };
  }

  const empty = () => ({
    names: [], active: null, sources: [], presets: [], categories: [], cursor: 0, notice: null, pending_update: false,
    ad_advertisers: [], ad_categories: [], ads_reason: null,
  });
  let data = empty();
  let clock = null; // the working copy, or null when none is open
  let isNew = false; // created in the page and never saved
  // How the saved wheel picks (steps and restart minutes), to tell a save
  // that changes only its station name, which applies at once (#472).
  let savedPicking = null;
  const picking = (value) => JSON.stringify([value.steps, value.resync_minutes]);
  let undoHistory = [];
  let dirty = false;
  let editing = null; // index of the step in the form, or null when adding
  const stepKeys = new WeakMap();
  let nextStepKey = 0;
  let optionsSignature = "";
  let lastNotice = null;
  let lastPendingUpdate = false;
  let loadFailed = false;
  let timer = null;
  let generation = 0;
  let listGeneration = 0;
  let suspended = false;
  let conditionId = 0;
  let stepCountTimer = null;
  let stepCountGeneration = 0;
  let stepCountRunning = false;
  let pendingStepCount = null;
  const stepEditor = createEditorDialog({
    form: el("clock-step-form"), heading: legend, dialog: stepDialog,
    cancel: cancelEdit, fallback: newStep, submit: addButton, save: saveStep,
    onCancel: () => {
      const index = editing;
      resetForm();
      renderSteps();
      say("Edit cancelled.");
      return index === null ? newStep : rowTarget(index);
    },
  });

  const current = (epoch, mine) => !suspended && generation === mine && isAuthenticatedRequestCurrent(epoch);
  const stepCountCurrent = (epoch, mine) => !suspended && stepCountGeneration === mine && isAuthenticatedRequestCurrent(epoch);
  const say = makeSay(() => stepEditor.region(status), {});
  const clockUrl = (name) => `/api/library/clock/${encodeURIComponent(name)}`;
  // A track step's file, inside the library folder chosen above it.
  const stepPath = el("clock-step-path");
  if (stepPath) {
    addBrowseButton(stepPath, {
      source: libraryFolderSource(() => el("clock-step-source")?.value || "", { files: true, request }),
      label: "track",
    });
  }
  const send = (url, method, body) => request(url, body === undefined ? { method } : { method, headers: JSON_HEADERS, body: JSON.stringify(body) });
  const count = (n) => `${n} ${n === 1 ? "step" : "steps"}`;
  // A step with days and hours says them after what it plays.
  installStepHours(doc);
  // A refused ad break field is unmarked once it is changed.
  for (const id of AD_FIELDS) {
    el(id)?.addEventListener("input", () => {
      if (el(id).getAttribute("aria-invalid") === "true") clearFieldError(el(id), el(AD_ERROR));
    });
  }
  const describe = (step) => describeStep(step, data.sources, data.categories);
  const position = (index) => `Step ${index + 1} of ${clock.steps.length}, ${describe(clock.steps[index])}`;

  function typedName() {
    const value = nameInput.value.trim();
    if (!NAME.test(value) || RESERVED.test(value)) {
      const message = "A clockwheel name needs 1 to 64 letters, digits, spaces, dashes or underscores.";
      showFieldError(nameInput, message, el("clock-import-name-error"), null);
      throw new Error(message);
    }
    clearFieldError(nameInput, el("clock-import-name-error"));
    return value;
  }

  // ---- Saved clockwheels ---------------------------------------------
  const savedTable = createCollectionTable({
    host: list, label: "Saved clockwheels", status, fallback: el("clock-create"),
    emptyText: "No saved clockwheels.", keyOf: (name) => name, nameOf: (name) => name,
    columns: [
      { key: "name", label: "Name", value: (name) => isDraft(name) ? `${name}, Not saved` : name },
      { key: "active", label: "Active", value: (name) => !isDraft(name) && name === data.active ? "Yes" : "No" },
    ],
    actions: (name) => [
      { id: "open", label: "Open", run: () => isDraft(name) ? (rowTarget(0) || newStep)?.focus() : openClock(name) },
      { id: "duplicate", label: "Duplicate", disabledReason: isDraft(name) ? "Save this clockwheel before duplicating it." : "", run: (button) => openNameEditor("duplicate", name, button) },
      { id: "rename", label: "Rename", disabledReason: savedActionReason(name), run: (button) => openNameEditor("rename", name, button) },
      { id: "activate", label: !isDraft(name) && name === data.active ? "Turn off" : "Activate", disabledReason: !isDraft(name) && name === data.active ? "" : savedActionReason(name), run: () => activateClock(name) },
      { id: "delete", label: "Delete", run: () => deleteClock(name) },
    ],
  });

  function isDraft(name) {
    return isNew && clock?.name === name;
  }

  function clockNames() {
    return isNew && clock ? [...data.names.filter((name) => name !== clock.name), clock.name] : data.names;
  }

  // The open wheel's name cell is marked current.
  function renderList() {
    savedTable.update(clockNames());
    for (const row of savedTable.body.querySelectorAll("tr[data-row-key]")) {
      const name = row.querySelector("td[data-name]");
      if (clock?.name === row.dataset.rowKey) name.setAttribute("aria-current", "true");
      else name.removeAttribute("aria-current");
    }
  }

  function stepKey(step) {
    if (!stepKeys.has(step)) stepKeys.set(step, String(++nextStepKey));
    return stepKeys.get(step);
  }

  const stepTable = createCollectionTable({
    host: steps, label: "Clockwheel steps", status, fallback: newStep,
    emptyText: "No steps. Open or create a clockwheel, then press New step.",
    keyOf: stepKey, nameOf: (step, index) => `Step ${index + 1}, ${stepSummary(step)}`,
    columns: [{ key: "step", label: "Step", value: (step, index) => `${index + 1}. ${describe(step)}` }],
    actions: (step, index) => ROW_ACTIONS.map(([id, label]) => ({
      id, label, run: () => rowAction(id, clock.steps.indexOf(step)),
      disabledReason: id === "move-up" && index === 0 ? "This is already the first step."
        : id === "move-down" && index === clock.steps.length - 1 ? "This is already the last step."
          : id === "delete" && editing !== null ? "Finish editing the step before deleting it." : "",
    })),
  });

  function stepSummary(step) {
    if (step.type === "category") return `music from ${describeConditions([{ field: "category", category: step.category }], "all", data.sources, data.categories)}`;
    if (FOLDER_TYPES.has(step.type)) {
      const conditions = step.conditions || [{ field: "folder", source: step.source, folder: step.folder || "", recursive: step.recursive }];
      return `${step.type} from ${describeConditions(conditions, step.match, data.sources, data.categories)}`;
    }
    if (step.type === "track") return `track ${step.path}`;
    if (step.type === "settings") return `switch to preset ${step.preset}`;
    return `${String(step.type).replaceAll("_", " ")}${step.type === "ad_break" ? "" : " step"}`;
  }

  function renderOptions() {
    const signature = JSON.stringify([data.sources, data.presets, data.categories, data.genres, data.beets_fields, data.beets_error,
      data.ad_advertisers, data.ad_categories, data.ads_reason, typeSelect.value]);
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
    fillAdChoice(el("clock-step-ad-advertiser"), "Any advertiser", data.ad_advertisers);
    fillAdChoice(el("clock-step-ad-category"), "Any category", data.ad_categories);
    const notice = el("clock-step-ads-notice");
    if (notice) {
      notice.textContent = data.ads_reason || "";
      notice.hidden = !data.ads_reason;
    }
    configureConditionOptions();
  }

  // The advertisers or categories of the station's spots, after "Any".  A
  // saved name no spot has now stays chosen, said as such.
  function fillAdChoice(select, any, names = [], chosen = select?.value) {
    if (!select) return;
    if (select === doc.activeElement && chosen === select.value) {
      // Rebuilding the list under the reader would move its place; try on the next refresh.
      optionsSignature = "";
      return;
    }
    const items = [["", any], ...(names || []).map((name) => [name, name])];
    if (chosen && !items.some(([value]) => value.toLowerCase() === chosen.toLowerCase())) {
      items.splice(1, 0, [chosen, `${chosen} (no spot now)`]);
    }
    select.replaceChildren(...items.map(([value, label]) => {
      const option = doc.createElement("option");
      option.value = value;
      option.textContent = label;
      return option;
    }));
    const match = items.find(([value]) => value.toLowerCase() === (chosen || "").toLowerCase());
    select.value = match ? match[0] : "";
  }

  function clearAdError() { clearFieldError(AD_FIELDS.map(el), el(AD_ERROR)); }

  function refuseAd(field, message, interactive) {
    if (interactive) {
      clearAdError();
      showFieldError(field, message, el(AD_ERROR), null, { focus: false });
    }
    throw Object.assign(new Error(message), { control: field });
  }

  // A whole number typed in *field*, or the error said for it.
  function wholeNumber(field, label, interactive) {
    const text = String(field?.value ?? "").trim();
    if (!/^\d+$/.test(text) || field?.validity?.badInput) {
      refuseAd(field, `Enter ${label} as a whole number.`, interactive);
    }
    return Number(text);
  }

  function adStep(interactive) {
    if (interactive) clearAdError();
    const spotsField = el("clock-step-ad-spots");
    const secondsField = el("clock-step-ad-seconds");
    const spots = wholeNumber(spotsField, "Spots in the break", interactive);
    if (spots < 1 || spots > 10) refuseAd(spotsField, "Spots in the break must be from 1 to 10.", interactive);
    const seconds = wholeNumber(secondsField, "Longest break in seconds", interactive);
    if (seconds > 900 || (seconds > 0 && seconds < 10)) {
      refuseAd(secondsField, "Longest break must be 0 for no limit, or from 10 to 900 seconds.", interactive);
    }
    const step = { type: "ad_break", max_spots: spots, max_seconds: seconds };
    const advertiser = el("clock-step-ad-advertiser")?.value || "";
    const category = el("clock-step-ad-category")?.value || "";
    if (advertiser) step.advertiser = advertiser;
    if (category) step.category = category;
    return step;
  }

  function conditionRow(value = {}) {
    const sourceList = data.sources.filter((s) => s.kind === (typeSelect.value === "liner" ? "liner" : "music"));
    return createConditionRow({
      document: doc,
      nextId: () => `clock-condition-${++conditionId}`,
      index: el("clock-step-conditions").children.length + 1,
      value,
      allowCategory: typeSelect.value === "music",
      sourceChoices: sourceList.map((item) => [item.id, item.path]),
      genreChoices: (data.genres || []).map((item) => [item, item]),
      beetsChoices: (data.beets_fields || []).map((item) => [item, item]),
      categoryChoices: data.categories.map((item) => [item.id, item.name]),
      onAddCategory: createCategory && (({ row, category, button }) => {
        const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
        const host = el("clock-step-conditions");
        void createCategory({
          from: button,
          isCurrent: () => current(epoch, mine) && stepDialog.open && host.contains(row),
          onCreated: (saved, categories) => {
            data.categories = categories;
            updateConditionCategories(host, categories.map((item) => [item.id, item.name]), category, saved.id);
            category.focus();
            scheduleStepCount();
          },
        });
      }),
      onFieldChange: updateBeetsNotice,
      onRemove: (row) => {
      const host = row.parentElement;
      const index = [...host.children].indexOf(row);
      row.remove();
      renumberConditionRows(host);
      const left = [...host.children];
      updateBeetsNotice();
      // The condition that took its place, or the one before it when it was last.
      (left[Math.min(index, left.length - 1)]?.querySelector("select") || el("clock-add-condition")).focus();
      say(`Condition ${index + 1} removed, ${left.length} ${left.length === 1 ? "condition" : "conditions"} left.`);
      },
    });
  }

  function renderConditions(values = [{}]) {
    const host = el("clock-step-conditions");
    if (!host) return;
    host.replaceChildren();
    for (const value of values.length ? values : [{}]) host.append(conditionRow(value).row);
    updateBeetsNotice();
  }

  function updateBeetsNotice() {
    const unavailable = !(data.beets_fields || []).length || Boolean(data.beets_error);
    let hasBeets = false;
    for (const row of el("clock-step-conditions")?.children || []) {
      const field = row.querySelector("select");
      const selected = field.value === "beets";
      field.querySelector('option[value="beets"]').disabled = unavailable && !selected;
      hasBeets ||= selected;
    }
    const notice = el("clock-step-beets-notice");
    if (notice) {
      notice.textContent = data.beets_error || "Beets fields are unavailable.";
      notice.hidden = !unavailable || !hasBeets;
    }
    // A hidden notice named by aria-describedby would still be read.
    const describedBy = notice && !notice.hidden ? "clock-step-beets-notice" : null;
    for (const row of el("clock-step-conditions")?.children || []) {
      const field = row.querySelectorAll("select")[4];
      if (describedBy) field.setAttribute("aria-describedby", describedBy);
      else field.removeAttribute("aria-describedby");
    }
  }

  function configureConditionOptions() {
    const kind = typeSelect.value === "liner" ? "liner" : "music";
    const sources = data.sources.filter((source) => source.kind === kind);
    for (const row of el("clock-step-conditions")?.children || []) {
      const selects = row.querySelectorAll("select");
      const [, source, , genre, name, category] = selects;
      const replace = (select, items, keep) => {
        if (select === doc.activeElement) {
          // Rebuilding the list under the reader would move its place; try on the next refresh.
          optionsSignature = "";
          return;
        }
        const selected = select.value;
        select.replaceChildren(...items.map(([v, label]) => {
          const option = doc.createElement("option");
          option.value = v;
          option.textContent = label;
          return option;
        }));
        if (keep && selected && !items.some(([v]) => v === selected)) {
          const option = doc.createElement("option");
          option.value = selected;
          option.textContent = `${selected} (unavailable)`;
          select.prepend(option);
        }
        if (selected) select.value = selected;
      };
      replace(source, sources.map((s) => [s.id, s.path]), true);
      replace(genre, (data.genres || []).map((v) => [v, v]), true);
      replace(name, (data.beets_fields || []).map((v) => [v, v]), true);
      replace(category, data.categories.map((item) => [item.id, item.name]), true);
    }
    updateBeetsNotice();
  }

  function readConditions(interactive = true) {
    const rows = [...el("clock-step-conditions").children];
    if (!rows.length) {
      if (interactive) el("clock-add-condition").focus();
      throw Object.assign(new Error("Add at least one condition."), { control: el("clock-add-condition") });
    }
    return readConditionRows(rows, {
      interactive,
      parsePointTime,
      folderRequired: "Choose a library folder for each folder condition.",
      durationError: "Enter the duration as seconds or minutes:seconds, for example 90 or 1:30.",
      numberError: (field) => field === "duration" ? "Duration must be non-negative." : `Enter a number for ${field}.`,
      negativeError: (field) => field === "duration" ? "Duration must be non-negative." : `Enter a number for ${field}.`,
      upperRequired: "Enter the upper value for the range.",
      beetsAvailable: (name) => Boolean(name) && !data.beets_error && (data.beets_fields || []).includes(name),
      beetsError: (name) => data.beets_error || (!name ? "Beets fields are unavailable." : `Beets field ${name} is unavailable.`),
    });
  }

  function renderNotice() {
    const notice = el("clock-notice");
    if (notice) {
      notice.textContent = data.notice || "";
      notice.hidden = !data.notice;
    }
    if (data.notice && data.notice !== lastNotice) say(data.notice, "error");
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
    else if (data.pending_update) next.textContent = "Saved changes are waiting for the next clock restart.";
    else if (!clock.steps.length) next.textContent = "";
    else next.textContent = `Plays next: ${position((data.cursor || 0) % clock.steps.length)}.`;
    const pending = Boolean(data.pending_update && clock && clock.name === data.active);
    if (pending && !lastPendingUpdate) say("Saved changes are waiting for the next clock restart.");
    lastPendingUpdate = pending;
    const apply = el("clock-apply-timing");
    if (apply) apply.hidden = !clock || isNew || clock.name !== data.active;
  }

  // *column* keeps focus in that column of the step (Alt+Up/Down); otherwise
  // it goes to the step's Actions cell, where its actions return.
  function renderSteps(focusIndex = null, { column, announceFocus = focusIndex === null } = {}) {
    if (editing !== null) {
      if (legend) legend.textContent = `Edit step ${editing + 1}`;
      addButton.textContent = `Update step ${editing + 1}`;
    }
    if (undoButton) {
      undoButton.disabled = false;
      undoButton.setAttribute("aria-disabled", String(!undoHistory.length || editing !== null));
    }
    stepTable.update(clock?.steps || [], { announceFocus });
    [...stepTable.body.querySelectorAll("tr[data-row-key]")].forEach((row, index) => { row.dataset.index = String(index); });
    renderHeading();
    renderNext();
    if (focusIndex !== null) focusRow(focusIndex, column);
  }

  // A category renamed or deleted under Categories changes the words of the
  // steps that name it.  Only the words change, so focus and the grid's tab
  // stop stay where they are.
  function renderStepWords() {
    if (clock) renderSteps();
  }

  // A step's cell in *column*, or its Actions cell, where its actions return.
  function rowTarget(index, column) {
    const step = clock?.steps[index];
    return step ? stepTable.target(stepKey(step), column) : null;
  }

  function focusRow(index, column) {
    rowTarget(index, column)?.focus();
  }

  function changed(message, focusIndex, column) {
    dirty = true;
    renderList();
    renderSteps(focusIndex, { column });
    say(`${message} Not saved yet.`);
  }

  function move(index, by, column) {
    const to = index + by;
    if (to < 0 || to >= clock.steps.length) {
      say(by < 0 ? "This is already the first step." : "This is already the last step.");
      return;
    }
    rememberSteps(index);
    const [step] = clock.steps.splice(index, 1);
    clock.steps.splice(to, 0, step);
    if (editing === index) editing = to;
    else if (editing === to) editing = index;
    changed(`Moved. ${position(to)}.`, to, column);
  }

  // Removes a step and renders the list; returns what to say and where focus belongs.
  function removeStep(index) {
    rememberSteps(index);
    const gone = describe(clock.steps[index]);
    clock.steps.splice(index, 1);
    if (editing !== null) resetForm();
    dirty = true;
    renderList();
    renderSteps(null, { announceFocus: false });
    const left = clock.steps.length;
    const message = `Deleted step ${index + 1}, ${gone}.`;
    if (!left) return { message: `${message} No steps are left. Not saved yet.`, target: newStep };
    return {
      message: `${message} ${left === 1 ? "1 step remains" : `${left} steps remain`}. Not saved yet.`,
      target: rowTarget(Math.min(index, left - 1)),
    };
  }

  function remove(index) {
    return deleteCollectionItem(doc, { undo: true, onConfirm: () => {
      const { message, target } = removeStep(index);
      target?.focus();
      say(message);
      return target;
    } });
  }

  function rememberSteps(index) {
    const steps = copy(clock.steps);
    // Snapshots copy values, but each logical step keeps its collection key.
    steps.forEach((step, at) => stepKeys.set(step, stepKey(clock.steps[at])));
    undoHistory.push({ steps, index });
  }

  function undoSteps() {
    if (editing !== null) { say("Finish editing the step before undoing."); return; }
    if (!clock || !undoHistory.length) { say("No step changes to undo."); return; }
    const previous = undoHistory.pop();
    clock.steps = previous.steps;
    resetForm();
    const index = clock.steps.length ? Math.min(previous.index, clock.steps.length - 1) : null;
    changed("Undid the last step change.", index);
    if (index === null) newStep?.focus();
  }

  function undoKey(event) {
    if (!event.ctrlKey || event.altKey || event.metaKey || event.shiftKey || event.key.toLowerCase() !== "z") return;
    if (editing !== null) return;
    // Leave text editing to the browser, including the name and condition fields.
    if (event.target.closest?.("input, textarea, [contenteditable]")) return;
    if (!undoHistory.length) return;
    event.preventDefault();
    if (!event.repeat) undoSteps();
  }
  editor?.addEventListener("keydown", undoKey);
  undoButton?.addEventListener("click", undoSteps);

  function rowAction(action, index) {
    if (!clock || (action === "delete" && editing !== null)) return;
    if (action === "edit") startEdit(index);
    else if (action === "move-up") move(index, -1);
    else if (action === "move-down") move(index, 1);
    else if (action === "duplicate") {
      rememberSteps(index);
      clock.steps.splice(index + 1, 0, copy(clock.steps[index]));
      if (editing !== null && editing > index) editing += 1;
      changed(`Duplicated as ${position(index + 1)}.`, index + 1);
    } else if (action === "delete") remove(index);
  }

  steps.addEventListener("keydown", (event) => {
    if (event.target.closest('[role="menu"]')) return;
    const row = event.target.closest?.("tr[data-index]");
    if (!row || !clock) return;
    if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
      event.preventDefault();
      if (!event.repeat) move(Number(row.dataset.index), event.key === "ArrowUp" ? -1 : 1,
        event.target.closest?.("td")?.dataset.action);
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.key === "Delete") {
      event.preventDefault();
      if (!event.repeat) rowAction("delete", Number(row.dataset.index));
    }
  });

  // ---- Step form ------------------------------------------------------
  function showFields() {
    const visible = new Set(FIELDS_FOR[typeSelect.value] || []);
    for (const id of ALL_FIELDS) el(`clock-step-${id}`)?.closest(".clock-step-field")?.toggleAttribute("hidden", !visible.has(id));
    if (FOLDER_TYPES.has(typeSelect.value) && !el("clock-step-conditions").children.length) renderConditions();
    renderOptions();
  }

  function resetForm() {
    clearStepError();
    stepCountGeneration += 1;
    if (stepCountTimer !== null) clearTimeout(stepCountTimer);
    stepCountTimer = null;
    pendingStepCount = null;
    const countText = el("clock-step-match-count");
    if (countText) countText.textContent = "Matching track count appears while editing.";
    editing = null;
    const match = el("clock-step-match");
    if (match) match.value = "all";
    renderConditions([{}]);
    const requireFollowing = el("clock-step-require-following");
    if (requireFollowing) requireFollowing.checked = false;
    fillAdFields();
    fillStepHours(doc);
    if (legend) legend.textContent = "Add a step";
    addButton.textContent = "Add step";
    stepEditor.close();
  }

  function fillAdFields(step = {}) {
    const spots = el("clock-step-ad-spots");
    if (spots) spots.value = String(step.max_spots ?? 3);
    const seconds = el("clock-step-ad-seconds");
    if (seconds) seconds.value = String(step.max_seconds ?? 0);
    fillAdChoice(el("clock-step-ad-advertiser"), "Any advertiser", data.ad_advertisers, step.advertiser || "");
    fillAdChoice(el("clock-step-ad-category"), "Any category", data.ad_categories, step.category || "");
    clearAdError();
  }

  function startEdit(index) {
    const step = clock.steps[index];
    editing = index;
    renderSteps();
    typeSelect.value = step.type === "category" || !(step.type in FIELDS_FOR) ? "music" : step.type;
    showFields();
    scheduleStepCount();
    const set = (id, value) => { const field = el(id); if (field) field.value = value; };
    if (step.type === "track") set("clock-step-source", step.source || "");
    set("clock-step-path", step.path || "");
    set("clock-step-preset", step.preset || "");
    if (step.type === "ad_break") fillAdFields(step);
    if (FOLDER_TYPES.has(typeSelect.value)) {
      set("clock-step-match", step.match || "all");
      renderConditions(step.type === "category"
        ? [{ field: "category", category: step.category }]
        : step.conditions || [{ field: "folder", source: step.source, folder: step.folder || "", recursive: Boolean(step.recursive) }]);
    }
    set("clock-step-selection", step.selection || "least_recent");
    const check = (id, value) => { const field = el(id); if (field) field.checked = value; };
    check("clock-step-enforce-rules", step.enforce_rules !== false);
    check("clock-step-require-following", Boolean(step.require_following));
    if (legend) legend.textContent = `Edit step ${index + 1}`;
    addButton.textContent = `Update step ${index + 1}`;
    fillStepHours(doc, step);
    scheduleStepCount();
    stepEditor.open(typeSelect, rowTarget(index));
    say(`Editing ${position(index)}.`);
  }

  function relative(id, label, optional, interactive = true) {
    const field = el(id);
    const value = (field?.value || "").trim().replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
    const bad = value.split("/").some((part) => !part.trim() || part === "." || part === "..") || value.includes(":");
    if (value ? bad : !optional) {
      const message = `${label} must be a path inside the library folder, such as Rock/Live, with no "..".`;
      if (interactive) showFieldError(field, message, stepError, null, { focus: false });
      throw Object.assign(new Error(message), { control: field });
    }
    clearFieldError(field, stepError);
    return value;
  }

  function formStep(interactive = true) {
    const type = typeSelect.value;
    if (FOLDER_TYPES.has(type)) {
      const step = {
        type,
        match: el("clock-step-match").value,
        conditions: readConditions(interactive),
        selection: el("clock-step-selection").value,
        enforce_rules: el("clock-step-enforce-rules").checked,
      };
      if (type === "liner") step.require_following = el("clock-step-require-following").checked;
      return step;
    }
    if (type === "track") {
      const source = el("clock-step-source")?.value || "";
      if (!source) {
        if (interactive) el("clock-step-source")?.focus();
        throw Object.assign(new Error(`Add a ${type === "liner" ? "voice liner" : "music"} folder under Music folders and Voice liner folders first.`), { control: el("clock-step-source") });
      }
      return { type, source, path: relative("clock-step-path", "The track", false, interactive) };
    }
    if (type === "settings") {
      const preset = el("clock-step-preset")?.value || "";
      if (!preset) throw Object.assign(new Error("Choose a preset."), { control: el("clock-step-preset") });
      return { type, preset };
    }
    if (type === "ad_break") return adStep(interactive);
    throw new Error(`The editor cannot make ${type} steps yet.`);
  }

  function scheduleStepCount() {
    if (stepCountTimer !== null) clearTimeout(stepCountTimer);
    const mine = ++stepCountGeneration;
    const output = el("clock-step-match-count");
    if (typeSelect.value === "settings" || typeSelect.value === "ad_break") {
      if (output) output.textContent = typeSelect.value === "settings" ? "Preset changes do not select tracks." : "Ad breaks do not select tracks.";
      return;
    }
    if (output) output.textContent = "Checking matching tracks…";
    stepCountTimer = setTimeout(async () => {
      stepCountTimer = null;
      const epoch = captureAuthenticatedRequestEpoch();
      let step;
      try { step = formStep(false); }
      catch { if (stepCountCurrent(epoch, mine) && output) output.textContent = "Complete the step conditions to check matching tracks."; return; }
      pendingStepCount = { epoch, mine, step };
      void drainStepCounts();
    }, 450);
  }

  async function drainStepCounts() {
    if (stepCountRunning) return;
    stepCountRunning = true;
    try {
      while (pendingStepCount) {
        const item = pendingStepCount;
        pendingStepCount = null;
        const output = el("clock-step-match-count");
        if (!stepCountCurrent(item.epoch, item.mine) || !output) continue;
        try {
          const result = await send("/api/library/clock/check", "POST", { step: item.step });
          if (!stepCountCurrent(item.epoch, item.mine)) continue;
          output.textContent = result.error
            ? `Count unavailable: ${result.error}`
            : result.tracks === null ? "Preset changes do not select tracks."
              : `${Number(result.tracks).toLocaleString("en-US")} ${Number(result.tracks) === 1 ? "track" : "tracks"}`;
          if (result.error || result.tracks !== null) makeSay(() => stepEditor.region(status), { force: false })(output.textContent, result.error ? "error" : "info");
        } catch (error) {
          if (stepCountCurrent(item.epoch, item.mine)) {
            output.textContent = `Count unavailable: ${error.message}`;
            makeSay(() => stepEditor.region(status), { force: false })(output.textContent, "error");
          }
        }
      }
    } finally { stepCountRunning = false; }
  }

  typeSelect.addEventListener("change", () => {
    let changedCategory = false;
    const kind = typeSelect.value === "liner" ? "liner" : "music";
    for (const row of el("clock-step-conditions").children) {
      const selects = row.querySelectorAll("select");
      const [field, source] = selects;
      if (!data.sources.some((item) => item.id === source.value && item.kind === kind)) source.value = "";
      const categoryOption = field.querySelector('option[value="category"]');
      if (typeSelect.value === "liner") {
        if (field.value === "category") {
          changedCategory = true;
          field.value = "folder";
          field.dispatchEvent(new Event("change"));
        }
        categoryOption?.remove();
      } else if (!categoryOption) {
        const option = doc.createElement("option");
        option.value = "category";
        option.textContent = "Category";
        field.append(option);
      }
    }
    showFields();
    if (changedCategory) say("Music category conditions changed to empty folder conditions for liner steps.");
  });
  el("clock-add-condition")?.addEventListener("click", () => {
    const host = el("clock-step-conditions");
    if (host.children.length >= 32) {
      say("A step can have at most 32 conditions.", "error");
      return;
    }
    const controls = conditionRow();
    host.append(controls.row);
    updateBeetsNotice();
    scheduleStepCount();
    controls.field.focus();
    say(`Condition ${host.children.length} added.`);
  });
  el("clock-step-form")?.addEventListener("input", (event) => {
    clearStepError();
    if (event.target.closest?.("#clock-step-conditions-field") || event.target === typeSelect) scheduleStepCount();
  });
  el("clock-step-form")?.addEventListener("change", (event) => {
    clearStepError();
    if (event.target.closest?.("#clock-step-conditions-field") || event.target === typeSelect) scheduleStepCount();
  });
  el("clock-step-conditions")?.addEventListener("click", (event) => {
    if (event.target.closest?.("button[data-remove-condition]")) scheduleStepCount();
  });
  function clearStepError() {
    clearFormErrors(el("clock-step-form"), stepError);
  }
  function saveStep() {
    clearStepError();
    if (!clock) { say("Open or create a clockwheel first.", "error"); return; }
    let step;
    try { step = { ...formStep(), ...readStepHours(doc) }; }
    catch (error) {
      const control = error.control || el("clock-step-form").querySelector('[aria-invalid="true"]');
      const existing = control?.getAttribute("aria-describedby")?.split(/\s+/)
        .map(el).find((node) => node?.classList.contains("field-error") && !node.hidden);
      showFieldError(control, error.message, existing || stepError, stepEditor.status);
      return;
    }
    rememberSteps(editing ?? clock.steps.length);
    if (editing !== null) {
      const index = editing;
      stepKeys.set(step, stepKey(clock.steps[index]));
      clock.steps[index] = step;
      resetForm();
      changed(`Updated ${position(index)}.`, index);
    } else {
      clock.steps.push(step);
      resetForm();
      changed(`Added ${position(clock.steps.length - 1)}.`, clock.steps.length - 1);
    }
  }
  newStep?.addEventListener("click", () => {
    if (!clock) { say("Open or create a clockwheel first.", "error"); return; }
    resetForm();
    showFields();
    scheduleStepCount();
    stepEditor.open(typeSelect, newStep);
  });
  function changedClockSettings() {
    if (!clock) return;
    if (!dirty) { dirty = true; renderList(); }
    renderNext();
  }
  resync.addEventListener("input", changedClockSettings);
  stationName?.addEventListener("input", () => {
    if (stationNameError) clearFieldError(stationName, stationNameError);
    changedClockSettings();
  });
  // A refused station name: marked, its error tied to it and said, focus on it.
  function refuseStationName(message) {
    if (stationName && stationNameError) showFieldError(stationName, message, stationNameError, stepEditor.region(status));
    else say(message, "error");
  }
  el("clock-step-match")?.addEventListener("change", scheduleStepCount);

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
    undoHistory = [];
    clock = copy(value);
    isNew = fresh;
    dirty = fresh;
    savedPicking = fresh ? null : picking(value);
    resetForm();
    resync.value = clock.resync_minutes.join(", ");
    clearFieldError(resync, el("clock-resync-error"));
    if (stationName) {
      stationName.value = clock.station_name || "";
      if (stationNameError) clearFieldError(stationName, stationNameError);
    }
    renderList();
    renderSteps();
  }

  function close() {
    undoHistory = [];
    clock = null; isNew = false; dirty = false;
    resetForm();
    renderList();
    renderSteps();
  }

  async function openClock(name, { quiet = false } = {}) {
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    if (!(await discardChanges()) || !current(epoch, mine)) return;
    try {
      const value = await request(clockUrl(name));
      if (!current(epoch, mine)) return;
      load(value);
      if (!quiet) say(`Opened ${name}, ${count(value.steps.length)}${name === data.active ? ", active" : ""}.`);
    } catch (error) { if (current(epoch, mine)) say(failureText(`open ${name}`, error), "error"); }
  }

  async function refresh() {
    if (!panel || panel.hidden || doc.hidden || suspended) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    const requestId = ++listGeneration;
    try {
      const value = await request("/api/library/clock");
      if (!current(epoch, mine) || requestId !== listGeneration) return;
      loadFailed = false;
      data = { ...empty(), ...value };
      renderList();
      renderOptions();
      renderStepWords();
      renderNotice();
      // The first look at the panel opens the active clockwheel, silently.
      if (!clock && data.active && data.names.includes(data.active)) await openClock(data.active, { quiet: true });
      else renderNext();
    } catch (error) {
      // A failed automatic refresh is said once, not on every retry.
      if (current(epoch, mine) && !loadFailed) say(failureText("load clockwheels", error), "error");
      if (current(epoch, mine)) loadFailed = true;
    }
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
        if (isStationNameError(error.message)) { refuseStationName(error.message); return null; }
        say(error.message, "error");
        const step = /^Step (\d+):/.exec(error.message);
        if (step && clock) focusRow(Number(step[1]) - 1);
      }
      return null;
    }
  }

  function savedActionReason(name) {
    if (isDraft(name)) return `Save ${name} before renaming or activating it.`;
    return clock?.name === name && dirty
      ? `Save the changes to ${name}, or open it again to drop them, before changing the saved clockwheel.` : "";
  }

  function savedActionAllowed(name) {
    const reason = savedActionReason(name);
    if (reason) {
      say(reason, "error");
      return false;
    }
    return true;
  }

  const nameForm = doc.createElement("form");
  nameForm.id = "clock-name-form";
  nameForm.hidden = true;
  const nameHeading = doc.createElement("h1");
  nameHeading.id = "clock-name-legend";
  const nameLabel = doc.createElement("label");
  nameLabel.htmlFor = "clock-editor-name";
  nameLabel.textContent = "Name";
  const editorName = doc.createElement("input");
  editorName.id = nameLabel.htmlFor;
  editorName.type = "text";
  editorName.maxLength = 64;
  editorName.autocomplete = "off";
  const nameError = doc.createElement("p");
  nameError.id = "clock-editor-name-error";
  nameError.className = "field-error";
  nameError.hidden = true;
  const nameActions = doc.createElement("div");
  nameActions.className = "dialog-actions";
  const nameSubmit = doc.createElement("button");
  nameSubmit.type = "submit";
  nameSubmit.textContent = "Save";
  const nameCancel = doc.createElement("button");
  nameCancel.type = "button";
  nameCancel.textContent = "Cancel";
  nameActions.append(nameSubmit, nameCancel);
  nameForm.append(nameHeading, nameLabel, editorName, nameError, nameActions);
  let naming = null;
  const nameEditor = createEditorDialog({ form: nameForm, heading: nameHeading, cancel: nameCancel,
    fallback: el("clock-create"), save: saveName });

  async function openNameEditor(action, name = null, from = el("clock-create")) {
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    if (name && action !== "duplicate" && !savedActionAllowed(name)) return;
    if ((action === "create" || action === "duplicate") && !(await discardChanges())) return;
    if (!current(epoch, mine)) return;
    naming = { action, name };
    nameHeading.textContent = action === "create" ? "Create clockwheel" : `${action === "rename" ? "Rename" : "Duplicate"} ${name}`;
    nameSubmit.textContent = action === "create" ? "Create" : action === "rename" ? "Rename" : "Duplicate";
    editorName.value = action === "rename" ? name : action === "create" ? nameInput.value.trim() : "";
    clearFieldError(editorName, nameError);
    nameEditor.open(editorName, from);
  }

  async function saveName(event, active) {
    event.preventDefault();
    const name = editorName.value.trim();
    const { action, name: source } = naming;
    if (!NAME.test(name) || RESERVED.test(name)) {
      showFieldError(editorName, "A clockwheel name needs 1 to 64 letters, digits, spaces, dashes or underscores.", nameError, nameEditor.status);
      return;
    }
    if (data.names.some((item) => item.toLowerCase() === name.toLowerCase())) {
      showFieldError(editorName, `A clockwheel named ${name} already exists. Type another name.`, nameError, nameEditor.status);
      return;
    }
    if (action === "create") {
      nameEditor.close();
      load({ version: 1, name, steps: [], resync_minutes: [0] }, { fresh: true });
      newStep?.focus();
      say(`New clockwheel ${name}. Add its steps, then press Save clockwheel.`);
      return;
    }
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    const result = await send(`${clockUrl(source)}/${action}`, "POST", { name });
    if (!active() || !current(epoch, mine)) return;
    // Renaming another saved row must not replace the current working copy.
    if (action === "duplicate" || clock?.name === source) load(result);
    await refresh();
    if (!current(epoch, mine)) return;
    nameEditor.close();
    (savedTable.target(name) || el("clock-create")).focus();
    say(action === "duplicate" ? `Copied ${source} as ${name}, and opened the copy.` : `Renamed ${source} to ${name}.`);
  }

  async function activateClock(name) {
    const turnOff = name === data.active;
    if (!turnOff && !savedActionAllowed(name)) return;
    if (await command(() => send("/api/library/clock/active", "POST", { name: turnOff ? null : name }))) {
      say(turnOff ? "No clockwheel is active now." : `${name} is the active clockwheel. It plays when Pick mode is set to Clockwheel.`);
    }
  }

  async function deleteClock(name) {
    let deleted = null;
    const index = clockNames().indexOf(name);
    const draft = isDraft(name);
    const discard = clock?.name === name && dirty ? ` Unsaved changes to ${name} will also be discarded.` : "";
    const message = draft
      ? "This clockwheel has not been saved. Its unsaved changes will be discarded."
      : (name === data.active
        ? "It is the active clockwheel, so no clockwheel plays until you activate another. Its references are removed from saved profiles and station defaults."
        : "Its file is removed, along with its references in saved profiles and station defaults. Backups made earlier still hold it.") + discard;
    // The request runs while the dialog is open; the result is said after it closes.
    await deleteCollectionItem(doc, {
      title: `Delete clockwheel ${name}?`,
      message,
      confirmLabel: "Delete clockwheel",
      onConfirm: async () => {
        if (draft) {
          if (!isDraft(name)) return null;
          close();
          deleted = true;
        } else {
          deleted = await command(() => send(clockUrl(name), "DELETE"), () => { if (clock?.name === name) close(); });
        }
        const names = clockNames();
        return deleted ? savedTable.target(names[Math.min(index, names.length - 1)]) || el("clock-create") : null;
      },
    });
    if (deleted) say(draft ? `Dropped the new clockwheel ${name}; it was never saved.` : `Deleted ${name}.`);
  }

  el("clock-create")?.addEventListener("click", () => void openNameEditor("create"));

  el("clock-save")?.addEventListener("click", async () => {
    if (!clock) { say("Open or create a clockwheel first.", "error"); return; }
    let minutes;
    try { minutes = parseResyncMinutes(resync.value); } catch (error) {
      showFieldError(resync, error.message, el("clock-resync-error"), stepEditor.region(status));
      return;
    }
    clearFieldError(resync, el("clock-resync-error"));
    const named = checkStationName(stationName?.value);
    if (named.error) { refuseStationName(named.error); return; }
    if (stationNameError) clearFieldError(stationName, stationNameError);
    const body = { version: 1, name: clock.name, steps: clock.steps, resync_minutes: minutes, station_name: named.name };
    const timing = el("clock-apply-timing")?.value || "next_restart";
    // Only the station name changed: nothing waits for a restart.
    const nameOnly = !isNew && savedPicking === picking(body);
    const nameChanged = (clock.station_name || "") !== named.name;
    const url = isNew ? "/api/library/clock" : `${clockUrl(clock.name)}?apply_timing=${encodeURIComponent(timing)}`;
    const saved = await command(() => send(url, isNew ? "POST" : "PUT", body),
      (result) => load(result));
    if (saved) {
      const activeEdit = !isNew && data.active === saved.name;
      let timingMessage = "";
      if (activeEdit && nameOnly) timingMessage = nameChanged ? " The station name applies now." : "";
      else if (activeEdit) {
        timingMessage = timing === "next_restart"
          ? " Changes apply at the next clock restart."
          : timing === "continue"
            ? " Changes apply immediately, continuing at the next step."
            : " Changes apply immediately from step 1.";
      }
      say(`Saved ${saved.name}, ${count(saved.steps.length)}.${timingMessage}`);
    }
  });

  el("clock-import")?.addEventListener("click", async () => {
    const fileInput = el("clock-pal-file");
    const file = fileInput?.files?.[0];
    if (!file) { fileInput?.focus(); say("Choose a .PAL file first.", "error"); return; }
    if (file.size > 256 * 1024) { say(".PAL files must be 256 KB or smaller.", "error"); return; }
    let name, minutes;
    try { name = typedName(); minutes = parseResyncMinutes(resync.value); } catch (error) { say(error.message, "error"); return; }
    if (!(await discardChanges())) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    let text;
    try { text = await file.text(); } catch (error) {
      if (current(epoch, mine)) say(failureText(`read ${file.name}`, error), "error");
      return;
    }
    if (!current(epoch, mine)) return;
    const imported = await command(() => send("/api/library/clock/import", "POST", { text, name, resync_minutes: minutes }),
      (result) => load(result));
    if (imported) say(`Imported ${imported.name} from ${file.name}, ${count(imported.steps.length)}. Choose Options for ${imported.name}, then Activate.`);
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
    undoHistory = [];
    clock = null; isNew = false; dirty = false;
    optionsSignature = ""; lastNotice = null; lastPendingUpdate = false;
    savedTable.clear();
    nameEditor.close();
    nameInput.value = "";
    resync.value = "0";
    if (stationName) {
      stationName.value = "";
      if (stationNameError) clearFieldError(stationName, stationNameError);
    }
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
    // A station switch drops the open clockwheel, so the page asks first.
    hasUnsavedWork: () => Boolean(clock && dirty),
    resume() { suspended = false; updatePolling(); },
    destroy() {
      clearProtectedData();
      stepEditor.destroy();
      nameEditor.destroy();
      editor?.removeEventListener("keydown", undoKey);
      undoButton?.removeEventListener("click", undoSteps);
      window.removeEventListener("hashchange", updatePolling);
      doc.removeEventListener("visibilitychange", updatePolling);
    },
  };
}
