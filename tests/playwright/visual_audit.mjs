// Visual accessibility audit: what a sighted, low-vision or keyboard-only
// user meets in every view and Settings subtab.  Per browser engine it
// checks, with every card opened:
//
//   - in each theme mode -- Light and Dark chosen in Settings (each
//     against the opposite system preference, so the choice must win),
//     and System under a light and a dark system preference:
//       - the stored choice is on <html> before any module script runs,
//         so nothing is painted in the other theme first, and the page
//         and the Theme radio buttons show that theme;
//       - axe-core's WCAG 2.2 A/AA rules at 1280 px: text contrast
//         (1.4.3), target size (2.5.8), label in name (2.5.3) and the rest;
//       - non-text contrast (1.4.11): every control's border or fill 3:1
//         against what is behind it, focus rings 3:1 (every stop in Dark,
//         the first stops in the other modes), the
//         progress fill 3:1 against its rail, each kind of cue tick 3:1
//         against the bar, and the Camelot wheel lit for 8A: every wedge
//         edge, the compatible outline and the current wedge 3:1 against
//         their neighbours, its numbers 4.5:1;
//       - the status message with its Dismiss button, as a confirmation
//         and as an error, through axe-core as well;
//   - in the System light mode only (layout does not change with colour):
//       - reflow at 320 CSS px, the width of 400 % zoom on a 1280 px
//         window, and 200 % text at 1280 px: the page must not scroll
//         sideways (1.4.10, 1.4.4);
//       - every native checkbox and radio button is at least 24 x 24 px
//         (2.5.8);
//       - Tab through the view: each stop draws a solid focus outline at
//         least 2 px wide, 3:1 against what is behind it, scrolled fully
//         into the window (2.4.7, 2.4.11, 1.4.11), so with the Dark mode
//         every focus ring in both themes is measured;
//       - choosing Dark in Settings, Display and keys switches the page at
//         once, says nothing, and survives a reload;
//       - Settings > Sound > Preview output device: a named select with
//         its help, 24 px targets, a described button, a visible reason
//         where the browser cannot choose a device, and a change said
//         politely without moving focus;
//       - Settings > Analysis workers, Machine tokens: the Station choice
//         is hidden until Token use is an announcer, then shows as a
//         named select with linked help and 24 px targets, focus staying
//         on Token use; it is measured in every theme mode (axe-core and
//         non-text contrast) and at 320 px with that choice shown;
//       - Voice tracks, with the microphone and recorder stubbed and the
//         open song change stood in for: shown idle, recording, with a
//         take, and with the microphone refused (its error linked to
//         Record); Record by keyboard keeps focus and says "Recording"
//         once, Alt+R from another control stops it without moving focus
//         and lets the microphone go, a refused booking is shown,
//         linked to Book voice track, focus staying there, and leaving
//         the tab stops a recording and says so once;
//       - the status message stays while the pointer is over it, goes
//         after the pointer leaves, lets a click on its frame (outside the
//         message and the button) through to the page underneath, and its
//         Dismiss button works by mouse and by keyboard, putting focus
//         back where it was.
//
//   - the public status pages: /status, and each station's
//     /stations/<slug>/status that /api/stations lists (a stopped
//     station's "off air" page too), in every theme mode: axe-core's
//     WCAG 2.2 A/AA rules, the theme applied, and every focus stop's
//     outline 3:1; in the System light mode reflow at 320 px and 200 %
//     text.
//
// Set AUTODJ_URL=http://host:port before running, e.g.:
//   AUTODJ_URL=http://localhost:8080 node tests/playwright/visual_audit.mjs
// Set AUTODJ_CPU_THROTTLE=<rate>, e.g. 6, to slow Chromium's CPU that many
// times, as on a loaded CI runner (Firefox has no such control).
// Set AUTODJ_SCREENSHOTS=<folder> to also save a full-page screenshot of
// every target in every theme mode, for review by eye.

import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { runAudit } from "./audit_helpers.mjs";

const BASE = process.env.AUTODJ_URL || "http://localhost:8080";
const SHOTS = process.env.AUTODJ_SCREENSHOTS || "";
const CPU_THROTTLE = Number(process.env.AUTODJ_CPU_THROTTLE || 0);
const AXE = fileURLToPath(import.meta.resolve("axe-core/axe.min.js"));
const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const VIEWS = ["now", "queue", "carts", "voice", "track-info", "history", "settings", "library"];
const MAX_TAB_STOPS = 80;
// Focus rings measured per target in the Light and System modes.  Every
// stop is measured in the Dark mode (fullFocus) and in the layout pass,
// which runs in System light, so each theme has one full pass without
// tabbing through every view four times.
const THEME_TAB_STOPS = 12;
const THEME_KEY = "autodj.theme";
// Body background per theme, as app.css sets --bg.
const THEME_BG = { light: "rgb(238, 241, 246)", dark: "rgb(26, 26, 46)" };
const THEME_MODES = [
  { name: "light", stored: "light", colorScheme: "dark", theme: "light" },
  { name: "dark", stored: "dark", colorScheme: "light", theme: "dark", fullFocus: true },
  { name: "system-light", stored: null, colorScheme: "light", theme: "light" },
  { name: "system-dark", stored: null, colorScheme: "dark", theme: "dark" },
];

// Settings > Song picking > Test rotation, opened: the server's answers
// are stood in for, so the running and finished states look the same on
// an empty library as on a full one.  The finished report carries long
// unbroken names, for the reflow checks.
const ROTATION_SECTIONS = [
  { title: "Summary", lines: [
    "Simulated 168 hours, from Monday 12 October 2026, 06:00 to Monday 19 October 2026, 06:02, station time (UTC).",
    "Picked 2,914 songs and 412 liners.",
    "Seed 1234. Use the same seed to repeat this run exactly.",
  ] },
  { title: "Songs", lines: [
    "The library has 77,000 songs; 2,604 played and 74,396 never played.",
    { text: "Never played, first two:", items: [
      "Example Artist — An_Exceptionally_Long_Title_Without_Any_Spaces_From_A_Tagless_File_0001",
      "Another Example — Song 2",
    ] },
  ] },
  { title: "Categories", lines: [
    { text: "Power: six songs, 504 plays.", items: [
      "Plays per song: fewest 80, most 90, average 84.",
      "Its clockwheel step was skipped 40 times.",
    ] },
  ] },
  { title: "Rules relaxed", lines: ["Same artist: relaxed for 12 picks."] },
  { title: "Dead air", lines: ["No moment had nothing to play."] },
];
const rotationStates = new WeakMap();

async function standInForRotationTest(page) {
  if (rotationStates.has(page)) return rotationStates.get(page);
  const state = { done: false };
  rotationStates.set(page, state);
  await page.route("**/api/rotation-test", (route) => {
    const view = state.done
      ? { state: "done", progress: { picks: 2914, percent: 100 }, elapsed_seconds: 61,
        report: { picks: 2914, station: "default", seed: 1234, stopped_early: false },
        text: "", sections: ROTATION_SECTIONS, error: null }
      : { state: "running", progress: { picks: 1200, percent: 40 }, elapsed_seconds: 20,
        report: null, text: null, sections: null, error: null };
    return route.fulfill({
      status: route.request().method() === "POST" ? 202 : 200,
      contentType: "application/json",
      body: JSON.stringify(view),
    });
  });
  return state;
}

async function openRotationTest(page, visit) {
  const state = await standInForRotationTest(page);
  state.done = false;
  await page.locator("#rotation-run").click();
  await page.locator("#rotation-progress-row").waitFor({ state: "visible" });
  await visit("settings/picking/rotation-running");
  state.done = true;
  await page.locator("#rotation-results").waitFor({ state: "visible", timeout: 10000 });
  await visit("settings/picking/rotation-results");
}

// Track info with a song loaded, so its Tags form is shown: the server's
// answers are stood in for, as the empty library has no song to open.  The
// title is long and unbroken, for the reflow checks, and beets is offered so
// its checkbox is measured too.
const TRACK_PATH = "/music/Example Artist/An_Exceptionally_Long_Title_Without_Any_Spaces_0001.flac";
const TRACK_TAGS = {
  title: "An_Exceptionally_Long_Title_Without_Any_Spaces_0001", artist: "Example Artist",
  album: "Example Album", genre: "Ambient", year: "1999", bpm: "120", key: "Am", isrc: "",
};
const TRACK_RECORD = {
  path: TRACK_PATH, kind: "music", ...TRACK_TAGS, year: 1999, bpm: 120, length: 215.4,
  last_played: 0, key_label: "8A", analysis: "ready", loudness_ready: true, allow_outro: false,
  points: { start: 0, intro: 8, outro: 200, segue: 210, end: 215 },
  point_sources: { start: "analysis", intro: "analysis", outro: "analysis", segue: "analysis", end: "analysis" },
  tags: TRACK_TAGS, tags_writable: true, beets_writeback: true,
};
const trackPages = new WeakSet();

async function standInForTrackInfo(page) {
  if (trackPages.has(page)) return;
  trackPages.add(page);
  await page.route("**/api/library/points?*", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(TRACK_RECORD),
  }));
  await page.route("**/api/library/tags", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify({ ...TRACK_RECORD, beets: "updated" }),
  }));
}

// Open the song's Track info, then show a refused save: two fields marked
// invalid with their errors on the page and focus on the first.
async function openTrackTags(page, visit) {
  await page.evaluate((path) => document.dispatchEvent(new CustomEvent("autodj:edit-points",
    { detail: { path, kind: "music", opener: document.getElementById("tab-track-info") } })), TRACK_PATH);
  await page.locator("#tags-form").waitFor({ state: "visible" });
  await visit("track-info/tags");
  await page.locator("#tag-year").fill("99");
  await page.locator("#tag-isrc").fill("123");
  await page.locator("#tags-save").click();
  await page.locator("#tag-year-error").waitFor({ state: "visible" });
  await visit("track-info/tags-errors");
  // Choosing another song with those edits unsaved opens Save, Discard or
  // Keep editing; Keep editing closes it again with the edits kept.
  await page.evaluate((path) => document.dispatchEvent(new CustomEvent("autodj:edit-points",
    { detail: { path, kind: "music", opener: document.getElementById("tab-track-info") } })),
  `${TRACK_PATH}.other.flac`);
  await page.locator("#track-info-dialog").waitFor({ state: "visible" });
  await visit("track-info/save-dialog");
  await page.locator("#track-info-keep").click();
  await page.locator("#track-info-dialog").waitFor({ state: "hidden" });
}

// Settings > Time checks with two voice packs, one with play dates and
// outside them, so the pack list and the Voice pack play dates form are
// shown (the audit server has no packs).  Only reading is stood in for.
const TIME_CHECKS = {
  enabled: true, hour_format: "12", rule: "after_hour_song", enabled_packs: ["Studio", "Christmas"],
  pack_order: "rotate", missing_minutes: 1440, missing_examples: [], tts_available: false,
  timezone: "UTC", duck_db: -12,
  packs: [
    { name: "Studio", enabled: true, minutes_covered: 1440, summary: "1440 of 1440 minutes covered.",
      not_before: null, not_after: null, dates_text: "", playable: true, unplayable_text: "" },
    { name: "Christmas", enabled: true, minutes_covered: 1440, summary: "1440 of 1440 minutes covered.",
      not_before: "2026-12-01", not_after: "2026-12-26",
      dates_text: "Plays from 1 December 2026 until 26 December 2026", playable: false,
      unplayable_text: "not playable before 1 December 2026" },
  ],
};
const timeCheckPages = new WeakSet();

async function standInForTimeChecks(page) {
  if (timeCheckPages.has(page)) return;
  timeCheckPages.add(page);
  await page.route("**/api/time-checks", (route) => (route.request().method() === "GET"
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(TIME_CHECKS) })
    : route.continue()));
}

// A backwards date range: the error shown on the page, linked to Do not
// play after and focused, as a refused save shows it.
async function openPackDatesError(page, visit) {
  await page.locator("#tc-dates").waitFor({ state: "visible" });
  await page.locator("#tc-not-before").fill("2026-12-26");
  await page.locator("#tc-not-after").fill("2026-12-01");
  await page.locator("#tc-dates-save").click();
  await page.locator("#tc-dates-error").waitFor({ state: "visible" });
  await visit("settings/time-checks/dates-error");
  await page.locator("#tc-not-after").fill("2026-12-31");
}

// Library tools: a category's season and a clockwheel step's hours, each
// refused with its error on the page, linked to the field and focused.
async function openDaypartErrors(page, visit) {
  await page.locator("#categories-add").click();
  await page.locator("#categories-name").fill("Christmas");
  await page.locator("#categories-query").fill("genre:Christmas");
  await page.locator("#categories-not-before").fill("2026-12-26");
  await page.locator("#categories-not-after").fill("2026-12-01");
  await page.locator("#categories-save").click();
  await page.locator("#categories-form-error").waitFor({ state: "visible" });
  await visit("library/category-dates-error");
  await page.locator("#categories-cancel").click();
  await page.locator("#clock-name").fill("Audit");
  await page.locator("#clock-create").click();
  await page.locator("#clock-step-hours-from").fill("22:00");
  await page.locator("#clock-add-step").click();
  await page.locator("#clock-step-hours-error").waitFor({ state: "visible" });
  await visit("library/step-hours-error");
}

// Voice tracks (#128): the open song change and a booked list are stood
// in for (the audit server takes no breaks), and the microphone and the
// recorder are stubbed before any page script, so Record works with no
// device and no permission prompt.  window.__micRefusal names the error
// the next request for the microphone fails with.
const VOICE_CONTEXT = {
  station: "default", accepting: true, reason: null, now: null, next: null,
  slot: {
    slot_id: "a".repeat(32), closes_in_s: 125, airs_in_s: 330, upcoming_intro_s: 8.5,
    pinned: true, breaks_booked: 1, auto_placement: "talk_up", auto_note: null,
    ending: { id: "song-c", title: "An_Exceptionally_Long_Title_Without_Any_Spaces_From_A_Tagless_File_0002", artist: "Example Artist", album: "", year: null, duration_s: 214 },
    upcoming: { id: "song-d", title: "Fourth Song", artist: "Another Example", album: "", year: null, duration_s: 187 },
  },
  limits: { max_bytes: 41943040, max_seconds: 300, sample_rate: 44100, dj_placement: "auto", min_intro_s: 2, max_per_slot: 2 },
  tracks: [
    { break_id: "b".repeat(32), label: "Third Song into Fourth Song", state: "queued", slot_id: "a".repeat(32),
      mixed_as: null, reason: null, eta: null, source: "voice_track" },
    { break_id: "c".repeat(32), label: "Second Song into Third Song", state: "dropped", slot_id: "e".repeat(32),
      mixed_as: null, reason: "cancelled in the voice tracker", eta: null, source: "voice_track" },
  ],
};
const voicePages = new WeakSet();

async function standInForVoiceTracker(page) {
  if (voicePages.has(page)) return;
  voicePages.add(page);
  await page.addInitScript(() => {
    const stream = { getTracks: () => [{ stop() { window.__micStopped = (window.__micStopped || 0) + 1; } }] };
    Object.defineProperty(window.Navigator.prototype, "mediaDevices", {
      configurable: true,
      get: () => ({
        getUserMedia: async () => {
          if (window.__micRefusal) throw new window.DOMException("refused", window.__micRefusal);
          return stream;
        },
      }),
    });
    window.MediaRecorder = class extends window.EventTarget {
      constructor() {
        super();
        this.mimeType = "audio/webm";
        this.state = "inactive";
      }

      start() { this.state = "recording"; }

      stop() {
        this.state = "inactive";
        const data = new Event("dataavailable");
        data.data = new window.Blob(["voice"], { type: "audio/webm" });
        this.dispatchEvent(data);
        this.dispatchEvent(new Event("stop"));
      }
    };
  });
  await page.route("**/api/voice-tracks", (route) => (route.request().method() === "GET"
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(VOICE_CONTEXT) })
    : route.continue()));
}

// Voice tracks while recording, with a take to book, and with the
// microphone refused: its error on the page, linked to Record.
async function openVoiceStates(page, visit) {
  await page.locator("#vt-slot").waitFor({ state: "visible" });
  const record = page.locator("#vt-record");
  await record.click();
  await page.waitForFunction(() => document.getElementById("vt-record").getAttribute("aria-pressed") === "true");
  await visit("voice/recording");
  await record.click();
  await page.waitForFunction(() => /^Recorded/.test(document.getElementById("vt-timer").textContent));
  await visit("voice/recorded");
  await page.locator("#vt-discard").click();
  await page.evaluate(() => { window.__micRefusal = "NotAllowedError"; });
  await record.click();
  await page.locator("#vt-error").waitFor({ state: "visible" });
  await visit("voice/error");
  await page.evaluate(() => { window.__micRefusal = ""; });
}

async function openEveryCard(page) {
  await page.evaluate(() => {
    for (const details of document.querySelectorAll("details")) details.open = true;
  });
}

// Under reduced motion app.css gives every property a 0.01 ms transition,
// so a change of focus, view or class shows its old colour, outline and
// width until the next frame.  On a loaded CI runner that frame can come
// well after a fixed wait: a focus ring was measured in the button's own
// white text colour (1.00:1).  Every measurement waits for the running
// transitions to end, so it reads the colours a person sees.
function settle(page) {
  return page.evaluate(() => {
    const running = document.getAnimations()
      .filter((animation) => animation instanceof window.CSSTransition);
    const ended = Promise.all(running.map((animation) => animation.finished.catch(() => null)));
    return Promise.race([ended, new Promise((resolve) => setTimeout(resolve, 2000))]);
  });
}

async function newPage(context) {
  const page = await context.newPage();
  if (CPU_THROTTLE > 1 && context.browser()?.browserType().name() === "chromium") {
    const session = await context.newCDPSession(page);
    await session.send("Emulation.setCPUThrottlingRate", { rate: CPU_THROTTLE });
  }
  return page;
}

// Every view, and with Settings every subtab this server shows (Stream only
// in stream mode).  `visit` runs once per target with that target shown.
async function eachTarget(page, visit) {
  await standInForRotationTest(page);
  await standInForTrackInfo(page);
  await standInForTimeChecks(page);
  await standInForVoiceTracker(page);
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  for (const view of VIEWS) {
    await page.locator(`#tab-${view}`).click();
    await page.locator(`#panel-${view}`).waitFor({ state: "visible" });
    // Under reduced motion every change still runs a 0.01 ms transition,
    // which takes a frame; let it end before colours are measured.
    await page.waitForTimeout(100);
    if (view !== "settings") {
      await openEveryCard(page);
      await settle(page);
      await visit(view);
      if (view === "track-info") await openTrackTags(page, visit);
      if (view === "library") await openDaypartErrors(page, visit);
      if (view === "voice") await openVoiceStates(page, visit);
      continue;
    }
    const ids = await page.$$eval("#settings-tabs [role='tab']:not([hidden])",
      (tabs) => tabs.map((tab) => tab.id));
    for (const id of ids) {
      await page.locator(`#${id}`).click();
      await page.waitForTimeout(150);
      await openEveryCard(page);
      await settle(page);
      await visit(`settings/${id.replace(/^stab-/, "")}`);
      if (id === "stab-picking") await openRotationTest(page, visit);
      if (id === "stab-time-checks") await openPackDatesError(page, visit);
    }
  }
}

function horizontalOverflow(page) {
  return page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const scrollWidth = document.documentElement.scrollWidth;
    if (scrollWidth <= width) return null;
    const widest = [...document.querySelectorAll("body *")]
      .filter((element) => element.getBoundingClientRect().right > width + 1)
      .map((element) => element.id ? `#${element.id}` : element.tagName.toLowerCase())
      .slice(0, 5);
    return { scrollWidth, width, widest };
  });
}

function smallChoiceControls(page) {
  return page.evaluate(() => [...document.querySelectorAll(
    'input[type="checkbox"], input[type="radio"]',
  )]
    .filter((input) => input.getClientRects().length)
    .map((input) => ({ id: input.id, ...input.getBoundingClientRect().toJSON() }))
    .filter(({ width, height }) => width < 23.5 || height < 23.5)
    .map(({ id, width, height }) => `${id || "input"} ${Math.round(width)}x${Math.round(height)}`));
}

async function axeViolations(page) {
  if (!await page.evaluate(() => Boolean(window.axe))) {
    await page.addScriptTag({ path: AXE });
  }
  return page.evaluate(async (tags) => {
    const result = await window.axe.run(document, { runOnly: { type: "tag", values: tags } });
    return result.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.slice(0, 5).map((node) => node.target.join(" ")),
    }));
  }, AXE_TAGS);
}

// Colour arithmetic for the page, installed once per document as
// window.__contrast.  Computed colours come back as rgb()/rgba(); a
// translucent layer is laid over what is behind it.
async function installContrastHelpers(page) {
  await page.evaluate(() => {
    if (window.__contrast) return;
    const parse = (value) => {
      const parts = String(value).match(/[\d.]+/g)?.map(Number) || [0, 0, 0, 0];
      return [parts[0], parts[1], parts[2], parts.length > 3 ? parts[3] : 1];
    };
    const over = (top, bottom) => {
      const a = top[3];
      return [0, 1, 2].map((i) => top[i] * a + bottom[i] * (1 - a)).concat(1);
    };
    const luminance = ([r, g, b]) => [r, g, b].map((v) => {
      const c = v / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
    const ratio = (a, b) => {
      const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    // What shows behind an element: every ancestor's background, laid in
    // order over white.
    const behind = (element) => {
      const layers = [];
      for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
        layers.push(parse(window.getComputedStyle(node).backgroundColor));
      }
      return layers.reverse().reduce((below, layer) => over(layer, below), [255, 255, 255, 1]);
    };
    window.__contrast = { parse, over, ratio, behind };
  });
}

// WCAG 1.4.11 for the controls on show: the border, or failing that the
// control's own fill, 3:1 against what is behind it.  Disabled controls
// are exempt, as WCAG exempts inactive components; native checkboxes and
// radio buttons keep the system's own drawing (docs/accessibility-testing.md).
function controlBoundaryProblems(page) {
  return page.evaluate(() => {
    const { parse, over, ratio, behind } = window.__contrast;
    const problems = [];
    const controls = document.querySelectorAll([
      "button", "select", "textarea", '[role="tab"]',
      'input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"])',
    ].join(", "));
    for (const control of controls) {
      if (!control.getClientRects().length || control.closest("[hidden], [aria-hidden='true']")) continue;
      if (control.disabled || control.getAttribute("aria-disabled") === "true"
        || control.classList.contains("is-pending")) continue;
      const style = window.getComputedStyle(control);
      if (style.visibility === "hidden" || Number(style.opacity) === 0) continue;
      const back = behind(control.parentElement);
      const fill = over(parse(style.backgroundColor), back);
      const border = style.borderTopStyle !== "none" && parseFloat(style.borderTopWidth) >= 1
        ? over(parse(style.borderTopColor), back) : back;
      const best = Math.max(ratio(border, back), ratio(fill, back));
      if (best < 3) {
        problems.push(`${control.id ? `#${control.id}` : control.tagName.toLowerCase()} boundary ${best.toFixed(2)}:1`);
      }
    }
    return problems.slice(0, 10);
  });
}

// Meters and marks: the progress fill against its rail, and one cue tick
// of each kind against the bar, drawn for this check and cleared at once
// (the empty library has no track to give them).
function meterProblems(page) {
  return page.evaluate(async () => {
    const { parse, over, ratio, behind } = window.__contrast;
    const problems = [];
    const track = document.getElementById("progress-track");
    const fill = document.getElementById("progress-fill");
    const strip = document.getElementById("cue-strip");
    if (!track || !track.getClientRects().length) return problems;
    const back = behind(track);
    const rail = over(parse(window.getComputedStyle(track, "::before").backgroundColor), back);
    const fillColour = over(parse(window.getComputedStyle(fill).backgroundColor), back);
    if (ratio(fillColour, rail) < 3) problems.push(`progress fill on rail ${ratio(fillColour, rail).toFixed(2)}:1`);
    const { renderCueStrip, CUE_COLORS } = await import(`${window.location.origin}/modules/cues.js`);
    const types = Object.keys(CUE_COLORS);
    renderCueStrip(strip, {
      path: "__audit__", length: 100,
      cues: types.map((type, index) => ({ type, time_s: 5 + index * 8 })),
    });
    for (const mark of strip.querySelectorAll(".cue-mark")) {
      const colour = over(parse(window.getComputedStyle(mark).backgroundColor), back);
      if (ratio(colour, back) < 3) problems.push(`cue tick ${mark.title} ${ratio(colour, back).toFixed(2)}:1`);
    }
    renderCueStrip(strip, { path: "", length: 0, cues: [] });
    return problems;
  });
}

// The Camelot wheel lit for 8A in compatible mode, measured in the same
// task so the page's own state push cannot repaint it first.
function wheelProblems(page) {
  return page.evaluate(async () => {
    const { parse, over, ratio, behind } = window.__contrast;
    const sectorsEl = document.getElementById("camelot-sectors");
    const labelsEl = document.getElementById("camelot-labels");
    if (!sectorsEl || !sectorsEl.getClientRects().length) return [];
    const { applyCamelotWheel } = await import(`${window.location.origin}/modules/camelot-wheel.js`);
    // The reduced-motion rule gives every property a 0.01 ms transition,
    // so a class change shows its old colour for a frame; measure the end.
    const still = document.createElement("style");
    still.textContent = "#camelot-wheel * { transition: none !important; }";
    document.head.append(still);
    applyCamelotWheel("8A", "compatible", { sectorsEl, labelsEl });
    const back = behind(sectorsEl.closest("svg"));
    const style = (cell) => window.getComputedStyle(sectorsEl.querySelector(`.sector[data-cell="${cell}"]`));
    const fillOf = (cell) => over(parse(style(cell).fill), back);
    const strokeOf = (cell) => over(parse(style(cell).stroke), back);
    const labelFill = (cell) => parse(window.getComputedStyle(
      labelsEl.querySelector(`.label[data-cell="${cell}"]`)).fill);
    // 8A current; 7A, 9A, 8B compatible; 3B and 6A idle.
    const checks = [
      ["idle edge on idle wedge", ratio(strokeOf("3B"), fillOf("3B")), 3],
      ["compatible outline on its wedge", ratio(strokeOf("7A"), fillOf("7A")), 3],
      ["compatible outline on idle wedge", ratio(strokeOf("7A"), fillOf("6A")), 3],
      ["current wedge on compatible wedge", ratio(fillOf("8A"), fillOf("8B")), 3],
      ["current edge on compatible wedge", ratio(strokeOf("8A"), fillOf("8B")), 3],
      ["current edge on idle wedge", ratio(strokeOf("8A"), fillOf("3B")), 3],
      ["number on idle wedge", ratio(labelFill("3B"), fillOf("3B")), 4.5],
      ["number on compatible wedge", ratio(labelFill("7A"), fillOf("7A")), 4.5],
      ["current number on its wedge", ratio(labelFill("8A"), fillOf("8A")), 4.5],
    ];
    const widths = ["3B", "7A", "8A"].map((cell) => parseFloat(style(cell).strokeWidth));
    const problems = checks.filter(([, measured, minimum]) => measured < minimum)
      .map(([label, measured]) => `wheel ${label} ${measured.toFixed(2)}:1`);
    if (!(widths[0] < widths[1] && widths[1] < widths[2])) {
      problems.push(`wheel edge widths not idle < compatible < current: ${widths.join(", ")}`);
    }
    applyCamelotWheel(null, "compatible", { sectorsEl, labelsEl });
    still.remove();
    return problems;
  });
}

function showToast(page, message, tone = "info") {
  return page.evaluate(async ([text, toneName]) => {
    const { showVisibleStatus } = await import(`${window.location.origin}/modules/live-region.js`);
    showVisibleStatus(text, document, { tone: toneName });
  }, [message, tone]);
}

// The status message and its Dismiss button: axe-core over the page with
// the toast showing, the frame's edge on the page, and the button's size.
async function toastProblems(page) {
  const problems = [];
  for (const tone of ["info", "error"]) {
    await showToast(page, tone === "error" ? "Could not save: the server refused." : "Settings saved.", tone);
    await settle(page);
    const found = await page.evaluate(() => {
      const { parse, over, ratio, behind } = window.__contrast;
      const box = document.getElementById("status-toast-box");
      const button = document.getElementById("status-toast-dismiss");
      const style = window.getComputedStyle(box);
      const back = behind(box.parentElement);
      const issues = [];
      const edge = ratio(over(parse(style.borderTopColor), back), back);
      if (edge < 3) issues.push(`toast edge ${edge.toFixed(2)}:1`);
      const rect = button.getBoundingClientRect();
      if (rect.width < 24 || rect.height < 24) issues.push(`Dismiss ${rect.width}x${rect.height}`);
      return issues;
    });
    problems.push(...found.map((issue) => `${tone}: ${issue}`));
    const violations = await axeViolations(page);
    problems.push(...violations.map((violation) => `${tone}: axe ${violation.id} ${violation.targets.join(", ")}`));
  }
  await showToast(page, "");
  return problems;
}

// Tab from the panel's own tab through the view.  Stops at the first
// element met twice (focus wrapped) or after `maxStops`.  With
// `measureColour`, each ring's colour must reach 3:1 against what is
// behind it: the parent's background for a ring drawn outside the
// control, the control's own for one drawn inside it.
async function focusProblems(page, startSelector, { maxStops = MAX_TAB_STOPS, measureColour = false } = {}) {
  await page.locator(startSelector).focus();
  const problems = [];
  // The start counts as met: focus coming back to it means the view is done.
  const seen = new Set([await page.evaluate(
    () => [...document.querySelectorAll("*")].indexOf(document.activeElement),
  )]);
  for (let stop = 0; stop < maxStops; stop += 1) {
    await page.keyboard.press("Tab");
    await page.waitForTimeout(30);
    await settle(page);
    const info = await page.evaluate((colour) => {
      const element = document.activeElement;
      if (!element || element === document.body) return null;
      const all = [...document.querySelectorAll("*")];
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      let ringRatio = null;
      if (colour && style.outlineStyle !== "none") {
        const { parse, over, ratio, behind } = window.__contrast;
        const inside = parseFloat(style.outlineOffset) < 0;
        const back = behind(inside ? element : element.parentElement);
        ringRatio = ratio(over(parse(style.outlineColor), back), back);
      }
      return {
        key: all.indexOf(element),
        name: element.id ? `#${element.id}` : element.tagName.toLowerCase(),
        outline: style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2,
        ringRatio,
        inView: rect.top >= -1 && rect.bottom <= window.innerHeight + 1
          && rect.left >= -1 && rect.right <= window.innerWidth + 1,
      };
    }, measureColour);
    // Focus left the page (past the last control): the view is done.
    if (!info) { if (seen.size) break; continue; }
    if (seen.has(info.key)) break;
    seen.add(info.key);
    if (!info.outline) problems.push(`${info.name} has no focus outline`);
    if (!info.inView) problems.push(`${info.name} not scrolled into view`);
    if (info.ringRatio !== null && info.ringRatio < 3) {
      problems.push(`${info.name} focus ring ${info.ringRatio.toFixed(2)}:1`);
    }
  }
  return problems;
}

function startOf(target) {
  const [view, sub] = target.split("/");
  // Only Settings targets name a subtab; "track-info/tags" is a state.
  return view === "settings" && sub ? `#stab-${sub}` : `#tab-${view}`;
}

async function themeContext(browser, mode) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    reducedMotion: "reduce",
    colorScheme: mode.colorScheme,
    bypassCSP: true,
  });
  // Before any page script, as a returning visitor's stored choice is.
  await context.addInitScript(([key, stored]) => {
    try {
      if (stored) window.localStorage.setItem(key, stored);
      else window.localStorage.removeItem(key);
    } catch { /* storage blocked: System */ }
    // The theme on <html> when parsing ended, before app.js ran: set by
    // the head script alone, ahead of the stylesheet.
    document.addEventListener("readystatechange", () => {
      if (document.readyState !== "interactive" || window.__beforeModules) return;
      window.__beforeModules = { theme: document.documentElement.getAttribute("data-theme") };
    });
  }, [THEME_KEY, mode.stored]);
  return context;
}

// One theme mode: colours only.
async function auditTheme(browser, mode, out) {
  const context = await themeContext(browser, mode);
  const page = await newPage(context);
  page.on("pageerror", (error) => out.pageerrors.push(`${mode.name}: ${error.message}`));
  const record = (key, target, problems) => {
    if (!problems.length) return;
    out[key][`${mode.name} ${target}`] = problems;
  };
  try {
    await eachTarget(page, async (target) => {
      await installContrastHelpers(page);
      record("axe", target, await axeViolations(page));
      record("nonText", target, await controlBoundaryProblems(page));
      record("focusColour", target, await focusProblems(page, startOf(target),
        { maxStops: mode.fullFocus ? MAX_TAB_STOPS : THEME_TAB_STOPS, measureColour: true }));
      if (target === "now") {
        record("nonText", "meters", await meterProblems(page));
        record("nonText", "camelot", await wheelProblems(page));
        record("toast", target, await toastProblems(page));
      }
      if (target === "settings/display") record("toast", target, await toastProblems(page));
      if (target === "settings/workers") {
        await chooseAnnouncer(page);
        record("axe", `${target} announcer`, await axeViolations(page));
        record("nonText", `${target} announcer`, await controlBoundaryProblems(page));
        await chooseAnnouncer(page, false);
      }
      if (SHOTS) {
        // A message showing in every shot, so the toast is seen in both
        // themes; on Now Playing the wheel lit as well.
        await showToast(page, "Settings saved.");
        if (target === "now") {
          await page.evaluate(async () => {
            const { applyCamelotWheel } = await import(`${window.location.origin}/modules/camelot-wheel.js`);
            applyCamelotWheel("8A", "compatible", {
              sectorsEl: document.getElementById("camelot-sectors"),
              labelsEl: document.getElementById("camelot-labels"),
            });
          });
        }
        await page.screenshot({
          path: join(SHOTS, `${out.browser}-${mode.name}-${target.replaceAll("/", "-")}.png`),
          fullPage: true,
        });
        await showToast(page, "");
      }
    });
    await settle(page);
    const state = await page.evaluate(() => ({
      before: window.__beforeModules,
      theme: document.documentElement.getAttribute("data-theme"),
      background: window.getComputedStyle(document.body).backgroundColor,
      checked: document.querySelector('input[name="theme"]:checked')?.value,
    }));
    const expected = {
      theme: mode.stored,
      background: THEME_BG[mode.theme],
      checked: mode.stored || "system",
    };
    const problems = [];
    if (state.before?.theme !== expected.theme) problems.push(`data-theme before modules ${state.before?.theme}`);
    if (state.theme !== expected.theme) problems.push(`data-theme ${state.theme}`);
    if (state.background !== expected.background) problems.push(`background ${state.background}`);
    if (state.checked !== expected.checked) problems.push(`checked ${state.checked}`);
    if (problems.length) out.themeState[mode.name] = problems;
  } finally {
    await context.close();
  }
}

// Choosing Dark in Settings, Display and keys: the page changes at once,
// nothing is announced, and a reload keeps it.
async function themeSettingProblems(page) {
  const problems = [];
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  await page.locator("#tab-settings").click();
  await page.locator("#stab-display").click();
  const regions = () => page.evaluate(() => [...document.querySelectorAll(
    '[aria-live]:not([aria-live="off"]), [role="status"], [role="alert"]',
  )].map((node) => node.textContent.trim()).join("|"));
  const before = await regions();
  await page.locator("#theme-dark").click();
  await settle(page);
  const after = await page.evaluate(() => ({
    theme: document.documentElement.getAttribute("data-theme"),
    background: window.getComputedStyle(document.body).backgroundColor,
    stored: window.localStorage.getItem("autodj.theme"),
    toast: document.getElementById("status-toast-box")?.hidden,
  }));
  if (after.theme !== "dark" || after.background !== THEME_BG.dark) {
    problems.push(`after choosing Dark: ${JSON.stringify(after)}`);
  }
  if (after.stored !== "dark") problems.push(`Dark not stored: ${after.stored}`);
  if (after.toast === false) problems.push("choosing a theme showed a status message");
  if (await regions() !== before) problems.push("choosing a theme wrote a live region");
  await page.reload({ waitUntil: "networkidle" });
  const reloaded = await page.evaluate(() => ({
    theme: document.documentElement.getAttribute("data-theme"),
    checked: document.querySelector('input[name="theme"]:checked')?.value,
  }));
  if (reloaded.theme !== "dark" || reloaded.checked !== "dark") {
    problems.push(`after reload: ${JSON.stringify(reloaded)}`);
  }
  await page.locator("#tab-settings").click();
  await page.locator("#stab-display").click();
  await page.locator("#theme-system").click();
  const system = await page.evaluate(() => ({
    theme: document.documentElement.getAttribute("data-theme"),
    stored: window.localStorage.getItem("autodj.theme"),
  }));
  if (system.theme !== null || system.stored !== "system") {
    problems.push(`after choosing System: ${JSON.stringify(system)}`);
  }
  return problems;
}

// The status message held by the pointer, dismissed by mouse and by
// keyboard, with focus put back where it was.
async function toastBehaviourProblems(page) {
  const problems = [];
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  const visible = () => page.evaluate(() => !document.getElementById("status-toast-box").hidden);
  const focused = () => page.evaluate(() => {
    const element = document.activeElement;
    if (!element || element === document.body) return "body";
    return element.id || element.tagName.toLowerCase();
  });
  await page.mouse.move(5, 5);
  await showToast(page, "Settings saved.");
  // Hovered 6.5 s in, then held past the 8 s it would last unattended.
  await page.waitForTimeout(6500);
  await page.locator("#status-toast").hover();
  await page.waitForTimeout(3000);
  if (!await visible()) problems.push("gone while the pointer was over it");
  await page.mouse.move(5, 5);
  await page.waitForTimeout(3000);
  if (await visible()) problems.push("still showing 3 s after the pointer left");

  // The frame's padding and the gap between the message and the button
  // take no pointer: a control underneath still gets the click.  A probe
  // button under the whole strip stands in for one, whatever the view.
  await page.evaluate(() => {
    const probe = document.createElement("button");
    probe.type = "button";
    probe.id = "audit-under-toast";
    probe.textContent = "under";
    probe.style.cssText = "position:fixed;left:0;right:0;bottom:0;height:6rem;z-index:9000";
    window.__underClicks = 0;
    probe.addEventListener("click", () => { window.__underClicks += 1; });
    // Focus on a control under the toast sends the toast to the top of the
    // window (WCAG 2.4.11), away from the next spot clicked; the probe
    // keeps the click and leaves focus alone.
    probe.addEventListener("mousedown", (event) => event.preventDefault());
    document.body.append(probe);
  });
  await showToast(page, "Settings saved.");
  await settle(page);
  const spotOf = (where) => page.evaluate((name) => {
    const frame = document.getElementById("status-toast-box").getBoundingClientRect();
    const message = document.getElementById("status-toast").getBoundingClientRect();
    const button = document.getElementById("status-toast-dismiss").getBoundingClientRect();
    const middle = frame.top + frame.height / 2;
    return {
      padding: { x: (frame.left + message.left) / 2, y: middle },
      gap: { x: (message.right + button.left) / 2, y: middle },
      message: { x: message.left + message.width / 2, y: message.top + message.height / 2 },
    }[name];
  }, where);
  for (const where of ["padding", "gap", "message"]) {
    const spot = await spotOf(where);
    const clicks = await page.evaluate(() => window.__underClicks);
    await page.mouse.click(spot.x, spot.y);
    const got = await page.evaluate(() => window.__underClicks) - clicks;
    // The message itself takes the pointer: hovering it holds the toast.
    const wanted = where === "message" ? 0 : 1;
    if (got !== wanted) problems.push(`a click on the toast's ${where} reached the page ${got} times, not ${wanted}`);
  }
  if (!await visible()) problems.push("a click through the frame hid the message");
  await page.mouse.move(5, 5);
  await showToast(page, "");
  await page.evaluate(() => document.getElementById("audit-under-toast").remove());

  await page.locator("#tab-queue").focus();
  await showToast(page, "Settings saved.");
  if (await focused() !== "tab-queue") problems.push(`showing the message moved focus to ${await focused()}`);
  await page.locator("#status-toast-dismiss").click();
  if (await visible()) problems.push("Dismiss by mouse did not hide it");
  if (await focused() !== "tab-queue") problems.push(`focus after a click on Dismiss: ${await focused()}`);

  // By keyboard: focus the last tab stop before the toast, show the
  // message, Tab once to Dismiss (so the ring is the keyboard one), then
  // Enter.  Focus must go back to that stop.
  const start = await page.evaluate(() => {
    const box = document.getElementById("status-toast-box");
    const stops = [...document.querySelectorAll(
      "a[href], button, input, select, textarea, summary, [tabindex]",
    )].filter((element) => element.tabIndex >= 0 && !element.disabled
      && !box.contains(element) && element.getClientRects().length
      && !(element.type === "radio" && !element.checked)
      && (box.compareDocumentPosition(element) & window.Node.DOCUMENT_POSITION_PRECEDING));
    const last = stops.at(-1);
    if (!last) return null;
    last.setAttribute("data-audit-start", "");
    return last.id || last.tagName.toLowerCase();
  });
  if (!start) {
    problems.push("no tab stop before the status message");
    return problems;
  }
  await page.locator("[data-audit-start]").focus();
  const before = await focused();
  await showToast(page, "Settings saved.");
  if (await focused() !== before) problems.push(`showing the message moved focus from ${before} to ${await focused()}`);
  await page.keyboard.press("Tab");
  if (await focused() !== "status-toast-dismiss") problems.push(`Tab from ${start} reached ${await focused()}, not Dismiss`);
  await settle(page);
  const ring = await page.evaluate(() => {
    const style = window.getComputedStyle(document.activeElement);
    return style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2;
  });
  if (!ring) problems.push("Dismiss has no visible focus ring");
  await page.keyboard.press("Enter");
  if (await visible()) problems.push("Dismiss by keyboard did not hide it");
  if (await focused() !== before) problems.push(`focus after Enter on Dismiss: ${await focused()}, not ${before}`);
  await page.evaluate(() => document.querySelector("[data-audit-start]")?.removeAttribute("data-audit-start"));
  return problems;
}

// The preview output device setting, in whatever this engine supports.
// Settings > Analysis workers with an announcer token chosen: the Station
// choice shows (#88).  Back to the default choice with `false`.
async function chooseAnnouncer(page, announcer = true) {
  await page.locator("#token-scope").selectOption(announcer ? "announcer" : "analysis-worker");
  await page.locator("#token-station-row").waitFor({ state: announcer ? "visible" : "hidden" });
}

async function tokenScopeProblems(page) {
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  await page.locator("#tab-settings").click();
  await page.locator("#stab-workers").click();
  const scope = page.locator("#token-scope");
  await scope.waitFor({ state: "visible" });
  const problems = [];
  const shown = () => page.evaluate(() => {
    const row = document.getElementById("token-station-row");
    const select = document.getElementById("token-station");
    const help = document.getElementById("token-station-desc");
    const box = (node) => node.getBoundingClientRect().toJSON();
    return {
      visible: Boolean(row.getClientRects().length && help.getClientRects().length),
      label: select.labels?.[0]?.textContent.trim(),
      describedBy: select.getAttribute("aria-describedby") || "",
      options: select.options.length,
      select: box(select),
      scope: box(document.getElementById("token-scope")),
      focus: document.activeElement?.id,
    };
  });
  if ((await shown()).visible) problems.push("the station choice shows for a worker token");
  await scope.focus();
  await chooseAnnouncer(page);
  const info = await shown();
  if (!info.visible) problems.push("the station choice does not show for an announcer token");
  if (info.label !== "Station") problems.push(`station label is ${JSON.stringify(info.label)}`);
  if (info.describedBy !== "token-station-desc") problems.push("station help is not linked");
  if (info.options < 1) problems.push("no station to choose");
  if (info.focus !== "token-scope") problems.push(`choosing moved focus to ${info.focus}`);
  for (const [what, rect] of [["station", info.select], ["token use", info.scope]]) {
    if (rect.width < 24 || rect.height < 24) {
      problems.push(`${what} select is ${Math.round(rect.width)}x${Math.round(rect.height)}`);
    }
  }
  await chooseAnnouncer(page, false);
  return problems;
}

// The voice tracker by keyboard: Record keeps focus and says "Recording"
// once; Alt+R from another control stops it without moving focus and lets
// the microphone go; a refused booking is words on the page, linked to
// Book voice track, with focus still there.
async function voiceTrackerProblems(page) {
  await standInForVoiceTracker(page);
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  await page.locator("#tab-voice").click();
  await page.locator("#vt-slot").waitFor({ state: "visible" });
  const problems = [];
  await page.evaluate(() => {
    window.__voiceSaid = [];
    const region = document.getElementById("vt-status");
    new window.MutationObserver(() => {
      if (region.textContent) window.__voiceSaid.push(region.textContent);
    }).observe(region, { childList: true, characterData: true, subtree: true });
  });
  const state = () => page.evaluate(() => ({
    pressed: document.getElementById("vt-record").getAttribute("aria-pressed"),
    focus: document.activeElement?.id || document.activeElement?.tagName.toLowerCase(),
    said: [...window.__voiceSaid],
    stopped: window.__micStopped || 0,
  }));
  await page.locator("#vt-record").focus();
  await page.keyboard.press("Enter");
  await page.waitForTimeout(300);
  let now = await state();
  if (now.pressed !== "true") problems.push("Enter on Record did not start recording");
  if (now.focus !== "vt-record") problems.push(`starting moved focus to ${now.focus}`);
  if (now.said.length !== 1 || !/^Recording\./.test(now.said[0])) problems.push(`starting said ${JSON.stringify(now.said)}`);
  await page.locator("#vt-placement").focus();
  await page.keyboard.press("Alt+KeyR");
  await page.waitForTimeout(300);
  now = await state();
  if (now.pressed !== "false") problems.push("Alt+R did not stop recording");
  if (now.focus !== "vt-placement") problems.push(`Alt+R moved focus to ${now.focus}`);
  if (now.stopped < 1) problems.push("the microphone was not let go");
  if (now.said.length !== 2 || !/^Recording stopped/.test(now.said[1])) problems.push(`stopping said ${JSON.stringify(now.said)}`);
  await page.locator("#vt-book").focus();
  await page.keyboard.press("Enter");
  await page.locator("#vt-error").waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
  const refused = await page.evaluate(() => ({
    error: document.getElementById("vt-error").textContent.trim(),
    shown: !document.getElementById("vt-error").hidden,
    linked: (document.getElementById("vt-book").getAttribute("aria-describedby") || "").split(" "),
    focus: document.activeElement?.id,
  }));
  if (!refused.shown || !refused.error) problems.push("a refused booking shows no error");
  if (!refused.linked.includes("vt-error")) problems.push("the error is not linked to Book voice track");
  if (refused.focus !== "vt-book") problems.push(`a refused booking moved focus to ${refused.focus}`);
  await page.locator("#vt-discard").click();
  // Leaving the tab stops a recording and lets the microphone go, said
  // once in the page-wide region (the tab's own is hidden with it).
  await page.locator("#vt-error").waitFor({ state: "hidden", timeout: 5000 }).catch(() => {});
  const before = (await state()).stopped;
  await page.locator("#vt-record").click();
  await page.waitForFunction(() => document.getElementById("vt-record").getAttribute("aria-pressed") === "true");
  await page.locator("#tab-carts").click();
  await page.waitForTimeout(300);
  const left = await page.evaluate(() => ({
    pressed: document.getElementById("vt-record").getAttribute("aria-pressed"),
    stopped: window.__micStopped || 0,
    said: document.getElementById("sr-status").textContent,
  }));
  if (left.pressed !== "false") problems.push("leaving the tab did not stop recording");
  if (left.stopped <= before) problems.push("leaving the tab kept the microphone");
  if (!/^You left the Voice tracks tab/.test(left.said)) problems.push(`leaving the tab said ${JSON.stringify(left.said)}`);
  await page.locator("#tab-voice").click();
  await page.locator("#vt-discard").click();
  return problems;
}

async function previewOutputProblems(page) {
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  await page.locator("#tab-settings").click();
  await page.locator("#stab-sound").click();
  const select = page.locator("#preview-device");
  await select.waitFor({ state: "visible" });
  // Let the device list load.
  await page.waitForTimeout(300);
  const problems = [];
  const info = await page.evaluate(() => {
    const element = document.getElementById("preview-device");
    const button = document.getElementById("preview-device-choose");
    const note = document.getElementById("preview-device-note");
    const box = (node) => node && node.getClientRects().length
      ? node.getBoundingClientRect().toJSON() : null;
    return {
      label: element.labels?.[0]?.textContent.trim(),
      describedBy: element.getAttribute("aria-describedby") || "",
      disabled: element.disabled,
      select: box(element),
      button: box(button),
      buttonDesc: button && !button.hidden
        ? (document.getElementById(button.getAttribute("aria-describedby") || "")?.textContent || "").trim()
        : null,
      noteShown: Boolean(note && !note.hidden && note.textContent.trim()),
    };
  });
  if (info.label !== "Preview output device") problems.push(`label is ${JSON.stringify(info.label)}`);
  if (!info.describedBy.split(" ").includes("preview-device-desc")) problems.push("help is not linked");
  if (info.buttonDesc === "") problems.push("button has no description");
  for (const [what, rect] of [["select", info.select], ["button", info.button]]) {
    if (rect && (rect.width < 24 || rect.height < 24)) {
      problems.push(`${what} is ${Math.round(rect.width)}x${Math.round(rect.height)}`);
    }
  }
  if (info.disabled) {
    // No setSinkId: the reason is words on the page, linked to the select.
    if (!info.noteShown) problems.push("unsupported browser shows no reason");
    if (!info.describedBy.includes("preview-device-note")) problems.push("reason is not linked");
    return problems;
  }
  await select.focus();
  await select.selectOption("");
  try {
    await page.waitForFunction(() => /Previews play on/.test(
      document.getElementById("settings-status").textContent), null, { timeout: 3000 });
  } catch {
    problems.push("a change was not said in the status region");
  }
  if (!await page.evaluate(() => document.activeElement?.id === "preview-device")) {
    problems.push("a change moved focus");
  }
  return problems;
}

// The public status pages to audit: the default station's, and each
// station's that the station list names (a paired or open server lists
// them; otherwise only /status is audited).
async function statusPagePaths(page) {
  await page.goto(`${BASE}/status`, { waitUntil: "networkidle", timeout: 20000 });
  const listed = await page.evaluate(async () => {
    try {
      const response = await fetch("/api/stations", { headers: { Accept: "application/json" } });
      if (!response.ok) return [];
      const body = await response.json();
      return (body.stations || []).map((station) => station.status_path).filter(Boolean);
    } catch {
      return [];
    }
  });
  return [...new Set(["/status", ...listed])];
}

function statusThemeState(page) {
  return page.evaluate(() => {
    const [r, g, b] = (window.getComputedStyle(document.body).backgroundColor.match(/\d+/g) || [])
      .map(Number);
    return {
      theme: document.documentElement.getAttribute("data-theme"),
      dark: (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 < 0.5,
    };
  });
}

// Every status page in every theme mode, then the layout checks.
async function auditStatusPages(browser, out) {
  const probe = await browser.newContext({ bypassCSP: true });
  const paths = await statusPagePaths(await probe.newPage());
  await probe.close();
  out.statusPaths = paths;
  for (const mode of THEME_MODES) {
    const context = await themeContext(browser, mode);
    const page = await newPage(context);
    page.on("pageerror", (error) => out.pageerrors.push(`${mode.name} status: ${error.message}`));
    try {
      for (const path of paths) {
        const where = `${mode.name} ${path}`;
        // A stopped station's page answers 503 and is still a whole page.
        await page.goto(`${BASE}${path}`, { waitUntil: "networkidle", timeout: 20000 });
        await settle(page);
        const problems = [];
        for (const violation of await axeViolations(page)) {
          problems.push(`axe ${violation.id}: ${violation.targets.join(", ")}`);
        }
        const state = await statusThemeState(page);
        if (state.theme !== mode.stored) problems.push(`data-theme ${state.theme}`);
        if (state.dark !== (mode.theme === "dark")) problems.push(`background not ${mode.theme}`);
        await installContrastHelpers(page);
        problems.push(...await focusProblems(page, "body", { measureColour: true }));
        if (problems.length) out.statusPage[where] = problems;
      }
    } finally {
      await context.close();
    }
  }
  for (const [label, viewport, fontSize] of [
    ["reflow 320", { width: 320, height: 640 }, ""],
    ["text 200%", { width: 1280, height: 800 }, "200%"],
  ]) {
    const context = await browser.newContext({ viewport, reducedMotion: "reduce",
      colorScheme: "light", bypassCSP: true });
    if (fontSize) {
      await context.addInitScript((size) => {
        document.addEventListener("DOMContentLoaded", () => {
          document.documentElement.style.fontSize = size;
        });
      }, fontSize);
    }
    const page = await newPage(context);
    try {
      for (const path of paths) {
        await page.goto(`${BASE}${path}`, { waitUntil: "networkidle", timeout: 20000 });
        const overflow = await horizontalOverflow(page);
        if (overflow) out.statusPage[`${label} ${path}`] = [JSON.stringify(overflow)];
      }
    } finally {
      await context.close();
    }
  }
}

export async function audit(name, launcher) {
  const browser = await launcher.launch();
  const out = {
    browser: name, axe: {}, nonText: {}, focusColour: {}, toast: {}, themeState: {},
    reflow320: {}, text200: {}, smallChoices: {}, focus: {}, behaviour: {}, statusPage: {},
    statusPaths: [], pageerrors: [],
  };
  if (SHOTS) mkdirSync(SHOTS, { recursive: true });
  try {
    for (const mode of THEME_MODES) await auditTheme(browser, mode, out);
    await auditStatusPages(browser, out);

    const wide = await browser.newContext({ viewport: { width: 1280, height: 800 },
      reducedMotion: "reduce", colorScheme: "light", bypassCSP: true });
    const page = await newPage(wide);
    page.on("pageerror", (error) => out.pageerrors.push(error.message));
    await eachTarget(page, async (target) => {
      const small = await smallChoiceControls(page);
      if (small.length) out.smallChoices[target] = small;
      await installContrastHelpers(page);
      const problems = await focusProblems(page, startOf(target), { measureColour: true });
      if (problems.length) out.focus[target] = problems;
    });
    const setting = await themeSettingProblems(page);
    if (setting.length) out.behaviour.theme = setting;
    const toast = await toastBehaviourProblems(page);
    if (toast.length) out.behaviour.toast = toast;
    const preview = await previewOutputProblems(page);
    if (preview.length) out.behaviour.previewOutput = preview;
    const tokenScope = await tokenScopeProblems(page);
    if (tokenScope.length) out.behaviour.tokenScope = tokenScope;
    const voiceTracker = await voiceTrackerProblems(page);
    if (voiceTracker.length) out.behaviour.voiceTracker = voiceTracker;
    // 200 % text at the same window width.
    await wide.addInitScript(() => {
      document.addEventListener("DOMContentLoaded", () => {
        document.documentElement.style.fontSize = "200%";
      });
    });
    await eachTarget(page, async (target) => {
      const overflow = await horizontalOverflow(page);
      if (overflow) out.text200[target] = overflow;
    });
    await wide.close();

    const narrow = await browser.newContext({ viewport: { width: 320, height: 640 },
      reducedMotion: "reduce", colorScheme: "light" });
    const phone = await newPage(narrow);
    phone.on("pageerror", (error) => out.pageerrors.push(error.message));
    await eachTarget(phone, async (target) => {
      const overflow = await horizontalOverflow(phone);
      if (overflow) out.reflow320[target] = overflow;
      if (target === "settings/workers") {
        await chooseAnnouncer(phone);
        const shown = await horizontalOverflow(phone);
        if (shown) out.reflow320[`${target} announcer`] = shown;
        await chooseAnnouncer(phone, false);
      }
    });
    await narrow.close();
    return out;
  } finally {
    await browser.close();
  }
}

export function validateVisualAudit(name, result) {
  const failures = [];
  for (const key of [
    "axe", "nonText", "focusColour", "toast", "themeState",
    "reflow320", "text200", "smallChoices", "focus", "behaviour", "statusPage",
  ]) {
    const entries = Object.entries(result[key]);
    if (entries.length) failures.push(`${key}: ${JSON.stringify(Object.fromEntries(entries))}`);
  }
  if (result.pageerrors.length) failures.push(`page errors: ${result.pageerrors.join("; ")}`);
  if (failures.length) throw new Error(`${name} visual audit\n${failures.join("\n")}`);
}

await runAudit({
  audit,
  report: "visual_audit.json",
  validate: validateVisualAudit,
});
