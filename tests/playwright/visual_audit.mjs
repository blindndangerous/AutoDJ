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
//       - Now Playing's Hour preview opened, with a stood-in answer: its
//         table of fixed and predicted rows and its backtiming list;
//       - Song picking with Fit songs to the hour ticked (not saved), so its
//         window field and help are shown (#126); and Schedule with a hard
//         event's Wait up to seconds refused in the browser, the error
//         linked and focused, against a stood-in action list;
//       - Settings > Stream (when the server streams) with its Listener
//         statistics stood in for: the summary, the tables by format and
//         by day (a long run of days, every format) and the session
//         lengths, and the address list of the formats that are on;
//       - the status message with its Dismiss button, as a confirmation
//         and as an error, through axe-core as well;
//       - the footer's link to the running commit, when the server knows
//         it: text 4.5:1 and underlined, 24 px tall, and a 2 px focus
//         ring 3:1 when reached by keyboard;
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
//       - Track info with a song open, its server answers stood in for: the
//         Tags form, a refused save with its errors, the Save or Discard
//         dialog, and Matched intros with a song intro (skipped now) and an
//         artist intro, then Upload intro refused with its error linked;
//       - Library tools > Shows, its server answers stood in for: a show on
//         the air with a skipped song, the saved shows, one show's list open
//         with its Move up, Move down and Remove buttons and a search result,
//         and Save show refused with its error linked to Name;
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
//   - the station switcher (#70), a menu button in the banner: with its
//     menu open it is measured like a view in every theme mode (axe-core,
//     non-text contrast, focus rings) and at 320 px and 200 % text; and
//     in the System light mode it opens with Enter on the checked item,
//     draws a 2 px focus ring 3:1 on each item and 44 px targets, closes
//     with Escape back on the button, and, when the server really has
//     several stations, switches to each other station (the heading and
//     the socket follow; a stopped one shows its off-air note and Start)
//     and back.  A server with one station gets a stand-in second station
//     in /api/stations, so the menu is always measured; it is never
//     switched to.
//
//   - the public status pages: /status, and each station's
//     /stations/<slug>/status that /api/stations lists (a stopped
//     station's "off air" page too), in every theme mode: axe-core's
//     WCAG 2.2 A/AA rules, the theme applied, and every focus stop's
//     outline 3:1; in the System light mode reflow at 320 px and 200 %
//     text.
//   - the listener request pages, where the server has them turned on:
//     /requests and each listed station's /stations/<slug>/requests, as
//     first shown, after a search that finds nothing and with a search
//     refused: the same checks, plus every control's boundary 3:1.
//   - the player pages, where the server has them turned on: /player and
//     /embed, and each listed station's /stations/<slug>/player and
//     /embed: the same checks, plus every control's boundary 3:1
//     (1.4.11).  Every public page must also not scroll sideways at
//     320 px with 200 % text.  The embed follows the system theme, not the web UI's
//     stored choice, and ?theme=light or ?theme=dark pins it.  Once per
//     page: nothing plays or loads on its own, the browser's own audio
//     controls are swapped for a Play button and a labelled Volume slider
//     (when the station streams), the slider moves with the arrow keys
//     and reads its value as a percentage without moving focus or
//     announcing anything, and the song line is the only live region.
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
  id: "a".repeat(32), path: TRACK_PATH, kind: "music", ...TRACK_TAGS, year: 1999, bpm: 120, length: 215.4,
  last_played: 0, key_label: "8A", analysis: "ready", loudness_ready: true, allow_outro: false,
  points: { start: 0, intro: 8, outro: 200, segue: 210, end: 215 },
  point_sources: { start: "analysis", intro: "analysis", outro: "analysis", segue: "analysis", end: "analysis" },
  tags: TRACK_TAGS, tags_writable: true, beets_writeback: true,
};
// Its matched intros (#127): one for the song, with dates and skipped now,
// and one for the artist with a long unbroken label, for the reflow checks,
// and the note that says the station's file skips an intro that is not valid.
const TRACK_INTROS = {
  station: "default", active: true, reason: null,
  problem: "1 matched intro in matched_intros.json is not valid and skipped until the file is "
    + "corrected: An_Unbroken_Broken_Intro_Identifier_0001. AutoDJ keeps it in the file as written.",
  limits: { max_bytes: 40 * 1024 * 1024, max_seconds: 60, extensions: [".mp3", ".wav"] },
  track: { id: TRACK_RECORD.id, title: TRACK_TAGS.title, artist: TRACK_TAGS.artist },
  song: [{
    id: "b".repeat(32), applies_to: "song", label: "Brand new from Example Artist",
    rules_text: "Plays about 50% of the times this song comes up, at most once every 1 hour.",
    dates_text: "Plays until 26 December 2026", playable: false,
    unplayable_text: "expired after 26 December 2026", speech_s: 4.2,
  }],
  artist: [{
    id: "c".repeat(32), applies_to: "artist", label: "An_Unbroken_Intro_Label_Without_Any_Spaces_0001",
    rules_text: "Plays every time a song by Example Artist comes up, with no wait between plays.",
    dates_text: "", playable: true, unplayable_text: "", speech_s: 2,
  }],
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
  await page.route("**/api/intros?*", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(TRACK_INTROS),
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
  // Matched intros: both lists shown, then Upload intro with no file chosen,
  // its error shown under the field and linked to it.
  await page.locator("#intros-artist-list li").first().waitFor({ state: "visible" });
  await page.locator("#intro-upload").click();
  await page.locator("#intro-file-error").waitFor({ state: "visible" });
  await visit("track-info/intros-error");
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

// Now Playing, Hour preview (#126), opened: the server's answer is stood
// in for, so its table, with fixed and predicted rows, and its backtiming
// list are measured on an empty library.  One title has no spaces, for
// the reflow checks.
const HOUR_PREVIEW_ITEM = (title, status, fixed, starts, words) => ({
  kind: "song",
  status,
  fixed,
  status_text: fixed ? { on_air: "Fixed: on the air", ready: "Fixed: ready to play", queued: "Fixed: in the queue" }[status] : "Predicted",
  title,
  starts_text: starts,
  starts_words: words,
  seconds: 200,
  length_text: "3:20",
  length_words: "3:20",
});
const HOUR_PREVIEW = {
  station: "default",
  minutes: 60,
  reason: null,
  summary: "Top of the hour, 1:00:00 PM: lands about 35 seconds early, at the song change into Example Artist, Song 4 (predicted) at 12:59:25 PM. That song runs 2:45 past 1:00:00 PM. Next hour: 3 fixed and 2 predicted, until 1:10:00 PM.",
  items: [
    HOUR_PREVIEW_ITEM("Example Artist, Song 1", "on_air", true, "12:49:10 PM", "12:49:10 PM"),
    HOUR_PREVIEW_ITEM("Another Example, An_Exceptionally_Long_Title_Without_Any_Spaces_From_A_Tagless_File_0001", "ready", true, "12:52:30 PM", "12:52:30 PM"),
    HOUR_PREVIEW_ITEM("Example Artist, Song 3", "queued", true, "12:55:50 PM", "12:55:50 PM"),
    HOUR_PREVIEW_ITEM("Example Artist, Song 4", "predicted", false, "12:59:25 PM", "12:59:25 PM"),
    HOUR_PREVIEW_ITEM("Example Artist, Song 5", "predicted", false, "1:02:45 PM", "1:02:45 PM"),
  ],
  points: [{ kind: "hour", text: "Top of the hour, 1:00:00 PM: lands about 35 seconds early." }],
  counts: { fixed: 3, predicted: 2 },
  stopped: null,
  notes: ["Predicted songs come from a dry run of the station's own picker."],
};
const hourPreviewPages = new WeakSet();

// Settings > Stream > Listener statistics (#50), stood in for so its
// tables, summary and session lengths are measured filled, with every
// format and a long run of days, on a server nobody has listened to.
const STATS_DAY = (date, peak, sessions) => ({
  date, peak, peak_at: peak ? "21:45" : null, sessions, seconds: sessions * 1260,
  longest: sessions ? 11400 : 0, bands: [1, 2, 0, Math.max(0, sessions - 4), 1, 0],
  formats: peak ? { mp3: { peak, sessions, seconds: sessions * 1260 } } : {},
});
const LISTENER_STATS = {
  keep_days: 90,
  days_shown: 30,
  today: "2026-10-13",
  now: { total: 12, formats: { mp3: 7, aac: 3, opus: 2 } },
  days: [
    STATS_DAY("2026-10-13", 14, 38), STATS_DAY("2026-10-12", 9, 21),
    STATS_DAY("2026-10-11", 0, 0), STATS_DAY("2026-10-10", 100, 1234),
  ],
  period: {
    sessions: 1293, seconds: 1629180, longest: 11400, peak: 100, peak_date: "2026-10-10",
    peak_at: "21:45", bands: [4, 8, 0, 1281, 4, 0],
    formats: {
      mp3: { peak: 100, sessions: 1293, seconds: 1629180 },
      aac: { peak: 3, sessions: 40, seconds: 50400 },
      opus: { peak: 2, sessions: 12, seconds: 15120 },
    },
  },
  bands: ["Under 1 minute", "1 to 5 minutes", "5 to 15 minutes", "15 minutes to 1 hour",
    "1 to 3 hours", "3 hours or more"],
};
const listenerStatsPages = new WeakSet();

async function standInForListenerStats(page) {
  if (listenerStatsPages.has(page)) return;
  listenerStatsPages.add(page);
  await page.route("**/stream/stats?*", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(LISTENER_STATS),
  }));
}

async function standInForHourPreview(page) {
  if (hourPreviewPages.has(page)) return;
  hourPreviewPages.add(page);
  await page.route("**/api/hour-preview*", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(HOUR_PREVIEW),
  }));
}

// Settings > Schedule: one action with no settings of its own, so the
// form's own checks reach Wait up to seconds whatever the server offers.
const SCHEDULE = {
  timezone: "UTC",
  actions: [{ name: "audit", description: "Audit action", fields: [] }],
  events: [], upcoming: [], runs: [],
};
const schedulePages = new WeakSet();

async function standInForSchedule(page) {
  if (schedulePages.has(page)) return;
  schedulePages.add(page);
  await page.route("**/api/schedule", (route) => (route.request().method() === "GET"
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(SCHEDULE) })
    : route.continue()));
}

// Song picking: Fit songs to the hour ticked in the page only, without the
// change event, so nothing is saved; its window field and help show (#126).
async function openHourFit(page, visit) {
  const toggle = page.locator("#pb-hour-fit");
  await toggle.waitFor({ state: "visible" });
  const was = await toggle.isChecked();
  await toggle.evaluate((box) => { box.checked = true; });
  await page.locator("#pb-hour-fit-window").waitFor({ state: "visible" });
  await settle(page);
  await visit("settings/picking/hour-fit");
  await toggle.evaluate((box, checked) => { box.checked = checked; }, was);
}

// Schedule: a hard event's Wait up to seconds refused in the browser, the
// field marked invalid and focused, the error shown and linked.  Nothing
// is saved; Cancel closes the form.
async function openScheduleWaitError(page, visit) {
  await page.locator("#schedule-add").click();
  await page.locator("#schedule-name").fill("News");
  await page.locator("#schedule-time").fill("13:00");
  await page.locator("#schedule-timing").selectOption("hard");
  const wait = page.locator("#schedule-wait");
  await wait.waitFor({ state: "visible" });
  await wait.fill("301");
  await page.locator("#schedule-save").click();
  await page.locator('#schedule-wait[aria-invalid="true"]').waitFor();
  await page.locator("#schedule-form-error").waitFor({ state: "visible" });
  const linked = await wait.evaluate((input) => input === document.activeElement
    && input.getAttribute("aria-describedby").split(/\s+/).includes("schedule-form-error"));
  if (!linked) throw new Error("Wait up to seconds error is not focused and linked");
  await settle(page);
  await visit("settings/schedule/wait-error");
  await page.locator("#schedule-cancel").click();
}

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

// Song picking > Repeats: daypart start hours refused in the browser, the
// field marked invalid and focused, its message shown under it (#129).  Nothing is saved.
async function openRepeatHoursError(page, visit) {
  const hours = page.locator("#repeat-daypart-hours");
  await hours.waitFor({ state: "visible" });
  const before = await hours.inputValue();
  await hours.fill("6, 6");
  await page.locator("#repeat-save").click();
  await page.locator('#repeat-daypart-hours[aria-invalid="true"]').waitFor();
  await page.locator("#repeat-error").waitFor({ state: "visible" });
  await visit("settings/picking/repeat-hours-error");
  await hours.fill(before);
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
  const condition = page.locator("#clock-step-conditions").getByRole("group").first();
  await condition.getByLabel("Condition type", { exact: true }).selectOption("title");
  await condition.getByLabel("Value", { exact: true }).fill("Audit");
  await page.locator("#clock-step-hours-from").fill("22:00");
  await page.locator("#clock-add-step").click();
  await page.locator("#clock-step-hours-error").waitFor({ state: "visible" });
  await visit("library/step-hours-error");
}

// Library tools > Shows (#106): the server's answers are stood in for, so a
// show on the air, a saved list and a search result look the same on an empty
// library as on a full one.  The titles are long and unbroken, for the reflow
// checks.
const SHOW_ID = "d".repeat(32);
const SHOW_SONGS = [
  { path: "/music/1.flac", title: "Example Artist, An_Exceptionally_Long_Title_Without_Any_Spaces_From_A_Tagless_File_0003",
    missing: false, seconds: 215, length_text: "3:35", length_words: "3 minutes 35 seconds" },
  { path: "/music/2.flac", title: "Another Example, Second Song", missing: false, seconds: 187,
    length_text: "3:07", length_words: "3 minutes 7 seconds" },
  { path: "/music/gone.flac", title: "gone.flac", missing: true, seconds: 0, length_text: "", length_words: "" },
];
const SHOW = {
  id: SHOW_ID, name: "Remembering the 2000s", source: "category", category: "hits", clockwheel: "",
  source_text: "Category Remembering the 2000s", target_minutes: 60, count: 3, missing: 1,
  total_seconds: 402, total_text: "6:42", total_words: "6 minutes 42 seconds",
  generated_at: "2026-10-12T20:00:00+00:00", note: "The list is 6 minutes 42 seconds, shorter than the target of 1 hour: the songs the repeat rules and the DMCA limits allow ran out.",
  events: ["Saturday throwback"],
};
const SHOWS = {
  shows: [SHOW],
  categories: [{ id: "hits", name: "Remembering the 2000s" }],
  clockwheels: ["Throwback"],
  on_air: {
    id: SHOW_ID, name: "Remembering the 2000s", total: 3, played: 1, waiting: 1, to_come: 0,
    skipped: [{ path: "/music/gone.flac", title: "gone.flac", reason: "it is not in the library" }],
    skipped_count: 1, started_at: "2026-10-12T20:00:00+00:00",
  },
  problem: null,
  limits: { max_shows: 200, max_songs: 500, min_minutes: 5, max_minutes: 360 },
  timezone: "UTC",
};
const showPages = new WeakSet();

async function standInForShows(page) {
  if (showPages.has(page)) return;
  showPages.add(page);
  await page.route("**/api/shows**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/api/shows") {
      return route.fulfill({ status: 400, contentType: "application/json",
        body: JSON.stringify({ detail: "A show named Remembering the 2000s already exists", field: "name" }) });
    }
    const body = path === "/api/shows" ? SHOWS : { ...SHOW, songs: SHOW_SONGS };
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.route("**/api/search?*", (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify({ results: [{ path: "/music/9.flac", title: "An_Exceptionally_Long_Title_Without_Any_Spaces_0009",
      artist: "Example Artist", album: "", length: 200, display_name: "" }] }),
  }));
}

// Shows: one list open with a search result, then Save show refused by the
// server with its error on the page, linked to Name and focused.
async function openShowStates(page, visit) {
  await page.locator("#shows-list button[data-action='open']").first().click();
  await page.locator("#shows-songs li[data-index]").first().waitFor({ state: "visible" });
  await page.locator("#shows-search").fill("example");
  await page.locator("#shows-search-run").click();
  await page.locator("#shows-results li").first().waitFor({ state: "visible" });
  await visit("library/show-list");
  await page.locator("#shows-close").click();
  await page.locator("#shows-add").click();
  await page.locator("#shows-name").fill("Remembering the 2000s");
  await page.locator("#shows-save").click();
  await page.locator("#shows-form-error").waitFor({ state: "visible" });
  await visit("library/show-error");
  await page.locator("#shows-cancel").click();
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

// The switcher shows only with two stations.  A server with one gets a
// stopped stand-in in this page's /api/stations, which is measured but
// never chosen.
const STAND_IN_STATION = {
  slug: "audit-stand-in", name: "Audit Stand-in Station", state: "stopped", listeners: 0,
  title: null, status_path: "/stations/audit-stand-in/status",
  nowplaying_path: "/api/stations/audit-stand-in/nowplaying", stream_path: null, m3u_path: null,
};
const stationPages = new WeakMap();

async function standInForStations(page) {
  if (stationPages.has(page)) return;
  const seen = { real: [] };
  stationPages.set(page, seen);
  await page.route("**/api/stations", async (route) => {
    const response = await route.fetch();
    const body = await response.json().catch(() => null);
    seen.real = Array.isArray(body?.stations) ? body.stations : [];
    if (seen.real.length >= 2) return route.fulfill({ response });
    return route.fulfill({ response, json: { stations: [...seen.real, STAND_IN_STATION] } });
  });
}

function realStations(page) {
  const list = stationPages.get(page)?.real || [];
  return list.length >= 2 ? list : [];
}

// The switcher's menu, open on the checked station, as a state of Now Playing.
async function openStationMenu(page, visit) {
  const button = page.locator("#station-button");
  await button.waitFor({ state: "visible", timeout: 5000 });
  await button.focus();
  await page.keyboard.press("Enter");
  await page.locator("#station-menu").waitFor({ state: "visible" });
  await settle(page);
  await visit("now/station-menu");
  if (await page.locator("#station-menu").isVisible()) await page.keyboard.press("Escape");
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
  await standInForHourPreview(page);
  await standInForListenerStats(page);
  await standInForVoiceTracker(page);
  await standInForShows(page);
  await standInForStations(page);
  await standInForSchedule(page);
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  for (const view of VIEWS) {
    await page.locator(`#tab-${view}`).click();
    await page.locator(`#panel-${view}`).waitFor({ state: "visible" });
    // Under reduced motion every change still runs a 0.01 ms transition,
    // which takes a frame; let it end before colours are measured.
    await page.waitForTimeout(100);
    if (view !== "settings") {
      await openEveryCard(page);
      if (view === "now") {
        await page.locator("#hour-preview-table").waitFor({ state: "visible", timeout: 10000 });
      }
      await settle(page);
      await visit(view);
      if (view === "track-info") await openTrackTags(page, visit);
      if (view === "library") {
        await openDaypartErrors(page, visit);
        await openShowStates(page, visit);
      }
      if (view === "voice") await openVoiceStates(page, visit);
      if (view === "now") await openStationMenu(page, visit);
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
      if (id === "stab-picking") {
        await openRotationTest(page, visit);
        await openRepeatHoursError(page, visit);
        await openHourFit(page, visit);
      }
      if (id === "stab-schedule") await openScheduleWaitError(page, visit);
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

// Checkboxes and radio buttons under 24 px, and the Stream settings'
// format Copy buttons and statistics buttons (#50) under the 44 px every
// other button has.
function smallChoiceControls(page) {
  return page.evaluate(() => {
    const measure = (selector, least) => [...document.querySelectorAll(selector)]
      .filter((control) => control.getClientRects().length)
      .map((control) => ({
        id: control.id || control.textContent.trim() || control.tagName.toLowerCase(),
        ...control.getBoundingClientRect().toJSON(),
      }))
      .filter(({ width, height }) => width < least || height < least)
      .map(({ id, width, height }) => `${id} ${Math.round(width)}x${Math.round(height)}`);
    return [
      ...measure('input[type="checkbox"], input[type="radio"]', 23.5),
      ...measure("#stream-formats-list button, #listener-stats button", 43.5),
    ];
  });
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

// The footer's link to the running commit, there when /api/version sends a
// commit_url (a git checkout, or an image built with AUTODJ_COMMIT): text
// 4.5:1 and underlined (1.4.3, 1.4.1), 24 px tall (2.5.8), and reached by
// keyboard with a 2 px focus ring 3:1 against the footer (2.4.7, 1.4.11).
async function footerLinkProblems(page) {
  const expected = await page.evaluate(async () => {
    const response = await fetch("/api/version", { cache: "no-cache" });
    return response.ok ? Boolean((await response.json()).commit_url) : false;
  });
  const link = page.locator("#ver-commit-link");
  if (!expected) return await link.count() ? ["commit link without a commit_url"] : [];
  if (!await link.count()) return ["no commit link though /api/version sent commit_url"];
  // Reached from the keyboard, so the ring is the :focus-visible one; WebKit
  // can skip links on Tab, so focus() after a key press stands in there.
  await link.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  if (!await link.evaluate((element) => element === document.activeElement)) await link.focus();
  await settle(page);
  const problems = await link.evaluate((element) => {
    const { parse, over, ratio, behind } = window.__contrast;
    const found = [];
    const style = window.getComputedStyle(element);
    const back = behind(element.parentElement);
    const text = ratio(over(parse(style.color), back), back);
    if (text < 4.5) found.push(`text ${text.toFixed(2)}:1`);
    if (!style.textDecorationLine.includes("underline")) found.push("not underlined");
    const { height } = element.getBoundingClientRect();
    if (height < 23.5) found.push(`${Math.round(height)} px tall`);
    if (!element.matches(":focus-visible")) found.push("no :focus-visible on keyboard focus");
    else if (style.outlineStyle === "none" || parseFloat(style.outlineWidth) < 2) {
      found.push("no 2 px focus outline");
    } else {
      const ring = ratio(over(parse(style.outlineColor), back), back);
      if (ring < 3) found.push(`focus ring ${ring.toFixed(2)}:1`);
    }
    return found;
  });
  await link.evaluate((element) => element.blur());
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
        record("footer", target, await footerLinkProblems(page));
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
    const hiddenInClosedDetails = (element) => {
      const closed = element.parentElement?.closest("details:not([open])");
      return Boolean(closed) && !(element.tagName === "SUMMARY" && element.parentElement === closed);
    };
    const box = document.getElementById("status-toast-box");
    const stops = [...document.querySelectorAll(
      "a[href], button, input, select, textarea, summary, [tabindex]",
    )].filter((element) => element.tabIndex >= 0 && !element.disabled
      && !box.contains(element) && element.getClientRects().length
      // Inside a closed <details> only its summary takes focus, though some
      // engines still give the hidden content boxes.
      && !hiddenInClosedDetails(element)
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

// The station switcher by keyboard (#70): the APG menu button pattern,
// focus rings and target sizes on the items, and with real stations the
// switch itself, there and back.
async function stationSwitcherProblems(page) {
  const problems = [];
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  await installContrastHelpers(page);
  const button = page.locator("#station-button");
  await button.waitFor({ state: "visible", timeout: 5000 });
  const state = () => page.evaluate(() => {
    const element = document.activeElement;
    const style = window.getComputedStyle(element);
    const { parse, over, ratio, behind } = window.__contrast;
    const back = behind(element);
    const rect = element.getBoundingClientRect();
    return {
      id: element.id,
      role: element.getAttribute("role"),
      station: element.dataset.station || null,
      checked: element.getAttribute("aria-checked"),
      expanded: document.getElementById("station-button").getAttribute("aria-expanded"),
      open: !document.getElementById("station-menu").hidden,
      ring: style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2
        ? ratio(over(parse(style.outlineColor), back), back) : 0,
      height: rect.height,
      inView: rect.left >= 0 && rect.right <= window.innerWidth,
      heading: document.getElementById("page-heading").textContent,
      offAir: !document.getElementById("station-off-air").hidden,
    };
  });
  const size = await button.boundingBox();
  if (!size || size.height < 44 || size.width < 44) {
    problems.push(`station button ${Math.round(size?.width || 0)}x${Math.round(size?.height || 0)} px, under 44 px`);
  }
  await button.focus();
  await page.keyboard.press("Enter");
  await settle(page);
  let now = await state();
  if (!now.open || now.expanded !== "true") problems.push("Enter did not open the menu");
  if (now.role !== "menuitemradio" || now.checked !== "true") {
    problems.push(`Enter opened on ${now.station} (${now.checked}), not the checked station`);
  }
  const items = await page.locator("#station-menu [role='menuitemradio']").count();
  for (let step = 0; step < items; step += 1) {
    now = await state();
    if (now.ring < 3) problems.push(`${now.station} focus ring ${now.ring.toFixed(2)}:1`);
    if (now.height < 44) problems.push(`${now.station} ${Math.round(now.height)} px high`);
    if (!now.inView) problems.push(`${now.station} not in the window`);
    await page.keyboard.press("ArrowDown");
    await settle(page);
  }
  now = await state();
  if (now.checked !== "true") problems.push("Down Arrow did not wrap to the first station");
  await page.keyboard.press("Escape");
  now = await state();
  if (now.open || now.id !== "station-button" || now.expanded !== "false") {
    problems.push("Escape did not close the menu and return to the button");
  }
  const stations = realStations(page);
  for (const [index, station] of stations.entries()) {
    if (index === 0) continue;
    await button.focus();
    await page.keyboard.press("Enter");
    await page.keyboard.press("Home");
    for (let step = 0; step < index; step += 1) await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await page.waitForFunction((name) => document.getElementById("page-heading").textContent
      === `AutoDJ: ${name}`, station.name, { timeout: 10000 }).catch(() => {});
    // The heading changes at once; the station's state comes after.
    await (station.state === "stopped"
      ? page.locator("#station-off-air").waitFor({ state: "visible", timeout: 10000 })
      : page.waitForFunction(() => document.getElementById("conn-status").className === "connected",
        null, { timeout: 10000 })).catch(() => {});
    await settle(page);
    now = await state();
    if (now.heading !== `AutoDJ: ${station.name}`) problems.push(`switch to ${station.slug}: heading ${now.heading}`);
    if (now.id !== "station-button") problems.push(`switch to ${station.slug}: focus on ${now.id || now.role}`);
    if (station.state === "stopped" && !now.offAir) problems.push(`${station.slug} off air not shown`);
    const violations = await axeViolations(page);
    if (violations.length) problems.push(`${station.slug}: ${JSON.stringify(violations)}`);
  }
  if (stations.length) {
    await button.focus();
    await page.keyboard.press("Enter");
    await page.keyboard.press("Home");
    await page.keyboard.press("Enter");
    const home = stations[0].name === "AutoDJ" ? "AutoDJ" : `AutoDJ: ${stations[0].name}`;
    await page.waitForFunction((text) => document.getElementById("page-heading").textContent === text,
      home, { timeout: 10000 })
      .catch(() => problems.push("could not switch back to the default station"));
  }
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

// The public pages to audit: the default station's, and each station's
// that the station list names (a paired or open server lists them;
// otherwise only the default station's are audited).  Each status page,
// and its player page and embed where the server answers for them (they
// are off unless [player_page] turns them on, and 404 then).
async function publicPagePaths(page) {
  await page.goto(`${BASE}/status`, { waitUntil: "networkidle", timeout: 20000 });
  return page.evaluate(async () => {
    let listed = [];
    try {
      const response = await fetch("/api/stations", { headers: { Accept: "application/json" } });
      if (response.ok) {
        const body = await response.json();
        listed = (body.stations || []).map((station) => station.status_path).filter(Boolean);
      }
    } catch { /* not listed: the default station only */ }
    const pages = [];
    for (const status of new Set(["/status", ...listed])) {
      pages.push({ path: status, kind: "status" });
      for (const kind of ["player", "embed"]) {
        const path = status.replace(/status$/, kind);
        const response = await fetch(path, { method: "HEAD" });
        if (response.status !== 404) pages.push({ path, kind });
      }
      // The request page ([requests]): as first shown, with a search that
      // finds nothing, and with a search refused (its error tied to the field).
      const requests = status.replace(/status$/, "requests");
      const response = await fetch(requests, { method: "HEAD" });
      if (response.status !== 404) {
        pages.push({ path: requests, kind: "requests" });
        if (response.ok) {
          pages.push({ path: `${requests}?q=zz+nothing+like+it`, kind: "requests" });
          pages.push({ path: `${requests}?q=a`, kind: "requests" });
        }
      }
    }
    return pages;
  });
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

// A player page, once: nothing plays on its own, the script's own
// controls replace the browser's, and the Volume slider works from the
// keyboard without moving focus or saying anything.
async function playerBehaviourProblems(page) {
  const problems = [];
  const before = await page.evaluate(() => {
    const audio = document.getElementById("player-audio");
    const toggle = document.getElementById("player-toggle");
    return {
      audio: Boolean(audio),
      autoplay: audio?.hasAttribute("autoplay"),
      paused: audio ? audio.paused : true,
      // currentSrc keeps a stale value after the source is dropped; what
      // matters is that nothing is fetched or buffered.
      loading: audio
        ? audio.networkState === audio.NETWORK_LOADING || audio.readyState > 0
          || audio.hasAttribute("src")
        : false,
      nativeControls: audio?.hasAttribute("controls"),
      toggle: toggle && !toggle.closest("[hidden]") ? toggle.textContent.trim() : null,
      volumeLabel: document.querySelector('label[for="player-volume"]')?.textContent.trim(),
      live: [...document.querySelectorAll('[aria-live]:not([aria-live="off"])')].map((node) => node.id),
      status: [...document.querySelectorAll('[role="status"]')].map((node) => node.id),
    };
  });
  if (!before.audio) {
    // A station that does not stream says so in words instead.
    const said = await page.evaluate(() => /not streaming right now/.test(document.body.textContent));
    if (!said) problems.push("no player and no reason given");
    return problems;
  }
  if (before.autoplay) problems.push("audio has autoplay");
  if (!before.paused || before.loading) problems.push("audio loads or plays before Play is pressed");
  if (before.nativeControls) problems.push("the browser's own controls still show");
  if (before.toggle !== "Play") problems.push(`Play button reads ${JSON.stringify(before.toggle)}`);
  if (before.volumeLabel !== "Volume") problems.push("Volume slider has no visible label");
  if (before.live.join() !== "status-now") problems.push(`live regions ${before.live.join()}`);
  if (before.status.join() !== "player-state") problems.push(`status regions ${before.status.join()}`);
  // From wherever this browser left it (it is remembered per site).
  const start = Number(await page.locator("#player-volume").inputValue());
  const expected = String(Math.max(0, start - 10));
  await page.locator("#player-volume").focus();
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  const after = await page.evaluate(() => {
    const volume = document.getElementById("player-volume");
    return {
      focused: document.activeElement === volume,
      value: volume.value,
      text: volume.getAttribute("aria-valuetext"),
      shown: document.getElementById("player-volume-value").textContent,
      said: document.getElementById("player-state").textContent,
    };
  });
  if (!after.focused) problems.push("the Volume slider lost focus");
  if (after.value !== expected || after.text !== `${expected}%` || after.shown !== `${expected}%`) {
    problems.push(`Volume after two Left arrows ${JSON.stringify(after)}`);
  }
  if (after.said) problems.push(`moving the slider said ${JSON.stringify(after.said)}`);
  return problems;
}

// Every public page in every theme mode, then the layout checks.
async function auditStatusPages(browser, out) {
  const probe = await browser.newContext({ bypassCSP: true });
  const pages = await publicPagePaths(await probe.newPage());
  await probe.close();
  out.statusPaths = pages.map(({ path }) => path);
  for (const mode of THEME_MODES) {
    const context = await themeContext(browser, mode);
    const page = await newPage(context);
    page.on("pageerror", (error) => out.pageerrors.push(`${mode.name} status: ${error.message}`));
    // The embed follows its host, not the web UI's stored theme: the
    // system's, or the one ?theme= pins against the system's.
    const pin = mode.colorScheme === "dark" ? "light" : "dark";
    const visits = pages.flatMap(({ path, kind }) => (kind === "embed" && !mode.stored
      ? [{ path, kind }, { path: `${path}?theme=${pin}`, kind, pinned: pin }]
      : [{ path, kind }]));
    try {
      for (const { path, kind, pinned } of visits) {
        const where = `${mode.name} ${path}`;
        // A stopped station's page answers 503 and is still a whole page.
        await page.goto(`${BASE}${path}`, { waitUntil: "networkidle", timeout: 20000 });
        await settle(page);
        const problems = [];
        for (const violation of await axeViolations(page)) {
          problems.push(`axe ${violation.id}: ${violation.targets.join(", ")}`);
        }
        const theme = kind === "embed" ? pinned || null : mode.stored;
        const dark = kind === "embed"
          ? (pinned || mode.colorScheme) === "dark"
          : mode.theme === "dark";
        const state = await statusThemeState(page);
        if (state.theme !== theme) problems.push(`data-theme ${state.theme}`);
        if (state.dark !== dark) problems.push(`background not ${dark ? "dark" : "light"}`);
        await installContrastHelpers(page);
        if (kind !== "status") problems.push(...await controlBoundaryProblems(page));
        problems.push(...await focusProblems(page, "body", { measureColour: true }));
        if ((kind === "player" || kind === "embed") && mode.name === "system-light" && !pinned) {
          await page.reload({ waitUntil: "networkidle" });
          problems.push(...await playerBehaviourProblems(page));
        }
        if (problems.length) out.statusPage[where] = problems;
      }
    } finally {
      await context.close();
    }
  }
  for (const [label, viewport, fontSize] of [
    ["reflow 320", { width: 320, height: 640 }, ""],
    ["text 200%", { width: 1280, height: 800 }, "200%"],
    // Both at once, as on a phone with large text.
    ["reflow 320 text 200%", { width: 320, height: 640 }, "200%"],
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
      for (const { path } of pages) {
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
    footer: {},
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
    const switcher = await stationSwitcherProblems(page);
    if (switcher.length) out.behaviour.stationSwitcher = switcher;
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
    "reflow320", "text200", "smallChoices", "focus", "behaviour", "statusPage", "footer",
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
