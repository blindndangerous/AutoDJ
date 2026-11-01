import { clearFieldError, showFieldError } from "./dom-helpers.js";
import { registerSettingsGroup } from "./settings-drafts.js";

const RULES = ["artist", "related_artist", "album", "track", "title", "related_track"];
// Each rule's boxes are els[`${PREFIX[rule]}Tracks`] and els[`${PREFIX[rule]}Minutes`].
const PREFIX = {
  artist: "artist", related_artist: "relatedArtist", album: "album", track: "track",
  title: "title", related_track: "relatedTrack",
};
// The names the messages give each rule; the page's legends say
// "Prevent the same artist from repeating within".
const RULE_NAMES = {
  artist: "Same artist", related_artist: "Related artists", album: "Same album",
  track: "Same track", title: "Same title", related_track: "Related tracks",
};
// The server caps every rule but the track rule at 5000 tracks (each pick
// walks that many plays); the track rule only keeps paths, so it allows more.
const MAX_TRACKS = {
  artist: 5000, related_artist: 5000, album: 5000, track: 100000, title: 5000,
  related_track: 5000,
};

const box = (els, rule, part) => els[`${PREFIX[rule]}${part}`];
const MAX_MINUTES = 10080;
// The daypart and quarter-hour rules (#129); the server caps them the same.
const MAX_DAYPART_DAYS = 7;
const MAX_HOURPART_PLAYS = 3;
const HOURS_MESSAGE = "Daypart start hours must be whole numbers from 0 to 23, "
  + "separated by commas, with no hour given twice.";

function controls(els) {
  return [
    ...RULES.flatMap((rule) => [box(els, rule, "Tracks"), box(els, rule, "Minutes")]),
    els.useRelatedArtists,
    els.useRelatedTracks,
    els.versionsAsOneSong,
    els.daypartDays,
    els.daypartHours,
    els.hourpartPlays,
  ];
}

// The daypart start hours typed as "0, 6, 10", in order.  A part that is
// not a number reads as NaN, which hoursValid refuses.
function parseHours(text) {
  const parts = String(text).split(",").map((part) => part.trim());
  if (parts.length === 1 && parts[0] === "") return [];
  return parts.map((part) => (/^\d+$/.test(part) ? Number(part) : Number.NaN))
    .sort((a, b) => a - b);
}

function hoursValid(hours) {
  return hours.length >= 1 && hours.length <= 24
    && hours.every((hour) => Number.isInteger(hour) && hour >= 0 && hour <= 23)
    && new Set(hours).size === hours.length;
}

function wholeIn(control, max) {
  const value = Number(control.value);
  return control.value.trim() !== "" && Number.isInteger(value) && value >= 0 && value <= max;
}

function values(els) {
  const result = {};
  for (const rule of RULES) {
    result[rule] = {
      tracks: Number(box(els, rule, "Tracks").value),
      minutes: Number(box(els, rule, "Minutes").value),
    };
  }
  result.use_related_artists = els.useRelatedArtists.checked;
  result.use_related_tracks = els.useRelatedTracks.checked;
  result.versions_as_one_song = els.versionsAsOneSong.checked;
  result.daypart = {
    days: Number(els.daypartDays.value),
    start_hours: parseHours(els.daypartHours.value),
  };
  result.hourpart = { plays: Number(els.hourpartPlays.value) };
  return result;
}

// [control, message] for the first box holding a value the server would
// refuse, or null.  With *target*, only that box is checked.
function invalidField(els, target = null) {
  for (const rule of RULES) {
    const tracks = box(els, rule, "Tracks");
    const minutes = box(els, rule, "Minutes");
    const trackValue = Number(tracks.value);
    const minuteValue = Number(minutes.value);
    const maxTracks = MAX_TRACKS[rule];
    if ((!target || target === tracks) && (tracks.value.trim() === ""
        || !Number.isInteger(trackValue) || trackValue < 0 || trackValue > maxTracks)) {
      return [tracks,
        `${RULE_NAMES[rule]} tracks must be a whole number from 0 to ${maxTracks}.`];
    }
    if ((!target || target === minutes) && (minutes.value.trim() === ""
        || !Number.isFinite(minuteValue) || minuteValue < 0 || minuteValue > MAX_MINUTES)) {
      return [minutes, `${RULE_NAMES[rule]} minutes must be a number from 0 to ${MAX_MINUTES}.`];
    }
  }
  if ((!target || target === els.daypartDays) && !wholeIn(els.daypartDays, MAX_DAYPART_DAYS)) {
    return [els.daypartDays,
      `Same daypart days must be a whole number from 0 to ${MAX_DAYPART_DAYS}.`];
  }
  if ((!target || target === els.daypartHours) && !hoursValid(parseHours(els.daypartHours.value))) {
    return [els.daypartHours, HOURS_MESSAGE];
  }
  if ((!target || target === els.hourpartPlays)
      && !wholeIn(els.hourpartPlays, MAX_HOURPART_PLAYS)) {
    return [els.hourpartPlays,
      `Same quarter hour plays must be a whole number from 0 to ${MAX_HOURPART_PLAYS}.`];
  }
  return null;
}

function write(els, incoming) {
  for (const rule of RULES) {
    const value = incoming?.[rule];
    if (!value) continue;
    box(els, rule, "Tracks").value = value.tracks;
    box(els, rule, "Minutes").value = value.minutes;
  }
  els.useRelatedArtists.checked = Boolean(incoming?.use_related_artists);
  els.useRelatedTracks.checked = Boolean(incoming?.use_related_tracks);
  els.versionsAsOneSong.checked = Boolean(incoming?.versions_as_one_song);
  if (incoming?.daypart) {
    els.daypartDays.value = incoming.daypart.days;
    if (Array.isArray(incoming.daypart.start_hours)) {
      els.daypartHours.value = incoming.daypart.start_hours.join(", ");
    }
  }
  if (incoming?.hourpart) els.hourpartPlays.value = incoming.hourpart.plays;
}

// The status line under Use related artists or Use related tracks, part
// of its description; hidden while the file has no problem.
function showStatus(status, problem) {
  if (!status) return;
  status.textContent = problem || "";
  status.hidden = !problem;
}

function showDefaultFile(element, path) {
  if (element && typeof path === "string" && path) element.textContent = path;
}

function clearError(els, control = els.errorFor) {
  if (!control || els.errorFor !== control) return;
  clearFieldError(control, els.error);
  els.errorFor = null;
}

export function applyRepeatSettings(st, els, { acceptDraft = true } = {}) {
  const playback = st?.playback;
  showDefaultFile(els.defaultFile, playback?.related_artists_default_file);
  showDefaultFile(els.tracksDefaultFile, playback?.related_tracks_default_file);
  if (playback && "related_artists_problem" in playback) {
    showStatus(els.fileStatus, playback.related_artists_problem);
  }
  if (playback && "related_tracks_problem" in playback) {
    showStatus(els.tracksFileStatus, playback.related_tracks_problem);
  }
  if (acceptDraft && playback?.repeat_rules) els.draft?.accept({ repeat_rules: playback.repeat_rules });
}

export function resetRepeatSettings(els) {
  for (const control of controls(els)) {
    if (control.type === "checkbox") control.checked = control.defaultChecked;
    else control.value = control.defaultValue;
    clearFieldError(control, els.error);
  }
  clearError(els);
  els.confirmed = null;
  els.dirty = false;
  els.echo = null;
  if (els.defaultFile) els.defaultFile.textContent = els.defaultFileText ?? "";
  if (els.tracksDefaultFile) els.tracksDefaultFile.textContent = els.tracksDefaultFileText ?? "";
  showStatus(els.fileStatus, "");
  showStatus(els.tracksFileStatus, "");
}

export function installRepeatSettings(els, { settingsStatus }) {
  els.defaultFileText = els.defaultFile?.textContent ?? "";
  els.tracksDefaultFileText = els.tracksDefaultFile?.textContent ?? "";
  els.draft = registerSettingsGroup({
    panel: controls(els).find(Boolean)?.closest('[id^="spanel-"]'), group: "playback", controls: controls(els),
    read: () => ({ repeat_rules: values(els) }),
    write(value) { if (value.repeat_rules) write(els, value.repeat_rules); },
    validate() {
      const invalid = invalidField(els);
      if (!invalid) { clearError(els); return true; }
      clearError(els);
      els.errorFor = invalid[0];
      showFieldError(invalid[0], invalid[1], els.error, settingsStatus);
      return false;
    },
  });
  for (const control of controls(els)) control.addEventListener("input", () => {
    if (!invalidField(els, control)) clearError(els, control);
  });
}
