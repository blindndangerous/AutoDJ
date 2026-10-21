// Visual accessibility audit: what a sighted, low-vision or keyboard-only
// user meets in every view and Settings subtab.  Per browser engine it
// checks, with every card opened:
//
//   - in Light and Dark chosen in Settings (each against the opposite
//     system preference, so the choice must win); Firefox in Dark only.
//     System under a light and a dark system preference shows the same
//     page, so for it only the first item below is checked, on one page:
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
//       - Library tools, Intro rules with two rules stood in for: the
//         list, and Add intro rule saved with no liner chosen, its error
//         linked to the first liner's checkbox;
//       - Track info with a song open, its server answers stood in for: the
//         Song structure list (#60, with a long unbroken cue name), the
//         Tags form, a refused save with its errors, the Save or Discard
//         dialog, and Matched intros with a song intro (skipped now) and an
//         artist intro, then Upload intro refused with its error linked;
//         the Right bracket key moves the cue point preview to the next
//         landmark, says it once in #sr-status ("Preview: ...") and leaves
//         focus where it is;
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
//       - Live DJ (#48), its server answers stood in for and the socket
//         pushes naming a live DJ: Now Playing's live line and Talk, Talk
//         latched on, Settings > Live DJ with its DJ list, and Add DJ
//         refused with its error linked.  Talk held with Space keeps focus
//         on it and sets aria-pressed, sends microphone audio and lets the
//         microphone go when released; Alt+M held from another control
//         talks without moving focus and says "Talking" and "Stopped
//         talking" once each, and a quick Alt+M never leaves it on; a
//         quick press of Talk keeps it on, said once and shown in words,
//         until Talk is pressed again; a DJ going live and leaving is said
//         once each.  The microphone, the capture worklet and the talk socket
//         are stubbed;
//       - Queue & Search's Listener requests card, its answers stood in
//         for: a waiting request that would get bridge songs, one with no
//         smooth spot and its reason, a bridged request in the decisions,
//         and the Smooth transitions settings; queue rows marked "Bridge
//         to <request>"; and a refused Roughness limit, marked, linked and
//         focused (#49);
//       - Ads, with its spots, breaks and report stood in for: the lists,
//         the spot form refused (its error linked to the file field), the
//         break form, and the report's two tables; by keyboard Add spot
//         focuses the file field, a refused save keeps focus there and
//         says the error once, Cancel returns to Add spot, Enter on the
//         closed Ad report card's summary opens it without moving focus or
//         saying anything, and Show report keeps focus, says the totals once
//         and shows tables with column and row headers in regions that take
//         focus;
//       - the status message stays while the pointer is over it, goes
//         after the pointer leaves, lets a click on its frame (outside the
//         message and the button) through to the page underneath, and its
//         Dismiss button works by mouse and by keyboard, putting focus
//         back where it was.
//
//   - Settings > Stations (#233): New station refused by a stood-in
//     server, its error on the page, linked to Station ID and focused, is
//     measured like a view in every theme mode and at 320 px and 200 %
//     text; in the System light mode a refused create keeps nothing said
//     but the focused field, and a stood-in create says it once and moves
//     focus to Switch.  /api/stations never receives a create: the stand-in
//     answers every POST, so no audit run adds a station to the server.
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
const VIEWS = ["now", "queue", "carts", "voice", "ads", "track-info", "history", "settings", "library"];
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
// Every view is audited in Light and Dark (each pinned against the opposite
// system theme) in Chromium, and in Dark in Firefox.  The System modes show
// the same page as Light and Dark, so they only check, once per browser,
// that following the system picks the right theme.
const FULL_MODES = {
  chromium: ["light", "dark"],
  firefox: ["dark"],
};

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
// Its song structure (#60): a range, the singing, a drop with a long
// unbroken cue name from DJ software for the reflow checks, a phrase and
// the intro's end (stops for the keys, never listed) and the outro.
const TRACK_LANDMARKS = {
  path: TRACK_PATH, kind: "music", length: 215.4, bpm: 120, analysed: true, bars: true, lyrics: true,
  landmarks: [
    { time_s: 0, kind: "intro", kinds: ["intro"], name: "Intro", source: "analysis", major: true, bar: 1, end_s: 8, end_bar: 5 },
    { time_s: 8, kind: "intro_end", kinds: ["intro_end"], name: "Intro ends", source: "analysis", major: true, bar: 5, end_s: null, end_bar: null },
    { time_s: 12.5, kind: "vocals", kinds: ["vocals"], name: "Vocals", source: "lyrics", major: true, bar: 7, end_s: null, end_bar: null },
    { time_s: 64, kind: "phrase", kinds: ["phrase"], name: "Phrase", source: "analysis", major: false, bar: 33, end_s: null, end_bar: null },
    { time_s: 95.5, kind: "drop", kinds: ["drop"], name: "Drop_An_Unbroken_Cue_Name_From_DJ_Software_0001", source: "imported", major: true, bar: 48, end_s: null, end_bar: null },
    { time_s: 200, kind: "outro", kinds: ["outro"], name: "Outro", source: "analysis", major: true, bar: 100, end_s: 215, end_bar: 108 },
  ],
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
  await page.route("**/api/landmarks?*", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(TRACK_LANDMARKS),
  }));
}

// The song structure is a list of text, the phrase and the intro's end
// left out; Right bracket
// moves the cue point preview to the next landmark (the preview does not
// play) and says it once in the page's polite status, focus staying put.
async function checkTrackLandmarks(page) {
  const list = page.locator("#landmarks-list li");
  await list.first().waitFor({ state: "visible" });
  const items = await list.allTextContents();
  if (items.length !== 4 || !items[2].startsWith("Drop_An_Unbroken")) {
    throw new Error(`Song structure: unexpected items ${JSON.stringify(items)}`);
  }
  const result = await page.evaluate(async () => {
    const preview = document.getElementById("points-preview");
    const heading = document.getElementById("track-info-heading");
    heading.focus();
    preview.currentTime = 10;
    const said = [];
    const region = document.getElementById("sr-status");
    const watch = new globalThis.MutationObserver(() => { if (region.textContent) said.push(region.textContent); });
    watch.observe(region, { childList: true, characterData: true, subtree: true });
    heading.dispatchEvent(new KeyboardEvent("keydown", { key: "]", bubbles: true, cancelable: true }));
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "]" }));
    await new Promise((resolve) => setTimeout(resolve, 200));
    watch.disconnect();
    return { said, time: preview.currentTime, focus: document.activeElement?.id };
  });
  if (result.said.join("|") !== "Preview: Vocals, 0:12, bar 7" || Math.abs(result.time - 12.5) > 0.01
      || result.focus !== "track-info-heading") {
    throw new Error(`Right bracket in Track info: ${JSON.stringify(result)}`);
  }
}

// Open the song's Track info, then show a refused save: two fields marked
// invalid with their errors on the page and focus on the first.
async function openTrackTags(page, visit) {
  await page.evaluate((path) => document.dispatchEvent(new CustomEvent("autodj:edit-points",
    { detail: { path, kind: "music", opener: document.getElementById("tab-track-info") } })), TRACK_PATH);
  await page.locator("#tags-form").waitFor({ state: "visible" });
  await checkTrackLandmarks(page);
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

// Listener requests (#49): the audit server has them off, so the card's
// answers are stood in for.  Long unbroken names test reflow.
const REQUESTS = {
  enabled: true,
  approval: "manual",
  bridges_enabled: true,
  max_bridges: 3,
  bridge_cost_threshold: 2,
  no_smooth_spot: "wait",
  today: 3,
  daily_limit: 50,
  pending: [
    {
      id: "a".repeat(32), title: "Slow Ballad", artist: "Example Singer", name: "Sam",
      requested_at: 1767268800, status: "pending", bridges: [],
      fit: "Fits after Example Artist — Song 3. It is far from the songs around it, so approving it looks for up to three bridge songs to lead into it first.",
      reason: "Fits after Example Artist — Song 3. It is far from the songs around it, so approving it looks for up to three bridge songs to lead into it first.",
      can_approve: true,
    },
    {
      id: "b".repeat(32), title: "An_Exceptionally_Long_Request_Title_Without_Any_Spaces_0001",
      artist: "Another Example", name: "", requested_at: 1767269100, status: "pending", bridges: [],
      fit: "Fits after Example Artist — Song 4: 30 BPM apart. No smooth spot within the next 30 minutes. Approving it uses the best legal spot.",
      reason: "Fits after Example Artist — Song 4: 30 BPM apart. No smooth spot within the next 30 minutes. Approving it uses the best legal spot.",
      can_approve: true,
    },
  ],
  recent: [
    {
      id: "c".repeat(32), title: "Quiet Song", artist: "Example Band", name: "Alex",
      requested_at: 1767265200, status: "queued", decided_at: 1767265260,
      note: "Bridge to Example Band — Quiet Song through Example Artist — Song 2.",
      reason: "Bridge to Example Band — Quiet Song through Example Artist — Song 2.",
      bridges: ["Example Artist — Song 2"],
    },
  ],
};
const requestPages = new WeakSet();

async function standInForRequests(page) {
  if (requestPages.has(page)) return;
  requestPages.add(page);
  await page.route("**/api/requests", (route) => (route.request().method() === "GET"
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(REQUESTS) })
    : route.continue()));
}

// Two queue rows that lead into a request, drawn by the page's own queue
// module.  Only the drawing: the queue the server pushes is unchanged, so
// it does not redraw over them.
async function showBridgedQueue(page) {
  // The page's own module URL, passed in so the import is the page's.
  await page.evaluate(async (moduleUrl) => {
    const { renderQueue } = await import(moduleUrl);
    renderQueue([
      { path: "/audit/bridge-1.mp3", artist: "Example Artist", title: "Song 2", bridge_to: "Example Band — Quiet Song" },
      { path: "/audit/bridge-2.mp3", artist: "Example Artist", title: "An_Exceptionally_Long_Bridge_Title_Without_Spaces_0002", bridge_to: "Example Band — An_Exceptionally_Long_Request_Title_Without_Spaces" },
      { path: "/audit/request.mp3", artist: "Example Band", title: "Quiet Song" },
    ], { queueList: document.getElementById("queue-list"), queueCount: document.getElementById("queue-count") });
  }, "/modules/queue.js");
}

// Smooth transitions: a Roughness limit out of range is refused in the
// browser, marked, linked and focused.  Nothing is saved.
async function openRequestSettingsError(page, visit) {
  const field = page.locator("#requests-threshold");
  await field.waitFor({ state: "visible" });
  await field.fill("12");
  await page.locator("#requests-settings-save").click();
  await page.locator("#requests-threshold-error").waitFor({ state: "visible" });
  await visit("queue/request-settings-error");
  await field.fill("2");
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

const hourFitPages = new WeakSet();

// Keep the unsaved checkbox enabled in status pushes while measuring its fields.
async function openHourFit(page, visit) {
  const toggle = page.locator("#pb-hour-fit");
  await toggle.waitFor({ state: "visible" });
  const was = await toggle.isChecked();
  hourFitPages.add(page);
  try {
    await toggle.evaluate((box) => { box.checked = true; });
    await page.locator("#pb-hour-fit-window").waitFor({ state: "visible" });
    await settle(page);
    await visit("settings/picking/hour-fit");
  } finally {
    hourFitPages.delete(page);
    await toggle.evaluate((box, checked) => { box.checked = checked; }, was);
  }
}

// Schedule: a hard event's Wait up to seconds refused in the browser, the
// field marked invalid and focused, the error shown and linked.  Nothing
// is saved; Cancel and Discard close the modal.
async function inspectEditorModal(page, formId, visit, target) {
  const dialog = page.locator(`#${formId}-dialog`);
  await dialog.waitFor({ state: "visible" });
  const name = await dialog.evaluate((element) => {
    const heading = document.getElementById(element.getAttribute("aria-labelledby"));
    return { open: element.open, modal: element.matches(":modal"), labelled: Boolean(heading && element.contains(heading) && heading.textContent.trim()) };
  });
  if (!name.open || !name.modal || !name.labelled) throw new Error(`${formId} is not an accessible native modal`);
  const viewport = page.viewportSize() || { width: 1280, height: 800 };
  await page.setViewportSize({ width: 320, height: 640 });
  await settle(page);
  const bounds = await dialog.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { left: rect.left, right: rect.right, width: rect.width, viewport: document.documentElement.clientWidth };
  });
  await page.setViewportSize(viewport);
  if (bounds.width > bounds.viewport || bounds.left < 0 || bounds.right > bounds.viewport) {
    throw new Error(`${formId} exceeds the 320 px viewport: ${JSON.stringify(bounds)}`);
  }
  await visit(target);
}

async function cancelEditor(page, formId, cancelId) {
  const dialog = page.locator(`#${formId}-dialog`);
  await page.locator(`#${cancelId}`).click();
  const confirm = page.locator("#confirm-dialog");
  if (await confirm.isVisible()) {
    await page.locator("#confirm-accept").click();
    await confirm.waitFor({ state: "hidden" });
  }
  await dialog.waitFor({ state: "hidden" });
}

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
  await inspectEditorModal(page, "schedule-form", visit, "settings/schedule/wait-error");
  await cancelEditor(page, "schedule-form", "schedule-cancel");
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

// Library tools: a category's season, a clockwheel step's hours and an ad
// break step's length, each refused with its error on the page, linked to
// the field and focused.
async function openDaypartErrors(page, visit) {
  await page.locator("#categories-add").click();
  await page.locator("#categories-name").fill("Christmas");
  await page.locator("#categories-query").fill("genre:Christmas");
  await page.locator("#categories-not-before").fill("2026-12-26");
  await page.locator("#categories-not-after").fill("2026-12-01");
  await page.locator("#categories-save").click();
  await page.locator("#categories-form-error").waitFor({ state: "visible" });
  await inspectEditorModal(page, "categories-form", visit, "library/category-dates-error");
  await cancelEditor(page, "categories-form", "categories-cancel");
  await page.locator("#clock-name").fill("Audit");
  await page.locator("#clock-create").click();
  await page.locator("#clock-new-step").click();
  const condition = page.locator("#clock-step-conditions").getByRole("group").first();
  await condition.getByLabel("Condition type", { exact: true }).selectOption("title");
  await condition.getByLabel("Value", { exact: true }).fill("Audit");
  await page.locator("#clock-step-hours-from").fill("22:00");
  await page.locator("#clock-add-step").click();
  await page.locator("#clock-step-hours-error").waitFor({ state: "visible" });
  await visit("library/step-hours-error");
  // An ad break step (#51) with its fields, the server's reason the station
  // airs no ads, and a refused length linked to its field.
  await page.locator("#clock-step-hours-from").fill("");
  await page.locator("#clock-step-type").selectOption("ad_break");
  await page.locator("#clock-step-ad-seconds").fill("5");
  await page.locator("#clock-add-step").click();
  await page.locator('#clock-step-ad-seconds[aria-invalid="true"]').waitFor();
  await page.locator("#clock-step-ad-error").waitFor({ state: "visible" });
  await visit("library/ad-step-error");
  await page.locator("#clock-step-ad-seconds").fill("0");
  await page.locator("#clock-step-type").selectOption("music");
  await cancelEditor(page, "clock-step", "clock-cancel-edit");
}

// Library tools: an automatic rescan under 5 minutes refused in the
// browser, its message under Minutes, linked to it and focused (#249).
// Nothing is saved.
async function openAutoRescanError(page, visit) {
  const minutes = page.locator("#auto-rescan-minutes");
  await minutes.waitFor({ state: "visible" });
  const before = await minutes.inputValue();
  await minutes.fill("3");
  await page.locator("#auto-rescan-save").click();
  await page.locator('#auto-rescan-minutes[aria-invalid="true"]').waitFor();
  await page.locator("#auto-rescan-error").waitFor({ state: "visible" });
  await visit("library/auto-rescan-error");
  await minutes.fill(before);
}

// Library tools, Intro rules (#127): two saved rules and the station's
// liners and folders are stood in for (the audit server has none), so the
// list and the form with its liner checkboxes are measured.  One liner name
// has no spaces, for the reflow checks.  Only reading is stood in for.
const INTRO_RULES = {
  rules: [
    { id: "d".repeat(32), label: "Eighties", match: "all", priority: "after_artist",
      conditions: [{ field: "year", operator: "between", value: 1980, upper: 1989 }],
      clip_ids: ["l1"], chance: 50, cooldown_minutes: 90, created_at: 1, last_aired: null },
    { id: "e".repeat(32), label: "", match: "any", priority: "before_song",
      conditions: [{ field: "genre", operator: "is", value: "Rock" }, { field: "artist", operator: "contains", value: "Example" }],
      clip_ids: [], liner_folder: "D:\\Radio\\Liners\\An_Unbroken_Folder_Name_Without_Any_Spaces_0001",
      chance: 100, cooldown_minutes: 0, not_before: "2026-12-01", not_after: "2026-12-26", created_at: 2, last_aired: null },
  ],
  problem: "1 invalid intro rules retained in intro_rules.json",
  max_rules: 2000,
  max_cooldown_minutes: 10080,
  liners: [
    { id: "l1", label: "eighties.wav" },
    { id: "l2", label: "An_Unbroken_Liner_Name_Without_Any_Spaces_From_A_Tagless_File_0001.wav" },
  ],
  liner_folders: ["D:\\Radio\\Liners"],
  sources: [{ id: "music", path: "D:\\Radio\\Music" }],
  genres: ["Pop", "Rock"],
  beets_fields: [],
  beets_error: null,
  categories: [],
};
const introRulePages = new WeakSet();

async function standInForIntroRules(page) {
  if (introRulePages.has(page)) return;
  introRulePages.add(page);
  await page.route(/\/api\/(stations\/[^/]+\/)?intros\/rules$/, (route) => (route.request().method() === "GET"
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(INTRO_RULES) })
    : route.continue()));
}

// Add intro rule, saved with no liner chosen: the error shown under the
// liners, linked to the first liner's checkbox and focused.  Nothing is
// sent; Cancel closes the form again.
async function openIntroRuleError(page, visit) {
  await page.locator("#intro-rules-list li").first().waitFor({ state: "visible" });
  await page.locator("#intro-rules-add").click();
  await page.locator("#intro-rules-form").waitFor({ state: "visible" });
  await page.locator("#intro-rules-label").fill("Audit draft");
  await page.locator("#intro-rules-save").click();
  await page.locator("#intro-rules-clips-error").waitFor({ state: "visible" });
  await inspectEditorModal(page, "intro-rules-form", visit, "library/intro-rule-error");
  await cancelEditor(page, "intro-rules-form", "intro-rules-cancel");
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
  limits: { max_bytes: 67108864, max_seconds: 600, sample_rate: 44100, dj_placement: "auto", min_intro_s: 2, max_per_slot: 2 },
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

// Live DJ (#48): GET /api/live says the station can go live, with a DJ
// on the air and two DJ accounts (one with a long unbroken username, for
// reflow); Add DJ is refused by the server.  The station's socket pushes
// are passed through with "live" set to what the stand-in says, and the
// talk socket is answered here, counting what the page sends.  The capture
// worklet and the microphone source are stubbed (the microphone itself is
// the Voice tracks stand-in's).
const LIVE_DJ = { dj: "DJ Alice", since: "2026-10-13T20:15:00Z" };
const LIVE_INFO = (live) => ({
  available: true, reason: null, live, talkover: false, source_path: "/live", plain_http_lan: false,
  settings: { mic_return_seconds: 0.5, mic_duck_db: 12 },
  djs: [
    { username: "dj_an_exceptionally_long_username_without_any_spaces_0001", display_name: "DJ Alice", password_set: true },
    { username: "bob", display_name: "Bob", password_set: false },
  ],
});
const livePages = new WeakMap();

async function standInForLiveDj(page) {
  if (livePages.has(page)) return livePages.get(page);
  const state = { live: LIVE_DJ, micSockets: 0, micMessages: 0 };
  livePages.set(page, state);
  await page.addInitScript(() => {
    const RealNode = window.AudioWorkletNode;
    window.AudioWorkletNode = function AudioWorkletNode(context, name, options) {
      if (name !== "autodj-talk-capture") return new RealNode(context, name, options);
      const node = { port: { onmessage: null, close() {} } };
      const timer = setInterval(() => node.port.onmessage?.({ data: new ArrayBuffer(4096) }), 50);
      node.disconnect = () => clearInterval(timer);
      return node;
    };
    const worklet = window.AudioWorklet?.prototype;
    if (worklet) {
      const addModule = worklet.addModule;
      worklet.addModule = function stubbedAddModule(url, options) {
        return String(url).startsWith("blob:") ? Promise.resolve() : addModule.call(this, url, options);
      };
    }
    const context = (window.AudioContext || window.webkitAudioContext)?.prototype;
    if (context) {
      const createSource = context.createMediaStreamSource;
      context.createMediaStreamSource = function stubbedSource(stream) {
        if (window.MediaStream && stream instanceof window.MediaStream) return createSource.call(this, stream);
        return { connect() {}, disconnect() {} };
      };
    }
  });
  await page.route("**/api/live", (route) => (route.request().method() === "GET"
    ? route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LIVE_INFO(state.live)) })
    : route.continue()));
  await page.route("**/api/live/djs", (route) => (route.request().method() === "POST"
    ? route.fulfill({ status: 400, contentType: "application/json",
      body: JSON.stringify({ detail: "Username must be 1 to 64 printable ASCII characters" }) })
    : route.continue()));
  await page.routeWebSocket(/\/live\/mic(\?|$)/, (socket) => {
    state.micSockets += 1;
    socket.onMessage(() => { state.micMessages += 1; });
  });
  await page.routeWebSocket(/\/(ws|api\/stations\/[^/]+\/ws)$/, (socket) => {
    const server = socket.connectToServer();
    server.onMessage((message) => {
      let data = message;
      try {
        const parsed = JSON.parse(message);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && !parsed.type) {
          if (hourFitPages.has(page) && parsed.settings?.playback) {
            parsed.settings.playback.hour_fit = true;
          }
          data = JSON.stringify({ ...parsed, live: state.live, talkover: false });
        }
      } catch { /* not JSON: passed on as it is */ }
      socket.send(data);
    });
    socket.onMessage((message) => server.send(message));
  });
  return state;
}

// WebKit's Windows build has no Web Audio at all.  While Talk is tried
// there, a stand-in audio context exists, and it is taken away after, so
// nothing else on the page ever sees it.
function fakeTalkAudio(page, on) {
  return page.evaluate((install) => {
    if (install && !window.AudioContext && !window.webkitAudioContext) {
      window.__fakeTalkAudio = true;
      window.AudioContext = class FakeTalkContext {
        constructor() {
          this.sampleRate = 48000;
          this.state = "running";
          this.destination = {};
          this.audioWorklet = { addModule: () => Promise.resolve() };
        }

        resume() { return Promise.resolve(); }

        close() { return Promise.resolve(); }

        createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
      };
    } else if (!install && window.__fakeTalkAudio) {
      delete window.AudioContext;
      delete window.__fakeTalkAudio;
    }
  }, on);
}

// Talk latched on by a quick press, as a state of Now Playing.
async function openTalkState(page, visit) {
  await fakeTalkAudio(page, true);
  try {
    await talkState(page, visit);
  } finally {
    await fakeTalkAudio(page, false);
  }
}

async function talkState(page, visit) {
  const talk = page.locator("#btn-talk");
  await talk.waitFor({ state: "visible", timeout: 5000 });
  await talk.focus();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.getElementById("btn-talk").getAttribute("aria-pressed") === "true",
    null, { timeout: 5000 }).catch(async () => {
    const why = await page.evaluate(() => document.getElementById("talk-error").textContent);
    throw new Error(`Talk did not latch on: ${why || "no error shown"}`);
  });
  await settle(page);
  await visit("now/talking");
  // Tabbing past the last control leaves the page, which stops talk; if it
  // is still on, pressing Talk again stops it.
  if (await talk.getAttribute("aria-pressed") === "true") {
    await talk.focus();
    await page.keyboard.press("Enter");
  }
  await page.waitForFunction(() => document.getElementById("btn-talk").getAttribute("aria-pressed") === "false",
    null, { timeout: 5000 });
}

// Settings > Live DJ with Add DJ refused: the error in words, linked.
async function openLiveDjError(page, visit) {
  await page.locator("#live-dj-add").click();
  await page.locator("#live-dj-username").fill("dj");
  await page.locator("#live-dj-name").fill("DJ Carol");
  await page.locator("#live-dj-password").fill("correct horse battery");
  await page.locator("#live-dj-save").click();
  await page.locator("#live-dj-error").waitFor({ state: "visible", timeout: 5000 });
  await settle(page);
  await visit("settings/live/dj-error");
  await page.locator("#live-dj-cancel").click();
}

// Ads (#51): the spots, breaks and report are stood in for (the audit
// server airs no ads), so the lists, the forms with a refused field and
// the report's two tables are measured with long words in them.
const ADS_VIEW = {
  station: "default", enabled: true, reason: null, problem: null, timezone: "UTC",
  limits: { max_bytes: 20971520, max_seconds: 120, max_spots: 200, max_breaks: 50, max_spots_per_break: 10 },
  settings: { separation_minutes: 15, late_minutes: 10, miss_after_minutes: 30 },
  spots: [
    { id: "a".repeat(32), title: "An_Exceptionally_Long_Spot_Title_Without_Any_Spaces_0001", advertiser: "Example Motors",
      campaign: "Autumn", category: "Car dealers", start_date: "2026-10-01", end_date: "2026-10-31",
      dates_text: "Flight from 1 October 2026 until 31 October 2026", in_flight: true, flight_text: "",
      max_per_day: 4, max_per_hour: 1, enabled: true, seconds: 30, plays_today: 2, booked: false, missing: false },
    { id: "b".repeat(32), title: "Fresh bread", advertiser: "Sample Bakery", campaign: "", category: "",
      start_date: null, end_date: null, dates_text: "", in_flight: true, flight_text: "",
      max_per_day: 0, max_per_hour: 0, enabled: false, seconds: 15, plays_today: 0, booked: false, missing: true },
  ],
  breaks: [
    { id: "c".repeat(32), name: "Quarter past", minutes: [15, 45], days: [0, 1, 2, 3, 4], from_hour: 6, until_hour: 20,
      start_date: null, end_date: null, max_spots: 3, max_seconds: 90, enabled: true,
      when_text: "At :15, :45 past the hour, from 06:00 until 20:00, on Monday, Tuesday, Wednesday, Thursday and Friday",
      next_at: "2026-10-13T12:15:00+00:00", next_text: "Tuesday 13 October 2026 at 12:15" },
  ],
  pending: [{ break_name: "Quarter past", scheduled_text: "Tuesday 13 October 2026 at 11:45", source_text: "Minutes past the hour" }],
  recent: [
    { id: "d".repeat(32), scheduled_text: "13 October 2026 at 11:15:00", aired_text: "13 October 2026 at 11:19:30",
      delay_minutes: 4.5, status: "aired", status_text: "Aired", break_name: "Quarter past", source_text: "Minutes past the hour",
      spot_id: "a".repeat(32), title: "Fall sale", advertiser: "Example Motors", campaign: "Autumn", seconds: 30, reason: "" },
  ],
};
const ADS_REPORT = {
  station: "default", from: "2026-10-01", to: "2026-10-13", count: 2, truncated: false, damaged: 0, late_minutes: 10,
  totals: { aired: 1, late: 1, missed: 1, waiting: 0, missed_breaks: 0 },
  summary: [{ advertiser: "Example Motors", campaign: "An_Exceptionally_Long_Campaign_Name_0001", aired: 1, late: 1, missed: 1, interrupted: 1, waiting: 0, seconds: 30 }],
  rows: [
    { id: "e".repeat(32), scheduled_text: "13 October 2026 at 12:15:00", aired_text: "13 October 2026 at 12:31:00",
      delay_minutes: 16, status: "late", status_text: "Aired late", break_name: "Quarter past", source_text: "Minutes past the hour",
      spot_id: "a".repeat(32), title: "Fall sale", advertiser: "Example Motors", campaign: "Autumn", seconds: 30, reason: "" },
    { id: "f".repeat(32), scheduled_text: "13 October 2026 at 12:45:00", aired_text: "", delay_minutes: null,
      status: "missed", status_text: "Missed", break_name: "Quarter past", source_text: "Run now",
      spot_id: "a".repeat(32), title: "Fall sale", advertiser: "Example Motors", campaign: "Autumn", seconds: 30,
      reason: "skipped before it aired" },
  ],
};
const adsPages = new WeakSet();

async function standInForAds(page) {
  if (adsPages.has(page)) return;
  adsPages.add(page);
  const json = (body) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/ads", (route) => (route.request().method() === "GET"
    ? route.fulfill(json(ADS_VIEW)) : route.continue()));
  await page.route("**/api/ads/report?**", (route) => route.fulfill(json(ADS_REPORT)));
}

// Ads with the spot form refused (its error linked to the file field), the
// break form open, and the report shown as its two tables.
async function openAdsStates(page, visit) {
  await page.locator("#ads-spots li button").first().waitFor({ state: "visible" });
  await page.locator("#ads-spot-add").click();
  await page.locator("#ads-spot-save").click();
  await page.locator("#ads-spot-error").waitFor({ state: "visible" });
  await page.locator("#ads-spot-title").fill("Audit spot");
  await page.locator("#ads-spot-advertiser").fill("Audit advertiser");
  await inspectEditorModal(page, "ads-spot-form", visit, "ads/spot-error");
  await cancelEditor(page, "ads-spot-form", "ads-spot-cancel");
  await page.locator("#ads-break-add").click();
  await page.locator("#ads-break-name").fill("Audit break");
  await inspectEditorModal(page, "ads-break-form", visit, "ads/break-form");
  await cancelEditor(page, "ads-break-form", "ads-break-cancel");
  await page.locator("#ads-report-show").click();
  await page.locator("#ads-rows-wrap").waitFor({ state: "visible" });
  await visit("ads/report");
}

// The Ads tab by keyboard: Add spot puts focus on the file field; saving
// with no file names it in words, links the words to it, says them once
// and keeps focus there; Cancel goes back to Add spot; Show report keeps
// focus on its button, says the totals once and shows two tables with
// headers, each in a region that takes focus.
async function adsProblems(page) {
  await standInForAds(page);
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  await page.locator("#tab-ads").click();
  await page.locator("#ads-spots li button").first().waitFor({ state: "visible" });
  const problems = [];
  await page.evaluate(() => {
    window.__adsSaid = [];
    for (const id of ["ads-status", "ads-spot-form-status", "ads-break-form-status"]) {
      const region = document.getElementById(id);
      new window.MutationObserver(() => {
        if (region.textContent) window.__adsSaid.push(region.textContent);
      }).observe(region, { childList: true, characterData: true, subtree: true });
    }
  });
  const focusId = () => page.evaluate(() => document.activeElement?.id || document.activeElement?.tagName.toLowerCase());
  await page.locator("#ads-spot-add").focus();
  await page.keyboard.press("Enter");
  if (await focusId() !== "ads-spot-file") problems.push(`Add spot moved focus to ${await focusId()}`);
  await page.locator("#ads-spot-save").focus();
  await page.keyboard.press("Enter");
  await page.locator("#ads-spot-error").waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(300);
  const refused = await page.evaluate(() => ({
    error: document.getElementById("ads-spot-error").textContent.trim(),
    invalid: document.getElementById("ads-spot-file").getAttribute("aria-invalid"),
    linked: (document.getElementById("ads-spot-file").getAttribute("aria-describedby") || "").split(" "),
    focus: document.activeElement?.id,
    said: [...window.__adsSaid],
  }));
  if (!refused.error) problems.push("saving with no file shows no error");
  if (refused.invalid !== "true") problems.push("the file field is not marked invalid");
  if (!refused.linked.includes("ads-spot-error")) problems.push("the error is not linked to the file field");
  if (refused.focus !== "ads-spot-file") problems.push(`a refused save moved focus to ${refused.focus}`);
  if (refused.said.length) problems.push(`a refused save duplicated its focused field error: ${JSON.stringify(refused.said)}`);
  await page.locator("#ads-spot-cancel").focus();
  await page.keyboard.press("Enter");
  if (await focusId() !== "ads-spot-add") problems.push(`Cancel moved focus to ${await focusId()}`);
  // Earlier layout checks persist open cards; start this keyboard check closed.
  await page.locator("#ad-report-card").evaluate((card) => { card.open = false; });
  // Enter opens it, keeps focus there and says nothing (the browser says expanded).
  await page.evaluate(() => { window.__adsSaid = []; });
  await page.locator("summary:has(> #ads-report-heading)").focus();
  await page.keyboard.press("Enter");
  const opened = await page.evaluate(() => {
    const summary = document.getElementById("ads-report-heading").parentElement;
    return {
      open: summary.parentElement.open,
      focused: document.activeElement === summary,
      said: [...window.__adsSaid],
    };
  });
  if (!opened.open) problems.push("Enter on the Ad report summary did not open the card");
  if (!opened.focused) problems.push("opening the Ad report card moved focus off its summary");
  if (opened.said.length) problems.push(`opening the Ad report card said ${JSON.stringify(opened.said)}`);
  await page.evaluate(() => { window.__adsSaid = []; });
  await page.locator("#ads-report-show").focus();
  await page.keyboard.press("Enter");
  await page.locator("#ads-rows-wrap").waitFor({ state: "visible", timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(300);
  const report = await page.evaluate(() => ({
    focus: document.activeElement?.id,
    said: [...window.__adsSaid],
    headers: [...document.querySelectorAll("#ads-rows-table thead th[scope='col']")].length,
    rowHeaders: [...document.querySelectorAll("#ads-rows tr > th[scope='row']")].length,
  }));
  if (report.focus !== "ads-report-show") problems.push(`Show report moved focus to ${report.focus}`);
  if (report.said.length !== 1 || !/^Ad report from/.test(report.said[0])) problems.push(`Show report said ${JSON.stringify(report.said)}`);
  if (report.headers !== 6 || report.rowHeaders !== 2) problems.push("the report table lacks its headers");
  // Past the export controls, the totals table's region takes focus.
  await page.locator("#ads-report-export").focus();
  await page.keyboard.press("Tab");
  const region = await focusId();
  if (region !== "ads-totals-wrap") problems.push(`Tab after Export reached ${region}, not the totals table`);
  return problems;
}

// A focused liner-name field must survive refresh, and moving to Save must
// deliver the click before any deferred result rendering. All writes are stood in.
async function speechStudioProblems(page) {
  let saved = false;
  const writes = [];
  const data = () => ({
    batches: { "audit-comparison": { text: "Audit station ID", candidates: [
      { state: "succeeded", duration: 0.1, connection: "audit", plan: { settings: { voice: "good" }, controls: {} } },
    ] } },
    saved: saved ? { "audit-liner": { label: "Morning station ID", published: false } } : {},
    presets: {}, sets: {}, default_preset: null, active_set: null, busy: false,
  });
  const stateRoute = (route) => route.fulfill(jsonReply(data()));
  const otherRoute = async (route) => {
    const request = route.request();
    if (request.url().endsWith("/providers") && request.method() === "GET") {
      return route.fulfill(jsonReply({ connections: [], engines: [] }));
    }
    if (request.method() !== "GET") {
      writes.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() });
      saved = request.url().endsWith("/audit-comparison/save");
      return route.fulfill(jsonReply(saved ? { id: "audit-liner" } : { detail: "Unexpected speech write" }, saved ? 200 : 400));
    }
    return route.fulfill({ status: 204, body: "" });
  };
  await page.route("**/api/speech", stateRoute);
  await page.route("**/api/speech/**", otherRoute);
  try {
    await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
    await page.locator("#tab-settings").click();
    await page.locator("#stab-liners").click();
    await page.locator("#speech-studio").evaluate((element) => { element.open = true; });
    const name = page.locator("#speech-results").getByLabel("Liner name");
    await name.fill("Morning station ID");
    await page.locator("#speech-text").focus();
    await page.waitForTimeout(50);
    if (await name.inputValue() !== "Morning station ID") return ["draft liner name lost on focus change"];
    await name.focus();
    await page.locator("#speech-results").getByRole("button", { name: "Save liner", exact: true }).click({ delay: 100 });
    await page.locator("#speech-saved li").waitFor({ state: "visible", timeout: 3000 }).catch(() => {});
    if (writes.length !== 1 || !writes[0].path.endsWith("/audit-comparison/save")
      || writes[0].body.label !== "Morning station ID") return [`Save sent ${JSON.stringify(writes)}`];
    if (!await page.locator("#speech-saved").textContent().then((text) => text.includes("not assigned"))) return ["saving did not show an unassigned liner"];
    return [];
  } finally {
    await page.unroute("**/api/speech", stateRoute);
    await page.unroute("**/api/speech/**", otherRoute);
  }
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
    // A create is refused here, never sent: the audit must not add a station.
    if (route.request().method() !== "GET") return route.fulfill(jsonReply(CREATE_REFUSAL, 409));
    const response = await route.fetch();
    const body = await response.json().catch(() => null);
    seen.real = Array.isArray(body?.stations) ? body.stations : [];
    if (seen.real.length >= 2) return route.fulfill({ response });
    return route.fulfill({ response, json: { stations: [...seen.real, STAND_IN_STATION] } });
  });
}

function jsonReply(body, status) {
  return { status, contentType: "application/json", body: JSON.stringify(body) };
}

// The refusal a stood-in server gives New station, naming its field.
const CREATE_REFUSAL = {
  detail: "A folder from an earlier station called audit-refused is still in the stations folder. Choose another ID, or move that folder away.",
  field: "slug",
};

// Settings > Stations: New station, a copy, refused on its Station ID.
async function openStationCreateError(page, visit) {
  await page.locator("#station-create-source").selectOption("default");
  await page.locator("#station-create-copies").waitFor({ state: "visible" });
  await page.locator("#station-create-name").fill("Audit Refused Station With A Long Name");
  await page.locator("#station-create-slug").fill("audit-refused");
  await page.locator("#station-create-submit").click();
  await page.locator("#station-create-error").waitFor({ state: "visible" });
  await visit("settings/stations/refused");
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
// in stream mode).  `measure` runs once per target with that target shown,
// after the running transitions have ended: the states opened by a click
// (a refused form, a dialog) are measured too.  After the click on
// #clock-add-step that shows its hours error, WebKit was still running the
// button's change from pressed (black on light blue in Dark) to hover
// (white on blue) most times the error was found, and axe-core now and then
// failed its text contrast on a colour read part way through.
async function eachTarget(page, measure) {
  const visit = async (target) => {
    await settle(page);
    await measure(target);
  };
  await standInForRotationTest(page);
  await standInForTrackInfo(page);
  await standInForTimeChecks(page);
  await standInForHourPreview(page);
  await standInForListenerStats(page);
  await standInForVoiceTracker(page);
  await standInForAds(page);
  await standInForShows(page);
  await standInForStations(page);
  await standInForIntroRules(page);
  await standInForLiveDj(page);
  await standInForRequests(page);
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
      if (view === "queue") {
        await page.locator("#requests-card").waitFor({ state: "visible", timeout: 10000 });
        await showBridgedQueue(page);
      }
      await visit(view);
      if (view === "queue") await openRequestSettingsError(page, visit);
      if (view === "track-info") await openTrackTags(page, visit);
      if (view === "library") {
        await openAutoRescanError(page, visit);
        await openIntroRuleError(page, visit);
        await openDaypartErrors(page, visit);
        await openShowStates(page, visit);
      }
      if (view === "voice") await openVoiceStates(page, visit);
      if (view === "ads") await openAdsStates(page, visit);
      if (view === "now") {
        await openStationMenu(page, visit);
        await openTalkState(page, visit);
      }
      continue;
    }
    const ids = await page.$$eval("#settings-tabs [role='tab']:not([hidden])",
      (tabs) => tabs.map((tab) => tab.id));
    for (const id of ids) {
      await page.locator(`#${id}`).click();
      await page.waitForTimeout(150);
      await openEveryCard(page);
      await visit(`settings/${id.replace(/^stab-/, "")}`);
      if (id === "stab-picking") {
        await openRotationTest(page, visit);
        await openRepeatHoursError(page, visit);
        await openHourFit(page, visit);
      }
      if (id === "stab-schedule") await openScheduleWaitError(page, visit);
      if (id === "stab-time-checks") await openPackDatesError(page, visit);
      if (id === "stab-stations") await openStationCreateError(page, visit);
      if (id === "stab-live") await openLiveDjError(page, visit);
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
    // WebKit scrolls a newly focused control into view at its next
    // rendering update, not during the key press: read at once,
    // #clock-step-type was still a screen below the window.  Wait for that
    // scroll (or for focus to leave the page); a control never scrolled in
    // still fails below.
    await page.waitForFunction(() => {
      const element = document.activeElement;
      if (!element || element === document.body) return true;
      const rect = element.getBoundingClientRect();
      return rect.top >= -1 && rect.bottom <= window.innerHeight + 1
        && rect.left >= -1 && rect.right <= window.innerWidth + 1;
    }, null, { timeout: 2000 }).catch(() => {});
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
    const problems = await themeStateProblems(page, mode);
    if (problems.length) out.themeState[mode.name] = problems;
  } finally {
    await context.close();
  }
}

// The theme a page opened in *mode* shows, before and after the modules ran.
async function themeStateProblems(page, mode) {
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
  return problems;
}

// A System mode: the page follows the system theme.  One page load.
async function checkSystemTheme(browser, mode, out) {
  const context = await themeContext(browser, mode);
  const page = await newPage(context);
  page.on("pageerror", (error) => out.pageerrors.push(`${mode.name}: ${error.message}`));
  try {
    await page.goto(`${BASE}/`, { waitUntil: "networkidle", timeout: 20000 });
    await settle(page);
    const problems = await themeStateProblems(page, mode);
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
    toastText: document.getElementById("status-toast")?.textContent.trim(),
  }));
  if (after.theme !== "dark" || after.background !== THEME_BG.dark) {
    problems.push(`after choosing Dark: ${JSON.stringify(after)}`);
  }
  if (after.stored !== "dark") problems.push(`Dark not stored: ${after.stored}`);
  if (after.toast === false) problems.push(`choosing a theme showed a status message: ${after.toastText}`);
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
// New station by keyboard: a refused create marks, links and focuses
// Station ID and says nothing else; a stood-in create says it once and
// moves focus to Switch to <name>.  Nothing reaches the server.
async function stationCreateProblems(page) {
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  await page.locator("#tab-settings").click();
  await page.locator("#stab-stations").click();
  await page.locator("#station-create-name").waitFor({ state: "visible" });
  const problems = [];
  const state = () => page.evaluate(() => {
    const slug = document.getElementById("station-create-slug");
    return {
      invalid: slug.getAttribute("aria-invalid"),
      linked: (slug.getAttribute("aria-describedby") || "").split(/\s+/),
      error: document.getElementById("station-create-error").textContent.trim(),
      focus: document.activeElement?.id,
      said: document.getElementById("stations-status").textContent.trim(),
      done: !document.getElementById("station-create-done").hidden,
    };
  });
  await page.locator("#station-create-name").fill("Audit Refused Station");
  await page.locator("#station-create-slug").fill("audit-refused");
  await page.locator("#station-create-slug").press("Enter");
  await page.locator("#station-create-error").waitFor({ state: "visible" });
  const refused = await state();
  if (!refused.error) problems.push("a refused create shows no error");
  if (refused.invalid !== "true") problems.push("Station ID is not marked invalid");
  if (!refused.linked.includes("station-create-error")) problems.push("the error is not linked to Station ID");
  if (!refused.linked.includes("station-create-slug-help")) problems.push("the ID's format hint lost its link");
  if (refused.focus !== "station-create-slug") problems.push(`a refused create moved focus to ${refused.focus}`);
  if (refused.said) problems.push(`a refused create also said ${JSON.stringify(refused.said)}`);

  const created = {
    ...STAND_IN_STATION, slug: "audit-created", name: "Audit Created Station",
    status_path: "/stations/audit-created/status", copied: ["saved settings"], not_copied: [],
  };
  const createdHandler = async (route) => {
    if (route.request().method() === "POST") return route.fulfill(jsonReply(created, 201));
    const response = await route.fetch();
    const body = await response.json().catch(() => null);
    const listed = Array.isArray(body?.stations) ? body.stations : [];
    return route.fulfill({ response, json: { stations: [...listed, created] } });
  };
  await page.route("**/api/stations", createdHandler);
  try {
    await page.locator("#station-create-name").fill("Audit Created Station");
    await page.locator("#station-create-slug").fill("audit-created");
    await page.locator("#station-create-submit").focus();
    await page.keyboard.press("Enter");
    await page.locator("#station-create-done").waitFor({ state: "visible" });
    await page.waitForFunction(() => document.getElementById("stations-status").textContent.trim());
    const done = await state();
    if (done.focus !== "station-create-switch") problems.push(`a create moved focus to ${done.focus}`);
    if (done.said !== "Created Audit Created Station, off air. Copied saved settings.") {
      problems.push(`a create said ${JSON.stringify(done.said)}`);
    }
    if (done.invalid) problems.push("Station ID is still marked invalid after a create");
  } finally {
    await page.unroute("**/api/stations", createdHandler);
  }
  return problems;
}

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

// Live DJ (#48): Talk held with Space and with Alt+M, and the live line
// said once as a DJ goes live and once as they leave.
async function liveDjProblems(page) {
  await standInForVoiceTracker(page);
  const live = await standInForLiveDj(page);
  live.live = LIVE_DJ;
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  const problems = [];
  const talk = page.locator("#btn-talk");
  try {
    await talk.waitFor({ state: "visible", timeout: 5000 });
  } catch {
    return ["Talk is not shown while the station can go live"];
  }
  await fakeTalkAudio(page, true);
  await page.evaluate(() => {
    window.__liveSaid = [];
    const region = document.getElementById("sr-status");
    new window.MutationObserver(() => {
      if (region.textContent) window.__liveSaid.push(region.textContent);
    }).observe(region, { childList: true, characterData: true, subtree: true });
  });
  const state = () => page.evaluate(() => ({
    pressed: document.getElementById("btn-talk").getAttribute("aria-pressed"),
    focus: document.activeElement?.id || document.activeElement?.tagName.toLowerCase(),
    said: [...window.__liveSaid],
    stopped: window.__micStopped || 0,
    line: document.getElementById("live-on-air").textContent,
  }));
  const pressedIs = (value) => page.waitForFunction(
    (wanted) => document.getElementById("btn-talk").getAttribute("aria-pressed") === wanted,
    value, { timeout: 5000 },
  ).then(() => true, () => false);

  // Hold Space on Talk.
  const before = await state();
  await talk.focus();
  await page.keyboard.down("Space");
  if (!await pressedIs("true")) problems.push("holding Space on Talk did not talk");
  await page.waitForTimeout(800);
  let now = await state();
  if (now.focus !== "btn-talk") problems.push(`holding Space moved focus to ${now.focus}`);
  if (!live.micMessages) problems.push("talking sent no microphone audio");
  await page.keyboard.up("Space");
  if (!await pressedIs("false")) problems.push("letting go of Space did not stop talking");
  now = await state();
  if (now.stopped <= before.stopped) problems.push("letting go of Talk kept the microphone");
  if (now.focus !== "btn-talk") problems.push(`letting go of Space moved focus to ${now.focus}`);
  if (now.said.length) problems.push(`holding Talk said ${JSON.stringify(now.said)}`);

  // Hold Alt+M from another control.
  await page.locator("#btn-skip").focus();
  await page.keyboard.down("Alt");
  await page.keyboard.down("KeyM");
  if (!await pressedIs("true")) problems.push("holding Alt+M did not talk");
  // Held well past the latch time, so letting go ends it.
  await page.waitForTimeout(800);
  await page.keyboard.up("KeyM");
  await page.keyboard.up("Alt");
  if (!await pressedIs("false")) problems.push("letting go of Alt+M did not stop talking");
  await page.waitForTimeout(300);
  now = await state();
  if (now.focus !== "btn-skip") problems.push(`Alt+M moved focus to ${now.focus}`);
  const talkSaid = now.said.filter((text) => /talking/i.test(text));
  if (JSON.stringify(talkSaid) !== JSON.stringify(["Talking.", "Stopped talking."])) {
    problems.push(`Alt+M said ${JSON.stringify(now.said)}`);
  }

  // A quick Alt+M never leaves the microphone on.
  await page.keyboard.press("Alt+KeyM");
  await page.waitForTimeout(1000);
  if (await talk.getAttribute("aria-pressed") !== "false") {
    problems.push("a quick Alt+M left talk on");
    await page.keyboard.press("Alt+KeyM");
  }

  // A quick press of Talk latches it, said once and shown in words.
  await talk.focus();
  const latchFrom = (await state()).said.length;
  await page.keyboard.press("Enter");
  if (!await pressedIs("true")) problems.push("a quick press of Talk did not keep it on");
  await page.waitForTimeout(300);
  const latched = await page.evaluate(() => {
    const note = document.getElementById("talk-latched");
    return { shown: !note.hidden && note.getBoundingClientRect().height > 0, text: note.textContent };
  });
  if (!latched.shown || latched.text !== "Talking until you press Talk again, for 2 minutes at most.") {
    problems.push(`latched talk shows ${JSON.stringify(latched)}`);
  }
  now = await state();
  const latchSaid = now.said.slice(latchFrom);
  if (latchSaid.length !== 1 || !/^Talking\. Press Talk again to stop\./.test(latchSaid[0])) {
    problems.push(`latching said ${JSON.stringify(latchSaid)}`);
  }
  await page.keyboard.press("Enter");
  if (!await pressedIs("false")) problems.push("pressing Talk again did not stop latched talk");

  // A DJ going live and leaving, said once each from the socket pushes.
  live.live = { dj: "DJ Bob", since: "2026-10-13T21:00:00Z" };
  const lineIs = (pattern) => page.waitForFunction(
    (source) => new RegExp(source).test(document.getElementById("live-on-air").textContent),
    pattern, { timeout: 8000 },
  ).then(() => true, () => false);
  if (!await lineIs("^Live: DJ Bob since ")) problems.push("the live line did not follow the socket");
  await page.waitForTimeout(2500);
  live.live = null;
  if (!await lineIs("^$")) problems.push("the live line stayed after the DJ left");
  await page.waitForTimeout(2500);
  now = await state();
  const liveSaid = now.said.filter((text) => /^Live: |live DJ has left/.test(text));
  if (liveSaid.length !== 2 || !/^Live: DJ Bob since /.test(liveSaid[0]) || !/left/.test(liveSaid[1])) {
    problems.push(`a DJ going live and leaving said ${JSON.stringify(liveSaid)}`);
  }
  live.live = LIVE_DJ;
  await fakeTalkAudio(page, false);
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
    const help = document.getElementById("preview-device-desc");
    const box = (node) => node && node.getClientRects().length
      ? node.getBoundingClientRect().toJSON() : null;
    return {
      label: element.labels?.[0]?.textContent.trim(),
      describedBy: element.getAttribute("aria-describedby") || "",
      helpShown: Boolean(help && !help.hidden && help.getClientRects().length && help.textContent.trim()),
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
  if (!info.helpShown) problems.push("help is not visible");
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
    const full = FULL_MODES[name] || FULL_MODES.chromium;
    for (const mode of THEME_MODES) {
      if (full.includes(mode.name)) await auditTheme(browser, mode, out);
      else if (!mode.stored) await checkSystemTheme(browser, mode, out);
    }
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
    const liveDj = await liveDjProblems(page);
    if (liveDj.length) out.behaviour.liveDj = liveDj;
    const ads = await adsProblems(page);
    if (ads.length) out.behaviour.ads = ads;
    const speech = await speechStudioProblems(page);
    if (speech.length) out.behaviour.speechStudio = speech;
    const stationCreate = await stationCreateProblems(page);
    if (stationCreate.length) out.behaviour.stationCreate = stationCreate;
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
