// Settings > Stream > Broadcast recording.  All requests use the paired API;
// the recordings folder is config-only and never reaches the page.
//
// The status and error lines are plain text, refreshed by every state push.
// Results of a press, and a new or cleared recording error, are spoken once
// through the status live region.
import { requestJson, withDisabled } from "./api-client.js";
import { announceStatus } from "./live-region.js";

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
  const split = doc.getElementById("recording-split");
  const retention = doc.getElementById("recording-retention-days");
  const freeSpace = doc.getElementById("recording-free-space-mb");
  const save = doc.getElementById("recording-save");
  let current = null;
  let lastError = null;

  function say(message, tone = "info") {
    announceStatus(liveRegion, message, { force: true, dwellMs: 6000, tone });
  }

  function apply(value) {
    if (!value) return;
    group.hidden = false;
    const wasRunning = Boolean(current?.running);
    current = value;
    // A fixed name: the pressed state alone says whether it records.
    toggle.setAttribute("aria-pressed", String(Boolean(value.running)));
    splitNow.setAttribute("aria-disabled", String(!value.running));
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
    if (doc.activeElement !== split) split.value = value.split;
    if (doc.activeElement !== retention) {
      retention.value = String(value.retention_days);
      retention.removeAttribute("aria-invalid");
    }
    if (doc.activeElement !== freeSpace) {
      freeSpace.value = String(value.free_space_mb);
      freeSpace.removeAttribute("aria-invalid");
    }
  }

  toggle.addEventListener("click", () => withDisabled(toggle, async () => {
    try {
      const value = await requestJson(
        current?.running ? "/api/recording/stop" : "/api/recording/start",
        { method: "POST" },
      );
      apply(value);
      say(value.running ? "Recording started." : "Recording stopped.");
    } catch (failure) {
      say(`Could not change recording: ${failure.message}`, "error");
    }
  }));

  splitNow.addEventListener("click", () => withDisabled(splitNow, async () => {
    // aria-disabled keeps the button focusable, so the press is answered.
    if (!current?.running) {
      say("Recording is stopped; start it first.");
      return;
    }
    try {
      apply(await requestJson("/api/recording/split", { method: "POST" }));
      say("Recording split. The next audio starts a new file.");
    } catch (failure) {
      say(`Could not split recording: ${failure.message}`, "error");
    }
  }));

  function wholeNumber(input) {
    const text = input.value.trim();
    const number = Number(text);
    const valid = text !== "" && Number.isInteger(number) && number >= 0;
    if (valid) input.removeAttribute("aria-invalid");
    else input.setAttribute("aria-invalid", "true");
    return valid ? number : null;
  }

  save.addEventListener("click", () => withDisabled(save, async () => {
    const days = wholeNumber(retention);
    const floor = wholeNumber(freeSpace);
    if (days === null || floor === null) {
      say("Days to keep recordings and minimum free space must be whole numbers, 0 or more.",
        "error");
      (days === null ? retention : freeSpace).focus();
      return;
    }
    try {
      apply(await requestJson("/api/recording/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ split: split.value, retention_days: days, free_space_mb: floor }),
      }));
      say("Recording settings saved.");
    } catch (failure) {
      say(`Could not save recording settings: ${failure.message}`, "error");
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
