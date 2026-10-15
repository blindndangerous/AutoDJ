// Shows (#106): a list of songs prepared ahead that a schedule event airs.
//
// The server keeps them in shows.json and generates a list with a dry run of
// the station's picker.  This module lists the shows (plain text), adds,
// edits and deletes them through one form, and opens one show's list to
// generate it again, move songs up and down, remove them and add songs from
// a library search.  Each change to the list is saved at once.
//
// Speech: each result of a button is said once through #shows-status; the
// lists, the totals and the on-air line are never live regions, so polling
// says nothing.  Focus: Add and Edit move it to Name; Save to the saved
// show's Open list button; Cancel back to the button that opened the form;
// Open list to the list's heading; Move up and Move down keep it on the
// same button of the moved song; Remove moves it to the next song's
// Remove, or the previous song's when it was the last (the search box when
// none is left); Close list to the show's Open list button; Delete to the
// next show (or Add show).  A field the server names gets aria-invalid, the
// error text as part of its description, and focus; an error that names no
// field is only said.  While the list is being generated, Move up, Move
// down, Remove and Add stay in the tab order but are marked unavailable and
// say why.  A refresh that fails is said once, until one succeeds again.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";
import { fmtTime, placeRows, selectOnFocus } from "./dom-helpers.js";

const JSON_HEADERS = { "Content-Type": "application/json" };
const POLL_MS = 15000;
const SEARCH_LIMIT = 20;
const FIELD_IDS = {
  name: "shows-name", source: "shows-source-category", category: "shows-category",
  clockwheel: "shows-clockwheel", target_minutes: "shows-target",
};

const number = (n) => Number(n || 0).toLocaleString("en-US");
const plural = (n, one, many) => `${number(n)} ${n === 1 ? one : many}`;

// What one saved show is, for its list item.
export function describeShow(show) {
  const sentences = [`${show.name}: ${plural(show.count, "song", "songs")}, ${show.count ? show.total_text : "no list yet"}.`];
  sentences.push(`From ${show.source_text}, target ${plural(show.target_minutes, "minute", "minutes")}.`);
  if (show.missing) sentences.push(`${plural(show.missing, "song is", "songs are")} no longer in the library.`);
  const events = show.events || [];
  sentences.push(events.length ? `Played by ${events.join(", ")}.` : "No schedule event plays it.");
  return sentences.join(" ");
}

// The show on the air, in words.
export function describeOnAir(onAir) {
  if (!onAir) return "No show is on the air.";
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
  const nameInput = el("shows-name");
  const categorySelect = el("shows-category");
  const clockSelect = el("shows-clockwheel");
  const targetInput = el("shows-target");
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
  let opener = null; // the button that opened the form
  let open = null; // the show whose list is open, with its songs
  let songs = []; // the open list's songs, as shown
  let listSignature = "";
  let songsSignature = "";
  let saving = Promise.resolve();
  let generating = false;
  let loadFailed = false;
  let timer = null;
  let generation = 0;
  let suspended = false;

  const current = (epoch, mine) => !suspended && generation === mine && isAuthenticatedRequestCurrent(epoch);
  const say = (message, error = false) => announceStatus(status, message, { force: true, tone: error ? "error" : "info" });
  const send = (url, method, body) => request(url, body === undefined ? { method } : { method, headers: JSON_HEADERS, body: JSON.stringify(body) });
  const source = () => (el("shows-source-clockwheel")?.checked ? "clockwheel" : "category");
  const byId = (id) => data.shows.find((show) => show.id === id);
  const url = (id, tail = "") => `/api/shows/${encodeURIComponent(id)}${tail}`;

  // A button whose visible word starts its name; the hidden words say which.
  function namedButton(action, title, which, extra = {}) {
    const button = doc.createElement("button");
    button.type = "button";
    button.dataset.action = action;
    for (const [key, value] of Object.entries(extra)) button.dataset[key] = value;
    button.textContent = title;
    button.setAttribute("aria-label", `${title} ${which}`);
    return button;
  }

  // ---- The shows -------------------------------------------------------
  function renderList() {
    const signature = JSON.stringify(data.shows);
    if (signature === listSignature) return;
    listSignature = signature;
    const rows = [...list.querySelectorAll(":scope > li[data-id]")];
    if (rows.length && rows.length === data.shows.length
        && rows.every((row, i) => row.dataset.id === data.shows[i].id && row.dataset.name === data.shows[i].name)) {
      // The same shows in the same order: change only the words.
      rows.forEach((row, i) => {
        const text = describeShow(data.shows[i]);
        const span = row.firstElementChild;
        if (span && span.textContent !== text) span.textContent = text;
      });
      return;
    }
    const focused = list.contains(doc.activeElement) ? doc.activeElement.dataset : null;
    if (!data.shows.length) {
      const none = doc.createElement("li");
      none.textContent = "No shows yet. Press Add show to plan one.";
      list.replaceChildren(none);
      return;
    }
    list.replaceChildren(...data.shows.map((show) => {
      const item = doc.createElement("li");
      item.dataset.id = show.id;
      item.dataset.name = show.name;
      const text = doc.createElement("span");
      text.textContent = describeShow(show);
      item.append(
        text, " ",
        namedButton("open", "Open list", show.name, { id: show.id }), " ",
        namedButton("edit", "Edit", show.name, { id: show.id }), " ",
        namedButton("delete", "Delete", show.name, { id: show.id }),
      );
      return item;
    }));
    if (focused?.id) rowFor(focused.id, focused.action)?.focus();
  }

  function rowFor(id, action = "open") {
    return [...list.querySelectorAll("button[data-action]")]
      .find((button) => button.dataset.id === id && button.dataset.action === action) || null;
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
    for (const id of Object.values(FIELD_IDS)) unmark(el(id));
    if (errorText) { errorText.textContent = ""; errorText.hidden = true; }
  }

  function showError(field, message) {
    if (field === "category") el("shows-source-category").checked = true;
    if (field === "clockwheel") el("shows-source-clockwheel").checked = true;
    showSource();
    const target = el(FIELD_IDS[field] || FIELD_IDS.name);
    target?.setAttribute("aria-invalid", "true");
    linkError(target, true);
    if (errorText) { errorText.textContent = message; errorText.hidden = false; }
    // Focus moving to the field reads the linked error, so say it only otherwise.
    const before = target?.ownerDocument.activeElement;
    target?.focus();
    if (!target || before === target || target.ownerDocument.activeElement !== target) say(message, true);
  }

  function body() {
    const kind = source();
    return {
      name: nameInput.value.trim(),
      source: kind,
      category: kind === "category" ? categorySelect.value : "",
      clockwheel: kind === "clockwheel" ? clockSelect.value : "",
      target_minutes: Number(targetInput.value.trim()),
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
    return true;
  }

  function openForm(show, from) {
    editing = show ? show.id : null;
    opener = from;
    clearErrors();
    nameInput.value = show?.name || "";
    targetInput.value = String(show?.target_minutes || 60);
    el(show?.source === "clockwheel" ? "shows-source-clockwheel" : "shows-source-category").checked = true;
    renderSources(show);
    showSource();
    if (legend) legend.textContent = show ? `Edit show ${show.name}` : "Add show";
    form.hidden = false;
    nameInput.focus();
  }

  function closeForm() {
    form.hidden = true;
    editing = null;
    clearErrors();
  }

  async function save(event) {
    event?.preventDefault();
    clearErrors();
    const value = body();
    if (!valid(value)) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const show = editing
        ? await send(url(editing), "PUT", value)
        : await send("/api/shows", "POST", value);
      if (!current(epoch, mine)) return;
      const added = !editing;
      // The list is drawn again while focus is still on Save show, and focus
      // moves to the saved show before the form is hidden.  Hiding the form
      // first dropped focus to the page for the reload, and NVDA then read
      // the whole Shows card before the Open list button.
      try { await load(); } catch (_) { /* the next refresh draws the list */ }
      if (!current(epoch, mine)) return;
      if (open?.id === show.id) applyOpen(show);
      (rowFor(show.id) || addButton).focus();
      closeForm();
      say(added ? `Saved ${show.name}. Press Open list, then Generate list, to pick its songs.` : `Saved ${show.name}.`);
    } catch (error) {
      if (!current(epoch, mine)) return;
      if (FIELD_IDS[error.field]) showError(error.field, error.message);
      else say(error.message, true);
    }
  }

  async function remove(id) {
    const show = byId(id);
    if (!show) return;
    const index = data.shows.indexOf(show);
    const events = show.events || [];
    let done = false;
    await confirmAction(doc, {
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
          await load();
          done = true;
          const next = data.shows[Math.min(index, data.shows.length - 1)];
          return (next && rowFor(next.id)) || addButton;
        } catch (error) {
          if (current(epoch, mine)) say(`Could not delete ${show.name}: ${error.message}`, true);
          return null;
        }
      },
    });
    if (done) say(`Deleted ${show.name}.`);
  }

  // ---- The open list ---------------------------------------------------
  // A song's row is built once and then changed in place: a list drawn
  // again from scratch destroyed the focused button, and NVDA read a cut-off
  // line of the list after Move up, Move down, Remove and Add (#328).
  function songRow() {
    const item = doc.createElement("li");
    const row = doc.createElement("div");
    row.className = "show-row";
    row.append(
      doc.createElement("span"),
      namedButton("up", "Move up", ""),
      namedButton("down", "Move down", ""),
      namedButton("remove", "Remove", ""),
    );
    item.append(row);
    return item;
  }

  // Changes only what differs, so a row that stays says nothing new.
  function fillSongRow(item, song, index) {
    if (item.dataset.index !== String(index)) item.dataset.index = String(index);
    const [text, ...buttons] = item.firstElementChild.children;
    const className = song.missing ? "show-song show-missing" : "show-song";
    if (text.className !== className) text.className = className;
    let words = song.missing
      ? `${song.title}, not in the library; skipped on the air`
      : `${song.title}, ${song.length_text}`;
    const others = (repeated.get(song.path) || []).filter((place) => place !== index + 1);
    if (others.length) words += `; warning: also number ${others.join(" and ")} in the list`;
    if (text.textContent !== words) text.textContent = words;
    for (const button of buttons) {
      const name = `${button.textContent} ${song.title}`;
      if (button.getAttribute("aria-label") !== name) button.setAttribute("aria-label", name);
      if (button.dataset.index !== String(index)) button.dataset.index = String(index);
      // At the ends, and while the list is generated, the buttons stay in the
      // tab order and say why they do nothing.
      const action = button.dataset.action;
      const off = generating || (action === "up" && index === 0)
        || (action === "down" && index === songs.length - 1);
      if (off && button.getAttribute("aria-disabled") !== "true") button.setAttribute("aria-disabled", "true");
      else if (!off && button.hasAttribute("aria-disabled")) button.removeAttribute("aria-disabled");
    }
  }

  let repeated = new Map();

  // Focus goes to its button before a row goes, so it never sits on the page.
  function renderSongs(focus = null) {
    const signature = JSON.stringify([songs, generating]);
    if (signature === songsSignature && !focus) return;
    songsSignature = signature;
    repeated = repeatedPlaces(songs);
    if (!songs.length) {
      let none = songsList.querySelector(":scope > li.no-results");
      if (!none) {
        none = doc.createElement("li");
        none.className = "no-results";
        none.textContent = "No songs yet. Press Generate list, or add songs below.";
      }
      if (focus) searchInput.focus();
      placeRows(songsList, [none]);
      return;
    }
    // The rows already drawn, by song and which copy of it.
    const drawn = new Map();
    const seen = new Map();
    const keyOf = (path) => {
      const copy = seen.get(path) || 0;
      seen.set(path, copy + 1);
      return JSON.stringify([path, copy]);
    };
    for (const item of songsList.querySelectorAll(":scope > li[data-path]")) {
      drawn.set(keyOf(item.dataset.path), item);
    }
    seen.clear();
    const rows = songs.map((song, index) => {
      const path = String(song.path ?? "");
      const item = drawn.get(keyOf(path)) || songRow();
      if (item.dataset.path !== path) item.dataset.path = path;
      fillSongRow(item, song, index);
      return item;
    });
    const target = focus
      ? rows[Number(focus.index)]?.querySelector(`button[data-action="${focus.action}"]`) || null
      : null;
    if (target?.isConnected) target.focus();
    placeRows(songsList, rows);
    if (focus && doc.activeElement !== (target || searchInput)) (target || searchInput).focus();
  }

  // Draws the list again, keeping focus on the same button when it was in it.
  function redrawSongs() {
    const focused = doc.activeElement;
    const place = songsList.contains(focused) && focused.dataset?.action
      ? { action: focused.dataset.action, index: focused.dataset.index } : null;
    songsSignature = "";
    renderSongs(place);
    for (const add of results.querySelectorAll("button[data-action='add']")) {
      if (generating) add.setAttribute("aria-disabled", "true");
      else add.removeAttribute("aria-disabled");
    }
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
    songs = show.songs || [];
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
      songsSignature = "";
      results.replaceChildren();
      applyOpen(show);
      editor.hidden = false;
      el("shows-editor-heading")?.focus();
      say(`Opened ${show.name}. ${summaryText(show)}`);
    } catch (error) {
      if (current(epoch, mine)) say(`Could not open the show: ${error.message}`, true);
    }
  }

  function closeEditor() {
    editor.hidden = true;
    open = null;
    songs = [];
    songsSignature = "";
    results.replaceChildren();
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
        say(`Could not save the list: ${error.message} The saved list is shown again.`, true);
        try {
          const show = await request(url(id));
          if (current(epoch, mine) && open?.id === id) {
            const focused = doc.activeElement;
            const place = songsList.contains(focused) ? { action: focused.dataset.action, index: focused.dataset.index } : null;
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

  function removeSong(index) {
    if (refusedWhileGenerating()) return;
    const gone = songs[index];
    if (!gone) return;
    songs = songs.filter((_song, i) => i !== index);
    const next = Math.min(index, songs.length - 1);
    renderSongs(next >= 0 ? { action: "remove", index: String(next) } : { action: "none", index: "-1" });
    renderSummary();
    void saveSongs(`Removed ${gone.title}.`);
  }

  async function generate() {
    if (!open || generating) {
      if (generating) say("The list is being generated; one moment.");
      return;
    }
    const button = el("shows-generate");
    const startInput = el("shows-start");
    if (startInput?.validity?.badInput) {
      startInput.setAttribute("aria-invalid", "true");
      startInput.focus();
      say("Pick as if at is not a whole date and time. Complete it or clear it.", true);
      return;
    }
    startInput?.removeAttribute("aria-invalid");
    const id = open.id;
    const value = startInput?.value ? { start: startInput.value } : {};
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    // generating holds off another press; no aria-disabled, or NVDA says
    // "unavailable" on the pressed button (#331).  aria-busy says it is busy.
    generating = true;
    button?.classList.add("is-pending");
    button?.setAttribute("aria-busy", "true");
    redrawSongs();
    say(`Generating the list for ${open.name}.`);
    try {
      await saving;
      const show = await send(url(id, "/generate"), "POST", value);
      if (!current(epoch, mine) || open?.id !== id) return;
      generating = false;
      applyOpen(show);
      void refresh();
      say(`Generated ${summaryText(show)}${show.note ? ` ${show.note}` : ""}`);
    } catch (error) {
      if (!current(epoch, mine)) return;
      if (error.field === "start" && startInput) {
        startInput.setAttribute("aria-invalid", "true");
        startInput.focus();
      }
      say(`Could not generate the list: ${error.message}`, true);
    } finally {
      generating = false;
      button?.classList.remove("is-pending");
      button?.removeAttribute("aria-busy");
      if (open?.id === id) redrawSongs();
    }
  }

  async function search() {
    const query = searchInput.value.trim();
    if (query.length < 2) {
      searchInput.focus();
      say("Type at least two letters to search.", true);
      return;
    }
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const answer = await request(`/api/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}`);
      if (!current(epoch, mine)) return;
      const found = answer.results || [];
      results.replaceChildren(...found.map((song, index) => {
        const item = doc.createElement("li");
        const row = doc.createElement("div");
        row.className = "show-row";
        const text = doc.createElement("span");
        text.className = "show-song";
        const title = song.artist ? `${song.artist}, ${song.title || song.display_name}` : (song.title || song.display_name);
        const seconds = Math.max(0, Math.round(Number(song.length) || 0));
        const shown = fmtTime(seconds);
        text.textContent = `${title}, ${shown}`;
        const add = namedButton("add", "Add", title, { index: String(index) });
        if (generating) add.setAttribute("aria-disabled", "true");
        add.dataset.path = song.path;
        add.dataset.title = title;
        add.dataset.seconds = String(seconds);
        add.dataset.shown = shown;
        row.append(text, add);
        item.append(row);
        return item;
      }));
      say(found.length ? `${plural(found.length, "song", "songs")} found${found.length === SEARCH_LIMIT ? `; the first ${SEARCH_LIMIT} are listed` : ""}.` : `No songs match ${query}.`);
    } catch (error) {
      if (current(epoch, mine)) say(`Could not search: ${error.message}`, true);
    }
  }

  function addSong(button) {
    if (!open || refusedWhileGenerating()) return;
    const max = data.limits?.max_songs || 500;
    if (songs.length >= max) { say(`A show holds at most ${max} songs.`, true); return; }
    const seconds = Number(button.dataset.seconds) || 0;
    songs = [...songs, {
      path: button.dataset.path, title: button.dataset.title, missing: false, seconds,
      length_text: button.dataset.shown,
    }];
    renderSongs();
    renderSummary();
    void saveSongs(`Added ${button.dataset.title} as number ${songs.length}.`);
  }

  async function startNow() {
    if (!open) return;
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      await saving;
      const answer = await send(url(open.id, "/start"), "POST");
      if (!current(epoch, mine)) return;
      await load();
      say(answer.message || "The show started.");
    } catch (error) {
      if (current(epoch, mine)) say(`The show did not start: ${error.message}`, true);
    }
  }

  async function stopShow() {
    const epoch = captureAuthenticatedRequestEpoch(), mine = generation;
    try {
      const wasResuming = Boolean(data.on_air?.pending_resume);
      const answer = await send("/api/shows/on-air", "DELETE");
      if (!current(epoch, mine)) return;
      // Focus moves to Add show only if Stop show is hidden (renderOnAir).
      await load();
      if (wasResuming) say(`${answer.stopped} will not resume.`);
      else if (answer.done === false) {
        say(`Stopping ${answer.stopped}: some of its songs could not be taken out of the queue yet. AutoDJ tries again at the next song.`);
      } else say(`Stopped ${answer.stopped}. Its waiting songs left the queue; normal programming resumes.`);
    } catch (error) {
      if (current(epoch, mine)) say(`Could not stop the show: ${error.message}`, true);
    }
  }

  // ---- Events ----------------------------------------------------------
  list.addEventListener("click", (event) => {
    const button = event.target.closest?.("button[data-action]");
    if (!button) return;
    const show = byId(button.dataset.id);
    if (!show) return;
    if (button.dataset.action === "open") void openEditor(show.id);
    else if (button.dataset.action === "edit") openForm(show, button);
    else if (button.dataset.action === "delete") void remove(show.id);
  });
  songsList.addEventListener("click", (event) => {
    const button = event.target.closest?.("button[data-action]");
    if (!button) return;
    const index = Number(button.dataset.index);
    if (button.dataset.action === "up") move(index, -1);
    else if (button.dataset.action === "down") move(index, 1);
    else if (button.dataset.action === "remove") removeSong(index);
  });
  results.addEventListener("click", (event) => {
    const button = event.target.closest?.("button[data-action='add']");
    if (button) addSong(button);
  });
  addButton.addEventListener("click", () => openForm(null, addButton));
  form.addEventListener("submit", (event) => void save(event));
  el("shows-cancel")?.addEventListener("click", () => {
    const back = editing ? rowFor(editing, "edit") : null;
    closeForm();
    (back || (opener?.isConnected ? opener : addButton)).focus();
    say("Cancelled. Nothing was saved.");
  });
  for (const id of ["shows-source-category", "shows-source-clockwheel"]) el(id)?.addEventListener("change", showSource);
  for (const field of [nameInput, categorySelect, clockSelect, targetInput]) {
    field.addEventListener("input", () => unmark(field));
  }
  el("shows-start")?.addEventListener("input", (event) => event.target.removeAttribute("aria-invalid"));
  el("shows-generate")?.addEventListener("click", () => void generate());
  el("shows-search-run")?.addEventListener("click", () => void search());
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); void search(); }
  });
  selectOnFocus(searchInput);
  el("shows-play-now")?.addEventListener("click", () => void startNow());
  stopButton?.addEventListener("click", () => void stopShow());
  el("shows-close")?.addEventListener("click", () => {
    const id = open?.id;
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
      if (current(epoch, mine) && !loadFailed) say(`Could not load shows: ${error.message}`, true);
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
    if (timer !== null) clearInterval(timer);
    timer = null;
    data = empty();
    listSignature = "";
    list.replaceChildren();
    closeForm();
    closeEditor();
    nameInput.value = "";
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
      window.removeEventListener("hashchange", updatePolling);
      doc.removeEventListener("visibilitychange", updatePolling);
    },
  };
}
