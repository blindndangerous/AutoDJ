import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent,
} from "./api-client.js";

const RULES = ["artist", "album", "track", "title"];

function controls(els) {
  return [
    ...RULES.flatMap((rule) => [els[`${rule}Tracks`], els[`${rule}Minutes`]]),
    els.useRelatedArtists, els.relatedArtistsFile,
  ].filter(Boolean);
}

function values(els) {
  return {
    artist: { tracks: Number(els.artistTracks.value), minutes: Number(els.artistMinutes.value) },
    album: { tracks: Number(els.albumTracks.value), minutes: Number(els.albumMinutes.value) },
    track: { tracks: Number(els.trackTracks.value), minutes: Number(els.trackMinutes.value) },
    title: { tracks: Number(els.titleTracks.value), minutes: Number(els.titleMinutes.value) },
    use_related_artists: els.useRelatedArtists.checked,
    related_artists_file: els.relatedArtistsFile.value,
  };
}

function validRule(rule) {
  return Number.isInteger(rule.tracks) && rule.tracks >= 0 && rule.tracks <= 100000
    && Number.isFinite(rule.minutes) && rule.minutes >= 0 && rule.minutes <= 10080;
}

function validFormRule(els, rule) {
  return els[`${rule}Tracks`].value.trim() !== ""
    && els[`${rule}Minutes`].value.trim() !== ""
    && validRule(values(els)[rule]);
}

function write(els, incoming) {
  for (const rule of RULES) {
    const value = incoming?.[rule];
    if (!value) continue;
    els[`${rule}Tracks`].value = value.tracks;
    els[`${rule}Minutes`].value = value.minutes;
  }
  els.useRelatedArtists.checked = Boolean(incoming?.use_related_artists);
  els.relatedArtistsFile.value = incoming?.related_artists_file || "";
}

export function applyRepeatSettings(st, els) {
  if (els.defaultFile && typeof st?.playback?.related_artists_default_file === "string") {
    els.defaultFile.textContent = st.playback.related_artists_default_file;
  }
  const incoming = st?.playback?.repeat_rules;
  if (!incoming) return;
  // An edited group belongs to the user until Save succeeds or validation fails.
  if (els.save.disabled) return;
  if (els.dirty && JSON.stringify(values(els)) !== JSON.stringify(els.confirmed)) return;
  els.dirty = false;
  write(els, incoming);
  els.confirmed = values(els);
}

export function resetRepeatSettings(els) {
  for (const control of controls(els)) {
    if (control.type === "checkbox") control.checked = control.defaultChecked;
    else control.value = control.defaultValue;
  }
  els.confirmed = null;
  els.dirty = false;
  if (els.defaultFile) els.defaultFile.textContent = "";
}

export function installRepeatSettings(els, { postSettings, settingsStatus }) {
  els.confirmed = values(els);
  for (const control of controls(els)) {
    for (const event of ["input", "change"]) {
      control.addEventListener(event, () => { els.dirty = true; });
    }
  }
  els.save.addEventListener("click", async () => {
    const epoch = captureAuthenticatedRequestEpoch();
    const body = values(els);
    const invalid = RULES.find((rule) => !validFormRule(els, rule));
    if (invalid) {
      const label = invalid[0].toUpperCase() + invalid.slice(1);
      announceStatus(settingsStatus, `${label} repeat rule needs 0 or a whole number from 1 to 100000 tracks, and 0 or a number up to 10080 minutes.`,
        { dwellMs: 6000, force: true, tone: "error" });
      write(els, els.confirmed);
      els.dirty = false;
      return;
    }
    const saved = await postSettings("/api/playback-settings", { repeat_rules: body }, els.save);
    if (!isAuthenticatedRequestCurrent(epoch)) return;
    if (saved) {
      els.confirmed = body;
      els.dirty = JSON.stringify(values(els)) !== JSON.stringify(body);
    } else {
      write(els, els.confirmed);
      els.dirty = false;
    }
  });
}
