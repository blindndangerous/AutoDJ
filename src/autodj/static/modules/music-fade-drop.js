// Shared music fade threshold in Library tools.
import { sayIfStillPending } from "./live-region.js";

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
  const help = doc.getElementById("music-fade-drop-help");
  const error = doc.getElementById("music-fade-drop-error");
  if (!input || !save || !error) return null;
  let loaded = 0;
  let saving = false;
  let generation = 0;

  function clearError() {
    input.removeAttribute("aria-invalid");
    error.textContent = "";
    error.hidden = true;
    if (help?.id) input.setAttribute("aria-describedby", help.id);
    else input.removeAttribute("aria-describedby");
  }
  function showError(message) {
    error.textContent = message;
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("aria-describedby", [error.id, help?.id].filter(Boolean).join(" "));
    input.focus();
  }
  function show(value) {
    const drop = Number(value?.drop_db);
    input.value = Number.isInteger(drop) && drop >= 1 && drop <= 30 ? String(drop) : "10";
  }
  async function load() {
    const ticket = ++loaded;
    const requestGeneration = generation;
    try {
      const value = await request("/api/library/music-fade");
      if (ticket === loaded && requestGeneration === generation && isCurrent() && !saving) show(value);
    } catch { /* Save reports a load failure when the operator acts. */ }
  }
  input.addEventListener("input", () => {
    if (input.getAttribute("aria-invalid") && !musicFadeDropProblem(input.value)) clearError();
  });
  save.addEventListener("click", async () => {
    if (!isCurrent() || saving) return;
    const problem = musicFadeDropProblem(input.value);
    if (problem) { showError(problem); return; }
    clearError();
    loaded += 1;
    const requestGeneration = generation;
    saving = true;
    save.classList.add("is-pending");
    save.setAttribute("aria-busy", "true");
    const stopPending = sayIfStillPending(say, "Saving the fade detection threshold…");
    try {
      const value = await request("/api/library/music-fade", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ drop_db: Number(input.value.trim()) }),
      });
      if (requestGeneration !== generation || !isCurrent()) return;
      show(value);
      say(`Saved. Fade detection threshold is ${input.value} dB.`);
    } catch (err) {
      if (requestGeneration !== generation || !isCurrent()) return;
      if (err?.status === 422 || err?.status === 400) showError(musicFadeDropProblem("x"));
      else say(`Could not save the fade detection threshold: ${err?.message || "error"}`, true);
    } finally {
      stopPending();
      if (requestGeneration === generation) {
        saving = false;
        save.classList.remove("is-pending");
        save.removeAttribute("aria-busy");
      }
    }
  });
  clearError();
  return {
    load,
    clear() {
      loaded += 1;
      generation += 1;
      saving = false;
      clearError();
      input.value = "10";
      save.classList.remove("is-pending");
      save.removeAttribute("aria-busy");
    },
  };
}
