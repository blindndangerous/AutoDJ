// Categories: the rotation categories clockwheel steps pick from.
//
// The server keeps them in categories.json and works out their songs.  This
// module lists them with their counts (plain text), and adds, edits and
// deletes them through one form.  Each result of a button (Check, Save,
// Delete) is said once through #categories-status; the list and the
// match count are never live regions, so polling never speaks.
//
// Focus: Add and Edit move it to Name; Save moves it to the saved
// category's Edit button, Cancel back to the button that opened the form,
// and Delete to the next category (or Add category).  A refused field gets
// aria-invalid, the error text as part of its description, and focus; the
// words stay visible in the form.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";
import { createConditionRow, readConditionRows } from "./condition-editor.js";
import { describeConditions } from "./clockwheel-step.js";
import { parsePointTime } from "./library-folders.js";

const JSON_HEADERS = { "Content-Type": "application/json" };
const POLL_MS = 15000;
const EXAMPLES_SHOWN = 5;
const FIELD_IDS = {
  name: "categories-name", query: "categories-query", playlist: "categories-playlist", source: "categories-source-query",
    conditions: "categories-filter-add",
  days: "categories-day-0", hours_from: "categories-hours-from", hours_until: "categories-hours-until",
  not_before: "categories-not-before", not_after: "categories-not-after",
};
const DAY_IDS = [0, 1, 2, 3, 4, 5, 6].map((day) => `categories-day-${day}`);

const number = (n) => Number(n || 0).toLocaleString("en-US");
const plural = (n, one, many) => `${number(n)} ${n === 1 ? one : many}`;

// "1,284 songs; 3 playlist lines match no library track; 12 use the release year ..."
export function countsText(counts) {
  if (counts.error) return counts.error;
  const parts = [plural(counts.tracks, "song", "songs")];
  if (counts.missing) parts.push(`${plural(counts.missing, "playlist line matches", "playlist lines match")} no library track`);
  if (counts.fallback) {
    parts.push(`${number(counts.fallback)} ${counts.fallback === 1 ? "uses" : "use"} the release year because the first-release year is missing`);
  }
  return parts.join("; ");
}

// What one saved category is, for its list item.
export function describeCategory(category, folders = []) {
  const source = category.source === "m3u" ? `playlist ${category.playlist}`
    : category.source === "filters" ? `filters: ${describeConditions(category.conditions, category.match, folders)}`
      : `query ${category.query}`;
  const sentences = [`${category.name}: ${countsText(category)}.`, `From ${source}.`];
  // Dayparting and the season, in words; "outside its hours now" (or its
  // dates) is said, not only shown.
  if (category.daypart_text) {
    const outside = ["days", "dates"].includes(category.outside_now) ? category.outside_now : "hours";
    sentences.push(`${category.daypart_text}.${category.allowed_now === false ? ` Outside its ${outside} now.` : ""}`);
  }
  const examples = (category.missing_examples || []).slice(0, EXAMPLES_SHOWN);
  if (category.missing && examples.length) {
    const more = category.missing - examples.length;
    sentences.push(`Not in the library: ${examples.join(", ")}${more > 0 ? `, and ${number(more)} more` : ""}.`);
  }
  const used = category.used_by || [];
  sentences.push(used.length ? `Used by ${used.join(", ")}.` : "No clockwheel uses it.");
  return sentences.join(" ");
}

export function installCategoriesPanel({ document: doc = document, request = requestJson, onChange = () => {} } = {}) {
  const el = (id) => doc.getElementById(id);
  const panel = el("panel-library");
  const list = el("categories-list");
  const status = el("categories-status");
  const form = el("categories-form");
  const addButton = el("categories-add");
  const nameInput = el("categories-name");
  const queryInput = el("categories-query");
  const playlistSelect = el("categories-playlist");
  const match = el("categories-match");
  const errorText = el("categories-form-error");
  const legend = el("categories-form-legend");
  const filterHost = el("categories-filter-conditions");
  if (!panel || !list || !status || !form || !addButton || !nameInput || !queryInput || !playlistSelect || !match) {
    return { refresh: async () => {}, clearProtectedData() {}, resume() {}, destroy() {} };
  }

  const empty = () => ({ categories: [], playlists: [], playlists_folder: "", beets: false });
  let data = empty();
  let editing = null; // the ID being edited, or null when adding
  let opener = null; // the button that opened the form
  let listSignature = "";
  let playlistSignature = "";
  let timer = null;
  let generation = 0;
  let suspended = false;
  let filterId = 0;
  let countTimer = null;
  let countGeneration = 0;
  let countRunning = false;
  let pendingCount = null;

  const current = (epoch, mine) => !suspended && generation === mine && isAuthenticatedRequestCurrent(epoch);
  const countCurrent = (epoch, mine) => !suspended && countGeneration === mine && isAuthenticatedRequestCurrent(epoch);
  const say = (message, error = false) => announceStatus(status, message, { force: true, tone: error ? "error" : "info" });
  const send = (url, method, body) => request(url, body === undefined ? { method } : { method, headers: JSON_HEADERS, body: JSON.stringify(body) });
  const source = () => el("categories-source-filters")?.checked ? "filters" : el("categories-source-m3u")?.checked ? "m3u" : "query";
  const byId = (id) => data.categories.find((category) => category.id === id);

  // ---- The list --------------------------------------------------------
  function rowButton(category, action, title) {
    const button = doc.createElement("button");
    button.type = "button";
    button.dataset.action = action;
    button.dataset.id = category.id;
    // The visible word starts the name; the hidden name says which category.
    const hidden = doc.createElement("span");
    hidden.className = "visually-hidden";
    hidden.textContent = ` ${category.name}`;
    button.append(title, hidden);
    return button;
  }

  function renderList() {
    const signature = JSON.stringify([data.categories, data.folders]);
    if (signature === listSignature) return;
    listSignature = signature;
    // The same categories in the same order: change only the words of each
    // row, so a poll that finds a category now outside its hours, days or
    // dates (at the station's midnight, say) moves no focus and says nothing.
    const rows = [...list.querySelectorAll(":scope > li[data-id]")];
    if (rows.length && rows.length === data.categories.length
        && rows.every((row, i) => row.dataset.id === data.categories[i].id
          && row.dataset.name === data.categories[i].name)) {
      rows.forEach((row, i) => {
      const text = describeCategory(data.categories[i], data.folders);
        const span = row.firstElementChild;
        if (span && span.textContent !== text) span.textContent = text;
      });
      return;
    }
    const focused = list.contains(doc.activeElement) ? doc.activeElement.dataset : null;
    if (!data.categories.length) {
      const none = doc.createElement("li");
      none.textContent = "No categories yet. Press Add category to make one.";
      list.replaceChildren(none);
      return;
    }
    list.replaceChildren(...data.categories.map((category) => {
      const item = doc.createElement("li");
      item.dataset.id = category.id;
      item.dataset.name = category.name; // the buttons' names say it
      const text = doc.createElement("span");
      text.textContent = describeCategory(category, data.folders);
      item.append(text, " ", rowButton(category, "edit", "Edit"), " ", rowButton(category, "delete", "Delete"));
      return item;
    }));
    if (focused?.id) focusRow(focused.id, focused.action);
  }

  function rowFor(id, action = "edit") {
    return [...list.querySelectorAll("button[data-action]")]
      .find((button) => button.dataset.id === id && button.dataset.action === action) || null;
  }

  function focusRow(id, action = "edit") {
    const button = rowFor(id, action);
    button?.focus();
    return button;
  }

  function renderPlaylists() {
    // A poll that finds the same files leaves the choice alone.
    const signature = JSON.stringify([data.playlists, data.playlists_folder]);
    if (signature === playlistSignature) return;
    playlistSignature = signature;
    const chosen = playlistSelect.value;
    const options = data.playlists.length
      ? data.playlists.map((name) => [name, name])
      : [["", "No playlist files found"]];
    playlistSelect.replaceChildren(...options.map(([value, label]) => {
      const option = doc.createElement("option");
      option.value = value;
      option.textContent = label;
      return option;
    }));
    if (data.playlists.includes(chosen)) playlistSelect.value = chosen;
    const folder = el("categories-folder");
    if (folder) folder.textContent = data.playlists_folder || "the playlists folder";
  }

  // ---- The form --------------------------------------------------------
  function showSource() {
    const kind = source();
    el("categories-query-field")?.toggleAttribute("hidden", kind !== "query");
    el("categories-playlist-field")?.toggleAttribute("hidden", kind !== "m3u");
    el("categories-filters-field")?.toggleAttribute("hidden", kind !== "filters");
  }

  function filterRow(value = {}) {
    return createConditionRow({
      document: doc,
      nextId: () => `category-condition-${++filterId}`,
      index: filterHost.children.length + 1,
      value,
      fields: ["folder", "year", "bpm", "duration", "artist", "title", "album", "genre", "beets"],
      sourceChoices: (data.folders || []).map((item) => [item.id, item.path]),
      genreChoices: (data.genres || []).map((item) => [item, item]),
      beetsChoices: (data.beets_fields || []).map((item) => [item, item]),
      onRemove: (row) => {
        row.remove();
        [...filterHost.children].forEach((item, index) => {
          item.querySelector("legend").textContent = `Condition ${index + 1}`;
          item.querySelector("button").textContent = `Remove condition ${index + 1}`;
        });
      },
    });
  }

  function renderFilterConditions(conditions = []) {
    filterHost?.replaceChildren();
    for (const value of conditions.length ? conditions : [{}]) filterHost.append(filterRow(value).row);
  }

  function readFilterConditions(interactive = true) {
    return readConditionRows([...filterHost.children], {
      interactive,
      parsePointTime,
      folderRequired: "Choose a registered music folder.",
      numberError: (field) => `Enter a valid value for ${field}.`,
      durationError: "Enter a duration in seconds or minutes:seconds.",
      beetsAvailable: (name) => Boolean(name) && !data.beets_error && (data.beets_fields || []).includes(name),
      beetsError: () => data.beets_error || "Choose an available Beets field.",
      errorField: "conditions",
    });
  }

  // The error text joins or leaves a control's own description.
  function linkError(control, linked) {
    if (!control || !errorText) return;
    const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== errorText.id);
    if (linked) ids.push(errorText.id);
    if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
    else control.removeAttribute("aria-describedby");
  }

  function unmark(control) {
    control?.removeAttribute("aria-invalid");
    linkError(control, false);
  }

  function clearErrors() {
    for (const id of new Set([...Object.values(FIELD_IDS), ...DAY_IDS])) unmark(el(id));
    for (const control of filterHost?.querySelectorAll("[aria-invalid]") || []) unmark(control);
    if (errorText) { errorText.textContent = ""; errorText.hidden = true; }
  }

  function showError(field, message) {
    const target = el(FIELD_IDS[field] || FIELD_IDS.name);
    if (field === "source" || field === "query" || field === "playlist" || field === "conditions") {
      // The field must be visible to take focus.
      if (field === "playlist") el("categories-source-m3u").checked = true;
      else if (field === "conditions") el("categories-source-filters").checked = true;
      else if (field === "query") el("categories-source-query").checked = true;
      showSource();
    }
    for (const control of field === "days" ? DAY_IDS.map(el) : [target]) {
      control?.setAttribute("aria-invalid", "true");
      linkError(control, true);
    }
    if (errorText) { errorText.textContent = message; errorText.hidden = false; }
    target?.focus();
    say(message, true);
  }

  function body(interactive = true) {
    const kind = source();
    const value = {
      name: nameInput.value.trim(),
      source: kind,
      query: kind === "query" ? queryInput.value.trim() : "",
      playlist: kind === "m3u" ? playlistSelect.value : "",
    };
    if (kind === "filters") {
      value.match = el("categories-filter-match").value;
      value.conditions = readFilterConditions(interactive);
    }
    // Sent only when set: none means every day, all day.
    const days = DAY_IDS.map(el).filter((box) => box?.checked).map((box) => Number(box.value));
    if (days.length) value.days = days;
    const from = el("categories-hours-from")?.value;
    const until = el("categories-hours-until")?.value;
    if (from) value.hours_from = from;
    if (until) value.hours_until = until;
    // The season, sent only when set: none means any day of the year.
    const first = el("categories-not-before")?.value;
    const last = el("categories-not-after")?.value;
    if (first) value.not_before = first;
    if (last) value.not_after = last;
    return value;
  }

  // Checks in the page what the server would refuse first; true when fine.
  function valid(value) {
    if (!value.name) { showError("name", "Enter a name for the category."); return false; }
    if (value.source === "query" && !value.query) {
      showError("query", "Enter a beets query, such as original_year:2000..2009.");
      return false;
    }
    if (value.source === "m3u" && !value.playlist) {
      showError("playlist", `Put an M3U or M3U8 file in ${data.playlists_folder || "the playlists folder"} first.`);
      return false;
    }
    if (value.source === "filters" && !value.conditions?.length) {
      showError("conditions", "Add at least one category condition.");
      return false;
    }
    // A half-typed time reads as empty; say so rather than save "all day".
    for (const [field, label] of [["hours_from", "Allowed from"], ["hours_until", "Allowed until"]]) {
      if (el(FIELD_IDS[field])?.validity?.badInput) {
        showError(field, `${label} is not a whole time. Enter hours and minutes, such as 22:00, or clear it.`);
        return false;
      }
    }
    if (Boolean(value.hours_from) !== Boolean(value.hours_until)) {
      showError(value.hours_from ? "hours_until" : "hours_from", "Enter both Allowed from and Allowed until, or leave both empty.");
      return false;
    }
    if (value.hours_from && value.hours_from === value.hours_until) {
      showError("hours_until", "Allowed until must differ from Allowed from.");
      return false;
    }
    // A half-typed date reads as empty too.
    for (const [field, label] of [["not_before", "Not before"], ["not_after", "Do not play after"]]) {
      if (el(FIELD_IDS[field])?.validity?.badInput) {
        showError(field, `${label} is not a whole date. Enter a day, month and year, or clear it.`);
        return false;
      }
    }
    // ISO dates compare as text.
    if (value.not_before && value.not_after && value.not_after < value.not_before) {
      showError("not_after", "Do not play after must be on or after the Not before date.");
      return false;
    }
    return true;
  }

  function openForm(category, from) {
    countGeneration += 1;
    editing = category ? category.id : null;
    opener = from;
    clearErrors();
    nameInput.value = category?.name || "";
    queryInput.value = category?.query || "";
    for (const id of DAY_IDS) {
      const box = el(id);
      if (box) box.checked = Boolean(category?.days?.includes(Number(box.value)));
    }
    for (const [id, key] of [["categories-hours-from", "hours_from"], ["categories-hours-until", "hours_until"]]) {
      const input = el(id);
      if (input) input.value = category?.[key]?.slice(0, 5) || "";
    }
    for (const [id, key] of [["categories-not-before", "not_before"], ["categories-not-after", "not_after"]]) {
      const input = el(id);
      if (input) input.value = category?.[key] || "";
    }
    el(category?.source === "m3u" ? "categories-source-m3u" : category?.source === "filters" ? "categories-source-filters" : "categories-source-query").checked = true;
    renderPlaylists();
    el("categories-filter-match").value = category?.match || "all";
    renderFilterConditions(category?.source === "filters" ? category.conditions || [] : []);
    if (category?.playlist) playlistSelect.value = category.playlist;
    showSource();
    match.textContent = category
      ? `Matching selection: ${countsText(category)} before playback restrictions.`
      : "Not checked yet.";
    if (legend) legend.textContent = category ? `Edit category ${category.name}` : "Add category";
    form.hidden = false;
    nameInput.focus();
  }

  function closeForm() {
    countGeneration += 1;
    if (countTimer !== null) clearTimeout(countTimer);
    countTimer = null;
    pendingCount = null;
    form.hidden = true;
    editing = null;
    clearErrors();
  }

  async function check() {
    countGeneration += 1;
    clearErrors();
    let value;
    try { value = body(); }
    catch (error) { showError(error.field || "conditions", error.message); return; }
    if (!valid(value)) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const counts = await send("/api/categories/check", "POST", value);
      if (!current(epoch, mine)) return;
      const text = countsText(counts);
      match.textContent = `${text}.`;
      say(`${value.name}: ${text}.`, Boolean(counts.error));
    } catch (error) {
      if (current(epoch, mine)) showError(error.field || "name", error.message);
    }
  }

  function scheduleCount() {
    if (source() === "query" && !el("categories-query").value.trim()) return;
    if (countTimer !== null) clearTimeout(countTimer);
    const mine = ++countGeneration;
    countTimer = setTimeout(async () => {
      countTimer = null;
      const epoch = captureAuthenticatedRequestEpoch();
      let value;
      try { value = body(false); }
      catch { if (countCurrent(epoch, mine)) match.textContent = "Complete the selection to check matching tracks."; return; }
      if (!countCurrent(epoch, mine)) return;
      if (!value.name) { match.textContent = "Enter a name to check matching tracks."; return; }
      pendingCount = { epoch, mine, value };
      void drainCounts();
    }, 450);
  }

  async function drainCounts() {
    if (countRunning) return;
    countRunning = true;
    try {
      while (pendingCount) {
        const item = pendingCount;
        pendingCount = null;
        if (!countCurrent(item.epoch, item.mine)) continue;
        match.textContent = "Checking matching tracks…";
        try {
          const result = await send("/api/categories/check", "POST", item.value);
          if (!countCurrent(item.epoch, item.mine)) continue;
          match.textContent = result.error
            ? `Count unavailable: ${result.error}`
            : `${countsText(result)} match the selection before playback restrictions.`;
        } catch (error) {
          if (countCurrent(item.epoch, item.mine)) match.textContent = `Count unavailable: ${error.message}`;
        }
      }
    } finally { countRunning = false; }
  }

  async function save(event) {
    event?.preventDefault();
    clearErrors();
    let value;
    try { value = body(); }
    catch (error) { showError(error.field || "conditions", error.message); return; }
    if (!valid(value)) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const result = editing
        ? await send(`/api/categories/${encodeURIComponent(editing)}`, "PUT", value)
        : await send("/api/categories", "POST", value);
      if (!current(epoch, mine)) return;
      apply(result);
      closeForm();
      focusRow(result.category.id);
      onChange();
      say(`Saved ${result.category.name}: ${countsText(result.category)}.`);
    } catch (error) {
      if (current(epoch, mine)) showError(error.field || "name", error.message);
    }
  }

  function apply(result) {
    data = { ...empty(), ...result };
    renderList();
    renderPlaylists();
  }

  async function remove(id) {
    const category = byId(id);
    if (!category) return;
    const used = category.used_by || [];
    const index = data.categories.indexOf(category);
    let done = false;
    // The request runs while the dialog is open; the result is said after it closes.
    await confirmAction(doc, {
      title: `Delete category ${category.name}?`,
      message: used.length
        ? `${used.join(", ")} ${used.length === 1 ? "has a step" : "have steps"} for it. Those steps are skipped until you change them.`
        : "No clockwheel uses it. Backups made earlier still hold it.",
      confirmLabel: "Delete category",
      onConfirm: async () => {
        const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
        try {
          const result = await send(`/api/categories/${encodeURIComponent(id)}`, "DELETE");
          if (!current(epoch, mine)) return null;
          if (editing === id) closeForm();
          apply(result);
          onChange();
          done = true;
          const next = data.categories[Math.min(index, data.categories.length - 1)];
          return (next && rowFor(next.id)) || addButton;
        } catch (error) {
          if (current(epoch, mine)) say(`Could not delete ${category.name}: ${error.message}`, true);
          return null;
        }
      },
    });
    if (done) say(`Deleted ${category.name}.`);
  }

  list.addEventListener("click", (event) => {
    const button = event.target.closest?.("button[data-action]");
    if (!button) return;
    if (button.dataset.action === "edit") {
      const category = byId(button.dataset.id);
      if (category) openForm(category, button);
    } else if (button.dataset.action === "delete") void remove(button.dataset.id);
  });
  addButton.addEventListener("click", () => openForm(null, addButton));
  el("categories-check")?.addEventListener("click", () => void check());
  form.addEventListener("submit", (event) => void save(event));
  el("categories-cancel")?.addEventListener("click", () => {
    const back = editing ? focusRow(editing) : null;
    closeForm();
    if (!back) (opener?.isConnected ? opener : addButton).focus();
    say("Cancelled. Nothing was saved.");
  });
  for (const id of ["categories-source-query", "categories-source-m3u", "categories-source-filters"]) {
    el(id)?.addEventListener("change", () => {
      showSource();
      match.textContent = "Not checked yet.";
      scheduleCount();
    });
  }
  el("categories-filter-add")?.addEventListener("click", () => {
    if (filterHost.children.length >= 32) { showError("conditions", "A category can have at most 32 conditions."); return; }
    const item = filterRow(); filterHost.append(item.row); item.field.focus(); match.textContent = "Not checked yet.";
  });
  el("categories-filter-match")?.addEventListener("change", () => { match.textContent = "Not checked yet."; scheduleCount(); });
  for (const field of [nameInput, queryInput, playlistSelect, el("categories-hours-from"),
    el("categories-hours-until"), el("categories-not-before"), el("categories-not-after")]) {
    field?.addEventListener("input", () => { unmark(field); scheduleCount(); });
    field?.addEventListener("change", scheduleCount);
  }
  // The days are marked as one group, so changing any one clears them all.
  for (const id of DAY_IDS) el(id)?.addEventListener("input", () => { DAY_IDS.forEach((day) => unmark(el(day))); scheduleCount(); });
  filterHost?.addEventListener("input", scheduleCount);
  filterHost?.addEventListener("change", scheduleCount);
  el("categories-filter-add")?.addEventListener("click", scheduleCount);
  filterHost?.addEventListener("click", (event) => { if (event.target.closest?.("button")) scheduleCount(); });

  // ---- Loading and polling ---------------------------------------------
  async function refresh() {
    if (panel.hidden || doc.hidden || suspended) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const value = await request("/api/categories");
      if (!current(epoch, mine)) return;
      apply(value);
    } catch (error) {
      if (current(epoch, mine)) say(`Could not load categories: ${error.message}`, true);
    }
  }

  function updatePolling() {
    if (suspended || panel.hidden || doc.hidden) {
      if (timer !== null) clearInterval(timer);
      timer = null;
    } else if (timer === null) {
      void refresh();
      timer = setInterval(refresh, POLL_MS);
    }
  }
  window.addEventListener("hashchange", updatePolling);
  doc.addEventListener("visibilitychange", updatePolling);
  showSource();
  updatePolling();

  function clearProtectedData() {
    suspended = true;
    generation += 1;
    if (timer !== null) clearInterval(timer);
    timer = null;
    data = empty();
    listSignature = "";
    playlistSignature = "";
    list.replaceChildren();
    closeForm();
    nameInput.value = "";
    queryInput.value = "";
    playlistSelect.replaceChildren();
    match.textContent = "Not checked yet.";
    status.textContent = "";
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
