import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  barsIn,
  clockTime,
  installLandmarks,
  landmarkSpeech,
  nextLandmark,
  previousLandmark,
  renderStructure,
  upcomingSpeech,
} from "../../src/autodj/static/modules/landmarks.js";
import { AuthenticationRequiredError } from "../../src/autodj/static/modules/api-client.js";
import { selectStation } from "../../src/autodj/static/modules/stations.js";

const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
const section = html.match(/<section id="landmarks-section"[\s\S]*?<\/section>/)[0];

const lm = (time_s, kind, name, extra = {}) => ({
  time_s, kind, kinds: [kind], name, source: "analysis", major: true, bar: null, end_s: null, end_bar: null, ...extra,
});
const LANDMARKS = [
  lm(0, "intro", "Intro", { bar: 1, end_s: 32.4, end_bar: 17 }),
  lm(32.4, "intro_end", "Intro ends", { bar: 17 }),
  lm(45, "vocals", "Vocals", { bar: 23, source: "lyrics" }),
  lm(63, "phrase", "Phrase", { bar: 33, major: false }),
  lm(95.2, "drop", "Drop", { bar: 49 }),
  lm(160, "breakdown", "Breakdown", { bar: 81 }),
  lm(230, "outro", "Outro", { bar: 116, end_s: 250, end_bar: 126 }),
];
const ANSWER = { path: "/music/song.flac", kind: "music", length: 250, bpm: 120, analysed: true, landmarks: LANDMARKS };

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const el = (id) => document.getElementById(id);

describe("landmark words", () => {
  it("says a landmark with its time in words and its bar", () => {
    expect(landmarkSpeech(LANDMARKS[4])).toBe("Drop, 1 minute 35 seconds, bar 49");
    expect(landmarkSpeech(lm(45, "vocals", "Vocals"))).toBe("Vocals, 45 seconds");
    expect(landmarkSpeech(LANDMARKS[0])).toBe("Intro, 0 seconds, bar 1");
  });

  it("shows times rounded the way the words are", () => {
    expect(clockTime(95.2)).toBe("1:35");
    expect(clockTime(59.6)).toBe("1:00");
    expect(clockTime(-3)).toBe("0:00");
    expect(clockTime(undefined)).toBe("0:00");
  });

  it("finds the next and previous landmark from a position", () => {
    expect(nextLandmark(LANDMARKS, 50).name).toBe("Phrase");
    // A landmark right where you are is not the next one.
    expect(nextLandmark(LANDMARKS, 95.1).name).toBe("Breakdown");
    expect(nextLandmark(LANDMARKS, 240)).toBeNull();
    // Previous goes to the start of the landmark you are in, or past it
    // when you have only just reached it.
    expect(previousLandmark(LANDMARKS, 100).name).toBe("Drop");
    expect(previousLandmark(LANDMARKS, 95.2).name).toBe("Phrase");
    expect(previousLandmark(LANDMARKS, 0.5)).toBeNull();
    expect(nextLandmark(null, 0)).toBeNull();
  });

  it("says what is coming next with seconds and bars", () => {
    expect(barsIn(8, 120)).toBe(4);
    expect(barsIn(0.9, 120)).toBeNull();
    expect(barsIn(8, 0)).toBeNull();
    expect(upcomingSpeech(LANDMARKS, 87.2, { bpm: 120 })).toBe("Drop in 8 seconds, 4 bars.");
    // Phrases are never "coming next"; the outro starts.
    expect(upcomingSpeech(LANDMARKS, 200, { bpm: 120 })).toBe("Outro starts in 30 seconds, 15 bars.");
    expect(upcomingSpeech(LANDMARKS, 10, { bpm: 0 })).toBe("Intro ends in 22 seconds.");
    expect(upcomingSpeech(LANDMARKS, 40, { bpm: 120 })).toBe("Vocals start in 5 seconds, 3 bars.");
    expect(upcomingSpeech(LANDMARKS, 94.8, { bpm: 120 })).toBe("Drop now.");
    expect(upcomingSpeech(LANDMARKS, 235, { duration: 250 })).toBe("No more landmarks. The song ends in 15 seconds.");
    expect(upcomingSpeech(LANDMARKS, 235)).toBe("No more landmarks in this song.");
    expect(upcomingSpeech([], 0)).toBe("No landmarks for this song yet.");
    const merged = { ...lm(120, "drop", "Drop and vocals"), kinds: ["drop", "vocals"] };
    expect(upcomingSpeech([merged], 100, { bpm: 120 })).toBe("Drop and vocals in 20 seconds, 10 bars.");
  });
});

describe("Song structure list", () => {
  beforeEach(() => {
    document.body.innerHTML = section;
  });

  it("is a real list of text, structure only, times shown as 1:35 and read in words", () => {
    const list = el("landmarks-list");
    expect(renderStructure(list, LANDMARKS)).toBe(5);
    expect(list.hidden).toBe(false);
    const items = [...list.querySelectorAll("li")];
    expect(items.map((item) => item.textContent)).toEqual([
      "Intro, 0:000 seconds to 0:3232 seconds, bars 1 to 17",
      "Vocals from 0:4545 seconds, bar 23",
      "Drop, 1:351 minute 35 seconds, bar 49",
      "Breakdown, 2:402 minutes 40 seconds, bar 81",
      "Outro, 3:503 minutes 50 seconds to 4:104 minutes 10 seconds, bars 116 to 126",
    ]);
    // What a screen reader reads: the words, never the bare clock time.
    const spoken = (item) => [...item.childNodes]
      .filter((node) => !(node.nodeType === 1 && node.getAttribute("aria-hidden") === "true"))
      .map((node) => node.textContent).join("");
    expect(spoken(items[2])).toBe("Drop, 1 minute 35 seconds, bar 49");
    expect(items[2].querySelector('[aria-hidden="true"]').textContent).toBe("1:35");
    expect(items[2].querySelector(".visually-hidden").textContent).toBe("1 minute 35 seconds");
    expect(renderStructure(list, [])).toBe(0);
    expect(list.hidden).toBe(true);
  });
});

describe("landmark keys and Track info", () => {
  let request;
  let speak;
  let playing;
  let seekPlaying;
  let landmarks;

  function openTrackInfo(path = ANSWER.path) {
    el("panel-track-info").hidden = false;
    el("points-preview").setAttribute("src", `/api/audio?path=${encodeURIComponent(path)}`);
  }

  beforeEach(() => {
    selectStation("default");
    document.body.innerHTML = `<section id="panel-track-info" hidden>${section}<audio id="points-preview"></audio></section>
      <button id="elsewhere">Elsewhere</button>`;
    request = vi.fn(async () => ANSWER);
    speak = vi.fn();
    playing = { path: ANSWER.path, position: 50, duration: 250, bpm: 120 };
    seekPlaying = vi.fn();
    landmarks = installLandmarks({ document, request, speak, getPlaying: () => playing, seekPlaying });
  });

  afterEach(() => vi.restoreAllMocks());

  it("draws the song structure when Track info opens a song, and hides it for a liner", async () => {
    landmarks.load({ path: ANSWER.path, kind: "music" });
    expect(el("landmarks-section").hidden).toBe(false);
    expect(el("landmarks-empty").textContent).toBe("Loading the song structure.");
    await settle();
    expect(request).toHaveBeenCalledWith("/api/landmarks?path=%2Fmusic%2Fsong.flac");
    expect(el("landmarks-list").querySelectorAll("li")).toHaveLength(5);
    expect(el("landmarks-empty").hidden).toBe(true);
    expect(el("landmarks-note").hidden).toBe(true);
    landmarks.load({ path: "/liners/id.mp3", kind: "liner" });
    expect(el("landmarks-section").hidden).toBe(true);
  });

  it("says when nothing is known yet and explains the analysis", async () => {
    request.mockResolvedValueOnce({ ...ANSWER, analysed: false, landmarks: [] });
    landmarks.load({ path: ANSWER.path, kind: "music" });
    await settle();
    expect(el("landmarks-empty").textContent).toBe("No landmarks for this song yet.");
    expect(el("landmarks-note").hidden).toBe(false);
    expect(el("landmarks-list").hidden).toBe(true);
  });

  it("shows a failure in words on the page, never live", async () => {
    request.mockRejectedValueOnce(new Error("Recording not in library"));
    landmarks.load({ path: ANSWER.path, kind: "music" });
    await settle();
    const empty = el("landmarks-empty");
    expect(empty.textContent).toBe("Could not load the song structure: Recording not in library");
    expect(empty.closest("[aria-live], [role=status]")).toBeNull();
  });

  it("moves the cue point preview from landmark to landmark in Track info and says each once", async () => {
    landmarks.load({ path: ANSWER.path, kind: "music" });
    await settle();
    openTrackInfo();
    const preview = el("points-preview");
    const focused = el("elsewhere");
    focused.focus();
    preview.currentTime = 50;
    await landmarks.jump(1);
    expect(preview.currentTime).toBeCloseTo(63);
    expect(speak).toHaveBeenLastCalledWith("Phrase, 1 minute 3 seconds, bar 33");
    await landmarks.jump(1);
    expect(preview.currentTime).toBeCloseTo(95.2);
    expect(speak).toHaveBeenLastCalledWith("Drop, 1 minute 35 seconds, bar 49");
    await landmarks.jump(-1);
    expect(speak).toHaveBeenLastCalledWith("Phrase, 1 minute 3 seconds, bar 33");
    expect(speak).toHaveBeenCalledTimes(3);
    // Never the song on air, never focus.
    expect(seekPlaying).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(focused);
    preview.currentTime = 240;
    await landmarks.jump(1);
    expect(speak).toHaveBeenLastCalledWith("No more landmarks after the preview position.");
    preview.currentTime = 0;
    await landmarks.jump(-1);
    expect(speak).toHaveBeenLastCalledWith("No landmark before the preview position.");
    // One answer served every key.
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("asks for a song when Track info shows none", async () => {
    el("panel-track-info").hidden = false;
    await landmarks.jump(1);
    expect(speak).toHaveBeenCalledWith("Open a song in Track info to move through its landmarks.");
    expect(request).not.toHaveBeenCalled();
  });

  it("moves the song on air on other tabs, through the page's seek", async () => {
    await landmarks.jump(1);
    expect(seekPlaying).toHaveBeenCalledWith(63, "Phrase, 1 minute 3 seconds, bar 33");
    playing = { ...playing, position: 100 };
    await landmarks.jump(-1);
    expect(seekPlaying).toHaveBeenLastCalledWith(95.2, "Drop, 1 minute 35 seconds, bar 49");
    // The answer is kept: two keys, one request.
    expect(request).toHaveBeenCalledTimes(1);
    playing = { ...playing, position: 245 };
    await landmarks.jump(1);
    expect(speak).toHaveBeenLastCalledWith("No more landmarks after this point in the song.");
    playing = null;
    await landmarks.jump(1);
    expect(speak).toHaveBeenLastCalledWith("Nothing is playing.");
    expect(seekPlaying).toHaveBeenCalledTimes(2);
  });

  it("says what is coming next on air", async () => {
    playing = { ...playing, position: 87.2 };
    await expect(landmarks.upcoming()).resolves.toBe("Drop in 8 seconds, 4 bars.");
    // The server's tempo and length stand in when the page has none.
    playing = { path: ANSWER.path, position: 200, duration: 0, bpm: 0 };
    await expect(landmarks.upcoming()).resolves.toBe("Outro starts in 30 seconds, 15 bars.");
    playing = null;
    await expect(landmarks.upcoming()).resolves.toBe("Nothing is playing.");
  });

  it("says nothing for a reply from a station the page has left", async () => {
    // api-client marks a reply that came back after a station switch.
    const stale = Object.assign(new AuthenticationRequiredError("That reply was for the station this page has just left."), { staleStation: true });
    request.mockRejectedValue(stale);
    await expect(landmarks.upcoming()).resolves.toBe("");
    await landmarks.jump(1);
    expect(speak).not.toHaveBeenCalled();
  });

  it("asks again after cue points are saved, and per station", async () => {
    await landmarks.upcoming();
    await landmarks.upcoming();
    expect(request).toHaveBeenCalledTimes(1);
    landmarks.invalidate(ANSWER.path);
    await landmarks.upcoming();
    expect(request).toHaveBeenCalledTimes(2);
    selectStation("night");
    await landmarks.upcoming();
    expect(request).toHaveBeenCalledTimes(3);
    selectStation("default");
  });

  it("draws the station's own answer again on a station switch", async () => {
    landmarks.load({ path: ANSWER.path, kind: "music" });
    await settle();
    request.mockResolvedValueOnce({ ...ANSWER, landmarks: LANDMARKS.slice(0, 2) });
    landmarks.refresh();
    await settle();
    expect(el("landmarks-list").querySelectorAll("li")).toHaveLength(1);
    landmarks.clear();
    expect(el("landmarks-section").hidden).toBe(true);
    landmarks.refresh();
    expect(request).toHaveBeenCalledTimes(2);
  });
});
