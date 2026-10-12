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

const JSON_HEADERS = { "Content-Type": "application/json" };
const POLL_MS = 15000;
const EXAMPLES_SHOWN = 5;
const FIELD_IDS = {
  name: "categories-name", query: "categories-query", playlist: "categories-playlist", source: "categories-source-query",
  days: "categories-day-0", hours_from: "categories-hours-from", hours_until: "categories-hours-until",
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
export function describeCategory(category) {
  const source = category.source === "m3u" ? `playlist ${category.playlist}` : `query ${category.query}`;
  const sentences = [`${category.name}: ${countsText(category)}.`, `From ${source}.`];
  // Dayparting, in words; "outside its hours now" is said, not only shown.
  if (category.daypart_text) {
    sentences.push(`${category.daypart_text}.${category.allowed_now === false ? " Outside its hours now." : ""}`);
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

  const current = (epoch, mine) => !suspended && generation === mine && isAuthenticatedRequestCurrent(epoch);
  const say = (message, error = false) => announceStatus(status, message, { force: true, tone: error ? "error" : "info" });
  const send = (url, method, body) => request(url, body === undefined ? { method } : { method, headers: JSON_HEADERS, body: JSON.stringify(body) });
  const source = () => (el("categories-source-m3u")?.checked ? "m3u" : "query");
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
    const signature = JSON.stringify(data.categories);
    if (signature === listSignature) return;
    listSignature = signature;
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
      const text = doc.createElement("span");
      text.textContent = describeCategory(category);
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
    if (errorText) { errorText.textContent = ""; errorText.hidden = true; }
  }

  function showError(field, message) {
    const target = el(FIELD_IDS[field] || FIELD_IDS.name);
    if (field === "source" || field === "query" || field === "playlist") {
      // The field must be visible to take focus.
      if (field === "playlist") el("categories-source-m3u").checked = true;
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

  function body() {
    const kind = source();
    const value = {
      name: nameInput.value.trim(),
      source: kind,
      query: kind === "query" ? queryInput.value.trim() : "",
      playlist: kind === "m3u" ? playlistSelect.value : "",
    };
    // Sent only when set: none means every day, all day.
    const days = DAY_IDS.map(el).filter((box) => box?.checked).map((box) => Number(box.value));
    if (days.length) value.days = days;
    const from = el("categories-hours-from")?.value;
    const until = el("categories-hours-until")?.value;
    if (from) value.hours_from = from;
    if (until) value.hours_until = until;
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
    return true;
  }

  function openForm(category, from) {
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
    el(category?.source === "m3u" ? "categories-source-m3u" : "categories-source-query").checked = true;
    renderPlaylists();
    if (category?.playlist) playlistSelect.value = category.playlist;
    showSource();
    match.textContent = category ? `Saved: ${countsText(category)}.` : "Not checked yet.";
    if (legend) legend.textContent = category ? `Edit category ${category.name}` : "Add category";
    form.hidden = false;
    nameInput.focus();
  }

  function closeForm() {
    form.hidden = true;
    editing = null;
    clearErrors();
  }

  async function check() {
    clearErrors();
    const value = body();
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

  async function save(event) {
    event?.preventDefault();
    clearErrors();
    const value = body();
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
  for (const id of ["categories-source-query", "categories-source-m3u"]) {
    el(id)?.addEventListener("change", () => {
      showSource();
      match.textContent = "Not checked yet.";
    });
  }
  for (const field of [nameInput, queryInput, playlistSelect, el("categories-hours-from"), el("categories-hours-until")]) {
    field?.addEventListener("input", () => unmark(field));
  }
  // The days are marked as one group, so changing any one clears them all.
  for (const id of DAY_IDS) el(id)?.addEventListener("input", () => DAY_IDS.forEach((day) => unmark(el(day))));

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
