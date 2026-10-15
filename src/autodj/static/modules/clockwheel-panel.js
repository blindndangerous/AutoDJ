// Clockwheels: a list of saved clockwheels and an editor for one of them.
//
// The server keeps each clockwheel as a native JSON file.  This module
// opens one into a working copy, edits its steps in the page, and sends it
// back only when Save clockwheel is pressed; nothing is saved on its own.
//
// The <ol> is a layout grid with a row header and a cell for each native
// button. The step name is the Edit button. Keyboard focus moves between
// the cells themselves (one roving tab stop), not the buttons: NVDA turns
// on focus mode when a grid cell gets focus, but not for a button inside
// one, and in browse mode it would keep the arrow keys. Enter or Space on
// a cell presses its button. Up/Down moves between names, Left/Right moves
// among row actions, and Alt+Up/Down moves the focused step. After every
// change focus stays with the step that moved, or the nearest one. A settled
// track count is also announced once through #clock-status.
//
// Folders are chosen from the registered library folders by ID, with an
// optional path inside the folder; the browser never sends a full path.
// Category conditions name a saved category (Categories, above) by its ID.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";
import { parsePointTime } from "./library-folders.js";
import { createConditionRow, readConditionRows } from "./condition-editor.js";
import { describeStep } from "./clockwheel-step.js";
export { describeStep } from "./clockwheel-step.js";
import { fillStepHours, installStepHours, readStepHours } from "./step-hours.js";

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

export function installClockwheelPanel({ document: doc = document, request = requestJson } = {}) {
  const el = (id) => doc.getElementById(id);
  const panel = el("panel-library");
  const list = el("clock-list");
  const steps = el("clock-slots");
  const undoButton = el("clock-undo");
  const editor = steps?.closest(".card");
  const nameInput = el("clock-name");
  const resync = el("clock-resync-minutes");
  const status = el("clock-status");
  const typeSelect = el("clock-step-type");
  const newStep = el("clock-new-step");
  const addButton = el("clock-add-step");
  const cancelEdit = el("clock-cancel-edit");
  const stepDialog = el("clock-step-dialog");
  function openStepDialog() {
    if (stepDialog && !stepDialog.open) stepDialog.showModal();
  }
  const legend = el("clock-step-legend");
  if (!panel || !list || !steps || !nameInput || !resync || !status || !typeSelect || !addButton) {
    return { refresh: async () => {}, clearProtectedData() {}, resume() {}, destroy() {} };
  }

  const empty = () => ({
    names: [], active: null, sources: [], presets: [], categories: [], cursor: 0, notice: null, pending_update: false,
    ad_advertisers: [], ad_categories: [], ads_reason: null,
  });
  let data = empty();
  let clock = null; // the working copy, or null when none is open
  let isNew = false; // created in the page and never saved
  let undoHistory = [];
  let dirty = false;
  let editing = null; // index of the step in the form, or null when adding
  let listSignature = "";
  let optionsSignature = "";
  let lastNotice = null;
  let lastPendingUpdate = false;
  let timer = null;
  let generation = 0;
  let suspended = false;
  let conditionId = 0;
  let stepCountTimer = null;
  let stepCountGeneration = 0;
  let stepCountRunning = false;
  let pendingStepCount = null;

  const current = (epoch, mine) => !suspended && generation === mine && isAuthenticatedRequestCurrent(epoch);
  const stepCountCurrent = (epoch, mine) => !suspended && stepCountGeneration === mine && isAuthenticatedRequestCurrent(epoch);
  const say = (message, error = false) => announceStatus(status, message, { force: true, tone: error ? "error" : "info" });
  const clockUrl = (name) => `/api/library/clock/${encodeURIComponent(name)}`;
  const send = (url, method, body) => request(url, body === undefined ? { method } : { method, headers: JSON_HEADERS, body: JSON.stringify(body) });
  const count = (n) => `${n} ${n === 1 ? "step" : "steps"}`;
  // A step with days and hours says them after what it plays.
  installStepHours(doc);
  // A refused ad break field is unmarked once it is changed.
  for (const id of AD_FIELDS) {
    el(id)?.addEventListener("input", () => { el(id).removeAttribute("aria-invalid"); el(id).removeAttribute("aria-describedby"); });
  }
  const describe = (step) => describeStep(step, data.sources, data.categories);
  const position = (index) => `Step ${index + 1} of ${clock.steps.length}, ${describe(clock.steps[index])}`;

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

  function clearAdError() {
    for (const id of AD_FIELDS) {
      el(id)?.removeAttribute("aria-invalid");
      el(id)?.removeAttribute("aria-describedby");
    }
    const error = el(AD_ERROR);
    if (error) { error.textContent = ""; error.hidden = true; }
  }

  function refuseAd(field, message, interactive) {
    if (interactive) {
      clearAdError();
      field.setAttribute("aria-invalid", "true");
      field.setAttribute("aria-describedby", AD_ERROR);
      const error = el(AD_ERROR);
      if (error) { error.textContent = message; error.hidden = false; }
      field.focus();
    }
    throw new Error(message);
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
    const fields = ["folder", "year", "bpm", "duration", "artist", "title", "album", "genre", "beets"];
    if (typeSelect.value === "music") fields.push("category");
    const sourceList = data.sources.filter((s) => s.kind === (typeSelect.value === "liner" ? "liner" : "music"));
    return createConditionRow({
      document: doc,
      nextId: () => `clock-condition-${++conditionId}`,
      index: el("clock-step-conditions").children.length + 1,
      value,
      fields,
      sourceChoices: sourceList.map((item) => [item.id, item.path]),
      genreChoices: (data.genres || []).map((item) => [item, item]),
      beetsChoices: (data.beets_fields || []).map((item) => [item, item]),
      categoryChoices: data.categories.map((item) => [item.id, item.name]),
      onFieldChange: updateBeetsNotice,
      onRemove: (row) => {
      const host = row.parentElement;
      const index = [...host.children].indexOf(row);
      row.remove();
      const left = [...host.children];
      left.forEach((item, at) => {
        item.querySelector("legend").textContent = `Condition ${at + 1}`;
        item.querySelector("button[data-remove-condition]").textContent = `Remove condition ${at + 1}`;
      });
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
      throw new Error("Add at least one condition.");
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
    else if (data.pending_update) next.textContent = "Saved changes are waiting for the next clock restart.";
    else if (!clock.steps.length) next.textContent = "";
    else next.textContent = `Plays next: ${position((data.cursor || 0) % clock.steps.length)}.`;
    const pending = Boolean(data.pending_update && clock && clock.name === data.active);
    if (pending && !lastPendingUpdate) say("Saved changes are waiting for the next clock restart.");
    lastPendingUpdate = pending;
    const apply = el("clock-apply-timing");
    if (apply) apply.hidden = !clock || isNew || clock.name !== data.active;
  }

  function renderSteps(focusIndex = null, action = "edit") {
    if (editing !== null) {
      if (legend) legend.textContent = `Edit step ${editing + 1}`;
      addButton.textContent = `Update step ${editing + 1}`;
    }
    if (undoButton) undoButton.disabled = !undoHistory.length || editing !== null;
    if (!clock || !clock.steps.length) {
      const none = doc.createElement("li");
      none.setAttribute("role", "row");
      const cell = doc.createElement("div");
      cell.setAttribute("role", "gridcell");
      cell.textContent = clock ? "No steps yet. Press New step to add one." : "Open or create a clockwheel to see its steps.";
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
          // The cell takes keyboard focus, which turns on NVDA's focus mode;
          // its button is for the mouse and browse mode, out of the tab order.
          const cell = doc.createElement("span");
          cell.setAttribute("role", "gridcell");
          cell.dataset.action = key;
          cell.tabIndex = index === 0 && key === "edit" ? 0 : -1;
          const button = doc.createElement("button");
          button.type = "button";
          button.dataset.action = key;
          button.tabIndex = -1;
          if (key === "edit") {
            button.textContent = describe(step);
            cell.append(button);
            item.append(cell);
            continue;
          }
          // The visible words start the name, which says which step.
          button.textContent = title;
          button.setAttribute("aria-label", `${title} step ${index + 1}`);
          button.disabled = (key === "move-up" && index === 0) || (key === "move-down" && index === clock.steps.length - 1)
            || (key === "delete" && editing !== null);
          if (button.disabled) cell.setAttribute("aria-disabled", "true");
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

  const CELL = '[role="gridcell"][data-action]';
  const ENABLED_CELL = `${CELL}:not([aria-disabled="true"])`;

  // Makes one cell the list's only tab stop.
  function makeTabStop(cell) {
    steps.querySelectorAll(CELL).forEach((item) => { item.tabIndex = item === cell ? 0 : -1; });
  }

  // Makes one cell of a row the list's tab stop and returns it.
  function rowTarget(index, action) {
    const row = steps.querySelector(`li[data-index="${index}"]`);
    if (!row) return null;
    const other = { "move-up": "move-down", "move-down": "move-up" }[action] || "edit";
    const target = row.querySelector(`[role="gridcell"][data-action="${action}"]:not([aria-disabled="true"])`)
      || row.querySelector(`[role="gridcell"][data-action="${other}"]:not([aria-disabled="true"])`)
      || row.querySelector('[role="gridcell"][data-action="edit"]');
    makeTabStop(target);
    return target;
  }

  function focusRow(index, action) {
    rowTarget(index, action)?.focus();
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
    rememberSteps(index);
    const [step] = clock.steps.splice(index, 1);
    clock.steps.splice(to, 0, step);
    if (editing === index) editing = to;
    else if (editing === to) editing = index;
    changed(`Moved. ${position(to)}.`, to, action);
  }

  // Removes a step and renders the list; returns what to say and where focus belongs.
  function removeStep(index) {
    rememberSteps(index);
    const gone = describe(clock.steps[index]);
    clock.steps.splice(index, 1);
    if (editing !== null) resetForm();
    dirty = true;
    renderSteps();
    const left = clock.steps.length;
    const message = `Deleted step ${index + 1}, ${gone}.`;
    if (!left) return { message: `${message} No steps are left. Not saved yet.`, target: newStep };
    return {
      message: `${message} ${left === 1 ? "1 step remains" : `${left} steps remain`}. Not saved yet.`,
      target: rowTarget(Math.min(index, left - 1), "edit"),
    };
  }

  function remove(index) {
    const { message, target } = removeStep(index);
    target?.focus();
    say(message);
  }

  function rememberSteps(index) {
    undoHistory.push({ steps: copy(clock.steps), index });
  }

  function undoSteps() {
    if (!clock || editing !== null || !undoHistory.length) return;
    const previous = undoHistory.pop();
    clock.steps = previous.steps;
    resetForm();
    const index = clock.steps.length ? Math.min(previous.index, clock.steps.length - 1) : null;
    changed("Undid the last step change.", index, "edit");
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
    else if (action === "move-up") move(index, -1, action);
    else if (action === "move-down") move(index, 1, action);
    else if (action === "duplicate") {
      rememberSteps(index);
      clock.steps.splice(index + 1, 0, copy(clock.steps[index]));
      if (editing !== null && editing > index) editing += 1;
      changed(`Duplicated as ${position(index + 1)}.`, index + 1, action);
    } else if (action === "delete") remove(index);
  }

  steps.addEventListener("click", (event) => {
    const button = event.target.closest?.("button[data-action]");
    const row = button?.closest("li[data-index]");
    if (row) {
      makeTabStop(button.closest(CELL));
      rowAction(button.dataset.action, Number(row.dataset.index));
    }
  });
  steps.addEventListener("keydown", (event) => {
    const row = event.target.closest?.("li[data-index]");
    if (!row || !clock) return;
    if (event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey
        && (event.key === "Home" || event.key === "End")) {
      event.preventDefault();
      focusRow(event.key === "Home" ? 0 : clock.steps.length - 1, "edit");
    } else if (event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
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
      const cells = [...row.querySelectorAll(ENABLED_CELL)];
      const at = cells.indexOf(event.target.closest?.(CELL));
      const next = at + (event.key === "ArrowLeft" ? -1 : 1);
      event.preventDefault();
      if (next >= 0 && next < cells.length) focusRow(Number(row.dataset.index), cells[next].dataset.action);
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && (event.key === "Home" || event.key === "End")) {
      const cells = [...row.querySelectorAll(ENABLED_CELL)];
      event.preventDefault();
      focusRow(Number(row.dataset.index), cells[event.key === "Home" ? 0 : cells.length - 1].dataset.action);
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && event.key === "Delete") {
      event.preventDefault();
      if (!event.repeat) rowAction("delete", Number(row.dataset.index));
    } else if (!event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey
        && (event.key === "Enter" || event.key === " ") && event.target.matches?.(CELL)) {
      // A focused cell acts as its button; a focused button handles these itself.
      event.preventDefault();
      const button = event.target.querySelector("button[data-action]");
      if (!event.repeat && button && !button.disabled) button.click();
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
    if (stepDialog?.open) stepDialog.close();
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
    openStepDialog();
    typeSelect.focus();
    say(`Editing ${position(index)}.`);
  }

  function relative(id, label, optional, interactive = true) {
    const field = el(id);
    const value = (field?.value || "").trim().replaceAll("\\", "/").replace(/^\/+|\/+$/g, "");
    const bad = value.split("/").some((part) => !part.trim() || part === "." || part === "..") || value.includes(":");
    if (value ? bad : !optional) {
      if (interactive) {
        field?.setAttribute("aria-invalid", "true");
        field?.focus();
      }
      throw new Error(`${label} must be a path inside the library folder, such as Rock/Live, with no "..".`);
    }
    field?.removeAttribute("aria-invalid");
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
        throw new Error(`Add a ${type === "liner" ? "voice liner" : "music"} folder under Music folders and Voice liner folders first.`);
      }
      return { type, source, path: relative("clock-step-path", "The track", false, interactive) };
    }
    if (type === "settings") {
      const preset = el("clock-step-preset")?.value || "";
      if (!preset) throw new Error("Choose a preset.");
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
          if (result.error || result.tracks !== null) announceStatus(status, output.textContent, { tone: result.error ? "error" : "info" });
        } catch (error) {
          if (stepCountCurrent(item.epoch, item.mine)) {
            output.textContent = `Count unavailable: ${error.message}`;
            announceStatus(status, output.textContent, { tone: "error" });
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
      say("A step can have at most 32 conditions.", true);
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
    if (event.target.closest?.("#clock-step-conditions-field") || event.target === typeSelect) scheduleStepCount();
  });
  el("clock-step-form")?.addEventListener("change", (event) => {
    if (event.target.closest?.("#clock-step-conditions-field") || event.target === typeSelect) scheduleStepCount();
  });
  el("clock-step-conditions")?.addEventListener("click", (event) => {
    if (event.target.closest?.("button[data-remove-condition]")) scheduleStepCount();
  });
  addButton.addEventListener("click", () => {
    if (!clock) { say("Open or create a clockwheel first.", true); return; }
    let step;
    try { step = { ...formStep(), ...readStepHours(doc) }; } catch (error) { say(error.message, true); return; }
    rememberSteps(editing ?? clock.steps.length);
    if (editing !== null) {
      const index = editing;
      clock.steps[index] = step;
      resetForm();
      changed(`Updated ${position(index)}.`, index, "edit");
    } else {
      clock.steps.push(step);
      resetForm();
      changed(`Added ${position(clock.steps.length - 1)}.`, clock.steps.length - 1, "edit");
    }
  });
  newStep?.addEventListener("click", () => {
    if (!clock) { say("Open or create a clockwheel first.", true); return; }
    resetForm();
    showFields();
    scheduleStepCount();
    openStepDialog();
    typeSelect.focus();
  });
  // Escape closes the dialog as Cancel does.
  stepDialog?.addEventListener("cancel", (event) => {
    event.preventDefault();
    cancelEdit?.click();
  });
  cancelEdit?.addEventListener("click", () => {
    const index = editing;
    resetForm();
    renderSteps();
    if (index !== null) {
      focusRow(index, "edit");
      say("Edit cancelled.");
    } else newStep?.focus();
  });
  resync.addEventListener("input", () => { if (clock) { dirty = true; renderNext(); } });
  el("clock-step-match")?.addEventListener("change", scheduleStepCount);

  // ---- Opening, saving and the clockwheel buttons ---------------------
  function discardChanges() {
    if (!dirty || !clock) return Promise.resolve(true);
    return confirmAction(doc, {
      station: true,
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
    resetForm();
    nameInput.value = clock.name;
    resync.value = clock.resync_minutes.join(", ");
    resync.removeAttribute("aria-invalid");
    listSignature = "";
    renderList();
    renderSteps();
  }

  function close() {
    undoHistory = [];
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
    newStep?.focus();
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
    const timing = el("clock-apply-timing")?.value || "next_restart";
    const url = isNew ? "/api/library/clock" : `${clockUrl(clock.name)}?apply_timing=${encodeURIComponent(timing)}`;
    const saved = await command(() => send(url, isNew ? "POST" : "PUT", body),
      (result) => load(result));
    if (saved) {
      const activeEdit = !isNew && data.active === saved.name;
      const timingMessage = activeEdit
        ? timing === "next_restart"
          ? " Changes apply at the next clock restart."
          : timing === "continue"
            ? " Changes apply immediately, continuing at the next step."
            : " Changes apply immediately from step 1."
        : "";
      say(`Saved ${saved.name}, ${count(saved.steps.length)}.${timingMessage}`);
    }
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
      station: true,
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
    undoHistory = [];
    clock = null; isNew = false; dirty = false;
    listSignature = ""; optionsSignature = ""; lastNotice = null; lastPendingUpdate = false;
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
      editor?.removeEventListener("keydown", undoKey);
      undoButton?.removeEventListener("click", undoSteps);
      window.removeEventListener("hashchange", updatePolling);
      doc.removeEventListener("visibilitychange", updatePolling);
    },
  };
}
