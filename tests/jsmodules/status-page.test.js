import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  NOTHING_PLAYING,
  OFF_AIR,
  PAUSED_TEXT,
  UNREACHABLE_TEXT,
  UPDATING_TEXT,
  feedUrl,
  listenersText,
  offAirText,
  startStatusPage,
  trackText,
  uptimeText,
} from "../../src/autodj/static/modules/status-page.js";

// The ids status_page.py renders (tests/unit/test_status_page.py checks
// that side), with the values a fresh page would carry.
function mount() {
  document.title = "Radio: station status";
  document.body.innerHTML = `
    <header><h1 id="status-station">Radio</h1></header>
    <main>
      <h2>Now playing</h2>
      <p id="status-now" aria-live="polite" aria-atomic="true">Song by Artist, from the album Album.</p>
      <section id="status-next-section" hidden><h2>Up next</h2><p id="status-next"></p></section>
      <dl>
        <dt>Listening now</dt><dd id="status-listeners">1 listener</dd>
        <dt>Peak today</dt><dd id="status-peak">2 listeners</dd>
        <dt>Uptime</dt><dd id="status-uptime">5 minutes</dd>
      </dl>
    </main>
    <footer><p id="status-updated">Reload the page for the latest.</p>
      <p><button type="button" id="status-pause" aria-pressed="false" hidden>Pause updates</button></p></footer>`;
}

const CURRENT = {
  artist: "Artist", title: "Song", album: "Album", started_at: "2026-10-11T12:00:00+00:00",
};
const FEED = {
  schema: 2,
  station_name: "Radio",
  current_track: CURRENT,
  next_track: null,
  listener_count: 1,
  peak_listeners_today: 2,
  uptime_seconds: 300,
};

describe("status page text", () => {
  it("says tracks, counts and uptime in words, like status_page.py", () => {
    expect(trackText(CURRENT, "")).toBe("Song by Artist, from the album Album.");
    expect(trackText({ title: " ", artist: "" }, "")).toBe("Unknown title by an unknown artist.");
    expect(trackText(null, NOTHING_PLAYING)).toBe(NOTHING_PLAYING);
    // A live DJ's set (#48): the DJ, with no title, album or length to read.
    const live = {
      artist: "DJ Alice", title: "Live", album: "", duration: 0,
      started_at: "2026-10-13T20:15:00Z", elapsed: 120, kind: "live",
    };
    expect(trackText(live, "")).toBe("Live: DJ Alice.");
    expect(trackText({ ...live, artist: " " }, "")).toBe("Live: a DJ.");
    expect(listenersText(1)).toBe("1 listener");
    expect(listenersText(0)).toBe("0 listeners");
    expect(listenersText("bad")).toBe("0 listeners");
    expect(uptimeText(59)).toBe("less than a minute");
    expect(uptimeText(60)).toBe("1 minute");
    expect(uptimeText(2 * 86400 + 3 * 3600 + 60)).toBe("2 days, 3 hours, 1 minute");
    expect(uptimeText(3600)).toBe("1 hour");
    expect(uptimeText(-5)).toBe("less than a minute");
  });
});

describe("status page polling", () => {
  let load;
  let timers;
  let page;

  const start = () => {
    page = startStatusPage({
      doc: document,
      load,
      setTimer: (callback, ms) => { timers.push({ callback, ms }); return timers.length; },
      clearTimer: vi.fn(),
    });
    return page;
  };
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
  const byId = (id) => document.getElementById(id);

  beforeEach(() => {
    mount();
    timers = [];
    load = vi.fn(async () => FEED);
  });
  afterEach(() => page?.stop());

  it("does nothing on a page without the status markup", () => {
    document.body.innerHTML = "";
    expect(startStatusPage({ doc: document, load })).toBeNull();
    expect(load).not.toHaveBeenCalled();
  });

  it("leaves the live line alone while the song is the same", async () => {
    const now = byId("status-now");
    const write = vi.spyOn(now, "textContent", "set");
    start();
    await settle();
    expect(load).toHaveBeenCalledTimes(1);
    expect(write).not.toHaveBeenCalled();
    expect(byId("status-updated").textContent).toBe(UPDATING_TEXT);
    expect(timers.at(-1).ms).toBe(15000);
    await page.poll();
    expect(write).not.toHaveBeenCalled();
  });

  it("rewrites the live line once when the song changes", async () => {
    start();
    await settle();
    const now = byId("status-now");
    const write = vi.spyOn(now, "textContent", "set");
    load.mockResolvedValue({
      ...FEED,
      current_track: { artist: "B", title: "Next", album: "", started_at: "later" },
      listener_count: 3,
      peak_listeners_today: 3,
      uptime_seconds: 3700,
    });
    await page.poll();
    await page.poll();
    expect(write).toHaveBeenCalledTimes(1);
    expect(now.textContent).toBe("Next by B.");
    expect(byId("status-listeners").textContent).toBe("3 listeners");
    expect(byId("status-peak").textContent).toBe("3 listeners");
    expect(byId("status-uptime").textContent).toBe("1 hour, 1 minute");
    // Only the now-playing line speaks.
    expect(document.querySelectorAll("[aria-live]")).toHaveLength(1);
  });

  it("says when nothing is playing", async () => {
    load.mockResolvedValue({ ...FEED, current_track: null });
    start();
    await settle();
    expect(byId("status-now").textContent).toBe(NOTHING_PLAYING);
  });

  it("shows the next song only while the feed carries one", async () => {
    load.mockResolvedValue({ ...FEED, next_track: { artist: "C", title: "Soon", album: "D" } });
    start();
    await settle();
    expect(byId("status-next-section").hidden).toBe(false);
    expect(byId("status-next").textContent).toBe("Soon by C, from the album D.");
    load.mockResolvedValue(FEED);
    await page.poll();
    expect(byId("status-next-section").hidden).toBe(true);
  });

  it("follows a renamed station in the heading and the title", async () => {
    load.mockResolvedValue({ ...FEED, station_name: "New name" });
    start();
    await settle();
    expect(byId("status-station").textContent).toBe("New name");
    expect(document.title).toBe("New name: station status");
    load.mockResolvedValue({ ...FEED, station_name: "" });
    await page.poll();
    expect(byId("status-station").textContent).toBe("AutoDJ");
  });

  it("gives a player page its own title and each live feed", async () => {
    const onFeed = vi.fn();
    page = startStatusPage({
      doc: document,
      load,
      setTimer: () => 1,
      clearTimer: vi.fn(),
      title: (station) => `${station}: listen live`,
      onFeed,
    });
    await settle();
    expect(document.title).toBe("Radio: listen live");
    expect(onFeed).toHaveBeenCalledWith(FEED);
    load.mockRejectedValue(Object.assign(new Error("off air"), { status: 503 }));
    await page.poll();
    expect(onFeed).toHaveBeenCalledTimes(1);
  });

  it("says in plain text when the station cannot be reached, and keeps polling", async () => {
    load.mockRejectedValue(new Error("offline"));
    start();
    await settle();
    expect(byId("status-updated").textContent).toBe(UNREACHABLE_TEXT);
    expect(byId("status-updated").hasAttribute("aria-live")).toBe(false);
    expect(timers).toHaveLength(1);
    load.mockResolvedValue(FEED);
    await page.poll();
    expect(byId("status-updated").textContent).toBe(UPDATING_TEXT);
  });

  it("skips polls while the tab is hidden and catches up when shown", async () => {
    let hidden = true;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    try {
      start();
      await settle();
      expect(load).not.toHaveBeenCalled();
      hidden = false;
      document.dispatchEvent(new Event("visibilitychange"));
      await settle();
      expect(load).toHaveBeenCalledTimes(1);
      hidden = true;
      document.dispatchEvent(new Event("visibilitychange"));
      await settle();
      expect(load).toHaveBeenCalledTimes(1);
    } finally {
      delete document.hidden;
    }
  });

  it("pauses and resumes updates with a fixed-name toggle", async () => {
    start();
    await settle();
    const pause = byId("status-pause");
    expect(pause.hidden).toBe(false);
    pause.click();
    expect(pause.getAttribute("aria-pressed")).toBe("true");
    expect(pause.textContent).toBe("Pause updates");
    expect(byId("status-updated").textContent).toBe(PAUSED_TEXT);
    await page.poll();
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    expect(load).toHaveBeenCalledTimes(1);
    pause.click();
    expect(pause.getAttribute("aria-pressed")).toBe("false");
    await settle();
    expect(load).toHaveBeenCalledTimes(2);
    expect(byId("status-updated").textContent).toBe(UPDATING_TEXT);
  });

  it("lets a poll that was overtaken or paused mid-request change nothing", async () => {
    let answer;
    load.mockImplementation(() => new Promise((resolve) => { answer = resolve; }));
    start();
    byId("status-pause").click();
    answer({ ...FEED, listener_count: 9 });
    await settle();
    expect(byId("status-listeners").textContent).toBe("1 listener");
    expect(byId("status-updated").textContent).toBe(PAUSED_TEXT);
  });

  it("says once that a stopped station is off air, then the song when it is back", async () => {
    const offAir = Object.assign(new Error("That station is off air"), { status: 503 });
    load.mockRejectedValue(offAir);
    start();
    await settle();
    const now = byId("status-now");
    expect(now.textContent).toBe(offAirText("Radio"));
    expect(byId("status-listeners").textContent).toBe("0 listeners");
    expect(byId("status-uptime").textContent).toBe(OFF_AIR);
    // Off air is an answer, not a failure to reach the station.
    expect(byId("status-updated").textContent).toBe(UPDATING_TEXT);
    const write = vi.spyOn(now, "textContent", "set");
    await page.poll();
    expect(write).not.toHaveBeenCalled();
    load.mockResolvedValue(FEED);
    await page.poll();
    expect(now.textContent).toBe("Song by Artist, from the album Album.");
    expect(byId("status-uptime").textContent).toBe("5 minutes");
  });

  it("stops polling when stopped", async () => {
    start();
    await settle();
    page.stop();
    await page.poll();
    expect(load).toHaveBeenCalledTimes(1);
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    expect(load).toHaveBeenCalledTimes(1);
  });
});

describe("status page feed address", () => {
  const named = (feed) => {
    document.body.innerHTML = "";
    if (feed === undefined) delete document.body.dataset.feed;
    else document.body.dataset.feed = feed;
    return feedUrl(document);
  };

  it("polls the station feed the page names", () => {
    expect(named("/api/stations/nightowl/nowplaying")).toBe("/api/stations/nightowl/nowplaying");
    expect(named("/api/nowplaying")).toBe("/api/nowplaying");
    expect(named(undefined)).toBe("/api/nowplaying");
    expect(named("")).toBe("/api/nowplaying");
  });

  it("refuses any address that could reach another host", () => {
    for (const feed of [
      "//evil.example/feed",
      "//evil",
      "/\\evil.example/feed",
      "/\\/evil.example/feed",
      "\\\\evil.example/feed",
      "/api/\\..\\..\\evil",
      "/%5Cevil.example/feed",
      "/%5c%5cevil.example/feed",
      "/api/%5Cfeed",
      "https://evil.example/api/feed",
      "javascript:alert(1)",
      "evil.example/api/feed",
      "/\t/evil.example/api/feed",
      "/status",
    ]) {
      expect(named(feed), feed).toBe("/api/nowplaying");
    }
  });
});
