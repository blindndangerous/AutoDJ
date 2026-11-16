import { describe, expect, it, vi } from "vitest";
import {
  applyHistoryFilterOnEnter,
  createHistoryFilterFeedback,
  SHOW_LINERS_KEY,
  historyFiltersKey,
  historyKindLabel,
  historyQuery,
  historyResultsMessage,
  readShowLiners,
  setHistoryTypeColumn,
  writeShowLiners,
} from "../../src/autodj/static/modules/history-view.js";

function memoryStorage() {
  const values = new Map();
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
}

function mountTable() {
  document.body.innerHTML = `
    <table id="history-table">
      <colgroup>
        <col class="hist-time"><col class="hist-title"><col class="hist-artist">
        <col class="hist-duration"><col class="hist-info">
      </colgroup>
      <thead><tr>
        <th scope="col">Played</th><th scope="col">Title</th><th scope="col">Artist</th>
        <th scope="col">Duration</th><th scope="col">Track info</th>
      </tr></thead>
      <tbody id="history-tbody"></tbody>
    </table>`;
  return document.getElementById("history-table");
}

describe("History filters", () => {
  it("is off until chosen and is remembered in this browser", () => {
    const storage = memoryStorage();
    expect(readShowLiners(storage)).toBe(false);
    writeShowLiners(storage, true);
    expect(storage.values.get(SHOW_LINERS_KEY)).toBe("true");
    expect(readShowLiners(storage)).toBe(true);
    writeShowLiners(storage, false);
    expect(readShowLiners(storage)).toBe(false);
  });

  it("works when the browser refuses storage", () => {
    const refusing = {
      getItem() { throw new Error("denied"); },
      setItem() { throw new Error("denied"); },
    };
    expect(readShowLiners(refusing)).toBe(false);
    expect(() => writeShowLiners(refusing, true)).not.toThrow();
    expect(readShowLiners(null)).toBe(false);
  });

  it("sends the same range, text and nonmusic values with every page", () => {
    expect(historyQuery({
      page: 2, from: "2026-09-01", to: "2026-09-17", q: "  artist title  ", nonmusic: false,
    })).toBe("from=2026-09-01&to=2026-09-17&q=artist+title&nonmusic=false&page=2&per_page=50");
    expect(historyQuery({
      page: 1, from: "2026-09-01", to: "2026-09-17", nonmusic: true, anchor: "abc",
    })).toBe("from=2026-09-01&to=2026-09-17&nonmusic=true&page=1&per_page=50&anchor=abc");
  });

  it("summarizes the filtered result count", () => {
    expect(historyResultsMessage({ total: 12, page: 1, pages: 3 })).toBe("12 plays.");
    expect(historyResultsMessage({ total: 1, page: 1, pages: 1 })).toBe("1 play.");
    expect(historyResultsMessage({ total: 0 })).toBe("No plays match these filters.");
  });

  it("keys pending feedback by the selected filters", () => {
    expect(historyFiltersKey({
      from: "2026-09-01", to: "2026-09-17", q: "  artist title  ", nonmusic: false,
    })).toBe(historyFiltersKey({
      from: "2026-09-01", to: "2026-09-17", q: "artist title", nonmusic: false,
    }));
    expect(historyFiltersKey({
      from: "2026-09-01", to: "2026-09-17", q: "artist title", nonmusic: true,
    })).not.toBe(historyFiltersKey({
      from: "2026-09-01", to: "2026-09-17", q: "artist title", nonmusic: false,
    }));
  });

  it("carries filter feedback across a passive refresh before the filtered response", () => {
    const completions = [];
    const selection = {
      cancel: vi.fn(),
      start: vi.fn(() => (message, token) => completions.push({ message, token })),
    };
    const say = vi.fn();
    const feedback = createHistoryFilterFeedback(selection, say);
    const key = "range-and-query";

    const token = feedback.start(key);
    // A passive response arrives first, then the selected filter response.
    expect(feedback.complete(key, { total: 1 })).toBe(true);
    expect(feedback.complete(key, { total: 2 })).toBe(true);
    expect(feedback.complete("other-filters", { total: 20 })).toBe(false);

    const latest = completions.at(-1);
    expect(feedback.deliver(latest.message, latest.token)).toBe(true);
    expect(feedback.deliver("1 play.", token)).toBe(false);
    expect(feedback.complete(key, { total: 3 })).toBe(false);
    expect(say).toHaveBeenCalledOnce();
    expect(say).toHaveBeenCalledWith("2 plays.");
    expect(selection.cancel).toHaveBeenCalled();
  });

  it("updates pending filter feedback when a passive refresh follows the filtered response", () => {
    const completions = [];
    const selection = {
      cancel: vi.fn(),
      start: vi.fn(() => (message, token) => completions.push({ message, token })),
    };
    const say = vi.fn();
    const feedback = createHistoryFilterFeedback(selection, say);
    const key = "range-and-query";

    feedback.start(key);
    expect(feedback.complete(key, { total: 1 })).toBe(true);
    // The first 500 ms announcement is replaced by the latest passive count.
    expect(feedback.complete(key, { total: 2 })).toBe(true);
    const latest = completions.at(-1);
    expect(feedback.deliver(latest.message, latest.token)).toBe(true);
    expect(feedback.deliver(latest.message, latest.token)).toBe(false);
    expect(say).toHaveBeenCalledOnce();
    expect(say).toHaveBeenCalledWith("2 plays.");
  });

  it("cancels stale feedback when a new filter or another explicit action starts", () => {
    const completions = [];
    const selection = {
      cancel: vi.fn(),
      start: vi.fn(() => (message, token) => completions.push({ message, token })),
    };
    const say = vi.fn();
    const feedback = createHistoryFilterFeedback(selection, say);
    const previous = feedback.start("old-filter");
    feedback.complete("old-filter", { total: 1 });

    const current = feedback.start("new-filter");
    const stale = completions[0];
    expect(feedback.deliver(stale.message, stale.token)).toBe(false);
    feedback.complete("new-filter", { total: 2 });
    feedback.cancel(); // explicit paging, refresh, export, or leaving History
    const cancelled = completions.at(-1);
    expect(feedback.deliver(cancelled.message, cancelled.token)).toBe(false);
    expect(feedback.deliver("No plays match these filters.", previous)).toBe(false);
    expect(say).not.toHaveBeenCalled();
    expect(selection.cancel).toHaveBeenCalled();
    expect(current).not.toBe(previous);
  });

  it("applies filters on Enter without submitting the export form or accepting IME Enter", () => {
    const apply = vi.fn();
    const enter = new KeyboardEvent("keydown", { key: "Enter", cancelable: true });
    expect(applyHistoryFilterOnEnter(enter, apply)).toBe(true);
    expect(enter.defaultPrevented).toBe(true);
    expect(apply).toHaveBeenCalledOnce();

    const ime = new KeyboardEvent("keydown", { key: "Enter", isComposing: true, cancelable: true });
    expect(applyHistoryFilterOnEnter(ime, apply)).toBe(false);
    expect(ime.defaultPrevented).toBe(false);
    expect(apply).toHaveBeenCalledOnce();
  });

  it("names every kind of item in words", () => {
    expect(historyKindLabel("song")).toBe("Song");
    expect(historyKindLabel("live")).toBe("Live DJ");
    expect(historyKindLabel("liner")).toBe("Liner");
    expect(historyKindLabel("ad")).toBe("Ad");
    expect(historyKindLabel("matched_intro")).toBe("Matched intro");
    expect(historyKindLabel("time_check")).toBe("Time check");
    expect(historyKindLabel("voice_track")).toBe("Voice track");
    expect(historyKindLabel("music_news")).toBe("Music news");
    expect(historyKindLabel("dj")).toBe("DJ break");
    expect(historyKindLabel("something_new")).toBe("Something new");
    expect(historyKindLabel(undefined)).toBe("Song");
  });

  it("adds a Type column after Duration and takes it away again", () => {
    const table = mountTable();
    const headings = () => [...table.querySelectorAll("thead th")].map((cell) => cell.textContent);
    setHistoryTypeColumn(table, true);
    setHistoryTypeColumn(table, true); // once only
    expect(headings()).toEqual(["Played", "Title", "Artist", "Duration", "Type", "Track info"]);
    const type = table.querySelectorAll("thead th")[4];
    expect(type.getAttribute("scope")).toBe("col");
    expect([...table.querySelectorAll("col")].map((col) => col.className)).toEqual([
      "hist-time", "hist-title", "hist-artist", "hist-duration", "hist-type", "hist-info",
    ]);
    expect(table.classList.contains("has-type")).toBe(true);
    setHistoryTypeColumn(table, false);
    expect(headings()).toEqual(["Played", "Title", "Artist", "Duration", "Track info"]);
    expect(table.querySelector("col.hist-type")).toBeNull();
    expect(table.classList.contains("has-type")).toBe(false);
  });

});
