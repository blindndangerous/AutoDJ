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
import { listRow, nextRowControl, rowButton } from "./dom-helpers.js";
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
    "smart_shuffle", "pure_shuffle", "anchor_to_seed", "liners_enabled",
    "liners_pick_mode",
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

  function render() {
    if (names.length === 0) {
      const empty = doc.createElement("li");
      empty.className = "no-results";
      empty.textContent = "No saved profiles yet.";
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...names.map((name) => {
      const apply = rowButton(doc, "Apply", `Apply profile ${name}`, () => void applyProfile(name));
      const remove = rowButton(doc, "Delete", `Delete profile ${name}`,
        (button) => void deleteProfile(name, button));
      remove.dataset.profile = name;
      return listRow(doc, name, "profile-name", apply, remove);
    }));
  }

  async function load() {
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const body = await requestJson("/api/profiles");
      if (!isAuthenticatedRequestCurrent(epoch)) return false;
      names = Array.isArray(body.profiles) ? body.profiles : [];
      render();
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

  // The delete runs while the confirmation is still open, so the list is
  // already updated when it closes and focus goes straight to the next
  // row's Delete, or the name field when none is left (see confirmAction).
  // The result is said after the dialog has closed: the status region is
  // behind it until then.
  async function deleteProfile(name, button) {
    const index = names.indexOf(name);
    const epoch = captureAuthenticatedRequestEpoch();
    let result = null;
    const confirmed = await confirmAction(doc, {
      title: `Delete profile ${name}?`,
      message: "The saved settings are removed.  The settings in use now do not change.",
      confirmLabel: "Delete profile",
      onConfirm: async () => {
        try {
          await requestJson(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          await load();
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          result = [`Deleted profile ${name}.`];
          return nextRowControl(list, "button[data-profile]", index, nameInput);
        } catch (errorValue) {
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          result = [`Could not delete profile ${name}: ${errorValue.message}`, "error"];
          return button;
        }
      },
    });
    if (!confirmed) {
      button.focus();
      return;
    }
    if (result && isAuthenticatedRequestCurrent(epoch)) say(...result);
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
