// Settings > Profiles: save the current settings under a name, apply a
// saved profile, delete one.
//
// The list loads when the Profiles card is opened.  Every result is said
// once through #profiles-status.  Focus stays on the control that was
// pressed, except after Delete, where that row is gone and focus moves to
// the neighbouring row (or the name field when the list is empty).

import {
  captureAuthenticatedRequestEpoch,
  isAuthenticatedRequestCurrent,
  requestJson,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { replaceRows } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";

// profiles.validate_name on the server.
const NAME_PATTERN = /^[A-Za-z0-9 _-]{1,64}$/;

// The settings a profile carries, read from the websocket settings
// snapshot.  Only fields the server's ProfileSaveBody declares: the
// server refuses unknown keys.
export function profileFromSettings(name, st) {
  const pb = st.playback || {};
  const bpm = st.bpm_range || {};
  const body = {
    name,
    preset: st.preset ?? null,
    bpm_lo: bpm.lo ?? null,
    bpm_hi: bpm.hi ?? null,
    harmonic_mode: st.djmix?.harmonic_mode ?? null,
  };
  for (const key of [
    "transition_mode", "post_queue_seed", "beat_sync_fx", "key_sync_fx",
    "beatmatch_on_skip", "crossfade_seconds", "fade_in_seconds",
    "smart_shuffle", "pure_shuffle", "anchor_to_seed", "enable_daypart",
    "enable_mood_arc", "mood_arc_hours", "liners_enabled", "liners_pick_mode",
  ]) {
    body[key] = pb[key] ?? null;
  }
  return body;
}

export function installProfiles(els, { getSettings }) {
  const { card, nameInput, saveButton, list, status } = els;
  if (!card || !nameInput || !saveButton || !list || !status) return;
  const doc = card.ownerDocument;
  let names = [];

  const say = (message, tone = "info") => {
    announceStatus(status, message, { dwellMs: tone === "error" ? 6000 : 3000, force: true, tone });
  };

  // `focus` comes from a delete: see replaceRows.
  function render(focus = null) {
    if (names.length === 0) {
      const empty = doc.createElement("li");
      empty.className = "no-results";
      empty.textContent = "No saved profiles yet.";
      replaceRows(list, [empty], focus);
      return;
    }
    const rows = [];
    for (const name of names) {
      const row = doc.createElement("li");
      const label = doc.createElement("span");
      label.className = "profile-name";
      label.textContent = name;
      const apply = doc.createElement("button");
      apply.type = "button";
      apply.textContent = "Apply";
      apply.setAttribute("aria-label", `Apply profile ${name}`);
      apply.addEventListener("click", () => void applyProfile(name));
      const remove = doc.createElement("button");
      remove.type = "button";
      remove.textContent = "Delete";
      remove.dataset.profile = name;
      remove.setAttribute("aria-label", `Delete profile ${name}`);
      remove.addEventListener("click", () => void deleteProfile(name, remove));
      row.append(label, " ", apply, " ", remove);
      rows.push(row);
    }
    replaceRows(list, rows, focus);
  }

  async function load(focus = null) {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const body = await requestJson("/api/profiles");
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      names = Array.isArray(body.profiles) ? body.profiles : [];
      render(focus);
      return true;
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      say(`Could not load profiles: ${errorValue.message}`, "error");
      return false;
    }
  }

  async function save() {
    const trigger = doc.activeElement === nameInput ? nameInput : saveButton;
    const name = nameInput.value.trim();
    if (!NAME_PATTERN.test(name)) {
      nameInput.setAttribute("aria-invalid", "true");
      say("Could not save: a profile name needs 1 to 64 letters, digits, spaces, dashes or underscores.", "error");
      return;
    }
    nameInput.removeAttribute("aria-invalid");
    const settings = getSettings();
    if (!settings) {
      say("Could not save: the settings have not loaded yet.  Try again in a moment.", "error");
      return;
    }
    const exists = names.some((other) => other.toLowerCase() === name.toLowerCase());
    if (exists) {
      const confirmed = await confirmAction(doc, {
        title: `Replace profile ${name}?`,
        message: "A profile with this name is already saved.  Saving replaces it with the current settings.",
        confirmLabel: "Replace profile",
      });
      trigger.focus();
      if (!confirmed) return;
    }
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson("/api/profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(profileFromSettings(name, settings)),
      });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      await load();
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(exists ? `Replaced profile ${name}.` : `Saved profile ${name}.`);
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Could not save profile ${name}: ${errorValue.message}`, "error");
    }
  }

  async function applyProfile(name) {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson(`/api/profiles/${encodeURIComponent(name)}/apply`, { method: "POST" });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Applied profile ${name}.`);
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Could not apply profile ${name}: ${errorValue.message}`, "error");
    }
  }

  async function deleteProfile(name, button) {
    const confirmed = await confirmAction(doc, {
      title: `Delete profile ${name}?`,
      message: "The saved settings are removed.  The settings in use now do not change.",
      confirmLabel: "Delete profile",
    });
    button.focus();
    if (!confirmed) return;
    const index = names.indexOf(name);
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      await requestJson(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      await load({ from: button, selector: "button[data-profile]", index, fallback: nameInput });
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Deleted profile ${name}.`);
    } catch (errorValue) {
      if (!isAuthenticatedRequestCurrent(epoch)) return;
      say(`Could not delete profile ${name}: ${errorValue.message}`, "error");
    }
  }

  saveButton.addEventListener("click", () => void save());
  nameInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    void save();
  });
  card.addEventListener("toggle", () => {
    if (card.open) void load();
  });
  if (card.open) void load();
}
