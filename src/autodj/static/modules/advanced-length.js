// Shared-library similarity limit. Longer songs retain their selected audio
// preparation; 0 permits model analysis at every length.
import { clearFieldError, showFieldError } from "./dom-helpers.js";
import { registerSettingsGroup } from "./settings-drafts.js";
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";

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
function libraryUrl(url, libraryId) {
  return `${url}${url.includes("?") ? "&" : "?"}library=${encodeURIComponent(libraryId || "main")}`;
}

export function installAdvancedLength({ document: doc, request, getLibrary = () => "main", getLibraryName = getLibrary, isCurrent = () => true }) {
  const input = doc.getElementById("advanced-length");
  const error = doc.getElementById("advanced-length-error");
  if (!input || !error) return null;
  let loaded = 0;

  const clearError = () => clearFieldError(input, error);
  const showError = (message) => showFieldError(input, message, error);

  function show(value) {
    const minutes = Number(value?.minutes);
    input.value = Number.isInteger(minutes) && minutes >= 0 ? String(minutes) : "15";
  }

  async function load({ libraryId = getLibrary(), accept = true } = {}) {
    const ticket = ++loaded;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const value = await request(libraryUrl("/api/library/advanced-length", libraryId));
      if (ticket !== loaded || !isAuthenticatedRequestCurrent(epoch) || !isCurrent()) return null;
      const loadedValue = { minutes: Number(value?.minutes) };
      if (accept) {
        if (getLibrary() !== libraryId) return null;
        draft.accept(loadedValue);
      }
      return loadedValue;
    } catch (error) {
      // The field keeps what it shows; Save reports any problem.
      if (ticket === loaded && isAuthenticatedRequestCurrent(epoch) && isCurrent()) throw error;
      return null;
    }
  }

  input.addEventListener("input", () => {
    if (!error.hidden && !advancedLengthProblem(input.value)) clearError();
  });
  const groupPanel = input.closest('[role="tabpanel"]') || doc.getElementById("panel-library") || input.parentElement;
  const draft = registerSettingsGroup({
    panel: groupPanel,
    loadSettings: false,
    scope: "library",
    scopeName: () => `Library: ${getLibraryName()}`,
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
    commit: (value) => request(libraryUrl("/api/library/advanced-length", getLibrary()), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes: value.minutes }),
    }),
  });
  clearError();
  return {
    load,
    accept(value, libraryId) {
      if (getLibrary() !== libraryId || !isCurrent()) return false;
      draft.accept(value);
      return true;
    },
    clear() {
      loaded += 1;
      clearError();
      input.value = "15";
    },
  };
}
