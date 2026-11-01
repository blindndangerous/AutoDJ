import { createEditorDialog } from "./editor-dialog.js";
// Library tools, Intro rules (#127): voice liners that play as the next song
// starts when that song matches a rule's conditions (genre, decade, artist,
// a Beets field, a folder or a category), each station's own.
//
// The collection has primary Edit and Delete in Options; Add and Edit
// share one form, which reuses the clockwheel and category condition rows
// (condition-editor.js).  A refused field is marked aria-invalid, its error
// is shown under it and linked to it with aria-describedby, and it takes
// focus, which reads the error, so nothing else speaks.  Any other result is
// said once in #intro-rules-status.
//
// Focus: Add and Edit move it to Label; Save to the saved rule's Edit
// cell, Cancel back to the opener cell, and Delete to the next rule's
// Options cell (or Add intro rule).
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson, failureText, withDisabled } from "./api-client.js";
import { describeConditions } from "./clockwheel-step.js";
import { createConditionRow, readConditionRows, renumberConditionRows, updateConditionCategories } from "./condition-editor.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { focusAfterDialog, clearFieldError, showFieldError, applyServerFieldError } from "./dom-helpers.js";
import { createCollectionTable } from "./collection-table.js";
import { parsePointTime } from "./library-folders.js";
import { makeSay } from "./live-region.js";

const MAX_CONDITIONS = 32;
const MAX_COOLDOWN = 7 * 24 * 60;
const FIELDS = ["label", "conditions", "clips", "folder", "pattern", "pattern-folder", "test-song", "chance", "cooldown", "not-before", "not-after"];
// The server's field names, mapped to the page's.
const SERVER_FIELDS = {
  label: "label", conditions: "conditions", clip_ids: "clips", liner_folder: "folder", filename_pattern: "pattern",
  chance: "chance", cooldown_minutes: "cooldown", not_before: "not-before", not_after: "not-after",
};
const PRIORITY_WORDS = {
  after_artist: "after the song’s and the artist’s own intros",
  before_artist: "after the song’s own intros, before the artist’s",
  before_song: "before the song’s and the artist’s own intros",
};
const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
  "August", "September", "October", "November", "December"];

// "2026-12-01" in words: "1 December 2026".
export function dayWords(iso) {
  const [year, month, day] = String(iso).split("-").map(Number);
  return MONTHS[month - 1] ? `${day} ${MONTHS[month - 1]} ${year}` : String(iso);
}

const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;

// A rule's wait in words, as matched intros say theirs.
export function waitWords(minutes) {
  const total = Math.max(0, Math.round(Number(minutes) || 0));
  if (!total) return "with no wait between plays";
  const days = Math.floor(total / 1440);
  const hours = Math.floor((total % 1440) / 60);
  const mins = total % 60;
  const parts = [];
  if (days) parts.push(plural(days, "day"));
  if (hours) parts.push(plural(hours, "hour"));
  if (mins) parts.push(plural(mins, "minute"));
  return `at most once every ${parts.join(" ")}`;
}

// The name a rule's buttons and messages use.
function ruleName(rule, index) {
  return rule.label || `Rule ${index + 1}`;
}

// The error for the form's values besides its conditions, as [field,
// message], or null.  The server checks the same rules again and names the
// field it refuses.
export function ruleFormError({ clipIds = [], folder = "", patternMode = false, chance = "", cooldown = "", notBefore = "", notAfter = "" }) {
  if (patternMode && !folder) return ["pattern-folder", "Choose a liner folder for filename matching."];
  if (!patternMode && !clipIds.length && !folder) return ["clips", "Check at least one voice liner or choose a liner folder."];
  const chanceText = String(chance).trim();
  if (!/^\d{1,3}$/.test(chanceText) || Number(chanceText) < 1 || Number(chanceText) > 100) {
    return ["chance", "Enter the chance as a whole number from 1 to 100."];
  }
  const waitText = String(cooldown).trim();
  if (!/^\d{1,5}$/.test(waitText) || Number(waitText) > MAX_COOLDOWN) {
    return ["cooldown", `Enter the wait as a whole number of minutes from 0 to ${MAX_COOLDOWN}.`];
  }
  if (notBefore && notAfter && notAfter < notBefore) {
    return ["not-after", "Do not play after must be on or after the Not before date."];
  }
  return null;
}

const NO_PANEL = { refresh() {}, clear() {}, clearProtectedData() {}, resume() {}, destroy() {} };

export function installIntroRulesPanel({ document: doc = document, request = requestJson, createCategory } = {}) {
  const el = (id) => doc.getElementById(id);
  const panel = el("panel-library");
  const section = el("intro-rules");
  const form = el("intro-rules-form");
  const list = el("intro-rules-list");
  const addButton = el("intro-rules-add");
  if (!panel || !section || !form || !list || !addButton) return NO_PANEL;
  const status = el("intro-rules-status");
  const problem = el("intro-rules-problem");
  const legend = el("intro-rules-form-legend");
  const conditionHost = el("intro-rules-conditions");
  const conditionAdd = el("intro-rules-condition-add");
  const linerHost = el("intro-rules-liners");
  const linersEmpty = el("intro-rules-liners-empty");
  const selectionMode = el("intro-rules-selection-mode");
  const specificLiners = el("intro-rules-specific-liners");
  const patternFields = el("intro-rules-pattern-fields");
  const patternInput = el("intro-rules-pattern");
  const patternFolder = el("intro-rules-pattern-folder");
  const testQuery = el("intro-rules-test-query");
  const testResults = el("intro-rules-test-results");
  const testSelected = el("intro-rules-test-selected");
  const testOutput = el("intro-rules-test-output");
  const inputs = {
    label: el("intro-rules-label"), folder: el("intro-rules-folder"), chance: el("intro-rules-chance"),
    cooldown: el("intro-rules-cooldown"), "not-before": el("intro-rules-not-before"), "not-after": el("intro-rules-not-after"),
  };
  const errors = Object.fromEntries(FIELDS.map((field) => [field, el(`intro-rules-${field}-error`)]));
  const blank = () => ({ rules: [], liners: [], liner_folders: [], sources: [], genres: [], beets_fields: [], categories: [] });
  let data = blank();
  let editing = null; // the rule ID being edited, or null when adding
  let generation = 0;
  let testGeneration = 0;
  let testController = null;
  let suspended = false;
  let conditionId = 0;
  let listSignature = "";
  let listFocusAnnouncementSuppressed = false;
  // The control each shown error is linked to.
  const marked = new Map();

  const editor = createEditorDialog({ form, heading: legend, cancel: el("intro-rules-cancel"), fallback: addButton,
    save, onCancel: () => { closeForm(); say("Cancelled. Nothing was saved."); } });

  const current = (epoch, mine) => !suspended && mine === generation && isAuthenticatedRequestCurrent(epoch);
  const say = makeSay(() => editor.region(status));
  const indexOf = (id) => data.rules.findIndex((rule) => rule.id === id);
  const nameOf = (id) => ruleName(data.rules[indexOf(id)] || {}, indexOf(id));
  const linerBoxes = () => [...linerHost.querySelectorAll('input[type="checkbox"]')];

  // ---- Errors ----------------------------------------------------------
  function clearError(field) {
    clearFieldError(marked.get(field), errors[field]);
    marked.delete(field);
  }
  function clearErrors() {
    for (const field of FIELDS) clearError(field);
    for (const control of conditionHost.querySelectorAll("[aria-invalid]")) clearFieldError(control, errors.conditions);
  }
  // The control a field's error belongs to: for the liners the first
  // checkbox (or the folder when there is none), for the conditions the
  // control the condition rows marked, else the first condition's type.
  function controlFor(field) {
    if (field === "clips") return linerBoxes()[0] || inputs.folder;
    if (field === "conditions") {
      return conditionHost.querySelector("[aria-invalid='true']") || conditionHost.querySelector("select") || conditionAdd;
    }
    return inputs[field];
  }
  function showError(field, message, target) {
    const control = target || ({ pattern: patternInput, "pattern-folder": patternFolder, "test-song": testQuery })[field] || controlFor(field);
    if (!control) { say(message, "error"); return; }
    marked.set(field, control);
    showFieldError(control, message, errors[field], editor.region(status));
  }

  const collection = createCollectionTable({
    host: list, label: "intro rules", keyOf: (rule) => rule.id, nameOf: ruleName,
    columns: [
      { key: "name", label: "Name", value: ruleName },
      { key: "conditions", label: "Conditions", value: (rule) => describeConditions(rule.conditions || [], rule.match, data.sources, data.categories) },
      { key: "clips", label: "Clips", value: (rule) => {
        const liners = new Map(data.liners.map((item) => [item.id, item.label]));
        const clips = (rule.clip_ids || []).map((id) => liners.get(id) || "a liner that is no longer registered");
        if (rule.filename_pattern) clips.push(`Filename pattern "${rule.filename_pattern}" in ${rule.liner_folder || "the selected folder"}`);
        else if (rule.liner_folder) clips.push(`Any liner in ${rule.liner_folder}`);
        return `${clips.length > 1 ? "One of " : ""}${clips.join(", ")}`;
      } },
      { key: "playback", label: "Playback", value: (rule) => `${rule.chance ?? 100}% chance, ${waitWords(rule.cooldown_minutes)}, ${PRIORITY_WORDS[rule.priority] || PRIORITY_WORDS.after_artist}.` },
      { key: "dates", label: "Dates", value: (rule) => rule.not_before && rule.not_after
        ? `From ${dayWords(rule.not_before)} until ${dayWords(rule.not_after)}`
        : rule.not_before ? `From ${dayWords(rule.not_before)}` : rule.not_after ? `Until ${dayWords(rule.not_after)}` : "Any date" },
    ],
    primary: (rule) => ({ id: "edit", label: "Edit", run: (button) => openForm(rule, button) }),
    actions: (rule) => [{ id: "delete", label: "Delete", run: (button) => void remove(rule.id, button) }],
    fallback: addButton, status, emptyText: "No intro rules yet.",
  });

  function renderList() {
    const signature = JSON.stringify([data.rules, data.liners, data.sources, data.categories]);
    if (signature === listSignature) return;
    listSignature = signature;
    collection.update(data.rules, { announceFocus: !listFocusAnnouncementSuppressed });
  }

  function rowControl(id, action = "edit") {
    return collection.target(id, action === "delete" ? "options" : action);
  }

  function apply(value) {
    data = { ...blank(), ...value };
    if (problem) {
      problem.textContent = data.problem || "";
      problem.hidden = !data.problem;
    }
    renderList();
  }

  // ---- The form --------------------------------------------------------
  function conditionRow(value = {}) {
    return createConditionRow({
      document: doc,
      nextId: () => `intro-rules-condition-${++conditionId}`,
      index: conditionHost.children.length + 1,
      value,
      allowCategory: true,
      sourceChoices: data.sources.map((item) => [item.id, item.path]),
      genreChoices: data.genres.map((item) => [item, item]),
      beetsChoices: data.beets_fields.map((item) => [item, item]),
      categoryChoices: data.categories.map((item) => [item.id, item.name]),
      onAddCategory: createCategory && (({ row, category, button }) => {
        const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
        void createCategory({
          from: button,
          isCurrent: () => current(epoch, mine) && editor.dialog.open && conditionHost.contains(row),
          onCreated: (saved, categories) => {
            data.categories = categories;
            updateConditionCategories(conditionHost, categories.map((item) => [item.id, item.name]), category, saved.id);
            category.focus();
          },
        });
      }),
      onRemove: (row) => {
        clearError("conditions");
        const index = [...conditionHost.children].indexOf(row);
        row.remove();
        renumberConditionRows(conditionHost);
        const left = [...conditionHost.children];
        // The condition that took its place, or the one before it, or Add condition.
        (left[Math.min(index, left.length - 1)]?.querySelector("select") || conditionAdd).focus();
        say(`Condition ${index + 1} removed, ${plural(left.length, "condition")} left.`);
      },
    });
  }

  function renderLiners(chosen = []) {
    const wanted = new Set(chosen);
    // A saved rule's liner that is no longer offered stays, so a save keeps it
    // unless it is unchecked.
    const items = [...data.liners];
    for (const id of chosen) {
      if (!items.some((item) => item.id === id)) items.push({ id, label: "A liner that is no longer registered" });
    }
    linerHost.replaceChildren(...items.map((item, index) => {
      const wrap = doc.createElement("div");
      wrap.className = "settings-check";
      const box = doc.createElement("input");
      box.type = "checkbox";
      box.id = `intro-rules-liner-${index}`;
      box.value = item.id;
      box.checked = wanted.has(item.id);
      const label = doc.createElement("label");
      label.htmlFor = box.id;
      label.textContent = item.label;
      wrap.append(box, label);
      return wrap;
    }));
    linerHost.hidden = items.length === 0;
    linersEmpty.hidden = items.length > 0;
  }

  function renderFolders(chosen = "") {
    const folders = [...data.liner_folders];
    if (chosen && !folders.includes(chosen)) folders.push(chosen);
    inputs.folder.replaceChildren(...[["", "None"], ...folders.map((path) => [path, path])].map(([value, text]) => {
      const option = doc.createElement("option");
      option.value = value;
      option.textContent = text;
      return option;
    }));
    inputs.folder.value = chosen;
  }

  function openForm(rule, from) {
    invalidateTest(true);
    editing = rule ? rule.id : null;
    clearErrors();
    inputs.label.value = rule?.label || "";
    el("intro-rules-match").value = rule?.match || "all";
    el("intro-rules-priority").value = rule?.priority || "after_artist";
    inputs.chance.value = String(rule?.chance ?? 100);
    inputs.cooldown.value = String(rule?.cooldown_minutes ?? 60);
    inputs["not-before"].value = rule?.not_before || "";
    inputs["not-after"].value = rule?.not_after || "";
    conditionHost.replaceChildren();
    for (const value of rule?.conditions?.length ? rule.conditions : [{}]) conditionHost.append(conditionRow(value).row);
    renderLiners(rule?.clip_ids || []);
    renderFolders(rule?.liner_folder || "");
    patternInput.value = rule?.filename_pattern || "";
    renderPatternFolders(rule?.liner_folder || "");
    selectionMode.value = rule?.filename_pattern ? "pattern" : "specific";
    updateSelectionMode();
    testQuery.value = "";
    testResults.replaceChildren();
    testSelected.textContent = "";
    legend.textContent = rule ? `Edit intro rule ${ruleName(rule, indexOf(rule.id))}` : "Add intro rule";
    editor.open(inputs.label, from);
  }

  function renderPatternFolders(chosen = "") {
    const folders = [...data.liner_folders];
    if (chosen && !folders.includes(chosen)) folders.push(chosen);
    patternFolder.replaceChildren(...[["", "Choose a folder"], ...folders.map((path) => [path, path])].map(([value, text]) => {
      const option = doc.createElement("option"); option.value = value; option.textContent = text; return option;
    }));
    patternFolder.value = chosen;
  }

  function updateSelectionMode() {
    const pattern = selectionMode.value === "pattern";
    specificLiners.hidden = pattern;
    patternFields.hidden = !pattern;
  }

  function invalidateTest(clearSong = false) {
    testController?.abort();
    testController = null;
    testGeneration += 1;
    testOutput.hidden = true;
    testOutput.replaceChildren();
    if (clearSong) {
      delete testSelected.dataset.trackPath;
      testSelected.textContent = "";
      testResults.replaceChildren();
    }
  }

  function ruleBody(conditions) {
    const patternMode = selectionMode.value === "pattern";
    const clipIds = linerBoxes().filter((box) => box.checked).map((box) => box.value);
    const folder = patternMode ? patternFolder.value : inputs.folder.value;
    return {
      label: inputs.label.value.trim(), match: el("intro-rules-match").value, conditions,
      clip_ids: patternMode ? [] : clipIds, filename_pattern: patternMode ? patternInput.value : null,
      priority: el("intro-rules-priority").value, chance: Number(inputs.chance.value.trim()),
      cooldown_minutes: Number(inputs.cooldown.value.trim()),
      ...(folder ? { liner_folder: folder } : {}),
      ...(inputs["not-before"].value ? { not_before: inputs["not-before"].value } : {}),
      ...(inputs["not-after"].value ? { not_after: inputs["not-after"].value } : {}),
    };
  }

  async function searchTestSong() {
    invalidateTest(true);
    const query = testQuery.value.trim();
    if (query.length < 2) { showError("test-song", "Type at least two letters to search."); return; }
    clearError("test-song");
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation, testMine = testGeneration;
    try {
      const answer = await request(`/api/search?q=${encodeURIComponent(query)}&limit=20`);
      if (!current(epoch, mine) || testMine !== testGeneration) return;
      testResults.replaceChildren(...(answer.results || []).map((song) => {
        const item = doc.createElement("li"), pick = doc.createElement("button");
        pick.type = "button";
        pick.textContent = song.artist ? `${song.artist}, ${song.title || song.display_name}` : (song.title || song.display_name);
        pick.addEventListener("click", () => {
          invalidateTest();
          testSelected.dataset.trackPath = song.path;
          testSelected.textContent = `Selected: ${pick.textContent}`;
          clearError("test-song");
          el("intro-rules-test").focus();
        });
        item.append(pick); return item;
      }));
      say(answer.results?.length ? `${answer.results.length} songs found. Choose a song to test.` : `No songs match ${query}.`);
    } catch (error) {
      if (current(epoch, mine) && testMine === testGeneration) say(failureText(`search songs`, error), "error");
    }
  }

  async function testPattern() {
    if (!testSelected.dataset.trackPath) { showError("test-song", "Search for and choose a song first."); return; }
    let conditions;
    try { conditions = readForm(); } catch (error) { showError("conditions", error.message, error.control); return; }
    clearError("test-song");
    invalidateTest();
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation, testMine = testGeneration;
    const control = el("intro-rules-test");
    testController = new AbortController();
    const signal = testController.signal;
    await withDisabled(control, async () => {
      try {
        const answer = await request(`/api/intros/rules/test?path=${encodeURIComponent(testSelected.dataset.trackPath)}`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(ruleBody(conditions)),
        });
        if (!current(epoch, mine) || testMine !== testGeneration) return;
        const matches = answer.matches || [];
        const eligible = matches.filter((item) => item.eligible).length;
        const missing = answer.missing_fields || [];
        const expansion = doc.createElement("p");
        expansion.textContent = answer.expanded_pattern == null ? "Pattern did not expand." : `Expanded pattern: ${answer.expanded_pattern}`;
        const missingText = doc.createElement("p");
        missingText.textContent = missing.length ? `Missing song fields: ${missing.join(", ")}.` : "No song fields are missing.";
        const linerList = doc.createElement("ul");
        linerList.setAttribute("aria-label", "Filename pattern test results");
        if (!matches.length) {
          const none = doc.createElement("li"); none.textContent = "No matching liners."; linerList.append(none);
        } else {
          for (const item of matches) {
            const row = doc.createElement("li");
            row.textContent = `${item.label}: ${item.eligible ? "eligible" : item.reason || "not eligible"}`;
            linerList.append(row);
          }
        }
        const note = doc.createElement("p"); note.textContent = answer.note || "";
        testOutput.replaceChildren(expansion, missingText, linerList, note);
        testOutput.hidden = false;
        say(`Pattern test complete: ${eligible} eligible, ${matches.length - eligible} excluded; ${missing.length} missing song fields.`);
      } catch (error) {
        if (!current(epoch, mine) || testMine !== testGeneration) return;
        const field = error.field === "filename_pattern" ? "pattern"
          : error.field === "liner_folder" ? (selectionMode.value === "pattern" ? "pattern-folder" : "folder")
            : error.field === "path" ? "test-song" : SERVER_FIELDS[error.field];
        showServerError(error, field, "test the pattern");
      }
    }, { signal });
  }

  // A refusal goes on the field the server names.  One the form cannot
  // place there is said in the status, so it is never dropped.
  function showServerError(error, field, action) {
    const control = field
      && (({ pattern: patternInput, "pattern-folder": patternFolder, "test-song": testQuery })[field] || controlFor(field));
    if (control && applyServerFieldError(error, { [error.field]: control }, errors[field], editor.region(status))) {
      marked.set(field, control);
    } else say(failureText(action, error), "error");
  }

  function closeForm() {
    invalidateTest(true);
    editor.close();
    editing = null;
    clearErrors();
  }

  function readForm() {
    const conditions = readConditionRows([...conditionHost.children], {
      parsePointTime,
      folderRequired: "Choose a registered music folder.",
      numberError: (field) => `Enter a valid value for ${field}.`,
      beetsAvailable: (name) => Boolean(name) && !data.beets_error && data.beets_fields.includes(name),
      beetsError: () => data.beets_error || "Choose an available Beets field.",
      errorField: "conditions",
    });
    if (!conditions.length) {
      const error = new Error("Add at least one condition.");
      error.field = "conditions";
      throw error;
    }
    return conditions;
  }

  async function save(event, active) {
    event.preventDefault();
    clearErrors();
    let conditions;
    try { conditions = readForm(); }
    catch (error) { showError("conditions", error.message, error.control); return; }
    const patternMode = selectionMode.value === "pattern";
    const values = {
      clipIds: linerBoxes().filter((box) => box.checked).map((box) => box.value),
      folder: patternMode ? patternFolder.value : inputs.folder.value,
      chance: inputs.chance.value,
      cooldown: inputs.cooldown.value,
      notBefore: inputs["not-before"].value,
      notAfter: inputs["not-after"].value,
    };
    // A half-typed date reads as empty; say so rather than save no date.
    for (const [field, label] of [["not-before", "Not before"], ["not-after", "Do not play after"]]) {
      if (inputs[field].validity?.badInput) {
        showError(field, `${label} is not a whole date. Enter a day, month and year, or clear it.`);
        return;
      }
    }
    if (patternMode && !patternInput.value.trim()) { showError("pattern", "Enter a filename pattern."); return; }
    const refused = ruleFormError({ ...values, patternMode });
    if (refused) { showError(refused[0], refused[1]); return; }
    const body = ruleBody(conditions);
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const saved = editing
        ? await request(`/api/intros/rules/${encodeURIComponent(editing)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        })
        : await request("/api/intros/rules", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
      if (!current(epoch, mine) || !active()) return;
      await refresh().catch(() => {});
      if (!current(epoch, mine) || !active()) return;
      closeForm();
      focusAfterDialog(rowControl(saved.id) || addButton);
      say(`Saved intro rule ${nameOf(saved.id)}.`);
    } catch (error) {
      if (!current(epoch, mine) || !active()) return;
      const field = error.field === "liner_folder" && patternMode ? "pattern-folder" : SERVER_FIELDS[error.field];
      showServerError(error, field, "save the intro rule");
    }
  }

  async function remove(id, button) {
    const index = indexOf(id);
    const name = nameOf(id);
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    let result = null;
    let tone = "info";
    const confirmed = await deleteCollectionItem(doc, {
      title: `Delete intro rule ${name}?`,
      message: "Its voice liners stay in the library.",
      confirmLabel: "Delete intro rule",
      onConfirm: async () => {
        try {
          await request(`/api/intros/rules/${encodeURIComponent(id)}`, { method: "DELETE" });
        } catch (error) {
          if (!current(epoch, mine)) return null;
          result = failureText(`delete intro rule ${name}`, error);
          tone = "error";
          return button.closest("td");
        }
        if (!current(epoch, mine)) return null;
        if (editing === id) closeForm();
        result = `Deleted intro rule ${name}.`;
        listFocusAnnouncementSuppressed = true;
        try { await refresh().catch(() => {}); } finally { listFocusAnnouncementSuppressed = false; }
        return rowControl(data.rules[Math.min(index, data.rules.length - 1)]?.id, "delete") || addButton;
      },
    });
    if (confirmed && result && current(epoch, mine)) say(result, tone);
  }

  // ---- Loading ---------------------------------------------------------
  async function refresh() {
    if (suspended) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    const value = await request("/api/intros/rules");
    if (current(epoch, mine)) apply(value);
  }

  function load() {
    if (suspended || panel.hidden || doc.hidden) return;
    refresh().catch((error) => say(failureText(`load the intro rules`, error), "error"));
  }

  selectionMode.addEventListener("change", () => { invalidateTest(); updateSelectionMode(); });
  el("intro-rules-pattern-insert").addEventListener("click", () => {
    const token = `{${el("intro-rules-pattern-field").value}}`;
    const start = patternInput.selectionStart ?? patternInput.value.length;
    const end = patternInput.selectionEnd ?? start;
    patternInput.setRangeText(token, start, end, "end");
    invalidateTest();
    patternInput.focus();
  });
  el("intro-rules-test-search").addEventListener("click", () => void searchTestSong());
  el("intro-rules-test").addEventListener("click", () => void testPattern());
  testQuery.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); void searchTestSong(); }
  });
  addButton.addEventListener("click", () => openForm(null, addButton));
  conditionAdd.addEventListener("click", () => {
    clearError("conditions");
    if (conditionHost.children.length >= MAX_CONDITIONS) {
      showError("conditions", `An intro rule can have at most ${MAX_CONDITIONS} conditions.`);
      return;
    }
    const item = conditionRow();
    conditionHost.append(item.row);
    item.field.focus();
  });
  // Changing a refused field takes its error away.
  const onInput = (event) => {
    if ([patternInput, patternFolder, selectionMode].includes(event.target)) invalidateTest();
    if (event.target === testQuery) invalidateTest(true);
    for (const [field, control] of marked) {
      const inField = field === "clips" ? linerHost.contains(event.target) || event.target === inputs.folder
        : field === "conditions" ? conditionHost.contains(event.target) : control === event.target;
      if (inField) clearError(field);
    }
  };
  form.addEventListener("input", onInput);
  form.addEventListener("change", onInput);
  window.addEventListener("hashchange", load);
  doc.addEventListener("visibilitychange", load);
  load();

  function clear() {
    generation += 1;
    data = blank();
    listSignature = "";
    listFocusAnnouncementSuppressed = true;
    try { renderList(); } finally { listFocusAnnouncementSuppressed = false; }
    closeForm();
    if (problem) { problem.textContent = ""; problem.hidden = true; }
    if (status) status.textContent = "";
  }

  return {
    // The station changed (#70), or a category was saved.
    refresh: () => { load(); },
    // Another station is chosen: its rules are loaded next.
    clear,
    // Signed out: nothing is loaded until resume.
    clearProtectedData() { suspended = true; clear(); },
    resume() { suspended = false; load(); },
    destroy() {
      suspended = true;
      clear();
      editor.destroy();
      window.removeEventListener("hashchange", load);
      doc.removeEventListener("visibilitychange", load);
    },
  };
}
