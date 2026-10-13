import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  CONNECTING_TEXT,
  ENDED_TEXT,
  PLAYING_TEXT,
  PLAY_FAILED_TEXT,
  STOPPED_TEXT,
  VOLUME_KEY,
  historyRenderer,
  startPlayer,
  startPlayerPage,
  streamUrl,
} from "../../src/autodj/static/modules/player-page.js";

// The markup player_page.py renders (tests/unit/test_player_page.py checks
// that side).
function mount(page = "player") {
  document.body.dataset.page = page;
  document.body.dataset.feed = "/api/stations/nightowl/nowplaying";
  document.body.dataset.stream = "/stations/nightowl.mp3";
  document.body.innerHTML = `
    <h1 id="status-station">Night Owl Radio</h1>
    <audio id="player-audio" controls preload="none" src="/stations/nightowl.mp3"></audio>
    <div id="player-controls" class="player-controls" hidden>
      <button type="button" id="player-toggle">Play</button>
      <label for="player-volume">Volume</label>
      <input type="range" id="player-volume" min="0" max="100" step="5" value="100" aria-valuetext="100%">
      <span id="player-volume-value" aria-hidden="true">100%</span>
    </div>
    <p id="player-state" role="status"></p>
    <p id="status-now" aria-live="polite" aria-atomic="true">Song by Artist.</p>
    <ol id="player-history" hidden></ol>
    <p id="player-history-empty">No songs have finished playing yet.</p>`;
}

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: vi.fn((key, value) => values.set(key, String(value))),
    values,
  };
}

const byId = (id) => document.getElementById(id);
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("player page stream address", () => {
  const named = (stream) => {
    document.body.innerHTML = "";
    if (stream === undefined) delete document.body.dataset.stream;
    else document.body.dataset.stream = stream;
    return streamUrl(document);
  };

  it("plays only an .mp3 path on this site", () => {
    expect(named("/stations/nightowl.mp3")).toBe("/stations/nightowl.mp3");
    expect(named("/stream.mp3")).toBe("/stream.mp3");
    for (const stream of [
      undefined,
      "",
      "//evil.example/stream.mp3",
      "/\\evil.example/stream.mp3",
      "/%5Cevil.example/stream.mp3",
      "https://evil.example/stream.mp3",
      "javascript:alert(1)",
      "stream.mp3",
      "/api/nowplaying",
    ]) {
      expect(named(stream), String(stream)).toBeNull();
    }
  });
});

describe("player controls", () => {
  let audio;
  let storage;
  let player;

  const start = () => {
    player = startPlayer({ doc: document, storage });
    return player;
  };

  beforeEach(() => {
    mount();
    audio = byId("player-audio");
    audio.play = vi.fn(() => Promise.resolve());
    audio.pause = vi.fn(() => audio.dispatchEvent(new Event("pause")));
    audio.load = vi.fn();
    storage = memoryStorage();
  });
  afterEach(() => {
    delete document.body.dataset.page;
    delete document.body.dataset.stream;
  });

  it("does nothing without the markup or a stream", () => {
    document.body.innerHTML = "";
    expect(startPlayer({ doc: document, storage })).toBeNull();
    mount();
    delete document.body.dataset.stream;
    expect(startPlayer({ doc: document, storage })).toBeNull();
    expect(byId("player-audio").hasAttribute("controls")).toBe(true);
  });

  it("swaps the browser's controls for its own and plays nothing on its own", () => {
    start();
    expect(audio.hasAttribute("controls")).toBe(false);
    expect(audio.hasAttribute("src")).toBe(false);
    expect(byId("player-controls").hidden).toBe(false);
    expect(audio.load).toHaveBeenCalledTimes(1);
    expect(audio.play).not.toHaveBeenCalled();
    expect(byId("player-toggle").textContent).toBe("Play");
    expect(byId("player-state").textContent).toBe("");
  });

  it("says connecting, then playing, with the button naming what it will do", async () => {
    start();
    const toggle = byId("player-toggle");
    toggle.focus();
    toggle.click();
    expect(audio.play).toHaveBeenCalledTimes(1);
    expect(audio.getAttribute("src")).toBe("/stations/nightowl.mp3");
    expect(toggle.textContent).toBe("Stop");
    expect(toggle.hasAttribute("aria-pressed")).toBe(false);
    expect(byId("player-state").textContent).toBe(CONNECTING_TEXT);
    audio.dispatchEvent(new Event("playing"));
    expect(byId("player-state").textContent).toBe(PLAYING_TEXT);
    expect(player.playing).toBe(true);
    expect(document.activeElement).toBe(toggle);
  });

  it("drops the connection when stopped, so no listener slot is held", () => {
    start();
    const toggle = byId("player-toggle");
    toggle.click();
    audio.dispatchEvent(new Event("playing"));
    toggle.click();
    expect(audio.pause).toHaveBeenCalled();
    expect(audio.hasAttribute("src")).toBe(false);
    expect(audio.load).toHaveBeenCalled();
    expect(toggle.textContent).toBe("Play");
    expect(byId("player-state").textContent).toBe(STOPPED_TEXT);
    expect(player.playing).toBe(false);
    // Play again joins the live stream afresh.
    toggle.click();
    expect(audio.getAttribute("src")).toBe("/stations/nightowl.mp3");
    expect(byId("player-state").textContent).toBe(CONNECTING_TEXT);
  });

  it("says why it could not play, once, and offers Play again", async () => {
    start();
    byId("player-toggle").click();
    audio.dispatchEvent(new Event("error"));
    expect(byId("player-state").textContent).toBe(PLAY_FAILED_TEXT);
    expect(byId("player-toggle").textContent).toBe("Play");
    // A late error after stopping says nothing more.
    const write = vi.spyOn(byId("player-state"), "textContent", "set");
    audio.dispatchEvent(new Event("error"));
    expect(write).not.toHaveBeenCalled();
  });

  it("reports a refused play() but not one cut short by Stop", async () => {
    audio.play = vi.fn(() => Promise.reject(Object.assign(new Error("no"), { name: "NotAllowedError" })));
    start();
    byId("player-toggle").click();
    await settle();
    expect(byId("player-state").textContent).toBe(PLAY_FAILED_TEXT);
    audio.play = vi.fn(() => Promise.reject(Object.assign(new Error("cut"), { name: "AbortError" })));
    byId("player-toggle").click();
    await settle();
    expect(byId("player-state").textContent).toBe(CONNECTING_TEXT);
    audio.play = vi.fn(() => { throw new Error("broken"); });
    byId("player-toggle").click(); // stop
    byId("player-toggle").click(); // play, which throws
    expect(byId("player-state").textContent).toBe(PLAY_FAILED_TEXT);
  });

  it("stops when paused from outside the page, and says when the stream ended", () => {
    start();
    byId("player-toggle").click();
    audio.dispatchEvent(new Event("pause"));
    expect(byId("player-state").textContent).toBe(STOPPED_TEXT);
    expect(byId("player-toggle").textContent).toBe("Play");
    byId("player-toggle").click();
    Object.defineProperty(audio, "ended", { configurable: true, get: () => true });
    audio.dispatchEvent(new Event("pause"));
    expect(byId("player-state").textContent).toBe(ENDED_TEXT);
  });

  it("sets the volume as a percentage and remembers it in this browser", () => {
    storage = memoryStorage({ [VOLUME_KEY]: "40" });
    start();
    const volume = byId("player-volume");
    expect(audio.volume).toBeCloseTo(0.4);
    expect(volume.value).toBe("40");
    expect(volume.getAttribute("aria-valuetext")).toBe("40%");
    expect(byId("player-volume-value").textContent).toBe("40%");
    volume.value = "75";
    volume.dispatchEvent(new Event("input"));
    expect(audio.volume).toBeCloseTo(0.75);
    expect(volume.getAttribute("aria-valuetext")).toBe("75%");
    expect(storage.values.get(VOLUME_KEY)).toBe("75");
    // The slider speaks for itself; nothing else is announced.
    expect(byId("player-state").textContent).toBe("");
  });

  it("keeps working when storage is blocked or holds nonsense", () => {
    const blocked = {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
    };
    player = startPlayer({ doc: document, storage: blocked });
    expect(byId("player-volume").value).toBe("100");
    const volume = byId("player-volume");
    volume.value = "20";
    expect(() => volume.dispatchEvent(new Event("input"))).not.toThrow();
    expect(audio.volume).toBeCloseTo(0.2);
    mount();
    audio = byId("player-audio");
    startPlayer({ doc: document, storage: memoryStorage({ [VOLUME_KEY]: "loud" }) });
    expect(byId("player-volume").value).toBe("100");
    mount();
    startPlayer({ doc: document, storage: memoryStorage({ [VOLUME_KEY]: "250" }) });
    expect(byId("player-volume").value).toBe("100");
    expect(byId("player-volume").getAttribute("aria-valuetext")).toBe("100%");
  });

  it("adds no live region of its own", () => {
    start();
    expect(document.querySelectorAll("[aria-live]")).toHaveLength(1);
    expect(document.querySelectorAll('[role="status"]')).toHaveLength(1);
  });
});

describe("recent songs", () => {
  beforeEach(() => mount());

  it("lists the feed's history in words and rebuilds it only when it changes", () => {
    const render = historyRenderer(document);
    const history = [
      { artist: "A", title: "One", played_at: "1" },
      { artist: "", title: "", played_at: "2" },
    ];
    render({ history });
    const list = byId("player-history");
    expect([...list.children].map((item) => item.textContent)).toEqual([
      "One by A.",
      "Unknown title by an unknown artist.",
    ]);
    expect(list.hidden).toBe(false);
    expect(byId("player-history-empty").hidden).toBe(true);
    const replace = vi.spyOn(list, "replaceChildren");
    render({ history: [...history] });
    expect(replace).not.toHaveBeenCalled();
    render({ history: [] });
    expect(list.hidden).toBe(true);
    expect(byId("player-history-empty").hidden).toBe(false);
    render({ history: "nonsense" });
    expect(list.children).toHaveLength(0);
  });

  it("does nothing on a page without the list", () => {
    document.body.innerHTML = "";
    expect(() => historyRenderer(document)({ history: [] })).not.toThrow();
  });
});

describe("starting the page", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    delete document.body.dataset.page;
  });

  it("starts only on a player page", () => {
    mount("status");
    expect(startPlayerPage(document)).toBeNull();
  });

  it("starts the controls and the feed loop on the player and the embed", async () => {
    for (const page of ["player", "embed"]) {
      mount(page);
      const fetch = vi.fn(async () => new globalThis.Response(JSON.stringify({
        station_name: "Night Owl Radio",
        current_track: { artist: "A", title: "B" },
        history: [{ artist: "C", title: "D", played_at: "x" }],
      }), { headers: { "Content-Type": "application/json" } }));
      vi.stubGlobal("fetch", fetch);
      const started = startPlayerPage(document);
      expect(started.player).not.toBeNull();
      await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
      expect(fetch.mock.calls[0][0]).toBe("/api/stations/nightowl/nowplaying");
      await vi.waitFor(() => expect(byId("status-now").textContent).toBe("B by A."));
      expect(document.title).toBe(
        page === "player" ? "Night Owl Radio: listen live" : "Night Owl Radio player",
      );
      if (page === "player") expect(byId("player-history").textContent).toBe("D by C.");
      started.feed.stop();
    }
  });
});
