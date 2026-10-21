// Settings > Breaks (#88): whether this station takes DJ breaks, news and
// weather from an announcer program, where a DJ break goes, the shortest
// intro talked up, and how far the
// music drops under a break.  config.toml gives only the first values: what
// is saved here is the station's from then on.
//
// The panel loads when it is seen.  A refresh fills the form only while
// nobody is changing it.  #brk-reason is plain text, not live: it says why
// breaks cannot be taken now, if they cannot.  Save break settings says its
// result once through #brk-status.  While it saves, the button is marked
// busy (aria-busy and .is-pending), never aria-disabled, so NVDA does not
// call the pressed button unavailable (#331).  A refused value gets
// aria-invalid, its error linked to the field and shown under it, and focus.

import { isStaleStationReply, requestJson, staleActionText } from "./api-client.js";
import { focusRevealed } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";

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

// What a save says.
export function savedText(before, after) {
  if (before?.enabled !== true && after.enabled === true) {
    return after.reason ? `Breaks are on. ${after.reason}.` : "Breaks are on.";
  }
  if (before?.enabled === true && after.enabled !== true) {
    const dropped = Number(after.dropped) || 0;
    const gone = dropped
      ? ` ${dropped} waiting break${dropped === 1 ? " was" : "s were"} dropped.`
      : "";
    return `Breaks are off. New announcer uploads are refused.${gone}`;
  }
  return "Break settings saved.";
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
  const save = get("save");
  const linerDuck = get("duck-liners");
  if (!form || !enabled || !minIntro || !duck || !status || !save) {
    return { refresh: async () => {}, clear: () => {} };
  }
  const errors = new Map([[minIntro, get("min-intro-error")], [duck, get("duck-error")]]);
  let settings = null;
  let dirty = false;
  let saving = false;
  let generation = 0;

  function say(message, tone = "info") {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 4000, tone });
  }

  function link(field, node, on) {
    const ids = (field.getAttribute("aria-describedby") || "").split(/\s+/)
      .filter((id) => id && id !== node.id);
    if (on) ids.push(node.id);
    field.setAttribute("aria-describedby", ids.join(" "));
  }

  function clearErrors() {
    for (const [field, node] of errors) {
      field.removeAttribute("aria-invalid");
      if (!node) continue;
      link(field, node, false);
      node.textContent = "";
      node.hidden = true;
    }
  }

  function fail(field, message) {
    const node = errors.get(field);
    field.setAttribute("aria-invalid", "true");
    if (node) {
      link(field, node, true);
      node.textContent = message;
      node.hidden = false;
    }
    focusRevealed(field);
    say(message, "error");
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
    if (dirty || saving || form.contains(doc.activeElement)) return;
    enabled.checked = state.enabled === true;
    const choice = form.querySelector(
      `input[name="brk-placement"][value="${["talk_up", "standalone"].includes(state.dj_placement) ? state.dj_placement : "auto"}"]`,
    );
    if (choice) choice.checked = true;
    if (Number.isFinite(state.min_intro_seconds)) minIntro.value = String(state.min_intro_seconds);
    duck.value = state.duck_db == null ? "" : String(state.duck_db);
  }

  // Background loads write into the panel; they are not announced.
  async function refresh() {
    const mine = ++generation;
    try {
      const loaded = await request("/api/breaks/settings");
      if (mine !== generation) return;
      settings = loaded;
      fill(loaded);
    } catch (error) {
      // A reply for the station the page has just left says nothing (#70).
      if (mine !== generation || isStaleStationReply(error)) return;
      if (reason) {
        reason.textContent = `Could not load the break settings: ${error?.message || "no answer"}`;
        reason.hidden = false;
      }
    }
  }

  // A station switch: what this panel showed was the old station's.
  function clear() {
    generation += 1;
    settings = null;
    dirty = false;
    clearErrors();
    status.textContent = "";
    showReason(null);
    if (linerDuck) linerDuck.textContent = "";
  }

  async function submit() {
    clearErrors();
    const intro = parseNumber(minIntro.value, 0, 60);
    if (intro === null) {
      fail(minIntro, MIN_INTRO_ERROR);
      return;
    }
    const blank = duck.value.trim() === "";
    const duckDb = blank ? null : parseNumber(duck.value, -30, 0);
    if (!blank && duckDb === null) {
      fail(duck, DUCK_ERROR);
      return;
    }
    const body = {
      enabled: enabled.checked,
      dj_placement: placement(),
      min_intro_seconds: intro,
      duck_db: duckDb,
    };
    const before = settings;
    // saving holds off another press; aria-busy, not aria-disabled (#331).
    saving = true;
    save.classList.add("is-pending");
    save.setAttribute("aria-busy", "true");
    try {
      const reply = await request("/api/breaks/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      settings = reply;
      dirty = false;
      saving = false;
      fill(reply);
      say(savedText(before, reply));
    } catch (error) {
      // Answered after a station switch: the form is the new station's (#70).
      const left = staleActionText(error);
      if (left) say(left, error?.actionDone ? "info" : "error");
      else say(`Could not save the break settings: ${error?.message || "no answer"}`, "error");
    } finally {
      saving = false;
      save.classList.remove("is-pending");
      save.removeAttribute("aria-busy");
    }
  }

  form.addEventListener("input", (event) => {
    dirty = true;
    if (event.target.getAttribute?.("aria-invalid") === "true") {
      event.target.removeAttribute("aria-invalid");
      const node = errors.get(event.target);
      if (node) {
        link(event.target, node, false);
        node.textContent = "";
        node.hidden = true;
      }
    }
  });
  form.addEventListener("change", () => { dirty = true; });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (saving) return;
    void submit();
  });
  panel?.addEventListener("toggle", () => {
    if (panel.open) void refresh();
  });
  if (panel?.open) void refresh();

  return { refresh, clear };
}
