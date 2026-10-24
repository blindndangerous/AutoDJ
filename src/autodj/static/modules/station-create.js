// Settings > Stations (#233): the stations on this server, and New station.
//
// The table and the Start from choice come from the station list the page
// already keeps (stations.js), so they follow the switcher; render() is
// called whenever that list changes.  Each row is built once and its cells
// change in place, so focus stays where it is.  Start with the server and
// Start or Stop say their result once through #stations-status; Stop asks
// first, since it takes the station off the air.  Start from is AutoDJ defaults or a
// copy of a station; Also copy (clockwheels, schedule, carts) shows only
// for a copy.  The server checks everything again (POST /api/stations).
//
// A refused field gets aria-invalid, the error words linked to it, and
// focus, so the field and its error are read together once; nothing else
// says it.  An error that names no field is said once through
// #stations-status and shown above the form.  A created station is said
// through #stations-status, and focus moves to Switch to <name>, the next
// step: the station is created stopped, to be reviewed and then started.
// Switching moves focus to the Station button, which names the station.

import {
  captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson, withDisabled,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";
import { isOnAir, knownStations } from "./stations.js";
import { installTableNavigation } from "./table-navigation.js";
import { tableRow } from "./table-row.js";

const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$/;
const NAME_MAX = 80;
const FIELD_IDS = Object.freeze({
  source: "station-create-source",
  name: "station-create-name",
  slug: "station-create-slug",
});
const COPY_IDS = Object.freeze({
  copy_clockwheels: "station-create-clockwheels",
  copy_schedule: "station-create-schedule",
  copy_carts: "station-create-carts",
});

// A station ID made from a name: "Night Owl Radio" -> "night-owl-radio".
// Empty when nothing usable is left.
export function slugFromName(name) {
  const slug = String(name || "")
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32)
    .replace(/-+$/, "");
  return SLUG_PATTERN.test(slug) ? slug : "";
}

// What is wrong with the form before it is sent, or null: {field, message}.
export function checkNewStation({ slug, name }, stations) {
  const trimmed = name.trim();
  if (!trimmed) return { field: "name", message: "Enter a name for the new station." };
  if (trimmed.length > NAME_MAX) {
    return { field: "name", message: `Use a name of up to ${NAME_MAX} characters.` };
  }
  const folded = trimmed.toLowerCase();
  const sameName = stations.find((station) => String(station.name).trim().toLowerCase() === folded);
  if (sameName) {
    return { field: "name", message: `${sameName.name} already has that name. Choose another name.` };
  }
  if (!SLUG_PATTERN.test(slug)) {
    return {
      field: "slug",
      message: "Use lower-case letters, digits and hyphens for the Station ID, up to 32, starting and ending with a letter or digit.",
    };
  }
  if (slug === "default" || stations.some((station) => station.slug === slug)) {
    return { field: "slug", message: `A station already has the ID ${slug}. Choose another ID.` };
  }
  return null;
}

// The Now playing and Listeners cells of a station's row.
export function stationCells(station) {
  if (!isOnAir(station)) return [station.state === "stopped" ? "Off air" : "Stopping", ""];
  return [station.title || "Nothing playing", String(station.listeners ?? 0)];
}

// "Created Night Owl Radio, off air. Copied saved settings and carts."
export function createdText(entry) {
  const copied = Array.isArray(entry.copied) ? entry.copied.filter(Boolean) : [];
  let text = `Created ${entry.name}, off air.`;
  if (copied.length === 1) text += ` Copied ${copied[0]}.`;
  else if (copied.length > 1) {
    text += ` Copied ${copied.slice(0, -1).join(", ")} and ${copied[copied.length - 1]}.`;
  }
  return text;
}

export function installStationCreator({
  doc = document,
  request = requestJson,
  stations = knownStations,
  onCreated = async () => {},
  onChanged = async () => {},
  onSwitch = async () => false,
} = {}) {
  const get = (id) => doc.getElementById(id);
  const panel = get("spanel-stations");
  const status = get("stations-status");
  const list = get("stations-list");
  const form = get("station-create-form");
  const errorText = get("station-create-error");
  const source = get(FIELD_IDS.source);
  const nameInput = get(FIELD_IDS.name);
  const slugInput = get(FIELD_IDS.slug);
  const copies = get("station-create-copies");
  const autostart = get("station-create-autostart");
  const submit = get("station-create-submit");
  const done = get("station-create-done");
  const doneText = get("station-create-done-text");
  const switchButton = get("station-create-switch");
  if (!panel || !status || !list || !form || !errorText || !source || !nameInput || !slugInput
      || !copies || !autostart || !submit || !done || !doneText || !switchButton) {
    return { render: () => {} };
  }
  let createdSlug = null;
  const navigation = installTableNavigation(list);
  const say = (text, error = false) =>
    announceStatus(status, text, { force: true, dwellMs: 6000, tone: error ? "error" : "info" });

  function syncAutostart(row) {
    const box = row.children[3].querySelector("input");
    const save = row.autostartActions.querySelector("button");
    const discard = row.autostartActions.querySelector("button:last-child");
    const changed = row.autostartDraft !== null
      && row.autostartDraft !== row.autostartBaseline;
    row.autostartActions.hidden = !changed;
    save.disabled = row.autostartSaving || !changed;
    discard.disabled = row.autostartSaving || !changed;
    box.disabled = row.station.autostart_locked || row.autostartSaving;
  }

  function editAutostart(row) {
    const box = row.children[3].querySelector("input");
    row.autostartDraft = box.checked === row.autostartBaseline ? null : box.checked;
    syncAutostart(row);
  }

  function discardAutostart(row) {
    if (row.autostartSaving) return;
    row.autostartDraft = null;
    row.children[3].querySelector("input").checked = row.autostartBaseline;
    syncAutostart(row);
    say(`Discarded the Start with the server change for ${row.station.name}.`);
  }

  async function saveAutostart(row) {
    if (row.autostartSaving || row.autostartDraft === null) return;
    const station = row.station;
    const enabled = row.autostartDraft;
    const epoch = captureAuthenticatedRequestEpoch();
    const pending = {};
    row.autostartSaving = pending;
    syncAutostart(row);
    try {
      await request(`/api/stations/${encodeURIComponent(station.slug)}/autostart`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
    } catch (errorValue) {
      if (isAuthenticatedRequestCurrent(epoch) && row.isConnected && row.station.slug === station.slug) {
        say(`Could not save the Start with the server change for ${station.name}. ${errorValue?.message || ""}`.trim(), true);
      }
      return;
    } finally {
      if (row.autostartSaving === pending) {
        row.autostartSaving = null;
        syncAutostart(row);
      }
    }
    if (!isAuthenticatedRequestCurrent(epoch) || !row.isConnected || row.station.slug !== station.slug) return;
    row.autostartBaseline = enabled;
    row.autostartDraft = null;
    row.station.autostart = enabled;
    syncAutostart(row);
    say(enabled ? `${station.name} starts with the server.`
      : `${station.name} no longer starts with the server.`);
    await onChanged();
  }

  async function startOrStop(station) {
    const stop = isOnAir(station);
    if (stop && !await confirmAction(doc, {
      title: `Stop ${station.name}?`,
      message: "It goes off the air and its listeners are disconnected.",
      confirmLabel: "Stop station",
    })) return;
    let entry;
    try {
      entry = await request(`/api/stations/${encodeURIComponent(station.slug)}/${stop ? "stop" : "start"}`,
        { method: "POST" });
    } catch (errorValue) {
      say(`Could not ${stop ? "stop" : "start"} ${station.name}. ${errorValue?.message || ""}`.trim(), true);
      return;
    }
    const warnings = Array.isArray(entry?.warnings) ? entry.warnings.join(" ") : "";
    say(`${stop ? "Stopped" : "Started"} ${station.name}.${warnings ? ` ${warnings}` : ""}`);
    await onChanged();
  }

  function stationRow(station) {
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.addEventListener("change", () => editAutostart(row));
    const actions = doc.createElement("div");
    actions.setAttribute("role", "group");
    actions.className = "dialog-actions";
    const save = doc.createElement("button");
    save.type = "button";
    save.textContent = "Save";
    save.addEventListener("click", () => void saveAutostart(row));
    const discard = doc.createElement("button");
    discard.type = "button";
    discard.textContent = "Discard";
    discard.addEventListener("click", () => discardAutostart(row));
    actions.append(save, discard);
    const button = doc.createElement("button");
    button.type = "button";
    button.addEventListener("click", () => void withDisabled(button, () => startOrStop(row.station)));
    const row = tableRow(doc, ["", "", "", [box, actions], button]);
    row.dataset.station = station.slug;
    row.station = station;
    row.autostartBaseline = Boolean(station.autostart);
    row.autostartDraft = null;
    row.autostartSaving = null;
    row.autostartActions = actions;
    return row;
  }

  function fillRow(row, station) {
    row.station = station;
    const cells = [...row.children];
    const [nameCell, playingCell, listenersCell] = cells;
    const [playing, listeners] = stationCells(station);
    for (const [cell, text] of [[nameCell, station.name], [playingCell, playing], [listenersCell, listeners]]) {
      if (cell.textContent !== text) cell.textContent = text;
    }
    const box = cells[3].querySelector("input");
    const serverValue = Boolean(station.autostart);
    if (row.autostartDraft === null || row.autostartDraft === serverValue) {
      row.autostartDraft = null;
      box.checked = serverValue;
    } else {
      box.checked = row.autostartDraft;
    }
    row.autostartBaseline = serverValue;
    box.setAttribute("aria-label", `Start ${station.name} with the server`);
    row.autostartActions.setAttribute("aria-label", `Start ${station.name} with the server`);
    const [save, discard] = row.autostartActions.querySelectorAll("button");
    save.setAttribute("aria-label", `Save Start with the server for ${station.name}`);
    discard.setAttribute("aria-label", `Discard Start with the server change for ${station.name}`);
    syncAutostart(row);
    const button = cells[4].querySelector("button");
    button.hidden = false;
    const action = isOnAir(station) ? "Stop" : "Start";
    if (button.textContent !== action) button.textContent = action;
    button.setAttribute("aria-label", `${action} ${station.name}`);
  }

  function renderList(all) {
    const rows = all.map((station) => {
      const row = list.querySelector(`[data-station="${station.slug}"]`) || stationRow(station);
      fillRow(row, station);
      return row;
    });
    for (const child of [...list.children]) if (!rows.includes(child)) child.remove();
    rows.forEach((row, index) => {
      if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    });
    navigation.refresh();
  }

  function renderSources(all) {
    const chosen = source.value;
    const wanted = [["", "AutoDJ defaults"],
      ...all.map((station) => [station.slug, `Copy of ${station.name}`])];
    const options = [...source.options];
    wanted.forEach(([value, text], index) => {
      let option = options.find((item) => item.value === value);
      if (!option) {
        option = doc.createElement("option");
        option.value = value;
      }
      if (option.textContent !== text) option.textContent = text;
      if (source.options[index] !== option) source.insertBefore(option, source.options[index] || null);
    });
    for (const option of [...source.options]) {
      if (!wanted.some(([value]) => value === option.value)) option.remove();
    }
    source.value = wanted.some(([value]) => value === chosen) ? chosen : "";
    showCopies();
  }

  function showCopies() {
    const copy = source.value !== "";
    if (copies.hidden === copy) copies.hidden = !copy;
  }

  function render() {
    const all = stations();
    renderList(all);
    renderSources(all);
  }

  function linkError(control, linked) {
    const ids = (control.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== errorText.id);
    if (linked) ids.unshift(errorText.id);
    if (ids.length) control.setAttribute("aria-describedby", ids.join(" "));
    else control.removeAttribute("aria-describedby");
  }

  function clearErrors() {
    for (const id of Object.values(FIELD_IDS)) {
      const control = get(id);
      control.removeAttribute("aria-invalid");
      linkError(control, false);
    }
    errorText.textContent = "";
    errorText.hidden = true;
  }

  function showError(field, message) {
    clearErrors();
    errorText.textContent = message;
    errorText.hidden = false;
    const control = field ? get(FIELD_IDS[field]) : null;
    if (control) {
      control.setAttribute("aria-invalid", "true");
      linkError(control, true);
      control.focus();
    } else {
      announceStatus(status, message, { force: true, dwellMs: 6000, tone: "error" });
    }
  }

  function hideDone() {
    createdSlug = null;
    done.hidden = true;
    doneText.textContent = "";
  }

  function body() {
    const copy = source.value !== "";
    const payload = {
      slug: slugInput.value.trim(),
      name: nameInput.value.trim(),
      source: copy ? source.value : null,
      autostart: autostart.checked,
    };
    for (const [key, id] of Object.entries(COPY_IDS)) payload[key] = copy && get(id).checked;
    return payload;
  }

  function reset() {
    nameInput.value = "";
    slugInput.value = "";
    source.value = "";
    autostart.checked = false;
    for (const id of Object.values(COPY_IDS)) get(id).checked = false;
    showCopies();
  }

  async function create() {
    if (!slugInput.value.trim()) slugInput.value = slugFromName(nameInput.value);
    const payload = body();
    const problem = checkNewStation(payload, stations());
    if (problem) {
      showError(problem.field, problem.message);
      return;
    }
    clearErrors();
    hideDone();
    let entry;
    try {
      entry = await request("/api/stations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (errorValue) {
      const field = errorValue?.field in FIELD_IDS ? errorValue.field : null;
      showError(field, `Could not create the station. ${errorValue?.message || ""}`.trim());
      return;
    }
    reset();
    createdSlug = entry.slug;
    await onCreated(entry);
    render();
    doneText.textContent = `${entry.name} is off air until you start it.`;
    switchButton.textContent = `Switch to ${entry.name}`;
    done.hidden = false;
    switchButton.focus();
    announceStatus(status, createdText(entry), { force: true, dwellMs: 6000 });
  }

  source.addEventListener("change", showCopies);
  for (const control of [nameInput, slugInput]) {
    control.addEventListener("input", () => {
      if (control.getAttribute("aria-invalid") !== "true") return;
      control.removeAttribute("aria-invalid");
      linkError(control, false);
    });
  }
  // A name typed with the ID still empty suggests an ID, once the name is left.
  nameInput.addEventListener("change", () => {
    if (!slugInput.value.trim()) slugInput.value = slugFromName(nameInput.value);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void withDisabled(submit, create);
  });
  // onSwitch moves focus once the page controls the station; a switch
  // turned down (browser playback asks first) leaves the button in place.
  switchButton.addEventListener("click", () => void withDisabled(switchButton, async () => {
    const slug = createdSlug;
    if (slug && await onSwitch(slug)) hideDone();
  }));
  panel.addEventListener("toggle", () => {
    if (panel.open) render();
  });
  render();
  return { render };
}
