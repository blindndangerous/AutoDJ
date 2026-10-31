import { createEditorDialog, showEditorError } from "./editor-dialog.js";
// Settings > Station > Schedule: timed events (scheduler.py on the server).
//
// The server lists the actions and their settings, so an action that a
// later feature registers appears here without changes to this file.  The
// panel loads when its subtab is seen, and again when the server says the
// schedule changed or an event ran. The collections use shared native
// tables; each result of a button is said once through #schedule-status.
//
// Focus: Add event and Edit move to the Name field.  Save and Cancel move
// to the event's Edit cell (Add event when there is none). Delete moves
// to the Edit cell of the event that took its place.  A bad field is
// named in words, marked aria-invalid, described by the error text and
// focused.  Rows are updated in place, so a refresh never takes focus away
// from a row's cell or open menu.

import { requestJson } from "./api-client.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { focusAfterDialog } from "./dom-helpers.js";
import { createCollectionTable } from "./collection-table.js";
import { announceStatus } from "./live-region.js";
import { clockText, relativeTimeText } from "./time-format.js";

const JSON_HEADERS = { "Content-Type": "application/json" };

// Which repeat choices use a time, a date and so on.
const USES_TIME = new Set(["once", "daily", "weekdays", "days"]);
// The help under Repeat dates, for each choice; the date fields read it too.
// The Repeat rule already sets the days, so the dates repeat yearly at most.
const DATES_REPEAT_HELP = {
  none: "The event runs only between these dates, both included.",
  yearly: "Repeats on these dates every year, ignoring the year; a range may run past New Year. "
    + "29 February counts as 28 February in other years.",
};

export function installSchedulerPanel({ doc = document, request = requestJson, now = () => Date.now(), offAir = false } = {}) {
  const panel = doc.getElementById("spanel-schedule");
  if (!panel) return { refresh: async () => {}, clear: () => {}, setOffAir() {} };
  const get = (id) => doc.getElementById(`schedule-${id}`);
  const status = get("status");
  const form = get("form");
  const formError = get("form-error");
  const nameInput = get("name");
  const actionSelect = get("action");
  const paramsBox = get("params");
  const repeatSelect = get("repeat");
  const addButton = get("add");
  let listFocusAnnouncementSuppressed = false;
  const dayBoxes = [...form.querySelectorAll('#schedule-days input[type="checkbox"]')];
  let data = emptyData();
  let editing = null; // the ID of the event in the form, or null when adding
  let session = 0;
  let generation = 0;
  let stationStopped = offAir;

  const editor = createEditorDialog({ form, heading: get("form-legend"), cancel: get("cancel"), fallback: addButton,
    save, onCancel: closeForm });

  function emptyData() {
    return { timezone: "", actions: [], events: [], upcoming: [], runs: [] };
  }

  function say(message, tone = "info") {
    announceStatus(editor.region(status), message, { force: true, dwellMs: tone === "error" ? 6000 : 3000, tone });
  }

  function option(value, label) {
    const item = doc.createElement("option");
    item.value = value;
    item.textContent = label;
    return item;
  }

  // ---- Times ----------------------------------------------------------

  // The year of an instant in the station time zone, as the list shows it.
  function stationYear(instant) {
    try {
      return new Intl.DateTimeFormat("en", { year: "numeric", timeZone: data.timezone || undefined }).format(instant);
    } catch {
      return String(new Date(instant).getFullYear());
    }
  }

  // The weekday and date are always said; the year only when it is not
  // this year's in the station time zone.
  function formatWhen(iso) {
    const options = { weekday: "long", day: "numeric", month: "long" };
    if (stationYear(new Date(iso)) !== stationYear(now())) options.year = "numeric";
    try {
      return clockText(new Date(iso), { ...options, timeZone: data.timezone || undefined });
    } catch {
      return clockText(new Date(iso), options);
    }
  }

  function relative(iso) {
    return relativeTimeText(iso, { reference: now(), maxUnit: "day", dayAfterHours: 48 });
  }

  // ---- Lists ----------------------------------------------------------

  function eventWhen(event) {
    const wait = event.wait_seconds > 0 ? `, waits up to ${event.wait_seconds} seconds` : "";
    const timing = event.timing === "hard" ? `Hard timing${wait}` : "Soft timing";
    return `${event.repeat_text}. ${timing}.`;
  }

  function nextRun(event) {
    if (event.ended) return "Ended, no runs left";
    if (!event.enabled) return "Disabled";
    const upcoming = data.upcoming.find((run) => run.id === event.id);
    // The schedule endpoint returns only the next five occurrences, not
    // each event's next time. Absence from that window does not mean ended.
    return upcoming ? `${formatWhen(upcoming.time)}, ${relative(upcoming.time)}` : "Not in next 5 runs";
  }

  const eventsTable = createCollectionTable({
    host: get("events"), label: "Schedule events", status, fallback: addButton,
    emptyText: "No events yet.", keyOf: (event) => event.id, nameOf: (event) => event.name,
    columns: [
      { key: "name", label: "Name", value: (event) => event.name },
      { key: "when", label: "When", value: eventWhen },
      { key: "action", label: "Action", value: (event) => event.action_text },
      { key: "next", label: "Next run", value: nextRun },
      { key: "enabled", label: "Enabled", value: (event) => event.enabled ? "Yes" : "No" },
    ],
    primary: (event) => ({ id: "edit", label: "Edit", run: (button) => openForm(event, button.closest("td")) }),
    actions: (event) => [
      // Run now changes what is on air at the station the page controls (#70).
      { id: "run", label: "Run now", run: () => runEvent(event),
        disabledReason: stationStopped ? "Start the station to run this event now" : "" },
      { id: "toggle", label: event.enabled ? "Disable" : "Enable", run: () => toggleEvent(event) },
      { id: "delete", label: "Delete", run: (button) => deleteEvent(event, button) },
    ],
  });
  const upcomingTable = createCollectionTable({
    host: get("upcoming"), label: "Next events", status, fallback: addButton,
    emptyText: "Nothing is scheduled.", keyOf: (run) => `${run.id}:${run.time}`, nameOf: (run) => run.name,
    columns: [
      { key: "name", label: "Name", value: (run) => run.name },
      { key: "when", label: "When", value: (run) => formatWhen(run.time) },
      { key: "relative", label: "Due", value: (run) => relative(run.time) },
      { key: "action", label: "Action", value: (run) => run.action_text },
    ],
  });
  const runsTable = createCollectionTable({
    host: get("runs"), label: "Recent runs", status, fallback: addButton,
    emptyText: "No events have run yet.", keyOf: (run) => run.key, nameOf: (run) => run.event,
    columns: [
      { key: "name", label: "Name", value: (run) => run.event },
      { key: "when", label: "When", value: (run) => formatWhen(run.time) },
      { key: "action", label: "Action", value: (run) => run.summary },
      { key: "result", label: "Result", value: (run) => run.result === "ok" ? "Done"
        : run.result === "error" ? `Failed: ${run.message}` : run.message || "Missed" },
    ],
  });

  function renderEvents() {
    eventsTable.update(data.events, { announceFocus: !listFocusAnnouncementSuppressed });
  }

  function renderUpcoming() {
    upcomingTable.update(data.upcoming);
  }

  function renderRuns() {
    const occurrences = new Map();
    runsTable.update(data.runs.map((run) => {
      const identity = JSON.stringify([run.time, run.event_id, run.event, run.summary, run.result, run.message]);
      const occurrence = occurrences.get(identity) || 0;
      occurrences.set(identity, occurrence + 1);
      return { ...run, key: `${identity}:${occurrence}` };
    }));
  }

  function render() {
    get("timezone").textContent = data.timezone;
    renderEvents();
    renderUpcoming();
    renderRuns();
  }

  async function load() {
    const mine = ++generation;
    const current = session;
    const reply = await request("/api/schedule");
    if (mine !== generation || current !== session) return false;
    data = { ...emptyData(), ...reply };
    render();
    return true;
  }

  function refresh() {
    if (!panel.open) return Promise.resolve();
    const current = session;
    return load().catch((error) => {
      if (current === session) say(`Could not load the schedule: ${error.message}`, "error");
    });
  }

  function focusRowButton(id, role) {
    focusAfterDialog(eventsTable.target(id, role === "edit" ? "primary" : "options") || addButton);
  }

  // ---- Row buttons ----------------------------------------------------

  async function toggleEvent(event) {
    const current = session;
    try {
      const saved = await request(`/api/schedule/${encodeURIComponent(event.id)}/enable`, {
        method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ enabled: !event.enabled }),
      });
      if (current !== session) return;
      await load();
      if (current !== session) return;
      say(`Event ${saved.name} ${saved.enabled ? "enabled" : "disabled"}`);
    } catch (error) {
      if (current === session) say(`Could not change ${event.name}: ${error.message}`, "error");
    }
  }

  async function runEvent(event) {
    const current = session;
    try {
      const run = await request(`/api/schedule/${encodeURIComponent(event.id)}/run`, { method: "POST" });
      if (current !== session) return;
      await load();
      if (current !== session) return;
      if (run.result === "ok") say(`Event ${event.name} ran`);
      else say(`Event ${event.name} failed: ${run.message}`, "error");
    } catch (error) {
      if (current === session) say(`Could not run ${event.name}: ${error.message}`, "error");
    }
  }

  // The delete runs while the confirmation is open, so the list is updated
  // when it closes and focus goes straight to the next event (see
  // confirmAction).  The result is said once the dialog has closed.
  async function deleteEvent(event, button) {
    const current = session;
    const index = data.events.findIndex((item) => item.id === event.id);
    let result = null;
    const confirmed = await deleteCollectionItem(doc, {
      title: `Delete event ${event.name}?`,
      message: "The event is removed from the schedule.  Its past runs stay in Recent runs.",
      confirmLabel: "Delete event",
      onConfirm: async () => {
        try {
          await request(`/api/schedule/${encodeURIComponent(event.id)}`, { method: "DELETE" });
          if (current !== session) return null;
          if (editing === event.id) closeForm();
          listFocusAnnouncementSuppressed = true;
          try { await load(); } finally { listFocusAnnouncementSuppressed = false; }
          if (current !== session) return null;
          result = [`Event ${event.name} deleted`];
          const next = data.events[Math.min(index, data.events.length - 1)];
          return next ? eventsTable.target(next.id) : addButton;
        } catch (error) {
          if (current !== session) return null;
          result = [`Could not delete ${event.name}: ${error.message}`, "error"];
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

  // ---- The form -------------------------------------------------------

  function renderParams(values = {}) {
    const action = data.actions.find((item) => item.name === actionSelect.value);
    const rows = (action?.fields || []).map((field) => {
      const id = `schedule-param-${field.name}`;
      const row = doc.createElement("div");
      row.className = "settings-row";
      const label = doc.createElement("label");
      label.htmlFor = id;
      label.textContent = field.label;
      const value = values[field.name];
      let control;
      if (field.type === "select") {
        control = doc.createElement("select");
        const options = field.options || [];
        control.append(...options.map((item) => option(item.value, item.label)));
        // A saved value that no longer exists (a deleted profile) stays
        // visible rather than being swapped for another one.
        if (value && !options.some((item) => item.value === value)) {
          control.append(option(value, `${value} (not found)`));
        }
        if (!control.options.length) control.append(option("", `No ${field.label.toLowerCase()} to choose yet`));
      } else {
        control = doc.createElement("input");
        control.type = "text";
        control.maxLength = 200;
        control.autocomplete = "off";
      }
      control.id = id;
      control.dataset.param = field.name;
      // Checked by firstProblem; native required made Firefox call the
      // group "invalid entry" before anything was typed.
      control.setAttribute("aria-required", "true");
      if (value != null) control.value = value;
      row.append(label, control);
      return row;
    });
    paramsBox.replaceChildren(...rows);
  }

  // Only the parts the repeat choice uses are shown.  The Repeat select
  // itself never hides, so changing it keeps focus where it is.
  function showRepeatFields() {
    for (const part of form.querySelectorAll("[data-repeat]")) {
      part.hidden = !part.dataset.repeat.split(" ").includes(repeatSelect.value);
    }
    // A one-time event's date is its whole range, so Date range is hidden
    // for it, unless the event being edited was saved with a range: then
    // the dates stay in view, to keep or clear.
    const range = get("range");
    if (range) {
      const dated = Boolean(get("start-date").value || get("end-date").value || get("dates-repeat").value !== "none");
      range.hidden = repeatSelect.value === "once" && !dated;
    }
    // A description named by aria-describedby is read even when hidden.
    const daysGroup = get("days");
    if (repeatSelect.value === "interval") daysGroup.setAttribute("aria-describedby", "schedule-days-desc");
    else daysGroup.removeAttribute("aria-describedby");
  }

  function openForm(event = null, opener = doc.activeElement) {
    editing = event?.id ?? null;
    clearError();
    get("form-legend").textContent = event ? `Edit ${event.name}` : "Add event";
    nameInput.value = event?.name ?? "";
    actionSelect.replaceChildren(...data.actions.map((item) => option(item.name, item.description)));
    actionSelect.value = event?.action ?? data.actions[0]?.name ?? "";
    renderParams(event?.params);
    repeatSelect.value = event?.repeat ?? "daily";
    get("date").value = event?.date ?? "";
    get("time").value = event?.time?.slice(0, 5) ?? "";
    for (const box of dayBoxes) box.checked = Boolean(event?.days?.includes(Number(box.value)));
    get("interval").value = String(event?.interval_minutes ?? 60);
    get("window-start").value = event?.window_start?.slice(0, 5) ?? "";
    get("window-end").value = event?.window_end?.slice(0, 5) ?? "";
    get("start-date").value = event?.start_date ?? "";
    get("end-date").value = event?.end_date ?? "";
    get("dates-repeat").value = DATES_REPEAT_HELP[event?.dates_repeat] ? event.dates_repeat : "none";
    showDatesRepeatHelp();
    get("timing").value = event?.timing ?? "soft";
    get("wait").value = String(event?.wait_seconds ?? 0);
    get("enabled").checked = event ? event.enabled : true;
    showRepeatFields();
    editor.open(nameInput, opener);
  }

  function showDatesRepeatHelp() {
    get("dates-repeat-desc").textContent = DATES_REPEAT_HELP[get("dates-repeat").value] || DATES_REPEAT_HELP.none;
  }

  function closeForm() {
    editor.close();
    editing = null;
    clearError();
  }

  // The error text joins or leaves a control's own description.
  function linkError(control, linked) {
    const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== formError.id);
    if (linked) ids.push(formError.id);
    if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
    else control.removeAttribute("aria-describedby");
  }

  function unmark(control) {
    control.removeAttribute("aria-invalid");
    linkError(control, false);
  }

  function clearError() {
    formError.hidden = true;
    formError.textContent = "";
    for (const element of form.querySelectorAll("[aria-invalid]")) unmark(element);
  }

  function fieldControls(field) {
    if (field === "days") return dayBoxes;
    if (field.startsWith("params")) {
      const name = field.slice("params.".length);
      const control = paramsBox.querySelector(`[data-param="${name}"]`) || paramsBox.querySelector("[data-param]");
      return [control || actionSelect];
    }
    const ids = {
      name: "name", action: "action", repeat: "repeat", date: "date", time: "time",
      interval_minutes: "interval", window_start: "window-start", window_end: "window-end",
      start_date: "start-date", end_date: "end-date", dates_repeat: "dates-repeat", timing: "timing",
      wait_seconds: "wait", enabled: "enabled",
    };
    const control = ids[field] && get(ids[field]);
    return control ? [control] : [];
  }

  // Say what is wrong, keep the words on screen, and put focus on the field.
  function showError(field, message) {
    const controls = fieldControls(field);
    if (!controls.length) return false;
    clearError();
    formError.textContent = message;
    formError.hidden = false;
    for (const control of controls.slice(1)) {
      control.setAttribute("aria-invalid", "true");
      linkError(control, true);
    }
    showEditorError(controls[0], message, formError, editor.region(status));
    return controls.length > 0;
  }

  // The checks the server makes too, in the order of the fields, so the
  // first problem is the one said.
  function firstProblem() {
    const repeat = repeatSelect.value;
    if (!nameInput.value.trim()) return ["name", "Enter a name"];
    if (!actionSelect.value) return ["action", "Choose an action"];
    for (const control of paramsBox.querySelectorAll("[data-param]")) {
      if (!control.value.trim()) {
        const label = paramsBox.querySelector(`label[for="${control.id}"]`)?.textContent || "this setting";
        const field = `params.${control.dataset.param}`;
        if (control.tagName !== "SELECT") return [field, `Enter ${label.toLowerCase()}`];
        // Only the "No profile to choose yet" placeholder is there.
        if (control.options.length === 1) return [field, control.options[0].textContent];
        return [field, `Choose ${label.toLowerCase()}`];
      }
    }
    if (repeat === "once" && !get("date").value) return ["date", "Enter a date"];
    if (USES_TIME.has(repeat) && !get("time").value) return ["time", "Enter a time"];
    if (repeat === "interval") {
      const minutes = Number(get("interval").value);
      if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) {
        return ["interval_minutes", "Minutes between runs must be a whole number from 1 to 1440"];
      }
      const start = get("window-start").value;
      const end = get("window-end").value;
      if (Boolean(start) !== Boolean(end)) {
        return [start ? "window_end" : "window_start", "Enter both Active from and Active until, or leave both empty"];
      }
      if (start && start === end) return ["window_end", "Active until must differ from Active from"];
    }
    if (repeat === "days" && !dayBoxes.some((box) => box.checked)) return ["days", "Choose at least one day"];
    // Date inputs give "" for a date that is not complete, and ISO dates
    // compare as text.
    for (const [field, id, label] of [["start_date", "start-date", "start date"], ["end_date", "end-date", "end date"]]) {
      if (!get(id).value && get(id).validity?.badInput) return [field, `Enter a whole ${label}, or leave it empty`];
    }
    const from = get("start-date").value;
    const until = get("end-date").value;
    // Every year ignores the year, so its end date may come first; the
    // server checks a one-time date against it.
    const yearly = get("dates-repeat").value === "yearly";
    if (yearly && !from) return ["start_date", "Enter a start date to repeat the dates every year"];
    if (!yearly && from && until && until < from) return ["end_date", "End date must be on or after the start date"];
    const day = get("date").value;
    if (repeat === "once" && !yearly && ((from && day < from) || (until && day > until))) {
      return ["date", "Date must be within the start and end dates"];
    }
    if (get("timing").value === "hard" && waitSeconds() === null) {
      return ["wait_seconds", "Wait up to seconds must be a whole number from 0 to 300"];
    }
    return null;
  }

  // The wait as a whole number of seconds from 0 to 300; empty is 0.
  function waitSeconds() {
    const input = get("wait");
    // A number input gives "" for text that is not a number.
    if (input.validity?.badInput) return null;
    const text = input.value.trim();
    const value = text === "" ? 0 : Number(text);
    return Number.isInteger(value) && value >= 0 && value <= 300 ? value : null;
  }

  function payload() {
    const repeat = repeatSelect.value;
    const params = {};
    for (const control of paramsBox.querySelectorAll("[data-param]")) params[control.dataset.param] = control.value.trim();
    const body = {
      name: nameInput.value.trim(), action: actionSelect.value, params, repeat,
      timing: get("timing").value, enabled: get("enabled").checked,
      // Only hard timing waits; the field is hidden for soft.
      wait_seconds: get("timing").value === "hard" ? waitSeconds() : 0,
    };
    // An empty date is no limit; the server reads a missing one the same way.
    if (get("start-date").value) body.start_date = get("start-date").value;
    if (get("end-date").value) body.end_date = get("end-date").value;
    if (get("dates-repeat").value === "yearly") body.dates_repeat = "yearly";
    if (repeat === "once") body.date = get("date").value;
    if (USES_TIME.has(repeat)) body.time = get("time").value;
    if (repeat === "days" || repeat === "interval") body.days = dayBoxes.filter((box) => box.checked).map((box) => Number(box.value));
    if (repeat === "interval") {
      body.interval_minutes = Number(get("interval").value);
      body.window_start = get("window-start").value || null;
      body.window_end = get("window-end").value || null;
    }
    return body;
  }

  async function save(submitEvent, active) {
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
      saved = await request(id ? `/api/schedule/${encodeURIComponent(id)}` : "/api/schedule", {
        method: id ? "PUT" : "POST", headers: JSON_HEADERS, body: JSON.stringify(payload()),
      });
    } catch (error) {
      if (current !== session || !active()) return;
      if (error.status === 404) {
        closeForm();
        addButton.focus();
        say(`Could not save: ${error.message}`, "error");
      } else if (!error.field || !showError(error.field, error.message)) {
        say(`Could not save: ${error.message}`, "error");
      }
      return;
    }
    if (current !== session || !active()) return;
    // The list is updated before the form closes, so focus goes from Save
    // straight to the event's Edit cell. A failed reload leaves the
    // list to the next schedule message.
    await load().catch(() => false);
    if (current !== session || !active()) return;
    closeForm();
    focusRowButton(saved.id, "edit");
    say(`Event ${saved.name} saved`);
  }

  addButton.addEventListener("click", () => openForm(null, addButton));
  actionSelect.addEventListener("change", () => renderParams());
  repeatSelect.addEventListener("change", showRepeatFields);
  get("dates-repeat").addEventListener("change", showDatesRepeatHelp);
  // A field the user changes is no longer marked as the bad one.
  form.addEventListener("input", (event) => {
    if (event.target.getAttribute?.("aria-invalid")) {
      const group = dayBoxes.includes(event.target) ? dayBoxes : [event.target];
      for (const control of group) unmark(control);
    }
  });
  panel.addEventListener("toggle", () => void refresh());

  return {
    refresh,
    setOffAir(stopped) {
      if (stationStopped === stopped) return;
      stationStopped = stopped;
      renderEvents();
    },
    // Signed out: forget everything this session showed.
    clear() {
      session += 1;
      generation += 1;
      data = emptyData();
      closeForm();
      status.textContent = "";
      get("timezone").textContent = "";
      actionSelect.replaceChildren();
      paramsBox.replaceChildren();
      eventsTable.clear();
      upcomingTable.clear();
      runsTable.clear();
    },
  };
}
