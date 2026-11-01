import { failureText } from "./api-client.js";
import { clearFieldError, showFieldError, applyServerFieldError } from "./dom-helpers.js";
import { createSettingsSave } from "./standalone-settings.js";
// Library tools, Automatic rescan every: hours and minutes (#249).  The
// server runs the same scan as the Rescan button once that long has passed
// since the last scan ended; 0 and 0 turns it off.
const MAX_HOURS = 168;
const MAX_MINUTES = 59;
const MIN_TOTAL_MINUTES = 5;
const SHORT_MESSAGE = `Use at least ${MIN_TOTAL_MINUTES} minutes, or 0 to turn it off.`;

const whole = (text, max) => /^\d+$/.test(String(text).trim()) && Number(text) <= max;

// [which field, message] for the first value the server would refuse, or null.
export function autoRescanProblem(hours, minutes) {
  if (!whole(hours, MAX_HOURS)) return ["hours", `Hours must be a whole number from 0 to ${MAX_HOURS}.`];
  if (!whole(minutes, MAX_MINUTES)) return ["minutes", `Minutes must be a whole number from 0 to ${MAX_MINUTES}.`];
  const total = Number(hours) * 60 + Number(minutes);
  if (total > 0 && total < MIN_TOTAL_MINUTES) return ["minutes", SHORT_MESSAGE];
  return null;
}

// *say(message, tone)* writes the Library tools status region; *isCurrent()*
// is false once the session has ended or the panel was cleared.  Returns
// null when the page has no such fields.
export function installAutoRescan({ document: doc, request, say, isCurrent = () => true }) {
  const hours = doc.getElementById("auto-rescan-hours");
  const minutes = doc.getElementById("auto-rescan-minutes");
  const save = doc.getElementById("auto-rescan-save");
  const error = doc.getElementById("auto-rescan-error");
  if (!hours || !minutes || !save || !error) return null;
  const fields = { hours, minutes };
  let loaded = 0;

  const clearError = () => clearFieldError([hours, minutes], error);
  const showError = (field, message) => {
    clearError();
    showFieldError(field, message, error);
  };

  function show(value) {
    hours.value = String(value?.hours ?? 0);
    minutes.value = String(value?.minutes ?? 0);
  }

  async function load() {
    const ticket = ++loaded;
    try {
      const value = await request("/api/library/auto-rescan");
      if (ticket === loaded && isCurrent()) show(value);
    } catch {
      // The fields keep what they show; Save reports any problem.
    }
  }

  for (const field of [hours, minutes]) {
    field.addEventListener("input", () => {
      if (field.getAttribute("aria-invalid") && !autoRescanProblem(hours.value, minutes.value)) clearError();
    });
  }

  const saver = createSettingsSave({ button: save, isCurrent, say, save: async (current) => {
    const problem = autoRescanProblem(hours.value, minutes.value);
    if (problem) { showError(fields[problem[0]], problem[1]); return; }
    clearError();
    loaded += 1;
    const body = { hours: Number(hours.value), minutes: Number(minutes.value) };
    try {
      const value = await request("/api/library/auto-rescan", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!current()) return;
      show(value);
      say("Saved.");
    } catch (err) {
      if (!current()) return;
      if (err?.status === 400) applyServerFieldError({ field: "minutes", message: err.message || SHORT_MESSAGE }, fields, error);
      else say(failureText("save automatic rescan", err), "error");
    }
  } });
  save.addEventListener("click", saver.run);

  return {
    load,
    clear() {
      saver.clear();
      loaded += 1;
      clearError();
      show(null);
    },
  };
}
