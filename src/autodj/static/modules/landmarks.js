// Spoken track landmarks (#60): a song's structure through keys and speech,
// in place of a waveform.
//
// The server merges the song's cue points, its DJ analysis (drops,
// breakdowns, build-ups, phrases, the beat grid for bar numbers) and its
// synced lyrics into named landmarks (/api/landmarks, a station route).
// This module:
//
// - draws Track info's Song structure: a real list, one landmark per item,
//   in time order, as plain text.  Times are numeric (1:35) on screen and
//   in speech alike, per the UI wording guidance.  The list is never live;
//   it is drawn when a song opens.
// - moves to the next or previous landmark (Right and Left bracket).  With
//   Track info showing, the cue point preview moves there ("Preview: Drop,
//   1:35, bar 49").  On any other tab the keys speak about
//   the song on air ("On air: Drop, 1:35, bar 49, in 0:12") and only move it when Settings, Keyboard, "Bracket keys seek
//   the song on air" is on (off by default): a seek names its song, and the
//   server refuses it once another song plays.  Each answer is said once
//   through the page's polite #sr-status, and focus stays where it is.
// - says what is coming next on air (Shift+U): "On air: Drop in 0:12,
//   6 bars".  Phrases and unnamed cues are stops for the bracket
//   keys only, never "coming next".
//
// The song on air is asked about afresh on every key; Track info keeps the
// answer for the song it shows until it opens another or is refreshed.

import { isStaleStationReply, requestJson } from "./api-client.js";
import { fmtTime, srSpeak } from "./dom-helpers.js";
import { stationEpoch } from "./stations.js";

// A landmark this close ahead counts as where you are, not the next one.
const AHEAD_S = 0.25;
// Previous goes back to the start of the landmark you are in, unless you
// are within this many seconds of it, as a media player's Previous does.
const BACK_GRACE_S = 1.0;

// Leads for "coming next" that read better than the bare name.
const SOON = {
  intro: "Intro starts",
  intro_end: "Intro ends",
  vocals: "Vocals start",
  outro: "Outro starts",
};

// "Drop, 1:35, bar 49": numeric times on screen and in speech alike, as
// fmtTime writes them (docs/accessibility-testing.md, UI wording).
export function landmarkSpeech(landmark) {
  const bar = landmark.bar ? `, bar ${landmark.bar}` : "";
  return `${landmark.name}, ${fmtTime(landmark.time_s)}${bar}`;
}

export function nextLandmark(landmarks, position) {
  return (landmarks || []).find((item) => item.time_s > position + AHEAD_S) || null;
}

export function previousLandmark(landmarks, position) {
  const list = landmarks || [];
  for (let index = list.length - 1; index >= 0; index -= 1) {
    if (list[index].time_s < position - BACK_GRACE_S) return list[index];
  }
  return null;
}

// Whole bars in *seconds* at *bpm*, four beats a bar; null when unknown or
// under one bar.
export function barsIn(seconds, bpm) {
  if (!(bpm > 0) || !(seconds > 0)) return null;
  const bars = Math.round((seconds * bpm) / 240);
  return bars >= 1 ? bars : null;
}

// What is coming next from *position*: "Drop in 12 seconds, 4 bars".
export function upcomingSpeech(landmarks, position, { bpm = 0, duration = 0 } = {}) {
  const list = landmarks || [];
  if (!list.length) return "No landmarks for this song yet.";
  const next = list.find((item) => item.major && item.time_s > position + AHEAD_S);
  if (!next) {
    // Phrases ahead are still stops for the bracket keys, so with some
    // left the words say only the major ones are over.
    const what = nextLandmark(list, position) ? "major landmarks" : "landmarks";
    const left = duration - position;
    return left >= 1
      ? `No more ${what}. The song ends in ${fmtTime(left)}.`
      : `No more ${what} in this song.`;
  }
  const lead = (next.kinds || [next.kind]).length === 1 && SOON[next.kind] ? SOON[next.kind] : next.name;
  const seconds = next.time_s - position;
  if (seconds < 1) return `${lead} now.`;
  const bars = barsIn(seconds, bpm);
  return `${lead} in ${fmtTime(seconds)}${bars ? `, ${bars} bar${bars === 1 ? "" : "s"}` : ""}.`;
}

// One Song structure item: "Intro, 0:00 to 0:32, bars 1 to 17",
// "Vocals from 0:45, bar 23", "Drop, 1:35, bar 49".
export function structureText(landmark) {
  const single = (landmark.kinds || [landmark.kind]).length === 1;
  if (landmark.end_s != null) {
    const bars = landmark.bar && landmark.end_bar ? `, bars ${landmark.bar} to ${landmark.end_bar}` : "";
    return `${landmark.name}, ${fmtTime(landmark.time_s)} to ${fmtTime(landmark.end_s)}${bars}`;
  }
  const bar = landmark.bar ? `, bar ${landmark.bar}` : "";
  if (single && landmark.kind === "vocals") return `Vocals from ${fmtTime(landmark.time_s)}${bar}`;
  return `${landmark.name}, ${fmtTime(landmark.time_s)}${bar}`;
}

// The structure list leaves out "Intro ends" on its own: the intro's item
// already says "Intro, 0:00 to 0:32".  It stays a stop for the keys and a
// "coming next" for Shift+U.
function inStructure(item) {
  return item.major && !(item.kind === "intro_end" && (item.kinds || [item.kind]).length === 1);
}

export function renderStructure(listEl, landmarks) {
  if (!listEl) return 0;
  const doc = listEl.ownerDocument;
  const items = (landmarks || []).filter(inStructure);
  listEl.replaceChildren(...items.map((landmark) => {
    const item = doc.createElement("li");
    item.textContent = structureText(landmark);
    return item;
  }));
  listEl.hidden = items.length === 0;
  return items.length;
}

const NO_LANDMARKS = {
  load() {}, clear() {}, refresh() {}, jump() {}, upcoming: async () => "",
};

// Who a key spoke about: the cue point preview or the song on air.
const PREVIEW = "Preview: ";
const ON_AIR = "On air: ";

export function installLandmarks({
  document: doc = document,
  request = requestJson,
  isCurrent = () => true,
  // The song on air: { path, position, duration, bpm }, or null.
  getPlaying = () => null,
  // Whether the bracket keys move the song on air (Settings, Keyboard,
  // "Bracket keys seek the song on air"; off by default).  Off, they only
  // say the landmark.
  seekOnAir = () => false,
  isStreamMode = () => false,
  // Moves this page's own playback to *seconds* after the server took the
  // seek (the browser deck in browser mode, the slider's clock otherwise).
  applyLocalSeek = () => {},
  speak = srSpeak,
} = {}) {
  if (!doc) return NO_LANDMARKS;
  const el = (id) => doc.getElementById(id);
  // The song open in Track info: { path, data } once its answer is in,
  // with the request still running in *pending* until then.
  let shown = null;
  let generation = 0;

  // Always asked afresh: the song on air moves on, and its cue points can
  // change in another tab.  The server keeps its own cache.
  function answerFor(path) {
    return request(`/api/landmarks?path=${encodeURIComponent(path)}`);
  }

  function showState(text, note = false) {
    const empty = el("landmarks-empty");
    if (empty) {
      empty.textContent = text;
      empty.hidden = !text;
    }
    const noteEl = el("landmarks-note");
    if (noteEl) noteEl.hidden = !note;
  }

  function clear() {
    generation += 1;
    shown = null;
    const section = el("landmarks-section");
    if (section) section.hidden = true;
    renderStructure(el("landmarks-list"), []);
    showState("");
  }

  async function fill(path, mine) {
    const pending = answerFor(path);
    if (shown?.path === path) shown.pending = pending;
    try {
      const data = await pending;
      if (mine !== generation || !isCurrent()) return;
      shown = { path, data, pending: null };
      const count = renderStructure(el("landmarks-list"), data.landmarks);
      showState(count ? "" : "No landmarks for this song yet.", !data.analysed);
    } catch (error) {
      if (mine !== generation || !isCurrent() || isStaleStationReply(error)) return;
      shown = { path, data: null, pending: null };
      showState(`Could not load the song structure: ${error.message}`);
    }
  }

  // Track info opened *record* (its /api/library/points answer).
  function load(record) {
    clear();
    const path = record?.path;
    if (!path || (record.kind || "music") !== "music") return;
    const section = el("landmarks-section");
    if (section) section.hidden = false;
    showState("Loading the song structure.");
    shown = { path, data: null, pending: null };
    void fill(path, generation);
  }

  // Another station now (#70), or new cue points saved: draw it again.
  function refresh() {
    if (!shown?.path) return;
    generation += 1;
    void fill(shown.path, generation);
  }

  function trackInfoShowing() {
    const panel = el("panel-track-info");
    return Boolean(panel && !panel.hidden);
  }

  function chooseLandmark(landmarks, position, direction) {
    return direction > 0 ? nextLandmark(landmarks, position) : previousLandmark(landmarks, position);
  }

  function noneLeft(direction, where) {
    return direction > 0 ? `No more landmarks after ${where}.` : `No landmark before ${where}.`;
  }

  async function jumpPreview(direction) {
    const preview = el("points-preview");
    if (!shown?.path || !preview
        || preview.getAttribute("src") !== `/api/audio?path=${encodeURIComponent(shown.path)}`) {
      speak("Open a song in Track info to move through its landmarks.");
      return;
    }
    const path = shown.path;
    const data = shown.data || await (shown.pending || answerFor(path));
    if (shown?.path !== path) return;
    const list = data.landmarks || [];
    if (!list.length) { speak(`${PREVIEW}No landmarks for this song yet.`); return; }
    const landmark = chooseLandmark(list, Number(preview.currentTime) || 0, direction);
    if (!landmark) { speak(`${PREVIEW}${noneLeft(direction, "the preview position")}`); return; }
    preview.currentTime = landmark.time_s;
    speak(`${PREVIEW}${landmarkSpeech(landmark)}`);
  }

  // Moves the song on air, but only the song the landmark is in: the
  // server refuses (409) when another song is playing by then.
  async function seekTo(path, landmark) {
    if (isStreamMode()) {
      speak("Seeking is not available while streaming.");
      return;
    }
    const station = stationEpoch();
    try {
      await request("/api/seek", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seconds: landmark.time_s, path }),
      });
    } catch (error) {
      if (isStaleStationReply(error) || station !== stationEpoch()) return;
      speak(error.status === 409 ? error.message : `Could not move the song on air: ${error.message}`);
      return;
    }
    if (station !== stationEpoch()) return;
    applyLocalSeek(landmark.time_s);
    speak(`${ON_AIR}${landmarkSpeech(landmark)}`);
  }

  async function jumpPlaying(direction) {
    const playing = getPlaying();
    if (!playing?.path) { speak(`${ON_AIR}Nothing is playing.`); return; }
    const station = stationEpoch();
    const data = await answerFor(playing.path);
    const now = getPlaying();
    if (station !== stationEpoch() || now?.path !== playing.path) return;
    const list = data.landmarks || [];
    if (!list.length) { speak(`${ON_AIR}No landmarks for this song yet.`); return; }
    const landmark = chooseLandmark(list, now.position, direction);
    if (!landmark) { speak(`${ON_AIR}${noneLeft(direction, "this point in the song")}`); return; }
    if (seekOnAir()) {
      await seekTo(playing.path, landmark);
      return;
    }
    // Only said: how far it is from where the song is now.
    const gap = landmark.time_s - now.position;
    const when = gap >= 0 ? `in ${fmtTime(gap)}` : `${fmtTime(-gap)} ago`;
    speak(`${ON_AIR}${landmarkSpeech(landmark)}, ${when}`);
  }

  // Right bracket (direction 1) or Left bracket (-1).
  async function jump(direction) {
    try {
      if (trackInfoShowing()) await jumpPreview(direction);
      else await jumpPlaying(direction);
    } catch (error) {
      if (!isStaleStationReply(error)) speak(`Could not load the landmarks: ${error.message}`);
    }
  }

  // Shift+U: what is coming next in the song on air, as a sentence; empty
  // when the reply was for a station the page has left.
  async function upcoming() {
    try {
      return await upcomingOnAir();
    } catch (error) {
      if (isStaleStationReply(error)) return "";
      throw error;
    }
  }

  async function upcomingOnAir() {
    const playing = getPlaying();
    if (!playing?.path) return `${ON_AIR}Nothing is playing.`;
    const data = await answerFor(playing.path);
    const now = getPlaying();
    if (now?.path !== playing.path) return `${ON_AIR}The song has just changed.`;
    return ON_AIR + upcomingSpeech(data.landmarks, now.position, {
      bpm: now.bpm || data.bpm || 0,
      duration: now.duration || data.length || 0,
    });
  }

  return { load, clear, refresh, jump, upcoming };
}
