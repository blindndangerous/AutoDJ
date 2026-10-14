import { readFileSync } from "node:fs";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  dayText,
  durationText,
  hoursText,
  installListenerStats,
  spanText,
  summaryText,
} from "../../src/autodj/static/modules/listener-stats.js";

const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
const section = html.slice(
  html.indexOf('<section id="listener-stats"'),
  html.indexOf("</section>", html.indexOf('<section id="listener-stats"')) + "</section>".length,
);
const tick = () => new Promise((resolve) => setTimeout(resolve, 10));

const BANDS = ["Under 1 minute", "1 to 5 minutes", "5 to 15 minutes", "15 minutes to 1 hour",
  "1 to 3 hours", "3 hours or more"];

function stats(overrides = {}) {
  return {
    keep_days: 90,
    days_shown: 30,
    today: "2026-10-13",
    now: { total: 3, formats: { mp3: 2, aac: 1, opus: 0 } },
    days: [
      {
        date: "2026-10-13", peak: 4, peak_at: "14:05", sessions: 6, seconds: 7200, longest: 3900,
        bands: [1, 2, 0, 2, 1, 0], formats: { mp3: { peak: 3, sessions: 5, seconds: 6000 } },
      },
      {
        date: "2026-10-12", peak: 0, peak_at: null, sessions: 0, seconds: 0, longest: 0,
        bands: [0, 0, 0, 0, 0, 0], formats: {},
      },
    ],
    period: {
      sessions: 6, seconds: 7200, longest: 3900, peak: 4, peak_date: "2026-10-13",
      peak_at: "14:05", bands: [1, 2, 0, 2, 1, 0],
      formats: { mp3: { peak: 3, sessions: 5, seconds: 6000 }, aac: { peak: 1, sessions: 1, seconds: 1200 } },
    },
    bands: BANDS,
    ...overrides,
  };
}

function setup({ request, download, save, saveKeepDays } = {}) {
  document.body.innerHTML = `<div id="settings-status" role="status"></div>
    <div id="status-toast" hidden></div><span id="station-context">Default station</span>${section}`;
  const deps = {
    request: request || vi.fn(async () => stats()),
    download: download || vi.fn(async () => ({
      blob: async () => "date\n",
      headers: { get: () => 'attachment; filename="autodj-listeners-default.csv"' },
    })),
    save: save || vi.fn(),
    saveKeepDays: saveKeepDays || vi.fn(async () => true),
  };
  const view = installListenerStats({
    doc: document, ...deps, settingsStatus: document.getElementById("settings-status"), locale: "en-GB",
  });
  return { view, ...deps };
}

const status = () => document.getElementById("settings-status").textContent;
const cells = (table) => [...table.querySelectorAll("tbody tr")]
  .map((row) => [...row.querySelectorAll("th, td")].map((cell) => cell.textContent));

describe("listener statistics text", () => {
  it("writes lengths, hours and days in words", () => {
    expect(durationText(0)).toBe("0 seconds");
    expect(durationText(1)).toBe("1 second");
    expect(durationText(59)).toBe("59 seconds");
    expect(durationText(60)).toBe("1 minute");
    expect(durationText(45 * 60)).toBe("45 minutes");
    expect(durationText(3600)).toBe("1 hour");
    expect(durationText(3900)).toBe("1 hour 5 minutes");
    expect(durationText(2 * 3600 + 60)).toBe("2 hours 1 minute");
    expect(hoursText(7200)).toBe("2.0");
    expect(hoursText(15120)).toBe("4.2");
    expect(dayText("2026-10-13", "en-GB")).toBe("Tue 13 Oct");
    expect(spanText(7)).toBe("the last 7 days");
    expect(spanText(3650)).toBe("every day kept");
  });

  it("sums up now, today and the period in one paragraph", () => {
    expect(summaryText(stats(), "en-GB")).toBe(
      "Listening now: 3 (MP3 2, AAC 1). Today: peak 4 at 14:05, 6 sessions, 2.0 listening hours."
      + " In the last 30 days: 6 sessions, 2.0 listening hours, peak 4 on Tue 13 Oct at 14:05,"
      + " longest session 1 hour 5 minutes.",
    );
    const quiet = stats({
      now: { total: 0, formats: { mp3: 0 } },
      days: [],
      period: { sessions: 0, seconds: 0, longest: 0, peak: 0, peak_date: null, peak_at: null, bands: [], formats: {} },
    });
    expect(summaryText(quiet, "en-GB")).toBe(
      "Listening now: 0. Nobody has listened yet today. No listening recorded in the last 30 days.",
    );
    expect(summaryText(stats({ keep_days: 0 }), "en-GB"))
      .toBe("Listening now: 3 (MP3 2, AAC 1). No statistics are kept: Days to keep statistics is 0.");
  });
});

describe("listener statistics view", () => {
  beforeEach(() => vi.clearAllMocks());

  it("loads once in stream mode and fills headed tables and a list", async () => {
    const { view, request } = setup();
    view.apply({ stream_mode: false });
    expect(request).not.toHaveBeenCalled();
    view.apply({ stream_mode: true, settings: { playback: { stream_stats_days: 90 } } });
    view.apply({ stream_mode: true });
    await tick();
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("/api/stream/stats?days=30");
    const formats = document.getElementById("listener-stats-formats");
    expect(formats.hidden).toBe(false);
    expect(formats.caption.textContent).toBe("Listeners by format, the last 30 days");
    expect(cells(formats)).toEqual([
      ["MP3", "2", "3", "5", "1.7"],
      ["AAC", "1", "1", "1", "0.3"],
      ["Opus", "0", "0", "0", "0.0"],
    ]);
    // Each row is named by its first cell.
    const head = formats.querySelector("tbody tr > :first-child");
    expect(head.tagName).toBe("TH");
    expect(head.getAttribute("scope")).toBe("row");
    const days = document.getElementById("listener-stats-days");
    expect(days.caption.textContent).toBe("Listeners by day, the last 30 days, newest first");
    expect(cells(days)).toEqual([
      ["Tue 13 Oct", "4 at 14:05", "6", "2.0", "20 minutes", "1 hour 5 minutes"],
      ["Mon 12 Oct", "0", "0", "0.0", "None", "None"],
    ]);
    expect([...document.querySelectorAll("#listener-stats-lengths li")].map((li) => li.textContent))
      .toEqual([
        "Under 1 minute: 1 session", "1 to 5 minutes: 2 sessions", "5 to 15 minutes: 0 sessions",
        "15 minutes to 1 hour: 2 sessions", "1 to 3 hours: 1 session", "3 hours or more: 0 sessions",
      ]);
    expect(document.getElementById("listener-stats-lengths-title").hidden).toBe(false);
    expect(document.getElementById("listener-stats-keep").value).toBe("90");
    // Loading on its own says nothing.
    expect(status()).toBe("");
  });

  it("hides the tables and the list when nothing was recorded", async () => {
    const { view } = setup({
      request: vi.fn(async () => stats({
        now: { total: 0, formats: {} },
        days: [],
        period: { sessions: 0, seconds: 0, longest: 0, peak: 0, peak_date: null, peak_at: null, bands: [0, 0, 0, 0, 0, 0], formats: {} },
      })),
    });
    view.apply({ stream_mode: true });
    await tick();
    expect(document.getElementById("listener-stats-formats").hidden).toBe(true);
    expect(document.getElementById("listener-stats-days").hidden).toBe(true);
    expect(document.getElementById("listener-stats-lengths").hidden).toBe(true);
    expect(document.getElementById("listener-stats-summary").textContent)
      .toContain("No listening recorded in the last 30 days.");
  });

  it("says what Refresh, Show and Days to keep did, once each", async () => {
    const { view, request, saveKeepDays } = setup();
    view.apply({ stream_mode: true });
    await tick();
    document.getElementById("listener-stats-refresh").click();
    await vi.waitFor(() => expect(status()).toBe("Listener statistics updated."));
    const span = document.getElementById("listener-stats-span");
    span.value = "7";
    span.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(status()).toBe("Showing the last 7 days."));
    expect(request).toHaveBeenLastCalledWith("/api/stream/stats?days=7");
    const keep = document.getElementById("listener-stats-keep");
    keep.value = "30";
    keep.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(status()).toBe("Listener statistics are kept for 30 days."));
    expect(saveKeepDays).toHaveBeenCalledWith(30, keep);
    keep.value = "0";
    keep.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(status())
      .toBe("Listener statistics are off, and those kept are deleted."));
  });

  it("refuses a bad number of days without saving and keeps focus on the field", async () => {
    const { view, saveKeepDays } = setup();
    view.apply({ stream_mode: true });
    await tick();
    const keep = document.getElementById("listener-stats-keep");
    for (const bad of ["-1", "3651", "2.5", ""]) {
      keep.value = bad;
      keep.dispatchEvent(new Event("change"));
      await vi.waitFor(() => expect(status())
        .toBe("Days to keep statistics must be a whole number from 0 to 3650."));
      expect(document.activeElement).toBe(keep);
      document.getElementById("settings-status").textContent = "";
    }
    expect(saveKeepDays).not.toHaveBeenCalled();
  });

  it("says a failed load once with the error tone, and nothing after a sign-out", async () => {
    const failing = vi.fn(async () => { throw new Error("Server error"); });
    const { view } = setup({ request: failing });
    view.apply({ stream_mode: true });
    await vi.waitFor(() => expect(status()).toBe("Could not load listener statistics: Server error"));
    document.getElementById("settings-status").textContent = "";
    failing.mockImplementation(async () => {
      const error = new Error("sign in");
      error.name = "AuthenticationRequiredError";
      throw error;
    });
    document.getElementById("listener-stats-refresh").click();
    await tick();
    expect(status()).toBe("");
  });

  it("downloads the CSV under the server's name", async () => {
    const { view, download, save } = setup();
    view.apply({ stream_mode: true });
    await tick();
    const button = document.getElementById("listener-stats-export");
    button.focus();
    button.click();
    await vi.waitFor(() => expect(status()).toBe("Listener statistics downloaded."));
    expect(download).toHaveBeenCalledWith("/api/stream/stats/export", ["text/csv"]);
    expect(save.mock.calls[0][2]).toBe("autodj-listeners-default.csv");
    expect(document.activeElement).toBe(button);
    download.mockRejectedValueOnce(new Error("Not Found"));
    button.click();
    await vi.waitFor(() => expect(status())
      .toBe("Could not download listener statistics: Not Found"));
  });

  it("forgets the station's numbers on reset and loads again when shown", async () => {
    const { view, request } = setup();
    view.apply({ stream_mode: true });
    await tick();
    view.reset();
    expect(document.getElementById("listener-stats-summary").textContent)
      .toBe("Statistics not loaded yet.");
    expect(document.getElementById("listener-stats-days").hidden).toBe(true);
    expect(document.querySelectorAll("#listener-stats-days tbody tr")).toHaveLength(0);
    view.apply({ stream_mode: true });
    await tick();
    expect(request).toHaveBeenCalledTimes(2);
  });
});
