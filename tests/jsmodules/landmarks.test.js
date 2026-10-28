import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  barsIn,
  installLandmarks,
  landmarkSpeech,
  nextLandmark,
  previousLandmark,
  renderStructure,
  structureText,
  upcomingSpeech,
} from "../../src/autodj/static/modules/landmarks.js";
import { AuthenticationRequiredError } from "../../src/autodj/static/modules/api-client.js";
import { selectStation } from "../../src/autodj/static/modules/stations.js";

const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
// Parsed without its stylesheet and scripts, which happy-dom would try to
// load from a server that is not there.
const page = new window.DOMParser().parseFromString(
  html.replace(/<link\b[^>]*>/g, "").replace(/<script\b[\s\S]*?<\/script>/g, ""), "text/html",
);
const section = page.getElementById("landmarks-section").outerHTML;

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
  it("says a landmark with its numeric time and its bar", () => {
    expect(landmarkSpeech(LANDMARKS[4])).toBe("Drop, 1:35, bar 49");
    expect(landmarkSpeech(lm(45, "vocals", "Vocals"))).toBe("Vocals, 0:45");
    expect(landmarkSpeech(LANDMARKS[0])).toBe("Intro, 0:00, bar 1");
  });

  it("writes a song structure item as plain text", () => {
    expect(structureText(LANDMARKS[0])).toBe("Intro, 0:00 to 0:32, bars 1 to 17");
    expect(structureText(LANDMARKS[2])).toBe("Vocals from 0:45, bar 23");
    expect(structureText(lm(95.2, "drop", "Drop"))).toBe("Drop, 1:35");
    const merged = { ...lm(45, "vocals", "Vocals and drop"), kinds: ["vocals", "drop"] };
    expect(structureText(merged)).toBe("Vocals and drop, 0:45");
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

  it("says what is coming next with the time and bars", () => {
    expect(barsIn(8, 120)).toBe(4);
    expect(barsIn(0.9, 120)).toBeNull();
    expect(barsIn(8, 0)).toBeNull();
    expect(upcomingSpeech(LANDMARKS, 87.2, { bpm: 120 })).toBe("Drop in 0:08, 4 bars.");
    // Phrases are never "coming next"; the outro starts.
    expect(upcomingSpeech(LANDMARKS, 200, { bpm: 120 })).toBe("Outro starts in 0:30, 15 bars.");
    expect(upcomingSpeech(LANDMARKS, 10, { bpm: 0 })).toBe("Intro ends in 0:22.");
    expect(upcomingSpeech(LANDMARKS, 40, { bpm: 120 })).toBe("Vocals start in 0:05, 3 bars.");
    expect(upcomingSpeech(LANDMARKS, 94.8, { bpm: 120 })).toBe("Drop now.");
    expect(upcomingSpeech(LANDMARKS, 235, { duration: 250 })).toBe("No more landmarks. The song ends in 0:15.");
    expect(upcomingSpeech(LANDMARKS, 235)).toBe("No more landmarks in this song.");
    expect(upcomingSpeech([], 0)).toBe("No landmarks for this song yet.");
    // Phrases left after the last major landmark: Right bracket still finds
    // them, so Shift+U does not say there are no landmarks at all.
    const phrasesLeft = [...LANDMARKS.slice(0, 5), lm(110, "phrase", "Phrase", { bar: 57, major: false })];
    expect(upcomingSpeech(phrasesLeft, 100, { duration: 250 })).toBe("No more major landmarks. The song ends in 2:30.");
    expect(upcomingSpeech(phrasesLeft, 100)).toBe("No more major landmarks in this song.");
    const merged = { ...lm(120, "drop", "Drop and vocals"), kinds: ["drop", "vocals"] };
    expect(upcomingSpeech([merged], 100, { bpm: 120 })).toBe("Drop and vocals in 0:20, 10 bars.");
  });
});

describe("Song structure list", () => {
  beforeEach(() => {
    document.body.innerHTML = section;
  });

  it("is a real list of plain text, structure only, times numeric as everywhere else", () => {
    const list = el("landmarks-list");
    expect(renderStructure(list, LANDMARKS)).toBe(5);
    expect(list.hidden).toBe(false);
    const items = [...list.querySelectorAll("li")];
    expect(items.map((item) => item.textContent)).toEqual([
      "Intro, 0:00 to 0:32, bars 1 to 17",
      "Vocals from 0:45, bar 23",
      "Drop, 1:35, bar 49",
      "Breakdown, 2:40, bar 81",
      "Outro, 3:50 to 4:10, bars 116 to 126",
    ]);
    // The same text for the eye and the screen reader: nothing hidden or swapped.
    expect(list.querySelectorAll("[aria-hidden], .visually-hidden, [aria-label]")).toHaveLength(0);
    expect(renderStructure(list, [])).toBe(0);
    expect(list.hidden).toBe(true);
  });
});

describe("landmark keys and Track info", () => {
  let request;
  let speak;
  let playing;
  let seekOn;
  let streaming;
  let applyLocalSeek;
  let seekReply;
  let landmarks;

  function openTrackInfo(path = ANSWER.path) {
    el("panel-track-info").hidden = false;
    el("points-preview").setAttribute("src", `/api/audio?path=${encodeURIComponent(path)}`);
  }
  const seeks = () => request.mock.calls.filter(([url]) => url === "/api/seek");
  const landmarkCalls = () => request.mock.calls.filter(([url]) => url.startsWith("/api/landmarks"));

  beforeEach(() => {
    selectStation("default");
    document.body.innerHTML = `<section id="panel-track-info" hidden>${section}<audio id="points-preview"></audio></section>
      <button id="elsewhere">Elsewhere</button>`;
    seekReply = async () => ({ elapsed: 0 });
    request = vi.fn(async (url) => (url === "/api/seek" ? seekReply() : ANSWER));
    speak = vi.fn();
    playing = { path: ANSWER.path, position: 50, duration: 250, bpm: 120 };
    seekOn = false;
    streaming = false;
    applyLocalSeek = vi.fn();
    landmarks = installLandmarks({
      document, request, speak, getPlaying: () => playing, applyLocalSeek,
      seekOnAir: () => seekOn, isStreamMode: () => streaming,
    });
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

  it("moves the cue point preview in Track info and says each landmark once, as the preview's", async () => {
    landmarks.load({ path: ANSWER.path, kind: "music" });
    await settle();
    openTrackInfo();
    const preview = el("points-preview");
    const focused = el("elsewhere");
    focused.focus();
    preview.currentTime = 50;
    await landmarks.jump(1);
    expect(preview.currentTime).toBeCloseTo(63);
    expect(speak).toHaveBeenLastCalledWith("Preview: Phrase, 1:03, bar 33");
    await landmarks.jump(1);
    expect(preview.currentTime).toBeCloseTo(95.2);
    expect(speak).toHaveBeenLastCalledWith("Preview: Drop, 1:35, bar 49");
    await landmarks.jump(-1);
    expect(speak).toHaveBeenLastCalledWith("Preview: Phrase, 1:03, bar 33");
    expect(speak).toHaveBeenCalledTimes(3);
    // Never the song on air, never focus, whatever the seek setting.
    seekOn = true;
    await landmarks.jump(1);
    expect(seeks()).toHaveLength(0);
    expect(applyLocalSeek).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(focused);
    preview.currentTime = 240;
    await landmarks.jump(1);
    expect(speak).toHaveBeenLastCalledWith("Preview: No more landmarks after the preview position.");
    preview.currentTime = 0;
    await landmarks.jump(-1);
    expect(speak).toHaveBeenLastCalledWith("Preview: No landmark before the preview position.");
    // The song shown in Track info is asked about once.
    expect(landmarkCalls()).toHaveLength(1);
  });

  it("asks for a song when Track info shows none", async () => {
    el("panel-track-info").hidden = false;
    await landmarks.jump(1);
    expect(speak).toHaveBeenCalledWith("Open a song in Track info to move through its landmarks.");
    expect(request).not.toHaveBeenCalled();
  });

  it("only says the landmark of the song on air by default, never moving it", async () => {
    await landmarks.jump(1);
    expect(speak).toHaveBeenLastCalledWith("On air: Phrase, 1:03, bar 33, in 0:13");
    playing = { ...playing, position: 100 };
    await landmarks.jump(-1);
    expect(speak).toHaveBeenLastCalledWith("On air: Drop, 1:35, bar 49, 0:04 ago");
    expect(seeks()).toHaveLength(0);
    expect(applyLocalSeek).not.toHaveBeenCalled();
    // Asked afresh on every key: the song on air moves on.
    expect(landmarkCalls()).toHaveLength(2);
    playing = { ...playing, position: 245 };
    await landmarks.jump(1);
    expect(speak).toHaveBeenLastCalledWith("On air: No more landmarks after this point in the song.");
    playing = null;
    await landmarks.jump(1);
    expect(speak).toHaveBeenLastCalledWith("On air: Nothing is playing.");
  });

  it("moves the song on air with the setting on, naming the song, then follows locally", async () => {
    seekOn = true;
    await landmarks.jump(1);
    expect(seeks()).toHaveLength(1);
    expect(JSON.parse(seeks()[0][1].body)).toEqual({ seconds: 63, path: ANSWER.path });
    expect(applyLocalSeek).toHaveBeenCalledWith(63);
    expect(speak).toHaveBeenLastCalledWith("On air: Phrase, 1:03, bar 33");
    expect(speak).toHaveBeenCalledTimes(1);
  });

  it("refuses to seek in stream mode, saying so once", async () => {
    seekOn = true;
    streaming = true;
    await landmarks.jump(1);
    expect(seeks()).toHaveLength(0);
    expect(applyLocalSeek).not.toHaveBeenCalled();
    expect(speak.mock.calls).toEqual([["Seeking is not available while streaming."]]);
  });

  it("says once that the song changed when the server refuses the seek, and does not move", async () => {
    seekOn = true;
    seekReply = async () => {
      throw Object.assign(new Error("The song on air has changed, so it was not moved."), { status: 409 });
    };
    await landmarks.jump(1);
    expect(applyLocalSeek).not.toHaveBeenCalled();
    expect(speak.mock.calls).toEqual([["The song on air has changed, so it was not moved."]]);
    seekReply = async () => { throw Object.assign(new Error("Server error"), { status: 500 }); };
    await landmarks.jump(1);
    expect(speak).toHaveBeenLastCalledWith("Could not move the song on air: Server error");
  });

  it("says nothing when the song changes while its landmarks load", async () => {
    seekOn = true;
    request.mockImplementationOnce(async () => {
      playing = { ...playing, path: "/music/next.flac" };
      return ANSWER;
    });
    await landmarks.jump(1);
    expect(seeks()).toHaveLength(0);
    expect(speak).not.toHaveBeenCalled();
  });

  it("says what is coming next on air, asked afresh each time", async () => {
    playing = { ...playing, position: 87.2 };
    await expect(landmarks.upcoming()).resolves.toBe("On air: Drop in 0:08, 4 bars.");
    // The server's tempo and length stand in when the page has none.
    playing = { path: ANSWER.path, position: 200, duration: 0, bpm: 0 };
    await expect(landmarks.upcoming()).resolves.toBe("On air: Outro starts in 0:30, 15 bars.");
    expect(landmarkCalls()).toHaveLength(2);
    playing = null;
    await expect(landmarks.upcoming()).resolves.toBe("On air: Nothing is playing.");
  });

  it("says nothing for a reply from a station the page has left", async () => {
    // api-client marks a reply that came back after a station switch.
    const stale = Object.assign(new AuthenticationRequiredError("That reply was for the station this page has just left."), { staleStation: true });
    request.mockRejectedValue(stale);
    await expect(landmarks.upcoming()).resolves.toBe("");
    await landmarks.jump(1);
    expect(speak).not.toHaveBeenCalled();
  });

  it("draws the station's own answer again on a station switch or a save", async () => {
    landmarks.load({ path: ANSWER.path, kind: "music" });
    await settle();
    request.mockResolvedValueOnce({ ...ANSWER, landmarks: LANDMARKS.slice(0, 2) });
    landmarks.refresh();
    await settle();
    expect(el("landmarks-list").querySelectorAll("li")).toHaveLength(1);
    landmarks.clear();
    expect(el("landmarks-section").hidden).toBe(true);
    landmarks.refresh();
    expect(landmarkCalls()).toHaveLength(2);
  });
});
