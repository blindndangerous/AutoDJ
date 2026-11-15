import { describe, expect, it, vi } from "vitest";
import {
  applyHistoryFilterOnEnter,
  SHOW_LINERS_KEY,
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
