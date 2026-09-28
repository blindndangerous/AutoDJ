import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyLyricsState, currentLyricLine, loadLyrics, resetLyricState, stripLyricTimestamps,
} from
  "../../src/autodj/static/modules/lyrics.js";

function lyricsResponse(path, text) {
  return new globalThis.Response(JSON.stringify({
    path,
    lyrics: [{ time: 0, text }],
  }), { headers: { "Content-Type": "application/json" } });
}

function lyricElements() {
  return {
    lyricsCard: document.createElement("section"),
    lyricsList: document.createElement("ul"),
  };
}

afterEach(() => {
  resetLyricState();
  vi.unstubAllGlobals();
});

describe("lyrics request ownership", () => {
  it("does not let stale lyrics replace the newest track response", async () => {
    const resolvers = [];
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolvers.push(resolve);
    })));
    const elements = lyricElements();

    const first = loadLyrics("first.flac", elements);
    const second = loadLyrics("second.flac", elements);
    resolvers[1](lyricsResponse("second.flac", "Newest lyrics"));
    await second;
    resolvers[0](lyricsResponse("first.flac", "Stale lyrics"));
    await first;

    expect(elements.lyricsList.textContent).toBe("Newest lyrics");
  });

  it("clears old lyrics while loading and requests the encoded track path", async () => {
    let resolveLyrics;
    const fetchImpl = vi.fn(() => new Promise((resolve) => {
      resolveLyrics = resolve;
    }));
    vi.stubGlobal("fetch", fetchImpl);
    const elements = lyricElements();
    elements.lyricsCard.hidden = false;
    elements.lyricsList.innerHTML = "<li>Old lyrics</li>";

    const loading = loadLyrics("Z:/Music/A & B.flac", elements);

    expect(elements.lyricsCard.hidden).toBe(true);
    expect(elements.lyricsList.children).toHaveLength(0);
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/lyrics?path=Z%3A%2FMusic%2FA%20%26%20B.flac",
      expect.objectContaining({ signal: expect.any(globalThis.AbortSignal) }),
    );

    resolveLyrics(lyricsResponse("Z:/Music/A & B.flac", "Fresh lyrics"));
    await loading;
    expect(elements.lyricsList.textContent).toBe("Fresh lyrics");
  });

  it("rejects a mismatched response path without rendering it", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      lyricsResponse("other.flac", "Wrong track"),
    ));
    const elements = lyricElements();

    await loadLyrics("wanted.flac", elements);

    expect(elements.lyricsList.children).toHaveLength(0);
    expect(elements.lyricsCard.hidden).toBe(true);
  });

  it("keeps the card hidden when the current track has no timed lyrics", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({ path: "empty.flac", lyrics: [] }),
      { headers: { "Content-Type": "application/json" } },
    )));
    const elements = lyricElements();

    await loadLyrics("empty.flac", elements);

    expect(elements.lyricsCard.hidden).toBe(true);
  });

  it("shows a later plain fallback after an empty timed response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({ path: "plain-after-empty.flac", lyrics: [] }),
      { headers: { "Content-Type": "application/json" } },
    )));
    const elements = lyricElements();
    await loadLyrics("plain-after-empty.flac", elements);

    applyLyricsState({
      has_lyrics: false,
      lyric_index: null,
      lyric_text: "",
      lyrics_plain: "Plain lyrics arrived later",
    }, elements);

    expect(elements.lyricsCard.hidden).toBe(false);
    expect(elements.lyricsList.querySelector(".plain-lyrics")?.textContent)
      .toBe("Plain lyrics arrived later");
    expect(elements.lyricsList.querySelector(".active")).toBeNull();
  });

  it("applies the current line after state arrives before the lyric list", async () => {
    let resolveLyrics;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveLyrics = resolve;
    })));
    const elements = lyricElements();
    const currentState = {
      has_lyrics: true,
      lyric_index: 0,
      lyric_text: "Arrived late",
    };
    const loading = loadLyrics("late.flac", elements);
    applyLyricsState(currentState, elements);
    resolveLyrics(lyricsResponse("late.flac", "Arrived late"));
    await loading;

    applyLyricsState(currentState, elements);

    const line = elements.lyricsList.querySelector("li");
    expect(line.classList.contains("active")).toBe(true);
    expect(line.getAttribute("aria-current")).toBe("true");
  });

  it("does not let an empty timed response erase a newer plain-lyrics fallback", async () => {
    let resolveLyrics;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveLyrics = resolve;
    })));
    const elements = lyricElements();
    const loading = loadLyrics("plain.flac", elements);
    applyLyricsState({
      has_lyrics: false,
      lyric_index: null,
      lyric_text: "",
      lyrics_plain: "Plain lyrics remain visible",
    }, elements);
    resolveLyrics(new globalThis.Response(JSON.stringify({
      path: "plain.flac",
      lyrics: [],
    }), { headers: { "Content-Type": "application/json" } }));

    await loading;

    expect(elements.lyricsCard.hidden).toBe(false);
    expect(elements.lyricsList.querySelector(".plain-lyrics")?.textContent)
      .toBe("Plain lyrics remain visible");
  });

  it("preserves request-owned timed lyrics across a transient no-lyrics state", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({
        path: "timed.flac",
        lyrics: [
          { time: 0, text: "First line" },
          { time: 2, text: "Second line" },
        ],
      }),
      { headers: { "Content-Type": "application/json" } },
    )));
    const elements = lyricElements();
    await loadLyrics("timed.flac", elements);

    applyLyricsState({
      has_lyrics: true,
      lyric_index: 0,
      lyric_text: "First line",
    }, elements);
    applyLyricsState({
      has_lyrics: false,
      lyric_index: null,
      lyric_text: "",
      lyrics_plain: "",
    }, elements);

    expect(elements.lyricsList.children).toHaveLength(2);
    expect(elements.lyricsCard.hidden).toBe(false);
    expect(elements.lyricsList.querySelector(".active")).toBeNull();

    applyLyricsState({
      has_lyrics: true,
      lyric_index: 0,
      lyric_text: "First line",
    }, elements);

    expect(elements.lyricsList.querySelector(".active")?.textContent).toBe("First line");
  });

  it("reset aborts the active request and clears the supplied lyric elements", async () => {
    let requestSignal;
    vi.stubGlobal("fetch", vi.fn((_url, options) => {
      requestSignal = options.signal;
      return new Promise(() => {});
    }));
    const elements = lyricElements();
    elements.lyricsCard.hidden = false;
    elements.lyricsList.innerHTML = "<li>Old lyrics</li>";
    void loadLyrics("pending.flac", elements);

    resetLyricState(elements);

    expect(requestSignal.aborted).toBe(true);
    expect(elements.lyricsCard.hidden).toBe(true);
    expect(elements.lyricsList.children).toHaveLength(0);
  });

  it.each([
    [true, "auto"],
    [false, "smooth"],
  ])("uses %s reduced-motion preference when scrolling the current line", async (
    reducedMotion, expectedBehavior,
  ) => {
    vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: reducedMotion })));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      lyricsResponse("motion.flac", "Line one"),
    ));
    const elements = lyricElements();
    await loadLyrics("motion.flac", elements);
    const line = elements.lyricsList.querySelector("li");
    line.scrollIntoView = vi.fn();
    elements.lyricsList.scrollTo = vi.fn();

    applyLyricsState({
      has_lyrics: true,
      lyric_index: 0,
      lyric_text: "Line one",
    }, elements);

    // Only the lyrics box scrolls -- scrollIntoView would drag every
    // scrollable ancestor, including the page, once per line.
    expect(line.scrollIntoView).not.toHaveBeenCalled();
    expect(elements.lyricsList.scrollTo).toHaveBeenCalledWith({
      top: expect.any(Number),
      behavior: expectedBehavior,
    });
    expect(line.getAttribute("aria-current")).toBe("true");
  });

  it.each([
    ["absent", undefined],
    ["non-callable", {}],
    ["throwing", vi.fn(() => { throw new Error("media query failed"); })],
  ])("uses non-animated scrolling when matchMedia is %s", async (
    _description, matchMediaValue,
  ) => {
    vi.stubGlobal("matchMedia", matchMediaValue);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      lyricsResponse("safe-motion.flac", "Line one"),
    ));
    const elements = lyricElements();
    await loadLyrics("safe-motion.flac", elements);
    const line = elements.lyricsList.querySelector("li");
    line.scrollIntoView = vi.fn();
    elements.lyricsList.scrollTo = vi.fn();

    expect(() => applyLyricsState({
      has_lyrics: true,
      lyric_index: 0,
      lyric_text: "Line one",
    }, elements)).not.toThrow();

    expect(line.scrollIntoView).not.toHaveBeenCalled();
    expect(elements.lyricsList.scrollTo).toHaveBeenCalledWith({
      top: expect.any(Number),
      behavior: "auto",
    });
  });
});

const NL = "\n";
const CRLF = "\r\n";

describe("plain lyric fallback", () => {
  it("never prints raw LRC timestamps on screen", () => {
    const elements = lyricElements();
    applyLyricsState({
      has_lyrics: false,
      lyrics_plain: "[00:17.49]So unaffectionate\n[00:21.00]<00:21.00>So insecure",
    }, elements);

    const block = elements.lyricsList.querySelector(".plain-lyrics");
    expect(block.textContent).toBe("So unaffectionate\nSo insecure");
    expect(block.textContent).not.toContain("[00:17");
  });

  it("keeps every untimestamped line, tidying only the whitespace", () => {
    const elements = lyricElements();
    applyLyricsState({
      has_lyrics: false,
      lyrics_plain: "Just words\n  and more words  ",
    }, elements);

    expect(elements.lyricsList.querySelector(".plain-lyrics").textContent)
      .toBe("Just words\nand more words");
  });

  it("keeps a time mentioned mid-line", () => {
    // Only a LEADING stamp is cue syntax; "meet me at [10:30] tonight" is
    // a lyric and must survive intact.
    expect(stripLyricTimestamps("meet me at [10:30] tonight"))
      .toBe("meet me at [10:30] tonight");
  });

  it("drops metadata-only lines but keeps untimestamped lyrics", () => {
    expect(stripLyricTimestamps(
      "[ar:Artist]" + NL + "[ti:Title]" + NL + "Written by X" + NL + "[00:00.00]Intro" + NL + "Verse one",
    )).toBe("Written by X" + NL + "Intro" + NL + "Verse one");
  });

  it("handles CRLF, a BOM and stamps with no fraction", () => {
    expect(stripLyricTimestamps(
      "\uFEFF[00:01]A" + CRLF + "[00:02.345]B" + CRLF,
    ).replace(/^\uFEFF/, "")).toBe("A" + NL + "B");
    // trim() leaves U+FEFF in place, so it has to be removed explicitly.
    expect(stripLyricTimestamps("\uFEFFplain").startsWith("\uFEFF")).toBe(false);
  });

  it("exports a no-op strip for non-string input", () => {
    expect(stripLyricTimestamps(null)).toBe(null);
    expect(stripLyricTimestamps("plain")).toBe("plain");
  });
});

describe("timed highlight from the local playback clock", () => {
  function timedResponse(path) {
    return new globalThis.Response(JSON.stringify({
      path,
      lyrics: [
        { time_s: 0, text: "Line zero" },
        { time_s: 17.49, text: "Line one" },
        { time_s: 42, text: "Line two" },
      ],
    }), { headers: { "Content-Type": "application/json" } });
  }

  async function loaded() {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(timedResponse("t.flac")));
    const elements = lyricElements();
    elements.lyricsList.scrollTo = vi.fn();
    await loadLyrics("t.flac", elements);
    return elements;
  }

  it("highlights from the browser position when the server clock is stuck", async () => {
    const elements = await loaded();

    // Browser-driven playback: the server reports elapsed 0.0 forever, so
    // lyric_index is null and the highlight could never fire.
    applyLyricsState(
      { has_lyrics: true, lyric_index: null, lyric_text: null },
      elements,
      { elapsed: 20.5, localClock: true },
    );

    const items = elements.lyricsList.querySelectorAll("li");
    expect(items[1].classList.contains("active")).toBe(true);
    expect(items[1].getAttribute("aria-current")).toBe("true");
  });

  it("moves aria-current to the next line", async () => {
    const elements = await loaded();
    applyLyricsState({ has_lyrics: true, lyric_index: null }, elements,
      { elapsed: 20.5, localClock: true });
    applyLyricsState({ has_lyrics: true, lyric_index: null }, elements,
      { elapsed: 42.1, localClock: true });

    const current = elements.lyricsList.querySelectorAll('li[aria-current="true"]');
    expect([...current].map((li) => li.textContent)).toEqual(["Line two"]);
  });

  it("highlights nothing before the first line's timestamp", async () => {
    const elements = await loaded();
    applyLyricsState({ has_lyrics: true, lyric_index: null }, elements,
      { elapsed: -1, localClock: true });

    expect(elements.lyricsList.querySelectorAll("li.active")).toHaveLength(0);
  });

  it("still trusts the server index for server-side playback", async () => {
    const elements = await loaded();

    applyLyricsState(
      { has_lyrics: true, lyric_index: 2, lyric_text: "Line two" },
      elements,
      { elapsed: 0, localClock: false },
    );

    const items = elements.lyricsList.querySelectorAll("li");
    expect(items[2].classList.contains("active")).toBe(true);
  });

  it("ignores the local clock when there are no timed lines", async () => {
    const elements = lyricElements();
    expect(() => applyLyricsState(
      { has_lyrics: true, lyric_index: null }, elements,
      { elapsed: 30, localClock: true },
    )).not.toThrow();
    expect(elements.lyricsList.querySelectorAll("li.active")).toHaveLength(0);
  });
});

describe("the current line said on request", () => {
  async function loadedWith(lines) {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({ path: "t.flac", lyrics: lines }),
      { headers: { "Content-Type": "application/json" } },
    )));
    const elements = lyricElements();
    elements.lyricsList.scrollTo = vi.fn();
    await loadLyrics("t.flac", elements);
    return elements;
  }
  const at = (elements, elapsed) => applyLyricsState(
    { has_lyrics: true, lyric_index: null }, elements, { elapsed, localClock: true },
  );

  it("says the line playing now, or the next one during a break", async () => {
    const elements = await loadedWith([
      { time_s: 5, text: "First" },
      { time_s: 10, text: "" },
      { time_s: 20, text: "Second" },
      { time_s: 30, text: "" },
    ]);
    at(elements, 1);
    expect(currentLyricLine()).toBe("Instrumental. Next line: First");
    at(elements, 6);
    expect(currentLyricLine()).toBe("First");
    at(elements, 12);
    expect(currentLyricLine()).toBe("Instrumental. Next line: Second");
    at(elements, 31);
    expect(currentLyricLine()).toBe("Instrumental.");
  });

  it("says when there are no lyrics or they are not timed", () => {
    expect(currentLyricLine()).toBe("No lyrics for this track.");
    applyLyricsState({ has_lyrics: false, lyrics_plain: "Words" }, lyricElements());
    expect(currentLyricLine()).toBe("Lyrics for this track are not timed.");
  });
});
