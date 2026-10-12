// Settings > Schedule: timed events (scheduler.py on the server).
//
// The server lists the actions and their settings, so an action that a
// later feature registers appears here without changes to this file.  The
// panel loads when its subtab is seen, and again when the server says the
// schedule changed or an event ran.  The lists are plain text, not live
// regions; each result of a button is said once through #schedule-status.
//
// Focus: Add event and Edit move to the Name field.  Save and Cancel move
// to the event's Edit button (Add event when there is none).  Delete moves
// to the Edit button of the event that took its place.  A bad field is
// named in words, marked aria-invalid and focused.  Rows are updated in
// place, so a refresh never takes focus away from a row's button.

import { requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";

const JSON_HEADERS = { "Content-Type": "application/json" };

// Which repeat choices use a time, a date and so on.
const USES_TIME = new Set(["once", "daily", "weekdays", "days"]);

export function installSchedulerPanel({ doc = document, request = requestJson, now = () => Date.now() } = {}) {
  const panel = doc.getElementById("spanel-schedule");
  if (!panel) return { refresh: async () => {}, clear: () => {} };
  const get = (id) => doc.getElementById(`schedule-${id}`);
  const status = get("status");
  const list = get("events");
  const form = get("form");
  const formError = get("form-error");
  const nameInput = get("name");
  const actionSelect = get("action");
  const paramsBox = get("params");
  const repeatSelect = get("repeat");
  const addButton = get("add");
  const dayBoxes = [...form.querySelectorAll('#schedule-days input[type="checkbox"]')];
  let data = emptyData();
  let editing = null; // the ID of the event in the form, or null when adding
  let session = 0;
  let generation = 0;

  function emptyData() {
    return { timezone: "", actions: [], events: [], upcoming: [], runs: [] };
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

  function textItem(text, className = "") {
    const item = doc.createElement("li");
    if (className) item.className = className;
    item.textContent = text;
    return item;
  }

  // ---- Times ----------------------------------------------------------

  function formatWhen(iso) {
    const options = { weekday: "long", day: "numeric", month: "long", hour: "numeric", minute: "2-digit" };
    try {
      return new Intl.DateTimeFormat(undefined, { ...options, timeZone: data.timezone || undefined }).format(new Date(iso));
    } catch {
      return new Intl.DateTimeFormat(undefined, options).format(new Date(iso));
    }
  }

  function relative(iso) {
    const minutes = Math.round((new Date(iso).getTime() - now()) / 60000);
    const words = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    if (Math.abs(minutes) < 60) return words.format(minutes, "minute");
    if (Math.abs(minutes) < 48 * 60) return words.format(Math.round(minutes / 60), "hour");
    return words.format(Math.round(minutes / 1440), "day");
  }

  // ---- Lists ----------------------------------------------------------

  function eventById(id) {
    return data.events.find((item) => item.id === id) || null;
  }

  function eventText(event) {
    const timing = event.timing === "hard" ? "Hard timing" : "Soft timing";
    return `${event.name}: ${event.repeat_text}. ${event.action_text}. ${timing}.${event.enabled ? "" : " Disabled."}`;
  }

  // One button of a row: short on screen, named for the event for a
  // screen reader ("Delete" and "Delete Morning show").
  function rowButton(role, onClick) {
    const button = doc.createElement("button");
    button.type = "button";
    button.dataset.role = role;
    button.addEventListener("click", () => {
      const id = button.closest("li")?.dataset.eventId;
      const event = id && eventById(id);
      if (event) void onClick(event, button);
    });
    return button;
  }

  function setText(element, text) {
    if (element.textContent !== text) element.textContent = text;
  }

  function setLabel(button, visible, name) {
    setText(button, visible);
    if (button.getAttribute("aria-label") !== name) button.setAttribute("aria-label", name);
  }

  function fillRow(row, event) {
    const [text, edit, toggle, run, remove] = row.children;
    setText(text, eventText(event));
    setLabel(edit, "Edit", `Edit ${event.name}`);
    setLabel(toggle, event.enabled ? "Disable" : "Enable", `${event.enabled ? "Disable" : "Enable"} ${event.name}`);
    setLabel(run, "Run now", `Run now, ${event.name}`);
    setLabel(remove, "Delete", `Delete ${event.name}`);
  }

  function newRow(event) {
    const row = doc.createElement("li");
    row.dataset.eventId = event.id;
    row.append(doc.createElement("span"));
    // Spaced like the other settings lists (dom-helpers listRow).
    for (const [role, onClick] of [["edit", openForm], ["toggle", toggleEvent], ["run", runEvent], ["delete", deleteEvent]]) {
      row.append(" ", rowButton(role, onClick));
    }
    fillRow(row, event);
    return row;
  }

  // Keep the rows that still exist, so focus on one of their buttons
  // survives a refresh; remove the gone ones and add the new ones in order.
  function renderEvents() {
    const ids = new Set(data.events.map((item) => item.id));
    for (const row of [...list.children]) {
      if (!ids.has(row.dataset.eventId)) row.remove();
    }
    data.events.forEach((event, index) => {
      const at = list.children[index];
      if (at?.dataset.eventId === event.id) {
        fillRow(at, event);
        return;
      }
      const existing = [...list.children].find((row) => row.dataset.eventId === event.id);
      if (existing) {
        list.insertBefore(existing, at || null);
        fillRow(existing, event);
      } else {
        list.insertBefore(newRow(event), at || null);
      }
    });
    if (!data.events.length) list.replaceChildren(textItem("No events yet.", "no-results"));
  }

  function renderUpcoming() {
    const items = data.upcoming.map((row) => textItem(
      `${formatWhen(row.time)}, ${relative(row.time)}: ${row.name}. ${row.action_text}.`,
    ));
    get("upcoming").replaceChildren(...(items.length ? items : [textItem("Nothing is scheduled.", "no-results")]));
  }

  function renderRuns() {
    const result = { ok: () => "Done", error: (run) => `Failed: ${run.message}`, missed: (run) => run.message || "Missed" };
    const items = data.runs.map((run) => textItem(
      `${formatWhen(run.time)}: ${run.event}. ${run.summary}. ${(result[run.result] || result.missed)(run)}.`,
    ));
    get("runs").replaceChildren(...(items.length ? items : [textItem("No events have run yet.", "no-results")]));
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
    const row = [...list.children].find((item) => item.dataset.eventId === id);
    (row?.querySelector(`[data-role="${role}"]`) || addButton).focus();
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
    const confirmed = await confirmAction(doc, {
      title: `Delete event ${event.name}?`,
      message: "The event is removed from the schedule.  Its past runs stay in Recent runs.",
      confirmLabel: "Delete event",
      onConfirm: async () => {
        try {
          await request(`/api/schedule/${encodeURIComponent(event.id)}`, { method: "DELETE" });
          if (current !== session) return null;
          if (editing === event.id) closeForm();
          await load();
          if (current !== session) return null;
          result = [`Event ${event.name} deleted`];
          const edits = list.querySelectorAll('[data-role="edit"]');
          return edits[Math.min(index, edits.length - 1)] || addButton;
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
      } else if (field.type === "time") {
        control = doc.createElement("input");
        control.type = "time";
      } else {
        control = doc.createElement("input");
        control.type = "text";
        control.maxLength = 200;
        control.autocomplete = "off";
      }
      control.id = id;
      control.dataset.param = field.name;
      control.required = true;
      if (value != null) control.value = value;
      row.append(label, control);
      return row;
    });
    paramsBox.replaceChildren(...rows);
    // An action with its own timing (Start show) runs on time; the
    // show's own Start setting says whether the song playing finishes.
    const own = Boolean(action?.own_timing);
    const timing = get("timing");
    const timingRow = timing.closest(".settings-row");
    if (timingRow) timingRow.hidden = own;
    get("timing-desc").hidden = own;
    get("own-timing").hidden = !own;
    // Said with the action, since the Timing field it replaces is gone.
    if (own) actionSelect.setAttribute("aria-describedby", "schedule-own-timing");
    else actionSelect.removeAttribute("aria-describedby");
  }

  // Only the parts the repeat choice uses are shown.  The Repeat select
  // itself never hides, so changing it keeps focus where it is.
  function showRepeatFields() {
    for (const part of form.querySelectorAll("[data-repeat]")) {
      part.hidden = !part.dataset.repeat.split(" ").includes(repeatSelect.value);
    }
  }

  function openForm(event = null) {
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
    get("timing").value = event?.timing ?? "soft";
    get("enabled").checked = event ? event.enabled : true;
    showRepeatFields();
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
      timing: "timing", enabled: "enabled",
    };
    const control = ids[field] && get(ids[field]);
    return control ? [control] : [];
  }

  // Say what is wrong, keep the words on screen, and put focus on the field.
  function showError(field, message) {
    clearError();
    formError.textContent = message;
    formError.hidden = false;
    const controls = fieldControls(field);
    for (const control of controls) control.setAttribute("aria-invalid", "true");
    say(message, "error");
    controls[0]?.focus();
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
    if (repeat === "days" && !dayBoxes.some((box) => box.checked)) return ["days", "Choose at least one day"];
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
    return null;
  }

  function payload() {
    const repeat = repeatSelect.value;
    const params = {};
    for (const control of paramsBox.querySelectorAll("[data-param]")) params[control.dataset.param] = control.value.trim();
    const body = {
      name: nameInput.value.trim(), action: actionSelect.value, params, repeat,
      timing: get("timing").value, enabled: get("enabled").checked,
    };
    if (repeat === "once") body.date = get("date").value;
    if (USES_TIME.has(repeat)) body.time = get("time").value;
    if (repeat === "days") body.days = dayBoxes.filter((box) => box.checked).map((box) => Number(box.value));
    if (repeat === "interval") {
      body.interval_minutes = Number(get("interval").value);
      body.window_start = get("window-start").value || null;
      body.window_end = get("window-end").value || null;
    }
    return body;
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
      saved = await request(id ? `/api/schedule/${encodeURIComponent(id)}` : "/api/schedule", {
        method: id ? "PUT" : "POST", headers: JSON_HEADERS, body: JSON.stringify(payload()),
      });
    } catch (error) {
      if (current !== session) return;
      if (error.status === 404) {
        closeForm();
        addButton.focus();
        say(`Could not save: ${error.message}`, "error");
      } else if (!error.field || !showError(error.field, error.message)) {
        say(`Could not save: ${error.message}`, "error");
      }
      return;
    }
    if (current !== session) return;
    // The list is updated before the form closes, so focus goes from Save
    // straight to the event's Edit button.  A failed reload leaves the
    // list to the next schedule message.
    await load().catch(() => false);
    if (current !== session) return;
    closeForm();
    focusRowButton(saved.id, "edit");
    say(`Event ${saved.name} saved`);
  }

  addButton.addEventListener("click", () => openForm());
  get("cancel").addEventListener("click", () => {
    const id = editing;
    closeForm();
    if (id) focusRowButton(id, "edit");
    else addButton.focus();
  });
  actionSelect.addEventListener("change", () => renderParams());
  repeatSelect.addEventListener("change", showRepeatFields);
  // A field the user changes is no longer marked as the bad one.
  form.addEventListener("input", (event) => {
    if (event.target.getAttribute?.("aria-invalid")) {
      const group = dayBoxes.includes(event.target) ? dayBoxes : [event.target];
      for (const control of group) control.removeAttribute("aria-invalid");
    }
  });
  form.addEventListener("submit", (event) => void save(event));
  panel.addEventListener("toggle", () => void refresh());

  return {
    refresh,
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
      list.replaceChildren();
      get("upcoming").replaceChildren();
      get("runs").replaceChildren();
    },
  };
}
