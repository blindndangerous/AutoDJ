// Library tools, Intro rules (#127): voice liners that play as the next song
// starts when that song matches a rule's conditions (genre, decade, artist,
// a Beets field, a folder or a category), each station's own.
//
// The list is plain text with Edit and Delete for each rule; Add and Edit
// share one form, which reuses the clockwheel and category condition rows
// (condition-editor.js).  A refused field is marked aria-invalid, its error
// is shown under it and linked to it with aria-describedby, and it takes
// focus, which reads the error, so nothing else speaks.  Any other result is
// said once in #intro-rules-status.
//
// Focus: Add and Edit move it to Label; Save to the saved rule's Edit
// button, Cancel back to the button that opened the form, and Delete to the
// next rule's Delete button (or Add intro rule).
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { describeConditions } from "./clockwheel-step.js";
import { createConditionRow, readConditionRows, renumberConditionRows } from "./condition-editor.js";
import { confirmAction } from "./confirm-dialog.js";
import { nextRowControl, rowButton } from "./dom-helpers.js";
import { parsePointTime } from "./library-folders.js";
import { announceStatus } from "./live-region.js";

const MAX_CONDITIONS = 32;
const MAX_COOLDOWN = 7 * 24 * 60;
const FIELDS = ["label", "conditions", "clips", "folder", "chance", "cooldown", "not-before", "not-after"];
// The server's field names, mapped to the page's.
const SERVER_FIELDS = {
  label: "label", conditions: "conditions", clip_ids: "clips", liner_folder: "folder",
  chance: "chance", cooldown_minutes: "cooldown", not_before: "not-before", not_after: "not-after",
};
const CONDITION_FIELDS = ["folder", "year", "bpm", "duration", "artist", "title", "album", "genre", "beets", "category"];
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

// One rule in words, for its list item.
export function describeRule(rule, index, data = {}) {
  const liners = new Map((data.liners || []).map((item) => [item.id, item.label]));
  const conditions = describeConditions(rule.conditions || [], rule.match, data.sources || [], data.categories || []);
  const clips = (rule.clip_ids || []).map((id) => liners.get(id) || "a liner that is no longer registered");
  if (rule.liner_folder) clips.push(`any liner in ${rule.liner_folder}`);
  const sentences = [
    `${ruleName(rule, index)}: when the next song matches ${conditions}.`,
    `Plays ${clips.length > 1 ? "one of " : ""}${clips.join(", ")}.`,
    `${rule.chance ?? 100}% chance, ${waitWords(rule.cooldown_minutes)}, ${PRIORITY_WORDS[rule.priority] || PRIORITY_WORDS.after_artist}.`,
  ];
  if (rule.not_before && rule.not_after) sentences.push(`Plays from ${dayWords(rule.not_before)} until ${dayWords(rule.not_after)}.`);
  else if (rule.not_before) sentences.push(`Plays from ${dayWords(rule.not_before)}.`);
  else if (rule.not_after) sentences.push(`Plays until ${dayWords(rule.not_after)}.`);
  return sentences.join(" ");
}

// The error for the form's values besides its conditions, as [field,
// message], or null.  The server checks the same rules again and names the
// field it refuses.
export function ruleFormError({ clipIds = [], folder = "", chance = "", cooldown = "", notBefore = "", notAfter = "" }) {
  if (!clipIds.length && !folder) return ["clips", "Check at least one voice liner or choose a liner folder."];
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

export function installIntroRulesPanel({ document: doc = document, request = requestJson } = {}) {
  const el = (id) => doc.getElementById(id);
  const panel = el("panel-library");
  const section = el("intro-rules");
  const form = el("intro-rules-form");
  const list = el("intro-rules-list");
  const addButton = el("intro-rules-add");
  if (!panel || !section || !form || !list || !addButton) return NO_PANEL;
  const status = el("intro-rules-status");
  const problem = el("intro-rules-problem");
  const empty = el("intro-rules-empty");
  const legend = el("intro-rules-form-legend");
  const conditionHost = el("intro-rules-conditions");
  const conditionAdd = el("intro-rules-condition-add");
  const linerHost = el("intro-rules-liners");
  const linersEmpty = el("intro-rules-liners-empty");
  const inputs = {
    label: el("intro-rules-label"), folder: el("intro-rules-folder"), chance: el("intro-rules-chance"),
    cooldown: el("intro-rules-cooldown"), "not-before": el("intro-rules-not-before"), "not-after": el("intro-rules-not-after"),
  };
  const errors = Object.fromEntries(FIELDS.map((field) => [field, el(`intro-rules-${field}-error`)]));
  const blank = () => ({ rules: [], liners: [], liner_folders: [], sources: [], genres: [], beets_fields: [], categories: [] });
  let data = blank();
  let editing = null; // the rule ID being edited, or null when adding
  let opener = null;
  let generation = 0;
  let suspended = false;
  let busy = false;
  let conditionId = 0;
  let listSignature = "";
  // The control each shown error is linked to.
  const marked = new Map();

  const current = (epoch, mine) => !suspended && mine === generation && isAuthenticatedRequestCurrent(epoch);
  const say = (message, tone = "info") => announceStatus(status, message, { force: true, tone });
  const indexOf = (id) => data.rules.findIndex((rule) => rule.id === id);
  const nameOf = (id) => ruleName(data.rules[indexOf(id)] || {}, indexOf(id));
  const linerBoxes = () => [...linerHost.querySelectorAll('input[type="checkbox"]')];

  // ---- Errors ----------------------------------------------------------
  function link(control, error, linked) {
    const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== error.id);
    if (linked) ids.push(error.id);
    if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
    else control.removeAttribute("aria-describedby");
  }
  function clearError(field) {
    const control = marked.get(field);
    if (control) {
      control.removeAttribute("aria-invalid");
      link(control, errors[field], false);
      marked.delete(field);
    }
    errors[field].textContent = "";
    errors[field].hidden = true;
  }
  function clearErrors() {
    for (const field of FIELDS) clearError(field);
    for (const control of conditionHost.querySelectorAll("[aria-invalid]")) control.removeAttribute("aria-invalid");
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
  function showError(field, message) {
    const control = controlFor(field);
    control.setAttribute("aria-invalid", "true");
    errors[field].textContent = message;
    errors[field].hidden = false;
    link(control, errors[field], true);
    marked.set(field, control);
    control.focus();
  }

  // ---- The list --------------------------------------------------------
  function renderList() {
    const signature = JSON.stringify([data.rules, data.liners, data.sources, data.categories]);
    if (signature === listSignature) return;
    listSignature = signature;
    const active = doc.activeElement;
    const focused = list.contains(active) ? { id: active.dataset.rule, action: active.dataset.action } : null;
    list.replaceChildren(...data.rules.map((rule, index) => {
      const row = doc.createElement("li");
      const text = doc.createElement("span");
      text.className = "intro-text";
      text.textContent = describeRule(rule, index, data);
      const name = ruleName(rule, index);
      const edit = rowButton(doc, "Edit", `Edit intro rule ${name}`, (button) => openForm(rule, button));
      const drop = rowButton(doc, "Delete", `Delete intro rule ${name}`, (button) => void remove(rule.id, button));
      for (const [button, action] of [[edit, "edit"], [drop, "delete"]]) {
        button.dataset.rule = rule.id;
        button.dataset.action = action;
      }
      row.append(text, edit, drop);
      return row;
    }));
    empty.hidden = data.rules.length > 0;
    if (focused) rowControl(focused.id, focused.action)?.focus();
  }

  function rowControl(id, action = "edit") {
    return [...list.querySelectorAll("button")].find((button) => button.dataset.rule === id && button.dataset.action === action) || null;
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
      fields: CONDITION_FIELDS,
      sourceChoices: data.sources.map((item) => [item.id, item.path]),
      genreChoices: data.genres.map((item) => [item, item]),
      beetsChoices: data.beets_fields.map((item) => [item, item]),
      categoryChoices: data.categories.map((item) => [item.id, item.name]),
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
    editing = rule ? rule.id : null;
    opener = from;
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
    legend.textContent = rule ? `Edit intro rule ${ruleName(rule, indexOf(rule.id))}` : "Add intro rule";
    form.hidden = false;
    inputs.label.focus();
  }

  function closeForm() {
    form.hidden = true;
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

  async function save(event) {
    event.preventDefault();
    if (busy) return;
    clearErrors();
    let conditions;
    try { conditions = readForm(); }
    catch (error) { showError("conditions", error.message); return; }
    const values = {
      clipIds: linerBoxes().filter((box) => box.checked).map((box) => box.value),
      folder: inputs.folder.value,
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
    const refused = ruleFormError(values);
    if (refused) { showError(refused[0], refused[1]); return; }
    const body = {
      label: inputs.label.value.trim(),
      match: el("intro-rules-match").value,
      conditions,
      clip_ids: values.clipIds,
      priority: el("intro-rules-priority").value,
      chance: Number(values.chance.trim()),
      cooldown_minutes: Number(values.cooldown.trim()),
      ...(values.folder ? { liner_folder: values.folder } : {}),
      ...(values.notBefore ? { not_before: values.notBefore } : {}),
      ...(values.notAfter ? { not_after: values.notAfter } : {}),
    };
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    busy = true;
    try {
      const saved = editing
        ? await request(`/api/intros/rules/${encodeURIComponent(editing)}`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        })
        : await request("/api/intros/rules", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
      if (!current(epoch, mine)) return;
      await refresh().catch(() => {});
      if (!current(epoch, mine)) return;
      closeForm();
      rowControl(saved.id)?.focus();
      say(`Saved intro rule ${nameOf(saved.id)}.`);
    } catch (error) {
      if (!current(epoch, mine)) return;
      const field = SERVER_FIELDS[error.field];
      if (field) showError(field, error.message);
      else say(`Could not save the intro rule: ${error.message}`, "error");
    } finally {
      busy = false;
    }
  }

  async function remove(id, button) {
    const index = [...list.querySelectorAll("button[data-action='delete']")].indexOf(button);
    const name = nameOf(id);
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    let result = null;
    let tone = "info";
    const confirmed = await confirmAction(doc, {
      title: `Delete intro rule ${name}?`,
      message: "Its voice liners stay in the library.",
      confirmLabel: "Delete intro rule",
      station: true,
      onConfirm: async () => {
        try {
          await request(`/api/intros/rules/${encodeURIComponent(id)}`, { method: "DELETE" });
        } catch (error) {
          if (!current(epoch, mine)) return null;
          result = `Could not delete intro rule ${name}: ${error.message}`;
          tone = "error";
          return button;
        }
        if (!current(epoch, mine)) return null;
        if (editing === id) closeForm();
        result = `Deleted intro rule ${name}.`;
        await refresh().catch(() => {});
        return nextRowControl(list, "button[data-action='delete']", Math.max(0, index), addButton);
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
    refresh().catch((error) => say(`Could not load the intro rules: ${error.message}`, "error"));
  }

  form.addEventListener("submit", (event) => void save(event));
  addButton.addEventListener("click", () => openForm(null, addButton));
  el("intro-rules-cancel")?.addEventListener("click", () => {
    const back = editing ? rowControl(editing) : null;
    closeForm();
    (back || (opener?.isConnected ? opener : addButton)).focus();
    say("Cancelled. Nothing was saved.");
  });
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
    list.replaceChildren();
    empty.hidden = false;
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
      window.removeEventListener("hashchange", load);
      doc.removeEventListener("visibilitychange", load);
    },
  };
}
