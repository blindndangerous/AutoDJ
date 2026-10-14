// Spoken track landmarks (#60): a song's structure through keys and speech,
// in place of a waveform.
//
// The server merges the song's cue points, its DJ analysis (drops,
// breakdowns, build-ups, phrases, the beat grid for bar numbers) and its
// synced lyrics into named landmarks (/api/landmarks, a station route).
// This module:
//
// - draws Track info's Song structure: a real list, one landmark per item,
//   in time order.  Times show as 1:35 and are read as "1 minute 35
//   seconds": a bare "1:35" can be read as a clock time.  The list is
//   never live; it is drawn when a song opens.
// - moves to the next or previous landmark (Right and Left bracket).  With
//   Track info showing, the cue point preview moves there; on any other
//   tab the playing song does, where seeking is allowed.  Either way the
//   landmark is said once ("Drop, 1 minute 35 seconds, bar 49") through
//   the page's polite #sr-status, and focus stays where it is.
// - says what is coming next on air (Shift+U): "Drop in 12 seconds,
//   4 bars", "Outro starts in 30 seconds, 16 bars".  Phrases and unnamed
//   cues are stops for the bracket keys only, never "coming next".
//
// Answers are kept for a minute per station and song; saving a song's cue
// points forgets its answer.

import { isStaleStationReply, requestJson } from "./api-client.js";
import { fmtDurationWords, srSpeak } from "./dom-helpers.js";
import { stationEpoch } from "./stations.js";

const CACHE_MS = 60_000;
const CACHE_SIZE = 8;
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

// "1:35" for the screen, rounded as fmtDurationWords rounds, so the
// screen and the speech never differ by a second.
export function clockTime(sec) {
  const total = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

// "Drop, 1 minute 35 seconds, bar 49".
export function landmarkSpeech(landmark) {
  const bar = landmark.bar ? `, bar ${landmark.bar}` : "";
  return `${landmark.name}, ${fmtDurationWords(landmark.time_s)}${bar}`;
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
    const left = duration - position;
    return left >= 1
      ? `No more landmarks. The song ends in ${fmtDurationWords(left)}.`
      : "No more landmarks in this song.";
  }
  const lead = (next.kinds || [next.kind]).length === 1 && SOON[next.kind] ? SOON[next.kind] : next.name;
  const seconds = next.time_s - position;
  if (seconds < 1) return `${lead} now.`;
  const bars = barsIn(seconds, bpm);
  return `${lead} in ${fmtDurationWords(seconds)}${bars ? `, ${bars} bar${bars === 1 ? "" : "s"}` : ""}.`;
}

// A time on the page: "1:35" to the eye, "1 minute 35 seconds" to a
// screen reader (the established aria-hidden plus visually-hidden pair).
function timeNodes(doc, sec) {
  const shown = doc.createElement("span");
  shown.setAttribute("aria-hidden", "true");
  shown.textContent = clockTime(sec);
  const spoken = doc.createElement("span");
  spoken.className = "visually-hidden";
  spoken.textContent = fmtDurationWords(sec);
  return [shown, spoken];
}

// The parts of one Song structure item, as text and time nodes.
function structureParts(landmark) {
  const single = (landmark.kinds || [landmark.kind]).length === 1;
  if (landmark.end_s != null) {
    const bars = landmark.bar && landmark.end_bar ? `, bars ${landmark.bar} to ${landmark.end_bar}` : "";
    return [`${landmark.name}, `, { time: landmark.time_s }, " to ", { time: landmark.end_s }, bars];
  }
  const bar = landmark.bar ? `, bar ${landmark.bar}` : "";
  if (single && landmark.kind === "vocals") return ["Vocals from ", { time: landmark.time_s }, bar];
  return [`${landmark.name}, `, { time: landmark.time_s }, bar];
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
    for (const part of structureParts(landmark)) {
      if (typeof part === "string") {
        if (part) item.append(part);
      } else {
        item.append(...timeNodes(doc, part.time));
      }
    }
    return item;
  }));
  listEl.hidden = items.length === 0;
  return items.length;
}

const NO_LANDMARKS = {
  load() {}, clear() {}, refresh() {}, invalidate() {}, jump() {}, upcoming: async () => "",
};

export function installLandmarks({
  document: doc = document,
  request = requestJson,
  isCurrent = () => true,
  // The song on air: { path, position, duration, bpm }, or null.
  getPlaying = () => null,
  // Moves the song on air to *seconds* and says *speech*; it says why
  // instead where seeking is not allowed (stream mode).
  seekPlaying = null,
  speak = srSpeak,
} = {}) {
  if (!doc) return NO_LANDMARKS;
  const el = (id) => doc.getElementById(id);
  const cache = new Map();
  // The song open in Track info: { path, data } once its answer is in.
  let shown = null;
  let generation = 0;

  function answerFor(path, { fresh = false } = {}) {
    const key = `${stationEpoch()}\n${path}`;
    const held = cache.get(key);
    if (!fresh && held && Date.now() - held.at < CACHE_MS) return held.promise;
    const promise = request(`/api/landmarks?path=${encodeURIComponent(path)}`);
    const entry = { at: Date.now(), promise };
    cache.delete(key);
    cache.set(key, entry);
    while (cache.size > CACHE_SIZE) cache.delete(cache.keys().next().value);
    promise.catch(() => { if (cache.get(key) === entry) cache.delete(key); });
    return promise;
  }

  function invalidate(path) {
    for (const key of [...cache.keys()]) if (key.endsWith(`\n${path}`)) cache.delete(key);
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

  async function fill(path, mine, fresh) {
    try {
      const data = await answerFor(path, { fresh });
      if (mine !== generation || !isCurrent()) return;
      shown = { path, data };
      const count = renderStructure(el("landmarks-list"), data.landmarks);
      showState(count ? "" : "No landmarks for this song yet.", !data.analysed);
    } catch (error) {
      if (mine !== generation || !isCurrent() || isStaleStationReply(error)) return;
      shown = null;
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
    const mine = generation;
    shown = { path, data: null };
    void fill(path, mine, true);
  }

  // Another station now (#70): its answer for the song shown.
  function refresh() {
    if (!shown?.path) return;
    generation += 1;
    void fill(shown.path, generation, true);
  }

  // The cue point preview, when Track info shows with a song open.
  function trackInfoPreview() {
    const panel = el("panel-track-info");
    if (!panel || panel.hidden) return null;
    return { preview: el("points-preview") };
  }

  function chooseLandmark(landmarks, position, direction) {
    return direction > 0 ? nextLandmark(landmarks, position) : previousLandmark(landmarks, position);
  }

  function noneLeft(direction, where) {
    return direction > 0 ? `No more landmarks after ${where}.` : `No landmark before ${where}.`;
  }

  async function jumpPreview(preview, direction) {
    if (!shown?.path || !preview
        || preview.getAttribute("src") !== `/api/audio?path=${encodeURIComponent(shown.path)}`) {
      speak("Open a song in Track info to move through its landmarks.");
      return;
    }
    const path = shown.path;
    const data = shown.data || await answerFor(path);
    if (shown?.path !== path) return;
    const list = data.landmarks || [];
    if (!list.length) { speak("No landmarks for this song yet."); return; }
    const landmark = chooseLandmark(list, Number(preview.currentTime) || 0, direction);
    if (!landmark) { speak(noneLeft(direction, "the preview position")); return; }
    preview.currentTime = landmark.time_s;
    speak(landmarkSpeech(landmark));
  }

  async function jumpPlaying(direction) {
    const playing = getPlaying();
    if (!playing?.path) { speak("Nothing is playing."); return; }
    const station = stationEpoch();
    const data = await answerFor(playing.path);
    const now = getPlaying();
    if (station !== stationEpoch() || now?.path !== playing.path) return;
    const list = data.landmarks || [];
    if (!list.length) { speak("No landmarks for this song yet."); return; }
    const landmark = chooseLandmark(list, now.position, direction);
    if (!landmark) { speak(noneLeft(direction, "this point in the song")); return; }
    if (seekPlaying) seekPlaying(landmark.time_s, landmarkSpeech(landmark));
  }

  // Right bracket (direction 1) or Left bracket (-1).
  async function jump(direction) {
    try {
      const context = trackInfoPreview();
      if (context) await jumpPreview(context.preview, direction);
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
    if (!playing?.path) return "Nothing is playing.";
    const data = await answerFor(playing.path);
    const now = getPlaying();
    if (now?.path !== playing.path) return "The song has just changed.";
    return upcomingSpeech(data.landmarks, now.position, {
      bpm: now.bpm || data.bpm || 0,
      duration: now.duration || data.length || 0,
    });
  }

  return { load, clear, refresh, invalidate, jump, upcoming };
}
