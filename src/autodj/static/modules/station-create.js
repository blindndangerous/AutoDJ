// Settings > Stations (#233): the stations on this server, and New station.
//
// The list and the Start from choice come from the station list the page
// already keeps (stations.js), so they follow the switcher; render() is
// called whenever that list changes.  Start from is AutoDJ defaults or a
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

import { requestJson, withDisabled } from "./api-client.js";
import { announceStatus } from "./live-region.js";
import { knownStations, stationItemText } from "./stations.js";

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

// "Night Owl Radio, off air, ID nightowl"
export function stationRowText(station) {
  return `${stationItemText(station)}, ID ${station.slug}`;
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

  function renderList(all) {
    const rows = all.map((station) => {
      let li = list.querySelector(`[data-station="${station.slug}"]`);
      if (!li) {
        li = doc.createElement("li");
        li.dataset.station = station.slug;
      }
      const text = stationRowText(station);
      if (li.textContent !== text) li.textContent = text;
      return li;
    });
    for (const child of [...list.children]) if (!rows.includes(child)) child.remove();
    rows.forEach((li, index) => {
      if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
    });
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
    if (linked) ids.push(errorText.id);
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
