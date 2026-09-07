// Camelot wheel -- decorative visual aid for harmonic mixing.
//
// AT users get key info via #badges-announce; the SVG is aria-hidden.
// Layout: 12 sectors around a circle, each sector split into outer
// (B = major) and inner (A = minor) ring.  Numbers 1..12 run clockwise
// starting at 12 o'clock.  Each sector path has data-cell="<n><A|B>".

const _built = new WeakSet();

function _polar(r, angleDeg) {
  // 0deg = 12 o'clock, clockwise
  const a = (angleDeg - 90) * Math.PI / 180;
  return [r * Math.cos(a), r * Math.sin(a)];
}

function _arcPath(r1, r2, a1, a2) {
  // Annular wedge from radius r1 (inner) to r2 (outer), spanning
  // angles a1..a2.
  const [x1o, y1o] = _polar(r2, a1);
  const [x2o, y2o] = _polar(r2, a2);
  const [x1i, y1i] = _polar(r1, a2);
  const [x2i, y2i] = _polar(r1, a1);
  const large = Math.abs(a2 - a1) > 180 ? 1 : 0;
  return `M ${x1o.toFixed(2)} ${y1o.toFixed(2)} `
       + `A ${r2} ${r2} 0 ${large} 1 ${x2o.toFixed(2)} ${y2o.toFixed(2)} `
       + `L ${x1i.toFixed(2)} ${y1i.toFixed(2)} `
       + `A ${r1} ${r1} 0 ${large} 0 ${x2i.toFixed(2)} ${y2i.toFixed(2)} Z`;
}

function _build(sectorsEl, labelsEl) {
  if (!sectorsEl || _built.has(sectorsEl)) return;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const sweep = 360 / 12;        // 30 deg per slot
  const rings = [
    { side: "B", rIn: 70, rOut: 100, labelR: 85 },
    { side: "A", rIn: 40, rOut: 70,  labelR: 55 },
  ];
  for (let n = 1; n <= 12; n++) {
    // Sector n spans (n-1)*30 - 15 .. n*30 - 15, so its CENTRE is at
    // (n-1)*30.  The label used to be placed at (n-0.5)*30, i.e. exactly
    // on the boundary with the next wedge, which put every number half a
    // sector clockwise of the wedge it names.
    const centre = (n - 1) * sweep;
    const a1 = centre - sweep / 2;
    const a2 = centre + sweep / 2;
    for (const r of rings) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", _arcPath(r.rIn, r.rOut, a1, a2));
      path.setAttribute("class", "sector");
      path.setAttribute("data-cell", `${n}${r.side}`);
      sectorsEl.appendChild(path);
      if (!labelsEl) continue;
      // One number per ring: the inner A/minor ring was previously
      // unlabelled, and a single shared number could not be highlighted
      // legibly because it might sit over an unlit wedge.
      const [lx, ly] = _polar(r.labelR, centre);
      const lab = document.createElementNS(SVG_NS, "text");
      lab.setAttribute("x", lx.toFixed(2));
      lab.setAttribute("y", ly.toFixed(2));
      lab.setAttribute("class", r.side === "A" ? "label minor" : "label");
      lab.setAttribute("data-cell", `${n}${r.side}`);
      lab.setAttribute("data-num", String(n));
      lab.textContent = String(n);
      labelsEl.appendChild(lab);
    }
  }
  if (labelsEl) {
    // Centre readout: names the current cell outright, which is also the
    // wheel's only A/B legend.
    const current = document.createElementNS(SVG_NS, "text");
    current.setAttribute("id", "camelot-current");
    current.setAttribute("x", "0");
    current.setAttribute("y", "-4");
    current.setAttribute("font-size", "18");
    current.setAttribute("class", "current-cell");
    current.textContent = "—";
    labelsEl.appendChild(current);
    const legend = document.createElementNS(SVG_NS, "text");
    legend.setAttribute("x", "0");
    legend.setAttribute("y", "14");
    legend.setAttribute("font-size", "8");
    legend.setAttribute("class", "ring-legend");
    legend.textContent = "outer B · inner A";
    labelsEl.appendChild(legend);
  }
  _built.add(sectorsEl);
}

// Camelot adjacency rules.  Mirrors dj_meta.harmonic_compatible on the
// Python side.  Returns the set of cell labels (e.g. "8A") considered
// compatible with `current` under the chosen mode.  current is
// "8A" | "8B" etc; mode is one of off / compatible / strict /
// neighbour / mood_change / energy_boost.
function _compatibleSet(current, mode) {
  const out = new Set();
  if (!current || current === "--") return out;
  const m = /^(\d{1,2})([AB])$/.exec(current);
  if (!m) return out;
  const num = parseInt(m[1], 10);
  const side = m[2];
  const wrap = (k) => ((k - 1 + 12) % 12) + 1;
  out.add(`${num}${side}`);
  if (mode === "off" || mode === "strict") return out;
  if (mode === "mood_change") {
    out.add(`${num}${side === "A" ? "B" : "A"}`);
    return out;
  }
  if (mode === "neighbour") {
    out.add(`${wrap(num - 1)}${side}`);
    out.add(`${wrap(num + 1)}${side}`);
    return out;
  }
  if (mode === "energy_boost") {
    out.add(`${wrap(num - 2)}${side}`);
    out.add(`${wrap(num + 2)}${side}`);
    return out;
  }
  // default: "compatible" -- adjacent same side plus relative major/minor.
  out.add(`${wrap(num - 1)}${side}`);
  out.add(`${wrap(num + 1)}${side}`);
  out.add(`${num}${side === "A" ? "B" : "A"}`);
  return out;
}

export function applyCamelotWheel(currentCell, harmonicMode, { sectorsEl, labelsEl }) {
  if (!sectorsEl) return;
  _build(sectorsEl, labelsEl);
  const compat = _compatibleSet(currentCell, harmonicMode || "compatible");
  for (const sec of sectorsEl.querySelectorAll(".sector")) {
    const cell = sec.getAttribute("data-cell");
    const isActive = cell === currentCell;
    sec.classList.toggle("active", isActive);
    sec.classList.toggle("compat", !isActive && compat.has(cell));
  }
  if (!labelsEl) return;
  for (const lab of labelsEl.querySelectorAll(".label")) {
    // Match the exact cell, so the highlighted number is always the one
    // drawn on the lit wedge -- black on --accent, 9.5:1.  Matching only
    // the number lit an unlit neighbour instead, where #000 measured
    // 1.43:1 and the current key was effectively invisible.
    lab.classList.toggle("active", lab.getAttribute("data-cell") === currentCell);
  }
  const current = labelsEl.querySelector("#camelot-current");
  if (current) {
    const text = currentCell && currentCell !== "--" ? currentCell : "—";
    if (current.textContent !== text) current.textContent = text;
  }
}
