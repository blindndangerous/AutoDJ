// Camelot wheel geometry: every number must sit inside its own wedge,
// and the number for the current key must be readable on the lit wedge.

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

describe("camelot wheel labels", () => {
  let els;
  beforeEach(() => { els = wheel(); });

  it("centres every number on its own sector, not on the boundary", () => {
    applyCamelotWheel("8A", "compatible", els);

    for (let n = 1; n <= 12; n++) {
      const expected = ((n - 1) * 30 + 360) % 360;
      for (const side of ["A", "B"]) {
        const label = labelFor(els.labelsEl, `${n}${side}`);
        expect(label, `${n}${side}`).not.toBeNull();
        const offBy = Math.abs(((bearing(label) - expected + 540) % 360) - 180);
        // Within a degree of the sector centre, i.e. 15 deg away from
        // both boundaries rather than sitting on one of them.
        expect(offBy, `${n}${side} bearing`).toBeLessThan(1);
      }
    }
  });

  it("labels both rings, each inside its own ring", () => {
    applyCamelotWheel(null, "compatible", els);

    expect(els.labelsEl.querySelectorAll(".label")).toHaveLength(24);
    for (let n = 1; n <= 12; n++) {
      const outer = radius(labelFor(els.labelsEl, `${n}B`));
      const inner = radius(labelFor(els.labelsEl, `${n}A`));
      expect(outer).toBeGreaterThan(70);
      expect(outer).toBeLessThan(100);
      expect(inner).toBeGreaterThan(40);
      expect(inner).toBeLessThan(70);
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
