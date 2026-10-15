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

// *say(message, error)* writes the Library tools status region; *isCurrent()*
// is false once the session has ended or the panel was cleared.  Returns
// null when the page has no such fields.
export function installAutoRescan({ document: doc, request, say, isCurrent = () => true }) {
  const hours = doc.getElementById("auto-rescan-hours");
  const minutes = doc.getElementById("auto-rescan-minutes");
  const save = doc.getElementById("auto-rescan-save");
  const error = doc.getElementById("auto-rescan-error");
  const djAnalysis = doc.getElementById("dj-analysis");
  if (!hours || !minutes || !save || !error) return null;
  const fields = { hours, minutes };
  let loaded = 0;

  function clearError() {
    for (const field of [hours, minutes]) {
      field.removeAttribute("aria-invalid");
      if (field.getAttribute("aria-describedby") === error.id) field.removeAttribute("aria-describedby");
    }
    error.textContent = "";
    error.hidden = true;
  }

  // The message under the field, linked to it and read when focus moves there.
  function showError(field, message) {
    clearError();
    error.textContent = message;
    error.hidden = false;
    (field.closest(".settings-row") || field).after(error);
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", error.id);
    field.focus();
  }

  function show(value) {
    hours.value = String(value?.hours ?? 0);
    minutes.value = String(value?.minutes ?? 0);
  }

  async function load() {
    const ticket = ++loaded;
    try {
      const value = await request("/api/library/auto-rescan");
      if (ticket === loaded && isCurrent()) show(value);
      if (djAnalysis) {
        const dj = await request("/api/library/dj-analysis");
        if (ticket === loaded && isCurrent()) djAnalysis.checked = dj?.enabled !== false;
      }
    } catch {
      // The fields keep what they show; Save reports any problem.
    }
  }

  for (const field of [hours, minutes]) {
    field.addEventListener("input", () => {
      if (field.getAttribute("aria-invalid") && !autoRescanProblem(hours.value, minutes.value)) clearError();
    });
  }

  djAnalysis?.addEventListener("change", async () => {
    if (!isCurrent()) return;
    loaded += 1;
    try {
      const value = await request("/api/library/dj-analysis", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: djAnalysis.checked }),
      });
      if (!isCurrent()) return;
      djAnalysis.checked = value?.enabled !== false;
      say("Saved.");
    } catch (err) {
      if (!isCurrent()) return;
      say(`Could not save Find beats and song structure: ${err?.message || "error"}`, true);
    }
  });

  save.addEventListener("click", async () => {
    if (!isCurrent()) return;
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
      if (!isCurrent()) return;
      show(value);
      say("Saved.");
    } catch (err) {
      if (!isCurrent()) return;
      if (err?.status === 400) showError(minutes, err.message || SHORT_MESSAGE);
      else say(`Could not save automatic rescan: ${err?.message || "error"}`, true);
    }
  });

  return {
    load,
    clear() {
      loaded += 1;
      clearError();
      show(null);
    },
  };
}
