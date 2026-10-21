
import { afterEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup, memoryStorage } from "./helpers.js";

const api = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../../src/autodj/static/modules/api-client.js", async (importOriginal) => ({
  ...(await importOriginal()),
  requestJson: api.requestJson,
}));

import {
  BROWSER_MAX_SECONDS,
  INSECURE_TEXT,
  NO_RECORDER_TEXT,
  clock,
  encodeWav,
  installVoiceTracker,
  microphoneProblem,
  recordingToWav,
} from "../../src/autodj/static/modules/voice-tracker.js";

const { Blob, EventTarget, MutationObserver } = globalThis;
const byId = (id) => document.getElementById(id);

function installIndex() {
  installIndexMarkup();
  byId("panel-voice").hidden = false;
}

const song = (id, title, artist = "Artist") => ({ id, title, artist, album: "", year: null, duration_s: 200 });

function slot(id = "a".repeat(32), over = {}) {
  return {
    slot_id: id,
    ending: song("song-c", "Third Song"),
    upcoming: song("song-d", "Fourth Song"),
    closes_in_s: 125,
    airs_in_s: 330,
    upcoming_intro_s: 8,
    pinned: false,
    breaks_booked: 0,
    auto_placement: "talk_up",
    auto_note: null,
    ...over,
  };
}

// A stand-in for the voice tracker's routes, as the server answers them.
function fakeServer() {
  const server = {
    accepting: true,
    reason: null,
    slot: slot(),
    limits: { max_bytes: 64 * 1024 * 1024, max_seconds: 600, sample_rate: 44100, dj_placement: "auto", min_intro_s: 2, max_per_slot: 2 },
    tracks: [],
    calls: [],
    refuse: null,
    slowPost: false,
    rejectPost: null,
  };
  api.requestJson.mockReset().mockImplementation(async (url, options = {}) => {
    const method = options.method || "GET";
    server.calls.push({ method, url, body: options.body, signal: options.signal });
    if (url === "/api/voice-tracks" && method === "GET") {
      const { calls: _calls, refuse: _refuse, ...view } = server;
      return JSON.parse(JSON.stringify(view));
    }
    if (url === "/api/voice-tracks" && method === "POST") {
      if (server.slowPost) {
        return new Promise((_resolve, reject) => {
          server.rejectPost = reject;
          options.signal?.addEventListener("abort", () => reject(
            Object.assign(new Error("Aborted"), { name: "AbortError" }),
          ), { once: true });
        });
      }
      if (server.refuse) {
        const refusal = server.refuse;
        server.refuse = null;
        throw Object.assign(new Error(refusal.message), { status: refusal.status });
      }
      const track = {
        break_id: "b".repeat(32), kind: "dj", label: "Third Song into Fourth Song", state: "queued",
        reason: null, slot_id: options.body.get("slot_id"), placement: options.body.get("placement"),
        mixed_as: null, source: "voice_track", eta: null, airs_in_s: 330,
      };
      server.tracks = [track];
      server.slot = { ...server.slot, pinned: true, breaks_booked: 1 };
      return track;
    }
    const match = url.match(/^\/api\/voice-tracks\/([0-9a-f]+)$/);
    if (match && method === "DELETE") {
      server.tracks = server.tracks.map((track) => (track.break_id === match[1]
        ? { ...track, state: "dropped", reason: "cancelled in the voice tracker" } : track));
      return server.tracks.find((track) => track.break_id === match[1]);
    }
    throw new Error(`unexpected ${method} ${url}`);
  });
  return server;
}

class FakeRecorder extends EventTarget {
  constructor(stream) {
    super();
    this.stream = stream;
    this.state = "inactive";
    this.mimeType = "audio/webm;codecs=opus";
    FakeRecorder.last = this;
  }

  start(timeslice) {
    this.timeslice = timeslice;
    this.state = "recording";
  }

  stop() {
    this.state = "inactive";
    const event = new Event("dataavailable");
    event.data = new Blob(["recorded voice"], { type: this.mimeType });
    this.dispatchEvent(event);
    this.dispatchEvent(new Event("stop"));
  }
}

function microphone() {
  const tracks = [];
  const streams = [];
  const makeStream = () => {
    const track = { stop: vi.fn(), getSettings: () => ({ deviceId: "mic-1" }) };
    const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
    tracks.push(track);
    streams.push(stream);
    return stream;
  };
  const stream = makeStream();
  const devices = [];
  return {
    track: tracks[0], tracks, stream, streams, devices,
    getUserMedia: vi.fn(async () => streams.shift() || makeStream()),
    enumerateDevices: vi.fn(async () => devices),
    addEventListener: vi.fn(),
  };
}

function tracker(over = {}) {
  installIndex();
  const server = fakeServer();
  const mic = microphone();
  const storage = memoryStorage();
  const time = { now: 0 };
  let tick = () => {};
  const toWav = over.toWav || vi.fn(async () => new Blob([new Uint8Array(1000)], { type: "audio/wav" }));
  const controls = installVoiceTracker(undefined, {
    Recorder: () => FakeRecorder,
    toWav,
    now: () => time.now,
    wallClock: () => 1000,
    interval: (fn) => { tick = fn; },
    objectUrl: () => "blob:take",
    revokeUrl: vi.fn(),
    storage,
    ...over,
    media: over.media ? () => over.media(mic) : () => mic,
  });
  const advance = async (seconds) => {
    for (let i = 0; i < seconds; i += 1) {
      time.now += 1000;
      tick();
    }
    await flush();
  };
  const tickNow = async (seconds) => {
    time.now = seconds * 1000;
    tick();
    await flush();
  };
  return { controls, server, mic, time, advance, tickNow, toWav, storage };
}

async function flush() {
  for (let i = 0; i < 6; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

const status = () => byId("vt-status").textContent;

async function press(id) {
  const button = byId(id);
  button.focus();
  button.click();
  await flush();
  return button;
}

function altR(target = document.activeElement || document.body) {
  const event = new KeyboardEvent("keydown", { key: "r", code: "KeyR", altKey: true, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

afterEach(() => {
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("voice tracker helpers", () => {
  it("writes a mono 16-bit WAV header and clamps samples", () => {
    const view = new DataView(encodeWav([new Float32Array([0, 1, -1, 2, Number.NaN])], 44100));
    const text = (offset) => String.fromCharCode(...[0, 1, 2, 3].map((i) => view.getUint8(offset + i)));
    expect([text(0), text(8), text(12), text(36)]).toEqual(["RIFF", "WAVE", "fmt ", "data"]);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(44100);
    expect(view.getUint16(34, true)).toBe(16);
    expect(view.getUint32(40, true)).toBe(10);
    expect([0, 1, 2, 3, 4].map((i) => view.getInt16(44 + i * 2, true))).toEqual([0, 32767, -32768, 32767, 0]);
  });

  it("averages the channels into the WAV and decodes at the playout rate", async () => {
    const two = new DataView(encodeWav([new Float32Array([1, 0]), new Float32Array([0, 0.5])], 44100));
    expect([two.getInt16(44, true), two.getInt16(46, true)]).toEqual([16383, 8191]);
    const audio = {
      numberOfChannels: 2, length: 2, sampleRate: 44100,
      getChannelData: (i) => (i === 0 ? new Float32Array([1, 0]) : new Float32Array([0, 0.5])),
    };
    const made = [];
    class Context {
      constructor(...args) { made.push(args); }
      async decodeAudioData() { return audio; }
    }
    const wav = await recordingToWav(new Blob(["x"]), 44100, Context);
    expect(made).toEqual([[1, 1, 44100]]);
    expect(wav.type).toBe("audio/wav");
    expect(wav.size).toBe(44 + 4);
    const view = new DataView(await wav.arrayBuffer());
    expect([view.getInt16(44, true), view.getInt16(46, true)]).toEqual([16383, 8191]);
  });

  it("converts a long take ten seconds at a time, letting the page breathe between", async () => {
    const frames = 44100 * 25;
    const left = new Float32Array(frames).fill(0.5);
    class Context {
      async decodeAudioData() {
        return { numberOfChannels: 1, length: frames, sampleRate: 44100, getChannelData: () => left };
      }
    }
    const pause = vi.fn(async () => {});
    const wav = await recordingToWav(new Blob(["x"]), 44100, Context, pause);
    expect(pause).toHaveBeenCalledTimes(2);
    expect(wav.size).toBe(44 + frames * 2);
    const view = new DataView(await wav.arrayBuffer());
    expect(view.getUint32(4, true)).toBe(wav.size - 8);
    expect(view.getUint32(40, true)).toBe(wav.size - 44);
    expect(view.getInt16(44 + (frames - 1) * 2, true)).toBe(16383);
    expect(BROWSER_MAX_SECONDS).toBe(600);
  });

  it("says why the microphone did not start in plain words", () => {
    expect(microphoneProblem({ name: "NotAllowedError" })).toMatch(/blocked the microphone/);
    expect(microphoneProblem({ name: "NotFoundError" })).toMatch(/No microphone was found/);
    expect(microphoneProblem({ name: "NotReadableError" })).toMatch(/Another program/);
    expect(microphoneProblem(new Error("x"))).toBe("The microphone could not start.");
    expect(clock(65.4)).toBe("1:05");
  });
});

describe("voice tracker", () => {
  it("shows the open song change, its time left and where the voice goes, saying nothing", async () => {
    const { controls, server, advance } = tracker();
    await controls.refresh();
    // The first tick with the tab showing asks again at once.
    await advance(1);
    expect(server.calls.filter((call) => call.method === "GET")).toHaveLength(2);
    expect(byId("vt-slot").hidden).toBe(false);
    expect(byId("vt-unavailable").hidden).toBe(true);
    expect(byId("vt-ending").textContent).toBe("Artist, Third Song");
    expect(byId("vt-upcoming").textContent).toBe("Artist, Fourth Song");
    expect(byId("vt-intro").textContent).toBe("8 seconds");
    expect(byId("vt-closes").textContent).toBe("2:05");
    expect(byId("vt-placement-desc").textContent).toBe("Here, a talk-up: the next song's intro is 8 seconds.");
    await advance(3);
    expect(byId("vt-closes").textContent).toBe("2:02");
    expect(byId("vt-airs").textContent).toBe("5:27");
    expect(status()).toBe("");
    expect(byId("vt-none").hidden).toBe(false);
  });

  it("says why no song change is open", async () => {
    const { controls, server } = tracker();
    server.slot = null;
    server.accepting = false;
    server.reason = "Voice tracks need server playback (server audio or stream) with saved-point playback";
    await controls.refresh();
    expect(byId("vt-slot").hidden).toBe(true);
    expect(byId("vt-unavailable").textContent).toBe(
      "No song change can take a voice track now. Voice tracks need server playback (server audio or stream) with saved-point playback.");
    await press("vt-record");
    expect(byId("vt-error").hidden).toBe(false);
    expect(byId("vt-record").getAttribute("aria-describedby").split(" ")).toContain("vt-error");
  });

  it("asks for the microphone only on Record, keeps focus, and lets it go when stopped", async () => {
    const { controls, mic, advance } = tracker();
    await controls.refresh();
    expect(mic.getUserMedia).not.toHaveBeenCalled();
    const record = await press("vt-record");
    expect(mic.getUserMedia).toHaveBeenCalledWith({
      audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
    expect(record.getAttribute("aria-pressed")).toBe("true");
    expect(document.activeElement).toBe(record);
    await vi.waitFor(() => expect(status()).toBe("Recording. Press Record or Alt+R to stop."));
    expect(byId("vt-status").getAttribute("aria-live")).toBe("polite");
    await advance(2);
    expect(byId("vt-timer").textContent).toBe("Recording 0:02 of 10:00.");
    expect(byId("vt-timer").closest("[aria-live]")).toBeNull();
    record.click();
    await flush();
    expect(mic.track.stop).toHaveBeenCalled();
    expect(record.getAttribute("aria-pressed")).toBe("false");
    expect(document.activeElement).toBe(record);
    expect(byId("vt-playback").getAttribute("src")).toBe("blob:take");
    expect(byId("vt-timer").textContent).toBe("Recorded 0:02, not booked yet.");
    await vi.waitFor(() => expect(status()).toMatch(/^Recording stopped at 0:02\. Listen back/));
  });

  it("remembers an exact microphone across recordings and clears it for System default", async () => {
    const { controls, mic, storage } = tracker();
    mic.devices.push({ kind: "audioinput", deviceId: "mic-1", label: "Studio mic" },
      { kind: "audioinput", deviceId: "mic-2", label: "USB mic" });
    await controls.refresh();
    const select = byId("vt-microphone");
    select.value = "mic-1";
    select.dispatchEvent(new Event("change"));
    expect(storage.data.get("autodj.voiceMicrophoneId")).toBe("mic-1");
    await press("vt-record");
    expect(byId("vt-microphone-status").textContent).toBe("Recording from Studio mic.");
    await press("vt-record");
    expect(byId("vt-microphone-status").textContent).toBe("Last recording used Studio mic.");
    await press("vt-record");
    await press("vt-record");
    await press("vt-record");
    await press("vt-record");
    expect(mic.getUserMedia.mock.calls).toEqual([
      [{ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false, deviceId: { exact: "mic-1" } } }],
      [{ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false, deviceId: { exact: "mic-1" } } }],
      [{ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false, deviceId: { exact: "mic-1" } } }],
    ]);
    expect(byId("vt-microphone-status").textContent).toBe("Last recording used Studio mic.");
    select.value = "mic-2";
    select.dispatchEvent(new Event("change"));
    expect(storage.data.get("autodj.voiceMicrophoneId")).toBe("mic-2");
    select.value = "";
    select.dispatchEvent(new Event("change"));
    expect(storage.data.has("autodj.voiceMicrophoneId")).toBe(false);
    await press("vt-record");
    expect(mic.getUserMedia.mock.calls.at(-1)[0].audio.deviceId).toBeUndefined();
  });

  it("never retries an unavailable selected microphone as System default", async () => {
    const { controls, mic } = tracker();
    mic.devices.push({ kind: "audioinput", deviceId: "mic-1", label: "Studio mic" });
    await controls.refresh();
    byId("vt-microphone").value = "mic-1";
    byId("vt-microphone").dispatchEvent(new Event("change"));
    mic.getUserMedia.mockRejectedValueOnce({ name: "OverconstrainedError" });
    await press("vt-record");
    expect(mic.getUserMedia).toHaveBeenCalledTimes(1);
    expect(mic.getUserMedia).toHaveBeenCalledWith({ audio: expect.objectContaining({ deviceId: { exact: "mic-1" } }) });
    expect(byId("vt-microphone-status").textContent).toMatch(/Could not use Studio mic/);
  });

  it("restores the saved microphone selection after a page reload", async () => {
    const first = tracker();
    first.mic.devices.push({ kind: "audioinput", deviceId: "mic-1", label: "Studio mic" });
    await first.controls.refresh();
    byId("vt-microphone").value = "mic-1";
    byId("vt-microphone").dispatchEvent(new Event("change"));
    first.controls.clear();
    const reloaded = tracker({ storage: first.storage });
    reloaded.mic.devices.push({ kind: "audioinput", deviceId: "mic-1", label: "Studio mic" });
    await reloaded.controls.refresh();
    expect(byId("vt-microphone").value).toBe("mic-1");
  });

  it("refreshes on device changes and warns when the saved input disappears", async () => {
    const { controls, mic } = tracker();
    mic.devices.push({ kind: "audioinput", deviceId: "mic-1", label: "Studio mic" });
    await controls.refresh();
    byId("vt-microphone").value = "mic-1";
    byId("vt-microphone").dispatchEvent(new Event("change"));
    mic.devices.splice(0);
    mic.addEventListener.mock.calls.find(([type]) => type === "devicechange")[1]();
    await flush();
    expect(byId("vt-microphone-status").textContent).toMatch(/not connected/);
    expect([...byId("vt-microphone").options].at(-1).textContent).toBe("Studio mic (not connected)");
  });

  it("monitors privately through the preview audio, blocks recording, and releases on stop or hide", async () => {
    const { controls, mic } = tracker();
    const player = byId("vt-microphone-playback");
    let monitoredStream = null;
    Object.defineProperty(player, "srcObject", {
      configurable: true,
      get: () => monitoredStream,
      set: (value) => { monitoredStream = value; },
    });
    player.play = vi.fn(async () => {});
    await controls.refresh();
    const test = await press("vt-microphone-test");
    expect(player.hasAttribute("data-preview-output")).toBe(true);
    expect(player.srcObject).toBe(mic.stream);
    expect(test.getAttribute("aria-pressed")).toBe("true");
    await press("vt-record");
    expect(mic.getUserMedia).toHaveBeenCalledTimes(1);
    expect(byId("vt-error").textContent).toMatch(/Stop the microphone test/);
    await press("vt-microphone-test");
    expect(player.srcObject).toBeNull();
    expect(mic.track.stop).toHaveBeenCalledTimes(1);
    await press("vt-microphone-test");
    byId("panel-voice").hidden = true;
    await flush();
    expect(player.srcObject).toBeNull();
      expect(mic.tracks[1].stop).toHaveBeenCalledTimes(1);
    expect(byId("vt-microphone-test").getAttribute("aria-pressed")).toBe("false");
    byId("panel-voice").hidden = false;
    await press("vt-microphone-test");
    controls.clear();
    expect(player.srcObject).toBeNull();
    expect(mic.tracks.at(-1).stop).toHaveBeenCalledTimes(1);
  });

  it("stops a pending microphone monitor if the tab hides while playback starts", async () => {
    const { controls, mic } = tracker();
    const player = byId("vt-microphone-playback");
    let monitoredStream = null;
    Object.defineProperty(player, "srcObject", {
      configurable: true,
      get: () => monitoredStream,
      set: (value) => { monitoredStream = value; },
    });
    let resolvePlay;
    player.play = vi.fn(() => new Promise((resolve) => { resolvePlay = resolve; }));
    await controls.refresh();
    byId("vt-microphone-test").click();
    await vi.waitFor(() => expect(player.srcObject).toBe(mic.stream));
    byId("panel-voice").hidden = true;
    await flush();
    expect(player.srcObject).toBeNull();
    expect(mic.track.stop).toHaveBeenCalledTimes(1);
    resolvePlay();
    await flush();
    expect(player.srcObject).toBeNull();
    expect(mic.track.stop).toHaveBeenCalledTimes(1);
    expect(byId("vt-microphone-test").getAttribute("aria-pressed")).toBe("false");
  });

  it("does not upload a take when its conversion finishes after station clear", async () => {
    let resolveWav;
    const { controls, server, toWav } = tracker({
      toWav: vi.fn(() => new Promise((resolve) => { resolveWav = resolve; })),
    });
    await controls.refresh();
    await press("vt-record");
    await press("vt-record");
    byId("vt-book").click();
    await vi.waitFor(() => expect(toWav).toHaveBeenCalledTimes(1));
    controls.clear();
    resolveWav(new Blob([new Uint8Array(1000)], { type: "audio/wav" }));
    await flush();
    expect(server.calls.filter((call) => call.method === "POST")).toHaveLength(0);
    expect(byId("vt-playback").getAttribute("src")).toBeNull();
  });

  it("releases a microphone permission result that arrives after sign-out", async () => {
    let resolveMic;
    const { controls, mic } = tracker({
      media: (device) => ({ ...device, getUserMedia: () => new Promise((resolve) => { resolveMic = resolve; }) }),
    });
    await controls.refresh();
    byId("vt-microphone-playback").play = vi.fn(async () => {});
    byId("vt-microphone-test").click();
    controls.clear();
    resolveMic(mic.stream);
    await flush();
    expect(mic.track.stop).toHaveBeenCalled();
    expect(byId("vt-microphone-playback").srcObject).toBeNull();
  });

  it("speaks once a minute and stops itself at the length limit", async () => {
    const { controls, server, mic, advance } = tracker();
    server.limits.max_seconds = 75;
    await controls.refresh();
    await press("vt-record");
    const spoken = [];
    const region = byId("vt-status");
    new MutationObserver(() => { if (region.textContent) spoken.push(region.textContent); })
      .observe(region, { childList: true, characterData: true, subtree: true });
    await advance(30);
    expect(spoken).toEqual([]);
    await advance(30);
    await vi.waitFor(() => expect(spoken).toEqual([
      "30 seconds left before the length limit.", "1 minute.",
    ]));
    await advance(15);
    await vi.waitFor(() => expect(spoken.at(-1)).toMatch(/^Stopped at the 1:15 limit\. Recording stopped/));
    expect(mic.track.stop).toHaveBeenCalled();
    expect(byId("vt-record").getAttribute("aria-pressed")).toBe("false");
  });

  it("stops at the installed ten-minute manual recording limit", async () => {
    const { controls, server, tickNow } = tracker();
    await controls.refresh();
    expect(server.limits.max_seconds).toBe(BROWSER_MAX_SECONDS);
    await press("vt-record");
    await tickNow(599);
    expect(byId("vt-timer").textContent).toBe("Recording 9:59 of 10:00.");
    await tickNow(600);
    await vi.waitFor(() => expect(status()).toMatch(/^Stopped at the 10:00 limit\./));
    expect(byId("vt-record").getAttribute("aria-pressed")).toBe("false");
    expect(byId("vt-playback").getAttribute("src")).toBe("blob:take");
  });

  it("warns when the song change is about to close during a recording", async () => {
    const { controls, server, advance } = tracker();
    server.slot = slot(undefined, { closes_in_s: 40 });
    await controls.refresh();
    await press("vt-record");
    await advance(10);
    await vi.waitFor(() => expect(status()).toBe("The song change closes in about 0:30."));
  });

  it.each([
    [{ name: "NotAllowedError" }, /blocked the microphone/],
    [{ name: "NotFoundError" }, /No microphone was found/],
  ])("shows and links a microphone refusal (%o)", async (problem, words) => {
    const { controls, mic } = tracker();
    mic.getUserMedia.mockRejectedValueOnce(problem);
    await controls.refresh();
    const record = await press("vt-record");
    expect(byId("vt-error").textContent).toMatch(words);
    expect(byId("vt-error").hidden).toBe(false);
    expect(record.getAttribute("aria-describedby")).toBe("vt-error vt-record-desc");
    expect(record.getAttribute("aria-pressed")).toBe("false");
    expect(document.activeElement).toBe(record);
    await vi.waitFor(() => expect(status()).toMatch(words));
  });

  it("explains a page that cannot record at all", async () => {
    let state = tracker({ media: () => undefined, secure: () => false });
    await state.controls.refresh();
    await press("vt-record");
    expect(byId("vt-error").textContent).toBe(INSECURE_TEXT);
    state = tracker({ Recorder: () => undefined });
    await state.controls.refresh();
    await press("vt-record");
    expect(byId("vt-error").textContent).toBe(NO_RECORDER_TEXT);
    expect(state.mic.getUserMedia).not.toHaveBeenCalled();
  });

  it("books the take as a WAV through the session and lists it, focus staying on Book", async () => {
    const { controls, server, toWav } = tracker();
    await controls.refresh();
    await press("vt-record");
    await press("vt-record");
    byId("vt-placement").value = "standalone";
    byId("vt-allow-outro").checked = true;
    const book = await press("vt-book");
    await vi.waitFor(() => expect(status()).toBe("Booked Third Song into Fourth Song. It airs in about 6:00."));
    expect(toWav).toHaveBeenCalledWith(expect.any(Blob), 44100);
    const post = server.calls.find((call) => call.method === "POST");
    expect(post.url).toBe("/api/voice-tracks");
    expect(Object.fromEntries([...post.body.entries()].filter(([key]) => key !== "file"))).toEqual({
      slot_id: "a".repeat(32), ending_id: "song-c", upcoming_id: "song-d",
      placement: "standalone", allow_outro: "true",
    });
    expect(post.body.get("file").name).toBe("voice-track.wav");
    expect(document.activeElement).toBe(book);
    expect(byId("vt-playback").hasAttribute("src")).toBe(false);
    const rows = byId("vt-list").querySelectorAll("li");
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toMatch(/^Third Song into Fourth Song: booked, waiting for its song change, airs in about 6:00 Cancel$/);
    expect(rows[0].querySelector("button").getAttribute("aria-label")).toBe("Cancel voice track Third Song into Fourth Song");
    expect(byId("vt-none").hidden).toBe(true);
  });

  it("aborts a slow upload on station clear without restoring the old take", async () => {
    const { controls, server } = tracker();
    await controls.refresh();
    await press("vt-record");
    await press("vt-record");
    server.slowPost = true;
    byId("vt-book").click();
    await vi.waitFor(() => expect(server.rejectPost).toBeTypeOf("function"));
    controls.clear();
    await flush();
    expect(server.calls.find((call) => call.method === "POST").signal.aborted).toBe(true);
    expect(byId("vt-playback").hasAttribute("src")).toBe(false);
    expect(byId("vt-error").textContent).toBe("");
    expect(server.calls.filter((call) => call.method === "POST")).toHaveLength(1);
  });

  it("does not start an upload when station clear wins during WAV conversion", async () => {
    let resolveWav;
    const { controls, server } = tracker({
      toWav: vi.fn(() => new Promise((resolve) => { resolveWav = resolve; })),
    });
    await controls.refresh();
    await press("vt-record");
    await press("vt-record");
    byId("vt-book").click();
    await vi.waitFor(() => expect(resolveWav).toBeTypeOf("function"));
    controls.clear();
    resolveWav(new Blob([new Uint8Array(1000)], { type: "audio/wav" }));
    await flush();
    expect(server.calls.filter((call) => call.method === "POST")).toHaveLength(0);
    expect(byId("vt-playback").hasAttribute("src")).toBe(false);
  });

  it("will not book a take on other songs until it is heard again and Book is pressed again", async () => {
    const { controls, server, advance } = tracker();
    await controls.refresh();
    await press("vt-record");
    await press("vt-record");
    server.slot = slot("c".repeat(32), { ending: song("song-d", "Fourth Song"), upcoming: song("song-e", "Fifth Song") });
    await advance(5);
    await vi.waitFor(() => expect(status()).toBe("The open song change is now Fourth Song into Fifth Song."));
    const book = await press("vt-book");
    const posts = () => server.calls.filter((call) => call.method === "POST");
    expect(posts()).toHaveLength(0);
    expect(byId("vt-error").textContent).toBe(
      "The song change you recorded for, Third Song into Fourth Song, has closed, and the open one, Fourth Song into Fifth Song, has other songs, so the take was not booked.");
    expect(book.getAttribute("aria-describedby")).toBe("vt-error");
    // Book again without listening: still refused, saying why.
    await press("vt-book");
    expect(posts()).toHaveLength(0);
    expect(byId("vt-error").textContent).toMatch(/^Listen back to the take first: it was recorded for Third Song into Fourth Song, not Fourth Song into Fifth Song\./);
    const player = byId("vt-playback");
    player.play = vi.fn(async () => {});
    await press("vt-listen");
    await press("vt-book");
    expect(posts()).toHaveLength(1);
    expect(posts()[0].body.get("slot_id")).toBe("c".repeat(32));
    expect(posts()[0].body.get("upcoming_id")).toBe("song-e");
    expect(byId("vt-error").hidden).toBe(true);
    expect(book.hasAttribute("aria-describedby")).toBe(false);
  });

  it("books a take on a new plan of the same two songs at once", async () => {
    const { controls, server, advance } = tracker();
    await controls.refresh();
    await press("vt-record");
    await press("vt-record");
    server.slot = slot("c".repeat(32));
    await advance(5);
    await press("vt-book");
    const post = server.calls.find((call) => call.method === "POST");
    expect(post.body.get("slot_id")).toBe("c".repeat(32));
  });

  it("explains a refusal from the server and keeps the take", async () => {
    const { controls, server } = tracker();
    await controls.refresh();
    await press("vt-record");
    await press("vt-record");
    server.refuse = { status: 422, message: "That break is unusable: the audio is silent" };
    await press("vt-book");
    expect(byId("vt-error").textContent).toBe("Could not book the voice track. That break is unusable: the audio is silent");
    expect(byId("vt-playback").getAttribute("src")).toBe("blob:take");
    // The slot closed between the page's last look and the upload.
    server.refuse = { status: 409, message: "That song change is no longer open" };
    server.slot = slot("d".repeat(32));
    await press("vt-book");
    expect(byId("vt-error").textContent).toBe(
      "The song change for Third Song into Fourth Song was planned again with the same songs. Press Book voice track again to book the take.");
    // The songs changed between the page's last look and the upload.
    server.refuse = { status: 409, message: "The songs around that song change have changed" };
    server.slot = slot("e".repeat(32), { upcoming: song("song-x", "Other Song") });
    await press("vt-book");
    expect(byId("vt-error").textContent).toMatch(/has other songs, so the take was not booked\./);
  });

  it("stops recording and lets the microphone go when the tab is left or the page hidden", async () => {
    const { controls, mic, advance } = tracker();
    await controls.refresh();
    await press("vt-record");
    const spoken = byId("sr-status");
    byId("panel-voice").hidden = true;
    await flush();
    expect(mic.track.stop).toHaveBeenCalledTimes(1);
    expect(byId("vt-record").getAttribute("aria-pressed")).toBe("false");
    expect(byId("vt-playback").getAttribute("src")).toBe("blob:take");
    // Said once, in the page-wide region: the tab's own is hidden.
    await vi.waitFor(() => expect(spoken.textContent).toMatch(/^You left the Voice tracks tab, so recording stopped\. Recording stopped at/));
    await advance(2);
    expect(mic.track.stop).toHaveBeenCalledTimes(1);

    byId("panel-voice").hidden = false;
    await press("vt-record");
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    try {
      document.dispatchEvent(new Event("visibilitychange"));
      await flush();
    expect(mic.tracks[1].stop).toHaveBeenCalledTimes(1);
      await vi.waitFor(() => expect(status()).toMatch(/^The page was hidden, so recording stopped\./));
    } finally {
      delete document.visibilityState;
    }
  });

  it("listens back with a toggle, then discards the take, focus staying put", async () => {
    const { controls } = tracker();
    await controls.refresh();
    const listen = await press("vt-listen");
    expect(byId("vt-error").textContent).toBe("Record a voice track first.");
    expect(listen.getAttribute("aria-describedby")).toBe("vt-error");
    await press("vt-record");
    await press("vt-record");
    const player = byId("vt-playback");
    player.play = vi.fn(async () => {});
    player.pause = vi.fn();
    await press("vt-listen");
    expect(player.play).toHaveBeenCalled();
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(listen.hasAttribute("aria-describedby")).toBe(false);
    player.dispatchEvent(new Event("ended"));
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    await press("vt-listen");
    await press("vt-listen");
    expect(player.pause).toHaveBeenCalled();
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    const discard = await press("vt-discard");
    expect(player.hasAttribute("src")).toBe(false);
    expect(document.activeElement).toBe(discard);
    await vi.waitFor(() => expect(status()).toBe("Recording discarded."));
    await press("vt-discard");
    await vi.waitFor(() => expect(status()).toBe("There is no recording to discard."));
  });

  it("cancels a booked track and moves focus to the list heading when its button goes", async () => {
    const { controls, server } = tracker();
    server.tracks = [{
      break_id: "b".repeat(32), label: "Third Song into Fourth Song", state: "queued",
      slot_id: "a".repeat(32), mixed_as: null, reason: null, eta: null,
    }];
    await controls.refresh();
    const cancel = byId("vt-list").querySelector("button");
    cancel.focus();
    cancel.click();
    await flush();
    expect(server.calls.at(-2)).toMatchObject({ method: "DELETE", url: `/api/voice-tracks/${"b".repeat(32)}` });
    expect(byId("vt-list").querySelector("button")).toBeNull();
    expect(byId("vt-list").textContent).toBe("Third Song into Fourth Song: dropped: cancelled in the voice tracker");
    expect(document.activeElement).toBe(byId("vt-booked-heading"));
    await vi.waitFor(() => expect(status()).toBe("Cancelled Third Song into Fourth Song."));
  });

  it("shows a failed Cancel on the page, linked to that row's button", async () => {
    const { controls, server } = tracker();
    server.tracks = [{
      break_id: "b".repeat(32), label: "Third Song into Fourth Song", state: "rendered",
      slot_id: "a".repeat(32), mixed_as: "talk_up", reason: null, eta: 1300,
    }, {
      break_id: "f".repeat(32), label: "Fourth Song into Fifth Song", state: "queued",
      slot_id: "a".repeat(32), mixed_as: null, reason: null, eta: null,
    }];
    await controls.refresh();
    const fallback = api.requestJson.getMockImplementation();
    api.requestJson.mockImplementation(async (url, options = {}) => {
      if (options.method === "DELETE") {
        throw Object.assign(new Error("That break is already mixed into the stream and will air"), { status: 409 });
      }
      return fallback(url, options);
    });
    const cancel = byId("vt-list").querySelector("button");
    cancel.focus();
    cancel.click();
    await flush();
    expect(byId("vt-error").hidden).toBe(false);
    expect(byId("vt-error").textContent).toBe(
      "Could not cancel Fourth Song into Fifth Song. That break is already mixed into the stream and will air");
    expect(cancel.getAttribute("aria-describedby")).toBe("vt-error");
    expect(document.activeElement).toBe(cancel);
  });

  it("keeps a focused row in place when the list refreshes", async () => {
    const { controls, server } = tracker();
    const track = (id, state) => ({
      break_id: id.repeat(32), label: `Track ${id}`, state, slot_id: "a".repeat(32), mixed_as: "talk_up", reason: null, eta: 1300,
    });
    server.tracks = [track("1", "queued"), track("2", "queued")];
    await controls.refresh();
    const second = byId("vt-list").querySelectorAll("button")[1];
    second.focus();
    server.tracks = [track("1", "rendered"), track("2", "queued")];
    await controls.refresh();
    expect(document.activeElement).toBe(second);
    expect(byId("vt-list").querySelector("li").textContent).toBe("Track 1: mixed in as a talk-up, airs in about 5:00");
  });

  it("starts and stops with Alt+R only while the tab shows, without moving focus", async () => {
    const { controls, mic } = tracker();
    await controls.refresh();
    const placement = byId("vt-placement");
    placement.focus();
    const event = altR(placement);
    await flush();
    expect(event.defaultPrevented).toBe(true);
    expect(mic.getUserMedia).toHaveBeenCalledTimes(1);
    expect(byId("vt-record").getAttribute("aria-pressed")).toBe("true");
    expect(document.activeElement).toBe(placement);
    altR(placement);
    await flush();
    expect(byId("vt-record").getAttribute("aria-pressed")).toBe("false");
    expect(document.activeElement).toBe(placement);
    byId("panel-voice").hidden = true;
    expect(altR(document.body).defaultPrevented).toBe(false);
    expect(mic.getUserMedia).toHaveBeenCalledTimes(1);
  });

  it("reports a recording or a take not booked as unsaved work, for a station switch", async () => {
    const { controls } = tracker();
    await controls.refresh();
    expect(controls.hasUnsavedWork()).toBe(false);
    await press("vt-record");
    expect(controls.hasUnsavedWork()).toBe(true);
    await press("vt-record");
    expect(controls.hasUnsavedWork()).toBe(true);
    await press("vt-discard");
    expect(controls.hasUnsavedWork()).toBe(false);
    await press("vt-record");
    await press("vt-record");
    controls.clear();
    expect(controls.hasUnsavedWork()).toBe(false);
  });

  it("lets the microphone go when the page signs out mid-recording", async () => {
    const { controls, mic } = tracker();
    await controls.refresh();
    await press("vt-record");
    controls.clear();
    await flush();
    expect(mic.track.stop).toHaveBeenCalled();
    expect(byId("vt-record").getAttribute("aria-pressed")).toBe("false");
    expect(byId("vt-playback").hasAttribute("src")).toBe(false);
    expect(byId("vt-slot").hidden).toBe(true);
  });
});


it("releases recording tracks when the tracker is removed", async () => {
  const { controls, mic, advance } = tracker();
  await controls.refresh();
  await press("vt-record");
  byId("voice-tracker").remove();
  await advance(1);
  expect(mic.track.stop).toHaveBeenCalled();
  controls.clear();
});
