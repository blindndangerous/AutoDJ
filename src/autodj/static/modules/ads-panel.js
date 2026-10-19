import { createEditorDialog, showEditorError } from "./editor-dialog.js";
// Ads tab (#51): spots, scheduled ad breaks, their rules and the ad report.
//
// Everything is found inside #ads-panel.  The panel loads when its tab
// shows, when the page changes station and after each change it makes.
// The lists are plain text, not live regions; each result of a button is
// said once through #ads-status, and the report's totals once when it is
// shown.  The report is two tables with column headers and a row header.
//
// Focus: Add spot and Add ad break move to the form's first field.  Save
// and Cancel move to the row's Edit button (the Add button when there is
// none).  Delete asks first, then moves to the Edit button of the row that
// took its place.  A refused field is named in words, marked aria-invalid,
// described by the error and focused.  Rows are updated in place, so a
// refresh never takes focus away from a row's button.

import { requestFile, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { focusAfterDialog, reconcileRows, stationDay } from "./dom-helpers.js";
import { installActionListNavigation } from "./action-list-navigation.js";
import { exportFilename, saveFile } from "./history-export.js";
import { announceStatus } from "./live-region.js";

const JSON_HEADERS = { "Content-Type": "application/json" };

export function hourLabel(hour) {
  if (hour === 24) return "24:00, midnight";
  return `${String(hour).padStart(2, "0")}:00`;
}

// "15, 45" or "15 45" to [15, 45]; null when a part is not a whole minute.
export function parseMinutes(text) {
  const parts = String(text || "").split(/[\s,;]+/).filter(Boolean);
  const minutes = [];
  for (const part of parts) {
    if (!/^\d{1,2}$/.test(part)) return null;
    const value = Number(part);
    if (value > 59) return null;
    if (!minutes.includes(value)) minutes.push(value);
  }
  return minutes.sort((a, b) => a - b);
}

export function spotText(spot) {
  const parts = [`${spot.title}, ${spot.advertiser}`];
  if (spot.campaign) parts[0] += `, campaign ${spot.campaign}`;
  parts.push(`${spot.seconds} seconds`);
  if (spot.category) parts.push(`Competing category ${spot.category}`);
  parts.push(spot.dates_text || "No flight dates");
  const limits = [];
  if (spot.max_per_day) limits.push(`at most ${spot.max_per_day} a day`);
  if (spot.max_per_hour) limits.push(`at most ${spot.max_per_hour} an hour`);
  parts.push(limits.length ? `Airs ${limits.join(" and ")}` : "No play limits");
  parts.push(`Aired or booked ${spot.plays_today} ${spot.plays_today === 1 ? "time" : "times"} today`);
  if (!spot.enabled) parts.push("Turned off");
  else if (!spot.in_flight) parts.push(`Not airing: ${spot.flight_text}`);
  if (spot.missing) parts.push("Its audio file is missing");
  if (spot.booked) parts.push("Booked for a coming song change");
  return `${parts.join(". ")}.`;
}

export function breakText(item) {
  let text = `${item.name}: ${item.when_text}. Up to ${item.max_spots} ${item.max_spots === 1 ? "spot" : "spots"}`;
  if (item.max_seconds) text += `, ${item.max_seconds} seconds at most`;
  text += ".";
  if (!item.enabled) text += " Turned off.";
  else if (item.next_text) text += ` Next ${item.next_text}.`;
  return text;
}

export function outcomeText(row) {
  const spot = row.spot_id ? `${row.advertiser}, ${row.title}` : "No spot";
  let text = `${row.status_text}: ${spot}, in ${row.break_name || "an ad break"} due ${row.scheduled_text}`;
  if (row.aired_text) text += `, aired ${row.aired_text}`;
  if (row.reason) text += `. ${row.reason}`;
  return `${text}.`;
}

export function reportSummary(report) {
  const totals = report.totals || {};
  const spots = (count, noun = "spot") => `${count} ${noun}${count === 1 ? "" : "s"}`;
  let text = `Ad report from ${report.from} to ${report.to}: ${spots(totals.aired || 0)} aired`;
  if (totals.late) text += `, ${totals.late} of them late`;
  text += `, ${spots(totals.missed || 0)} missed`;
  if (totals.missed_breaks) text += ` (${spots(totals.missed_breaks, "break")} with no spot)`;
  if (totals.interrupted) text += `, ${spots(totals.interrupted)} cut off on the air that may have aired`;
  if (totals.waiting) text += `, ${totals.waiting} waiting to air`;
  return `${text}.`;
}

export function installAdsPanel({
  doc = document,
  request = requestJson,
  download = requestFile,
  save = saveFile,
} = {}) {
  const root = doc.getElementById("ads-panel");
  if (!root) return null;
  const panel = root.closest("[role='tabpanel']") || root;
  const get = (id) => doc.getElementById(`ads-${id}`);
  const status = get("status");
  const spotList = get("spots");
  const breakList = get("breaks");
  const listNavigation = [spotList, breakList].map(installActionListNavigation);
  let listFocusAnnouncementSuppressed = false;
  const spotForm = get("spot-form");
  const breakForm = get("break-form");
  const rulesForm = get("rules-form");
  const reportForm = get("report-form");
  const spotAdd = get("spot-add");
  const breakAdd = get("break-add");
  const dayBoxes = [...breakForm.querySelectorAll('#ads-break-days input[type="checkbox"]')];
  let data = null;
  let editingSpot = null;
  let editingBreak = null;
  let session = 0;
  let generation = 0;
  let rulesStation = null;
  let datesShown = false;

  const spotEditor = createEditorDialog({ form: spotForm, heading: get("spot-legend"), cancel: get("spot-cancel"),
    fallback: spotAdd, save: saveSpot, onCancel: closeSpotForm });
  const breakEditor = createEditorDialog({ form: breakForm, heading: get("break-legend"), cancel: get("break-cancel"),
    fallback: breakAdd, save: saveBreak, onCancel: closeBreakForm });

  const shown = () => !panel.hidden;
  function say(message, tone = "info") {
    announceStatus(spotEditor.region(shown() ? status : (doc.getElementById("sr-status") || status)), message, {
      force: true, dwellMs: tone === "error" ? 6000 : 3000, tone,
    });
  }

  function setText(element, text) {
    if (element.textContent !== text) element.textContent = text;
  }

  function option(value, label) {
    const item = doc.createElement("option");
    item.value = String(value);
    item.textContent = label;
    return item;
  }

  function textItem(text, className = "") {
    const item = doc.createElement("li");
    if (className) item.className = className;
    item.textContent = text;
    return item;
  }

  for (let hour = 0; hour < 24; hour += 1) get("break-from").append(option(hour, hourLabel(hour)));
  for (let hour = 1; hour <= 24; hour += 1) get("break-until").append(option(hour, hourLabel(hour)));

  // ---- Errors: words in the form, linked to the field, said once ----

  function linkError(control, error, linked) {
    const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== error.id);
    if (linked) ids.unshift(error.id);
    if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
    else control.removeAttribute("aria-describedby");
  }

  function clearError(form) {
    const error = form.querySelector(".field-error");
    error.hidden = true;
    error.textContent = "";
    for (const control of form.querySelectorAll("[aria-invalid]")) {
      control.removeAttribute("aria-invalid");
      linkError(control, error, false);
    }
  }

  function showError(form, controls, message) {
    if (!controls.length) return false;
    clearError(form);
    const error = form.querySelector(".field-error");
    error.textContent = message;
    error.hidden = false;
    for (const control of controls.slice(1)) {
      control.setAttribute("aria-invalid", "true");
      linkError(control, error, true);
    }
    const targetStatus = form === spotForm && spotEditor.dialog.open ? spotEditor.status
      : form === breakForm && breakEditor.dialog.open ? breakEditor.status : status;
    showEditorError(controls[0], message, error, targetStatus);
    return controls.length > 0;
  }

  const SPOT_FIELDS = {
    file: "spot-file", title: "spot-title", advertiser: "spot-advertiser", campaign: "spot-campaign",
    category: "spot-category", start_date: "spot-start", end_date: "spot-end",
    max_per_day: "spot-day", max_per_hour: "spot-hour", enabled: "spot-enabled",
  };
  const BREAK_FIELDS = {
    name: "break-name", minutes: "break-minutes", from_hour: "break-from", until_hour: "break-until",
    start_date: "break-start", end_date: "break-end", max_spots: "break-spots",
    max_seconds: "break-seconds", enabled: "break-enabled",
  };
  const RULE_FIELDS = {
    separation_minutes: "rules-separation", late_minutes: "rules-late", miss_after_minutes: "rules-missed",
  };

  function fieldControls(fields, field) {
    if (field === "days") return dayBoxes;
    const id = fields[field];
    const control = id && get(id);
    return control && !control.closest("[hidden]") ? [control] : [];
  }

  // The server's refusal: on its field when it names one, else said.
  function refused(form, fields, error, what) {
    if (!error.field || !showError(form, fieldControls(fields, error.field), error.message)) {
      say(`Could not save ${what}: ${error.message}`, "error");
    }
  }

  // ---- Lists ----

  function rowButton(label, onClick) {
    const button = doc.createElement("button");
    button.type = "button";
    button.dataset.role = label.toLowerCase().replace(/\s+/g, "-");
    button.textContent = label;
    button.addEventListener("click", () => void onClick(button));
    return button;
  }

  // Keep the rows that still exist so focus on their buttons survives.
  function renderRows(list, items, emptyText, build, fill) {
    const controls = list === spotList ? listNavigation[0] : listNavigation[1];
    const fallback = list === spotList ? spotAdd : breakAdd;
    const hasRows = !!list.querySelector(":scope > li[data-row-key]");
    if (items.length || hasRows) reconcileRows(list, items, (item) => item.id, build, fill, { fallback, onFocusLost: ({ target }) => {
      if (!listFocusAnnouncementSuppressed) say(`The ad list changed. Focus moved to ${target?.getAttribute("aria-label") || target?.textContent.trim() || "the Add control"}.`);
    } });
    if (!items.length && !list.querySelector(":scope > li.no-results")) list.append(textItem(emptyText, "no-results"));
    controls.refresh();
  }

  function spotRow(spot) {
    const row = doc.createElement("li");
    row.dataset.id = spot.id;
    row.append(doc.createElement("span"));
    row.append(rowButton("Edit", (button) => openSpotForm(spotById(row.dataset.id), button)));
    row.append(rowButton("Delete", (button) => deleteSpot(spotById(row.dataset.id), button)));
    return row;
  }

  function fillSpot(row, spot) {
    const [text, edit, remove] = row.children;
    setText(text, spotText(spot));
    const editName = `Edit spot ${spot.title}, ${spot.advertiser}`;
    const deleteName = `Delete spot ${spot.title}, ${spot.advertiser}`;
    if (edit.getAttribute("aria-label") !== editName) edit.setAttribute("aria-label", editName);
    if (remove.getAttribute("aria-label") !== deleteName) remove.setAttribute("aria-label", deleteName);
  }

  function breakRow(item) {
    const row = doc.createElement("li");
    row.dataset.id = item.id;
    row.append(doc.createElement("span"));
    row.append(rowButton("Edit", (button) => openBreakForm(breakById(row.dataset.id), button)));
    const run = rowButton("Run now", (button) => runBreak(breakById(row.dataset.id), button));
    // Run now changes what is on air at the station the page controls.
    row.append(run);
    row.append(rowButton("Delete", (button) => deleteBreak(breakById(row.dataset.id), button)));
    return row;
  }

  function fillBreak(row, item) {
    const [text, edit, run, remove] = row.children;
    setText(text, breakText(item));
    const labels = [[edit, `Edit ad break ${item.name}`], [run, `Run now, ad break ${item.name}`],
      [remove, `Delete ad break ${item.name}`]];
    for (const [button, name] of labels) if (button.getAttribute("aria-label") !== name) button.setAttribute("aria-label", name);
  }

  const spotById = (id) => data?.spots.find((spot) => spot.id === id) || null;
  const breakById = (id) => data?.breaks.find((item) => item.id === id) || null;

  function render() {
    setText(get("timezone"), data.timezone || "");
    for (const [id, text] of [["reason", data.reason], ["problem", data.problem]]) {
      const note = get(id);
      setText(note, text || "");
      note.hidden = !text;
    }
    renderRows(spotList, data.spots, "No spots yet.", spotRow, fillSpot);
    renderRows(breakList, data.breaks, "No ad breaks yet.", breakRow, fillBreak);
    const pending = data.pending.map((run) => textItem(
      `${run.break_name}, due ${run.scheduled_text}, from ${run.source_text}: waiting for a song change.`,
    ));
    get("pending").replaceChildren(...(pending.length ? pending : [textItem("Nothing is waiting.", "no-results")]));
    const recent = data.recent.map((row) => textItem(outcomeText(row)));
    get("recent").replaceChildren(...(recent.length ? recent : [textItem("No ad break has run since AutoDJ started.", "no-results")]));
    if (rulesStation !== data.station) fillRules();
    if (!datesShown && data.timezone) {
      const today = stationDay(data.timezone);
      if (today) {
        get("report-from").value = `${today.slice(0, 8)}01`;
        get("report-to").value = today;
        datesShown = true;
      }
    }
  }

  function fillRules() {
    for (const [field, id] of Object.entries(RULE_FIELDS)) get(id).value = String(data.settings[field]);
    rulesStation = data.station;
  }

  async function load() {
    const mine = ++generation;
    const current = session;
    const reply = await request("/api/ads");
    if (mine !== generation || current !== session) return false;
    data = reply;
    render();
    return true;
  }

  function refresh() {
    const current = session;
    return load().catch((error) => {
      if (current === session && shown()) say(`Could not load the ads: ${error.message}`, "error");
    });
  }

  function focusRow(list, id, fallback) {
    const row = [...list.children].find((item) => item.dataset.id === id);
    focusAfterDialog(row?.querySelector('[data-role="edit"]') || fallback);
  }

  // ---- Spots ----

  function openSpotForm(spot = null, opener = doc.activeElement) {
    editingSpot = spot?.id ?? null;
    clearError(spotForm);
    setText(get("spot-legend"), spot ? `Edit spot ${spot.title}` : "Add spot");
    get("spot-file-row").hidden = Boolean(spot);
    get("spot-file-desc").hidden = Boolean(spot);
    get("spot-file").value = "";
    get("spot-title").value = spot?.title ?? "";
    get("spot-advertiser").value = spot?.advertiser ?? "";
    get("spot-campaign").value = spot?.campaign ?? "";
    get("spot-category").value = spot?.category ?? "";
    get("spot-start").value = spot?.start_date ?? "";
    get("spot-end").value = spot?.end_date ?? "";
    get("spot-day").value = String(spot?.max_per_day ?? 0);
    get("spot-hour").value = String(spot?.max_per_hour ?? 0);
    get("spot-enabled").checked = spot ? spot.enabled : true;
    spotEditor.open(spot ? get("spot-title") : get("spot-file"), opener);
  }

  function closeSpotForm() {
    spotEditor.close();
    editingSpot = null;
    clearError(spotForm);
  }

  function wholeNumber(id, low, high) {
    const text = get(id).value.trim();
    if (!/^\d+$/.test(text)) return null;
    const value = Number(text);
    return value >= low && value <= high ? value : null;
  }

  function spotProblem() {
    const max = data?.limits?.max_bytes || 0;
    if (!editingSpot) {
      const file = get("spot-file").files?.[0];
      if (!file) return ["file", "Choose the spot's audio file"];
      if (max && file.size > max) return ["file", `The file is larger than ${Math.round(max / 1048576)} MB`];
    }
    if (!get("spot-title").value.trim()) return ["title", "Enter a title for the spot"];
    if (!get("spot-advertiser").value.trim()) return ["advertiser", "Enter the advertiser"];
    const from = get("spot-start").value;
    const until = get("spot-end").value;
    if (from && until && until < from) return ["end_date", "The last day must be on or after the first day"];
    if (wholeNumber("spot-day", 0, 500) === null) return ["max_per_day", "Plays a day must be a whole number from 0 to 500"];
    if (wholeNumber("spot-hour", 0, 60) === null) return ["max_per_hour", "Plays an hour must be a whole number from 0 to 60"];
    return null;
  }

  function spotFields() {
    return {
      title: get("spot-title").value.trim(),
      advertiser: get("spot-advertiser").value.trim(),
      campaign: get("spot-campaign").value.trim(),
      category: get("spot-category").value.trim(),
      start_date: get("spot-start").value || null,
      end_date: get("spot-end").value || null,
      max_per_day: wholeNumber("spot-day", 0, 500),
      max_per_hour: wholeNumber("spot-hour", 0, 60),
      enabled: get("spot-enabled").checked,
    };
  }

  async function saveSpot(event, active) {
    event.preventDefault();
    const problem = spotProblem();
    if (problem) {
      showError(spotForm, fieldControls(SPOT_FIELDS, problem[0]), problem[1]);
      return;
    }
    clearError(spotForm);
    const current = session;
    const id = editingSpot;
    const fields = spotFields();
    let saved;
    if (!id) say("Uploading the spot");
    try {
      if (id) {
        saved = await request(`/api/ads/spots/${encodeURIComponent(id)}`, {
          method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(fields),
        });
      } else {
        const form = new FormData();
        form.append("file", get("spot-file").files[0]);
        for (const [key, value] of Object.entries(fields)) form.append(key, value === null ? "" : String(value));
        saved = await request("/api/ads/spots", { method: "POST", body: form });
      }
    } catch (error) {
      if (current === session && active()) refused(spotForm, SPOT_FIELDS, error, "the spot");
      return;
    }
    if (current !== session || !active()) return;
    await load().catch(() => false);
    if (current !== session || !active()) return;
    closeSpotForm();
    focusRow(spotList, saved.id, spotAdd);
    say(`Spot ${saved.title} saved`);
  }

  async function deleteSpot(spot, button) {
    if (!spot) return;
    const current = session;
    const index = data.spots.findIndex((item) => item.id === spot.id);
    let result = null;
    const confirmed = await confirmAction(doc, {
      title: `Delete spot ${spot.title}?`,
      message: "The spot and its audio are removed. What it aired stays in the ad report.",
      confirmLabel: "Delete spot",
      onConfirm: async () => {
        try {
          await request(`/api/ads/spots/${encodeURIComponent(spot.id)}`, { method: "DELETE" });
          if (current !== session) return null;
          if (editingSpot === spot.id) closeSpotForm();
          listFocusAnnouncementSuppressed = true;
          try { await load(); } finally { listFocusAnnouncementSuppressed = false; }
          if (current !== session) return null;
          result = [`Spot ${spot.title} deleted`];
          const edits = spotList.querySelectorAll('[data-role="edit"]');
          return edits[Math.min(index, edits.length - 1)] || spotAdd;
        } catch (error) {
          if (current !== session) return null;
          result = [`Could not delete ${spot.title}: ${error.message}`, "error"];
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

  // ---- Breaks ----

  function openBreakForm(item = null, opener = doc.activeElement) {
    editingBreak = item?.id ?? null;
    clearError(breakForm);
    setText(get("break-legend"), item ? `Edit ad break ${item.name}` : "Add ad break");
    get("break-name").value = item?.name ?? "";
    get("break-minutes").value = item ? item.minutes.join(", ") : "15, 45";
    get("break-from").value = String(item?.from_hour ?? 0);
    get("break-until").value = String(item?.until_hour ?? 24);
    for (const box of dayBoxes) box.checked = Boolean(item?.days?.includes(Number(box.value)));
    get("break-start").value = item?.start_date ?? "";
    get("break-end").value = item?.end_date ?? "";
    get("break-spots").value = String(item?.max_spots ?? 3);
    get("break-seconds").value = String(item?.max_seconds ?? 0);
    get("break-enabled").checked = item ? item.enabled : true;
    breakEditor.open(get("break-name"), opener);
  }

  function closeBreakForm() {
    breakEditor.close();
    editingBreak = null;
    clearError(breakForm);
  }

  function breakProblem() {
    if (!get("break-name").value.trim()) return ["name", "Enter a name for the break"];
    if (parseMinutes(get("break-minutes").value) === null) {
      return ["minutes", "Minutes past the hour must be numbers from 0 to 59, separated by commas"];
    }
    if (Number(get("break-until").value) <= Number(get("break-from").value)) {
      return ["until_hour", "Until hour must be after From hour"];
    }
    const from = get("break-start").value;
    const until = get("break-end").value;
    if (from && until && until < from) return ["end_date", "The last day must be on or after the first day"];
    if (wholeNumber("break-spots", 1, 10) === null) return ["max_spots", "Spots in the break must be a whole number from 1 to 10"];
    const seconds = wholeNumber("break-seconds", 0, 900);
    if (seconds === null || (seconds > 0 && seconds < 10)) {
      return ["max_seconds", "The longest break must be 0 (no limit) or from 10 to 900 seconds"];
    }
    return null;
  }

  function breakFields() {
    return {
      name: get("break-name").value.trim(),
      minutes: parseMinutes(get("break-minutes").value),
      days: dayBoxes.filter((box) => box.checked).map((box) => Number(box.value)),
      from_hour: Number(get("break-from").value),
      until_hour: Number(get("break-until").value),
      start_date: get("break-start").value || null,
      end_date: get("break-end").value || null,
      max_spots: wholeNumber("break-spots", 1, 10),
      max_seconds: wholeNumber("break-seconds", 0, 900),
      enabled: get("break-enabled").checked,
    };
  }

  async function saveBreak(event, active) {
    event.preventDefault();
    const problem = breakProblem();
    if (problem) {
      showError(breakForm, fieldControls(BREAK_FIELDS, problem[0]), problem[1]);
      return;
    }
    clearError(breakForm);
    const current = session;
    const id = editingBreak;
    let saved;
    try {
      saved = await request(id ? `/api/ads/breaks/${encodeURIComponent(id)}` : "/api/ads/breaks", {
        method: id ? "PUT" : "POST", headers: JSON_HEADERS, body: JSON.stringify(breakFields()),
      });
    } catch (error) {
      if (current === session && active()) refused(breakForm, BREAK_FIELDS, error, "the ad break");
      return;
    }
    if (current !== session || !active()) return;
    await load().catch(() => false);
    if (current !== session || !active()) return;
    closeBreakForm();
    focusRow(breakList, saved.id, breakAdd);
    say(`Ad break ${saved.name} saved`);
  }

  async function runBreak(item) {
    if (!item) return;
    const current = session;
    try {
      const reply = await request(`/api/ads/breaks/${encodeURIComponent(item.id)}/run`, { method: "POST" });
      if (current !== session) return;
      await load().catch(() => false);
      if (current === session) say(reply.message);
    } catch (error) {
      if (current === session) say(`Could not run ${item.name}: ${error.message}`, "error");
    }
  }

  async function deleteBreak(item, button) {
    if (!item) return;
    const current = session;
    const index = data.breaks.findIndex((entry) => entry.id === item.id);
    let result = null;
    const confirmed = await confirmAction(doc, {
      title: `Delete ad break ${item.name}?`,
      message: "The break no longer runs. A Schedule event that names it fails until it names another. What it aired stays in the ad report.",
      confirmLabel: "Delete ad break",
      onConfirm: async () => {
        try {
          await request(`/api/ads/breaks/${encodeURIComponent(item.id)}`, { method: "DELETE" });
          if (current !== session) return null;
          if (editingBreak === item.id) closeBreakForm();
          listFocusAnnouncementSuppressed = true;
          try { await load(); } finally { listFocusAnnouncementSuppressed = false; }
          if (current !== session) return null;
          result = [`Ad break ${item.name} deleted`];
          const edits = breakList.querySelectorAll('[data-role="edit"]');
          return edits[Math.min(index, edits.length - 1)] || breakAdd;
        } catch (error) {
          if (current !== session) return null;
          result = [`Could not delete ${item.name}: ${error.message}`, "error"];
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

  // ---- Rules ----

  async function saveRules(event) {
    event.preventDefault();
    const values = {};
    for (const [field, id, low, high, label] of [
      ["separation_minutes", "rules-separation", 0, 1440, "Separation"],
      ["late_minutes", "rules-late", 1, 240, "Late after"],
      ["miss_after_minutes", "rules-missed", 5, 720, "Missed after"],
    ]) {
      const value = wholeNumber(id, low, high);
      if (value === null) {
        showError(rulesForm, [get(id)], `${label} must be a whole number of minutes from ${low} to ${high}`);
        return;
      }
      values[field] = value;
    }
    if (values.miss_after_minutes < values.late_minutes) {
      showError(rulesForm, [get("rules-missed")], "Missed after must not be shorter than Late after");
      return;
    }
    clearError(rulesForm);
    const current = session;
    try {
      await request("/api/ads/settings", { method: "PUT", headers: JSON_HEADERS, body: JSON.stringify(values) });
    } catch (error) {
      if (current === session) refused(rulesForm, RULE_FIELDS, error, "the rules");
      return;
    }
    if (current !== session) return;
    await load().catch(() => false);
    if (current === session) say("Rules saved");
  }

  // ---- The report ----

  function cell(tag, text, className = "") {
    const element = doc.createElement(tag);
    if (tag === "th") element.scope = "row";
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  function reportDates() {
    const from = get("report-from");
    const to = get("report-to");
    if (!from.value || !to.value) {
      showError(reportForm, [from.value ? to : from], "Choose both a From date and a To date");
      return null;
    }
    if (to.value < from.value) {
      showError(reportForm, [to], "To must be on or after From");
      return null;
    }
    clearError(reportForm);
    return new URLSearchParams({ from: from.value, to: to.value });
  }

  function renderReport(report) {
    const totals = (report.summary || []).map((group) => {
      const row = doc.createElement("tr");
      row.append(
        cell("th", group.advertiser),
        cell("td", group.campaign || "None"),
        cell("td", String(group.aired), "ads-number"),
        cell("td", String(group.late), "ads-number"),
        cell("td", String(group.missed), "ads-number"),
        cell("td", String(group.interrupted ?? 0), "ads-number"),
        cell("td", String(group.waiting), "ads-number"),
        cell("td", String(group.seconds), "ads-number"),
      );
      return row;
    });
    get("totals").replaceChildren(...totals);
    get("totals-wrap").hidden = !totals.length;
    const rows = (report.rows || []).map((entry) => {
      const row = doc.createElement("tr");
      let result = entry.status_text;
      if (entry.delay_minutes !== null && entry.delay_minutes !== undefined) {
        result += `, ${entry.delay_minutes} minutes after its time`;
      }
      const spot = entry.spot_id
        ? `${entry.advertiser}: ${entry.title}${entry.campaign ? `, campaign ${entry.campaign}` : ""}`
        : "No spot";
      row.append(
        cell("th", entry.scheduled_text),
        cell("td", entry.aired_text || "Did not air"),
        cell("td", result),
        cell("td", spot),
        cell("td", `${entry.break_name}, ${entry.source_text}`),
        cell("td", entry.reason || "None"),
      );
      return row;
    });
    get("rows").replaceChildren(...rows);
    get("rows-wrap").hidden = !rows.length;
    const summary = get("report-summary");
    summary.textContent = rows.length ? reportSummary(report) : `No ad breaks ran from ${report.from} to ${report.to}.`;
    summary.hidden = false;
    const notes = [];
    if (report.truncated) notes.push(`The table shows the first ${rows.length} of ${report.count}; Export has them all.`);
    if (report.damaged) notes.push(`${report.damaged} ${report.damaged === 1 ? "line" : "lines"} of the ad play log could not be read and ${report.damaged === 1 ? "is" : "are"} left out.`);
    const note = get("report-note");
    note.textContent = notes.join(" ");
    note.hidden = !notes.length;
    return summary.textContent;
  }

  async function showReport(event) {
    event.preventDefault();
    const query = reportDates();
    if (!query) return;
    const current = session;
    try {
      const report = await request(`/api/ads/report?${query}`);
      if (current === session) say(renderReport(report));
    } catch (error) {
      if (current === session) say(`Could not show the ad report: ${error.message}`, "error");
    }
  }

  async function exportReport() {
    const query = reportDates();
    if (!query) return;
    query.set("format", get("report-format").value);
    const current = session;
    try {
      const response = await download(`/api/ads/report/export?${query}`, ["text/csv"]);
      const blob = await response.blob();
      if (current !== session) return;
      save(doc, blob, exportFilename(response.headers.get("Content-Disposition")));
      const count = Number(response.headers.get("X-Row-Count")) || 0;
      say(`Ad report saved: ${count} ${count === 1 ? "row" : "rows"}.`);
    } catch (error) {
      if (current === session) say(`Could not export the ad report: ${error.message}`, "error");
    }
  }

  // ---- Wiring ----

  spotAdd.addEventListener("click", () => openSpotForm(null, spotAdd));
  breakAdd.addEventListener("click", () => openBreakForm(null, breakAdd));
  rulesForm.addEventListener("submit", (event) => void saveRules(event));
  reportForm.addEventListener("submit", (event) => void showReport(event));
  get("report-export").addEventListener("click", () => void exportReport());
  // A field the user changes is no longer marked as the bad one.
  const clearInvalid = (event) => {
    const control = event.target;
    if (!control.getAttribute?.("aria-invalid")) return;
    const form = control.closest("form");
    const error = form?.querySelector(".field-error");
    const group = dayBoxes.includes(control) ? dayBoxes : [control];
    for (const item of group) {
      item.removeAttribute("aria-invalid");
      if (error) linkError(item, error, false);
    }
  };
  root.addEventListener("input", clearInvalid);
  spotForm.addEventListener("input", clearInvalid);
  breakForm.addEventListener("input", clearInvalid);
  if (typeof globalThis.MutationObserver === "function") {
    new globalThis.MutationObserver(() => {
      if (shown()) void refresh();
    }).observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  }

  return {
    refresh,
    // Signed out, or another station: forget what this session showed.
    clear() {
      session += 1;
      generation += 1;
      data = null;
      rulesStation = null;
      closeSpotForm();
      closeBreakForm();
      status.textContent = "";
      listFocusAnnouncementSuppressed = true;
      try {
        for (const [list, fallback] of [[spotList, spotAdd], [breakList, breakAdd]]) {
          reconcileRows(list, [], (item) => item.id, () => null, () => {}, { fallback, onFocusLost: ({ target }) => {
            if (!listFocusAnnouncementSuppressed) say(`The ad list changed. Focus moved to ${target?.getAttribute("aria-label") || target?.textContent.trim() || "the Add control"}.`);
          } });
        }
      } finally { listFocusAnnouncementSuppressed = false; }
      get("pending").replaceChildren();
      get("recent").replaceChildren();
      get("totals").replaceChildren();
      get("rows").replaceChildren();
      get("totals-wrap").hidden = true;
      get("rows-wrap").hidden = true;
      get("report-summary").hidden = true;
      get("report-note").hidden = true;
      for (const id of ["reason", "problem"]) get(id).hidden = true;
    },
  };
}
