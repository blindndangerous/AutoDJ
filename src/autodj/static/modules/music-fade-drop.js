// Per-library fade detection threshold for automatic Segue suggestions.
import { clearFieldError, showFieldError } from "./dom-helpers.js";
import { registerSettingsGroup } from "./settings-drafts.js";
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent } from "./api-client.js";

export function musicFadeDropProblem(text) {
  const value = String(text ?? "").trim();
  if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 30) {
    return "Enter a whole number from 1 to 30 dB.";
  }
  return null;
}

function libraryUrl(url, libraryId) {
  return `${url}${url.includes("?") ? "&" : "?"}library=${encodeURIComponent(libraryId || "main")}`;
}

export function installMusicFadeDrop({ document: doc, request, getLibrary = () => "main",
  getLibraryName = getLibrary, isCurrent = () => true }) {
  const input = doc.getElementById("music-fade-drop");
  const error = doc.getElementById("music-fade-drop-error");
  if (!input || !error) return null;
  let loaded = 0;

  const clearError = () => clearFieldError(input, error);
  const showError = (message) => showFieldError(input, message, error);
  function show(value) {
    const drop = Number(value?.drop_db);
    input.value = Number.isInteger(drop) && drop >= 1 && drop <= 30 ? String(drop) : "10";
  }
  async function load({ libraryId = getLibrary(), accept = true } = {}) {
    const ticket = ++loaded;
    const epoch = captureAuthenticatedRequestEpoch();
    try {
      const value = await request(libraryUrl("/api/library/music-fade", libraryId));
      if (ticket !== loaded || !isAuthenticatedRequestCurrent(epoch) || !isCurrent()) return null;
      const loadedValue = { drop_db: Number(value?.drop_db) };
      if (accept) {
        if (getLibrary() !== libraryId) return null;
        draft.accept(loadedValue);
      }
      return loadedValue;
    } catch (error) {
      if (ticket === loaded && isAuthenticatedRequestCurrent(epoch) && isCurrent()) throw error;
      return null;
    }
  }
  input.addEventListener("input", () => {
    if (input.getAttribute("aria-invalid") && !musicFadeDropProblem(input.value)) clearError();
  });
  const panel = input.closest('[role="tabpanel"]') || doc.getElementById("spanel-libraries") || input.parentElement;
  const draft = registerSettingsGroup({
    panel,
    loadSettings: false,
    scope: "library",
    scopeName: () => `Library: ${getLibraryName()}`,
    controls: [input],
    read: () => ({ drop_db: Number(input.value) }),
    write: show,
    validate: () => {
      const problem = musicFadeDropProblem(input.value);
      if (problem) { showError(problem); return false; }
      clearError();
      return true;
    },
    commit: (value) => request(libraryUrl("/api/library/music-fade", getLibrary()), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ drop_db: value.drop_db }),
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
      input.value = "10";
    },
  };
}
