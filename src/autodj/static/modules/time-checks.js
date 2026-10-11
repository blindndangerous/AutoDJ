// Settings > Time checks: the three switches a paired browser may change,
// the recorded-clip coverage, and "Play a time check now".
//
// In browser playback this page also plays the time check after the song
// that crosses the hour, through the same voice liner path (duck and
// master gain) as liners.js.  When the server mixes, the server plays it.
// Each button or setting result is said once through #tc-status; the
// automatic check after the hour says nothing.

import { requestBinary, requestJson, withDisabled } from "./api-client.js";
import { announceStatus } from "./live-region.js";

const MINUTES = 1440;

export function installTimeChecks({ doc = document, request = requestJson,
  binary = requestBinary, playLiner = async () => false,
  testOnServer = () => true, canPlay = () => false,
  prepareTest = () => null, now = () => new Date(), interval = setInterval } = {}) {
  const get = (name) => doc.getElementById(`tc-${name}`);
  const enabled = get("enabled");
  const format = get("hour-format");
  const rule = get("rule");
  const clips = get("clips-status");
  const status = get("status");
  const test = get("test");
  const panel = doc.getElementById("spanel-time-checks");
  if (!enabled || !format || !rule || !clips || !status || !test) {
    return { refresh: async () => {}, noteTrack: () => {} };
  }
  let settings = null;
  let previousTrack = null;
  let previousHour = null;
  let playing = false;
  let pending = false;

  function say(message, tone = "info") {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 4000, tone });
  }

  function hourKey(date) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: settings?.timezone || "UTC", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", hourCycle: "h23", timeZoneName: "shortOffset",
    }).formatToParts(date);
    return parts.filter((part) => part.type !== "literal").map((part) => part.value).join(":");
  }

  function showSettings() {
    if (doc.activeElement !== enabled) enabled.checked = settings.enabled;
    if (doc.activeElement !== format) format.value = settings.hour_format;
    if (doc.activeElement !== rule) rule.value = settings.rule;
    const recorded = MINUTES - settings.missing_minutes;
    const fallback = settings.tts_available
      ? "Text to speech says the minutes without a clip."
      : "No text to speech is set up, so minutes without a clip are skipped.";
    clips.textContent = `${recorded} of ${MINUTES} minute clips recorded. ${fallback} `
      + `Times are in the station time zone, ${settings.timezone}.`;
  }

  // Background loads write into the panel; they are not announced.
  async function refresh() {
    try {
      settings = await request("/api/time-checks");
      if (previousTrack && previousHour === null) previousHour = hourKey(now());
      showSettings();
    } catch (error) {
      clips.textContent = `Could not load time checks: ${error.message}`;
    }
  }

  async function save() {
    const body = { enabled: enabled.checked, hour_format: format.value, rule: rule.value };
    try {
      settings = await request("/api/time-checks", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!settings.enabled || settings.rule === "off") pending = false;
      showSettings();
      say("Time check settings saved.");
    } catch (error) {
      if (settings) {
        enabled.checked = settings.enabled;
        format.value = settings.hour_format;
        rule.value = settings.rule;
      }
      say(`Could not save time check settings: ${error.message}`, "error");
    }
  }

  async function playHere() {
    const audio = await binary("/api/time-checks/preview");
    return playLiner(audio, settings?.duck_db ?? -12);
  }

  async function testNow() {
    try {
      if (testOnServer()) {
        await request("/api/time-checks/test", { method: "POST" });
        say("Time check played.");
        return;
      }
      const refusal = prepareTest();
      if (refusal) {
        say(refusal);
        return;
      }
      if (await playHere()) say("Time check played.");
      else say("Time check was not played.");
    } catch (error) {
      say(`Time check failed: ${error.message}`, "error");
    }
  }

  // The check after the hour, once, when the browser can play it (not
  // during a crossfade, a pause or before the audio has started).
  function playPending() {
    if (!pending || playing || testOnServer() || !canPlay()) return;
    pending = false;
    playing = true;
    void playHere().catch(() => {}).finally(() => { playing = false; });
  }

  for (const control of [enabled, format, rule]) control.addEventListener("change", save);
  test.addEventListener("click", () => withDisabled(test, testNow));
  panel?.addEventListener("toggle", () => {
    if (panel.open) void refresh();
  });
  interval(playPending, 1000);
  void refresh();

  return {
    refresh,
    noteTrack(state) {
      const path = state?.current_track?.path;
      if (!path || path === previousTrack) return;
      if (!settings) {
        previousTrack = path;
        return;
      }
      const currentHour = hourKey(now());
      const crossed = previousHour !== null && previousHour !== currentHour;
      previousTrack = path;
      previousHour = currentHour;
      if (crossed && settings.enabled && settings.rule === "after_hour_song"
          && !testOnServer()) {
        pending = true;
        playPending();
      }
    },
  };
}
