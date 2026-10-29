// Shared-library similarity limit. Longer songs retain their selected audio
// preparation; 0 permits model analysis at every length.
import { registerSettingsGroup } from "./settings-drafts.js";

const MAX_ADVANCED_MINUTES = 600;

// The message for a value the server would refuse, or null.
export function advancedLengthProblem(text) {
  const value = String(text ?? "").trim();
  if (!/^\d+$/.test(value) || Number(value) > MAX_ADVANCED_MINUTES) {
    return `Enter a whole number of minutes from 0 to ${MAX_ADVANCED_MINUTES}; 0 means no limit.`;
  }
  return null;
}

// *isCurrent()* is false when the session has ended or the panel was cleared.
export function installAdvancedLength({ document: doc, request, isCurrent = () => true }) {
  const input = doc.getElementById("advanced-length");
  const help = doc.getElementById("advanced-length-help");
  const error = doc.getElementById("advanced-length-error");
  if (!input || !error) return null;
  const helpId = help?.id || "";
  let loaded = 0;

  function clearError() {
    input.removeAttribute("aria-invalid");
    if (helpId) input.setAttribute("aria-describedby", helpId);
    else input.removeAttribute("aria-describedby");
    error.textContent = "";
    error.hidden = true;
  }

  // The message under the field, linked to it and read when focus moves there.
  function showError(message) {
    error.textContent = message;
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("aria-describedby", [error.id, helpId].filter(Boolean).join(" "));
    input.focus();
  }

  function show(value) {
    const minutes = Number(value?.minutes);
    input.value = Number.isInteger(minutes) && minutes >= 0 ? String(minutes) : "15";
  }

  async function load() {
    const ticket = ++loaded;
    try {
      const value = await request("/api/library/advanced-length");
      if (ticket === loaded && isCurrent()) draft.accept({ minutes: Number(value?.minutes) });
    } catch {
      // The field keeps what it shows; Save reports any problem.
    }
  }

  input.addEventListener("input", () => {
    if (!error.hidden && !advancedLengthProblem(input.value)) clearError();
  });
  const groupPanel = input.closest('[role="tabpanel"]') || doc.getElementById("panel-library") || input.parentElement;
  const draft = registerSettingsGroup({
    panel: groupPanel,
    scope: "server",
    controls: [input],
    read: () => ({ minutes: Number(input.value) }),
    write: (value) => show(value),
    validate: () => {
      const problem = advancedLengthProblem(input.value);
      if (problem) { showError(problem); return false; }
      clearError();
      return true;
    },
    describe: (value) => value.minutes === 0
      ? "Similarity analysis has no length limit."
      : `Songs longer than ${value.minutes} minutes get audio analysis only.`,
    commit: (value) => request("/api/library/advanced-length", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes: value.minutes }),
    }),
  });
  clearError();
  return {
    load,
    clear() {
      loaded += 1;
      clearError();
      input.value = "15";
    },
  };
}
