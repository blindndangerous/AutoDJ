// Library tools, Longest song for advanced analysis (#391).  Longer songs
// are imported and play like any other, with the basic analysis only; the
// advanced analysis is slow and memory-heavy on an hour-long mix.  0
// analyses every length.
import { sayIfStillPending } from "./live-region.js";

export const MAX_ADVANCED_MINUTES = 600;

// The message for a value the server would refuse, or null.
export function advancedLengthProblem(text) {
  const value = String(text ?? "").trim();
  if (!/^\d+$/.test(value) || Number(value) > MAX_ADVANCED_MINUTES) {
    return `Enter a whole number of minutes from 0 to ${MAX_ADVANCED_MINUTES}; 0 analyses every length.`;
  }
  return null;
}

// *say(message, error)* writes the Library tools status region; *isCurrent()*
// is false once the session has ended or the panel was cleared.  Returns
// null when the page has no such field.
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
    loaded += 1;
    // No aria-disabled while saving: NVDA would call the pressed button
    // unavailable (#331).  aria-busy says it is busy; a slow reply says so.
    saving = true;
    save.classList.add("is-pending");
    save.setAttribute("aria-busy", "true");
    const stopPending = sayIfStillPending(say, "Saving the advanced analysis length…");
    try {
      const value = await request("/api/library/advanced-length", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ minutes: Number(input.value.trim()) }),
      });
      if (!isCurrent()) return;
      show(value);
      const minutes = Number(input.value);
      say(minutes === 0
        ? "Saved. Advanced analysis takes songs of every length."
        : `Saved. Songs longer than ${minutes} minutes get basic analysis only.`);
    } catch (err) {
      if (!isCurrent()) return;
      if (err?.status === 422 || err?.status === 400) showError(advancedLengthProblem("x"));
      else say(`Could not save the advanced analysis length: ${err?.message || "error"}`, true);
    } finally {
      stopPending();
      saving = false;
      save.classList.remove("is-pending");
      save.removeAttribute("aria-busy");
    }
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
