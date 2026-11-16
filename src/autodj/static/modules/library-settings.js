// Settings whose values belong to a shared music library.
import { captureAuthenticatedRequestEpoch, failureText, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { chooseAction } from "./confirm-dialog.js";
import { onCardOpen } from "./card-state.js";
import { discardSettingsPanel, saveSettingsPanel, settingsPanelHasChanges } from "./settings-drafts.js";
import { installAdvancedLength } from "./advanced-length.js";
import { installMusicFadeDrop } from "./music-fade-drop.js";
import { makeSay } from "./live-region.js";

export function installLibrarySettings({ document: doc = document, request = requestJson,
  selector = doc.getElementById("library-settings-library") } = {}) {
  const panel = doc.getElementById("spanel-libraries");
  if (!panel || !selector) return { load: async () => {}, refresh: async () => {} };
  const say = makeSay(doc.getElementById("libraries-status"));

  let selectedLibrary = selector.value || "main";
  let changeGeneration = 0;
  let loadGeneration = 0;
  let changingLibrary = false;
  const getLibrary = () => selectedLibrary;
  const getLibraryName = () => [...selector.options].find((option) => option.value === selectedLibrary)?.textContent || selectedLibrary;
  const advancedLength = installAdvancedLength({ document: doc, request, getLibrary, getLibraryName });
  const musicFadeDrop = installMusicFadeDrop({ document: doc, request, getLibrary, getLibraryName });

  async function loadLibrary(libraryId, { switching = false } = {}) {
    const mine = ++loadGeneration;
    const epoch = captureAuthenticatedRequestEpoch();
    let result;
    try {
      result = await Promise.all([
        advancedLength?.load({ libraryId, accept: false }),
        musicFadeDrop?.load({ libraryId, accept: false }),
      ]);
    } catch (error) {
      if (mine === loadGeneration && isAuthenticatedRequestCurrent(epoch) && selector.value === libraryId) {
        say(failureText("load library settings", error), "error");
      }
      return false;
    }
    if (mine !== loadGeneration || !isAuthenticatedRequestCurrent(epoch)
        || selector.value !== libraryId || (!switching && selectedLibrary !== libraryId)
        || result.length !== 2 || result.some((value) => value == null)) return false;
    if (switching) selectedLibrary = libraryId;
    return advancedLength.accept(result[0], libraryId) && musicFadeDrop.accept(result[1], libraryId);
  }

  const load = () => loadLibrary(selectedLibrary);

  selector.addEventListener("change", async () => {
    const requested = selector.value || "main";
    if (changingLibrary) {
      selector.value = selectedLibrary;
      say("Library settings are still loading.");
      return;
    }
    const previous = selectedLibrary;
    if (requested === previous) return;
    const generation = ++changeGeneration;
    const epoch = captureAuthenticatedRequestEpoch();
    const dirty = settingsPanelHasChanges(panel);
    changingLibrary = true;
    selector.setAttribute("aria-busy", "true");
    try {
      if (dirty) {
        const answer = await chooseAction(doc, {
          title: "Unsaved library settings",
          message: `Save changes for ${getLibraryName()}, discard them, or keep working with that library.`,
          choices: ["Save changes", "Discard changes", "Cancel"],
        });
        if (generation !== changeGeneration || !isAuthenticatedRequestCurrent(epoch)) return;
        if (answer === "accept") {
          if (!await saveSettingsPanel(panel)) return;
        } else if (answer === "alternate") {
          discardSettingsPanel(panel);
        } else return;
      }
      if (generation !== changeGeneration || !isAuthenticatedRequestCurrent(epoch)) return;
      if (!await loadLibrary(requested, { switching: true })) selector.value = previous;
    } finally {
      if (generation === changeGeneration) {
        if (selectedLibrary !== requested) selector.value = previous;
        selector.removeAttribute("aria-busy");
        changingLibrary = false;
      }
    }
  });

  onCardOpen(panel, () => { void load(); });
  return { load, refresh: load, getSelectedLibrary: getLibrary };
}
