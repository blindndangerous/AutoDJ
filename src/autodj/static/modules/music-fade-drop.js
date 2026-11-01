import { failureText } from "./api-client.js";
// Shared music fade threshold in Library tools.
import { clearFieldError, showFieldError, applyServerFieldError } from "./dom-helpers.js";
import { createSettingsSave } from "./standalone-settings.js";

export function musicFadeDropProblem(text) {
  const value = String(text ?? "").trim();
  if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 30) {
    return "Enter a whole number from 1 to 30 dB.";
  }
  return null;
}

export function installMusicFadeDrop({ document: doc, request, say, isCurrent = () => true }) {
  const input = doc.getElementById("music-fade-drop");
  const save = doc.getElementById("music-fade-drop-save");
  const error = doc.getElementById("music-fade-drop-error");
  if (!input || !save || !error) return null;
  let loaded = 0;

  const clearError = () => clearFieldError(input, error);
  const showError = (message) => showFieldError(input, message, error);
  function show(value) {
    const drop = Number(value?.drop_db);
    input.value = Number.isInteger(drop) && drop >= 1 && drop <= 30 ? String(drop) : "10";
  }
  async function load() {
    const ticket = ++loaded;
    try {
      const value = await request("/api/library/music-fade");
      if (ticket === loaded && isCurrent() && !saver.pending) show(value);
    } catch { /* Save reports a load failure when the operator acts. */ }
  }
  input.addEventListener("input", () => {
    if (input.getAttribute("aria-invalid") && !musicFadeDropProblem(input.value)) clearError();
  });
  const saver = createSettingsSave({ button: save, isCurrent, say, pendingText: "Saving the fade detection threshold\u2026", save: async (current) => {
    const problem = musicFadeDropProblem(input.value);
    if (problem) { showError(problem); return; }
    clearError();
    loaded += 1;
    try {
      const value = await request("/api/library/music-fade", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ drop_db: Number(input.value.trim()) }),
      });
      if (!current()) return;
      show(value);
      say(`Saved. Fade detection threshold is ${input.value} dB.`);
    } catch (err) {
      if (!current()) return;
      if (err?.status === 422 || err?.status === 400) applyServerFieldError({ field: "drop_db", message: musicFadeDropProblem("x") }, { drop_db: input }, error);
      else say(failureText("save the fade detection threshold", err), "error");
    }
  } });
  save.addEventListener("click", saver.run);
  clearError();
  return {
    load,
    clear() {
      loaded += 1;
      saver.clear();
      clearError();
      input.value = "10";
    },
  };
}
