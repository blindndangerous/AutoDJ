// Settings > Shows: themed shows (shows.py and show_runtime.py on the server),
// and the show on air on the Now Playing card.
//
// The panel loads when its subtab is seen, and again when the server says
// the shows changed.  The list, the show on air and the match count are
// plain text, not live regions: each result of a button -- Check and Save
// included -- is said once through #shows-status.  The start and end of a
// show are said once through #sr-status from any tab, except when this
// page's own Start now or Stop caused them (that result is already said).
//
// Focus: Add show and Edit move to the Name field.  Save and Cancel move to
// the show's Edit button (Add show when there is none).  Start now moves to
// the show's Stop button, and Stop to its Start now button.  Delete moves
// to the Edit button of the show that took its place.  Adding a genre keeps
// focus in its field; removing one moves to the next Remove button, or the
// field.  A bad field is named in words, marked aria-invalid and focused.

import { requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";

const JSON_HEADERS = { "Content-Type": "application/json" };
// How long an announcement this page caused itself stays expected.
const OWN_CHANGE_MS = 15000;

// "21:00": the station-zone ISO time as written, as the status page reads it.
export function untilText(iso) {
  const text = String(iso || "");
  return text.length >= 16 && text[13] === ":" ? text.slice(11, 16) : "";
}

// "On air: Remembering the 2000s, until 21:00." (the status page's words).
export function showLine(show) {
  if (!show) return "";
  const name = String(show.name || "").trim();
  if (!name) return "";
  const until = untilText(show.ends_at);
  return `On air: ${name}${until ? `, until ${until}` : ""}.`;
}

// One sentence or more for a Check or Save result.
export function matchText(report) {
  if (!report) return "";
  const parts = [`${report.count_text}.`];
  if (report.fallback_text) parts.push(`${report.fallback_text[0].toUpperCase()}${report.fallback_text.slice(1)}.`);
  if (report.warning) parts.push(`Warning: ${report.warning}.`);
  return parts.join(" ");
}

export function installShowsPanel({ doc = document, request = requestJson, now = () => Date.now() } = {}) {
  const panel = doc.getElementById("spanel-shows");
  const nowLine = doc.getElementById("now-playing-show");
  const srStatus = doc.getElementById("sr-status");
  // The Now Playing line and the start and end announcements work even
  // without the Settings panel.
  let lastKey; // undefined until the first state: a page loaded mid-show says nothing
  let lastName = "";
  let ownChange = null;
  let onAirState = null;

  function noteState(show) {
    onAirState = show || null;
    const line = showLine(onAirState);
    if (nowLine) {
      if (nowLine.textContent !== line) nowLine.textContent = line;
      if (nowLine.hidden === Boolean(line)) nowLine.hidden = !line;
    }
    const key = onAirState ? `${onAirState.id}|${onAirState.started_at}` : "";
    const changed = key !== lastKey;
    if (lastKey !== undefined && changed) {
      const mine = ownChange && ownChange.until > now() && ownChange.id === (onAirState?.id ?? "");
      if (mine) ownChange = null;
      else if (onAirState) {
        const until = untilText(onAirState.ends_at);
        announceStatus(srStatus, `Show ${onAirState.name} started${until ? `, until ${until}` : ""}`);
      } else if (lastName) {
        announceStatus(srStatus, `Show ${lastName} ended`);
      }
    }
    lastKey = key;
    lastName = onAirState?.name || "";
    return changed;
  }

  if (!panel) {
    return { refresh: async () => {}, clear: () => {}, applyState: (state) => noteState(state?.show) };
  }

  const get = (id) => doc.getElementById(`shows-${id}`);
  const status = get("status");
  const list = get("list");
  const form = get("form");
  const formError = get("form-error");
  const nameInput = get("name");
  const addButton = get("add");
  const matchLine = get("match");
  const genreLists = {
    include: { input: get("include-input"), list: get("include-list"), values: [], words: "genres to include", field: "genres_include" },
    exclude: { input: get("exclude-input"), list: get("exclude-list"), values: [], words: "genres to leave out", field: "genres_exclude" },
  };
  let data = emptyData();
  let editing = null; // the ID of the show in the form, or null when adding
  let session = 0;
  let generation = 0;

  function emptyData() {
    return { timezone: "", shows: [], active: null, options: { genres: [], liners: [], liner_folders: [], clockwheels: [] } };
  }

  function say(message, tone = "info") {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 3000, tone });
  }

  function option(value, label) {
    const item = doc.createElement("option");
    item.value = value;
    item.textContent = label;
    return item;
  }

  function setText(element, text) {
    if (element && element.textContent !== text) element.textContent = text;
  }

  function setLabel(button, visible, name) {
    setText(button, visible);
    if (button.getAttribute("aria-label") !== name) button.setAttribute("aria-label", name);
  }

  function showById(id) {
    return data.shows.find((item) => item.id === id) || null;
  }

  function activeId() {
    return onAirState?.id ?? data.active?.id ?? null;
  }

  // ---- The list -------------------------------------------------------

  function renderOnAir() {
    const show = onAirState || data.active;
    let text = show ? showLine(show) : "No show is on air.";
    if (show && data.active && data.active.id === show.id && data.active.count) {
      text += ` ${data.active.count.toLocaleString("en-US")} songs in it.`;
    }
    setText(get("on-air"), text);
  }

  function rowButton(role, onClick) {
    const button = doc.createElement("button");
    button.type = "button";
    button.dataset.role = role;
    button.addEventListener("click", () => {
      const id = button.closest("li")?.dataset.showId;
      const show = id && showById(id);
      if (show) void onClick(show, button);
    });
    return button;
  }

  function fillRow(row, show) {
    const [text, edit, start, stop, remove] = row.children;
    const onAir = activeId() === show.id;
    setText(text, `${show.name}${onAir ? ", on air" : ""}: ${show.summary}`);
    setLabel(edit, "Edit", `Edit ${show.name}`);
    setLabel(start, "Start now", `Start now, ${show.name}, for ${show.default_minutes} minutes`);
    setLabel(stop, "Stop", `Stop ${show.name}`);
    setLabel(remove, "Delete", `Delete ${show.name}`);
    if (stop.hidden === onAir) {
      // A show that ended on its own takes its Stop button away; focus
      // there goes to Start now instead of the page top.
      const hadFocus = doc.activeElement === stop;
      stop.hidden = !onAir;
      if (hadFocus && !onAir) start.focus();
    }
  }

  function newRow(show) {
    const row = doc.createElement("li");
    row.dataset.showId = show.id;
    row.append(doc.createElement("span"));
    for (const [role, onClick] of [["edit", openForm], ["start", startShow], ["stop", stopShow], ["delete", deleteShow]]) {
      const button = rowButton(role, onClick);
      if (role === "stop") button.hidden = true;
      // Spaced like the other settings lists.
      row.append(" ", button);
    }
    fillRow(row, show);
    return row;
  }

  // Rows that still exist are kept, so focus on their buttons survives a refresh.
  function renderShows() {
    const ids = new Set(data.shows.map((item) => item.id));
    for (const row of [...list.children]) {
      if (!ids.has(row.dataset.showId)) row.remove();
    }
    data.shows.forEach((show, index) => {
      const at = list.children[index];
      if (at?.dataset.showId === show.id) {
        fillRow(at, show);
        return;
      }
      const existing = [...list.children].find((row) => row.dataset.showId === show.id);
      if (existing) {
        list.insertBefore(existing, at || null);
        fillRow(existing, show);
      } else {
        list.insertBefore(newRow(show), at || null);
      }
    });
    if (!data.shows.length) {
      const empty = doc.createElement("li");
      empty.className = "no-results";
      empty.textContent = "No shows yet.";
      list.replaceChildren(empty);
    }
  }

  function render() {
    renderOnAir();
    renderShows();
    const datalist = get("genre-options");
    if (datalist) datalist.replaceChildren(...data.options.genres.map((genre) => option(genre, genre)));
  }

  async function load() {
    const mine = ++generation;
    const current = session;
    const reply = await request("/api/shows");
    if (mine !== generation || current !== session) return false;
    data = { ...emptyData(), ...reply, options: { ...emptyData().options, ...(reply.options || {}) } };
    render();
    return true;
  }

  function refresh() {
    if (!panel.open) return Promise.resolve();
    const current = session;
    return load().catch((error) => {
      if (current === session) say(`Could not load the shows: ${error.message}`, "error");
    });
  }

  function focusRowButton(id, role) {
    const row = [...list.children].find((item) => item.dataset.showId === id);
    const button = row?.querySelector(`[data-role="${role}"]`);
    (button && !button.hidden ? button : addButton).focus();
  }

  function expectOwnChange(id) {
    ownChange = { id, until: now() + OWN_CHANGE_MS };
  }

  // ---- Row buttons ----------------------------------------------------

  async function startShow(show) {
    const current = session;
    expectOwnChange(show.id);
    try {
      const active = await request(`/api/shows/${encodeURIComponent(show.id)}/start`, {
        method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ minutes: show.default_minutes }),
      });
      if (current !== session) return;
      noteState(active);
      await load().catch(() => false);
      if (current !== session) return;
      focusRowButton(show.id, "stop");
      const until = untilText(active.ends_at);
      say(`Show ${show.name} started${until ? `, until ${until}` : ""}`);
    } catch (error) {
      ownChange = null;
      if (current === session) say(`Could not start ${show.name}: ${error.message}`, "error");
    }
  }

  async function stopShow(show) {
    const current = session;
    expectOwnChange("");
    try {
      await request("/api/shows/stop", { method: "POST" });
      if (current !== session) return;
      noteState(null);
      await load().catch(() => false);
      if (current !== session) return;
      focusRowButton(show.id, "start");
      say(`Show ${show.name} stopped`);
    } catch (error) {
      ownChange = null;
      if (current === session) say(`Could not stop ${show.name}: ${error.message}`, "error");
    }
  }

  // The delete runs while the confirmation is open, so the list is updated
  // when it closes and focus goes straight to the next show (confirmAction).
  async function deleteShow(show, button) {
    const current = session;
    const index = data.shows.findIndex((item) => item.id === show.id);
    const onAir = activeId() === show.id;
    let result = null;
    const confirmed = await confirmAction(doc, {
      title: `Delete show ${show.name}?`,
      message: onAir
        ? "The show is on air: it ends now, then it is removed. Schedule events that start it will fail."
        : "The show is removed. Schedule events that start it will fail.",
      confirmLabel: "Delete show",
      onConfirm: async () => {
        try {
          if (onAir) expectOwnChange("");
          await request(`/api/shows/${encodeURIComponent(show.id)}`, { method: "DELETE" });
          if (current !== session) return null;
          if (onAir) noteState(null);
          if (editing === show.id) closeForm();
          await load();
          if (current !== session) return null;
          result = [`Show ${show.name} deleted${onAir ? "; it has ended" : ""}`];
          const edits = list.querySelectorAll('[data-role="edit"]');
          return edits[Math.min(index, edits.length - 1)] || addButton;
        } catch (error) {
          ownChange = null;
          if (current !== session) return null;
          result = [`Could not delete ${show.name}: ${error.message}`, "error"];
          return button;
        }
      },
    });
    if (!confirmed) {
      button.focus();
      return;
    }
    if (result) say(...result);
  }

  // ---- Genres ---------------------------------------------------------

  function renderGenres(kind) {
    const group = genreLists[kind];
    const items = group.values.map((genre) => {
      const item = doc.createElement("li");
      item.dataset.genre = genre;
      const remove = doc.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${genre} from ${group.words}`);
      remove.addEventListener("click", () => removeGenre(kind, genre));
      item.append(genre, " ", remove);
      return item;
    });
    group.list.replaceChildren(...items);
    group.list.hidden = !items.length;
  }

  function addGenre(kind) {
    const group = genreLists[kind];
    const other = genreLists[kind === "include" ? "exclude" : "include"];
    const typed = group.input.value.trim();
    if (!typed) {
      showError(group.field, `Enter a genre to add to ${group.words}`);
      return;
    }
    // The library's spelling, when the typed genre is one of its genres.
    const genre = data.options.genres.find((item) => item.toLowerCase() === typed.toLowerCase()) || typed;
    const folded = genre.toLowerCase();
    if (group.values.some((item) => item.toLowerCase() === folded)) {
      say(`${genre} is already in ${group.words}`);
      return;
    }
    if (other.values.some((item) => item.toLowerCase() === folded)) {
      showError(group.field, `${genre} is in ${other.words}; remove it there first`);
      return;
    }
    clearError();
    group.values.push(genre);
    renderGenres(kind);
    group.input.value = "";
    group.input.focus();
    say(`${genre} added to ${group.words}`);
  }

  function removeGenre(kind, genre) {
    const group = genreLists[kind];
    const index = group.values.indexOf(genre);
    if (index < 0) return;
    group.values.splice(index, 1);
    renderGenres(kind);
    const buttons = group.list.querySelectorAll("button");
    (buttons[Math.min(index, buttons.length - 1)] || group.input).focus();
    say(`${genre} removed from ${group.words}`);
  }

  // ---- The form -------------------------------------------------------

  function fillSelect(select, none, choices, value, missing) {
    select.replaceChildren(option("", none), ...choices.map(([key, label]) => option(key, label)));
    if (value && !choices.some(([key]) => key === value)) select.append(option(value, missing(value)));
    select.value = value || "";
  }

  function openForm(show = null) {
    editing = show?.id ?? null;
    clearError();
    const options = data.options;
    get("form-legend").textContent = show ? `Edit ${show.name}` : "Add show";
    nameInput.value = show?.name ?? "";
    get("year-from").value = show?.year_from ?? "";
    get("year-to").value = show?.year_to ?? "";
    genreLists.include.values = [...(show?.genres_include ?? [])];
    genreLists.exclude.values = [...(show?.genres_exclude ?? [])];
    for (const kind of ["include", "exclude"]) {
      genreLists[kind].input.value = "";
      renderGenres(kind);
    }
    get("query").value = show?.query ?? "";
    fillSelect(get("clockwheel"), "None, keep the station's song picking",
      options.clockwheels.map((name) => [name, name]), show?.clockwheel,
      (name) => `${name} (not found)`);
    const liners = options.liners.map((item) => [item.id, item.name]);
    const gone = () => "A liner that is no longer registered";
    fillSelect(get("intro"), "None", liners, show?.intro, gone);
    fillSelect(get("outro"), "None", liners, show?.outro, gone);
    fillSelect(get("liner-source"), "None, use the station's liners",
      options.liner_folders.map((item) => [item.id, item.path]), show?.liner_folder?.source,
      () => "A folder that is no longer registered");
    get("liner-folder").value = show?.liner_folder?.folder ?? "";
    get("start").value = show?.start ?? "soft";
    get("end").value = show?.end ?? "soft";
    get("minutes").value = String(show?.default_minutes ?? 60);
    setText(matchLine, "Not checked yet.");
    form.hidden = false;
    nameInput.focus();
  }

  function closeForm() {
    form.hidden = true;
    editing = null;
    clearError();
  }

  function clearError() {
    formError.hidden = true;
    formError.textContent = "";
    for (const element of form.querySelectorAll("[aria-invalid]")) element.removeAttribute("aria-invalid");
  }

  const FIELD_CONTROLS = {
    name: "name", year_from: "year-from", year_to: "year-to",
    genres_include: "include-input", genres_exclude: "exclude-input", query: "query",
    clockwheel: "clockwheel", intro: "intro", outro: "outro", liner_folder: "liner-source",
    start: "start", end: "end", default_minutes: "minutes", minutes: "minutes",
  };

  // Say what is wrong, keep the words on screen, and put focus on the field.
  function showError(field, message) {
    clearError();
    formError.textContent = message;
    formError.hidden = false;
    const control = FIELD_CONTROLS[field] && get(FIELD_CONTROLS[field]);
    if (control) control.setAttribute("aria-invalid", "true");
    say(message, "error");
    control?.focus();
    return Boolean(control);
  }

  function wholeNumber(id) {
    const text = get(id).value.trim();
    if (!text) return null;
    const value = Number(text);
    return Number.isInteger(value) ? value : NaN;
  }

  // The checks the server makes too, in the order of the fields.
  function filterProblem() {
    const from = wholeNumber("year-from");
    const to = wholeNumber("year-to");
    for (const [field, value, label] of [["year_from", from, "First released from year"], ["year_to", to, "First released to year"]]) {
      if (value !== null && (Number.isNaN(value) || value < 1 || value > 9999)) {
        return [field, `${label} must be a year from 1 to 9999`];
      }
    }
    if (from !== null && to !== null && from > to) return ["year_to", "The year range ends before it starts"];
    const minutes = wholeNumber("minutes");
    if (minutes === null || Number.isNaN(minutes) || minutes < 1 || minutes > 1440) {
      return ["default_minutes", "Length when started now must be a whole number of minutes from 1 to 1440"];
    }
    return null;
  }

  function firstProblem() {
    if (!nameInput.value.trim()) return ["name", "Enter a name"];
    const problem = filterProblem();
    if (problem) return problem;
    if (get("liner-folder").value.trim() && !get("liner-source").value) {
      return ["liner_folder", "Choose the show liner folder the folder is inside"];
    }
    return null;
  }

  function filterBody() {
    return {
      year_from: wholeNumber("year-from"),
      year_to: wholeNumber("year-to"),
      genres_include: [...genreLists.include.values],
      genres_exclude: [...genreLists.exclude.values],
      query: get("query").value.trim(),
    };
  }

  function payload() {
    const source = get("liner-source").value;
    return {
      name: nameInput.value.trim(),
      ...filterBody(),
      clockwheel: get("clockwheel").value || null,
      intro: get("intro").value || null,
      outro: get("outro").value || null,
      liner_folder: source ? { source, folder: get("liner-folder").value.trim() } : null,
      start: get("start").value,
      end: get("end").value,
      default_minutes: wholeNumber("minutes"),
    };
  }

  function rejected(error, prefix) {
    if (!error.field || !showError(error.field, error.message)) say(`${prefix}: ${error.message}`, "error");
  }

  async function check() {
    const problem = filterProblem();
    if (problem) {
      showError(...problem);
      return;
    }
    clearError();
    const current = session;
    try {
      const report = await request("/api/shows/check", {
        method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ ...filterBody(), minutes: wholeNumber("minutes") }),
      });
      if (current !== session) return;
      const text = matchText(report);
      setText(matchLine, text);
      say(text, report.warning ? "error" : "info");
    } catch (error) {
      if (current === session) rejected(error, "Could not check");
    }
  }

  async function save(submitEvent) {
    submitEvent.preventDefault();
    const problem = firstProblem();
    if (problem) {
      showError(...problem);
      return;
    }
    clearError();
    const current = session;
    const id = editing;
    let saved;
    try {
      saved = await request(id ? `/api/shows/${encodeURIComponent(id)}` : "/api/shows", {
        method: id ? "PUT" : "POST", headers: JSON_HEADERS, body: JSON.stringify(payload()),
      });
    } catch (error) {
      if (current !== session) return;
      if (error.status === 404) {
        closeForm();
        addButton.focus();
        say(`Could not save: ${error.message}`, "error");
      } else {
        rejected(error, "Could not save");
      }
      return;
    }
    if (current !== session) return;
    const text = matchText(saved.check);
    setText(matchLine, text);
    // The list is updated before the form closes, so focus goes from Save
    // straight to the show's Edit button.
    await load().catch(() => false);
    if (current !== session) return;
    closeForm();
    focusRowButton(saved.show.id, "edit");
    say(`Show ${saved.show.name} saved. ${text}`, saved.check?.warning ? "error" : "info");
  }

  addButton.addEventListener("click", () => openForm());
  get("cancel").addEventListener("click", () => {
    const id = editing;
    closeForm();
    if (id) focusRowButton(id, "edit");
    else addButton.focus();
  });
  get("check").addEventListener("click", () => void check());
  for (const kind of ["include", "exclude"]) {
    get(`${kind}-add`).addEventListener("click", () => addGenre(kind));
    // Enter in a genre field adds it rather than saving the whole form.
    genreLists[kind].input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addGenre(kind);
    });
  }
  // A field the user changes is no longer marked as the bad one.
  form.addEventListener("input", (event) => {
    if (event.target.getAttribute?.("aria-invalid")) event.target.removeAttribute("aria-invalid");
  });
  form.addEventListener("submit", (event) => void save(event));
  panel.addEventListener("toggle", () => void refresh());

  return {
    refresh,
    applyState(state) {
      if (noteState(state?.show)) {
        renderOnAir();
        for (const row of list.children) {
          const show = row.dataset.showId && showById(row.dataset.showId);
          if (show) fillRow(row, show);
        }
      }
    },
    // Signed out: forget everything this session showed.
    clear() {
      session += 1;
      generation += 1;
      data = emptyData();
      onAirState = null;
      lastKey = undefined;
      lastName = "";
      ownChange = null;
      closeForm();
      status.textContent = "";
      list.replaceChildren();
      setText(get("on-air"), "No show is on air.");
      if (nowLine) {
        nowLine.textContent = "";
        nowLine.hidden = true;
      }
    },
  };
}
