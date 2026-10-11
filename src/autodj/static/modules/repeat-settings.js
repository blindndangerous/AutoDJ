import { announceStatus } from "./live-region.js";
import {
  captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent,
} from "./api-client.js";

const RULES = ["artist", "album", "track", "title"];
// The names the messages give each rule; the page's legends say
// "Prevent the same artist from repeating within".
const RULE_NAMES = {
  artist: "Same artist", album: "Same album", track: "Same track", title: "Same title",
};
// The server caps artist, album and title at 5000 tracks (each pick walks
// that many plays); the track rule only keeps paths, so it allows more.
const MAX_TRACKS = { artist: 5000, album: 5000, track: 100000, title: 5000 };
const MAX_MINUTES = 10080;
// As in settings-panel.js: after a save, a push that left the server
// before it still holds the old rules, and writing them back would undo
// the save on screen.  Pushes wait for one that agrees with the save;
// ECHO_WAIT_MS only backstops an echo that never comes.
const ECHO_WAIT_MS = 5000;

function controls(els) {
  return [
    ...RULES.flatMap((rule) => [els[`${rule}Tracks`], els[`${rule}Minutes`]]),
    els.useRelatedArtists,
  ];
}

function values(els) {
  const result = {};
  for (const rule of RULES) {
    result[rule] = {
      tracks: Number(els[`${rule}Tracks`].value),
      minutes: Number(els[`${rule}Minutes`].value),
    };
  }
  result.use_related_artists = els.useRelatedArtists.checked;
  return result;
}

function sameRules(a, b) {
  if (!a || !b) return false;
  return RULES.every((rule) => Number(a[rule]?.tracks) === Number(b[rule]?.tracks)
      && Number(a[rule]?.minutes) === Number(b[rule]?.minutes))
    && Boolean(a.use_related_artists) === Boolean(b.use_related_artists);
}

// [control, message] for the first box holding a value the server would
// refuse, or null.  With *target*, only that box is checked.
function invalidField(els, target = null) {
  for (const rule of RULES) {
    const tracks = els[`${rule}Tracks`];
    const minutes = els[`${rule}Minutes`];
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
  return null;
}

function write(els, incoming) {
  for (const rule of RULES) {
    const value = incoming?.[rule];
    if (!value) continue;
    els[`${rule}Tracks`].value = value.tracks;
    els[`${rule}Minutes`].value = value.minutes;
  }
  els.useRelatedArtists.checked = Boolean(incoming?.use_related_artists);
}

// The status line under Use related artists, part of its description;
// hidden while the file has no problem.
function showFileProblem(els, problem) {
  if (!els.fileStatus) return;
  els.fileStatus.textContent = problem || "";
  els.fileStatus.hidden = !problem;
}

function reportInvalid(control, message, settingsStatus) {
  control.setAttribute("aria-invalid", "true");
  control.focus();
  announceStatus(settingsStatus, message, { dwellMs: 6000, force: true, tone: "error" });
}

export function applyRepeatSettings(st, els) {
  const playback = st?.playback;
  if (els.defaultFile && typeof playback?.related_artists_default_file === "string"
      && playback.related_artists_default_file) {
    els.defaultFile.textContent = playback.related_artists_default_file;
  }
  if (playback && "related_artists_problem" in playback) {
    showFileProblem(els, playback.related_artists_problem);
  }
  const incoming = playback?.repeat_rules;
  if (!incoming) return;
  const echo = els.echo;
  if (echo) {
    if (echo.answeredAt === 0) return;
    if (!sameRules(incoming, echo.value) && Date.now() - echo.answeredAt <= ECHO_WAIT_MS) return;
    els.echo = null;
  }
  // Edits belong to the user until they are saved or match the server.
  if (els.dirty && !sameRules(values(els), els.confirmed)) return;
  els.dirty = false;
  write(els, incoming);
  els.confirmed = values(els);
}

export function resetRepeatSettings(els) {
  for (const control of controls(els)) {
    if (control.type === "checkbox") control.checked = control.defaultChecked;
    else control.value = control.defaultValue;
    control.removeAttribute("aria-invalid");
  }
  els.confirmed = null;
  els.dirty = false;
  els.echo = null;
  if (els.defaultFile) els.defaultFile.textContent = els.defaultFileText ?? "";
  showFileProblem(els, "");
}

export function installRepeatSettings(els, { postSettings, settingsStatus }) {
  els.confirmed = values(els);
  els.echo = null;
  els.defaultFileText = els.defaultFile?.textContent ?? "";
  for (const control of controls(els)) {
    for (const event of ["input", "change"]) {
      control.addEventListener(event, () => {
        els.dirty = true;
        if (!invalidField(els, control)) control.removeAttribute("aria-invalid");
      });
    }
  }
  els.save.addEventListener("click", async () => {
    const invalid = invalidField(els);
    if (invalid) {
      reportInvalid(invalid[0], invalid[1], settingsStatus);
      return;
    }
    const epoch = captureAuthenticatedRequestEpoch();
    const body = values(els);
    const echo = { value: body, answeredAt: 0 };
    els.echo = echo;
    let failure = null;
    const saved = await postSettings("/api/playback-settings", { repeat_rules: body }, els.save,
      { onError: (err) => { failure = err; } });
    if (!isAuthenticatedRequestCurrent(epoch)) {
      if (els.echo === echo) els.echo = null;
      return;
    }
    if (saved) {
      echo.answeredAt = Date.now();
      els.confirmed = body;
      els.dirty = !sameRules(values(els), body);
      for (const control of controls(els)) control.removeAttribute("aria-invalid");
      showFileProblem(els, "");
      announceStatus(settingsStatus, "Repeat settings saved.", { dwellMs: 4000, force: true });
      return;
    }
    // The edits stay in the form, to fix and save again.
    if (els.echo === echo) els.echo = null;
    els.dirty = true;
    const detail = failure?.message || "";
    if (failure?.status === 400 && body.use_related_artists) {
      // The boxes were checked above, so the server refused the file.
      showFileProblem(els, detail);
      reportInvalid(els.useRelatedArtists,
        `Use related artists: ${detail} Fix the file or turn this off, then save again.`,
        settingsStatus);
      return;
    }
    announceStatus(settingsStatus,
      `Repeat settings were not saved. ${detail} Your changes are still in the form.`
        .replace(/\s+/g, " "),
      { dwellMs: 6000, force: true, tone: "error" });
  });
}
