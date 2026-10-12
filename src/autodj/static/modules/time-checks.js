// Settings > Time checks: the switches a paired browser may change, which
// configured voice packs speak (by name; the server keeps the folders), each
// pack's and the recorded clips' coverage, and "Play a time check now".
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
  const order = get("pack-order");
  const packList = get("pack-list");
  const packsEmpty = get("packs-empty");
  const testVoice = get("test-voice");
  const panel = doc.getElementById("spanel-time-checks");
  if (!enabled || !format || !rule || !clips || !status || !test || !order || !packList
      || !packsEmpty || !testVoice) {
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

  const packBoxes = () => [...packList.querySelectorAll("input[type=checkbox]")];

  // One native checkbox per pack, its coverage as its description.  The
  // rows are rebuilt only when the configured packs change, so a refresh
  // never takes focus away from a checkbox.
  function showPacks(packs) {
    const names = packs.map((pack) => pack.name);
    if (JSON.stringify(packBoxes().map((box) => box.value)) !== JSON.stringify(names)) {
      packList.replaceChildren(...packs.map((pack, index) => {
        const row = doc.createElement("div");
        row.className = "settings-check";
        const box = doc.createElement("input");
        box.type = "checkbox";
        box.id = `tc-pack-${index}`;
        box.value = pack.name;
        box.setAttribute("aria-describedby", `tc-pack-${index}-desc`);
        box.addEventListener("change", save);
        const label = doc.createElement("label");
        label.htmlFor = box.id;
        label.textContent = pack.name;
        const desc = doc.createElement("span");
        desc.className = "setting-desc";
        desc.id = `tc-pack-${index}-desc`;
        row.append(box, label);
        return [row, desc];
      }).flat());
      const chosen = testVoice.value;
      testVoice.replaceChildren(testVoice.options[0], ...names.map((name) => {
        const option = doc.createElement("option");
        option.value = name;
        option.textContent = `Voice pack ${name}`;
        return option;
      }));
      testVoice.value = names.includes(chosen) ? chosen : "";
    }
    packBoxes().forEach((box, index) => {
      if (doc.activeElement !== box) box.checked = packs[index].enabled;
      get(`pack-${index}-desc`).textContent = packs[index].summary;
    });
    packsEmpty.hidden = packs.length > 0;
  }

  function showSettings() {
    if (doc.activeElement !== enabled) enabled.checked = settings.enabled;
    if (doc.activeElement !== format) format.value = settings.hour_format;
    if (doc.activeElement !== rule) rule.value = settings.rule;
    if (doc.activeElement !== order) order.value = settings.pack_order;
    showPacks(settings.packs || []);
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
    const body = { enabled: enabled.checked, hour_format: format.value, rule: rule.value,
      enabled_packs: packBoxes().filter((box) => box.checked).map((box) => box.value),
      pack_order: order.value };
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
        order.value = settings.pack_order;
        packBoxes().forEach((box, index) => { box.checked = Boolean(settings.packs?.[index]?.enabled); });
      }
      say(`Could not save time check settings: ${error.message}`, "error");
    }
  }

  async function playHere(pack = "") {
    const query = pack ? `?pack=${encodeURIComponent(pack)}` : "";
    const audio = await binary(`/api/time-checks/preview${query}`);
    return playLiner(audio, settings?.duck_db ?? -12);
  }

  async function testNow() {
    const pack = testVoice.value;
    try {
      if (testOnServer()) {
        const options = pack ? { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pack }) } : { method: "POST" };
        const result = await request("/api/time-checks/test", options);
        say(`${result?.message || "Time check played"}.`);
        return;
      }
      const refusal = prepareTest();
      if (refusal) {
        say(refusal);
        return;
      }
      if (await playHere(pack)) say("Time check played.");
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

  for (const control of [enabled, format, rule, order]) control.addEventListener("change", save);
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
