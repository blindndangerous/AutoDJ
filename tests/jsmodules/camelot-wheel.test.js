// Camelot wheel geometry: every number must sit inside its own wedge,
// and the number for the current key must be readable on the lit wedge.

import { readFileSync } from "node:fs";

import { beforeEach, describe, expect, it } from "vitest";

import { applyCamelotWheel } from
  "../../src/autodj/static/modules/camelot-wheel.js";

const SVG_NS = "http://www.w3.org/2000/svg";

function wheel() {
  document.body.innerHTML = "";
  const svg = document.createElementNS(SVG_NS, "svg");
  const sectorsEl = document.createElementNS(SVG_NS, "g");
  const labelsEl = document.createElementNS(SVG_NS, "g");
  svg.append(sectorsEl, labelsEl);
  document.body.appendChild(svg);
  return { sectorsEl, labelsEl };
}

// 0 deg = 12 o'clock, growing clockwise -- the convention _polar uses.
function bearing(label) {
  const x = Number(label.getAttribute("x"));
  const y = Number(label.getAttribute("y"));
  return ((Math.atan2(y, x) * 180) / Math.PI + 90 + 360) % 360;
}

function radius(label) {
  const x = Number(label.getAttribute("x"));
  const y = Number(label.getAttribute("y"));
  return Math.hypot(x, y);
}

function labelFor(labelsEl, cell) {
  return labelsEl.querySelector(`.label[data-cell="${cell}"]`);
}

// Read the wedge back out of the path the module actually drew, so the
// labels and the sectors are checked against each other rather than both
// against the same formula.
function wedgeFor(sectorsEl, cell) {
  const d = sectorsEl.querySelector(`.sector[data-cell="${cell}"]`)
    .getAttribute("d");
  // M x1o y1o  A rOut rOut 0 large 1 x2o y2o  L x1i y1i  A rIn rIn 0 …
  //   0   1     2    3    4   5   6  7   8       9  10     11
  const n = d.match(/-?\d+(?:\.\d+)?/g).map(Number);
  const bearingOf = (x, y) => ((Math.atan2(y, x) * 180) / Math.PI + 90 + 360) % 360;
  return {
    rOut: n[2],
    rIn: n[11],
    from: bearingOf(n[0], n[1]),
    to: bearingOf(n[7], n[8]),
  };
}

// Shortest signed distance from `angle` to `target`, in degrees.
function angleGap(angle, target) {
  return Math.abs(((angle - target + 540) % 360) - 180);
}

describe("camelot wheel labels", () => {
  let els;
  beforeEach(() => { els = wheel(); });

  it("centres every number on the wedge that was actually drawn", () => {
    applyCamelotWheel("8A", "compatible", els);

    for (let n = 1; n <= 12; n++) {
      for (const side of ["A", "B"]) {
        const cell = `${n}${side}`;
        const label = labelFor(els.labelsEl, cell);
        expect(label, cell).not.toBeNull();
        const wedge = wedgeFor(els.sectorsEl, cell);

        // The label must sit on the midpoint of its own wedge's arc, which
        // is 15 deg from either boundary -- the old bug put it exactly on
        // the boundary with the next wedge.
        const midpoint = (wedge.from + 15) % 360;
        expect(angleGap(bearing(label), midpoint), `${cell} bearing`)
          .toBeLessThan(1);
        expect(angleGap(bearing(label), wedge.from), `${cell} vs start`)
          .toBeGreaterThan(10);
        expect(angleGap(bearing(label), wedge.to), `${cell} vs end`)
          .toBeGreaterThan(10);

        // ...and inside that wedge's own ring, not a neighbouring one.
        expect(radius(label), `${cell} radius`).toBeGreaterThan(wedge.rIn);
        expect(radius(label), `${cell} radius`).toBeLessThan(wedge.rOut);
      }
    }
  });

  it("labels both rings, the B ring outside the A ring", () => {
    applyCamelotWheel(null, "compatible", els);

    expect(els.labelsEl.querySelectorAll(".label")).toHaveLength(24);
    for (let n = 1; n <= 12; n++) {
      expect(radius(labelFor(els.labelsEl, `${n}B`)))
        .toBeGreaterThan(radius(labelFor(els.labelsEl, `${n}A`)));
      expect(wedgeFor(els.sectorsEl, `${n}B`).rIn)
        .toBe(wedgeFor(els.sectorsEl, `${n}A`).rOut);
    }
  });

  it("marks the number on the lit wedge, and only that one", () => {
    applyCamelotWheel("10A", "compatible", els);

    const active = els.labelsEl.querySelectorAll(".label.active");
    expect(active).toHaveLength(1);
    expect(active[0].getAttribute("data-cell")).toBe("10A");

    const litSectors = els.sectorsEl.querySelectorAll(".sector.active");
    expect(litSectors).toHaveLength(1);
    expect(litSectors[0].getAttribute("data-cell")).toBe("10A");
  });

  it("distinguishes minor-ring numbers from major-ring numbers", () => {
    applyCamelotWheel("3B", "compatible", els);

    expect(labelFor(els.labelsEl, "3A").classList.contains("minor")).toBe(true);
    expect(labelFor(els.labelsEl, "3B").classList.contains("minor")).toBe(false);
  });

  it("names the current key in the middle of the wheel", () => {
    applyCamelotWheel("8A", "compatible", els);
    expect(els.labelsEl.ownerDocument.querySelector("#camelot-current").textContent)
      .toBe("8A");

    applyCamelotWheel(null, "compatible", els);
    expect(els.labelsEl.ownerDocument.querySelector("#camelot-current").textContent)
      .toBe("—");
  });

  it("clears every highlight when there is no key", () => {
    applyCamelotWheel("8A", "compatible", els);
    applyCamelotWheel(null, "compatible", els);

    expect(els.labelsEl.querySelectorAll(".label.active")).toHaveLength(0);
    expect(els.sectorsEl.querySelectorAll(".sector.active")).toHaveLength(0);
    expect(els.sectorsEl.querySelectorAll(".sector.compat")).toHaveLength(0);
  });
});

describe("camelot label legibility", () => {
  const css = readFileSync("src/autodj/static/app.css", "utf8");

  function styled(stylesheet = css) {
    document.head.innerHTML = `<style>${stylesheet}</style>`;
    document.body.innerHTML = '<div id="host"></div>';
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("id", "camelot-wheel");
    const sectorsEl = document.createElementNS(SVG_NS, "g");
    const labelsEl = document.createElementNS(SVG_NS, "g");
    svg.append(sectorsEl, labelsEl);
    document.querySelector("#host").appendChild(svg);
    applyCamelotWheel("8A", "compatible", { sectorsEl, labelsEl });
    return { sectorsEl, labelsEl };
  }

  const fillOf = (element) => window.getComputedStyle(element).fill;

  function channels(colour) {
    if (colour.startsWith("#")) {
      const hex = colour.length === 4
        ? [...colour.slice(1)].map((c) => c + c).join("")
        : colour.slice(1);
      return [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
    }
    return colour.match(/[\d.]+/g).slice(0, 3).map(Number);
  }

  function alphaOf(colour) {
    const parts = colour.match(/[\d.]+/g);
    return colour.startsWith("rgba") && parts.length > 3 ? Number(parts[3]) : 1;
  }

  function over(top, bottom) {
    const a = alphaOf(top);
    const [tr, tg, tb] = channels(top);
    const [br, bg, bb] = channels(bottom);
    return [
      tr * a + br * (1 - a),
      tg * a + bg * (1 - a),
      tb * a + bb * (1 - a),
    ];
  }

  function luminance([r, g, b]) {
    const channel = (value) => {
      const v = value / 255;
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
  }

  function contrast(fg, bg) {
    const [light, dark] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
    return (light + 0.05) / (dark + 0.05);
  }

  const CARD = "#16213e";

  it("keeps minor-ring numbers legible on a compatible wedge", () => {
    const { sectorsEl, labelsEl } = styled();
    const compatWedge = sectorsEl.querySelector(".sector.compat");
    expect(compatWedge).not.toBeNull();

    const wedge = over(fillOf(compatWedge), CARD);
    const minor = labelsEl.querySelector(".label.minor:not(.active)");
    const measured = contrast(channels(fillOf(minor)), wedge);

    // --text-dim measured 3.33:1 here, under the 4.5:1 floor.
    expect(measured).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps minor-ring numbers legible on an idle wedge", () => {
    const { sectorsEl, labelsEl } = styled();
    const idle = sectorsEl.querySelector(".sector:not(.compat):not(.active)");
    const wedge = over(fillOf(idle), CARD);
    const minor = labelsEl.querySelector(".label.minor:not(.active)");

    expect(contrast(channels(fillOf(minor)), wedge)).toBeGreaterThanOrEqual(4.5);
  });

  it("keeps the active number legible on the lit wedge", () => {
    const { sectorsEl, labelsEl } = styled();
    const lit = sectorsEl.querySelector(".sector.active");
    const active = labelsEl.querySelector(".label.active");

    expect(fillOf(active)).toBe("#000");
    expect(contrast(channels(fillOf(active)), over(fillOf(lit), CARD)))
      .toBeGreaterThanOrEqual(4.5);
  });

  it("lets the active rule win on specificity, not source order", () => {
    // 8A is a minor-ring label AND the active one.  Move the minor rule
    // after the active rule: if the cascade depended on order, the number
    // would go back to the ring colour and vanish on the lit wedge.
    const minorRule = "#camelot-wheel .label.minor:not(.active) { font-weight: 400; }";
    const activeRule = "#camelot-wheel .label.active { fill: #000; font-weight: 700; }";
    expect(css).toContain(minorRule);
    const reordered = css.replace(minorRule, "") + `
${minorRule}
`;
    expect(reordered.indexOf(minorRule)).toBeGreaterThan(reordered.indexOf(activeRule));

    const { labelsEl } = styled(reordered);
    const active = labelsEl.querySelector(".label.active");

    expect(active.getAttribute("data-cell")).toBe("8A");
    expect(active.classList.contains("minor")).toBe(true);
    expect(fillOf(active)).toBe("#000");
  });
});
