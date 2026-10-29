import { copyByCommand } from "./clipboard.js";
// Settings > Stream > More formats (#50): the AAC and Opus streams'
// quality, and the address of every format that is on.
//
// Speech rules:
//   - A select is never rewritten while it has an unsaved draft.
//   - Save changes says what each changed format now does, after the
//     tab's result.
//   - The address list is plain text, rebuilt only when its text changes,
//     so reading it is not disturbed by the once-a-second push.
//   - Copy is a user action: its confirmation is forced, and focus stays
//     on the button.
//   - A push whose saved bitrates differ from the list's asks for the
//     addresses again, once, so a saved format is listed without a reload.

import { announceStatus } from "./live-region.js";
import { registerSettingsGroup } from "./settings-drafts.js";

const FORMATS = ["aac", "opus"];

// "AAC, 96 kbps: http://host/stream.aac", and what its encoder last
// said on stderr while it runs, if anything.
export function formatLine(item, origin) {
  const line = `${item.label}, ${item.bitrate} kbps: ${origin}${item.path}`;
  return item.error ? `${line}. Its encoder last said: ${item.error}` : line;
}

// What a save did, in words.
function formatSavedText(label, bitrate) {
  return bitrate ? `${label} stream on at ${bitrate} kbps.` : `${label} stream off.`;
}

async function copyText(doc, text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    return copyByCommand(text, doc);
  }
}

export function installStreamFormats({
  doc, srStatus, refresh = () => {},
}) {
  const list = doc.getElementById("stream-formats-list");
  const intro = doc.getElementById("stream-formats-intro");
  const selects = Object.fromEntries(FORMATS.map((name) => [
    name, doc.getElementById(`stream-${name}-bitrate`),
  ]));
  const labels = { aac: "AAC", opus: "Opus" };
  const settingsDrafts = Object.fromEntries(FORMATS.map((name) => [name, registerSettingsGroup({
    panel: doc.getElementById("spanel-stream"),
    group: "stream",
    controls: [selects[name]],
    read: () => ({ [`${name}_bitrate`]: Number(selects[name].value) }),
    write: (body) => {
      if (body[`${name}_bitrate`] != null) selects[name].value = String(body[`${name}_bitrate`]);
    },
    describe: (body) => formatSavedText(labels[name], body[`${name}_bitrate`]),
  })]));

  // The bitrates the list was drawn from, and the pushed ones a lookup was
  // last asked for.
  let shown = null;
  let asked = null;
  const bitrates = (bitrateOf) => FORMATS.map((name) => Number(bitrateOf(name) || 0)).join();

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
      if (settingsDrafts[item.format]) settingsDrafts[item.format].accept({
        [`${item.format}_bitrate`]: item.bitrate,
      });
    }
    shown = bitrates((name) => items.find((item) => item.format === name)?.bitrate);
    showList(items);
  }

  // The once-a-second push carries the saved bitrates.
  function apply(state) {
    const playback = state?.settings?.playback;
    if (!playback) return;
    for (const name of FORMATS) {
      settingsDrafts[name].accept({ [`${name}_bitrate`]: playback[`stream_${name}_bitrate`] });
    }
    const pushed = bitrates((name) => playback[`stream_${name}_bitrate`]);
    if (shown !== null && pushed !== shown && pushed !== asked) {
      asked = pushed;
      refresh();
    }
  }

  function reset() {
    shown = null;
    asked = null;
    for (const name of FORMATS) {
      selects[name].value = "0";
    }
    list.replaceChildren();
    intro.hidden = true;
    list.hidden = true;
  }

  reset();
  return { fill, apply, reset };
}
