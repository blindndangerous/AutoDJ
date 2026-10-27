// Shared-library similarity limit. Longer songs retain their selected audio
// preparation; 0 permits model analysis at every length.
import { sayIfStillPending } from "./live-region.js";

const MAX_ADVANCED_MINUTES = 600;

// The message for a value the server would refuse, or null.
export function advancedLengthProblem(text) {
  const value = String(text ?? "").trim();
  if (!/^\d+$/.test(value) || Number(value) > MAX_ADVANCED_MINUTES) {
    return `Enter a whole number of minutes from 0 to ${MAX_ADVANCED_MINUTES}; 0 means no limit.`;
  }
  return null;
}

// *say(message, error)* announces a save result; *isCurrent()* is false when
// the session has ended or the panel was cleared.
export function installAdvancedLength({ document: doc, request, say, isCurrent = () => true }) {
  const input = doc.getElementById("advanced-length");
  const save = doc.getElementById("advanced-length-save");
  const help = doc.getElementById("advanced-length-help");
  const error = doc.getElementById("advanced-length-error");
  if (!input || !save || !error) return null;
  const helpId = help?.id || "";
  let loaded = 0;
  let saving = false;

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
    if (saving) return;
    const ticket = ++loaded;
    try {
      const value = await request("/api/library/advanced-length");
      if (ticket === loaded && isCurrent() && !saving) show(value);
    } catch {
      // The field keeps what it shows; Save reports any problem.
    }
  }

  input.addEventListener("input", () => {
    if (input.getAttribute("aria-invalid") && !advancedLengthProblem(input.value)) clearError();
  });

  save.addEventListener("click", async () => {
    if (!isCurrent() || saving) return;
    const problem = advancedLengthProblem(input.value);
    if (problem) { showError(problem); return; }
    clearError();
    const ticket = ++loaded;
    // No aria-disabled while saving: NVDA would call the pressed button
    // unavailable (#331).  aria-busy says it is busy; a slow reply says so.
    saving = true;
    save.classList.add("is-pending");
    save.setAttribute("aria-busy", "true");
    const current = () => ticket === loaded && isCurrent();
    const stopPending = sayIfStillPending((message) => { if (current()) say(message); }, "Saving the similarity analysis limit…");
    try {
      const value = await request("/api/library/advanced-length", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ minutes: Number(input.value.trim()) }),
      });
      if (!current()) return;
      show(value);
      const minutes = Number(input.value);
      say(minutes === 0
        ? "Saved. Similarity analysis has no length limit."
        : `Saved. Songs longer than ${minutes} minutes get audio analysis only.`);
    } catch (err) {
      if (!current()) return;
      if (err?.status === 422 || err?.status === 400) showError(advancedLengthProblem("x"));
      else say(`Could not save the similarity analysis limit: ${err?.message || "error"}`, true);
    } finally {
      stopPending();
      if (ticket === loaded) {
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
      saving = false;
      save.classList.remove("is-pending");
      save.removeAttribute("aria-busy");
      clearError();
      input.value = "15";
    },
  };
}
