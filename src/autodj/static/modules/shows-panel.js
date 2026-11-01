// Shows (#106): a list of songs prepared ahead that a schedule event airs.
//
// The server keeps them in shows.json and generates a list with a dry run of
// the station's picker. This module lists shows in a shared table, adds,
// edits and deletes them through a dialog, and opens one show's list to
// generate it again, move songs up and down, remove them and add songs from
// a library search.  Each change to the list is saved at once.
//
// Collection rows share cell navigation, menus and focus recovery. Metadata
// edits use the guarded editor dialog; song changes save immediately.
import { createCollectionTable } from "./collection-table.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { createEditorDialog } from "./editor-dialog.js";
import { failureText, captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson, withDisabled } from "./api-client.js";
import { sendWithVoiceTrackConflict } from "./voice-track-conflict.js";
import { makeSay } from "./live-region.js";
import { applyServerFieldError, clearFieldError, fmtTime, selectOnFocus, sentences, showFieldError } from "./dom-helpers.js";
import { installPreparedEditions } from "./prepared-editions.js";
import { checkStationName } from "./station-name-field.js";

const JSON_HEADERS = { "Content-Type": "application/json" };
const POLL_MS = 15000;
const SEARCH_LIMIT = 20;
const FIELD_IDS = {
  name: "shows-name", source: "shows-source-category", category: "shows-category",
  clockwheel: "shows-clockwheel", target_minutes: "shows-target", station_name: "shows-station-name",
};

const number = (n) => Number(n || 0).toLocaleString("en-US");
const plural = (n, one, many) => `${number(n)} ${n === 1 ? one : many}`;

export function describeShow(show) {
  const parts = [`${show.name}: ${plural(show.count, "song", "songs")}, ${show.count ? show.total_text : "no list yet"}.`];
  parts.push(`From ${show.source_text}, target ${plural(show.target_minutes, "minute", "minutes")}.`);
  if (show.missing) parts.push(`${plural(show.missing, "song is", "songs are")} no longer in the library.`);
  if (show.station_name) parts.push(`Station name while on the air: ${show.station_name}.`);
  const events = show.events || [];
  parts.push(events.length ? `Played by ${events.join(", ")}.` : "No schedule event plays it.");
  return parts.join(" ");
}

// The show on the air, in words.
export function describeOnAir(onAir) {
  if (!onAir) return "No show is on the air.";
  if (onAir.interrupted) {
    return `${onAir.name} was interrupted. Normal automation continues. Open its prepared edition, choose an item, and Resume edition explicitly.`;
  }
  if (onAir.pending_resume) {
    return `${onAir.name} was on the air before AutoDJ restarted and will resume at the next song, `
      + `from song ${number(onAir.resume_at)} of ${number(onAir.total)}. Stop show cancels it.`;
  }
  if (onAir.stopping) {
    return `${onAir.name} is stopping: ${plural(onAir.waiting, "song is", "songs are")} still in the queue, `
      + "and AutoDJ takes them out at the next song.";
  }
  const parts = [
    `${plural(onAir.played, "song", "songs")} of ${number(onAir.total)} played`,
    `${number(onAir.waiting)} waiting in the queue`,
    `${number(onAir.to_come)} to come`,
  ];
  let text = `${onAir.name} is on the air: ${parts.join(", ")}.`;
  if (onAir.deferred_reason) text += ` ${onAir.deferred_reason}`;
  const skipped = onAir.skipped || [];
  if (onAir.skipped_count) {
    const shown = skipped.slice(0, 3).map((item) => `${item.title} (${item.reason})`);
    const more = onAir.skipped_count - shown.length;
    text += ` Skipped: ${shown.join("; ")}${more > 0 ? `, and ${number(more)} more` : ""}.`;
  }
  return text;
}

// The list's total against its target.
export function summaryText(show) {
  if (!show.count) return `No songs yet. Target ${plural(show.target_minutes, "minute", "minutes")}.`;
  let text = `${plural(show.count, "song", "songs")}, ${show.total_text}; target ${plural(show.target_minutes, "minute", "minutes")}.`;
  if (show.missing) text += ` ${plural(show.missing, "song is", "songs are")} no longer in the library and will be skipped.`;
  if (show.repeated) text += ` Warning: ${plural(show.repeated, "song is", "songs are")} in the list more than once, and each copy plays.`;
  return text;
}

// Each path's places in the list (1-based), for the songs listed more than once.
function repeatedPlaces(songs) {
  const places = new Map();
  songs.forEach((song, index) => {
    places.set(song.path, [...(places.get(song.path) || []), index + 1]);
  });
  return new Map([...places].filter(([, numbers]) => numbers.length > 1));
}

export function installShowsPanel({ document: doc = document, request = requestJson } = {}) {
  const el = (id) => doc.getElementById(id);
  const panel = el("panel-library");
  const list = el("shows-list");
  const status = el("shows-status");
  const form = el("shows-form");
  const addButton = el("shows-add");
  let listFocusAnnouncementSuppressed = false;
  const nameInput = el("shows-name");
  const categorySelect = el("shows-category");
  const clockSelect = el("shows-clockwheel");
  const targetInput = el("shows-target");
  const stationNameInput = el("shows-station-name");
  const errorText = el("shows-form-error");
  const legend = el("shows-form-legend");
  const editor = el("shows-editor");
  const songsList = el("shows-songs");
  const results = el("shows-results");
  const searchInput = el("shows-search");
  const onAirText = el("shows-on-air");
  const stopButton = el("shows-stop");
  if (!panel || !list || !status || !form || !addButton || !nameInput || !categorySelect
    || !clockSelect || !targetInput || !editor || !songsList || !results || !searchInput) {
    return { refresh: async () => {}, clearProtectedData() {}, resume() {}, destroy() {} };
  }

  const empty = () => ({ shows: [], categories: [], clockwheels: [], on_air: null, problem: null, limits: {} });
  let data = empty();
  let editing = null; // the show ID the form edits, or null when adding
  let open = null; // the show whose list is open, with its songs
  let songs = []; // the open list's songs, as shown
  let listSignature = "";
  let songsSignature = "";
  let saving = Promise.resolve();
  let formGeneration = 0;
  let generating = false;
  let loadFailed = false;
  let timer = null;
  let generation = 0;
  let suspended = false;

  const current = (epoch, mine) => !suspended && generation === mine && isAuthenticatedRequestCurrent(epoch);
  const say = makeSay(() => metadataEditor.region(status), {});
  const send = (url, method, body) => request(url, body === undefined ? { method } : { method, headers: JSON_HEADERS, body: JSON.stringify(body) });
  const source = () => (el("shows-source-clockwheel")?.checked ? "clockwheel" : "category");
  const byId = (id) => data.shows.find((show) => show.id === id);
  const url = (id, tail = "") => `/api/shows/${encodeURIComponent(id)}${tail}`;
  const editions = installPreparedEditions({ doc, request, onChange: updateEditionSearch });

  const metadataEditor = createEditorDialog({
    form, heading: legend, cancel: el("shows-cancel"), fallback: addButton, save, pendingText: "Saving show?",
    onCancel: () => { editing = null; clearErrors(); say("Cancelled. Nothing was saved."); },
  });
  const showTable = createCollectionTable({
    host: list, label: "Shows", status, fallback: addButton,
    keyOf: (show) => show.id, nameOf: (show) => show.name,
    emptyText: "No shows yet. Press Add show to plan one.",
    columns: [
      { key: "name", label: "Name", value: (show) => show.name },
      { key: "count", label: "Songs", value: (show) => number(show.count) },
      { key: "length", label: "Length", value: (show) => show.count ? show.total_text : "No list yet" },
      { key: "details", label: "Details", value: (show) => sentences(
        `From ${show.source_text}, target ${plural(show.target_minutes, "minute", "minutes")}`,
        show.missing && `${plural(show.missing, "song is", "songs are")} no longer in the library`,
        show.station_name && `Station name while active: ${show.station_name}`,
        show.events?.length ? `Played by ${show.events.join(", ")}` : "No schedule event plays it") },
    ],
    actions: (show) => [
      { id: "open", label: "Open list", run: () => void openEditor(show.id) },
      { id: "edit", label: "Edit", run: (button) => openForm(show, button) },
      { id: "delete", label: "Delete", run: () => void remove(show.id) },
    ],
  });
  showTable.update([], { announceFocus: false });
  const songKeys = new WeakMap();
  let nextSongKey = 0;
  const songKey = (song) => {
    if (!songKeys.has(song)) songKeys.set(song, String(++nextSongKey));
    return songKeys.get(song);
  };
  const generationReason = () => generating ? "The list is being generated; change it when that is done." : "";
  const songTable = createCollectionTable({
    host: songsList, label: "Show songs", status, fallback: searchInput,
    keyOf: songKey, nameOf: (song) => song.title,
    emptyText: "No songs yet. Press Generate list, or add songs below.",
    columns: [
      // The song's number and its warnings, as the list read before #707.
      { key: "title", label: "Song", value: (song, index) => {
        const others = (repeated.get(song.path) || []).filter((place) => place !== index + 1);
        return `${number(index + 1)}. ${song.title}${song.missing ? ", not in the library; skipped on the air" : ""}`
          + (others.length ? `; warning: also number ${others.join(" and ")} in the list` : "");
      } },
      { key: "length", label: "Length", value: (song) => song.missing ? "Unknown" : song.length_text },
    ],
    actions: (song, index) => [
      { id: "info", label: "Track info", disabledReason: song.missing ? "This song is no longer in the library." : "",
        run: (button) => doc.dispatchEvent(new CustomEvent("autodj:edit-points", { detail: { path: song.path, kind: "music", opener: button } })) },
      { id: "up", label: "Move up", disabledReason: generationReason() || (index === 0 ? "Already the first song." : ""), run: () => move(index, -1) },
      { id: "down", label: "Move down", disabledReason: generationReason() || (index === songs.length - 1 ? "Already the last song." : ""), run: () => move(index, 1) },
      { id: "remove", label: "Remove", disabledReason: generationReason(), run: () => void removeSong(song) },
    ],
  });
  let searchSongs = [];
  const searchTable = createCollectionTable({
    host: results, label: "Show search results", status, fallback: searchInput,
    keyOf: (song) => song.path, nameOf: (song) => song.title,
    emptyText: "No songs found.",
    columns: [
      { key: "title", label: "Song", value: (song) => song.title },
      { key: "length", label: "Length", value: (song) => song.length_text },
    ],
    actions: (song) => [
      { id: "add", label: "Add", disabledReason: generationReason(), run: () => addSong(song) },
      { id: "edition-add", label: "Add to edition", disabledReason: editions.active() ? "" : "Open a prepared edition first.",
        run: () => void editions.addSong({ path: song.path, title: song.title }) },
    ],
  });

  function updateEditionSearch() { searchTable.update(searchSongs); }

  function renderList() {
    const signature = JSON.stringify(data.shows);
    if (signature === listSignature) return;
    listSignature = signature;
    showTable.update(data.shows, { announceFocus: !listFocusAnnouncementSuppressed });
  }

  function rowFor(id) {
    return showTable.target(id);
  }

  function renderOnAir() {
    const text = describeOnAir(data.on_air);
    if (onAirText && onAirText.textContent !== text) onAirText.textContent = text;
    if (stopButton) {
      const hide = !data.on_air;
      if (hide && doc.activeElement === stopButton) addButton.focus();
      stopButton.hidden = hide;
    }
    const problem = el("shows-problem");
    if (problem) {
      problem.textContent = data.problem || "";
      problem.hidden = !data.problem;
    }
  }

  function fillSelect(select, options, chosen, none) {
    const items = options.length ? options : [["", none]];
    select.replaceChildren(...items.map(([value, label]) => {
      const option = doc.createElement("option");
      option.value = value;
      option.textContent = label;
      return option;
    }));
    if (options.some(([value]) => value === chosen)) select.value = chosen;
  }

  function renderSources(show = null) {
    fillSelect(categorySelect, data.categories.map((item) => [item.id, item.name]),
      show?.category ?? categorySelect.value, "No categories yet");
    fillSelect(clockSelect, data.clockwheels.map((name) => [name, name]),
      show?.clockwheel ?? clockSelect.value, "No clockwheels yet");
  }

  // ---- The form --------------------------------------------------------
  function showSource() {
    const kind = source();
    el("shows-category-field")?.toggleAttribute("hidden", kind !== "category");
    el("shows-category-help")?.toggleAttribute("hidden", kind !== "category");
    el("shows-clockwheel-field")?.toggleAttribute("hidden", kind !== "clockwheel");
    el("shows-clockwheel-help")?.toggleAttribute("hidden", kind !== "clockwheel");
  }

  function clearErrors() {
    for (const id of Object.values(FIELD_IDS)) clearFieldError(el(id), errorText);
  }

  function showError(field, message) {
    if (field === "category") el("shows-source-category").checked = true;
    if (field === "clockwheel") el("shows-source-clockwheel").checked = true;
    showSource();
    const target = el(FIELD_IDS[field] || FIELD_IDS.name);
    showFieldError(target, message, errorText, metadataEditor.region(status));
  }

  function body() {
    const kind = source();
    return {
      name: nameInput.value.trim(),
      source: kind,
      category: kind === "category" ? categorySelect.value : "",
      clockwheel: kind === "clockwheel" ? clockSelect.value : "",
      target_minutes: Number(targetInput.value.trim()),
      station_name: checkStationName(stationNameInput?.value).name ?? "",
    };
  }

  function valid(value) {
    const limits = data.limits || {};
    const low = limits.min_minutes || 5;
    const high = limits.max_minutes || 360;
    if (!value.name) { showError("name", "Enter a name for the show."); return false; }
    if (value.source === "category" && !value.category) {
      showError("category", "Add a category under Categories first, or choose a clockwheel.");
      return false;
    }
    if (value.source === "clockwheel" && !value.clockwheel) {
      showError("clockwheel", "Create a clockwheel under Clockwheels first, or choose a category.");
      return false;
    }
    if (!/^\d+$/.test(targetInput.value.trim()) || value.target_minutes < low || value.target_minutes > high) {
      showError("target_minutes", `Target length must be a whole number of minutes from ${low} to ${high}.`);
      return false;
    }
    const named = checkStationName(stationNameInput?.value);
    if (named.error) { showError("station_name", named.error); return false; }
    return true;
  }

  function openForm(show, from) {
    formGeneration += 1;
    editing = show ? show.id : null;
    clearErrors();
    nameInput.value = show?.name || "";
    targetInput.value = String(show?.target_minutes || 60);
    if (stationNameInput) stationNameInput.value = show?.station_name || "";
    el(show?.source === "clockwheel" ? "shows-source-clockwheel" : "shows-source-category").checked = true;
    renderSources(show);
    showSource();
    if (legend) legend.textContent = show ? `Edit show ${show.name}` : "Add show";
    metadataEditor.open(nameInput, from);
  }

  function closeForm() {
    formGeneration += 1;
    metadataEditor.close();
    editing = null;
    clearErrors();
  }

  async function save(event, editorCurrent) {
    event?.preventDefault();
    clearErrors();
    const value = body();
    if (!valid(value)) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    const formMine = formGeneration;
    const editingId = editing;
    const added = editingId === null;
    try {
      const show = editingId
        ? await send(url(editingId), "PUT", value)
        : await send("/api/shows", "POST", value);
      if (!current(epoch, mine) || !editorCurrent()) return;
      if (formMine !== formGeneration) {
        try { await load(); } catch (_) { /* the next refresh draws the list */ }
        if (current(epoch, mine)) say(added
          ? `Saved ${show.name}. Press Open list, then Generate list, to pick its songs.`
          : `Saved ${show.name}.`);
        return;
      }
      // Keep the editor open through the reload. The shared dialog guard
      // hands focus directly to the saved show when the editor closes.
      try { await load(); } catch (_) { /* the next refresh draws the list */ }
      if (!current(epoch, mine) || formMine !== formGeneration) return;
      if (open?.id === show.id) applyOpen(show);
      closeForm();
      (rowFor(show.id) || addButton).focus();
      say(added ? `Saved ${show.name}. Press Open list, then Generate list, to pick its songs.` : `Saved ${show.name}.`);
    } catch (error) {
      if (!current(epoch, mine) || !editorCurrent()) return;
      if (formMine !== formGeneration) say(error.message, "error");
      else if (FIELD_IDS[error.field]) {
        if (error.field === "category") el("shows-source-category").checked = true;
        if (error.field === "clockwheel") el("shows-source-clockwheel").checked = true;
        showSource();
        applyServerFieldError(error, FIELD_IDS, errorText, metadataEditor.region(status));
      }
      else say(error.message, "error");
    }
  }

  async function remove(id) {
    const show = byId(id);
    if (!show) return;
    const index = data.shows.indexOf(show);
    if (open?.id === id && !await editions.confirmLeave()) return;
    const events = show.events || [];
    let done = false;
    let destination = "Add show";
    await deleteCollectionItem(doc, {
      title: `Delete show ${show.name}?`,
      message: events.length
        ? `${events.join(", ")} ${events.length === 1 ? "plays" : "play"} it; those events fail until you change them.`
        : "No schedule event plays it. Backups made earlier still hold it.",
      confirmLabel: "Delete show",
      onConfirm: async () => {
        const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
        try {
          await send(url(id), "DELETE");
          if (!current(epoch, mine)) return null;
          if (editing === id) closeForm();
          if (open?.id === id) closeEditor();
          listFocusAnnouncementSuppressed = true;
          try { await load(); } finally { listFocusAnnouncementSuppressed = false; }
          done = true;
          const next = data.shows[Math.min(index, data.shows.length - 1)];
          destination = next ? `Open list ${next.name}` : "Add show";
          return (next && rowFor(next.id)) || addButton;
        } catch (error) {
          if (current(epoch, mine)) say(failureText(`delete ${show.name}`, error), "error");
          return null;
        }
      },
    });
    if (done) say(`Deleted ${show.name}. Focus moved to ${destination}.`);
  }

  // ---- The open list ---------------------------------------------------
  let repeated = new Map();

  function renderSongs(focus = null) {
    const signature = JSON.stringify([songs, generating]);
    if (signature === songsSignature && !focus) return;
    songsSignature = signature;
    repeated = repeatedPlaces(songs);
    songTable.update(songs, { announceFocus: !focus });
    if (focus) (songTable.target(songKeys.get(songs[Number(focus.index)]), "options") || searchInput).focus();
  }

  function redrawSongs() {
    songsSignature = "";
    renderSongs();
    searchTable.update(searchSongs);
  }

  // While the list is generated, an edit would be overwritten: it is refused, in words.
  function refusedWhileGenerating() {
    if (!generating) return false;
    say("The list is being generated; change it when that is done.");
    return true;
  }

  function renderSummary() {
    if (!open) return;
    const summary = el("shows-summary");
    const total = songs.reduce((sum, song) => sum + (song.missing ? 0 : Number(song.seconds) || 0), 0);
    const repeatedCount = [...repeatedPlaces(songs).values()].reduce((sum, places) => sum + places.length - 1, 0);
    const view = {
      ...open, count: songs.length, missing: songs.filter((song) => song.missing).length,
      repeated: repeatedCount, total_text: fmtTime(total),
    };
    if (summary) summary.textContent = summaryText(view);
    const note = el("shows-note");
    if (note) { note.textContent = open.note || ""; note.hidden = !open.note; }
  }

  function applyOpen(show) {
    open = show;
    const previous = new Map();
    for (const song of songs) previous.set(song.path, [...(previous.get(song.path) || []), songKey(song)]);
    songs = show.songs || [];
    for (const song of songs) {
      const key = previous.get(song.path)?.shift();
      if (key) songKeys.set(song, key);
    }
    const name = el("shows-editor-name");
    if (name) name.textContent = show.name;
    redrawSongs();
    renderSummary();
  }

  async function openEditor(id) {
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const show = await request(url(id));
      if (!current(epoch, mine)) return;
      if (await editions.openShow(show, { refresh: false }) === false || !current(epoch, mine)) return;
      songsSignature = "";
      searchSongs = [];
      searchTable.clear();
      applyOpen(show);
      editor.hidden = false;
      el("shows-editor-heading")?.focus();
      say(`Opened ${show.name}. ${summaryText(show)}`);
    } catch (error) {
      if (current(epoch, mine)) say(failureText("open the show", error), "error");
    }
  }

  function closeEditor() {
    editions.clear();
    editor.hidden = true;
    open = null;
    songs = [];
    songsSignature = "";
    searchSongs = [];
    searchTable.clear();
    songTable.clear();
  }

  // Saves the list as it is now; changes made meanwhile wait their turn,
  // and each save sends the latest list.
  function saveSongs(message) {
    if (!open) return Promise.resolve();
    const id = open.id;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    saving = saving.then(async () => {
      if (!open || open.id !== id) return;
      try {
        const show = await send(url(id, "/songs"), "PUT", { songs: songs.map((song) => song.path) });
        if (!current(epoch, mine) || open?.id !== id) return;
        open = { ...show, songs: open.songs };
        renderSummary();
        if (message) say(`${message} ${summaryText({ ...show, count: songs.length })}`);
        void refresh();
      } catch (error) {
        if (!current(epoch, mine)) return;
        say(`${failureText("save the list", error)} The saved list is shown again.`, "error");
        try {
          const show = await request(url(id));
          if (current(epoch, mine) && open?.id === id) {
            const focused = doc.activeElement;
            const focusedRow = focused.closest?.("tr[data-row-key]");
            const place = songsList.contains(focused) ? { index: songs.findIndex((song) => songKey(song) === focusedRow?.dataset.rowKey) } : null;
            applyOpen(show);
            if (place) renderSongs(place);
          }
        } catch (_) { /* the error is said already */ }
      }
    });
    return saving;
  }

  function move(index, delta) {
    if (refusedWhileGenerating()) return;
    const target = index + delta;
    const action = delta < 0 ? "up" : "down";
    if (target < 0 || target >= songs.length) {
      say(delta < 0 ? "Already the first song." : "Already the last song.");
      return;
    }
    const moved = songs[index];
    songs = [...songs];
    songs.splice(index, 1);
    songs.splice(target, 0, moved);
    renderSongs({ action, index: String(target) });
    renderSummary();
    void saveSongs(`Moved ${moved.title} to number ${target + 1} of ${songs.length}.`);
  }

  async function removeSong(gone) {
    if (refusedWhileGenerating() || !open || !songs.includes(gone)) return;
    const id = open.id, epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    let removed = false;
    let destination = "Search for songs";
    await deleteCollectionItem(doc, {
      title: `Remove ${gone.title}?`, message: "This removes the song from this show's list. The library file is kept.",
      confirmLabel: "Remove song",
      onConfirm: () => {
        if (!current(epoch, mine) || open?.id !== id || refusedWhileGenerating()) return null;
        const index = songs.indexOf(gone);
        if (index < 0) return null;
        songs = songs.filter((song) => song !== gone);
        const next = Math.min(index, songs.length - 1);
        renderSongs();
        renderSummary();
        removed = true;
        destination = next >= 0 ? `Options for ${songs[next].title}` : "Search for songs";
        return next >= 0 ? songTable.target(songKey(songs[next]), "options") : searchInput;
      },
    });
    if (removed) void saveSongs(`Removed ${gone.title}. Focus moved to ${destination}.`);
  }

  async function generate() {
    if (!open || generating) {
      if (generating) say("The list is being generated; one moment.");
      return;
    }
    const button = el("shows-generate");
    const startInput = el("shows-start");
    if (startInput?.validity?.badInput) {
      showFieldError(startInput, "Pick as if at is not a whole date and time. Complete it or clear it.", el("shows-start-error"), status);
      return;
    }
    clearFieldError(startInput, el("shows-start-error"));
    const id = open.id;
    const value = startInput?.value ? { start: startInput.value } : {};
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    // generating holds off another press; no aria-disabled, or NVDA says
    // "unavailable" on the pressed button (#331).  aria-busy says it is busy.
    generating = true;
    redrawSongs();
    say(`Generating the list for ${open.name}.`);
    try {
      const show = await withDisabled(button, async () => {
        await saving;
        return send(url(id, "/generate"), "POST", value);
      });
      if (!current(epoch, mine) || open?.id !== id) return;
      generating = false;
      applyOpen(show);
      void refresh();
      say(`Generated ${summaryText(show)}${show.note ? ` ${show.note}` : ""}`);
    } catch (error) {
      if (!current(epoch, mine)) return;
      const message = failureText("generate the list", error);
      if (!applyServerFieldError({ field: error.field, message }, { start: startInput }, el("shows-start-error"), status)) say(message, "error");
    } finally {
      generating = false;
      if (open?.id === id) redrawSongs();
    }
  }

  async function search() {
    const query = searchInput.value.trim();
    if (query.length < 2) {
      searchInput.focus();
      say("Type at least two letters to search.", "error");
      return;
    }
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const answer = await request(`/api/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}`);
      if (!current(epoch, mine)) return;
      const found = answer.results || [];
      searchSongs = found.map((song) => {
        const title = song.artist ? `${song.artist}, ${song.title || song.display_name}` : (song.title || song.display_name);
        const seconds = Math.max(0, Math.round(Number(song.length) || 0));
        return { path: song.path, title, seconds, length_text: fmtTime(seconds), missing: false };
      });
      updateEditionSearch();
      say(found.length ? `${plural(found.length, "song", "songs")} found${found.length === SEARCH_LIMIT ? `; the first ${SEARCH_LIMIT} are listed` : ""}.` : `No songs match ${query}.`);
    } catch (error) {
      if (current(epoch, mine)) say(failureText("search", error), "error");
    }
  }

  function addSong(song) {
    if (!open || refusedWhileGenerating()) return;
    const max = data.limits?.max_songs || 500;
    if (songs.length >= max) { say(`A show holds at most ${max} songs.`, "error"); return; }
    songs = [...songs, { ...song }];
    renderSongs();
    renderSummary();
    void saveSongs(`Added ${song.title} as number ${songs.length}.`);
  }

  async function startNow() {
    if (!open) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      await saving;
        const target = url(open.id, "/start");
        const result = await sendWithVoiceTrackConflict(doc, (voice) => current(epoch, mine)
          ? send(target, "POST", Object.keys(voice).length ? voice : undefined) : undefined);
        if (!result) return;
        const answer = result.value;
      if (!current(epoch, mine)) return;
      await load();
      say(answer.message || "The show started.");
    } catch (error) {
      if (current(epoch, mine)) say(`The show did not start: ${error.message}`, "error");
    }
  }

  async function stopShow() {
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const wasResuming = Boolean(data.on_air?.pending_resume);
        const result = await sendWithVoiceTrackConflict(doc, (voice) => current(epoch, mine)
          ? send("/api/shows/on-air", "DELETE", Object.keys(voice).length ? voice : undefined) : undefined);
        if (!result) return;
        const answer = result.value;
      if (!current(epoch, mine)) return;
      // Focus moves to Add show only if Stop show is hidden (renderOnAir).
      await load();
      if (wasResuming) say(`${answer.stopped} will not resume.`);
      else if (answer.done === false) {
        say(`Stopping ${answer.stopped}: some of its songs could not be taken out of the queue yet. AutoDJ tries again at the next song.`);
      } else say(`Stopped ${answer.stopped}. Its waiting songs left the queue; normal programming resumes.`);
    } catch (error) {
      if (current(epoch, mine)) say(failureText("stop the show", error), "error");
    }
  }

  // ---- Events ----------------------------------------------------------
  songsList.addEventListener("keydown", (event) => {
    if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
    if (event.target.closest('[role="menu"]')) return;
    const row = event.target.closest("tr[data-row-key]");
    const index = songs.findIndex((song) => songKey(song) === row?.dataset.rowKey);
    if (index < 0) return;
    event.preventDefault();
    event.stopPropagation();
    move(index, event.key === "ArrowUp" ? -1 : 1);
  });
  addButton.addEventListener("click", () => openForm(null, addButton));
  for (const id of ["shows-source-category", "shows-source-clockwheel"]) el(id)?.addEventListener("change", showSource);
  for (const field of [nameInput, categorySelect, clockSelect, targetInput, stationNameInput].filter(Boolean)) {
    field.addEventListener("input", () => {
      if (field.getAttribute("aria-invalid") === "true") clearFieldError(field, errorText);
    });
  }
  el("shows-start")?.addEventListener("input", (event) => clearFieldError(event.target, el("shows-start-error")));
  el("shows-generate")?.addEventListener("click", () => void generate());
  el("shows-search-run")?.addEventListener("click", () => void search());
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); void search(); }
  });
  selectOnFocus(searchInput);
  el("shows-play-now")?.addEventListener("click", () => void startNow());
  stopButton?.addEventListener("click", () => void stopShow());
  el("shows-close")?.addEventListener("click", async () => {
    const id = open?.id, epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    if (!await editions.confirmLeave() || !current(epoch, mine)) return;
    closeEditor();
    ((id && rowFor(id)) || addButton).focus();
  });

  // ---- Loading and polling ---------------------------------------------
  async function load() {
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    const value = await request("/api/shows");
    if (!current(epoch, mine)) return;
    data = { ...empty(), ...value };
    renderList();
    renderOnAir();
    if (form.hidden) renderSources();
  }

  async function refresh() {
    if (panel.hidden || doc.hidden || suspended) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      await load();
      loadFailed = false;
    } catch (error) {
      // Said once on failing, not on every 15-second retry.
      if (current(epoch, mine) && !loadFailed) say(failureText("load shows", error), "error");
      if (current(epoch, mine)) loadFailed = true;
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
    formGeneration += 1;
    if (timer !== null) clearInterval(timer);
    timer = null;
    data = empty();
    listSignature = "";
    listFocusAnnouncementSuppressed = true;
    try { showTable.clear(); }
    finally { listFocusAnnouncementSuppressed = false; }
    closeForm();
    closeEditor();
    nameInput.value = "";
    if (stationNameInput) stationNameInput.value = "";
    searchInput.value = "";
    categorySelect.replaceChildren();
    clockSelect.replaceChildren();
    if (onAirText) onAirText.textContent = "No show is on the air.";
    if (stopButton) stopButton.hidden = true;
    status.textContent = "";
  }
  return {
    refresh,
    clearProtectedData,
    resume() { suspended = false; updatePolling(); },
    destroy() {
      clearProtectedData();
      editions.destroy();
      metadataEditor.destroy();
      window.removeEventListener("hashchange", updatePolling);
      doc.removeEventListener("visibilitychange", updatePolling);
    },
  };
}
