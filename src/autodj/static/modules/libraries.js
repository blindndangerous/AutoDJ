import { captureAuthenticatedRequestEpoch, failureText, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { createCollectionTable } from "./collection-table.js";
import { createEditorDialog } from "./editor-dialog.js";
import { clearFieldError, focusAfterDialog, showFieldError } from "./dom-helpers.js";
import { makeSay } from "./live-region.js";
import { isStationRoute } from "./stations.js";

const JSON_HEADERS = { "Content-Type": "application/json" };
const ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/;

function idFromName(name) {
  return name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 32).replace(/-+$/, "");
}

function addQuery(url, key, value) {
  const hashAt = url.indexOf("#");
  const base = hashAt < 0 ? url : url.slice(0, hashAt);
  const hash = hashAt < 0 ? "" : url.slice(hashAt);
  return `${base}${base.includes("?") ? "&" : "?"}${key}=${encodeURIComponent(value)}${hash}`;
}

function queryHas(url, key) {
  const queryAt = url.indexOf("?");
  if (queryAt < 0) return false;
  const hashAt = url.indexOf("#", queryAt);
  return new URLSearchParams(url.slice(queryAt + 1, hashAt < 0 ? undefined : hashAt)).has(key);
}

function bodyHasStation(options) {
  if (typeof options?.body !== "string") return false;
  try { return Boolean(JSON.parse(options.body)?.station); }
  catch { return false; }
}

export function libraryScopedRequest(request = requestJson, getLibrary = () => "main") {
  return (url, options = {}) => {
    if (typeof url !== "string" || !url.startsWith("/api/library/")) return request(url, options);
    const queryAt = url.search(/[?#]/);
    const path = queryAt < 0 ? url : url.slice(0, queryAt);
    const relative = path.slice("/api".length);
    if (isStationRoute(relative) || queryHas(url, "library") || queryHas(url, "station") || bodyHasStation(options)) {
      return request(url, options);
    }
    return request(addQuery(url, "library", getLibrary() || "main"), options);
  };
}

export function installLibraries({ document: doc = document, request = requestJson,
  stationSelect = doc.getElementById("station-library"), toolSelect = doc.getElementById("library-tools-library"),
  settingsSelect = doc.getElementById("library-settings-library"),
  onLibraryChange = () => {} } = {}) {
  const panel = doc.getElementById("spanel-libraries");
  const host = doc.getElementById("libraries-list");
  const status = doc.getElementById("libraries-status");
  const addButton = doc.getElementById("libraries-add");
  if (!panel || !host || !status || !addButton) {
    return { refresh: async () => [], getSelectedLibrary: () => "main", request: libraryScopedRequest(request) };
  }

  const say = makeSay(status);
  let records = [];
  let generation = 0;
  let editing = null;
  let idTouched = false;
  let lastToolLibrary = toolSelect?.value || "main";
  const collection = createCollectionTable({
    host, label: "music libraries", hideCaption: true,
    columns: [{ key: "name", label: "Name", value: (item) => item.name }],
    keyOf: (item) => item.id, nameOf: (item) => item.name,
    actions: (item) => [{ id: "rename", label: "Rename", run: (button) => openEditor(item, button) }],
    fallback: addButton, status, emptyText: "No libraries yet.",
  });

  const form = doc.createElement("form");
  form.id = "library-editor-form";
  form.hidden = true;
  form.noValidate = true;
  const fieldset = doc.createElement("fieldset");
  const legend = doc.createElement("legend");
  const heading = doc.createElement("h1");
  heading.id = "library-editor-heading";
  legend.append(heading);
  const error = doc.createElement("p");
  error.id = "library-editor-error";
  error.className = "field-error";
  error.hidden = true;
  const nameRow = doc.createElement("div");
  nameRow.className = "settings-row";
  const nameLabel = doc.createElement("label");
  nameLabel.htmlFor = "library-editor-name";
  nameLabel.textContent = "Name";
  const nameInput = doc.createElement("input");
  nameInput.id = "library-editor-name";
  nameInput.type = "text";
  nameInput.maxLength = 80;
  nameInput.required = true;
  nameInput.autocomplete = "off";
  nameRow.append(nameLabel, nameInput);
  const idRow = doc.createElement("div");
  idRow.className = "settings-row";
  const idLabel = doc.createElement("label");
  idLabel.htmlFor = "library-editor-id";
  idLabel.textContent = "Library ID";
  const idInput = doc.createElement("input");
  idInput.id = "library-editor-id";
  idInput.type = "text";
  idInput.maxLength = 32;
  idInput.autocomplete = "off";
  idInput.autocapitalize = "off";
  idInput.spellcheck = false;
  const idHelp = doc.createElement("p");
  idHelp.id = "library-editor-id-help";
  idHelp.className = "setting-desc";
  idHelp.textContent = "Lowercase letters, digits and interior hyphens.";
  idRow.append(idLabel, idInput, idHelp);
  const actions = doc.createElement("div");
  actions.className = "dialog-actions";
  const cancel = doc.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  const save = doc.createElement("button");
  save.type = "submit";
  save.textContent = "Save library";
  actions.append(cancel, save);
  fieldset.append(legend, error, nameRow, idRow, actions);
  form.append(fieldset);
  const editor = createEditorDialog({
    form, heading, cancel, fallback: addButton, pendingText: "Saving…",
    save: saveLibrary,
    describe: false,
  });

  function syncSelect(select, { keepMissing = false } = {}) {
    if (!select) return;
    const old = select.value;
    const options = records.map((item) => {
      const option = doc.createElement("option");
      option.value = item.id;
      option.textContent = item.name;
      return option;
    });
    if (keepMissing && old && !records.some((item) => item.id === old)) {
      const option = doc.createElement("option");
      option.value = old;
      option.textContent = `${old} (unavailable)`;
      options.unshift(option);
    }
    select.replaceChildren(...options);
    if (old && [...select.options].some((item) => item.value === old)) select.value = old;
    else if (select.options.length) select.selectedIndex = 0;
  }

  async function refresh({ quiet = true } = {}) {
    const mine = ++generation;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const value = await request("/api/libraries");
      if (mine !== generation || !isAuthenticatedRequestCurrent(epoch)) return records;
      records = Array.isArray(value.libraries) ? value.libraries : [];
      collection.update(records, { announceFocus: false });
      syncSelect(stationSelect, { keepMissing: true });
      syncSelect(toolSelect);
      syncSelect(settingsSelect);
      const selected = toolSelect?.value || "main";
      if (selected !== lastToolLibrary) {
        lastToolLibrary = selected;
        onLibraryChange(selected);
      }
      if (!quiet) say(`${records.length} ${records.length === 1 ? "library" : "libraries"}.`);
      return records;
    } catch (errorValue) {
      if (mine === generation && isAuthenticatedRequestCurrent(epoch)) say(failureText("load libraries", errorValue), "error");
      return records;
    }
  }

  function openEditor(item, opener = doc.activeElement) {
    if (editor.dialog.open) return;
    editing = item || null;
    idTouched = false;
    heading.textContent = editing ? `Rename ${editing.name}` : "Add library";
    nameLabel.textContent = "Name";
    nameInput.value = editing?.name || "";
    idInput.value = editing?.id || "";
    idRow.hidden = Boolean(editing);
    idInput.required = !editing;
    idInput.readOnly = Boolean(editing);
    save.textContent = editing ? "Save name" : "Create library";
    clearFieldError(nameInput, error);
    clearFieldError(idInput, error);
    editor.open(nameInput, opener);
    if (!editing) nameInput.select();
  }

  nameInput.addEventListener("input", () => {
    clearFieldError(nameInput, error);
    if (!editing && !idTouched) idInput.value = idFromName(nameInput.value);
  });
  idInput.addEventListener("input", () => {
    idTouched = true;
    clearFieldError(idInput, error);
  });

  async function saveLibrary(_event, current) {
    const name = nameInput.value.trim();
    clearFieldError(nameInput, error);
    clearFieldError(idInput, error);
    if (!name) {
      showFieldError(nameInput, "Enter a name.", error, editor.status);
      return;
    }
    if (!editing && !ID_PATTERN.test(idInput.value)) {
      showFieldError(idInput, "Use 1 to 32 lowercase letters, digits or interior hyphens.", error, editor.status);
      return;
    }
    const target = editing;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await request(target ? `/api/libraries/${encodeURIComponent(target.id)}` : "/api/libraries", {
        method: target ? "PATCH" : "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(target ? { name } : { id: idInput.value, name }),
      });
      if (!current() || !isAuthenticatedRequestCurrent(epoch)) return;
      await refresh();
      if (!current() || !isAuthenticatedRequestCurrent(epoch)) return;
      editor.close({ restore: false });
      const targetCell = collection.target(target?.id || idInput.value);
      const focus = targetCell || addButton;
      focusAfterDialog(focus);
      say(target ? `Renamed ${target.name} to ${name}.` : `Created ${name}.`);
    } catch (errorValue) {
      if (!current() || !isAuthenticatedRequestCurrent(epoch)) return;
      const message = errorValue.status === 409 ? errorValue.message : failureText(target ? `rename ${target.name}` : "create library", errorValue);
      showFieldError(target ? nameInput : idInput, message, error, editor.status);
    }
  }

  addButton.addEventListener("click", () => openEditor(null, addButton));
  toolSelect?.addEventListener("change", () => {
    lastToolLibrary = toolSelect.value || "main";
    onLibraryChange(lastToolLibrary);
  });
  panel.addEventListener("toggle", (event) => {
    if (event.target === panel && !panel.hidden) void refresh();
  });
  return {
    refresh,
    getSelectedLibrary: () => toolSelect?.value || "main",
    getSelectedSettingsLibrary: () => settingsSelect?.value || "main",
    request: (url, options) => libraryScopedRequest(request, () => toolSelect?.value || "main")(url, options),
  };
}
