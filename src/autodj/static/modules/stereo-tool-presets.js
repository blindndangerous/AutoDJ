// Stereo Tool presets in Settings, Sound: choose the active preset, upload
// a .sts file and delete an uploaded one.
//
// The list loads when the Sound panel is shown (subtabs.js gives it the
// open and toggle of a card).  Every result is spoken through the shared
// settings status region and mirrored in the visible toast, and focus is
// put where the next step is: the preset list after an upload or a delete,
// the file field when the file is missing or refused.  Included is the
// bundled preset; it is always listed first and cannot be deleted.

import { requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";

const INCLUDED = "included";
const MAX_BYTES = 2 * 1024 * 1024;

export function installStereoToolPresets({ document: doc = document, request = requestJson,
  confirm = confirmAction, status }) {
  const panel = doc.getElementById("spanel-sound");
  const select = doc.getElementById("stereo-preset");
  const file = doc.getElementById("stereo-preset-file");
  const upload = doc.getElementById("stereo-preset-upload");
  const remove = doc.getElementById("stereo-preset-delete");
  if (!panel || !select || !file || !upload || !remove) return null;
  const speak = (message, error = false) => announceStatus(status, message,
    { force: true, dwellMs: 8000, tone: error ? "error" : "info" });
  let active = INCLUDED;
  let uploading = false;
  let selectionDirty = false;
  const labelOf = (name) =>
    [...select.options].find((option) => option.value === name)?.textContent || name;
  // Delete stays enabled while its confirmation is open, so Cancel can
  // put focus back on it.
  const syncDelete = () => { remove.disabled = select.value === INCLUDED; };

  async function refresh() {
    const data = await request("/api/stereo-tool-presets");
    const selected = select.value;
    active = data.active;
    select.replaceChildren(...data.presets.map((preset) => {
      const option = doc.createElement("option");
      option.value = preset.name;
      option.textContent = preset.label;
      return option;
    }));
    select.value = selectionDirty
      && [...select.options].some((option) => option.value === selected) ? selected : active;
    syncDelete();
  }

  const loadFailed = (error) =>
    speak(`Could not load Stereo Tool presets: ${error.message}`, true);
  panel.addEventListener("toggle", () => {
    if (panel.open) void refresh().catch(loadFailed);
  });
  if (panel.open) void refresh().catch(loadFailed);

  const choose = async (name) => {
    const current = await request("/api/settings");
    const result = await request("/api/settings", { method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revision: current.revision, changes: { playback: { stereotool_preset: name } } }) });
    if (!result.applied.includes("playback")) throw new Error(result.failed.playback);
  };
  select.addEventListener("change", () => { selectionDirty = true; syncDelete(); });

  // Upload is not disabled while it runs: a disabled button loses focus,
  // and NVDA would fall back to the top of the page.  A second press is
  // ignored instead.
  upload.addEventListener("click", async () => {
    if (uploading) return;
    const chosen = file.files?.[0];
    const refuse = (message) => {
      speak(message, true);
      file.focus();
    };
    if (!chosen) {
      refuse("Choose a Stereo Tool preset file first.");
      return;
    }
    if (!chosen.name.toLowerCase().endsWith(".sts")) {
      refuse(`${chosen.name} is not a Stereo Tool preset. Choose a .sts file.`);
      return;
    }
    if (chosen.size > MAX_BYTES) {
      refuse(`${chosen.name} is larger than 2 MB.`);
      return;
    }
    const body = new FormData();
    body.append("file", chosen, chosen.name);
    uploading = true;
    try {
      await request("/api/stereo-tool-presets/upload", { method: "POST", body });
    } catch (error) {
      refuse(`Could not upload ${chosen.name}: ${error.message}`);
      return;
    } finally {
      uploading = false;
    }
    file.value = "";
    try {
      await refresh();
    } catch (error) {
      select.focus();
      speak(`Uploaded ${chosen.name}, but could not reload the presets: ${error.message}`, true);
      return;
    }
    select.focus();
    speak(`Uploaded ${chosen.name}. Choose it in the Preset list to use it.`);
  });

  remove.addEventListener("click", async () => {
    const name = select.value;
    if (name === INCLUDED) return;
    const label = labelOf(name);
    const wasActive = name === active;
    let result = null;
    const confirmed = await confirm(doc, {
      title: `Delete preset ${label}?`,
      message: wasActive
        ? "Stereo Tool switches to the Included preset first, then the file is removed."
        : "The preset file is removed from the station's data folder.",
      confirmLabel: "Delete preset",
      // Runs while the dialog is open; focus goes to the list as it closes.
      onConfirm: async () => {
        try {
          if (wasActive) {
            await choose(INCLUDED);
            active = INCLUDED;
          }
          await request(`/api/stereo-tool-presets/${encodeURIComponent(name)}`,
            { method: "DELETE" });
          selectionDirty = false;
          await refresh();
          result = [`Preset ${label} deleted. ${labelOf(active)} is active.`, false];
        } catch (error) {
          select.value = active;
          selectionDirty = false;
          syncDelete();
          result = [`Could not delete preset ${label}: ${error.message}`, true];
        }
        return select;
      },
    });
    if (confirmed && result) speak(...result);
  });

  syncDelete();
  return {
    refresh,
    reset: () => { active = INCLUDED; selectionDirty = false; select.value = INCLUDED; syncDelete(); },
    setActive: (name) => {
      active = name;
      if (select.value === name) selectionDirty = false;
      syncDelete();
    },
  };
}
