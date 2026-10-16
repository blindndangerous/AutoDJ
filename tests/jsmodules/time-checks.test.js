
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup as installIndex } from "./helpers.js";

import { initSubtabs } from "../../src/autodj/static/modules/subtabs.js";
import { installTimeChecks } from "../../src/autodj/static/modules/time-checks.js";

// The real Settings markup, so the ids, labels and live region the module
// relies on are the ones the page ships.

const byId = (id) => document.getElementById(id);
const liveText = () => vi.waitFor(() => {
  const text = byId("tc-status").textContent;
  if (!text) throw new Error("nothing said yet");
  return text;
});

function server(overrides = {}) {
  const settings = { enabled: true, hour_format: "12", rule: "after_hour_song",
    timezone: "UTC", duck_db: -12, missing_minutes: 1439, tts_available: true,
    enabled_packs: ["Studio"], pack_order: "rotate",
    packs: [
      { name: "Studio", enabled: true, minutes_covered: 1440,
        summary: "1440 of 1440 minutes covered. Hour files missing: none." },
      { name: "Night & Day", enabled: false, minutes_covered: 0, summary: "Folder not found." },
    ],
    ...overrides };
  return vi.fn(async (url, options = {}) => {
    if (options.method === "PUT") return { ...settings, ...JSON.parse(options.body) };
    if (url.endsWith("/test")) return { message: "Time check played" };
    return settings;
  });
}

beforeEach(installIndex);

describe("Time checks panel markup", () => {
  it("is a Settings subtab between Voice liners and Breaks, with no aria-controls", () => {
    const tab = byId("stab-time-checks");
    expect(tab.getAttribute("role")).toBe("tab");
    expect(tab.hasAttribute("aria-controls")).toBe(false);
    expect(tab.previousElementSibling.id).toBe("stab-liners");
    expect(tab.nextElementSibling.id).toBe("stab-breaks");
    const panel = byId(tab.dataset.panel);
    expect(panel.id).toBe("spanel-time-checks");
    expect(panel.getAttribute("role")).toBe("tabpanel");
    expect(panel.getAttribute("aria-labelledby")).toBe("stab-time-checks");
    expect(panel.querySelector(":scope > h3").textContent).toBe("Time checks");
  });

  it("labels native controls and keeps general help available separately", () => {
    expect(byId("tc-enabled").type).toBe("checkbox");
    expect(byId("tc-hour-format").tagName).toBe("SELECT");
    expect([...byId("tc-hour-format").options].map((o) => o.value)).toEqual(["12", "24"]);
    expect(byId("tc-rule").tagName).toBe("SELECT");
    expect([...byId("tc-rule").options].map((o) => o.value)).toEqual(["off", "after_hour_song"]);
    for (const id of ["tc-enabled", "tc-hour-format", "tc-rule"]) {
      const label = document.querySelector(`label[for="${id}"]`);
      expect(label?.textContent.trim(), id).toBeTruthy();
    }
    const test = byId("tc-test");
    expect(test.tagName).toBe("BUTTON");
    expect(test.type).toBe("button");
    expect(test.textContent).toBe("Play a time check now");
    for (const id of ["tc-enabled", "tc-hour-format", "tc-rule", "tc-test"]) {
      const help = byId(`${id}-desc`);
      expect(help?.textContent.trim(), id).toBeTruthy();
      expect(help.hidden, id).toBe(false);
      expect(byId(id).getAttribute("aria-describedby")).toBe(id === "tc-test" ? help.id : null);
    }
    const status = byId("tc-status");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.getAttribute("aria-atomic")).toBe("true");
    expect(byId("tc-clips-status").getAttribute("aria-live")).toBeNull();
  });

  it("groups the voice packs and labels the order and test choices", () => {
    const packs = byId("tc-packs");
    expect(packs.tagName).toBe("FIELDSET");
    expect(packs.querySelector("legend").textContent).toBe("Voice packs");
    expect(packs.hasAttribute("aria-describedby")).toBe(false);
    expect(byId("tc-packs-desc").textContent).toContain("RadioBOSS");
    expect(byId("tc-pack-list").getAttribute("aria-live")).toBeNull();
    expect([...byId("tc-pack-order").options].map((o) => o.value)).toEqual(["rotate", "random"]);
    for (const id of ["tc-pack-order", "tc-test-voice"]) {
      expect(document.querySelector(`label[for="${id}"]`)?.textContent.trim(), id).toBeTruthy();
      expect(byId(id).hasAttribute("aria-describedby")).toBe(false);
      expect(byId(`${id}-desc`)?.textContent.trim(), id).toBeTruthy();
    }
    expect(byId("tc-test-voice").options[0].value).toBe("");
  });
});

describe("voice packs", () => {
  it("lists each pack as a labelled, described native checkbox", async () => {
    const controls = installTimeChecks({ request: server(), interval: () => {} });
    await controls.refresh();
    const boxes = [...byId("tc-pack-list").querySelectorAll("input")];
    expect(boxes.map((box) => [box.type, box.value, box.checked])).toEqual([
      ["checkbox", "Studio", true], ["checkbox", "Night & Day", false]]);
    for (const box of boxes) {
      expect(document.querySelector(`label[for="${box.id}"]`).textContent).toBe(box.value);
    }
    expect(byId(boxes[1].getAttribute("aria-describedby")).textContent).toBe("Folder not found.");
    expect(byId("tc-packs-empty").hidden).toBe(true);
    expect([...byId("tc-test-voice").options].map((o) => o.textContent)).toEqual([
      "The ticked packs, then clips, then text to speech", "Voice pack Studio",
      "Voice pack Night & Day"]);
    expect(byId("tc-pack-order").value).toBe("rotate");
  });

  it("says so when there are no packs", async () => {
    const controls = installTimeChecks({ request: server({ packs: [] }), interval: () => {} });
    await controls.refresh();
    expect(byId("tc-packs-empty").hidden).toBe(false);
    expect(byId("tc-pack-list").children).toHaveLength(0);
    expect(byId("tc-dates").hidden).toBe(true);
  });

  it("says each pack's play dates and saves them for the chosen pack", async () => {
    const dated = { not_before: "2026-12-01", not_after: "2026-12-26",
      dates_text: "Plays from 1 December 2026 until 26 December 2026",
      playable: false, unplayable_text: "not playable before 1 December 2026" };
    const request = vi.fn(async (url, options = {}) => {
      const packs = [
        { name: "Studio", enabled: true, minutes_covered: 1440, summary: "All covered." },
        { name: "Christmas", enabled: true, minutes_covered: 1440, summary: "All covered.",
          ...(url.includes("/packs/") || request.saved ? dated : {}) },
      ];
      if (url.includes("/packs/")) request.saved = JSON.parse(options.body);
      return { enabled: true, hour_format: "12", rule: "off", timezone: "UTC", duck_db: -12,
        missing_minutes: 0, tts_available: false, enabled_packs: ["Studio", "Christmas"],
        pack_order: "rotate", packs };
    });
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    const group = byId("tc-dates");
    expect(group.hidden).toBe(false);
    expect(group.tagName).toBe("FIELDSET");
    expect(group.querySelector("legend").textContent).toBe("Voice pack play dates");
    expect(group.hasAttribute("aria-describedby")).toBe(false);
    for (const id of ["tc-dates-pack", "tc-not-before", "tc-not-after"]) {
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim(), id).toBeTruthy();
    }
    expect(byId("tc-not-before").type).toBe("date");
    byId("tc-dates-pack").value = "Christmas";
    byId("tc-dates-pack").dispatchEvent(new Event("change"));
    byId("tc-not-before").value = "2026-12-01";
    byId("tc-not-after").value = "2026-12-26";
    byId("tc-dates-save").click();
    expect(await liveText()).toBe(
      "Saved. Voice pack Christmas: Plays from 1 December 2026 until 26 December 2026.");
    const call = request.mock.calls.find(([url]) => url.includes("/packs/"));
    expect(call[0]).toBe("/api/time-checks/packs/Christmas/dates");
    expect(request.saved).toEqual({ not_before: "2026-12-01", not_after: "2026-12-26" });
    const box = [...byId("tc-pack-list").querySelectorAll("input")][1];
    expect(byId(box.getAttribute("aria-describedby")).textContent).toBe(
      "All covered. Plays from 1 December 2026 until 26 December 2026. "
      + "Skipped now: not playable before 1 December 2026.");
  });

  it("names a backwards range and a refused one, linked to the field", async () => {
    const request = server();
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    const until = byId("tc-not-after");
    byId("tc-not-before").value = "2026-12-26";
    until.value = "2026-12-01";
    byId("tc-dates-save").click();
    const message = "Do not play after must be on or after the Not before date.";
    await vi.waitFor(() => expect(document.activeElement).toBe(until));
    expect(until.getAttribute("aria-invalid")).toBe("true");
    expect(until.getAttribute("aria-describedby")).toBe("tc-dates-error");
    expect(byId("tc-dates-error").textContent).toBe(message);
    expect(byId("tc-dates-error").hidden).toBe(false);
    expect(request.mock.calls.some(([url]) => url.includes("/packs/"))).toBe(false);
    until.value = "2026-12-31";
    until.dispatchEvent(new Event("input"));
    expect(until.hasAttribute("aria-invalid")).toBe(false);
    await vi.waitFor(() => expect(byId("tc-dates-save").disabled).toBe(false));

    request.mockImplementationOnce(async () => {
      const error = new Error("Not before: Input should be a valid date");
      error.field = "not_before";
      throw error;
    });
    byId("tc-dates-save").click();
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("tc-not-before")));
    expect(byId("tc-not-before").getAttribute("aria-describedby")).toBe("tc-dates-error");
    expect(byId("tc-dates-error").textContent).toBe(
      "Could not save the play dates. Not before: Input should be a valid date");
  });

  it("saves ticked packs and the order by name, out loud, and keeps focus on refresh", async () => {
    const request = server();
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    const night = byId("tc-pack-list").querySelectorAll("input")[1];
    night.focus();
    night.checked = true;
    night.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith("/api/time-checks",
      expect.objectContaining({ method: "PUT" })));
    const put = request.mock.calls.find(([, options]) => options?.method === "PUT")[1];
    expect(JSON.parse(put.body)).toMatchObject({
      enabled_packs: ["Studio", "Night & Day"], pack_order: "rotate" });
    expect(await liveText()).toBe("Time check settings saved.");
    await controls.refresh();
    expect(document.activeElement).toBe(night);
    const order = byId("tc-pack-order");
    order.value = "random";
    order.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(request.mock.calls.some(([, options]) =>
      options?.body?.includes('"pack_order":"random"'))).toBe(true));
  });

  it("puts the ticks back when the save fails", async () => {
    const base = server();
    const request = vi.fn((url, options = {}) => options.method === "PUT"
      ? Promise.reject(new Error("No voice pack named Old")) : base(url, options));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    const studio = byId("tc-pack-list").querySelector("input");
    studio.checked = false;
    studio.dispatchEvent(new Event("change"));
    expect(await liveText()).toBe("Could not save time check settings: No voice pack named Old");
    expect(studio.checked).toBe(true);
  });

  it("tests a chosen pack on the server and says where the voice came from", async () => {
    const request = vi.fn(async (url, options = {}) => (url.endsWith("/test")
      ? { message: "Voice pack Studio cannot say this minute; played the recorded clip" }
      : server()(url, options)));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    byId("tc-test-voice").value = "Studio";
    byId("tc-test").click();
    expect(await liveText()).toBe(
      "Voice pack Studio cannot say this minute; played the recorded clip.");
    expect(request).toHaveBeenCalledWith("/api/time-checks/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pack: "Studio" }) });
  });

  it("previews a chosen pack in this browser", async () => {
    const binary = vi.fn(async () => new ArrayBuffer(8));
    const controls = installTimeChecks({ request: server(), binary,
      playLiner: async () => true, testOnServer: () => false, interval: () => {} });
    await controls.refresh();
    byId("tc-test-voice").value = "Night & Day";
    byId("tc-test").click();
    expect(await liveText()).toBe("Time check played.");
    expect(binary).toHaveBeenCalledWith("/api/time-checks/preview?pack=Night%20%26%20Day");
  });
});

describe("time checks panel", () => {
  it("shows clip coverage and the time zone, and saves a changed switch out loud", async () => {
    const request = server();
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    expect(byId("tc-clips-status").textContent).toBe(
      "1 of 1440 minute clips recorded. Text to speech says the minutes without a clip. "
      + "Times are in the station time zone, UTC.",
    );
    expect(byId("tc-enabled").checked).toBe(true);
    expect(byId("tc-rule").value).toBe("after_hour_song");
    const format = byId("tc-hour-format");
    format.value = "24";
    format.dispatchEvent(new Event("change"));
    await vi.waitFor(() => expect(request).toHaveBeenCalledWith("/api/time-checks",
      expect.objectContaining({ method: "PUT", body: expect.stringContaining('"hour_format":"24"') })));
    expect(await liveText()).toBe("Time check settings saved.");
  });

  it("keeps the newest load when an older reply lands late", async () => {
    const base = server();
    const replies = [];
    const request = vi.fn((url, options = {}) => (options.method
      ? base(url, options)
      : new Promise((resolve) => { replies.push(resolve); })));
    const controls = installTimeChecks({ request, interval: () => {} });
    const newer = controls.refresh();
    replies[1]({ ...(await base("/api/time-checks")), timezone: "Europe/London" });
    await newer;
    const shown = byId("tc-clips-status").textContent;
    expect(shown).toContain("Europe/London");
    replies[0]({ ...(await base("/api/time-checks")), timezone: "America/New_York" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(byId("tc-clips-status").textContent).toBe(shown);
  });

  it("empties on a station switch, and a reply for the old station writes nothing", async () => {
    const base = server();
    let late;
    const request = vi.fn((url, options = {}) => (options.method || !late
      ? base(url, options)
      : new Promise((resolve) => { late(resolve); })));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    expect(byId("tc-pack-list").querySelectorAll("input")).toHaveLength(2);
    let answer;
    late = (resolve) => { answer = resolve; };
    const old = controls.refresh();
    controls.clear();
    expect(byId("tc-clips-status").textContent).toBe("");
    expect(byId("tc-status").textContent).toBe("");
    expect(byId("tc-pack-list").children).toHaveLength(0);
    expect(byId("tc-packs-empty").hidden).toBe(true);
    answer(await base("/api/time-checks"));
    await old;
    expect(byId("tc-clips-status").textContent).toBe("");
  });

  it("says nothing for a failed load that was for the station just left (#70)", async () => {
    const { selectStation, setKnownStations } = await import("../../src/autodj/static/modules/stations.js");
    setKnownStations([{ slug: "default", name: "Main" }, { slug: "night", name: "Night" }]);
    let reply;
    const fetchImpl = vi.fn(() => new Promise((resolve) => { reply = resolve; }));
    vi.stubGlobal("fetch", fetchImpl);
    try {
      installTimeChecks({ interval: () => {} });
      await vi.waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1));
      selectStation("night");
      reply(new globalThis.Response("{}", { status: 500, headers: { "Content-Type": "application/json" } }));
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(byId("tc-clips-status").textContent).toBe("");
      expect(byId("tc-status").textContent).toBe("");
    } finally {
      selectStation("default");
      vi.unstubAllGlobals();
    }
  });

  it("says what a save, a test or new dates answered after a switch did, leaving the form alone (#70)", async () => {
    const { selectStation, setKnownStations } = await import("../../src/autodj/static/modules/stations.js");
    setKnownStations([{ slug: "default", name: "Main" }, { slug: "night", name: "Night" }]);
    const json = (body, status = 200) => new globalThis.Response(JSON.stringify(body),
      { status, headers: { "Content-Type": "application/json" } });
    const loaded = await server()("/api/time-checks");
    let reply = null;
    const fetchImpl = vi.fn((url, options = {}) => (options.method
      ? new Promise((resolve) => { reply = resolve; })
      : Promise.resolve(json(loaded))));
    vi.stubGlobal("fetch", fetchImpl);
    // Each action is sent for Main, the page switches to Night, and the
    // reply lands; the next one goes the other way.
    const answeredAfterSwitch = async (press, body, status, to) => {
      reply = null;
      press();
      await vi.waitFor(() => expect(reply).toBeTypeOf("function"));
      selectStation(to);
      reply(json(body, status));
      return liveText();
    };
    try {
      const controls = installTimeChecks({ interval: () => {}, testOnServer: () => true });
      await controls.refresh();
      const enabled = byId("tc-enabled");
      enabled.checked = false;
      expect(await answeredAfterSwitch(() => enabled.dispatchEvent(new Event("change")),
        { ...loaded, enabled: false }, 200, "night"))
        .toBe("Done on Main, the station this page has just left.");
      // Not put back to the old station's saved value.
      expect(enabled.checked).toBe(false);
      byId("tc-status").textContent = "";

      expect(await answeredAfterSwitch(() => byId("tc-test").click(), { detail: "No" }, 409, "default"))
        .toBe("Night did not do that; it is the station this page has just left.");
      byId("tc-status").textContent = "";

      byId("tc-dates-pack").value = "Studio";
      byId("tc-not-before").value = "2026-12-01";
      expect(await answeredAfterSwitch(() => byId("tc-dates-save").click(),
        { detail: "boom", field: "not_before" }, 500, "night"))
        .toBe("Main may not have done that; it is the station this page has just left.");
      expect(byId("tc-not-before").hasAttribute("aria-invalid")).toBe(false);
    } finally {
      selectStation("default");
      vi.unstubAllGlobals();
    }
  });

  it("says when minutes without a clip are skipped", async () => {
    const controls = installTimeChecks({
      request: server({ tts_available: false, missing_minutes: 1440 }), interval: () => {} });
    await controls.refresh();
    expect(byId("tc-clips-status").textContent).toContain(
      "0 of 1440 minute clips recorded. No text to speech is set up");
  });

  it("puts a failed save back and says why", async () => {
    const base = server();
    const request = vi.fn((url, options = {}) => options.method === "PUT"
      ? Promise.reject(new Error("Server busy")) : base(url, options));
    const controls = installTimeChecks({ request, interval: () => {} });
    await controls.refresh();
    const enabled = byId("tc-enabled");
    enabled.checked = false;
    enabled.dispatchEvent(new Event("change"));
    expect(await liveText()).toBe("Could not save time check settings: Server busy");
    expect(enabled.checked).toBe(true);
  });

  it("refreshes the coverage when its subtab is shown", async () => {
    const request = server();
    byId("panel-settings").hidden = false;
    installTimeChecks({ request, interval: () => {} });
    const store = { getItem: () => null, setItem: () => {} };
    initSubtabs(byId("settings-tabs"), { storage: store });
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(1));
    byId("stab-time-checks").click();
    await vi.waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  });

  it("plays on the server and says the result when the server mixes", async () => {
    const request = server();
    installTimeChecks({ request, interval: () => {} });
    byId("tc-test").click();
    expect(await liveText()).toBe("Time check played.");
    expect(request).toHaveBeenCalledWith("/api/time-checks/test", { method: "POST" });
  });

  it("says why server playback failed", async () => {
    const base = server();
    const request = vi.fn((url, options) => url.endsWith("/test")
      ? Promise.reject(new Error("The stream must be playing to air a time check"))
      : base(url, options));
    installTimeChecks({ request, interval: () => {} });
    byId("tc-test").click();
    expect(await liveText()).toBe(
      "Time check failed: The stream must be playing to air a time check");
  });

  it("plays in this browser through the voice liner path, or says why not", async () => {
    const binary = vi.fn(async () => new ArrayBuffer(8));
    const playLiner = vi.fn(async () => true);
    let refusal = "Browser playback is muted.";
    const controls = installTimeChecks({ request: server({ duck_db: -9 }), binary, playLiner,
      testOnServer: () => false, prepareTest: () => refusal, interval: () => {} });
    await controls.refresh();
    byId("tc-test").click();
    expect(await liveText()).toBe("Browser playback is muted.");
    expect(binary).not.toHaveBeenCalled();
    refusal = null;
    byId("tc-status").textContent = "";
    byId("tc-test").click();
    expect(await liveText()).toBe("Time check played.");
    expect(binary).toHaveBeenCalledWith("/api/time-checks/preview");
    expect(playLiner).toHaveBeenCalledWith(expect.any(ArrayBuffer), -9);
  });
});

describe("the check after the hour in browser playback", () => {
  it("plays once, silently, when the song that crossed the hour ends", async () => {
    const binary = vi.fn(async () => new ArrayBuffer(8));
    const playLiner = vi.fn(async () => true);
    let clock = new Date("2026-10-11T03:59:00Z");
    const controls = installTimeChecks({ request: server(), binary, playLiner,
      testOnServer: () => false, canPlay: () => true, now: () => clock, interval: () => {} });
    await controls.refresh();
    controls.noteTrack({ current_track: { path: "first.mp3" } });
    clock = new Date("2026-10-11T04:02:00Z");
    controls.noteTrack({ current_track: { path: "second.mp3" } });
    controls.noteTrack({ current_track: { path: "second.mp3" } });
    await vi.waitFor(() => expect(playLiner).toHaveBeenCalledWith(expect.any(ArrayBuffer), -12));
    clock = new Date("2026-10-11T04:05:00Z");
    controls.noteTrack({ current_track: { path: "third.mp3" } });
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(playLiner).toHaveBeenCalledOnce();
    expect(byId("tc-status").textContent).toBe("");
  });

  it("waits for the crossfade to finish before playing the pending hour", async () => {
    const playLiner = vi.fn(async () => true);
    let ready = false;
    let tick;
    let clock = new Date("2026-10-11T03:59:00Z");
    const controls = installTimeChecks({ request: server(), binary: async () => new ArrayBuffer(8),
      playLiner, testOnServer: () => false, canPlay: () => ready, now: () => clock,
      interval: (callback) => { tick = callback; } });
    await controls.refresh();
    controls.noteTrack({ current_track: { path: "first.mp3" } });
    clock = new Date("2026-10-11T04:02:00Z");
    controls.noteTrack({ current_track: { path: "second.mp3" } });
    expect(playLiner).not.toHaveBeenCalled();
    ready = true;
    tick();
    await vi.waitFor(() => expect(playLiner).toHaveBeenCalledOnce());
  });

  it("leaves the check to the server when the server mixes, and to nobody when off", async () => {
    const playLiner = vi.fn(async () => true);
    let clock = new Date("2026-10-11T03:59:00Z");
    for (const [settings, onServer] of [[{}, true], [{ rule: "off" }, false],
      [{ enabled: false }, false]]) {
      installIndex();
      const controls = installTimeChecks({ request: server(settings),
        binary: async () => new ArrayBuffer(8), playLiner, testOnServer: () => onServer,
        canPlay: () => true, now: () => clock, interval: () => {} });
      await controls.refresh();
      clock = new Date("2026-10-11T03:59:00Z");
      controls.noteTrack({ current_track: { path: "first.mp3" } });
      clock = new Date("2026-10-11T04:02:00Z");
      controls.noteTrack({ current_track: { path: "second.mp3" } });
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(playLiner).not.toHaveBeenCalled();
  });
});
