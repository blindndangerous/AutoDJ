// Cue-strip / legend render + cue summary announcements.

import { describe, it, expect, vi } from "vitest";
import * as cuesModule from "../../src/autodj/static/modules/cues.js";

const { renderCueStrip, renderCueLegend, CUE_COLORS } = cuesModule;

describe("renderCueStrip", () => {
  it("clears strip when no cues", () => {
    const el = document.createElement("div");
    el.innerHTML = "<span></span>";
    renderCueStrip(el, { path: "a.flac", length: 100, cues: [] });
    expect(el.innerHTML).toBe("");
  });

  it("renders cue marks at correct percentages", () => {
    const el = document.createElement("div");
    renderCueStrip(el, {
      path: "b.flac",
      length: 100,
      cues: [{ type: "drop", time_s: 25 }],
    });
    const marker = el.querySelector(".cue-mark");
    expect(marker.style.left).toBe("25.00%");
    expect(marker.style.background).toBe(CUE_COLORS.drop);
  });

  it("dedupes against previous render of same key", () => {
    const el = document.createElement("div");
    const track = {
      path: "c.flac", length: 60,
      cues: [{ type: "drop", time_s: 30 }],
    };
    renderCueStrip(el, track);
    el.innerHTML = "tampered";
    renderCueStrip(el, track);
    // Same path + same cue count -> no rebuild.
    expect(el.innerHTML).toBe("tampered");
  });

  it("rejects a malicious cue color without creating event attributes", () => {
    const element = document.createElement("div");

    renderCueStrip(element, {
      path: "malicious-color.flac",
      length: 60,
      cues: [{
        color: 'red;" onmouseover="alert(1)',
        label: "Safe label",
        time_s: 10,
        type: "drop",
      }],
    });

    const marker = element.querySelector(".cue-mark");
    expect(marker.getAttribute("onmouseover")).toBeNull();
    expect(marker.onmouseover).toBeNull();
    expect(marker.style.background).toBe(CUE_COLORS.drop);
    expect(marker.title).toBe("drop: Safe label");
  });

  it("falls back when the browser rejects a conservatively shaped color", () => {
    const supports = vi.fn(() => false);
    vi.stubGlobal("CSS", { supports });
    const element = document.createElement("div");

    renderCueStrip(element, {
      path: "unsupported-color.flac",
      length: 60,
      cues: [{ color: "plum", time_s: 10, type: "drop" }],
    });

    expect(supports).toHaveBeenCalledWith("color", "plum");
    expect(element.querySelector(".cue-mark").style.background)
      .toBe(CUE_COLORS.drop);
    vi.unstubAllGlobals();
  });
});

describe("applyCueSummary", () => {
  it("describes exactly the cue points valid for the visual strip", () => {
    const element = document.createElement("p");
    cuesModule.applyCueSummary?.({
      length: 100,
      cues: [
        { type: "drop", time_s: 25 },
        { type: "outro_downbeat", time_s: 90 },
        { type: "late", time_s: 101 },
        { type: "broken", time_s: Number.NaN },
      ],
    }, element);

    expect(element.textContent).toBe(
      "2 cue points, drop at 25 seconds, outro downbeat at 90 seconds",
    );
  });

  it("resets to durable empty text for a null track", () => {
    const element = document.createElement("p");
    element.textContent = "Old cues";

    cuesModule.applyCueSummary?.(null, element);

    expect(element.textContent).toBe("No cue points");
  });

  it("includes a trimmed user label without repeating fallback wording", () => {
    const element = document.createElement("p");
    const strip = document.createElement("div");
    const track = {
      path: "labeled-user.flac",
      length: 60,
      cues: [
        { type: " user ", label: " Chorus ", time_s: 10 },
        { type: "user", label: "user", time_s: 20 },
      ],
    };

    cuesModule.applyCueSummary?.(track, element);
    renderCueStrip(strip, track);

    expect(element.textContent).toBe(
      "2 cue points, user: Chorus at 10 seconds, user at 20 seconds",
    );
    expect(Array.from(strip.querySelectorAll(".cue-mark"), (mark) => mark.title))
      .toEqual(["user: Chorus", "user"]);
  });

  it.each(["", "   "])("uses the cue type when the label is %j", (label) => {
    const element = document.createElement("p");

    cuesModule.applyCueSummary?.({
      length: 60,
      cues: [{ type: " user ", label, time_s: 10 }],
    }, element);

    expect(element.textContent).toBe("1 cue point, user at 10 seconds");
  });

  it("writes markup-like labels as text", () => {
    const element = document.createElement("p");

    cuesModule.applyCueSummary?.({
      length: 60,
      cues: [{ type: "user", label: "<img src=x>", time_s: 10 }],
    }, element);

    expect(element.textContent).toContain("user: <img src=x> at 10 seconds");
    expect(element.querySelector("img")).toBeNull();
  });

  it("caps the slider summary and exposes all details separately", () => {
    const summary = document.createElement("p");
    const details = document.createElement("p");
    const track = {
      length: 100,
      cues: [
        { type: "user", label: "Intro", time_s: 10 },
        { type: "drop", time_s: 20 },
        { type: "breakdown", time_s: 30 },
        { type: "first_downbeat", time_s: 40 },
        { type: "outro_downbeat", time_s: 50 },
      ],
    };

    cuesModule.applyCueSummary?.(track, summary, details);

    expect(summary.textContent).toBe(
      "5 cue points, user: Intro at 10 seconds, drop at 20 seconds, "
      + "breakdown at 30 seconds, and 2 more",
    );
    expect(summary.textContent).not.toContain("first downbeat");
    expect(details.textContent).toBe(
      "5 cue points, user: Intro at 10 seconds, drop at 20 seconds, "
      + "breakdown at 30 seconds, first downbeat at 40 seconds, "
      + "outro downbeat at 50 seconds",
    );
  });

  it("does not rewrite unchanged summary or detail text", () => {
    const element = document.createElement("p");
    const details = document.createElement("p");
    let summaryWrites = 0;
    let detailWrites = 0;
    const descriptor = Object.getOwnPropertyDescriptor(
      globalThis.Node.prototype,
      "textContent",
    );
    const countWrites = (target, onWrite) => {
      let storedText = target.textContent;
      Object.defineProperty(target, "textContent", {
        configurable: true,
        get() { return storedText; },
        set(value) {
          onWrite();
          storedText = String(value);
          descriptor.set.call(this, value);
        },
      });
    };
    countWrites(element, () => { summaryWrites += 1; });
    countWrites(details, () => { detailWrites += 1; });
    const track = { length: 60, cues: [{ type: "drop", time_s: 30 }] };

    cuesModule.applyCueSummary?.(track, element, details);
    cuesModule.applyCueSummary?.(track, element, details);

    expect(summaryWrites).toBe(1);
    expect(detailWrites).toBe(1);
  });
});

describe("renderCueLegend", () => {
  function legendEl() {
    document.body.innerHTML = '<div id="cue-legend" hidden></div>';
    return document.querySelector("#cue-legend");
  }

  it("lists one visible swatch per cue type present", () => {
    const el = legendEl();
    renderCueLegend(el, {
      path: "a.mp3",
      length: 200,
      cues: [
        { type: "drop", time_s: 10 },
        { type: "drop", time_s: 90 },
        { type: "breakdown", time_s: 40 },
      ],
    });

    const keys = el.querySelectorAll(".cue-key");
    expect(keys).toHaveLength(2);
    expect([...keys].map((k) => k.textContent)).toEqual(["drop", "breakdown"]);
    expect(keys[0].querySelector(".cue-swatch").style.background)
      .toBe(CUE_COLORS.drop);
    expect(el.hidden).toBe(false);
  });

  it("stays hidden and empty when the track has no cues", () => {
    const el = legendEl();
    renderCueLegend(el, { path: "b.mp3", length: 200, cues: [] });
    expect(el.hidden).toBe(true);
    expect(el.children).toHaveLength(0);
  });

  it("spells underscored type names as words", () => {
    const el = legendEl();
    renderCueLegend(el, {
      path: "c.mp3", length: 100, cues: [{ type: "first_downbeat", time_s: 1 }],
    });
    expect(el.textContent).toBe("first downbeat");
  });

  it("does no DOM work when the cue set is unchanged", () => {
    const el = legendEl();
    const track = {
      path: "d.mp3", length: 100, cues: [{ type: "drop", time_s: 5 }],
    };
    renderCueLegend(el, track);
    const first = el.firstElementChild;
    renderCueLegend(el, track);
    expect(el.firstElementChild).toBe(first);
  });

  it("no-ops without an element", () => {
    expect(() => renderCueLegend(null, null)).not.toThrow();
  });
});
