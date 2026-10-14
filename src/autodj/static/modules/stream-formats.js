// Settings > Stream > More formats (#50): the AAC and Opus streams'
// quality, and the address of every format that is on.
//
// Speech rules:
//   - A quality select saves once the choice settles: arrowing through a
//     closed select fires change on every step, and each save restarts
//     that format's encoder and drops its listeners.  The result is said
//     once in the settings status region.
//   - A select is never rewritten while it has focus or while its save is
//     out.
//   - The address list is plain text, rebuilt only when its text changes,
//     so reading it is not disturbed by the once-a-second push.
//   - Copy is a user action: its confirmation is forced, and focus stays
//     on the button.

import { announceStatus } from "./live-region.js";

export const FORMAT_SETTLE_MS = 600;
const FORMATS = ["aac", "opus"];

// "AAC, 96 kbps: http://host/stream.aac", and what its encoder last
// said on stderr while it runs, if anything.
export function formatLine(item, origin) {
  const line = `${item.label}, ${item.bitrate} kbps: ${origin}${item.path}`;
  return item.error ? `${line}. Its encoder last said: ${item.error}` : line;
}

// What a save did, in words.
export function formatSavedText(label, bitrate) {
  return bitrate ? `${label} stream on at ${bitrate} kbps.` : `${label} stream off.`;
}

async function copyText(doc, text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    // Plain http has no clipboard API; the older copy command still works.
    let wrote = false;
    const onCopy = (event) => {
      if (!event.clipboardData) return;
      event.clipboardData.setData("text/plain", text);
      event.preventDefault();
      wrote = true;
    };
    doc.addEventListener("copy", onCopy);
    try {
      return typeof doc.execCommand === "function" && doc.execCommand("copy") === true && wrote;
    } catch (_err) {
      return false;
    } finally {
      doc.removeEventListener("copy", onCopy);
    }
  }
}

// save(name, bitrate, control) -> Promise<boolean>; refresh() asks the
// server for the stream details again, which come back through fill().
export function installStreamFormats({
  doc, save, refresh = () => {}, srStatus, settingsStatus,
}) {
  const list = doc.getElementById("stream-formats-list");
  const intro = doc.getElementById("stream-formats-intro");
  const selects = Object.fromEntries(FORMATS.map((name) => [
    name, doc.getElementById(`stream-${name}-bitrate`),
  ]));
  const labels = { aac: "AAC", opus: "Opus" };
  // Per format: the value the server last confirmed, the settle timer
  // and whether a save is out.
  const saved = Object.fromEntries(FORMATS.map((name) => [name, selects[name].value]));
  const timers = {};
  const saving = {};

  function busy(name) {
    return Boolean(saving[name] || timers[name]);
  }

  function showValue(name, value) {
    if (value == null || busy(name)) return;
    saved[name] = String(value);
    const select = selects[name];
    if (doc.activeElement !== select && select.value !== saved[name]) select.value = saved[name];
  }

  function showList(items) {
    const origin = doc.location.origin;
    const on = items.filter((item) => item.bitrate > 0);
    const lines = on.map((item) => formatLine(item, origin));
    const current = [...list.children].map((row) => row.dataset.line);
    if (lines.join("\n") !== current.join("\n")) {
      list.replaceChildren(...on.map((item, index) => {
        const row = doc.createElement("li");
        row.dataset.line = lines[index];
        const text = doc.createElement("span");
        text.textContent = `${lines[index]} `;
        const button = doc.createElement("button");
        button.type = "button";
        button.textContent = `Copy ${item.label} address`;
        button.addEventListener("click", async () => {
          const address = `${doc.location.origin}${item.path}`;
          if (await copyText(doc, address)) {
            announceStatus(srStatus, `${item.label} address copied.`, { dwellMs: 3000, force: true });
          } else {
            announceStatus(srStatus,
              `Could not copy. The ${item.label} address is ${address}`,
              { dwellMs: 8000, force: true, tone: "error" });
          }
        });
        row.append(text, button);
        return row;
      }));
    }
    const empty = lines.length === 0;
    if (intro.hidden !== empty) intro.hidden = empty;
    if (list.hidden !== empty) list.hidden = empty;
  }

  // The stream details from GET /api/stream (or a settings save).
  function fill(info) {
    const items = Array.isArray(info?.formats) ? info.formats : [];
    for (const item of items) {
      if (selects[item.format]) showValue(item.format, item.bitrate);
    }
    showList(items);
  }

  // The once-a-second push carries the saved bitrates.
  function apply(state) {
    const playback = state?.settings?.playback;
    if (!playback) return;
    for (const name of FORMATS) showValue(name, playback[`stream_${name}_bitrate`]);
  }

  function reset() {
    for (const name of FORMATS) {
      clearTimeout(timers[name]);
      timers[name] = null;
      saving[name] = false;
      selects[name].value = "0";
      saved[name] = "0";
    }
    list.replaceChildren();
    intro.hidden = true;
    list.hidden = true;
  }

  async function flush(name) {
    timers[name] = null;
    const select = selects[name];
    const chosen = select.value;
    if (chosen === saved[name] || saving[name]) return;
    saving[name] = true;
    let ok = false;
    try {
      ok = await save(name, Number(chosen), select);
    } finally {
      saving[name] = false;
    }
    if (ok) {
      saved[name] = chosen;
      announceStatus(settingsStatus, formatSavedText(labels[name], Number(chosen)),
        { dwellMs: 4000, force: true });
      refresh();
    } else if (select.value === chosen) {
      // postSettings has said why; the select shows what the server has.
      select.value = saved[name];
    }
    // A choice made while the save was out goes now.
    if (select.value !== saved[name] && !timers[name]) void flush(name);
  }

  for (const name of FORMATS) {
    selects[name].addEventListener("change", () => {
      clearTimeout(timers[name]);
      timers[name] = setTimeout(() => void flush(name), FORMAT_SETTLE_MS);
    });
  }
  reset();
  return { fill, apply, reset };
}
