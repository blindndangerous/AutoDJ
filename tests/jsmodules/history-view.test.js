import { describe, expect, it } from "vitest";
import {
  SHOW_LINERS_KEY,
  historyAnchor,
  historyKindLabel,
  historyQuery,
  linersMessage,
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

describe("History's Show liners choice", () => {
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

  it("asks for liners and the anchor only when there are any", () => {
    expect(historyQuery({ page: 2, showLiners: false })).toBe("page=2&per_page=50");
    expect(historyQuery({ page: 1, showLiners: true, anchor: "abc" }))
      .toBe("page=1&per_page=50&liners=true&anchor=abc");
    expect(historyQuery({ page: 3, showLiners: false, anchor: null })).toBe("page=3&per_page=50");
  });

  it("anchors on the first row shown", () => {
    const table = mountTable();
    const tbody = table.querySelector("tbody");
    expect(historyAnchor(tbody)).toBeNull();
    tbody.innerHTML = `
      <tr class="history-date"><td colspan="5"><h3>1 Sept 2026</h3></td></tr>
      <tr data-played-at="x" data-history-id="liner-9" data-kind="liner"><td>a</td></tr>
      <tr data-played-at="y" data-history-id="song-8" data-kind="song"><td>b</td></tr>`;
    expect(historyAnchor(tbody)).toBe("liner-9");
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

  it("says once what the view now shows, in numbers", () => {
    expect(linersMessage(true, { total: 120, page: 3, pages: 3 }))
      .toBe("Liners shown.  120 items, page 3 of 3.");
    expect(linersMessage(false, { total: 1, page: 1, pages: 1 }))
      .toBe("Liners hidden.  1 item, page 1 of 1.");
    expect(linersMessage(true, { total: 0, page: 1, pages: 1 }))
      .toBe("Liners shown.  No items.");
  });
});
