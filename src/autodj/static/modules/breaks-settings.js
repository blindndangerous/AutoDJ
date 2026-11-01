// Settings > Breaks (#88): whether this station takes DJ breaks, news and
// weather from an announcer program, where a DJ break goes, the shortest
// intro talked up, and how far the
// music drops under a break.  config.toml gives only the first values: what
// is saved here is the station's from then on.
//
// The panel loads when it is seen.  A refresh fills the form only while
// nobody is changing it.  #brk-reason is plain text, not live: it says why
// breaks cannot be taken now, if they cannot. A refused value gets
// aria-invalid, its error linked to the field and shown under it, and focus.

import { failureText, isStaleStationReply, requestJson } from "./api-client.js";
import { clearFieldError, showFieldError } from "./dom-helpers.js";
import { onCardOpen } from "./card-state.js";
import { registerSettingsGroup } from "./settings-drafts.js";

export const MIN_INTRO_ERROR = "Enter the shortest intro in seconds, from 0 to 60, such as 2 or 1.5.";
export const DUCK_ERROR = "Enter how far the music drops in decibels, from minus 30 to 0, such as -12, or leave it blank.";
export const OFF_REASON = "Breaks are off for this station: new announcer uploads are refused. Voice tracks can still be booked with server playback and saved-point transitions.";

// A number typed with a dot or a comma, within low and high, or null.
export function parseNumber(text, low, high) {
  const value = String(text ?? "").trim().replace(",", ".").replace(/^[−–]/, "-");
  if (!/^-?(\d+(\.\d+)?|\.\d+)$/.test(value)) return null;
  const number = Number(value);
  return number >= low && number <= high ? number : null;
}

// Decibels in words, as a screen reader should say them.
function decibels(value) {
  const number = Math.round(Number(value) * 10) / 10;
  return number < 0 ? `minus ${-number}` : String(number);
}

export function installBreakSettings({ doc = document, request = requestJson } = {}) {
  const get = (name) => doc.getElementById(`brk-${name}`);
  const panel = doc.getElementById("spanel-breaks");
  const form = get("form");
  const enabled = get("enabled");
  const minIntro = get("min-intro");
  const duck = get("duck");
  const status = get("status");
  const reason = get("reason");
  const linerDuck = get("duck-liners");
  if (!form || !enabled || !minIntro || !duck || !status) {
    return { refresh: async () => {}, clear: () => {} };
  }
  const errors = new Map([[minIntro, get("min-intro-error")], [duck, get("duck-error")]]);
  let generation = 0;

  function clearErrors() {
    for (const [field, node] of errors) clearFieldError(field, node);
  }

  function fail(field, message) {
    showFieldError(field, message, errors.get(field), status);
  }

  function placement() {
    return form.querySelector('input[name="brk-placement"]:checked')?.value || "auto";
  }

  function showReason(state) {
    if (!reason) return;
    const text = !state ? "" : state.enabled !== true ? OFF_REASON : state.reason ? `${state.reason}.` : "";
    reason.textContent = text;
    reason.hidden = !text;
  }

  // The saved settings, unless the operator is changing them now.
  function fill(state) {
    showReason(state);
    if (linerDuck && Number.isFinite(state.liners_duck_db)) {
      linerDuck.textContent = `, now ${decibels(state.liners_duck_db)}`;
    }
    enabled.checked = state.enabled === true;
    const choice = form.querySelector(
      `input[name="brk-placement"][value="${["talk_up", "standalone"].includes(state.dj_placement) ? state.dj_placement : "auto"}"]`,
    );
    if (choice) choice.checked = true;
    if (Number.isFinite(state.min_intro_seconds)) minIntro.value = String(state.min_intro_seconds);
    duck.value = state.duck_db == null ? "" : String(state.duck_db);
  }

  const controls = [enabled, ...form.querySelectorAll('input[name="brk-placement"]'), minIntro, duck];
  const read = () => ({ enabled: enabled.checked, dj_placement: placement(),
    min_intro_seconds: parseNumber(minIntro.value, 0, 60),
    duck_db: duck.value.trim() === "" ? null : parseNumber(duck.value, -30, 0) });
  const draft = registerSettingsGroup({
    panel, group: "breaks", controls, read, write: fill,
    fields: { enabled, dj_placement: controls.filter((control) => control.name === "brk-placement"), min_intro_seconds: minIntro, duck_db: duck },
    validate() {
      clearErrors();
      if (read().min_intro_seconds === null) {
        fail(minIntro, MIN_INTRO_ERROR);
        return false;
      }
      if (duck.value.trim() !== "" && read().duck_db === null) {
        fail(duck, DUCK_ERROR);
        return false;
      }
      return true;
    },
  });

  // Background loads write into the panel; they are not announced.
  async function refresh() {
    const mine = ++generation;
    try {
      const loaded = await request("/api/breaks/settings");
      if (mine !== generation) return;
      draft.accept(loaded);
    } catch (error) {
      // A reply for the station the page has just left says nothing (#70).
      if (mine !== generation || isStaleStationReply(error)) return;
      if (reason) {
        reason.textContent = failureText("load the break settings", error, "no answer");
        reason.hidden = false;
      }
    }
  }

  // A station switch: what this panel showed was the old station's.
  function clear() {
    generation += 1;
    clearErrors();
    status.textContent = "";
    showReason(null);
    if (linerDuck) linerDuck.textContent = "";
  }

  form.addEventListener("submit", (event) => event.preventDefault());

  form.addEventListener("input", (event) => {
    if (event.target.getAttribute?.("aria-invalid") === "true") {
      clearFieldError(event.target, errors.get(event.target));
    }
  });
  onCardOpen(panel, refresh);
  if (panel?.open) void refresh();

  return { refresh, clear };
}
