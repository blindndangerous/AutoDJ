// Settings > Stream > Broadcast recording.  All requests use the paired API;
// the recordings folder is config-only and never reaches the page.
//
// The status and error lines are plain text, refreshed by every state push.
// Results of a press, and a new or cleared recording error, are spoken once
// through the status live region.
import { failureText, requestJson, withDisabled } from "./api-client.js";
import { clearFieldError, showFieldError } from "./dom-helpers.js";
import { makeSay } from "./live-region.js";
import { registerSettingsGroup } from "./settings-drafts.js";

function formatSize(bytes) {
  const megabytes = (Number(bytes) || 0) / (1024 * 1024);
  return `${megabytes.toFixed(1)} MB`;
}

export function createRecordingControls(doc, liveRegion) {
  const group = doc.getElementById("recording-controls");
  if (!group) return { apply() {}, reset() {} };
  const toggle = doc.getElementById("recording-toggle");
  const splitNow = doc.getElementById("recording-split-now");
  const status = doc.getElementById("recording-status");
  const error = doc.getElementById("recording-error");
  const settingsError = doc.getElementById("recording-settings-error");
  const split = doc.getElementById("recording-split");
  const retention = doc.getElementById("recording-retention-days");
  const freeSpace = doc.getElementById("recording-free-space-mb");
  let current = null;
  let lastError = null;

  const say = makeSay(liveRegion, { dwellMs: 6000 });

  function apply(value) {
    if (!value) return;
    group.hidden = false;
    const wasRunning = Boolean(current?.running);
    current = value;
    const available = value.available !== false;
    // A fixed name: the pressed state alone says whether it records.
    toggle.setAttribute("aria-pressed", String(Boolean(value.running)));
    toggle.setAttribute("aria-disabled", String(!available));
    splitNow.setAttribute("aria-disabled", String(!available || !value.running));
    if (value.file) {
      status.textContent = `Recording to ${value.file}, ${formatSize(value.bytes)} so far.`;
    } else if (value.running) {
      status.textContent = "Recording, waiting for audio.";
    } else {
      status.textContent = "Not recording.";
    }
    error.textContent = value.error ? `Recording error: ${value.error}` : "";
    error.hidden = !value.error;
    if (value.error && value.error !== lastError) say(error.textContent, "error");
    else if (!value.error && lastError && value.running && wasRunning) say("Recording resumed.");
    lastError = value.error || null;
    draft.accept({ split: value.split, retention_days: value.retention_days,
      free_space_mb: value.free_space_mb });
  }

  function write(value) {
    group.hidden = false;
    if (value.split != null) split.value = value.split;
    if (value.retention_days != null) retention.value = String(value.retention_days);
    if (value.free_space_mb != null) freeSpace.value = String(value.free_space_mb);
    clearFieldError([retention, freeSpace], settingsError);
  }
  function readWholeNumber(input) {
    const text = input.value.trim();
    const number = Number(text);
    return text !== "" && Number.isInteger(number) && number >= 0 ? number : null;
  }
  const draft = registerSettingsGroup({
    panel: doc.getElementById("spanel-stream"), group: "recording",
    controls: [split, retention, freeSpace],
    fields: { split, retention_days: retention, free_space_mb: freeSpace },
    read: () => ({ split: split.value, retention_days: readWholeNumber(retention),
      free_space_mb: readWholeNumber(freeSpace) }),
    write,
    validate() {
      clearFieldError([retention, freeSpace], settingsError);
      const days = readWholeNumber(retention);
      const floor = readWholeNumber(freeSpace);
      if (days !== null && floor !== null) return true;
      showFieldError([days === null && retention, floor === null && freeSpace].filter(Boolean),
        "Days to keep recordings and minimum free space must be whole numbers, 0 or more.", settingsError, liveRegion);
      return false;
    },
  });

  toggle.addEventListener("click", () => withDisabled(toggle, async () => {
    if (current?.available === false) {
      say("Recording is available only while stream mode is active.");
      return;
    }
    try {
      const value = await requestJson(
        current?.running ? "/api/recording/stop" : "/api/recording/start",
        { method: "POST" },
      );
      apply(value);
      say(value.running ? "Recording started." : "Recording stopped.");
    } catch (failure) {
      say(failureText("change recording", failure), "error");
    }
  }));

  splitNow.addEventListener("click", () => withDisabled(splitNow, async () => {
    // aria-disabled keeps the button focusable, so the press is answered.
    if (current?.available === false) {
      say("Recording is available only while stream mode is active.");
      return;
    }
    if (!current?.running) {
      say("Recording is stopped; start it first.");
      return;
    }
    try {
      apply(await requestJson("/api/recording/split", { method: "POST" }));
      say("Recording split. The next audio starts a new file.");
    } catch (failure) {
      say(failureText("split recording", failure), "error");
    }
  }));

  function reset() {
    group.hidden = true;
    current = null;
    lastError = null;
    status.textContent = "";
    error.textContent = "";
    error.hidden = true;
  }

  return { apply, reset };
}
