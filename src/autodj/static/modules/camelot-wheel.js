// Camelot wheel -- decorative visual aid for harmonic mixing.
//
// AT users get key info via #badges-announce; the SVG is aria-hidden.
// Layout: 12 sectors around a circle, each sector split into outer
// (B = major) and inner (A = minor) ring.  Numbers 1..12 run clockwise
// starting at 12 o'clock.  Each sector path has data-cell="<n><A|B>".

let _built = false;

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
  if (_built || !sectorsEl) return;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const sweep = 360 / 12;        // 30 deg per slot
  const rings = [
    { side: "B", rIn: 70, rOut: 100, labelR: 85 },
    { side: "A", rIn: 40, rOut: 70,  labelR: 55 },
  ];
  for (let n = 1; n <= 12; n++) {
    const a1 = (n - 1) * sweep - sweep / 2;
    const a2 = n * sweep - sweep / 2;
    for (const r of rings) {
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", _arcPath(r.rIn, r.rOut, a1, a2));
      path.setAttribute("class", "sector");
      path.setAttribute("data-cell", `${n}${r.side}`);
      sectorsEl.appendChild(path);
    }
    const [lx, ly] = _polar(78, (n - 0.5) * sweep);
    const lab = document.createElementNS(SVG_NS, "text");
    lab.setAttribute("x", lx.toFixed(2));
    lab.setAttribute("y", ly.toFixed(2));
    lab.setAttribute("class", "label");
    lab.setAttribute("data-num", String(n));
    lab.textContent = String(n);
    labelsEl.appendChild(lab);
  }
  _built = true;
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
  const activeNum = currentCell && currentCell !== "--"
    ? currentCell.replace(/[AB]$/, "")
    : null;
  for (const lab of labelsEl.querySelectorAll(".label")) {
    lab.classList.toggle("active", lab.getAttribute("data-num") === activeNum);
  }
}
