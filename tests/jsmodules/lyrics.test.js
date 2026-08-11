import { describe, expect, it, vi } from "vitest";

import { applyLyricsState, loadLyrics, resetLyricState } from
  "../../src/autodj/static/modules/lyrics.js";

function lyricsResponse(text) {
  return new globalThis.Response(JSON.stringify({
    lyrics: [{ time: 0, text }],
  }), { headers: { "Content-Type": "application/json" } });
}

describe("lyrics request ownership", () => {
  it("does not erase a current-line announcement when loading succeeds", async () => {
    resetLyricState();
    let resolveLyrics;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveLyrics = resolve;
    })));
    const elements = {
      lyricAnnounce: document.createElement("p"),
      lyricsCard: document.createElement("section"),
      lyricsList: document.createElement("ul"),
    };
    elements.lyricsList.innerHTML = "<li>Current line</li>";

    const loading = loadLyrics(elements);
    applyLyricsState({
      has_lyrics: true,
      lyric_index: 0,
      lyric_text: "Current line",
    }, elements);
    resolveLyrics(lyricsResponse("Current line"));
    await loading;

    expect(elements.lyricAnnounce.textContent).toBe("Current line");
    vi.unstubAllGlobals();
  });

  it("announces a current-generation lyrics failure", async () => {
    resetLyricState();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({ detail: "Lyrics unavailable" }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    )));
    const elements = {
      lyricAnnounce: document.createElement("p"),
      lyricsCard: document.createElement("section"),
      lyricsList: document.createElement("ul"),
    };

    await loadLyrics(elements);

    expect(elements.lyricAnnounce.textContent).toContain("Lyrics unavailable");
    vi.unstubAllGlobals();
  });

  it("does not let stale lyrics replace the newest track response", async () => {
    resetLyricState();
    const resolvers = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolvers.push(resolve);
    })));
    const elements = {
      lyricsCard: document.createElement("section"),
      lyricsList: document.createElement("ul"),
    };

    const first = loadLyrics(elements);
    const second = loadLyrics(elements);
    resolvers[1](lyricsResponse("Newest lyrics"));
    await second;
    resolvers[0](lyricsResponse("Stale lyrics"));
    await first;

    expect(elements.lyricsList.textContent).toBe("Newest lyrics");
    vi.unstubAllGlobals();
  });
});
