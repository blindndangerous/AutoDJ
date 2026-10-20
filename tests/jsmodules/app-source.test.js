import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

const moduleMocks = [
  "../../src/autodj/static/modules/library-jobs.js",
  "../../src/autodj/static/modules/liners.js",
  "../../src/autodj/static/modules/hotkeys.js",
  "../../src/autodj/static/modules/media-session.js",
  "../../src/autodj/static/modules/audio-engine.js",
];
// Each test imports a fresh copy of app.js into the same document, and
// nothing unloads the copies before it.  Their document and window
// listeners and their timers outlived the test, so by the middle of this
// file one click on a Track info button was answered by forty apps at
// once, and opening it ran past vi.waitFor's one second on a busy CI
// runner. The cached microphone PermissionStatus and MutationObservers also
// retain app closures and their DOM. Track those through public APIs too;
// afterEach releases every resource installed by this fixture.
const appCleanups = [];

async function trackAppGlobals() {
  if (appCleanups.length) return;
  let permission;
  try {
    permission = await navigator.permissions?.query?.({ name: "microphone" });
  } catch {
    // A browser may not support querying microphone permission.
  }
  for (const target of [window, document, permission].filter(Boolean)) {
    const own = Object.hasOwn(target, "addEventListener");
    const original = target.addEventListener;
    const tracked = function (type, listener, options) {
      appCleanups.push(() => target.removeEventListener(type, listener, options));
      return original.call(this, type, listener, options);
    };
    target.addEventListener = tracked;
    appCleanups.push(() => {
      if (target.addEventListener !== tracked) return;
      if (own) target.addEventListener = original;
      else delete target.addEventListener;
    });
  }
  const prototype = window.MutationObserver.prototype;
  const observe = prototype.observe;
  const trackedObserve = function (...args) {
    const result = observe.apply(this, args);
    appCleanups.push(() => this.disconnect());
    return result;
  };
  prototype.observe = trackedObserve;
  appCleanups.push(() => {
    if (prototype.observe === trackedObserve) prototype.observe = observe;
  });
  for (const [set, clear] of [["setTimeout", "clearTimeout"], ["setInterval", "clearInterval"]]) {
    const original = globalThis[set];
    const cancel = globalThis[clear];
    // A fake clock installed after this restores the wrapper on
    // useRealTimers, so afterEach puts the original back only then.
    const tracked = function (...args) {
      const id = original.apply(this, args);
      appCleanups.push(() => cancel(id));
      return id;
    };
    globalThis[set] = tracked;
    appCleanups.push(() => {
      if (globalThis[set] === tracked) globalThis[set] = original;
    });
  }
}

function jsonResponse(body, status = 200) {
  return new globalThis.Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function installDocument() {
  const html = readFileSync(
    join(process.cwd(), "src/autodj/static/index.html"), "utf8",
  );
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll('script, link[rel="stylesheet"]')
    .forEach((element) => element.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  const dialog = document.querySelector("#auth-dialog");
  dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
  return dialog;
}

// How many track changes the liner cadence counted.
function countedTracks(bump) {
  return bump.mock.results.filter(({ value }) => value === true).length;
}

async function setupApp({
  audio = {},
  initialState = {},
  onAuthStatus = () => jsonResponse({ required: true, authenticated: true }),
  onInstallLiners = () => {},
  onRequest = (url) => {
    throw new Error(`Unexpected request: ${url}`);
  },
  // GET /api/stations: one station unless a test gives several (#70).
  stations = [{ slug: "default", name: "AutoDJ", state: "playing", listeners: 0, title: null }],
  // GET /api/version: the footer build stamp.
  version = { version: "test", commit: "test", built_at: "2026-01-01T00:00:00Z" },
} = {}) {
  vi.resetModules();
  await trackAppGlobals();
  const dialog = installDocument();
  const linerModule = await vi.importActual(moduleMocks[1]);
  const stopAllDecks = audio.stopAllDecks || vi.fn();
  const resetTrackCaches = audio.resetTrackCaches || vi.fn();
  const resetTransitionCaches = audio.resetTransitionCaches || vi.fn();
  const loadCoverArt = audio.loadCoverArt || vi.fn();
  const unlockAndPlay = audio.unlockAndPlay || vi.fn().mockResolvedValue();
  const startCrossfade = audio.startCrossfade || vi.fn().mockResolvedValue(true);
  const setLastBrowserPlayback = audio.setLastBrowserPlayback || vi.fn();
  const updateMediaSession = vi.fn();
  const bumpLinerTrackCount = vi.fn(linerModule.bumpLinerTrackCount);
  const audioDefaults = {
    _beatmatchOnSkip: false,
    _ctx: null,
    _crossfadeSecondsCache: 3,
    _inBpmCache: 0,
    _lastBrowserPlayback: false,
    _master: null,
    _nextTrackPathCache: null,
    _outBpmCache: 0,
    activeIdx: 0,
    applyBrowserPlaybackState: vi.fn(),
    applyEqState: vi.fn(),
    applyTransitionFx: vi.fn(),
    crossfading: false,
    deckActive: vi.fn(),
    decks: [],
    deckStandby: vi.fn(),
    duckMusic: vi.fn(),
    ensureAudioGraph: vi.fn(),
    isStaleAdvanceState: vi.fn(() => false),
    eqValueLabel: vi.fn(),
    loadCoverArt,
    musicSpeedRate: vi.fn(() => 1),
    playbackEnabled: false,
    playOnDeck: vi.fn(),
    postEq: vi.fn(),
    resetTrackCaches,
    resetTransitionCaches,
    setApplyState: vi.fn(),
    setLastBrowserPlayback,
    setLinkUp: vi.fn(),
    setMuted: vi.fn(),
    setSrcOnDeck: vi.fn(),
    setVolume: vi.fn(),
    startCrossfade,
    stopAllDecks,
    unlockAndPlay,
    ...audio,
  };
  vi.doMock(moduleMocks[0], () => ({
    applyLibraryJobState: vi.fn(),
    installLibraryJobs: vi.fn(),
    libraryJobStatusText: vi.fn(),
  }));
  vi.doMock(moduleMocks[1], () => ({
    bumpLinerTrackCount,
    installLiners: vi.fn(onInstallLiners),
  }));
  vi.doMock(moduleMocks[2], () => ({
    bracketSeekEnabled: vi.fn(() => false),
    installHotkeys: vi.fn(),
    toggleShortcutsModal: vi.fn(),
  }));
  vi.doMock(moduleMocks[3], () => ({
    installMediaActionHandlers: vi.fn(),
    updateMediaSession,
  }));
  vi.doMock(moduleMocks[4], () => audioDefaults);

  const state = {
    browser_playback: false,
    current_track: null,
    discovery_available: false,
    duration: 0,
    elapsed: 0,
    eq: {},
    is_muted: false,
    is_paused: false,
    next_track: null,
    queue: [],
    settings: null,
    volume: 1,
    ...initialState,
  };
  const fetchImpl = vi.fn((url, options) => {
    if (url === "/api/auth/status") {
      return Promise.resolve(onAuthStatus());
    }
    if (url === "/api/status") return Promise.resolve(jsonResponse(state));
    if (url === "/api/stations") {
      // null: the list cannot be loaded.
      return Promise.resolve(stations
        ? jsonResponse({ stations })
        : jsonResponse({ detail: "Station list unavailable" }, 503));
    }
    if (url === "/api/version") return Promise.resolve(jsonResponse(version));
    return Promise.resolve(onRequest(url, options));
  });
  const webSocket = { close: vi.fn(), readyState: 1, send: vi.fn() };
  const WebSocketImpl = vi.fn(function WebSocketMock() {
    return webSocket;
  });
  WebSocketImpl.CONNECTING = 0;
  WebSocketImpl.OPEN = 1;
  vi.stubGlobal("fetch", fetchImpl);
  vi.stubGlobal("WebSocket", WebSocketImpl);

  await import("../../src/autodj/static/app.js");
  try {
    await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledOnce());
  } catch (error) {
    throw new Error(`${error.message}; connection=${document.querySelector("#conn-status")?.textContent}; requests=${fetchImpl.mock.calls.map(([url]) => url).join(",")}`, {
      cause: error,
    });
  }
  return {
    bumpLinerTrackCount,
    dialog,
    fetchImpl,
    loadCoverArt,
    resetTrackCaches,
    resetTransitionCaches,
    setLastBrowserPlayback,
    startCrossfade,
    stopAllDecks,
    unlockAndPlay,
    updateMediaSession,
    webSocket,
    WebSocketImpl,
  };
}

function cleanupApp() {
  // A switch remembers its station per browser (#70); every test starts fresh.
  try {
    globalThis.localStorage?.removeItem("autodj.station");
  } catch {
    // No storage here: nothing was remembered.
  }
  // Real timers first: a fake clock put in after setupApp hands back the
  // tracking wrapper, which the cleanups then remove.
  vi.useRealTimers();
  for (const cleanup of appCleanups.splice(0)) {
    try {
      cleanup();
    } catch {
      // A fake clock's timer died with the clock.
    }
  }
  vi.unstubAllGlobals();
  for (const modulePath of moduleMocks) vi.doUnmock(modulePath);
  document.body.replaceChildren();
}

afterEach(cleanupApp);

describe("app fixture cleanup", () => {
  it("removes microphone permission callbacks from the discarded app", async () => {
    const permission = await navigator.permissions.query({ name: "microphone" });
    await setupApp({ onRequest: () => jsonResponse({ ok: true }) });
    const button = document.getElementById("audio-device-refresh");
    button.textContent = "Discarded app";
    cleanupApp();
    permission.dispatchEvent(new window.Event("change"));
    expect(button.textContent).toBe("Discarded app");
  });

  it("disconnects observers before discarding their DOM", async () => {
    await setupApp({ onRequest: () => jsonResponse({ ok: true }) });
    const target = document.getElementById("settings-status");
    const changed = vi.fn();
    const observer = new window.MutationObserver(changed);
    observer.observe(target, { attributes: true });
    try {
      cleanupApp();
      target.setAttribute("data-cleanup-probe", "changed");
      await Promise.resolve();
      expect(changed).not.toHaveBeenCalled();
    } finally {
      observer.disconnect();
    }
  });
});

describe("durable playback semantics", () => {
  it("exposes deliberate live regions and durable non-live descriptions", () => {
    installDocument();
    const queueAnnounce = document.querySelector("#queue-announce");
    const settingsStatus = document.querySelector("#settings-status");
    const linerStatus = document.querySelector("#ln-status");
    const queueList = document.querySelector("#queue-list");
    const metadata = document.querySelector("#now-playing-meta");
    const cueSummary = document.querySelector("#cue-summary");
    const cueDetails = document.querySelector("#cue-details");
    const progress = document.querySelector("#progress-track");

    expect(queueAnnounce.getAttribute("role")).toBe("status");
    expect(queueAnnounce.getAttribute("aria-live")).toBe("polite");
    expect(queueAnnounce.getAttribute("aria-atomic")).toBe("true");
    expect(settingsStatus.getAttribute("aria-atomic")).toBe("true");
    expect(linerStatus.getAttribute("role")).toBe("status");
    expect(linerStatus.getAttribute("aria-live")).toBe("polite");
    expect(linerStatus.getAttribute("aria-atomic")).toBe("true");

    expect(metadata.hasAttribute("aria-hidden")).toBe(false);
    expect(metadata.hasAttribute("role")).toBe(false);
    expect(metadata.hasAttribute("aria-live")).toBe(false);
    expect(cueSummary).not.toBeNull();
    expect(cueSummary.hasAttribute("role")).toBe(false);
    expect(cueSummary.hasAttribute("aria-live")).toBe(false);
    expect(progress.contains(cueSummary)).toBe(false);
    expect(cueDetails).not.toBeNull();
    expect(cueDetails.hasAttribute("role")).toBe(false);
    expect(cueDetails.hasAttribute("aria-live")).toBe(false);
    expect(progress.contains(cueDetails)).toBe(false);
    const describedBy = progress.getAttribute("aria-describedby").split(/\s+/);
    expect(describedBy).toContain("cue-summary");
    expect(describedBy).not.toContain("cue-details");

    expect(queueList.getAttribute("role")).toBe("list");
    expect(queueList.getAttribute("aria-label")).toBe("Queued tracks");
    expect(queueList.getAttribute("tabindex")).toBe("-1");
    expect(queueList.hasAttribute("aria-live")).toBe(false);
  });

  it("renders complete metadata and cue text without rewriting unchanged ticks", async () => {
    const track = {
      album: "Night Drive",
      bpm: 128,
      cues: [{ type: "drop", time_s: 30 }],
      energy: 0.7,
      key_label: "8A",
      length: 120,
      path: "current.mp3",
      title: "Current",
    };
    const { webSocket } = await setupApp({
      initialState: { current_track: track, duration: 120 },
      onRequest: (url) => url.startsWith("/api/lyrics?path=")
        ? jsonResponse({ lyrics: [], path: track.path })
        : jsonResponse({ ok: true }),
    });
    const metadata = document.querySelector("#now-playing-meta");
    const cueSummary = document.querySelector("#cue-summary");
    const cueDetails = document.querySelector("#cue-details");
    expect(cueSummary).not.toBeNull();
    expect(cueDetails).not.toBeNull();
    expect(metadata.textContent).toBe(
      "Album Night Drive · BPM 128 · Key 8A · Energy 0.70",
    );
    expect(cueSummary.textContent).toBe("1 cue point, drop at 0:30");
    expect(cueDetails.textContent).toBe("1 cue point, drop at 0:30");
    let metadataWrites = 0;
    let cueWrites = 0;
    let cueDetailWrites = 0;
    const descriptor = Object.getOwnPropertyDescriptor(
      globalThis.Node.prototype,
      "textContent",
    );
    const countWrites = (element, count) => {
      let storedText = element.textContent;
      Object.defineProperty(element, "textContent", {
        configurable: true,
        get() { return storedText; },
        set(value) {
          count();
          storedText = String(value);
          descriptor.set.call(this, value);
        },
      });
    };
    countWrites(metadata, () => { metadataWrites += 1; });
    countWrites(cueSummary, () => { cueWrites += 1; });
    countWrites(cueDetails, () => { cueDetailWrites += 1; });

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false,
      current_track: track,
      discovery_available: false,
      duration: 120,
      elapsed: 1,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });

    expect(metadataWrites).toBe(0);
    expect(cueWrites).toBe(0);
    expect(cueDetailWrites).toBe(0);
  });

  it("resets the cue summary when applied state has no track", async () => {
    const { webSocket } = await setupApp({
      initialState: {
        current_track: {
          cues: [{ type: "drop", time_s: 10 }],
          length: 60,
          path: "current.mp3",
          title: "Current",
        },
        duration: 60,
      },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const cueSummary = document.querySelector("#cue-summary");
    const cueDetails = document.querySelector("#cue-details");
    const metadata = document.querySelector("#now-playing-meta");
    expect(cueSummary).not.toBeNull();
    expect(cueDetails).not.toBeNull();
    expect(cueSummary.textContent).toContain("drop at 0:10");

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false,
      current_track: null,
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });

    expect(cueSummary.textContent).toBe("No cue points");
    expect(cueDetails.textContent).toBe("No cue points");
    expect(metadata.textContent).toBe("");
  });
});

describe("footer build stamp", () => {
  const built = "2026-01-01T00:00:00Z";

  it("links a known commit, its name starting with the words shown", async () => {
    const url = "https://github.com/blindndangerous/AutoDJ/commit/1a2b3c4";
    await setupApp({
      version: { version: "0.20.0", commit: "1a2b3c4", built_at: built, commit_url: url },
    });
    const link = await vi.waitFor(() => {
      const found = document.querySelector("#ver-build > a#ver-commit-link");
      expect(found).not.toBeNull();
      return found;
    });
    expect(link.getAttribute("href")).toBe(url);
    expect(link.target).toBe("_blank");
    expect(link.rel.split(" ")).toEqual(expect.arrayContaining(["noopener", "noreferrer"]));
    expect(link.querySelector("#ver-commit").textContent).toBe("1a2b3c4");
    // Visible text first (2.5.3), then where it goes, in one name: hidden
    // text inside the link would be read as a second line.
    expect(link.textContent.replace(/\s+/g, " ").trim()).toBe("build 1a2b3c4");
    expect(link.getAttribute("aria-label")).toBe("build 1a2b3c4 on GitHub (opens in a new tab)");
    // Reference data, not a status update: still no live region.
    expect(document.querySelector("#version-stamp [aria-live]")).toBeNull();
    expect(document.querySelector("#version-stamp").hasAttribute("aria-live")).toBe(false);
  });

  it("keeps an unknown commit as plain text", async () => {
    await setupApp({ version: { version: "0.20.0", commit: "unknown", built_at: built } });
    await vi.waitFor(() => expect(document.querySelector("#ver-commit").textContent)
      .toBe("unknown"));
    expect(document.querySelector("#version-stamp a")).toBeNull();
    expect(document.querySelector("#ver-build").textContent.replace(/\s+/g, " ").trim())
      .toBe("build unknown");
  });

  it.each([
    "javascript:alert(1)",
    "https://evil.example/blindndangerous/AutoDJ/commit/1a2b3c4",
    "https://github.com/blindndangerous/AutoDJ/commit/1a2b3c4?x=1",
    "https://github.com/blindndangerous/AutoDJ/commit/not-a-sha",
  ])("does not link a commit address it does not expect: %s", async (url) => {
    await setupApp({
      version: { version: "0.20.0", commit: "1a2b3c4", built_at: built, commit_url: url },
    });
    await vi.waitFor(() => expect(document.querySelector("#ver-commit").textContent)
      .toBe("1a2b3c4"));
    expect(document.querySelector("#version-stamp a")).toBeNull();
  });
});

describe("app request behavior", () => {
  it("continues applying unrelated state while pointer seeking", async () => {
    const { setLastBrowserPlayback, webSocket } = await setupApp({
      initialState: {
        current_track: { path: "same.flac", title: "Same" },
        duration: 120,
        elapsed: 0,
      },
      onRequest: (url) => url.startsWith("/api/lyrics?path=")
        ? jsonResponse({ path: "same.flac", lyrics: [] })
        : jsonResponse({ ok: true }),
    });
    const progress = document.querySelector("#progress-track");
    progress.getBoundingClientRect = () => ({ bottom: 20, left: 0, right: 100, top: 0, width: 100 });
    const down = new Event("pointerdown", { bubbles: true, cancelable: true });
    Object.defineProperties(down, {
      button: { value: 0 },
      clientX: { value: 50 },
      isPrimary: { value: true },
      pointerId: { value: 23 },
    });
    progress.dispatchEvent(down);

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: true,
      current_track: { path: "same.flac", title: "Same" },
      discovery_available: false,
      duration: 120,
      elapsed: 30,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });

    expect(document.querySelector("#progress-fill").style.width).toBe("50.0%");
    expect(setLastBrowserPlayback).toHaveBeenLastCalledWith(true);
  });

  it("cancels an active seek whenever applied state has no track", async () => {
    const { fetchImpl, webSocket } = await setupApp({
      initialState: { duration: 120, elapsed: 0 },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const progress = document.querySelector("#progress-track");
    progress.getBoundingClientRect = () => ({ bottom: 20, left: 0, right: 100, top: 0, width: 100 });
    const dispatchPointer = (type, clientX) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        clientY: { value: 10 },
        isPrimary: { value: true },
        pointerId: { value: 24 },
      });
      progress.dispatchEvent(event);
    };
    dispatchPointer("pointerdown", 50);

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false,
      current_track: null,
      discovery_available: false,
      duration: 120,
      elapsed: 30,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });
    dispatchPointer("pointerup", 100);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.querySelector("#progress-fill").style.width).toBe("25.0%");
    const seekBodies = fetchImpl.mock.calls
      .filter(([url]) => url === "/api/seek")
      .map(([, options]) => JSON.parse(options.body));
    expect(seekBodies).toEqual([]);
  });

  it("cancels an active seek when the current track changes", async () => {
    const pathFromLyricsUrl = (url) => decodeURIComponent(url.split("path=")[1]);
    const deckAudio = { currentTime: 10, duration: 120 };
    const { fetchImpl, webSocket } = await setupApp({
      audio: {
        _lastBrowserPlayback: true,
        decks: [{ audio: deckAudio }],
        playbackEnabled: true,
      },
      initialState: {
        current_track: { path: "first.flac", title: "First" },
        duration: 120,
      },
      onRequest: (url) => url.startsWith("/api/lyrics?path=")
        ? jsonResponse({ path: pathFromLyricsUrl(url), lyrics: [] })
        : jsonResponse({ ok: true }),
    });
    const progress = document.querySelector("#progress-track");
    progress.getBoundingClientRect = () => ({ bottom: 20, left: 0, right: 100, top: 0, width: 100 });
    const dispatchPointer = (type, clientX) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        clientY: { value: 10 },
        isPrimary: { value: true },
        pointerId: { value: 29 },
      });
      progress.dispatchEvent(event);
    };
    dispatchPointer("pointerdown", 50);

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false,
      current_track: { path: "second.flac", title: "Second" },
      discovery_available: false,
      duration: 200,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }) });
    dispatchPointer("pointerup", 100);
    await new Promise((resolve) => setTimeout(resolve, 0));

    const seekBodies = fetchImpl.mock.calls
      .filter(([url]) => url === "/api/seek")
      .map(([, options]) => JSON.parse(options.body));
    expect(seekBodies).toEqual([]);
    expect(deckAudio.currentTime).toBe(10);
  });

  it("passes every applied state through the liner cadence hook", async () => {
    const initialTrack = { path: "current.mp3", title: "Current" };
    const { bumpLinerTrackCount, webSocket } = await setupApp({
      initialState: { current_track: initialTrack },
    });
    expect(bumpLinerTrackCount).toHaveBeenCalledWith(
      expect.objectContaining({ current_track: initialTrack }),
    );

    const nextState = {
      browser_playback: false,
      current_track: null,
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    };
    webSocket.onmessage({ data: JSON.stringify(nextState) });

    expect(bumpLinerTrackCount).toHaveBeenLastCalledWith(nextState);
  });

  it("reports a failing saved sink once, not once per devicechange", async () => {
    const deck = {
      audio: { setSinkId: vi.fn().mockRejectedValue(new Error("device gone")) },
    };
    let deviceChange = null;
    vi.stubGlobal("navigator", {
      mediaDevices: {
        enumerateDevices: vi.fn().mockResolvedValue([
          { deviceId: "usb-dac", kind: "audiooutput", label: "USB DAC" },
        ]),
        addEventListener: (type, handler) => {
          if (type === "devicechange") deviceChange = handler;
        },
      },
    });
    // happy-dom exposes localStorage on window, not on globalThis.
    const store = new Map([["autodj.sinkId", "usb-dac"]]);
    vi.stubGlobal("localStorage", {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, value),
      removeItem: (key) => store.delete(key),
    });
    vi.stubGlobal("HTMLMediaElement", { prototype: { setSinkId() {} } });

    await setupApp({
      audio: { decks: [deck] },
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const status = document.querySelector("#settings-status");
    const records = [];
    await vi.waitFor(() =>
      expect(status.textContent).toContain("Could not switch audio device"));
    new window.MutationObserver((r) => records.push(...r))
      .observe(status, { childList: true, characterData: true, subtree: true });

    // Three USB plug events, same failure every time.
    for (let i = 0; i < 3; i += 1) {
      deviceChange();
      await vi.waitFor(() => expect(deck.audio.setSinkId).toHaveBeenCalled());
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(records.filter((r) => r.addedNodes.length > 0)).toHaveLength(0);

    // The user picking the device is a fresh action and must be reported,
    // even though the sentence has not changed.
    document.querySelector("#audio-device").value = "usb-dac";
    document.querySelector("#audio-device")
      .dispatchEvent(new window.Event("change"));
    await vi.waitFor(() =>
      expect(records.filter((r) => r.addedNodes.length > 0)).toHaveLength(1));

  });

  it("lets a preview on its own device keep its own volume and mute", async () => {
    vi.stubGlobal("navigator", {
      mediaDevices: {
        enumerateDevices: vi.fn().mockResolvedValue([
          { deviceId: "phones", kind: "audiooutput", label: "Headphones" },
        ]),
        addEventListener: () => {},
      },
    });
    const store = new Map([["autodj.previewSinkId", "phones"]]);
    vi.stubGlobal("localStorage", {
      getItem: (key) => (store.has(key) ? store.get(key) : null),
      setItem: (key, value) => store.set(key, value),
      removeItem: (key) => store.delete(key),
    });
    vi.stubGlobal("HTMLMediaElement", { prototype: { setSinkId() {} } });
    const state = {
      current_track: { path: "a.mp3", title: "A" },
      volume: 0.5,
      is_muted: true,
    };
    const { webSocket } = await setupApp({
      initialState: state,
      onRequest: () => jsonResponse({ ok: true }),
    });
    const preview = document.getElementById("points-preview");
    await vi.waitFor(() => expect(preview.sinkId).toBe("phones"));
    // On its own device it starts at full level and unmuted, and the
    // page's volume and mute leave it alone, including a level set in its
    // own player.
    expect(preview.volume).toBe(1);
    expect(preview.muted).toBe(false);
    preview.volume = 0.3;
    webSocket.onmessage({ data: JSON.stringify({ ...state, volume: 0.4 }) });
    expect(preview.volume).toBe(0.3);
    expect(preview.muted).toBe(false);

    // Back on the system default, it follows the page again.
    const select = document.querySelector("#preview-device");
    select.value = "";
    select.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(preview.sinkId).toBe(""));
    expect(preview.volume).toBe(document.getElementById("stream-audio").volume);
    expect(preview.volume).toBeLessThan(1);
    expect(preview.muted).toBe(true);
    expect(store.has("autodj.previewSinkId")).toBe(false);
  });

  it("announces a lost link once, however many retries it takes", async () => {
    const { webSocket } = await setupApp({
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const pill = document.querySelector("#conn-status");
    const records = [];
    new window.MutationObserver((r) => records.push(...r))
      .observe(pill, { childList: true, characterData: true, subtree: true });
    vi.useFakeTimers();

    // Ten failed cycles.  The old loop wrote "Connecting...", "Error" and
    // "Disconnected" once per cycle -- thirty announcements.
    let delay = 3000;
    for (let cycle = 0; cycle < 10; cycle += 1) {
      webSocket.onclose({ code: 1006, wasClean: false });
      webSocket.onerror();
      await vi.advanceTimersByTimeAsync(delay);
      delay = Math.min(delay * 2, 60000);
    }

    expect(records.filter((r) => r.addedNodes.length > 0)).toHaveLength(1);
    expect(pill.textContent).toBe("Disconnected, retrying");
  });

  it("backs off between retries instead of hammering every three seconds", async () => {
    const { webSocket, WebSocketImpl } = await setupApp({
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onRequest: () => jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    await vi.advanceTimersByTimeAsync(2999);
    expect(WebSocketImpl).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(WebSocketImpl).toHaveBeenCalledTimes(2);

    // Second failure waits six seconds, not three.
    webSocket.onclose({ code: 1006, wasClean: false });
    await vi.advanceTimersByTimeAsync(3000);
    expect(WebSocketImpl).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(3000);
    expect(WebSocketImpl).toHaveBeenCalledTimes(3);
  });

  it("cancels a scheduled transient auth probe after REST expiry", async () => {
    let authStatusCalls = 0;
    const { dialog, webSocket, WebSocketImpl } = await setupApp({
      initialState: {
        current_track: { path: "current.mp3", title: "Current" },
      },
      onAuthStatus: () => {
        authStatusCalls += 1;
        return jsonResponse({ required: true, authenticated: true });
      },
      onRequest: (url) => url === "/api/pause"
        ? jsonResponse({ detail: "Authentication required" }, 401)
        : jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(3000);

    expect(authStatusCalls).toBe(1);
    expect(WebSocketImpl).toHaveBeenCalledOnce();
  });

  it("ignores an in-flight transient probe failure after REST expiry", async () => {
    let authStatusCalls = 0;
    let rejectProbe;
    const { dialog, webSocket, WebSocketImpl } = await setupApp({
      initialState: {
        current_track: { path: "current.mp3", title: "Current" },
      },
      onAuthStatus: () => {
        authStatusCalls += 1;
        if (authStatusCalls === 1) {
          return jsonResponse({ required: true, authenticated: true });
        }
        return new Promise((_resolve, reject) => { rejectProbe = reject; });
      },
      onRequest: (url) => url === "/api/pause"
        ? jsonResponse({ detail: "Authentication required" }, 401)
        : jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    await vi.advanceTimersByTimeAsync(3000);
    expect(authStatusCalls).toBe(2);
    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    rejectProbe(new Error("transient probe failed"));
    await vi.advanceTimersByTimeAsync(0);

    expect(WebSocketImpl).toHaveBeenCalledOnce();
  });

  it("says the new play state once after Pause toggles", async () => {
    let paused = false;
    await setupApp({
      initialState: {
        current_track: { path: "current.mp3", title: "Current" },
      },
      onRequest: (url) => {
        if (url !== "/api/pause") return jsonResponse({ ok: true });
        paused = !paused;
        return jsonResponse({ paused });
      },
    });
    const status = document.querySelector("#sr-status");
    const pause = document.querySelector("#btn-pause");

    pause.click();
    await vi.waitFor(() => expect(status.textContent).toBe("Paused"));
    pause.click();
    await vi.waitFor(() => expect(status.textContent).toBe("Playing"));
  });

  it("keeps playing through a dropped link without clearing session data", async () => {
    let linerDeps;
    const source = {
      addEventListener: vi.fn(),
      connect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
    };
    const audioContext = {
      createBufferSource: vi.fn(() => source),
      createGain: vi.fn(() => ({ gain: { value: 1 }, connect: vi.fn() })),
      currentTime: 2,
      decodeAudioData: vi.fn().mockResolvedValue({ duration: 1 }),
      destination: {},
    };
    const stopAllDecks = vi.fn();
    const resetTrackCaches = vi.fn();
    const resetTransitionCaches = vi.fn();
    const {
      dialog, loadCoverArt, webSocket, WebSocketImpl,
    } = await setupApp({
      audio: {
        _ctx: audioContext,
        decks: [{ gain: { gain: {
          cancelScheduledValues: vi.fn(),
          linearRampToValueAtTime: vi.fn(),
          setValueAtTime: vi.fn(),
          value: 1,
        } } }],
        playbackEnabled: true,
        resetTrackCaches,
        resetTransitionCaches,
        stopAllDecks,
      },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
      },
      onInstallLiners: (_elements, deps) => { linerDeps = deps; },
      onRequest: () => jsonResponse({ lyrics: [] }),
    });
    expect(await linerDeps.playLiner(new ArrayBuffer(1), -12)).toBe(true);
    const nowPlaying = document.querySelector("#now-playing-title");
    for (let index = 2; index <= 5; index += 1) {
      webSocket.onmessage({ data: JSON.stringify({
        browser_playback: true,
        current_track: { path: `track-${index}.mp3`, title: `Track ${index}` },
        discovery_available: false,
        duration: 0,
        elapsed: 0,
        eq: {},
        is_muted: false,
        is_paused: false,
        next_track: null,
        queue: [],
        settings: null,
        volume: 1,
      }) });
    }
    expect(nowPlaying.textContent).toContain("Track 5");
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });

    // A tunnel drops the link every few minutes: the decks and the liner
    // play on, and the next track's markers are kept for the fade.
    expect(stopAllDecks).not.toHaveBeenCalled();
    expect(source.stop).not.toHaveBeenCalled();
    expect(resetTrackCaches).not.toHaveBeenCalled();
    expect(resetTransitionCaches).not.toHaveBeenCalled();
    expect(loadCoverArt).not.toHaveBeenCalledWith(null);
    expect(nowPlaying.textContent).toContain("Track 5");
    expect(dialog.showModal).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledTimes(2));
    expect(dialog.showModal).not.toHaveBeenCalled();
  });

  it("owns first-click unlock until it settles and restores Pause", async () => {
    let resolveUnlock;
    const unlockAndPlay = vi.fn(() => new Promise((resolve) => {
      resolveUnlock = resolve;
    }));
    await setupApp({
      audio: { _lastBrowserPlayback: true, playbackEnabled: false, unlockAndPlay },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
      },
    });
    const pause = document.querySelector("#btn-pause");

    pause.click();
    pause.click();
    expect(unlockAndPlay).toHaveBeenCalledOnce();
    expect(pause.disabled).toBe(true);
    resolveUnlock();
    await vi.waitFor(() => expect(pause.disabled).toBe(false));
  });

  it("says Playing after the first Play only when playback really started", async () => {
    const unlockAndPlay = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    await setupApp({
      audio: { _lastBrowserPlayback: true, playbackEnabled: false, unlockAndPlay },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
      },
    });
    const pause = document.querySelector("#btn-pause");
    const status = document.querySelector("#sr-status");

    pause.click();
    await vi.waitFor(() => expect(unlockAndPlay).toHaveBeenCalledOnce());
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(status.textContent).not.toBe("Playing");

    pause.click();
    await vi.waitFor(() => expect(status.textContent).toBe("Playing"));
  });

  it("keeps Skip owned through a browser crossfade and ignores double click", async () => {
    let resolveCrossfade;
    const startCrossfade = vi.fn(() => new Promise((resolve) => {
      resolveCrossfade = resolve;
    }));
    const { fetchImpl } = await setupApp({
      audio: {
        _ctx: {},
        _lastBrowserPlayback: true,
        _nextTrackPathCache: "next.mp3",
        crossfading: false,
        decks: [{ audio: { duration: Number.NaN } }],
        playbackEnabled: true,
        startCrossfade,
      },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
        next_track: { path: "next.mp3", title: "Next" },
      },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const skip = document.querySelector("#btn-skip");

    skip.click();
    skip.click();
    await vi.waitFor(() => expect(startCrossfade).toHaveBeenCalledOnce());
    expect(skip.disabled).toBe(true);
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/skip")).toBe(false);
    resolveCrossfade(true);
    await vi.waitFor(() => expect(skip.disabled).toBe(false));
  });

  it("sends an absolute seek with the exact seconds payload", async () => {
    const { fetchImpl } = await setupApp({
      initialState: { duration: 100 },
      onRequest: () => jsonResponse({ elapsed: 99 }),
    });
    const seek = document.querySelector("#progress-track");

    seek.dispatchEvent(new KeyboardEvent("keydown", {
      key: "End", bubbles: true, cancelable: true,
    }));
    await vi.waitFor(() => expect(fetchImpl.mock.calls.some(
      ([url]) => url === "/api/seek",
    )).toBe(true));
    const [, options] = fetchImpl.mock.calls.find(([url]) => url === "/api/seek");
    expect(JSON.parse(options.body)).toEqual({ seconds: 99 });
  });

  it("steps the focused seek slider from the real position, not its frozen value", async () => {
    const baseState = {
      current_track: { path: "a.mp3", title: "A" }, duration: 200, elapsed: 50,
    };
    const { fetchImpl, webSocket } = await setupApp({
      initialState: baseState,
      onRequest: () => jsonResponse({ ok: true }),
    });
    const seek = document.querySelector("#progress-track");
    seek.focus();
    const seeks = () => fetchImpl.mock.calls
      .filter(([url]) => url === "/api/seek")
      .map(([, options]) => JSON.parse(options.body).seconds);
    const press = (key) => seek.dispatchEvent(new KeyboardEvent("keydown", {
      key, bubbles: true, cancelable: true,
    }));

    // The playhead moved on while the slider held focus; its attribute did not.
    webSocket.onmessage({ data: JSON.stringify({ ...baseState, elapsed: 80 }) });
    expect(seek.getAttribute("aria-valuetext")).toBe("0:50 of 3:20");
    press("ArrowRight");
    await vi.waitFor(() => expect(seeks()).toEqual([85]));
    expect(seek.getAttribute("aria-valuetext")).toBe("1:25 of 3:20");

    // A stale tick lands before the server applies the seek; the next
    // press still builds on the position just requested.
    webSocket.onmessage({ data: JSON.stringify({ ...baseState, elapsed: 81 }) });
    press("ArrowRight");
    await vi.waitFor(() => expect(seeks()).toEqual([85, 90]));
    expect(seek.getAttribute("aria-valuetext")).toBe("1:30 of 3:20");
  });

  it("shows 0:00 / 0:00 between tracks instead of a time past the song (#428)", async () => {
    const baseState = {
      current_track: { path: "a.mp3", title: "A" }, duration: 200, elapsed: 150,
    };
    const { webSocket } = await setupApp({
      initialState: baseState,
      onRequest: () => jsonResponse({ ok: true }),
    });
    const { installHotkeys } = await import("../../src/autodj/static/modules/hotkeys.js");
    const hotkeys = installHotkeys.mock.calls[0][0];
    const seek = document.querySelector("#progress-track");
    const label = document.getElementById("progress-bar-label");
    const fill = document.getElementById("progress-fill");
    webSocket.onmessage({ data: JSON.stringify(baseState) });
    expect(label.textContent).toBe("2:30 / 3:20");
    expect(seek.getAttribute("aria-valuetext")).toBe("2:30 of 3:20");

    // A liner plays after the song; the server clock passes its length.
    webSocket.onmessage({ data: JSON.stringify({ ...baseState, elapsed: 207.4 }) });
    expect(label.textContent).toBe("0:00 / 0:00");
    expect(fill.style.width).toBe("0.0%");
    expect(seek.getAttribute("aria-valuenow")).toBe("0");
    expect(seek.getAttribute("aria-valuetext")).toBe("0:00 of 0:00");
    expect(hotkeys.getPosition()).toBeNull();
    expect(hotkeys.getRemaining()).toBeNull();

    // The next song starts and its clock shows as usual.
    webSocket.onmessage({ data: JSON.stringify({
      current_track: { path: "b.mp3", title: "B" }, duration: 180, elapsed: 3,
    }) });
    expect(label.textContent).toBe("0:03 / 3:00");
    expect(seek.getAttribute("aria-valuetext")).toBe("0:03 of 3:00");
    expect(hotkeys.getPosition()).toEqual({ elapsed: 3, duration: 180 });
  });

  it("finds no landmark ahead and does not seek between tracks (#428)", async () => {
    const between = {
      current_track: { path: "a.mp3", title: "A", bpm: 120 }, duration: 200, elapsed: 207,
    };
    const { fetchImpl, webSocket } = await setupApp({
      initialState: between,
      onRequest: (url) => url.includes("/landmarks?")
        ? jsonResponse({
          length: 200,
          landmarks: [
            { time_s: 12, major: true, kind: "drop", name: "Drop" },
            { time_s: 150, major: true, kind: "outro", name: "Outro" },
          ],
        })
        : jsonResponse({ ok: true }),
    });
    const hotkeysModule = await import("../../src/autodj/static/modules/hotkeys.js");
    const hotkeys = hotkeysModule.installHotkeys.mock.calls[0][0];
    // Bracket keys seek the song on air.
    hotkeysModule.bracketSeekEnabled.mockReturnValue(true);
    const status = document.querySelector("#sr-status");
    const seeks = () => fetchImpl.mock.calls.filter(([url]) => url === "/api/seek");
    webSocket.onmessage({ data: JSON.stringify(between) });

    // Shift+U: the song is over, so nothing in it is coming next.
    expect(await hotkeys.getUpcomingLandmark()).toBe("On air: No more landmarks in this song.");
    // Right bracket: no landmark ahead, so the station is not moved back into the song.
    await hotkeys.jumpLandmark(1);
    await vi.waitFor(() => expect(status.textContent)
      .toBe("On air: No more landmarks after this point in the song."));
    // Comma / Period: no position to move from or to say.
    hotkeys.seekDelta(5);
    await vi.waitFor(() => expect(status.textContent).toBe("Position unknown"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(seeks()).toEqual([]);
  });

  it("does not set a point from the clock between tracks (#428)", async () => {
    window.location.hash = "#now";
    try {
      const playing = {
        current_track: { path: "M:/song.flac", title: "Song" }, duration: 200, elapsed: 50,
      };
      const { webSocket } = await setupApp({
        initialState: playing,
        onRequest: (url) => url.includes("/library/points?")
          ? jsonResponse({
            path: "M:/song.flac", title: "Song", kind: "music", length: 200, points: {},
          })
          : jsonResponse({ ok: true }),
      });
      webSocket.onmessage({ data: JSON.stringify(playing) });
      document.querySelector("#edit-current-points").click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      const outro = document.querySelector('[data-point="outro"]');
      const setOutro = document.querySelector('[data-set-current="outro"]');
      await vi.waitFor(() => expect(setOutro.closest("[hidden]")).toBeNull());
      // Without the song in the preview, the button reads the station's clock.
      document.getElementById("points-preview").removeAttribute("src");
      setOutro.click();
      expect(outro.value).toBe("0:50");

      outro.value = "";
      webSocket.onmessage({ data: JSON.stringify({ ...playing, elapsed: 207 }) });
      setOutro.click();
      expect(outro.value).toBe("");
      expect(document.getElementById("points-status").textContent)
        .toBe("This track is not playing in the preview or on the station.");
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("saves key notation without announcing into the hidden Now Playing panel", async () => {
    const { fetchImpl } = await setupApp({ onRequest: () => jsonResponse({ ok: true }) });
    const notation = document.querySelector("#key-notation");
    notation.value = "musical";
    notation.dispatchEvent(new Event("change"));
    const flats = document.querySelector("#key-prefer-flats");
    flats.checked = true;
    flats.dispatchEvent(new Event("change"));

    await vi.waitFor(() => expect(fetchImpl.mock.calls
      .filter(([url]) => url === "/api/playback-settings")).toHaveLength(2));
    const nowPanel = document.querySelector("#panel-now").textContent;
    expect(nowPanel).not.toContain("Key notation");
    expect(nowPanel).not.toContain("key spelling");
  });

  it("keeps Play / Pause and Mute at fixed names, Mute with pressed state", async () => {
    const { webSocket } = await setupApp({
      initialState: { current_track: { path: "a.mp3", title: "A" } },
    });
    const pause = document.querySelector("#btn-pause");
    const mute = document.querySelector("#btn-mute");
    // The accessible name: aria-label, else the button's text without its
    // aria-hidden parts.
    const name = (button) => button.getAttribute("aria-label") ?? [...button.childNodes]
      .filter((node) => node.nodeType === 3 || !node.hasAttribute("aria-hidden"))
      .map((node) => node.textContent).join("").replace(/\s+/g, " ").trim();
    const glyph = (button) => button.querySelector('[aria-hidden="true"]').textContent;
    // Browse mode reads a button's text, not only its name: every word
    // Play / Pause shows is hidden, so its one name is all that is read
    // ("PlayorPause" in Chrome and three lines in Firefox before, D16).
    const readableText = (button) => {
      const walker = document.createTreeWalker(button, 4);
      let text = "";
      while (walker.nextNode()) {
        if (!walker.currentNode.parentElement.closest('[aria-hidden="true"]')) {
          text += walker.currentNode.textContent;
        }
      }
      return text.trim();
    };
    expect(pause.getAttribute("aria-label")).toBe("Play or Pause");
    expect(readableText(pause)).toBe("");
    // The visible words are the name, so speech input can say what it
    // sees (WCAG 2.5.3, label in name).
    expect(pause.textContent).toMatch(/ Play or Pause$/);

    webSocket.onmessage({ data: JSON.stringify({
      current_track: { path: "a.mp3", title: "A" }, is_paused: true, is_muted: true,
      queue: [], eq: {}, volume: 1,
    }) });
    // A label that changed with the state was spoken by Firefox on top of
    // the "Paused" status, so the name stays put and only the glyph moves.
    expect(name(pause)).toBe("Play or Pause");
    const pausedGlyph = glyph(pause);
    expect(pause.hasAttribute("aria-pressed")).toBe(false);
    expect(name(mute)).toBe("Mute");
    expect(mute.getAttribute("aria-pressed")).toBe("true");

    webSocket.onmessage({ data: JSON.stringify({
      current_track: { path: "a.mp3", title: "A" }, is_paused: false, is_muted: false,
      queue: [], eq: {}, volume: 1,
    }) });
    expect(name(pause)).toBe("Play or Pause");
    expect(readableText(pause)).toBe("");
    expect(pause.querySelectorAll("*")).toHaveLength(1);
    expect(glyph(pause)).not.toBe(pausedGlyph);
    expect(name(mute)).toBe("Mute");
    expect(mute.getAttribute("aria-pressed")).toBe("false");
  });

  it("leaves unchanged transport buttons untouched on a repeated push", async () => {
    const tick = {
      current_track: { path: "a.mp3", title: "A" }, is_paused: false, is_muted: true,
      discovery_available: true, discovery_enabled: true,
      queue: [], eq: {}, volume: 1,
    };
    const { webSocket } = await setupApp({ initialState: tick });
    webSocket.onmessage({ data: JSON.stringify(tick) });
    const records = [];
    const observer = new window.MutationObserver((batch) => records.push(...batch));
    for (const id of ["#btn-pause", "#btn-mute", "#btn-discovery"]) {
      observer.observe(document.querySelector(id), {
        attributes: true, childList: true, characterData: true, subtree: true,
      });
    }

    webSocket.onmessage({ data: JSON.stringify(tick) });
    await Promise.resolve();
    expect(records).toHaveLength(0);

    webSocket.onmessage({ data: JSON.stringify({ ...tick, is_paused: true }) });
    await Promise.resolve();
    expect(records.length).toBeGreaterThan(0);
    observer.disconnect();
  });

  it("announces a track change once, key included, with Up Next silent", async () => {
    const { webSocket } = await setupApp();
    webSocket.onmessage({ data: JSON.stringify({
      current_track: {
        path: "a.mp3", artist: "Artist", title: "Song", bpm: 128,
        key_label: "F#m", key_spoken: "F sharp minor",
      },
      next_track: { path: "b.mp3", artist: "Other", title: "Next" },
      queue: [], eq: {}, volume: 1,
    }) });

    const title = document.querySelector("#now-playing-title");
    expect(title.closest("[aria-live]")).toBeNull();
    expect(title.textContent).toBe("Artist — Song, 128 BPM");
    const region = document.querySelector("#track-announce");
    expect(region.parentElement).toBe(document.body);
    expect(region.textContent).toBe("Artist — Song, 128 BPM, key F sharp minor");
    const upNext = document.querySelector("#next-track-text");
    expect(upNext.textContent).toBe("Other — Next");
    expect(upNext.hasAttribute("aria-live")).toBe(false);
    expect(upNext.closest("[aria-live]")).toBeNull();
    expect(document.querySelector("#badges-announce")).toBeNull();
  });

  it("speaks a track change on every tab, only on Now Playing, or not at all", async () => {
    const { webSocket } = await setupApp();
    const region = document.querySelector("#track-announce");
    const choice = document.querySelector("#announce-track-changes");
    const push = (path) => webSocket.onmessage({ data: JSON.stringify({
      current_track: { path, artist: "Artist", title: path },
      queue: [], eq: {}, volume: 1,
    }) });
    document.querySelector("#panel-now").hidden = true;

    expect(choice.value).toBe("now");
    push("one.mp3");
    expect(region.textContent).not.toContain("one.mp3");

    choice.value = "all";
    choice.dispatchEvent(new Event("change"));
    push("two.mp3");
    expect(region.textContent).toBe("Artist — two.mp3");

    document.querySelector("#panel-now").hidden = false;
    choice.value = "off";
    choice.dispatchEvent(new Event("change"));
    push("three.mp3");
    expect(region.textContent).not.toContain("three.mp3");
  });

  it("leaves focus where a skip or mute key found it, and says the mute state", async () => {
    await setupApp({
      initialState: { current_track: { path: "a.mp3", title: "Song" } },
      onRequest: (url) => url === "/api/mute"
        ? jsonResponse({ muted: true })
        : jsonResponse({ ok: true }),
    });
    const eq = document.querySelector("#eq-low");
    eq.focus();

    document.querySelector("#btn-skip").click();
    document.querySelector("#btn-mute").click();
    await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent)
      .toBe("Muted"));
    expect(document.activeElement).toBe(eq);
  });

  it("speaks volume once: from the slider when focused, the region otherwise", async () => {
    await setupApp({ onRequest: () => jsonResponse({ ok: true }) });
    const slider = document.querySelector("#vol");
    const region = document.querySelector("#vol-announce");

    slider.focus();
    slider.value = "95";
    slider.dispatchEvent(new Event("input"));
    expect(slider.getAttribute("aria-valuetext")).toBe("95%");
    await new Promise((resolve) => setTimeout(resolve, 350));
    expect(region.textContent).toBe("");

    // A shortcut press while focus is elsewhere still gets spoken.
    slider.blur();
    slider.value = "90";
    slider.dispatchEvent(new Event("input"));
    expect(slider.getAttribute("aria-valuetext")).toBe("90%");
    await vi.waitFor(() => expect(region.textContent).toBe("Volume 90%."));
  });

  it("keeps a low volume through the server echo and hands it to the deck engine", async () => {
    const quiet = 10 ** (-57 / 20);   // the fader curve's gain for 5 %
    const setVolume = vi.fn();
    const { fetchImpl, webSocket } = await setupApp({
      audio: { setVolume },
      initialState: { volume: quiet },
      onRequest: (url, options) => jsonResponse(JSON.parse(options.body)),
    });
    const slider = document.querySelector("#vol");
    expect(slider.value).toBe("5");
    expect(slider.getAttribute("aria-valuetext")).toBe("5%");
    expect(setVolume).toHaveBeenCalledWith(quiet);

    vi.useFakeTimers();
    slider.value = "3";
    slider.dispatchEvent(new Event("input"));
    await vi.advanceTimersByTimeAsync(200);
    const sent = fetchImpl.mock.calls.find(([url]) => url === "/api/volume");
    const gain = JSON.parse(sent[1].body).volume;
    expect(gain).toBeGreaterThan(0);
    await vi.advanceTimersByTimeAsync(700);
    webSocket.onmessage({ data: JSON.stringify({
      current_track: null, queue: [], eq: {}, volume: gain,
    }) });
    expect(slider.value).toBe("3");
    expect(slider.getAttribute("aria-valuetext")).toBe("3%");
    expect(setVolume).toHaveBeenLastCalledWith(gain);
  });

  it("plays a Test liner into the master while paused, without ducking, and not while muted", async () => {
    const source = { addEventListener: vi.fn(), connect: vi.fn(), start: vi.fn() };
    const master = { gain: { value: 0.5 } };
    const deckGain = {
      cancelScheduledValues: vi.fn(), linearRampToValueAtTime: vi.fn(),
      setValueAtTime: vi.fn(), value: 1,
    };
    const audioContext = {
      createBufferSource: vi.fn(() => source),
      currentTime: 2,
      decodeAudioData: vi.fn().mockResolvedValue({ duration: 1 }),
      resume: vi.fn().mockResolvedValue(),
      state: "suspended",
    };
    const ensureAudioGraph = vi.fn(() => audioContext);
    const duckMusic = vi.fn();
    let linerDeps;
    const { webSocket } = await setupApp({
      audio: {
        _ctx: audioContext, _lastBrowserPlayback: true, _master: master,
        decks: [{ audio: {}, gain: { gain: deckGain } }], duckMusic, ensureAudioGraph,
        playbackEnabled: true,
      },
      initialState: { browser_playback: true, is_paused: true },
      onInstallLiners: (_elements, deps) => { linerDeps = deps; },
    });
    const push = (state) => webSocket.onmessage({ data: JSON.stringify({
      browser_playback: true, current_track: null, queue: [], eq: {}, volume: 0.5, ...state,
    }) });

    // Paused: nothing scheduled, but Test is ready and starts the audio.
    expect(linerDeps.canPlay()).toBe(false);
    expect(linerDeps.prepareTest()).toBeNull();
    expect(ensureAudioGraph).toHaveBeenCalled();
    expect(audioContext.resume).toHaveBeenCalled();
    expect(await linerDeps.playLiner(new ArrayBuffer(1), -12)).toBe(true);
    expect(source.connect).toHaveBeenCalledWith(master);
    expect(duckMusic).not.toHaveBeenCalled();

    push({ is_paused: false, is_muted: true });
    expect(linerDeps.prepareTest()).toBe("Muted, so the liner was not played.");

    // Playing and unmuted: the liner ducks the music, on its own stage
    // after the decks, so no deck gain (a crossfade's ramps) is touched.
    push({ is_paused: false, is_muted: false });
    expect(linerDeps.canPlay()).toBe(true);
    expect(await linerDeps.playLiner(new ArrayBuffer(1), -12)).toBe(true);
    expect(duckMusic).toHaveBeenCalledExactlyOnceWith(10 ** (-12 / 20), 1);
    expect(deckGain.cancelScheduledValues).not.toHaveBeenCalled();
    expect(deckGain.linearRampToValueAtTime).not.toHaveBeenCalled();
  });

  it("plays carts through the liner path in browser playback and speaks their events", async () => {
    const sources = [];
    const audioContext = {
      createBufferSource: vi.fn(() => {
        const source = { addEventListener: vi.fn(), connect: vi.fn(), start: vi.fn(), stop: vi.fn() };
        sources.push(source);
        return source;
      }),
      currentTime: 2,
      decodeAudioData: vi.fn().mockResolvedValue({ duration: 7 }),
      resume: vi.fn().mockResolvedValue(),
      state: "running",
    };
    const master = { gain: { value: 1 } };
    const duckMusic = vi.fn();
    const slot = (number, mode) => ({ name: `Cart${number}`, source: "liner", clip: "id", mode, duration: 7 });
    const slots = Array(12).fill(null);
    slots[0] = slot(1, "replace");
    slots[1] = slot(2, "over");
    slots[2] = slot(3, "duck");
    const wall = {
      wall: { active_page: 1, pages: [1, 2, 3, 4].map((n) => ({ name: `Page ${n}`, slots: n === 1 ? slots : Array(12).fill(null) })) },
      liners: [{ id: "id", name: "id.wav" }],
      uploads: [],
    };
    const requests = [];
    const { webSocket } = await setupApp({
      audio: {
        _ctx: audioContext, _lastBrowserPlayback: true, _master: master,
        decks: [{ audio: {} }], duckMusic, ensureAudioGraph: vi.fn(() => audioContext),
        playbackEnabled: true,
      },
      initialState: { browser_playback: true },
      onRequest: (url, options = {}) => {
        requests.push(`${options.method || "GET"} ${url}`);
        if (url === "/api/carts") return jsonResponse(wall);
        const play = url.match(/slots\/(\d+)\/play$/);
        if (play) {
          const mode = slots[play[1] - 1].mode;
          return jsonResponse({ label: "x", mode, duck_db: -12, duration: 7, url: "/api/liners/file/id" });
        }
        if (url === "/api/liners/file/id") {
          return new globalThis.Response(new Uint8Array([1, 2]), { headers: { "Content-Type": "audio/wav" } });
        }
        if (url === "/api/carts/stop") return jsonResponse({ stopped: true });
        throw new Error(`Unexpected request: ${url}`);
      },
    });
    const cart = (number) => document.querySelector(`#cart-slots [data-slot="${number}"]`);
    await vi.waitFor(() => expect(cart(1).textContent).toBe("Cart 1, Cart1, 0:07"));

    cart(1).click();
    await vi.waitFor(() => expect(sources).toHaveLength(1));
    await vi.waitFor(() => expect(sources[0].start).toHaveBeenCalled());
    expect(sources[0].connect).toHaveBeenCalledWith(master);
    expect(duckMusic).toHaveBeenLastCalledWith(0, 7);  // Replace silences the music
    cart(2).click();
    await vi.waitFor(() => expect(sources).toHaveLength(2));
    await vi.waitFor(() => expect(sources[1].start).toHaveBeenCalled());
    // The new cart cut off the last one, and Over leaves the music alone.
    expect(sources[0].stop).toHaveBeenCalled();
    expect(duckMusic).toHaveBeenLastCalledWith(1, 0);
    cart(3).click();
    await vi.waitFor(() => expect(sources).toHaveLength(3));
    await vi.waitFor(() => expect(duckMusic).toHaveBeenLastCalledWith(10 ** (-12 / 20), 7));

    // Stop carts stops this page's clip and brings the music back up.
    duckMusic.mockClear();
    document.querySelector("#cart-stop").click();
    expect(sources[2].stop).toHaveBeenCalled();
    expect(duckMusic).toHaveBeenCalledExactlyOnceWith(1, 0);
    await vi.waitFor(() => expect(requests).toContain("POST /api/carts/stop"));

    webSocket.onmessage({ data: JSON.stringify({
      type: "cart", event: "start", page: 1, slot: 3, label: "Cart 3, Cart3",
    }) });
    await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent)
      .toBe("Cart 3, Cart3, playing."));

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: true, current_track: null, queue: [], eq: {}, volume: 1, is_muted: true,
    }) });
    const before = requests.length;
    cart(1).click();
    await vi.waitFor(() => expect(document.querySelector("#cart-status").textContent)
      .toBe("Muted, so the cart was not played."));
    expect(requests.slice(before)).toEqual([]);
  });

  it("fires no scheduled liner while playback is stopped, as after a lost link", async () => {
    for (const playbackEnabled of [false, true]) {
      let linerDeps;
      await setupApp({
        audio: {
          _ctx: {}, _lastBrowserPlayback: true, decks: [{ audio: {} }], playbackEnabled,
        },
        initialState: { browser_playback: true },
        onInstallLiners: (_elements, deps) => { linerDeps = deps; },
      });
      expect(linerDeps.canPlay()).toBe(playbackEnabled);
      vi.unstubAllGlobals();
      for (const modulePath of moduleMocks) vi.doUnmock(modulePath);
    }
  });

  it("takes no louder volume from the server after a reconnect until set here", async () => {
    const quiet = 10 ** (-57 / 20);   // 5 %
    const quieter = quiet / 2;
    const setVolume = vi.fn();
    const { fetchImpl, webSocket, WebSocketImpl } = await setupApp({
      audio: { setVolume },
      initialState: { volume: quiet },
      onRequest: (url, options) => url === "/api/volume"
        ? jsonResponse(JSON.parse(options.body))
        : jsonResponse({ ok: true }),
    });
    const slider = document.querySelector("#vol");
    const push = (volume) => webSocket.onmessage({ data: JSON.stringify({
      current_track: null, queue: [], eq: {}, volume,
    }) });
    vi.useFakeTimers();

    // A restarted server comes back at full volume.
    webSocket.onclose({ code: 1006, wasClean: false });
    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledTimes(2));
    webSocket.onopen();
    push(1);
    expect(slider.value).toBe("5");
    expect(setVolume).toHaveBeenLastCalledWith(quiet);
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/volume")).toBe(false);

    // A lower volume set elsewhere is still followed.
    push(quieter);
    expect(setVolume).toHaveBeenLastCalledWith(quieter);
    push(1);
    expect(setVolume).toHaveBeenLastCalledWith(quiet);

    // A volume change on this page lifts the limit.
    slider.value = "50";
    slider.dispatchEvent(new Event("input"));
    await vi.advanceTimersByTimeAsync(700);
    push(10 ** (-30 / 20));   // the server's echo of the change
    push(1);
    expect(slider.value).toBe("100");
    expect(setVolume).toHaveBeenLastCalledWith(1);
  });

  it("keeps pointer previews local and sends only the final absolute seek", async () => {
    let resolveSeek;
    const deckAudio = { currentTime: 10, duration: 100 };
    const { fetchImpl } = await setupApp({
      audio: {
        _lastBrowserPlayback: true,
        decks: [{ audio: deckAudio }],
        playbackEnabled: true,
      },
      initialState: { duration: 100 },
      onRequest: (url) => url === "/api/seek"
        ? new Promise((resolve) => { resolveSeek = resolve; })
        : jsonResponse({ ok: true }),
    });
    const seek = document.querySelector("#progress-track");
    seek.getBoundingClientRect = () => ({ bottom: 20, left: 0, right: 100, top: 0, width: 100 });
    const dispatchPointer = (type, clientX) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        clientY: { value: 10 },
        isPrimary: { value: true },
        pointerId: { value: 55 },
      });
      seek.dispatchEvent(event);
    };

    dispatchPointer("pointerdown", 20);
    dispatchPointer("pointermove", 80);
    expect(document.querySelector("#progress-fill").style.width).toBe("20.0%");
    expect(fetchImpl.mock.calls.filter(([url]) => url === "/api/seek")).toHaveLength(0);
    expect(deckAudio.currentTime).toBe(10);

    dispatchPointer("pointerup", 90);
    await vi.waitFor(() => expect(fetchImpl.mock.calls.filter(
      ([url]) => url === "/api/seek",
    )).toHaveLength(1));
    const [, options] = fetchImpl.mock.calls.find(([url]) => url === "/api/seek");
    expect(JSON.parse(options.body)).toEqual({ seconds: 90 });
    expect(document.querySelector("#progress-fill").style.width).toBe("90.0%");
    expect(deckAudio.currentTime).toBe(90);

    resolveSeek(jsonResponse({ elapsed: 90 }));
  });

  it("keeps browser audio at its start position on pointer lifecycle cancellation", async () => {
    const deckAudio = { currentTime: 10, duration: 100 };
    const { fetchImpl } = await setupApp({
      audio: {
        _lastBrowserPlayback: true,
        decks: [{ audio: deckAudio }],
        playbackEnabled: true,
      },
      initialState: { duration: 100 },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const seek = document.querySelector("#progress-track");
    seek.getBoundingClientRect = () => ({
      bottom: 20, left: 0, right: 100, top: 0, width: 100,
    });
    const pointer = (type, clientX = 80) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        clientY: { value: 10 },
        isPrimary: { value: true },
        pointerId: { value: 57 },
      });
      seek.dispatchEvent(event);
    };
    const cancellations = [
      () => pointer("pointercancel"),
      () => pointer("lostpointercapture"),
      () => window.dispatchEvent(new Event("blur")),
      () => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true, value: "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
        Object.defineProperty(document, "visibilityState", {
          configurable: true, value: "visible",
        });
      },
      () => pointer("pointerup", 101),
    ];

    for (const cancel of cancellations) {
      deckAudio.currentTime = 10;
      pointer("pointerdown");
      expect(deckAudio.currentTime).toBe(10);
      cancel();
      expect(deckAudio.currentTime).toBe(10);
    }
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/seek")).toBe(false);
  });

  it("loads history on a direct #history visit and pages without losing focus", async () => {
    // Let the initial hashchange settle before installing this app instance.
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    const pageOf = (url) => Number(new URL(url, "http://x").searchParams.get("page"));
    try {
      const { fetchImpl } = await setupApp({
        onRequest: (url) => url.startsWith("/api/history")
          ? jsonResponse({
            total: 60, page: pageOf(url), pages: 2,
            items: pageOf(url) === 1 ? [
              { played_at: new Date(2026, 0, 2, 0, 1).toISOString(), title: "Newest", artist: "A", duration: 60 },
              { played_at: new Date(2026, 0, 1, 23, 0).toISOString(), title: "Previous day", artist: "B", duration: 60 },
            ] : [
              { played_at: new Date(2025, 11, 31, 23, 0).toISOString(), title: "Older", artist: "C", duration: 60 },
            ],
          })
          : jsonResponse({ ok: true }),
      });
      const historyCalls = () => fetchImpl.mock.calls
        .filter(([url]) => url.startsWith("/api/history"));
      await vi.waitFor(() => expect(historyCalls()).toHaveLength(1));
      document.dispatchEvent(new Event("DOMContentLoaded"));
      const prev = document.querySelector("#hist-prev");
      const next = document.querySelector("#hist-next");
      const tbody = document.querySelector("#history-tbody");
      const trackRows = () => [...tbody.querySelectorAll("tr[data-played-at]")];
      const status = document.querySelector("#sr-status");
      await vi.waitFor(() => expect(prev.getAttribute("aria-disabled")).toBe("true"));
      expect(trackRows().map((row) => row.cells[1].firstChild.textContent)).toEqual(["Newest", "Previous day"]);
      expect([...tbody.querySelectorAll(".history-date")]).toHaveLength(2);
      for (const row of trackRows()) {
        expect(row.cells[0].textContent).not.toMatch(/202[0-9]/);
      }
      // The automatic first load is not announced; only user paging is.
      expect(status.textContent).toBe("");

      next.focus();
      next.click();
      await vi.waitFor(() => expect(status.textContent).toBe("Page 2 of 2"));
      expect(trackRows().map((row) => row.cells[1].firstChild.textContent)).toEqual(["Older"]);
      expect(tbody.querySelectorAll(".history-date")).toHaveLength(1);
      expect(next.getAttribute("aria-disabled")).toBe("true");
      expect(next.disabled).toBe(false);
      expect(document.activeElement).toBe(next);

      // Next is unavailable on the last page, so nothing asks for page 3.
      next.click();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(historyCalls().some(([url]) => pageOf(url) === 3)).toBe(false);
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("opens Track info from History and returns focus to its row button", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      const { fetchImpl } = await setupApp({
        onRequest: (url) => url.startsWith("/api/history")
          ? jsonResponse({ total: 2, page: 1, pages: 1, items: [{
            event_id: "play-1", path: "M:/song.flac", played_at: "2026-01-01T00:00:00Z",
            title: "Song", artist: "Artist", duration: 60,
          }, {
            event_id: "play-2", path: "M:/other.flac", played_at: "2025-12-31T23:00:00Z",
            title: "Other", artist: "", duration: 120,
          }] })
          : url.startsWith("/api/library/points?")
            ? jsonResponse({ path: "M:/song.flac", title: "Song", artist: "Artist", kind: "music", points: {} })
            : jsonResponse({ ok: true }),
      });
      document.dispatchEvent(new Event("DOMContentLoaded"));
      const button = await vi.waitFor(() => {
        const found = document.querySelector("#history-tbody .history-info");
        expect(found).not.toBeNull();
        return found;
      });
      expect(button.getAttribute("aria-label")).toBe("Track info for Artist, Song");
      expect(document.querySelectorAll("#history-tbody .history-info")[1].getAttribute("aria-label"))
        .toBe("Track info for Other");
      expect(button.textContent).toBe("Track info");
      const tbody = document.querySelector("#history-tbody");
      const cells = [...tbody.querySelectorAll("tr[data-played-at] td")];
      expect(cells.map((cell) => cell.getAttribute("role"))).toEqual([
        "gridcell", "gridcell", "gridcell", "gridcell", "gridcell",
        "gridcell", "gridcell", "gridcell", "gridcell", "gridcell",
      ]);
      expect(cells.filter((cell) => cell.tabIndex === 0)).toEqual([cells[0]]);
      expect(button.tabIndex).toBe(-1);
      cells[0].focus();
      cells[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
      expect(document.activeElement).toBe(cells[5]);
      cells[5].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
      expect(document.activeElement).toBe(cells[0]);
      cells[0].dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
      expect(document.activeElement).toBe(cells[4]);
      button.click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      expect(document.querySelector("#track-info-heading").textContent).toBe("Track info: Artist — Song");
      const back = document.querySelector("#track-info-return");
      expect(back.hidden).toBe(false);
      expect(back.textContent).toBe("Return to History");
      const historyCalls = () => fetchImpl.mock.calls.filter(([url]) => url.startsWith("/api/history"));
      const beforeReturn = historyCalls().length;
      const focusEvents = [];
      document.addEventListener("focusin", (event) => focusEvents.push(event.target));
      back.click();
      await vi.waitFor(() => expect(document.querySelector("#panel-history").hidden).toBe(false));
      await vi.waitFor(() => expect(document.activeElement).toBe(cells[4]));
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(historyCalls()).toHaveLength(beforeReturn);
      expect(document.querySelector("#history-tbody .history-info")).toBe(button);
      expect(focusEvents.filter((node) => node.classList?.contains("history-info"))).toEqual([button]);
      expect(cells[4].tabIndex).toBe(0);
      cells[4].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
      expect(document.activeElement).toBe(cells[3]);
      expect(document.querySelector("#sr-status").textContent).toBe("");
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("lists a live DJ session in History without Track info", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      await setupApp({
        onRequest: (url) => url.startsWith("/api/history")
          ? jsonResponse({ total: 3, page: 1, pages: 1, items: [{
            event_id: "live-1", kind: "live", path: null, played_at: "2026-01-01T01:00:00Z",
            title: "Live", artist: "DJ Alice", duration: 0, ended_at: null,
          }, {
            event_id: "live-0", kind: "live", path: null, played_at: "2026-01-01T00:30:00Z",
            title: "Live", artist: "DJ Bob", duration: 754, ended_at: "2026-01-01T00:42:34Z",
          }, {
            event_id: "play-1", kind: "song", path: "M:/song.flac", played_at: "2026-01-01T00:00:00Z",
            title: "Song", artist: "Artist", duration: 60,
          }] })
          : jsonResponse({ ok: true }),
      });
      const rows = await vi.waitFor(() => {
        // The rows themselves; a date heading row comes before them.
        const found = [...document.querySelectorAll("#history-tbody tr[data-played-at]")];
        expect(found).toHaveLength(3);
        return found;
      });
      const text = (row) => [...row.cells].map((cell) => cell.textContent.trim());
      expect(text(rows[0]).slice(1)).toEqual(["Live", "DJ Alice", "On the air", ""]);
      expect(text(rows[1]).slice(1)).toEqual(["Live", "DJ Bob", "12:34", ""]);
      expect(rows[0].querySelector("button")).toBeNull();
      expect(rows[1].querySelector("button")).toBeNull();
      expect(rows[2].querySelector(".history-info").textContent).toBe("Track info");
      // The title cell is still the row header, so the row reads the same.
      expect(rows[0].cells[1].getAttribute("role")).toBe("gridcell");
      expect(rows[0].cells[1].hasAttribute("data-name")).toBe(true);
      expect(rows.map((row) => row.dataset.historyId)).toEqual(["live-1", "live-0", "play-1"]);
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("shows Settings > Live DJ and Talk when the station can go live", async () => {
    await setupApp({
      onRequest: (url) => url === "/api/live"
        ? jsonResponse({
          available: true, reason: null, live: { dj: "DJ Alice", since: "2026-01-01T00:00:00Z" },
          talkover: false, source_path: "/live", plain_http_lan: false,
          settings: { mic_return_seconds: 0.5, mic_duck_db: 12 }, djs: [],
        })
        : jsonResponse({ ok: true }),
    });
    await vi.waitFor(() => expect(document.querySelector("#stab-live").hidden).toBe(false));
    expect(document.querySelector("#talk-desk").hidden).toBe(false);
    expect(document.querySelector("#live-on-air").textContent).toMatch(/^Live: DJ Alice since /);
    expect(document.querySelector("#live-dj-none").hidden).toBe(false);
  });

  it("keeps the History page on Return but opens page 1 on a normal visit", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      const { fetchImpl } = await setupApp({ onRequest: (url) => url.startsWith("/api/history")
        ? jsonResponse({ total: 51, page: Number(new URL(url, "http://x").searchParams.get("page")), pages: 2,
          items: [{ event_id: `play-${new URL(url, "http://x").searchParams.get("page")}`,
            path: "M:/song.flac", played_at: "2026-01-01T00:00:00Z", title: "Song", artist: "A", duration: 60 }] })
        : url.startsWith("/api/library/points?")
          ? jsonResponse({ path: "M:/song.flac", title: "Song", kind: "music", points: {} })
          : jsonResponse({ ok: true }) });
      await vi.waitFor(() => expect(document.querySelector("#history-tbody .history-info")).not.toBeNull());
      document.querySelector("#hist-next").click();
      await vi.waitFor(() => expect(document.querySelector("#hist-page-info").textContent).toBe("Page 2 of 2"));
      document.querySelector("#history-tbody .history-info").click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      const historyCalls = () => fetchImpl.mock.calls.filter(([url]) => url.startsWith("/api/history"));
      const beforeReturn = historyCalls().length;
      document.querySelector("#track-info-return").click();
      await vi.waitFor(() => expect(document.querySelector("#panel-history").hidden).toBe(false));
      expect(document.querySelector("#hist-page-info").textContent).toBe("Page 2 of 2");
      expect(historyCalls()).toHaveLength(beforeReturn);
      document.querySelector("#tab-now").click();
      await vi.waitFor(() => expect(document.querySelector("#panel-now").hidden).toBe(false));
      document.querySelector("#tab-history").click();
      await vi.waitFor(() => expect(historyCalls()).toHaveLength(beforeReturn + 1));
      expect(historyCalls().at(-1)[0]).toContain("page=1");
      await vi.waitFor(() => expect(document.querySelector("#hist-page-info").textContent).toBe("Page 1 of 2"));
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("explains why a history entry without a recording path has no Track info", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      await setupApp({
        onRequest: (url) => url.startsWith("/api/history")
          ? jsonResponse({ total: 1, page: 1, pages: 1, items: [{
            played_at: "2026-01-01T00:00:00Z", title: "Old song", artist: "Artist", duration: 60,
          }] })
          : jsonResponse({ ok: true }),
      });
      document.dispatchEvent(new Event("DOMContentLoaded"));
      const button = await vi.waitFor(() => {
        const found = document.querySelector("#history-tbody .history-info");
        expect(found).not.toBeNull();
        return found;
      });
      button.click();
      await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent).toContain("no saved recording path"));
      expect(document.querySelector("#panel-history").hidden).toBe(false);
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("keeps History visible and the invoking button focused when points return 404", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    try {
      await setupApp({ onRequest: (url) => url.startsWith("/api/history")
        ? jsonResponse({ total: 1, page: 1, pages: 1, items: [{
          event_id: "gone", path: "M:/gone.flac", played_at: "2026-01-01T00:00:00Z",
          title: "Gone", artist: "A", duration: 60,
        }] })
        : url.startsWith("/api/library/points?")
          ? jsonResponse({ detail: "Not found" }, 404) : jsonResponse({ ok: true }) });
      const button = await vi.waitFor(() => {
        const found = document.querySelector("#history-tbody .history-info");
        expect(found).not.toBeNull();
        return found;
      });
      button.focus();
      button.click();
      await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent)
        .toContain("no longer in the library"));
      expect(document.querySelector("#panel-history").hidden).toBe(false);
      expect(document.querySelector("#panel-track-info").hidden).toBe(true);
      expect(document.activeElement).toBe(button.closest("td"));
      expect(document.querySelector("#points-status").textContent).toBe("");
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("focuses the queue list when its source item disappears before Return completes", async () => {
    window.location.hash = "#queue";
    try {
      await setupApp({ onRequest: (url) => url.startsWith("/api/library/points?")
        ? jsonResponse({ path: "M:/song.flac", title: "Song", kind: "music", points: {} })
        : jsonResponse({ ok: true }) });
      const { renderQueue } = await import("../../src/autodj/static/modules/queue.js");
      const list = document.querySelector("#queue-list");
      const els = { queueList: list, queueCount: document.querySelector("#queue-count") };
      renderQueue([{ path: "M:/song.flac", title: "Song" }], els);
      const opener = list.querySelector('[data-action="info"]');
      opener.click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      document.querySelector("#track-info-return").click();
      renderQueue([], els);
      window.dispatchEvent(new window.HashChangeEvent("hashchange", {
        newURL: new URL("#track-info", location.href).href,
      }));
      await vi.waitFor(() => expect(document.querySelector("#panel-queue").hidden).toBe(false));
      await vi.waitFor(() => expect(document.activeElement).toBe(list));
      expect(document.querySelector("#sr-status").textContent).toContain("no longer in the queue");
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("returns to Now Playing and the Settings liner button", async () => {
    window.location.hash = "#now";
    try {
      await setupApp({
        initialState: { current_track: { path: "M:/song.flac", title: "Song" } },
        onRequest: (url) => url.includes("/library/points?")
          ? jsonResponse({ path: decodeURIComponent(url.split("path=")[1]), title: "Song", kind: "music", points: {} })
          : jsonResponse({ ok: true }),
      });
      const nowButton = document.querySelector("#edit-current-points");
      nowButton.click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      expect(document.querySelector("#track-info-return").textContent).toBe("Return to Now Playing");
      document.querySelector("#track-info-return").click();
      await vi.waitFor(() => expect(document.activeElement).toBe(nowButton));

      // Liner file maintenance is in Settings > Voice liners.
      document.querySelector("#stab-liners").click();
      await vi.waitFor(() => expect(document.querySelector("#spanel-liners").hidden).toBe(false));
      const { renderLinerFileList } = await vi.importActual("../../src/autodj/static/modules/liners.js");
      const list = document.querySelector("#ln-file-list");
      renderLinerFileList(list, [{ id: "liner-1", name: "liner.wav", path: "N:/liner.wav" }], vi.fn());
      const liner = list.querySelector('[aria-label="Track info for liner.wav"]');
      liner.focus();
      liner.click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      expect(document.querySelector("#track-info-return").textContent).toBe("Return to Settings");
      document.querySelector("#track-info-return").click();
      await vi.waitFor(() => expect(document.activeElement).toBe(liner));
      expect(document.querySelector("#spanel-liners").hidden).toBe(false);
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  // Search and folder browsing use the same library result table.
  const browseTracks = (url) => {
    const params = new URL(url, "http://x").searchParams;
    const offset = Number(params.get("offset"));
    return jsonResponse({
      total: 150, offset,
      results: [{ path: "M:/song.flac", title: "Song", artist: "Artist", kind: params.get("kind") }],
    });
  };
  const resultInfo = () => document.querySelector('#search-results [data-points="true"]');

  it("returns from Track info to its shared library row in Queue & Search", async () => {
    window.location.hash = "#queue";
    try {
      await setupApp({ onRequest: (url) => url.startsWith("/api/library/tracks?")
        ? browseTracks(url)
        : url.startsWith("/api/library/points?")
          ? jsonResponse({ path: "M:/song.flac", title: "Song", kind: "music", points: {} })
          : jsonResponse({ folders: [], ok: true }) });
      await vi.waitFor(() => expect(document.querySelector("#panel-queue").hidden).toBe(false));
      document.querySelector("#search-input").value = "song";
      document.querySelector("#btn-search").click();
      await vi.waitFor(() => expect(resultInfo()).not.toBeNull());
      const info = resultInfo();
      info.focus();
      info.click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      const back = document.querySelector("#track-info-return");
      expect(back.textContent).toBe("Return to Queue & Search");
      back.click();
      await vi.waitFor(() => expect(document.activeElement).toBe(info.closest("td")));
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("returns to the redrawn shared result row", async () => {
    window.location.hash = "#queue";
    try {
      await setupApp({ onRequest: (url) => url.startsWith("/api/library/tracks?")
        ? browseTracks(url)
        : url.startsWith("/api/library/points?")
          ? jsonResponse({ path: "M:/song.flac", title: "Song", kind: "music", points: {} })
          : jsonResponse({ folders: [], ok: true }) });
      await vi.waitFor(() => expect(document.querySelector("#panel-queue").hidden).toBe(false));
      document.querySelector("#search-input").value = "song";
      document.querySelector("#btn-search").click();
      await vi.waitFor(() => expect(resultInfo()).not.toBeNull());
      const original = resultInfo();
      original.click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      document.querySelector("#btn-search").click();
      await vi.waitFor(() => expect(resultInfo()).not.toBe(original));
      const replacement = resultInfo();
      document.querySelector("#track-info-return").click();
      await vi.waitFor(() => expect(document.activeElement).toBe(replacement.closest("td")));
      expect(document.querySelector("#sr-status").textContent).toBe("");
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("pages the shared library results without losing focus", async () => {
    window.location.hash = "#queue";
    try {
      await setupApp({ onRequest: (url) => url.startsWith("/api/library/tracks?")
        ? browseTracks(url)
        : jsonResponse({ folders: [], ok: true }) });
      await vi.waitFor(() => expect(document.querySelector("#panel-queue").hidden).toBe(false));
      const count = document.querySelector("#search-count");
      document.querySelector("#btn-search").click();
      await vi.waitFor(() => expect(count.textContent).toBe("150 tracks found; showing 1–1."));
      const next = document.querySelector("#library-browse-next");
      const previous = document.querySelector("#library-browse-previous");
      expect(next.disabled).toBe(false);
      next.focus();
      next.click();
      await vi.waitFor(() => expect(count.textContent).toBe("150 tracks found; showing 101–101."));
      expect(next.disabled).toBe(true);
      expect(document.activeElement).toBe(previous);
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("loads type-specific folder choices when Queue & Search opens", async () => {
    window.location.hash = "#now";
    try {
      const { fetchImpl } = await setupApp({ onRequest: (url) => url === "/api/library/folders"
        ? jsonResponse({ folders: [
          { id: 1, path: "M:/rock", kind: "music", recursive: true, advanced: true },
          { id: 2, path: "N:/ids", kind: "liner", recursive: true, advanced: false },
        ] })
        : jsonResponse({ ok: true }) });
      const folderCalls = () => fetchImpl.mock.calls.filter(([url]) => url === "/api/library/folders");
      expect(folderCalls()).toHaveLength(0);
      document.querySelector("#tab-queue").click();
      await vi.waitFor(() => expect(document.querySelector("#panel-queue").hidden).toBe(false));
      const kind = document.querySelector("#library-kind");
      const select = document.querySelector("#library-folder");
      await vi.waitFor(() => expect([...select.options].map((option) => option.value)).toEqual(["all", "folder:1"]));
      expect(folderCalls()).toHaveLength(1);
      kind.value = "liner";
      kind.dispatchEvent(new Event("change", { bubbles: true }));
      expect([...select.options].map((option) => option.value)).toEqual(["all", "folder:2"]);
      expect(select.value).toBe("all");
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("returns to a queue Track info button after the queue redraws", async () => {
    window.location.hash = "#queue";
    try {
      await setupApp({
        onRequest: (url) => url.startsWith("/api/library/points?")
          ? jsonResponse({ path: "M:/song.flac", title: "Song", kind: "music", points: {} })
          : jsonResponse({ ok: true }),
      });
      const { renderQueue } = await import("../../src/autodj/static/modules/queue.js");
      const queueList = document.querySelector("#queue-list");
      const els = { queueList, queueCount: document.querySelector("#queue-count") };
      renderQueue([{ path: "M:/song.flac", title: "Song" }], els);
      const original = queueList.querySelector('[data-action="info"]');
      original.focus();
      original.click();
      await vi.waitFor(() => expect(document.querySelector("#panel-track-info").hidden).toBe(false));
      // The row went and came back: a new button for the same song.
      renderQueue([], els);
      renderQueue([{ path: "M:/song.flac", title: "Song" }], els);
      const replacement = queueList.querySelector('[data-action="info"]');
      expect(replacement).not.toBe(original);
      document.querySelector("#track-info-return").click();
      await vi.waitFor(() => expect(document.activeElement).toBe(replacement));
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("adds a new track to the top of history page 1 without rebuilding the rows", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    const first = { played_at: new Date(2026, 0, 1, 23, 58).toISOString(), title: "First", artist: "A", duration: 60 };
    const second = { played_at: new Date(2026, 0, 2, 0, 1).toISOString(), title: "Second", artist: "B", duration: 60 };
    let items = [first];
    try {
      const { webSocket } = await setupApp({
        initialState: { current_track: { path: "first.flac", title: "First" } },
        onRequest: (url) => url.startsWith("/api/history")
          ? jsonResponse({ total: items.length, page: 1, pages: 1, items })
          : jsonResponse({ ok: true }),
      });
      const tbody = document.querySelector("#history-tbody");
      const trackRows = () => [...tbody.querySelectorAll("tr[data-played-at]")];
      await vi.waitFor(() => expect(trackRows()).toHaveLength(1));
      const firstRow = trackRows()[0];
      firstRow.cells[2].focus();
      const status = document.querySelector("#sr-status");
      const pushTrack = (path, title) => webSocket.onmessage({ data: JSON.stringify({
        browser_playback: false, current_track: { path, title }, discovery_available: false,
        duration: 60, elapsed: 0, eq: {}, is_muted: false, is_paused: false,
        next_track: null, queue: [], settings: null, volume: 1,
      }) });
      pushTrack("first.flac", "First");

      items = [second, first];
      pushTrack("second.flac", "Second");

      await vi.waitFor(() => expect(trackRows()).toHaveLength(2));
      expect(trackRows()[0].textContent).toContain("Second");
      expect(trackRows()[1]).toBe(firstRow);
      expect(document.activeElement).toBe(firstRow.cells[2]);
      expect(tbody.querySelectorAll(".history-date")).toHaveLength(2);
      const sameDay = { ...second, played_at: new Date(2026, 0, 2, 0, 4).toISOString(), title: "Third" };
      items = [sameDay, second, first];
      pushTrack("third.flac", "Third");
      await vi.waitFor(() => expect(trackRows()).toHaveLength(3));
      expect(tbody.querySelectorAll(".history-date")).toHaveLength(2);
      expect(status.textContent).toBe("");
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("moves focus to the new last History row when a live update trims the focused play", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    const old = Array.from({ length: 50 }, (_, index) => ({
      event_id: `old-${index}`, path: `M:/old-${index}.flac`,
      played_at: new Date(Date.UTC(2026, 0, 1, 0, 50 - index)).toISOString(),
      title: `Old ${index}`, artist: "A", duration: 60,
    }));
    const fresh = { event_id: "new", path: "M:/new.flac", played_at: "2026-01-01T01:00:00Z",
      title: "New", artist: "B", duration: 60 };
    let items = old;
    try {
      const { webSocket } = await setupApp({
        initialState: { current_track: { path: "M:/old.flac" } },
        onRequest: (url) => url.startsWith("/api/history")
          ? jsonResponse({ total: items.length, page: 1, pages: 1, items }) : jsonResponse({ ok: true }),
      });
      const tbody = document.querySelector("#history-tbody");
      const trackRows = () => [...tbody.querySelectorAll("tr[data-played-at]")];
      await vi.waitFor(() => expect(trackRows()).toHaveLength(50));
      const focused = trackRows()[49].cells[2];
      focused.focus();
      items = [fresh, ...old.slice(0, 49)];
      webSocket.onmessage({ data: JSON.stringify({ current_track: { path: "M:/new.flac" },
        browser_playback: false, discovery_available: false, duration: 60, elapsed: 0, eq: {},
        is_muted: false, is_paused: false, next_track: null, queue: [], settings: null, volume: 1 }) });
      await vi.waitFor(() => expect(tbody.querySelector("[data-history-id='new']")).not.toBeNull());
      expect(focused.isConnected).toBe(false);
      expect(document.activeElement).toBe(trackRows()[49].cells[2]);
      await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent).toContain("no longer in the displayed History rows"));
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("keeps History after a failed live check and reports a failed manual refresh", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    const yesterday = new Date(Date.now() - 2 * 86400000).toISOString();
    const today = new Date().toISOString();
    let historyCall = 0;
    try {
      const { webSocket } = await setupApp({
        initialState: { current_track: { path: "M:/old.flac" } },
        onRequest: (url) => {
          if (!url.startsWith("/api/history")) return jsonResponse({ ok: true });
          historyCall += 1;
          if (historyCall >= 2) return jsonResponse({ detail: "History offline" }, 503);
          const item = historyCall === 1
            ? { event_id: "old", played_at: yesterday, path: "M:/old.flac", title: "Old" }
            : { event_id: "new", played_at: today, path: "M:/new.flac", title: "New" };
          return jsonResponse({ total: 1, page: 1, pages: 1, items: [item] });
        },
      });
      const button = await vi.waitFor(() => {
        const found = document.querySelector("#history-tbody .history-info");
        expect(found).not.toBeNull();
        return found;
      });
      button.focus();
      webSocket.onmessage({ data: JSON.stringify({ current_track: { path: "M:/new.flac" },
        browser_playback: false, discovery_available: false, duration: 60, elapsed: 0, eq: {},
        is_muted: false, is_paused: false, next_track: null, queue: [], settings: null, volume: 1 }) });
      await vi.waitFor(() => expect(historyCall).toBe(2));
      expect(document.querySelector("#sr-status").textContent).toBe("");
      expect(document.activeElement).toBe(button.closest("td"));
      expect(button.isConnected).toBe(true);
      expect(document.querySelector("#history-table").hidden).toBe(false);
      document.querySelector("#hist-refresh").click();
      await vi.waitFor(() => expect(historyCall).toBe(3));
      await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent)
        .toContain("Could not load history: History offline"));
      expect(button.isConnected).toBe(false);
      expect(document.querySelector("#history-table").hidden).toBe(true);
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("focuses the nearest History row and explains when the focused play leaves a refreshed page", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    let items = ["one", "two", "three"].map((name) => ({
      event_id: name, path: `M:/${name}.flac`, played_at: "2026-01-01T00:00:00Z",
      title: name, artist: "A", duration: 60,
    }));
    try {
      await setupApp({ onRequest: (url) => url.startsWith("/api/history")
        ? jsonResponse({ total: items.length, page: 1, pages: 1, items }) : jsonResponse({ ok: true }) });
      const tbody = document.querySelector("#history-tbody");
      const trackRows = () => [...tbody.querySelectorAll("tr[data-played-at]")];
      await vi.waitFor(() => expect(trackRows()).toHaveLength(3));
      const focusedCell = trackRows()[1].cells[2];
      focusedCell.focus();
      document.querySelector("#hist-refresh").click();
      await vi.waitFor(() => expect(trackRows()).toHaveLength(3));
      expect(document.activeElement).toBe(trackRows()[1].cells[2]);
      items = [items[0], items[2]];
      document.querySelector("#hist-refresh").click();
      await vi.waitFor(() => expect(tbody.querySelectorAll(".history-info")).toHaveLength(2));
      expect(document.activeElement).toBe(trackRows()[1].cells[2]);
      expect(document.activeElement.closest("tr").querySelector(".history-info").dataset.historyId).toBe("three");
      expect(document.querySelector("#sr-status").textContent).toContain("no longer on this History page");
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("steps back from the last page after Go to a page past it", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    const asked = (url) => Number(new URL(url, "http://x").searchParams.get("page"));
    try {
      const { fetchImpl } = await setupApp({
        onRequest: (url) => {
          if (!url.startsWith("/api/history")) return jsonResponse({ ok: true });
          // The server keeps a page past the last to the last.
          const page = Math.max(1, Math.min(asked(url), 3));
          return jsonResponse({ total: 120, page, pages: 3, items: [
            { event_id: `p${page}`, played_at: "2026-01-01T00:00:00Z", path: `M:/${page}.flac`,
              title: `Song ${page}`, artist: "A", duration: 60 },
          ] });
        },
      });
      const info = document.querySelector("#hist-page-info");
      await vi.waitFor(() => expect(info.textContent).toBe("Page 1 of 3"));
      document.querySelector("#hist-goto").value = "9";
      document.querySelector("#hist-go").click();
      await vi.waitFor(() => expect(info.textContent).toBe("Page 3 of 3"));
      document.querySelector("#hist-prev").click();
      await vi.waitFor(() => expect(info.textContent).toBe("Page 2 of 3"));
      const pages = fetchImpl.mock.calls.map(([url]) => url)
        .filter((url) => url.startsWith("/api/history")).map(asked);
      expect(pages.slice(-2)).toEqual([9, 2]);
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("puts focus on Refresh history when a failed Next hides the pager", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    window.location.hash = "#history";
    await new Promise((resolve) => setTimeout(resolve, 0));
    const asked = (url) => Number(new URL(url, "http://x").searchParams.get("page"));
    try {
      await setupApp({
        onRequest: (url) => {
          if (!url.startsWith("/api/history")) return jsonResponse({ ok: true });
          if (asked(url) === 2) return jsonResponse({ detail: "History offline" }, 503);
          return jsonResponse({ total: 60, page: 1, pages: 2, items: [
            { event_id: "one", played_at: "2026-01-01T00:00:00Z", path: "M:/1.flac",
              title: "One", artist: "A", duration: 60 },
          ] });
        },
      });
      const next = document.querySelector("#hist-next");
      await vi.waitFor(() => expect(next.getAttribute("aria-disabled")).toBe("false"));
      next.focus();
      next.click();
      await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent)
        .toBe("Could not load history: History offline"));
      expect(document.querySelector("#history-pagination").hidden).toBe(true);
      expect(document.activeElement).toBe(document.querySelector("#hist-refresh"));
    } finally {
      window.location.hash = "";
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
  });

  it("clears stale history and exposes a current load failure", async () => {
    await setupApp({
      onRequest: (url) => url.startsWith("/api/history")
        ? jsonResponse({ detail: "History unavailable" }, 503)
        : jsonResponse({ ok: true }),
    });
    document.dispatchEvent(new Event("DOMContentLoaded"));
    const tbody = document.querySelector("#history-tbody");
    const table = document.querySelector("#history-table");
    const pagination = document.querySelector("#history-pagination");
    const empty = document.querySelector("#history-empty");
    tbody.innerHTML = "<tr><td>stale</td><td>track</td></tr>";
    table.removeAttribute("hidden");
    pagination.removeAttribute("hidden");
    document.querySelector("#hist-goto").value = "1";

    document.querySelector("#hist-go").click();
    await vi.waitFor(() => expect(empty.textContent).toContain("History unavailable"));
    expect(tbody.children).toHaveLength(0);
    expect(table.hasAttribute("hidden")).toBe(true);
    expect(pagination.hasAttribute("hidden")).toBe(true);
    expect(empty.hasAttribute("hidden")).toBe(false);
  });

  it("tears down buffered decks and liners before showing auth on REST 401", async () => {
    let linerDeps;
    const deckAudio = { currentTime: 10, duration: 100 };
    const source = {
      addEventListener: vi.fn(),
      buffer: null,
      connect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
    };
    const gainParam = {
      cancelScheduledValues: vi.fn(),
      linearRampToValueAtTime: vi.fn(),
      setValueAtTime: vi.fn(),
      value: 1,
    };
    const audioContext = {
      createBufferSource: vi.fn(() => source),
      createGain: vi.fn(() => ({ gain: { value: 1 }, connect: vi.fn() })),
      currentTime: 2,
      decodeAudioData: vi.fn().mockResolvedValue({ duration: 1 }),
      destination: {},
    };
    const stopAllDecks = vi.fn();
    const resetTrackCaches = vi.fn();
    const { dialog, fetchImpl, updateMediaSession, webSocket } = await setupApp({
      audio: {
        _ctx: audioContext,
        _lastBrowserPlayback: true,
        decks: [{
          audio: deckAudio,
          gain: { gain: gainParam },
        }],
        playbackEnabled: true,
        resetTrackCaches,
        stopAllDecks,
      },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
        duration: 100,
        next_track: { path: "next.mp3", title: "Next" },
        queue: [{ path: "queued.mp3", title: "Queued secret" }],
        settings: {
          available_presets: ["Secret preset"],
          bpm_range: {},
          discovery_every: null,
          djmix: {},
          playback: {},
          preset: "Secret preset",
          transition: "echo_out",
        },
      },
      onInstallLiners: (_elements, deps) => { linerDeps = deps; },
      onRequest: (url) => url === "/api/pause"
        ? jsonResponse({ detail: "Authentication required" }, 401)
        : jsonResponse({ ok: true }),
    });
    expect(await linerDeps.playLiner(new ArrayBuffer(1), -12)).toBe(true);
    expect(document.querySelector("#next-track-text").textContent).toContain("Next");
    expect(document.querySelector("#queue-list").textContent).toContain("Queued secret");
    expect(document.querySelector("#preset-select").textContent).toContain("Secret preset");

    const progress = document.querySelector("#progress-track");
    progress.getBoundingClientRect = () => ({ bottom: 20, left: 0, right: 100, top: 0, width: 100 });
    const dispatchPointer = (type, clientX) => {
      const event = new Event(type, { bubbles: true, cancelable: true });
      Object.defineProperties(event, {
        button: { value: 0 },
        clientX: { value: clientX },
        clientY: { value: 10 },
        isPrimary: { value: true },
        pointerId: { value: 61 },
      });
      progress.dispatchEvent(event);
    };
    dispatchPointer("pointerdown", 50);
    expect(document.querySelector("#progress-fill").style.width).toBe("50.0%");

    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    expect(stopAllDecks).toHaveBeenCalledOnce();
    expect(source.stop).toHaveBeenCalledOnce();
    expect(resetTrackCaches).toHaveBeenCalledOnce();
    expect(webSocket.close).toHaveBeenCalledOnce();
    expect(document.querySelector("#now-playing-title").textContent).not.toContain("Current");
    expect(document.querySelector("#now-playing-meta").textContent).toBe("");
    const cueSummary = document.querySelector("#cue-summary");
    expect(cueSummary).not.toBeNull();
    if (cueSummary) expect(cueSummary.textContent).toBe("No cue points");
    const cueDetails = document.querySelector("#cue-details");
    expect(cueDetails).not.toBeNull();
    if (cueDetails) expect(cueDetails.textContent).toBe("No cue points");
    expect(document.querySelector("#next-track-text").textContent).toBe("—None");
    expect(document.querySelector("#queue-list").textContent).not.toContain("Queued secret");
    expect(document.querySelector("#preset-select").textContent).not.toContain("Secret preset");
    expect(updateMediaSession).toHaveBeenLastCalledWith({ current_track: null });

    dispatchPointer("pointermove", 90);
    dispatchPointer("pointerup", 90);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.querySelector("#progress-fill").style.width).toBe("0%");
    expect(document.querySelector("#progress-track").getAttribute("aria-valuenow")).toBe("0");
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/seek")).toBe(false);
    expect(deckAudio.currentTime).toBe(10);
  });

  it("does not apply a shuffle response completed after confirmed expiry", async () => {
    let resolveShuffle;
    const { dialog, updateMediaSession } = await setupApp({
      initialState: {
        current_track: { path: "current.mp3", title: "Current" },
      },
      onRequest: (url) => {
        if (url === "/api/random-track") {
          return new Promise((resolve) => { resolveShuffle = resolve; });
        }
        if (url === "/api/pause") {
          return jsonResponse({ detail: "Authentication required" }, 401);
        }
        return jsonResponse({ ok: true });
      },
    });

    document.querySelector("#btn-shuffle").click();
    await vi.waitFor(() => expect(resolveShuffle).toEqual(expect.any(Function)));
    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    resolveShuffle(jsonResponse({
      browser_playback: false,
      current_track: { path: "late.mp3", title: "Late secret" },
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.querySelector("#now-playing-title").textContent)
      .not.toContain("Late secret");
    expect(updateMediaSession).toHaveBeenLastCalledWith({ current_track: null });
  });

  it("does not apply a delayed shuffle response after newer WebSocket state", async () => {
    let resolveShuffle;
    const {
      bumpLinerTrackCount,
      webSocket,
    } = await setupApp({
      initialState: {
        current_track: { path: "a.mp3", title: "Track A" },
      },
      onRequest: (url) => url === "/api/random-track"
        ? new Promise((resolve) => { resolveShuffle = resolve; })
        : jsonResponse({ ok: true }),
    });
    const stateC = {
      browser_playback: false,
      current_track: { path: "c.mp3", title: "Track C" },
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    };

    document.querySelector("#btn-shuffle").click();
    await vi.waitFor(() => expect(resolveShuffle).toEqual(expect.any(Function)));
    webSocket.onmessage({ data: JSON.stringify(stateC) });
    resolveShuffle(jsonResponse({
      ...stateC,
      current_track: { path: "late-b.mp3", title: "Late Track B" },
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.querySelector("#now-playing-title").textContent)
      .toContain("Track C");
    expect(document.querySelector("#now-playing-title").textContent)
      .not.toContain("Late Track B");
    webSocket.onmessage({ data: JSON.stringify(stateC) });
    expect(countedTracks(bumpLinerTrackCount)).toBe(1);
    expect(bumpLinerTrackCount.mock.calls.map(
      ([state]) => state.current_track?.path ?? null,
    )).toEqual(["a.mp3", "c.mp3", "c.mp3"]);
  });

  it("applies an ordinary shuffle response when no newer state arrived", async () => {
    const shuffledState = {
      browser_playback: false,
      current_track: { path: "b.mp3", title: "Track B" },
      discovery_available: false,
      duration: 0,
      elapsed: 0,
      eq: {},
      is_muted: false,
      is_paused: false,
      next_track: null,
      queue: [],
      settings: null,
      volume: 1,
    };
    const {
      bumpLinerTrackCount,
    } = await setupApp({
      initialState: {
        current_track: { path: "a.mp3", title: "Track A" },
      },
      onRequest: (url) => url === "/api/random-track"
        ? jsonResponse(shuffledState)
        : jsonResponse({ ok: true }),
    });

    document.querySelector("#btn-shuffle").click();
    await vi.waitFor(() => expect(document.querySelector(
      "#now-playing-title",
    ).textContent).toContain("Track B"));

    expect(countedTracks(bumpLinerTrackCount)).toBe(1);
    expect(bumpLinerTrackCount).toHaveBeenLastCalledWith(shuffledState);
  });
});

describe("stream mode", () => {
  const streamState = {
    browser_playback: false,
    current_track: { path: "a.mp3", artist: "Artist", title: "Song", bpm: 128 },
    duration: 200,
    elapsed: 50,
    stream_event: { id: "boot", seq: 1, name: "set_started" },
    stream_listeners: 1,
    stream_mode: true,
    stream_state: "playing",
  };
  const lyricsFor = (path) => jsonResponse({
    path,
    lyrics: [
      { time_s: 0, text: "One" },
      { time_s: 46, text: "Two" },
      { time_s: 49, text: "Three" },
    ],
  });
  const hotkeyOptions = async () => {
    const hotkeys = await import("../../src/autodj/static/modules/hotkeys.js");
    return hotkeys.installHotkeys.mock.calls[0][0];
  };

  it("never starts the deck engine in stream mode and hides seek", async () => {
    const applyBrowserPlaybackState = vi.fn();
    // browser_playback is false in stream mode; true here proves stream wins.
    const app = await setupApp({
      audio: { applyBrowserPlaybackState },
      initialState: {
        ...streamState, browser_playback: true, current_track: null, stream_state: "idle",
      },
    });
    expect(app.unlockAndPlay).not.toHaveBeenCalled();
    expect(applyBrowserPlaybackState).not.toHaveBeenCalled();
    expect(app.setLastBrowserPlayback).toHaveBeenLastCalledWith(false);
    expect(document.getElementById("progress-track").hidden).toBe(true);
    expect(document.getElementById("btn-listen").hidden).toBe(false);
    expect(document.getElementById("stream-idle-note").hidden).toBe(false);
    // The slider is gone, so the visible clock becomes the readable one.
    expect(document.getElementById("progress-bar-label").hasAttribute("aria-hidden"))
      .toBe(false);
  });

  it("shows a fixed idle line, not Loading, on a page loaded while idle", async () => {
    await setupApp({
      initialState: { ...streamState, current_track: null, stream_state: "idle" },
    });
    const idleLine = document.getElementById("now-playing-idle");
    expect(idleLine.hidden).toBe(false);
    expect(idleLine.textContent)
      .toBe("Nothing playing. The station starts when someone listens.");
    expect(idleLine.hasAttribute("aria-live")).toBe(false);
    // Emptied, which is not spoken; "Loading…" must not linger.
    expect(document.getElementById("now-playing-title").textContent).toBe("");
  });

  it("does not speak the title region alongside Set stopped", async () => {
    const { webSocket } = await setupApp({
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    const title = document.getElementById("now-playing-title");
    const idleLine = document.getElementById("now-playing-idle");
    expect(title.textContent).toContain("Artist");
    expect(idleLine.hidden).toBe(true);
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState,
      current_track: null,
      stream_state: "idle",
      stream_event: { id: "boot", seq: 2, name: "set_stopped" },
    }) });
    expect(title.textContent).toBe("");
    expect(title.textContent).not.toContain("—");
    expect(idleLine.hidden).toBe(false);
    // The next set's first track is announced as usual.
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState, current_track: { path: "b.mp3", artist: "B", title: "Next" },
    }) });
    expect(title.textContent).toContain("Next");
    expect(idleLine.hidden).toBe(true);
  });

  it("leaves browser mode unchanged", async () => {
    await setupApp();
    expect(document.getElementById("btn-listen").hidden).toBe(true);
    expect(document.getElementById("stream-idle-note").hidden).toBe(true);
    expect(document.getElementById("now-playing-idle").hidden).toBe(true);
    expect(document.getElementById("progress-track").hidden).toBe(false);
    expect(document.getElementById("progress-bar-label").getAttribute("aria-hidden"))
      .toBe("true");
  });

  it("shows the server clock minus the stream delay, lyrics included", async () => {
    const { webSocket } = await setupApp({
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    await vi.waitFor(() => expect(
      document.querySelectorAll("#lyrics-list li"),
    ).toHaveLength(3));
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState, has_lyrics: true, lyric_index: 2, lyric_text: "Three",
    }) });
    expect(document.getElementById("progress-bar-label").textContent).toBe("0:47 / 3:20");
    expect(document.querySelector('#lyrics-list li[aria-current="true"]').textContent)
      .toBe("Two");
    // Shift+L says the line the listener hears, not the server's.
    expect((await hotkeyOptions()).getLyricLine()).toBe("Two");
  });

  it("shows 0:00 / 0:00 once the listener passes the song's end (#428)", async () => {
    const { webSocket } = await setupApp({
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    await vi.waitFor(() => expect(
      document.querySelectorAll("#lyrics-list li"),
    ).toHaveLength(3));
    const label = document.getElementById("progress-bar-label");
    // The server has passed the end, but the listener is still in the song.
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState, has_lyrics: true, elapsed: 202,
    }) });
    expect(label.textContent).toBe("3:19 / 3:20");
    // A liner after the song: the clock runs on until the next song starts.
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState, has_lyrics: true, elapsed: 204,
    }) });
    expect(label.textContent).toBe("0:00 / 0:00");
    expect(label.hasAttribute("aria-hidden")).toBe(false);
    expect((await hotkeyOptions()).getPosition()).toBeNull();
    // The lyrics hold the song's last line rather than jump to its first.
    expect(document.querySelector('#lyrics-list li[aria-current="true"]').textContent)
      .toBe("Three");
  });

  it("starts and stops listening from the button with a fresh link", async () => {
    const { fetchImpl } = await setupApp({
      initialState: streamState,
      onRequest: (url) => url === "/api/stream"
        ? jsonResponse({ path: "/stream.mp3", m3u_path: "/stream.m3u" })
        : lyricsFor("a.mp3"),
    });
    const audio = document.getElementById("stream-audio");
    audio.play = vi.fn().mockResolvedValue();
    audio.pause = vi.fn();
    const listen = document.getElementById("btn-listen");
    const lookups = () => fetchImpl.mock.calls.filter(([url]) => url === "/api/stream").length;
    await vi.waitFor(() => expect(document.getElementById("stream-url").value)
      .toContain("/stream.mp3"));
    const before = lookups();

    listen.click();
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledOnce());
    expect(new URL(audio.src).pathname).toBe("/stream.mp3");
    expect(new URL(audio.src).searchParams.has("connection")).toBe(true);
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(lookups()).toBe(before + 1);

    listen.click();
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    expect(audio.getAttribute("src")).toBeNull();
  });

  it("keeps listening when the link drops and after it returns", async () => {
    const { webSocket, WebSocketImpl } = await setupApp({
      initialState: streamState,
      onRequest: (url) => url === "/api/stream"
        ? jsonResponse({ path: "/stream.mp3", m3u_path: "/stream.m3u" })
        : lyricsFor("a.mp3"),
    });
    const audio = document.getElementById("stream-audio");
    audio.play = vi.fn().mockResolvedValue();
    audio.pause = vi.fn();
    const listen = document.getElementById("btn-listen");
    const status = document.getElementById("sr-status");
    listen.click();
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledOnce());
    vi.useFakeTimers();

    // A tunnel in front of AutoDJ closes the websocket every few minutes.
    // Listen here is its own connection and plays on; the server ended the
    // set 30 s after the page used to stop it here.
    const sourceBeforeDrop = audio.getAttribute("src");
    webSocket.onclose({ code: 1006, wasClean: false });
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(audio.getAttribute("src")).toBe(sourceBeforeDrop);
    expect(audio.pause).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledTimes(2));
    webSocket.onopen();
    webSocket.onmessage({ data: JSON.stringify(streamState) });

    expect(document.getElementById("conn-status").textContent).toBe("Live");
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(audio.pause).not.toHaveBeenCalled();
    expect(status.textContent).toBe("");
  });

  it("keeps Listen here on while AutoDJ is down and asks for the stream again once the link is back", async () => {
    let down = false;
    const { webSocket, WebSocketImpl } = await setupApp({
      initialState: streamState,
      onRequest: (url) => {
        if (url !== "/api/stream") return lyricsFor("a.mp3");
        if (down) throw new TypeError("Failed to fetch");
        return jsonResponse({ path: "/stream.mp3", m3u_path: "/stream.m3u" });
      },
    });
    const audio = document.getElementById("stream-audio");
    audio.play = vi.fn().mockResolvedValue();
    audio.pause = vi.fn();
    const listen = document.getElementById("btn-listen");
    const status = document.getElementById("sr-status");
    listen.click();
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledOnce());
    audio.dispatchEvent(new Event("playing"));
    vi.useFakeTimers();

    // AutoDJ stops: the link and the stream go together, for longer than
    // the stream's own quiet retries last.
    down = true;
    webSocket.onclose({ code: 1006, wasClean: false });
    audio.dispatchEvent(new Event("error"));
    await vi.advanceTimersByTimeAsync(60000);
    expect(WebSocketImpl.mock.calls.length).toBeGreaterThan(1);
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(status.textContent).toBe("");

    // Back: the stream is asked for at once and plays, with nothing said.
    down = false;
    webSocket.onopen();
    await vi.advanceTimersByTimeAsync(0);
    expect(document.getElementById("conn-status").textContent).toBe("Live");
    expect(audio.play).toHaveBeenCalledTimes(2);
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    expect(status.textContent).toBe("");
  });

  it("stops listening once the reconnect check finds the session ended", async () => {
    let authChecks = 0;
    const { dialog, webSocket } = await setupApp({
      initialState: streamState,
      onAuthStatus: () => {
        authChecks += 1;
        return jsonResponse({ required: true, authenticated: authChecks === 1 });
      },
      onRequest: (url) => url === "/api/stream"
        ? jsonResponse({ path: "/stream.mp3", m3u_path: "/stream.m3u" })
        : lyricsFor("a.mp3"),
    });
    const audio = document.getElementById("stream-audio");
    audio.play = vi.fn().mockResolvedValue();
    audio.pause = vi.fn();
    const listen = document.getElementById("btn-listen");
    listen.click();
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledOnce());
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    expect(listen.getAttribute("aria-pressed")).toBe("true");
    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());

    expect(listen.getAttribute("aria-pressed")).toBe("false");
    expect(audio.getAttribute("src")).toBeNull();
  });

  it("refuses seeking and answers status keys from the server clock", async () => {
    const { fetchImpl } = await setupApp({
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    const options = await hotkeyOptions();

    options.seekDelta(5);
    await vi.waitFor(() => expect(document.getElementById("sr-status").textContent)
      .toBe("Seeking is not available while streaming."));
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/seek")).toBe(false);

    expect(options.getBpm()).toBe(128);
    expect(options.getRemaining()).toBe(153);
  });

  it("routes Space and K to Listen here in stream mode", async () => {
    await setupApp({
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    const options = await hotkeyOptions();
    const listenClick = vi.spyOn(document.getElementById("btn-listen"), "click")
      .mockImplementation(() => {});
    const pauseClick = vi.spyOn(document.getElementById("btn-pause"), "click");

    options.togglePlay();
    expect(listenClick).toHaveBeenCalledOnce();
    expect(pauseClick).not.toHaveBeenCalled();
  });

  it("keeps Space and K on Play / Pause outside stream mode", async () => {
    await setupApp({ initialState: { current_track: { path: "a.mp3", title: "A" } },
      onRequest: () => jsonResponse({ paused: true }) });
    const options = await hotkeyOptions();
    const pauseClick = vi.spyOn(document.getElementById("btn-pause"), "click")
      .mockImplementation(() => {});
    options.togglePlay();
    expect(pauseClick).toHaveBeenCalledOnce();
  });

  it("tests liners through the server whenever the server mixes the audio", async () => {
    let linerDeps;
    const { webSocket } = await setupApp({
      initialState: streamState,
      onInstallLiners: (_els, deps) => { linerDeps = deps; },
      onRequest: () => lyricsFor("a.mp3"),
    });
    expect(linerDeps.testOnServer()).toBe(true);
    // Plain --server-audio: no stream, and the browser does not play.
    webSocket.onmessage({ data: JSON.stringify({ ...streamState, stream_mode: false }) });
    expect(linerDeps.testOnServer()).toBe(true);
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState, stream_mode: false, browser_playback: true,
    }) });
    expect(linerDeps.testOnServer()).toBe(false);
  });

  it("keeps volume and mute on this page's own listening in stream mode", async () => {
    const setVolume = vi.fn();
    const { fetchImpl, webSocket } = await setupApp({
      audio: { setVolume },
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    const audio = document.getElementById("stream-audio");
    const slider = document.querySelector("#vol");
    const region = document.querySelector("#vol-announce");
    const mute = document.querySelector("#btn-mute");
    const serverCalls = () => fetchImpl.mock.calls
      .filter(([url]) => url === "/api/volume" || url === "/api/mute");

    // The arrow-key hotkeys drive the slider with focus elsewhere.
    slider.value = "50";
    slider.dispatchEvent(new Event("input"));
    expect(audio.volume).toBeCloseTo(10 ** (-30 / 20), 4);
    expect(slider.getAttribute("aria-valuetext")).toBe("50%");
    await vi.waitFor(() => expect(region.textContent).toBe("Volume 50%."));
    // The unused deck engine's master holds the same level.
    expect(setVolume).toHaveBeenLastCalledWith(audio.volume);

    // M and the Mute button toggle only this page's audio.
    mute.click();
    await Promise.resolve();
    expect(audio.muted).toBe(true);
    expect(mute.getAttribute("aria-pressed")).toBe("true");
    expect(mute.textContent.replace(/[^A-Za-z]/g, "")).toBe("Mute");

    // The station's own volume and mute never overwrite the page's.
    await new Promise((resolve) => setTimeout(resolve, 650));
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState, volume: 0.2, is_muted: false,
    }) });
    expect(slider.value).toBe("50");
    expect(mute.getAttribute("aria-pressed")).toBe("true");

    mute.click();
    await Promise.resolve();
    expect(audio.muted).toBe(false);
    expect(mute.getAttribute("aria-pressed")).toBe("false");
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(serverCalls()).toHaveLength(0);
  });

  it("drives the server's speakers too when stream mode has server audio", async () => {
    const serverState = { ...streamState, stream_server_audio: true, volume: 1, is_muted: false };
    const { fetchImpl, webSocket } = await setupApp({
      initialState: serverState,
      onRequest: (url) => {
        if (url === "/api/mute") return jsonResponse({ muted: true });
        if (url === "/api/volume") return jsonResponse({ volume: 0.03 });
        return lyricsFor("a.mp3");
      },
    });
    const audio = document.getElementById("stream-audio");
    const slider = document.querySelector("#vol");
    const mute = document.querySelector("#btn-mute");
    const calls = (path) => fetchImpl.mock.calls.filter(([url]) => url === path);

    slider.value = "50";
    slider.dispatchEvent(new Event("input"));
    expect(audio.volume).toBeCloseTo(10 ** (-30 / 20), 4);
    await vi.waitFor(() => expect(calls("/api/volume")).toHaveLength(1));
    expect(JSON.parse(calls("/api/volume")[0][1].body).volume).toBeCloseTo(10 ** (-30 / 20), 4);

    mute.click();
    await vi.waitFor(() => expect(audio.muted).toBe(true));
    expect(calls("/api/mute")).toHaveLength(1);
    expect(mute.getAttribute("aria-pressed")).toBe("true");

    // A change made elsewhere (another page, the terminal) reaches this
    // page's listening as well as the slider and Mute, once the server
    // has echoed this page's own change.
    webSocket.onmessage({ data: JSON.stringify({ ...serverState, volume: 0.03, is_muted: true }) });
    webSocket.onmessage({ data: JSON.stringify({ ...serverState, volume: 0.1, is_muted: false }) });
    expect(slider.value).toBe(String(Math.round((20 * Math.log10(0.1) / 30 + 2) * 50)));
    expect(audio.volume).toBeCloseTo(0.1, 4);
    expect(audio.muted).toBe(false);
    expect(mute.getAttribute("aria-pressed")).toBe("false");
  });

  it("starts a fresh stream-mode page at the server's saved volume and mute", async () => {
    const quiet = 10 ** (-57 / 20);   // 5 %
    const { webSocket } = await setupApp({
      initialState: { ...streamState, volume: quiet, is_muted: true },
      onRequest: () => lyricsFor("a.mp3"),
    });
    const audio = document.getElementById("stream-audio");
    const slider = document.querySelector("#vol");
    const mute = document.querySelector("#btn-mute");
    expect(slider.value).toBe("5");
    expect(slider.getAttribute("aria-valuetext")).toBe("5%");
    expect(audio.volume).toBeCloseTo(quiet, 6);
    expect(audio.muted).toBe(true);
    expect(mute.getAttribute("aria-pressed")).toBe("true");

    // Seeded once: later station pushes leave the page's level alone.
    await new Promise((resolve) => setTimeout(resolve, 650));
    webSocket.onmessage({ data: JSON.stringify({ ...streamState, volume: 1, is_muted: false }) });
    expect(slider.value).toBe("5");
    expect(audio.volume).toBeCloseTo(quiet, 6);
    expect(audio.muted).toBe(true);
  });

  it("never lets a page media element start playing above the page level (D18)", async () => {
    const quiet = 10 ** (-57 / 20);   // 5 %
    const half = 10 ** (-30 / 20);    // 50 %
    // Every play() of any media element, with the level it started at.
    const plays = [];
    const mediaPrototype = globalThis.HTMLMediaElement.prototype;
    vi.spyOn(mediaPrototype, "play").mockImplementation(function recordPlay() {
      plays.push({ element: this, volume: this.volume, muted: this.muted });
      return Promise.resolve();
    });
    try {
      // The page was at 5 % in browser playback when the link dropped and
      // the server came back in stream mode, pushing full volume.
      const { webSocket, WebSocketImpl } = await setupApp({
        initialState: { volume: quiet },
        onRequest: (url) => url === "/api/stream"
          ? jsonResponse({ path: "/stream.mp3", m3u_path: "/stream.m3u" })
          : lyricsFor("a.mp3"),
      });
      const audio = document.getElementById("stream-audio");
      audio.pause = vi.fn();
      audio.load = vi.fn();
      const slider = document.querySelector("#vol");
      const mute = document.querySelector("#btn-mute");
      const listen = document.getElementById("btn-listen");
      const push = (state = {}) => webSocket.onmessage({ data: JSON.stringify({
        ...streamState, volume: 1, is_muted: false, ...state,
      }) });
      vi.useFakeTimers();
      const reconnect = async (count) => {
        webSocket.onclose({ code: 1006, wasClean: false });
        await vi.advanceTimersByTimeAsync(60000);
        await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledTimes(count));
        webSocket.onopen();
      };

      await reconnect(2);
      expect(audio.volume).toBeCloseTo(quiet, 6);
      push();
      expect(slider.value).toBe("5");
      expect(audio.volume).toBeCloseTo(quiet, 6);

      listen.click();
      await vi.waitFor(() => expect(plays).toHaveLength(1));
      expect(plays[0].element).toBe(audio);
      expect(plays[0].volume).toBeCloseTo(quiet, 6);
      expect(plays[0].muted).toBe(false);

      await vi.advanceTimersByTimeAsync(700);
      push();
      expect(audio.volume).toBeCloseTo(quiet, 6);

      slider.value = "50";
      slider.dispatchEvent(new Event("input"));
      expect(audio.volume).toBeCloseTo(half, 6);
      mute.click();
      expect(audio.muted).toBe(true);
      expect(audio.volume).toBeCloseTo(half, 6);
      mute.click();
      expect(audio.muted).toBe(false);
      expect(audio.volume).toBeCloseTo(half, 6);

      // A dropped link keeps listening, at the same level after the return.
      await reconnect(3);
      expect(listen.getAttribute("aria-pressed")).toBe("true");
      expect(audio.volume).toBeCloseTo(half, 6);
      push();
      expect(audio.volume).toBeCloseTo(half, 6);
      // Listening again after a stop starts at the page level too.
      listen.click();
      listen.click();
      await vi.waitFor(() => expect(plays).toHaveLength(2));
      expect(plays[1].volume).toBeCloseTo(half, 6);
      expect(plays[1].muted).toBe(false);
    } finally {
      mediaPrototype.play.mockRestore?.();
      vi.restoreAllMocks();
    }
  });

  it("plays sound only through outputs the page level reaches", () => {
    const dir = join(process.cwd(), "src/autodj/static");
    const files = ["app.js", ...readdirSync(join(dir, "modules")).map((name) => `modules/${name}`)]
      .filter((name) => name.endsWith(".js"));
    const found = { plays: [], destinations: [], detached: [] };
    for (const name of files) {
      const lines = readFileSync(join(dir, name), "utf8").split(/\r?\n/);
      lines.forEach((line, index) => {
        if (line.includes(".play()")) {
          found.plays.push(`${name}: ${lines[index - 1].trim()} | ${line.trim()}`);
        }
        if (/\.destination\b/.test(line)) found.destinations.push(`${name}: ${line.trim()}`);
      });
      // A media element outside the document escapes applyPageLevel.
      if (lines.some((line) => /new (?:Audio|Video)\(|createElement\(\s*["'](?:audio|video)["']/.test(line))) {
        found.detached.push(name);
      }
    }
    expect(found.detached).toEqual([]);
    // Web Audio reaches the speakers through the master alone.
    expect(found.destinations).toEqual([
      "modules/audio-engine.js: _master.connect(_ctx.destination);",
    ]);
    // The decks play through the master; the stream sets the page level
    // on its element first.  The public player page (not the web UI) has
    // its own Volume slider.  A new play() must be added here on purpose.
    expect(found.plays.map((line) => line.replace(/^([^:]+):.*\| /, "$1: "))).toEqual([
      "modules/audio-engine.js: return deck.audio.play().catch((err) => {",
      "modules/audio-engine.js: await deck.audio.play();",
      "modules/library-folders.js: await preview.play();",
      // Track info, Matched intros, Listen: a preview in the page, like Listen back.
      "modules/matched-intros.js: await playback.play();",
      "modules/player-page.js: playing = audio.play();",
      "modules/stream-mode.js: await audio.play();",
      // Listen back: a preview in the page, which follows the page level
      // or plays on the Preview output device.
      "modules/voice-tracker.js: await playback.play();",
      "modules/voice-tracker.js: await testPlayback.play();",
    ]);
    expect(found.plays.find((line) => line.startsWith("modules/stream-mode.js")))
      .toBe("modules/stream-mode.js: beforePlay(); | await audio.play();");
  });

  it("still sends volume and mute to the server outside stream mode", async () => {
    const { fetchImpl } = await setupApp({
      initialState: { current_track: { path: "a.mp3", title: "A" } },
      onRequest: (url) => url === "/api/mute"
        ? jsonResponse({ muted: true })
        : jsonResponse({ volume: 0.5 }),
    });
    const slider = document.querySelector("#vol");
    slider.value = "60";
    slider.dispatchEvent(new Event("input"));
    document.querySelector("#btn-mute").click();
    await vi.waitFor(() => expect(fetchImpl.mock.calls.map(([url]) => url))
      .toEqual(expect.arrayContaining(["/api/volume", "/api/mute"])));
    // The page's mute reaches every output it owns, used or not.
    expect(document.getElementById("stream-audio").muted).toBe(true);
  });

  it("lets the OS media keys start Listen here only after this page pressed it", async () => {
    const { webSocket } = await setupApp({
      initialState: streamState,
      onRequest: () => lyricsFor("a.mp3"),
    });
    const mediaSession = await import("../../src/autodj/static/modules/media-session.js");
    const options = mediaSession.installMediaActionHandlers.mock.calls[0][0];
    const listen = document.getElementById("btn-listen");
    const play = vi.spyOn(document.getElementById("stream-audio"), "play")
      .mockResolvedValue(undefined);

    // Never pressed here: Play and Pause are dealt with, and nothing plays.
    expect(await options.onPlay()).toBe(true);
    expect(await options.onPause()).toBe(true);
    expect(play).not.toHaveBeenCalled();
    expect(listen.getAttribute("aria-pressed")).not.toBe("true");

    listen.click();
    await vi.waitFor(() => expect(play).toHaveBeenCalledOnce());
    expect(await options.onPause()).toBe(true);
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    // Pause never starts it again.
    expect(await options.onPause()).toBe(true);
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    expect(await options.onPlay()).toBe(true);
    expect(play).toHaveBeenCalledTimes(2);
    expect(listen.getAttribute("aria-pressed")).toBe("true");

    webSocket.onmessage({ data: JSON.stringify({ ...streamState, stream_mode: false }) });
    expect(await options.onPause()).toBe(false);
  });

  it("never lets the OS media Play key start a page that has not pressed Play", async () => {
    const unlockAndPlay = vi.fn().mockResolvedValue(true);
    await setupApp({
      audio: { _lastBrowserPlayback: true, playbackEnabled: false, unlockAndPlay },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
        is_paused: true,
      },
    });
    const mediaSession = await import("../../src/autodj/static/modules/media-session.js");
    const options = mediaSession.installMediaActionHandlers.mock.calls[0][0];

    // true: handled here, so the server's pause toggle does not run either.
    expect(await options.onPlay()).toBe(true);
    expect(await options.onPause()).toBe(true);
    expect(unlockAndPlay).not.toHaveBeenCalled();

    // Once Play was pressed on this page, the media key may start it again
    // after a hard stop (playbackEnabled is still false in this mock).
    document.querySelector("#btn-pause").click();
    await vi.waitFor(() => expect(unlockAndPlay).toHaveBeenCalledOnce());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(await options.onPlay()).toBe(true);
    expect(unlockAndPlay).toHaveBeenCalledTimes(2);
  });

  it("lets Play now on a search result, and nothing else there, start a page that never pressed Play", async () => {
    // The mock plays the way the engine does: the unlock runs inside the
    // click, then the Play now request, then the deck starts.
    const unlockAndPlay = vi.fn(async (beforePlay) => {
      if (beforePlay) await beforePlay();
      return true;
    });
    const { fetchImpl } = await setupApp({
      audio: { _lastBrowserPlayback: true, playbackEnabled: false, unlockAndPlay },
      initialState: {
        browser_playback: true,
        current_track: { path: "current.mp3", title: "Current" },
      },
      onRequest: (url) => (url.startsWith("/api/library/tracks?")
        ? jsonResponse({ results: [{ path: "song.mp3", title: "Song", artist: "Band", kind: "music" }], total: 1, offset: 0 })
        : jsonResponse({ ok: true })),
    });
    const mediaSession = await import("../../src/autodj/static/modules/media-session.js");
    const options = mediaSession.installMediaActionHandlers.mock.calls[0][0];
    document.querySelector("#search-input").value = "song";
    document.querySelector("#btn-search").click();
    await vi.waitFor(() => expect(document.querySelectorAll(".result-btn")).toHaveLength(4));
    const [playNow, playNext, addToQueue] = document.querySelectorAll(".result-btn");
    const posts = () => fetchImpl.mock.calls.filter(([url]) => url === "/api/play-next"
      || url === "/api/queue/add").map(([url, init]) => [url, JSON.parse(init.body)]);

    playNext.click();
    addToQueue.click();
    await vi.waitFor(() => expect(posts()).toHaveLength(2));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(unlockAndPlay).not.toHaveBeenCalled();
    expect(await options.onPlay()).toBe(true);
    expect(unlockAndPlay).not.toHaveBeenCalled();

    playNow.click();
    // Synchronously, in the click, before the request is sent.
    expect(unlockAndPlay).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(document.querySelector("#queue-announce").textContent)
      .toContain("now."));
    expect(posts().at(-1)).toEqual(["/api/play-next", { path: "song.mp3", now: true }]);
    // The page opted in: the media Play key may now restart it.
    expect(await options.onPlay()).toBe(true);
    expect(unlockAndPlay).toHaveBeenCalledTimes(2);
  });

  it("never lets the OS media Pause key unpause the server", async () => {
    const { webSocket } = await setupApp({ initialState: { is_paused: true } });
    const mediaSession = await import("../../src/autodj/static/modules/media-session.js");
    const options = mediaSession.installMediaActionHandlers.mock.calls[0][0];

    expect(await options.onPause()).toBe(true);
    expect(await options.onPlay()).toBe(false);

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: false, current_track: null, is_paused: false, queue: [],
    }) });
    expect(await options.onPause()).toBe(false);
    expect(await options.onPlay()).toBe(true);
  });

  it("moves focus to Play / Pause when a mode change hides the focused control", async () => {
    const track = { path: "a.mp3", title: "A" };
    const { webSocket } = await setupApp({
      initialState: { ...streamState, current_track: track },
      onRequest: () => lyricsFor("a.mp3"),
    });
    const listen = document.getElementById("btn-listen");
    listen.focus();
    webSocket.onmessage({ data: JSON.stringify({
      ...streamState, current_track: track, stream_mode: false,
    }) });
    expect(listen.hidden).toBe(true);
    expect(document.activeElement.id).toBe("btn-pause");

    const seek = document.getElementById("progress-track");
    seek.focus();
    webSocket.onmessage({ data: JSON.stringify({ ...streamState, current_track: track }) });
    expect(seek.hidden).toBe(true);
    expect(document.activeElement.id).toBe("btn-pause");
  });

  it("answers the BPM and remaining-time keys from server state without --stream", async () => {
    await setupApp({
      initialState: {
        browser_playback: false,
        current_track: { path: "a.mp3", title: "A", bpm: 120 },
        duration: 180,
        elapsed: 30,
      },
      onRequest: () => lyricsFor("a.mp3"),
    });
    const options = await hotkeyOptions();
    expect(options.getBpm()).toBe(120);
    expect(options.getRemaining()).toBe(150);
  });

  it("stops listening when the session expires", async () => {
    let expire = false;
    await setupApp({
      initialState: streamState,
      onRequest: (url) => {
        if (expire) return jsonResponse({ detail: "Authentication required" }, 401);
        return url === "/api/stream"
          ? jsonResponse({ path: "/stream.mp3" })
          : lyricsFor("a.mp3");
      },
    });
    const audio = document.getElementById("stream-audio");
    audio.play = vi.fn().mockResolvedValue();
    audio.pause = vi.fn();
    const listen = document.getElementById("btn-listen");
    listen.click();
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledOnce());

    expire = true;
    document.getElementById("btn-skip").click();
    await vi.waitFor(() => expect(document.getElementById("conn-status").textContent)
      .toBe("Authentication required"));
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    expect(audio.getAttribute("src")).toBeNull();
    // Clear the stream address from the page with the session state.
    expect(document.getElementById("stream-url").value).toBe("");
    expect(document.getElementById("stream-m3u").hasAttribute("href")).toBe(false);
  });

  describe("stream settings", () => {
    const oldInfo = {
      path: "/stream.mp3", m3u_path: "/stream.m3u", bitrate: 320, listeners: 1, state: "playing",
    };
    const settle = () => new Promise((resolve) => setTimeout(resolve, 20));
    const stationServer = (overrides = {}) => (url, options) => {
      if (url in overrides) return overrides[url](options);
      if (url === "/api/stream") return jsonResponse(oldInfo);
      return lyricsFor("a.mp3");
    };

    it("stays hidden in browser mode", async () => {
      await setupApp();
      expect(document.getElementById("stream-settings").hidden).toBe(true);
      expect(document.getElementById("stream-url").value).toBe("");
    });

    it("shows the address, playlist link and listener count in stream mode", async () => {
      await setupApp({ initialState: streamState, onRequest: stationServer() });
      expect(document.getElementById("stream-settings").hidden).toBe(false);
      await vi.waitFor(() => expect(document.getElementById("stream-url").value)
        .toBe(`${location.origin}/stream.mp3`));
      expect(document.getElementById("stream-m3u").getAttribute("href")).toBe("/stream.m3u");
      expect(document.getElementById("stream-listeners").textContent).toBe("1 listener");
    });

    it("sends one quality save for quick arrow presses", async () => {
      const { fetchImpl } = await setupApp({
        initialState: streamState,
        onRequest: stationServer({
          "/api/stream/settings": () => jsonResponse({ ...oldInfo, bitrate: 128 }),
        }),
      });
      const quality = document.getElementById("stream-bitrate");
      await vi.waitFor(() => expect(document.getElementById("stream-url").value)
        .toContain("/stream.mp3"));
      for (const value of ["256", "192", "128"]) {
        quality.value = value;
        quality.dispatchEvent(new Event("change"));
      }
      const saves = () => fetchImpl.mock.calls.filter(([called]) => called === "/api/stream/settings");
      await vi.waitFor(() => expect(saves()).toHaveLength(1));
      await new Promise((resolve) => setTimeout(resolve, 800));
      expect(saves()).toHaveLength(1);
      expect(JSON.parse(saves()[0][1].body)).toEqual({ bitrate: 128 });
    });

    it("follows the quality pushed by the server", async () => {
      const { webSocket } = await setupApp({ initialState: streamState, onRequest: stationServer() });
      const quality = document.getElementById("stream-bitrate");
      await vi.waitFor(() => expect(quality.value).toBe("320"));
      webSocket.onmessage({ data: JSON.stringify({
        ...streamState, settings: { playback: { stream_bitrate: 192 } },
      }) });
      expect(quality.value).toBe("192");
    });

    it("looks the address up again when the Settings tab opens", async () => {
      let fail = true;
      const { fetchImpl } = await setupApp({
        initialState: streamState,
        onRequest: stationServer({
          "/api/stream": () => (fail
            ? jsonResponse({ detail: "stream encoder failed" }, 503)
            : jsonResponse(oldInfo)),
        }),
      });
      const note = document.getElementById("stream-url-note");
      await vi.waitFor(() => expect(fetchImpl.mock.calls
        .filter(([called]) => called === "/api/stream")).toHaveLength(1));
      await settle();
      expect(note.hidden).toBe(false);
      expect(note.textContent).toBe("Stream address not loaded yet.");
      fail = false;
      try {
        location.hash = "#settings";
        window.dispatchEvent(new Event("hashchange"));
        await vi.waitFor(() => expect(document.getElementById("stream-url").value)
          .toBe(`${location.origin}/stream.mp3`));
        expect(note.hidden).toBe(true);
      } finally {
        location.hash = "";
      }
    });

    it("saves the quality and puts it back when the encoder cannot restart", async () => {
      let bitrateReply = () => jsonResponse({ ...oldInfo, bitrate: 192 });
      const { fetchImpl } = await setupApp({
        initialState: streamState,
        onRequest: stationServer({ "/api/stream/settings": () => bitrateReply() }),
      });
      const quality = document.getElementById("stream-bitrate");
      await vi.waitFor(() => expect(quality.value).toBe("320"));
      quality.value = "192";
      quality.dispatchEvent(new Event("change"));
      await vi.waitFor(() => expect(fetchImpl.mock.calls
        .filter(([called]) => called === "/api/stream/settings")).toHaveLength(1));
      const [, options] = fetchImpl.mock.calls.find(([called]) => called === "/api/stream/settings");
      expect(options.method).toBe("POST");
      expect(JSON.parse(options.body)).toEqual({ bitrate: 192 });
      await settle();
      expect(quality.value).toBe("192");

      bitrateReply = () => jsonResponse({ detail: "stream encoder failed" }, 503);
      quality.value = "128";
      quality.dispatchEvent(new Event("change"));
      await vi.waitFor(() => expect(document.getElementById("settings-status").textContent)
        .toBe("Could not save Quality; it is still 192 kbps. stream encoder failed"));
      await vi.waitFor(() => expect(quality.value).toBe("192"));
    });
  });
});

describe("browser robustness", () => {
  it("tells the decks when the link drops and when it is back", async () => {
    const setLinkUp = vi.fn();
    const { webSocket, WebSocketImpl } = await setupApp({
      audio: { setLinkUp },
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onRequest: () => jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();
    webSocket.onopen();
    expect(setLinkUp).toHaveBeenLastCalledWith(true);

    webSocket.onclose({ code: 1006, wasClean: false });
    expect(setLinkUp).toHaveBeenLastCalledWith(false);
    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledTimes(2));
    webSocket.onopen();
    expect(setLinkUp).toHaveBeenLastCalledWith(true);
  });

  it("stops the decks once the reconnect check finds the session ended", async () => {
    let authChecks = 0;
    const { dialog, stopAllDecks, webSocket } = await setupApp({
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onAuthStatus: () => {
        authChecks += 1;
        return jsonResponse({ required: true, authenticated: authChecks === 1 });
      },
      onRequest: () => jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    expect(stopAllDecks).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    expect(stopAllDecks).toHaveBeenCalledOnce();
  });

  it("holds scheduled liners while a crossfade runs", async () => {
    let linerDeps;
    await setupApp({
      audio: {
        _ctx: {}, _lastBrowserPlayback: true, crossfading: true,
        decks: [{ audio: {} }], playbackEnabled: true,
      },
      initialState: { browser_playback: true },
      onInstallLiners: (_elements, deps) => { linerDeps = deps; },
    });
    expect(linerDeps.canPlay()).toBe(false);
  });

  it("ignores a push that predates this page's advance, saying nothing", async () => {
    const applyBrowserPlaybackState = vi.fn();
    const isStaleAdvanceState = vi.fn((state) => state.current_track?.path === "old.mp3");
    const { webSocket } = await setupApp({
      audio: { applyBrowserPlaybackState, isStaleAdvanceState },
      initialState: {
        browser_playback: true,
        current_track: { path: "new.mp3", title: "New" },
      },
      onRequest: () => jsonResponse({ lyrics: [] }),
    });
    const announce = document.querySelector("#track-announce");
    const said = vi.spyOn(announce, "textContent", "set");
    applyBrowserPlaybackState.mockClear();

    webSocket.onmessage({ data: JSON.stringify({
      browser_playback: true, current_track: { path: "old.mp3", title: "Old" },
      queue: [], eq: {}, volume: 1,
    }) });

    expect(applyBrowserPlaybackState).not.toHaveBeenCalled();
    expect(document.querySelector("#now-playing-title").textContent).toContain("New");
    expect(said).not.toHaveBeenCalled();
  });

  it("holds the volume slider through a slow save, not for a fixed time", async () => {
    let answer;
    const { webSocket } = await setupApp({
      initialState: { volume: 1 },
      onRequest: (url, options) => (url === "/api/volume"
        ? new Promise((resolve) => { answer = () => resolve(jsonResponse(JSON.parse(options.body))); })
        : jsonResponse({ ok: true })),
    });
    const slider = document.querySelector("#vol");
    const push = (volume) => webSocket.onmessage({ data: JSON.stringify({
      current_track: null, queue: [], eq: {}, volume,
    }) });
    vi.useFakeTimers();

    slider.value = "50";
    slider.dispatchEvent(new Event("input"));
    // Two seconds into a slow save, a push still carries the old volume.
    await vi.advanceTimersByTimeAsync(2000);
    push(1);
    expect(slider.value).toBe("50");

    answer();
    await vi.advanceTimersByTimeAsync(0);
    push(1);
    expect(slider.value).toBe("50");
    // The echo of the saved volume hands the slider back to the server.
    push(10 ** (-30 / 20));
    push(10 ** (-15 / 20));
    expect(slider.value).toBe("75");
  });

  it("says once, after a few failed checks, that the page may need a reload to sign in", async () => {
    let authChecks = 0;
    const { webSocket, WebSocketImpl } = await setupApp({
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onAuthStatus: () => {
        authChecks += 1;
        if (authChecks === 1) return jsonResponse({ required: true, authenticated: true });
        return Promise.reject(new TypeError("Failed to fetch"));
      },
      onRequest: () => jsonResponse({ ok: true }),
    });
    const status = document.querySelector("#sr-status");
    const said = vi.spyOn(status, "textContent", "set");
    vi.useFakeTimers();

    let delay = 3000;
    for (let cycle = 0; cycle < 6; cycle += 1) {
      webSocket.onclose({ code: 1006, wasClean: false });
      await vi.advanceTimersByTimeAsync(delay);
      await vi.waitFor(() => expect(WebSocketImpl).toHaveBeenCalledTimes(cycle + 2));
      delay = Math.min(delay * 2, 60000);
      if (cycle === 1) expect(said).not.toHaveBeenCalled();
    }

    const notices = said.mock.calls.filter(([text]) => String(text).includes("reload the page"));
    expect(notices).toHaveLength(1);
    expect(notices[0][0]).toBe("AutoDJ still cannot be reached. If this site asks you to sign in, "
      + "reload the page to sign in again.");
  });

  it("says at once that the sign-in expired when the check is sent to a sign-in page", async () => {
    let authChecks = 0;
    const { webSocket } = await setupApp({
      initialState: { current_track: { path: "current.mp3", title: "Current" } },
      onAuthStatus: () => {
        authChecks += 1;
        if (authChecks === 1) return jsonResponse({ required: true, authenticated: true });
        return { type: "opaqueredirect", status: 0, ok: false };
      },
      onRequest: () => jsonResponse({ ok: true }),
    });
    vi.useFakeTimers();

    webSocket.onclose({ code: 1006, wasClean: false });
    await vi.advanceTimersByTimeAsync(3000);
    await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent)
      .toBe("Your sign-in to this site has expired. Reload the page to sign in again."));
  });

  describe("beatmatch on Skip", () => {
    const skipWith = async ({ outBpm, inBpm }) => {
      let finish;
      const startCrossfade = vi.fn((path) => {
        standbyDeck.path = path;
        return new Promise((resolve) => { finish = resolve; });
      });
      const standby = { defaultPlaybackRate: 1, playbackRate: 1, preservesPitch: false };
      const standbyDeck = { audio: standby, kind: "music", path: "previous-standby.mp3" };
      await setupApp({
        audio: {
          _beatmatchOnSkip: true,
          _ctx: {},
          _inBpmCache: inBpm,
          _lastBrowserPlayback: true,
          _nextTrackPathCache: "next.mp3",
          _outBpmCache: outBpm,
          crossfading: false,
          decks: [{ audio: { duration: Number.NaN } }, standbyDeck],
          playbackEnabled: true,
          startCrossfade,
        },
        initialState: {
          browser_playback: true,
          current_track: { path: "current.mp3", title: "Current" },
          next_track: { path: "next.mp3", title: "Next" },
        },
        onRequest: () => jsonResponse({ ok: true }),
      });
      vi.useFakeTimers();
      document.querySelector("#btn-skip").click();
      await vi.waitFor(() => expect(startCrossfade).toHaveBeenCalledOnce());
      return { finish, standby };
    };

    it("holds the stretch for the whole transition, then eases back", async () => {
      const { finish, standby } = await skipWith({ outBpm: 120, inBpm: 125 });
      expect(standby.playbackRate).toBeCloseTo(0.96, 6);
      expect(standby.defaultPlaybackRate).toBeCloseTo(0.96, 6);
      expect(standby.preservesPitch).toBe(true);
      // An effect can run up to 12 s, well past crossfade seconds.
      await vi.advanceTimersByTimeAsync(11000);
      expect(standby.playbackRate).toBeCloseTo(0.96, 6);
      expect(standby.defaultPlaybackRate).toBeCloseTo(0.96, 6);

      finish(true);
      await vi.advanceTimersByTimeAsync(2000);
      expect(standby.playbackRate).toBeGreaterThan(0.96);
      expect(standby.playbackRate).toBeLessThan(1);
      expect(standby.defaultPlaybackRate).toBeCloseTo(standby.playbackRate, 6);
      await vi.advanceTimersByTimeAsync(2000);
      expect(standby.playbackRate).toBe(1);
      expect(standby.defaultPlaybackRate).toBe(1);
      expect(standby.preservesPitch).toBe(false);
    });

    it("leaves a tempo more than 8 percent away alone, as the server does", async () => {
      const { standby } = await skipWith({ outBpm: 120, inBpm: 140 });
      expect(standby.playbackRate).toBe(1);
      expect(standby.preservesPitch).toBe(false);
    });
  });
});

describe("several stations (#70)", () => {
  const AUTODJ = { slug: "default", name: "AutoDJ", state: "playing", listeners: 2, title: "A - B" };
  const OWL = { slug: "nightowl", name: "Night Owl Radio", state: "playing", listeners: 3, title: "Owl - Hoot" };
  const owlState = {
    stream_mode: true,
    stream_state: "playing",
    current_track: { path: "owl.mp3", artist: "Owl", title: "Hoot" },
    duration: 100,
    elapsed: 10,
  };

  // Every socket the page opens after this, newest last.
  function trackSockets(WebSocketImpl) {
    const sockets = [];
    WebSocketImpl.mockImplementation(function WebSocketMock(url) {
      const socket = { url, close: vi.fn(), readyState: 1, send: vi.fn() };
      sockets.push(socket);
      return socket;
    });
    return sockets;
  }

  function spoken() {
    const said = [];
    const region = document.getElementById("sr-status");
    new window.MutationObserver(() => {
      if (region.textContent) said.push(region.textContent);
    }).observe(region, { childList: true, characterData: true, subtree: true });
    return said;
  }

  function owlRequests(url) {
    if (url === "/api/stations/nightowl/status") return jsonResponse(owlState);
    if (url === "/api/stations/nightowl/start") return jsonResponse(OWL);
    return jsonResponse({ detail: "not in this test" }, 404);
  }

  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  function chooseOwl() {
    const button = document.getElementById("station-button");
    button.focus();
    button.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    // Keys go where focus is: from the checked AutoDJ down to Night Owl.
    const press = (key) => document.activeElement.dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true }));
    press("ArrowDown");
    expect(document.activeElement.dataset.station).toBe("nightowl");
    press("Enter");
  }

  it("shows no switcher and changes nothing with one station", async () => {
    await setupApp();
    expect(document.getElementById("station-switcher").hidden).toBe(true);
    expect(document.getElementById("page-heading").textContent).toBe("AutoDJ");
    expect(document.getElementById("main-content").hasAttribute("aria-label")).toBe(false);
    expect(document.getElementById("panel-queue").getAttribute("aria-labelledby")).toBe("tab-queue");
  });

  it("titles the page without the album and names a lone station not called AutoDJ", async () => {
    await setupApp({
      initialState: { current_track: { path: "a.mp3", artist: "A", title: "B", album: "C" } },
      stations: [{ ...AUTODJ, name: "Night Owl Radio" }],
    });
    await vi.waitFor(() => expect(document.title).toBe("A - B - Night Owl Radio - AutoDJ"));
  });

  it("switches inside the page: socket, requests, names and the announcement", async () => {
    const app = await setupApp({
      initialState: { current_track: { path: "a.mp3", artist: "A", title: "B" } },
      stations: [AUTODJ, OWL],
      onRequest: owlRequests,
    });
    const sockets = trackSockets(app.WebSocketImpl);
    const said = spoken();
    const first = app.webSocket;
    expect(document.getElementById("station-switcher").hidden).toBe(false);
    expect(document.getElementById("station-button").textContent.replace(/\s+/g, " ").trim())
      .toBe("Station: AutoDJ▾");

    chooseOwl();
    await vi.waitFor(() => expect(sockets).toHaveLength(1));

    // The old socket is closed on purpose and never reconnects.
    expect(first.close).toHaveBeenCalledWith(1000, "Station switched");
    expect(sockets[0].url).toMatch(/\/api\/stations\/nightowl\/ws$/);
    expect(app.fetchImpl.mock.calls.map(([url]) => url)).toContain("/api/stations/nightowl/status");
    first.onclose({ code: 1000, wasClean: true });
    expect(document.getElementById("conn-status").textContent).not.toMatch(/Disconnected/);

    // Named everywhere a reader looks; focus stays on the switcher.
    expect(document.activeElement.id).toBe("station-button");
    expect(document.getElementById("page-heading").textContent).toBe("AutoDJ: Night Owl Radio");
    expect(document.title).toBe("Owl - Hoot - Night Owl Radio - AutoDJ");
    expect(document.getElementById("shared-context").textContent).toBe("Shared by all stations");
    // Only the heading names the station; panels and the main region do not repeat it.
    expect(document.getElementById("main-content").hasAttribute("aria-label")).toBe(false);
    expect(document.getElementById("panel-queue").getAttribute("aria-labelledby")).toBe("tab-queue");
    expect(document.querySelector('[data-station="nightowl"]').getAttribute("aria-checked")).toBe("true");
    expect(location.search).toBe("?station=nightowl");
    // Its own public feed and status page, not the default station's.
    expect(document.getElementById("np-url").value).toMatch(/\/api\/stations\/nightowl\/nowplaying$/);
    expect(document.getElementById("np-status-open").getAttribute("href")).toBe("/stations/nightowl/status");
    await vi.waitFor(() => expect(said.join(" | ")).toContain(
      "Night Owl Radio. Playing Owl — Hoot. On air, 3 listeners."));
  });

  it("says a dropped link that comes inside the switch's quiet second (#327)", async () => {
    const app = await setupApp({
      initialState: { current_track: { path: "a.mp3", artist: "A", title: "B" } },
      stations: [AUTODJ, OWL],
      onRequest: owlRequests,
    });
    const sockets = trackSockets(app.WebSocketImpl);
    const pill = document.getElementById("conn-status");
    pill.textContent = "Off air";
    chooseOwl();
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    sockets[0].onopen();
    expect(pill.textContent).toBe("Live");
    expect(pill.getAttribute("aria-live")).toBe("off");
    const written = [];
    new window.MutationObserver(() => written.push([pill.textContent, pill.getAttribute("aria-live")]))
      .observe(pill, { childList: true, characterData: true, subtree: true });
    sockets[0].onclose({ code: 1006, wasClean: false });
    // On at once; the words go in once the browser has seen it on.
    expect(pill.getAttribute("aria-live")).toBe("polite");
    await vi.waitFor(() => expect(written).toEqual([["Disconnected, retrying", "polite"]]));
  });

  it("says a switch once: not the song line, and not Live when the socket opens (#327)", async () => {
    const app = await setupApp({
      initialState: { current_track: { path: "a.mp3", artist: "A", title: "B" } },
      stations: [AUTODJ, OWL],
      onRequest: owlRequests,
    });
    const sockets = trackSockets(app.WebSocketImpl);
    const said = spoken();
    const pill = document.getElementById("conn-status");
    const song = document.getElementById("track-announce");
    document.getElementById("announce-track-changes").value = "all";
    document.getElementById("announce-track-changes").dispatchEvent(new Event("change"));
    // The pill said something else before: the words "Live" would be news.
    pill.textContent = "Off air";
    const written = [];
    new window.MutationObserver(() => written.push([pill.textContent, pill.getAttribute("aria-live")]))
      .observe(pill, { childList: true, characterData: true, subtree: true });
    const songs = [];
    new window.MutationObserver(() => { if (song.textContent) songs.push(song.textContent); })
      .observe(song, { childList: true, characterData: true, subtree: true });

    chooseOwl();
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    await vi.waitFor(() => expect(said.join(" | ")).toContain("Night Owl Radio. Playing Owl — Hoot. On air, 3 listeners."));
    expect(document.getElementById("now-playing-title").textContent).toContain("Owl — Hoot");
    sockets[0].onopen();
    await vi.waitFor(() => expect(written).toEqual([["Live", "off"]]));
    expect(songs).toEqual([]);
    // Polite again afterwards: the next real transition is said.
    await vi.waitFor(() => expect(pill.getAttribute("aria-live")).toBe("polite"), { timeout: 2000 });
    // A song change on the new station is said as before.
    sockets[0].onmessage({ data: JSON.stringify({
      current_track: { path: "owl2.mp3", artist: "Owl", title: "Night" }, queue: [], eq: {}, volume: 1,
    }) });
    await vi.waitFor(() => expect(songs.join(" | ")).toContain("Owl — Night"));
  });

  it("opens the station named in the address", async () => {
    window.history.replaceState(null, "", "/?station=nightowl");
    const app = await setupApp({ stations: [AUTODJ, OWL], onRequest: owlRequests });
    expect(app.WebSocketImpl.mock.calls[0][0]).toMatch(/\/api\/stations\/nightowl\/ws$/);
    expect(document.getElementById("page-heading").textContent).toBe("AutoDJ: Night Owl Radio");
  });

  it("falls back to the default station for an unknown name, and says so", async () => {
    window.history.replaceState(null, "", "/?station=oldies");
    const app = await setupApp({ stations: [AUTODJ, OWL], onRequest: owlRequests });
    expect(app.WebSocketImpl.mock.calls[0][0]).toMatch(/\/ws$/);
    expect(app.WebSocketImpl.mock.calls[0][0]).not.toMatch(/stations/);
    expect(location.search).toBe("");
    expect(document.getElementById("page-heading").textContent).toBe("AutoDJ");
    expect(document.getElementById("sr-status").textContent)
      .toMatch(/No station is called oldies\. Showing AutoDJ\./);
  });

  it("says a stopped station is off air and offers to start it", async () => {
    const app = await setupApp({ stations: [AUTODJ, OWL], onRequest: owlRequests });
    const sockets = trackSockets(app.WebSocketImpl);
    chooseOwl();
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    vi.useFakeTimers();
    sockets[0].onclose({ code: 4409, wasClean: true });
    expect(document.getElementById("station-off-air").hidden).toBe(false);
    expect(document.getElementById("station-off-air-text").textContent).toBe("Night Owl Radio is off air.");
    expect(document.getElementById("station-start").textContent).toBe("Start Night Owl Radio");
    expect(document.getElementById("conn-status").textContent).toBe("Off air");
    // Not a dropped link: no reconnect storm.
    await vi.advanceTimersByTimeAsync(5000);
    expect(sockets).toHaveLength(1);
    vi.useRealTimers();

    document.getElementById("station-start").click();
    await vi.waitFor(() => expect(sockets).toHaveLength(2));
    expect(app.fetchImpl.mock.calls.map(([url, init]) => [url, init?.method]))
      .toContainEqual(["/api/stations/nightowl/start", "POST"]);
    expect(document.getElementById("station-off-air").hidden).toBe(true);
  });

  it("loads and edits saved playback settings while the selected station is stopped", async () => {
    const playback = { hour_fit: true, hour_fit_window_minutes: 12 };
    const onRequest = (url, options) => {
      if (url === "/api/stations/nightowl/settings") return jsonResponse({ playback });
      if (url === "/api/stations/nightowl/playback-settings") {
        Object.assign(playback, JSON.parse(options.body));
        return jsonResponse({ playback });
      }
      return owlRequests(url, options);
    };
    const app = await setupApp({
      stations: [AUTODJ, OWL],
      onRequest,
    });
    const sockets = trackSockets(app.WebSocketImpl);
    chooseOwl();
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    sockets[0].onclose({ code: 4409, wasClean: true });

    const skip = document.getElementById("btn-skip");
    const fit = document.getElementById("pb-hour-fit");
    const fitWindow = document.getElementById("pb-hour-fit-window");
    await vi.waitFor(() => expect(skip.disabled).toBe(true));
    expect(app.fetchImpl.mock.calls.map(([url]) => url))
      .toContain("/api/stations/nightowl/settings");
    await vi.waitFor(() => expect(fit.checked).toBe(true));
    expect(fitWindow.value).toBe("12");
    expect(fit.disabled).toBe(false);
    expect(fitWindow.disabled).toBe(false);

    fitWindow.value = "9";
    fitWindow.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(playback.hour_fit_window_minutes).toBe(9));
    expect(app.fetchImpl.mock.calls.map(([url]) => url))
      .toContain("/api/stations/nightowl/playback-settings");
    expect(fit.disabled).toBe(false);
  });

  it("asks before leaving browser decks that play, and stops them only on yes", async () => {
    const stopAllDecks = vi.fn();
    const app = await setupApp({
      audio: { _lastBrowserPlayback: true, playbackEnabled: true, stopAllDecks },
      stations: [AUTODJ, OWL],
      onRequest: owlRequests,
    });
    const sockets = trackSockets(app.WebSocketImpl);
    const confirm = document.getElementById("confirm-dialog");
    confirm.showModal = vi.fn(() => { confirm.open = true; });
    confirm.close = vi.fn((value) => {
      confirm.open = false;
      confirm.returnValue = value;
      confirm.dispatchEvent(new Event("close"));
    });

    chooseOwl();
    await vi.waitFor(() => expect(confirm.showModal).toHaveBeenCalled());
    expect(document.getElementById("confirm-title").textContent).toBe("Switch to Night Owl Radio?");
    document.getElementById("confirm-cancel").click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(stopAllDecks).not.toHaveBeenCalled();
    expect(sockets).toHaveLength(0);
    expect(document.getElementById("page-heading").textContent).toBe("AutoDJ");

    chooseOwl();
    await vi.waitFor(() => expect(confirm.showModal).toHaveBeenCalledTimes(2));
    document.getElementById("confirm-accept").click();
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    expect(stopAllDecks).toHaveBeenCalled();
  });

  it("asks once before a switch drops unsaved clockwheel work, and switches only on yes", async () => {
    const clockwheelPath = "../../src/autodj/static/modules/clockwheel-panel.js";
    const clockwheel = {
      refresh: vi.fn(async () => {}), clearProtectedData: vi.fn(), resume: vi.fn(), destroy: vi.fn(),
      hasUnsavedWork: vi.fn(() => true),
    };
    vi.doMock(clockwheelPath, async () => ({
      ...(await vi.importActual(clockwheelPath)),
      installClockwheelPanel: () => clockwheel,
    }));
    try {
      const stopAllDecks = vi.fn();
      const app = await setupApp({
        audio: { _lastBrowserPlayback: true, playbackEnabled: true, stopAllDecks },
        stations: [AUTODJ, OWL],
        onRequest: owlRequests,
      });
      const sockets = trackSockets(app.WebSocketImpl);
      const confirm = document.getElementById("confirm-dialog");
      confirm.showModal = vi.fn(() => { confirm.open = true; });
      confirm.close = vi.fn((value) => {
        confirm.open = false;
        confirm.returnValue = value;
        confirm.dispatchEvent(new Event("close"));
      });

      chooseOwl();
      await vi.waitFor(() => expect(confirm.showModal).toHaveBeenCalled());
      // One dialog names both losses: the decks that stop and the work dropped.
      expect(document.getElementById("confirm-title").textContent).toBe("Switch to Night Owl Radio?");
      expect(document.getElementById("confirm-message").textContent).toBe(
        "This page is playing AutoDJ itself. Switching stops that music on this page; the station goes on for everyone else. "
        + "It also discards the clockwheel changes not saved yet.");
      expect(document.getElementById("confirm-accept").textContent).toBe("Switch and discard");
      document.getElementById("confirm-cancel").click();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(sockets).toHaveLength(0);
      expect(clockwheel.clearProtectedData).not.toHaveBeenCalled();
      expect(stopAllDecks).not.toHaveBeenCalled();

      chooseOwl();
      await vi.waitFor(() => expect(confirm.showModal).toHaveBeenCalledTimes(2));
      document.getElementById("confirm-accept").click();
      await vi.waitFor(() => expect(sockets).toHaveLength(1));
      expect(confirm.showModal).toHaveBeenCalledTimes(2);
      expect(clockwheel.clearProtectedData).toHaveBeenCalled();
      expect(stopAllDecks).toHaveBeenCalled();
      expect(document.getElementById("page-heading").textContent).toBe("AutoDJ: Night Owl Radio");
    } finally {
      vi.doUnmock(clockwheelPath);
    }
  });

  it("asks about unsaved work alone when no decks play", async () => {
    const clockwheelPath = "../../src/autodj/static/modules/clockwheel-panel.js";
    vi.doMock(clockwheelPath, async () => ({
      ...(await vi.importActual(clockwheelPath)),
      installClockwheelPanel: () => ({
        refresh: async () => {}, clearProtectedData() {}, resume() {}, destroy() {},
        hasUnsavedWork: () => true,
      }),
    }));
    try {
      await setupApp({ stations: [AUTODJ, OWL], onRequest: owlRequests });
      const confirm = document.getElementById("confirm-dialog");
      confirm.showModal = vi.fn(() => { confirm.open = true; });
      chooseOwl();
      await vi.waitFor(() => expect(confirm.showModal).toHaveBeenCalled());
      expect(document.getElementById("confirm-message").textContent)
        .toBe("Switching discards the clockwheel changes not saved yet.");
    } finally {
      vi.doUnmock(clockwheelPath);
    }
  });

  it("names a voice track not booked, with the clockwheel changes, in the one question", async () => {
    const clockwheelPath = "../../src/autodj/static/modules/clockwheel-panel.js";
    const voicePath = "../../src/autodj/static/modules/voice-tracker.js";
    vi.doMock(clockwheelPath, async () => ({
      ...(await vi.importActual(clockwheelPath)),
      installClockwheelPanel: () => ({
        refresh: async () => {}, clearProtectedData() {}, resume() {}, destroy() {},
        hasUnsavedWork: () => true,
      }),
    }));
    const voice = { refresh: async () => {}, toggle() {}, clear: vi.fn(), hasUnsavedWork: () => true };
    vi.doMock(voicePath, async () => ({
      ...(await vi.importActual(voicePath)),
      installVoiceTracker: () => voice,
    }));
    try {
      await setupApp({ stations: [AUTODJ, OWL], onRequest: owlRequests });
      const confirm = document.getElementById("confirm-dialog");
      confirm.showModal = vi.fn(() => { confirm.open = true; });
      chooseOwl();
      await vi.waitFor(() => expect(confirm.showModal).toHaveBeenCalledOnce());
      expect(document.getElementById("confirm-message").textContent).toBe(
        "Switching discards the clockwheel changes not saved yet and the voice track not booked yet.");
      expect(voice.clear).not.toHaveBeenCalled();
    } finally {
      vi.doUnmock(clockwheelPath);
      vi.doUnmock(voicePath);
    }
  });

  it("leaves focus on the Station button after the question, answered either way", async () => {
    const clockwheelPath = "../../src/autodj/static/modules/clockwheel-panel.js";
    vi.doMock(clockwheelPath, async () => ({
      ...(await vi.importActual(clockwheelPath)),
      installClockwheelPanel: () => ({
        refresh: async () => {}, clearProtectedData() {}, resume() {}, destroy() {},
        hasUnsavedWork: () => true,
      }),
    }));
    try {
      const app = await setupApp({ stations: [AUTODJ, OWL], onRequest: owlRequests });
      const sockets = trackSockets(app.WebSocketImpl);
      // As a browser does: a modal dialog gives focus back to what had it.
      const confirm = document.getElementById("confirm-dialog");
      let opener = null;
      confirm.showModal = vi.fn(() => {
        opener = document.activeElement;
        confirm.open = true;
      });
      confirm.close = vi.fn((value) => {
        confirm.open = false;
        confirm.returnValue = value;
        opener?.focus();
        confirm.dispatchEvent(new Event("close"));
      });
      const button = document.getElementById("station-button");

      chooseOwl();
      await vi.waitFor(() => expect(confirm.showModal).toHaveBeenCalledOnce());
      expect(opener).toBe(button);
      expect(document.activeElement).toBe(document.getElementById("confirm-cancel"));
      document.getElementById("confirm-cancel").click();
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(document.activeElement).toBe(button);

      chooseOwl();
      await vi.waitFor(() => expect(confirm.showModal).toHaveBeenCalledTimes(2));
      document.getElementById("confirm-accept").click();
      await vi.waitFor(() => expect(sockets).toHaveLength(1));
      await vi.waitFor(() => expect(document.getElementById("page-heading").textContent)
        .toBe("AutoDJ: Night Owl Radio"));
      expect(document.activeElement).toBe(button);
    } finally {
      vi.doUnmock(clockwheelPath);
    }
  });

  it("says a station off air when its state answers 409 on a switch", async () => {
    const app = await setupApp({
      stations: [AUTODJ, OWL],
      onRequest: (url) => (url === "/api/stations/nightowl/status"
        ? jsonResponse({ detail: "Night Owl Radio is not running." }, 409)
        : owlRequests(url)),
    });
    const sockets = trackSockets(app.WebSocketImpl);
    chooseOwl();
    await vi.waitFor(() => expect(document.getElementById("station-off-air").hidden).toBe(false));
    expect(document.getElementById("station-off-air-text").textContent).toBe("Night Owl Radio is off air.");
    expect(document.getElementById("conn-status").textContent).toBe("Off air");
    expect(sockets).toHaveLength(0);
  });

  it("is not off air when the new station's state fails with anything but a 409", async () => {
    const app = await setupApp({
      stations: [AUTODJ, OWL],
      onRequest: (url) => (url === "/api/stations/nightowl/status"
        ? jsonResponse({ detail: "The AutoDJ server hit an error." }, 500)
        : owlRequests(url)),
    });
    const sockets = trackSockets(app.WebSocketImpl);
    const said = spoken();
    chooseOwl();
    // The socket brings the state when it opens.
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    expect(sockets[0].url).toMatch(/\/api\/stations\/nightowl\/ws$/);
    expect(document.getElementById("station-off-air").hidden).toBe(true);
    expect(document.getElementById("conn-status").textContent).not.toBe("Off air");
    await vi.waitFor(() => expect(said.join(" | ")).toContain(
      "Now controlling Night Owl Radio. Its state could not be loaded: The AutoDJ server hit an error."));
  });

  it("says why a station cannot be chosen while the link is down", async () => {
    const app = await setupApp({ stations: [AUTODJ, OWL], onRequest: owlRequests });
    const sockets = trackSockets(app.WebSocketImpl);
    app.webSocket.onclose({ code: 1006, wasClean: false });
    chooseOwl();
    await vi.waitFor(() => expect(document.getElementById("sr-status").textContent)
      .toBe("AutoDJ is reconnecting. Choose the station again once it is back."));
    expect(sockets).toHaveLength(0);
    expect(document.getElementById("page-heading").textContent).toBe("AutoDJ");
    expect(document.activeElement.id).toBe("station-button");
  });

  it("keeps Listen here on the station it started on, and says so", async () => {
    const app = await setupApp({
      initialState: { stream_mode: true, stream_state: "playing" },
      stations: [AUTODJ, OWL],
      onRequest: (url) => (url === "/api/stream" || url === "/api/stations/default/stream"
        ? jsonResponse({ path: "/stream.mp3" })
        : owlRequests(url)),
    });
    const sockets = trackSockets(app.WebSocketImpl);
    const audio = document.getElementById("stream-audio");
    audio.play = vi.fn().mockResolvedValue();
    document.getElementById("btn-listen").click();
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalled());
    expect(audio.src).toMatch(/\/stream\.mp3\?connection=/);

    chooseOwl();
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    expect(audio.src).toMatch(/\/stream\.mp3\?connection=/);
    expect(document.getElementById("btn-listen").getAttribute("aria-pressed")).toBe("true");
    const note = document.getElementById("listen-station");
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe("Playing AutoDJ");

    // A dropped stream reconnects to its own station, not the one shown.
    audio.dispatchEvent(new Event("playing"));
    audio.dispatchEvent(new Event("error"));
    await vi.waitFor(() => expect(app.fetchImpl.mock.calls.map(([url]) => url))
      .toContain("/api/stations/default/stream"));
    await vi.waitFor(() => expect(audio.play).toHaveBeenCalledTimes(2));
    expect(audio.src).toMatch(/\/stream\.mp3\?connection=/);
  });
});

describe("several stations: the first station (#70 review)", () => {
  const OWL = { slug: "nightowl", name: "Night Owl Radio", state: "playing", listeners: 0, title: null };
  const AUTODJ = { slug: "default", name: "AutoDJ", state: "playing", listeners: 0, title: null };
  const owlStatus = (url) => (url === "/api/stations/nightowl/status"
    ? jsonResponse({ stream_mode: true, stream_state: "idle" })
    : jsonResponse({ detail: "not in this test" }, 404));

  function memoryStorage(initial = {}) {
    const values = new Map(Object.entries(initial));
    return {
      getItem: vi.fn((key) => (values.has(key) ? values.get(key) : null)),
      setItem: vi.fn((key, value) => values.set(key, String(value))),
      removeItem: vi.fn((key) => values.delete(key)),
      values,
    };
  }

  afterEach(() => {
    window.history.replaceState(null, "", "/");
  });

  it("keeps ?station= when the station list cannot be loaded, and says nothing about it", async () => {
    window.history.replaceState(null, "", "/?station=nightowl");
    const app = await setupApp({ stations: null, onRequest: owlStatus });
    expect(app.WebSocketImpl.mock.calls[0][0]).toMatch(/\/api\/stations\/nightowl\/ws$/);
    expect(location.search).toBe("?station=nightowl");
    expect(document.getElementById("sr-status").textContent).not.toMatch(/No station is called/);
  });

  it("opens the last station chosen in this browser when the address names none", async () => {
    vi.stubGlobal("localStorage", memoryStorage({ "autodj.station": "nightowl" }));
    const app = await setupApp({ stations: [AUTODJ, OWL], onRequest: owlStatus });
    expect(app.WebSocketImpl.mock.calls[0][0]).toMatch(/\/api\/stations\/nightowl\/ws$/);
    expect(location.search).toBe("?station=nightowl");
  });

  it("lets the address win over the remembered station", async () => {
    vi.stubGlobal("localStorage", memoryStorage({ "autodj.station": "nightowl" }));
    window.history.replaceState(null, "", "/?station=default");
    const app = await setupApp({ stations: [AUTODJ, OWL], onRequest: owlStatus });
    expect(app.WebSocketImpl.mock.calls[0][0]).not.toMatch(/stations/);
  });

  it("forgets a remembered station that is no longer configured, without a word", async () => {
    const storage = memoryStorage({ "autodj.station": "oldies" });
    vi.stubGlobal("localStorage", storage);
    const app = await setupApp({ stations: [AUTODJ, OWL], onRequest: owlStatus });
    expect(app.WebSocketImpl.mock.calls[0][0]).not.toMatch(/stations/);
    expect(storage.values.has("autodj.station")).toBe(false);
    expect(document.getElementById("sr-status").textContent).not.toMatch(/No station is called/);
  });

  it("remembers the station a switch chooses", async () => {
    const storage = memoryStorage();
    vi.stubGlobal("localStorage", storage);
    await setupApp({ stations: [AUTODJ, OWL], onRequest: owlStatus });
    const button = document.getElementById("station-button");
    button.focus();
    button.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await vi.waitFor(() => expect(storage.values.get("autodj.station")).toBe("nightowl"));
  });
});
