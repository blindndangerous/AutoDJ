import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyRepeatSettings, installRepeatSettings, resetRepeatSettings,
} from "../../src/autodj/static/modules/repeat-settings.js";
import { applySettingsDrafts, resetSettingsDrafts } from
  "../../src/autodj/static/modules/settings-drafts.js";

const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");

afterEach(() => {
  vi.restoreAllMocks();
  resetSettingsDrafts(document);
  document.body.replaceChildren();
});

// The real page, without its scripts, and the same elements app.js hands
// to installRepeatSettings.
function installPage() {
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll('script, link[rel="stylesheet"]')
    .forEach((element) => element.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  const $ = (id) => document.getElementById(id);
  return {
    artistTracks: $("repeat-artist-tracks"), artistMinutes: $("repeat-artist-minutes"),
    albumTracks: $("repeat-album-tracks"), albumMinutes: $("repeat-album-minutes"),
    trackTracks: $("repeat-track-tracks"), trackMinutes: $("repeat-track-minutes"),
    titleTracks: $("repeat-title-tracks"), titleMinutes: $("repeat-title-minutes"),
    relatedArtistTracks: $("repeat-related-artist-tracks"),
    relatedArtistMinutes: $("repeat-related-artist-minutes"),
    relatedTrackTracks: $("repeat-related-track-tracks"),
    relatedTrackMinutes: $("repeat-related-track-minutes"),
    useRelatedArtists: $("repeat-use-related-artists"),
    useRelatedTracks: $("repeat-use-related-tracks"),
    versionsAsOneSong: $("repeat-versions-as-one-song"),
    daypartDays: $("repeat-daypart-days"),
    daypartHours: $("repeat-daypart-hours"),
    hourpartPlays: $("repeat-hourpart-plays"),
    error: $("repeat-error"),
    defaultFile: $("repeat-related-default"),
    fileStatus: $("repeat-related-status"),
    tracksDefaultFile: $("repeat-related-tracks-default"),
    tracksFileStatus: $("repeat-related-tracks-status"),
    repeatGroup: $("repeat-artist-tracks").closest("fieldset").parentElement,
  };
}

function setup(fail = false) {
  const els = installPage();
  const settingsStatus = document.getElementById("settings-status");
  const panel = document.getElementById("spanel-picking");
  installRepeatSettings(els, { settingsStatus });
  const requests = [];
  vi.stubGlobal("fetch", vi.fn(async (_url, options = {}) => {
    const body = JSON.parse(options.body);
    requests.push(body);
    if (fail) {
      return new globalThis.Response(JSON.stringify({ detail: "The settings service is busy." }), {
        status: 503, headers: { "Content-Type": "application/json" },
      });
    }
    const values = { playback: { repeat_rules: body.changes.playback.repeat_rules } };
    return new globalThis.Response(JSON.stringify({ revision: "two", settings: { revision: "two", values },
      applied: ["playback"], failed: {} }), { headers: { "Content-Type": "application/json" } });
  }));
  applySettingsDrafts({ profile: "", revision: "one", values: { playback: { repeat_rules: fullRules } } });
  els.save = panel.querySelector("[data-settings-save]");
  els.panel = panel;
  els.repeatGroup = els.artistTracks.closest("fieldset").parentElement;
  els.requests = requests;
  return { els, settingsStatus, requests };
}

function type(control, value) {
  control.value = value;
  control.dispatchEvent(new Event("input", { bubbles: true }));
}

const describedBy = (element) => (element.getAttribute("aria-describedby") || "")
  .split(/\s+/).filter(Boolean).map((id) => document.getElementById(id));

const fullRules = {
  artist: { tracks: 8, minutes: 0 }, related_artist: { tracks: 8, minutes: 0 },
  album: { tracks: 0, minutes: 0 }, track: { tracks: 50, minutes: 0 },
  title: { tracks: 0, minutes: 0 }, related_track: { tracks: 20, minutes: 0 },
  use_related_artists: true, use_related_tracks: false, versions_as_one_song: false,
  daypart: { days: 0, start_hours: [0, 6, 10, 15, 19] }, hourpart: { plays: 0 },
};

describe("Repeats markup", () => {
  it("groups each rule under a legend and labels every box", () => {
    const els = installPage();
    const outer = els.repeatGroup;
    // Repeats is a card closed by default; the heading in its summary
    // names the outer group.
    const card = outer.parentElement;
    expect(card.matches("details.card:not([open])")).toBe(true);
    const heading = card.querySelector(":scope > summary > h4");
    expect(heading.textContent).toBe("Repeats");
    expect(outer.getAttribute("aria-labelledby")).toBe(heading.id);
    expect(outer.querySelector(":scope > legend")).toBeNull();
    const groups = [...outer.querySelectorAll(":scope > fieldset")];
    expect(groups.map((group) => group.querySelector(":scope > legend").textContent)).toEqual([
      "Prevent the same artist from repeating within",
      "Related artists",
      "Prevent the same album from repeating within",
      "Prevent the same track from repeating within",
      "Prevent the same title from repeating within",
      "Related tracks",
      "Prevent a song from playing in the same daypart",
      "Prevent a song from playing in the same quarter of the hour",
    ]);
    for (const control of outer.querySelectorAll("input")) {
      expect(control.labels).toHaveLength(1);
      expect(control.labels[0].textContent.trim()).not.toBe("");
    }
    for (const rule of ["artist", "relatedArtist", "album", "track", "title", "relatedTrack"]) {
      const group = els[`${rule}Tracks`].closest("fieldset");
      expect(els[`${rule}Minutes`].closest("fieldset")).toBe(group);
      expect(els[`${rule}Tracks`].labels[0].textContent).toBe("Tracks");
      expect(els[`${rule}Minutes`].labels[0].textContent).toBe("Minutes");
      expect(els[`${rule}Tracks`].max).toBe(rule === "track" ? "100000" : "5000");
      expect(els[`${rule}Minutes`].max).toBe("10080");
      expect(els[`${rule}Tracks`].closest(".settings-row").textContent)
        .toContain(`0 to ${rule === "track" ? "100000" : "5000"} tracks`);
      expect(els[`${rule}Minutes`].closest(".settings-row").textContent)
        .toContain("0 to 10080 minutes");
    }
  });

  it("keeps the Repeats introduction and field range hints visible", () => {
    const els = installPage();
    const outer = els.repeatGroup;
    const description = document.getElementById("repeat-rules-desc");
    expect(outer.hasAttribute("aria-describedby")).toBe(false);
    expect(description.hidden).toBe(false);
    expect(description.textContent).toContain("0 turns a limit off");
    expect(description.parentElement).toBe(outer);
    expect(description.compareDocumentPosition(els.artistTracks)
      & globalThis.Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    for (const box of outer.querySelectorAll("input[type=number]")) {
      expect(box.hasAttribute("aria-describedby")).toBe(false);
    }
  });

  it("puts Use related artists, its file note and its status line in one group", () => {
    const els = installPage();
    expect(document.getElementById("repeat-related-file")).toBeNull();
    const group = els.useRelatedArtists.closest("fieldset");
    expect(group.querySelector(":scope > legend").textContent).toBe("Related artists");
    expect(els.useRelatedArtists.labels[0].textContent).toBe("Use related artists");
    const [status] = describedBy(els.useRelatedArtists);
    const note = document.getElementById("repeat-related-desc");
    expect(describedBy(els.useRelatedArtists)).toEqual([els.fileStatus]);
    expect(note.hidden).toBe(false);
    expect(note.closest("fieldset")).toBe(group);
    expect(note.textContent).toContain("related-artists.toml in the library folder");
    expect(note.contains(els.defaultFile)).toBe(true);
    expect(status).toBe(els.fileStatus);
    expect(status.closest("fieldset")).toBe(group);
    expect(status.hidden).toBe(true);
    // Shown on load, not spoken: failures go through the settings status.
    expect(status.hasAttribute("aria-live")).toBe(false);
    expect(status.getAttribute("role")).toBeNull();
    // Its own limits sit in the same group.
    expect(els.relatedArtistTracks.closest("fieldset")).toBe(group);
  });

  it("puts Use related tracks, its file note, status line and limits in one group", () => {
    const els = installPage();
    const group = els.useRelatedTracks.closest("fieldset");
    expect(group.querySelector(":scope > legend").textContent).toBe("Related tracks");
    expect(els.useRelatedTracks.labels[0].textContent).toBe("Use related tracks");
    expect(els.useRelatedTracks.checked).toBe(false);
    const [status] = describedBy(els.useRelatedTracks);
    const note = document.getElementById("repeat-related-tracks-desc");
    expect(describedBy(els.useRelatedTracks)).toEqual([els.tracksFileStatus]);
    expect(note.hidden).toBe(false);
    expect(note.closest("fieldset")).toBe(group);
    expect(note.textContent).toContain("related-tracks.toml in the library folder");
    expect(note.textContent).toContain("Artist - Title");
    expect(note.contains(els.tracksDefaultFile)).toBe(true);
    expect(status).toBe(els.tracksFileStatus);
    expect(status.hidden).toBe(true);
    expect(status.hasAttribute("aria-live")).toBe(false);
    expect(els.relatedTrackTracks.closest("fieldset")).toBe(group);
    expect(els.relatedTrackTracks.value).toBe("20");
  });

  it("keeps daypart and quarter-hour help visible without linking it to the groups", () => {
    const els = installPage();
    const daypart = els.daypartDays.closest("fieldset");
    expect(els.daypartHours.closest("fieldset")).toBe(daypart);
    expect(els.daypartDays.labels[0].textContent).toBe("Days");
    expect(els.daypartDays.value).toBe("0");
    expect(els.daypartDays.max).toBe("7");
    expect(els.daypartHours.labels[0].textContent).toBe("Daypart start hours");
    expect(els.daypartHours.value).toBe("0, 6, 10, 15, 19");
    const groupNote = document.getElementById("repeat-daypart-desc");
    expect(daypart.hasAttribute("aria-describedby")).toBe(false);
    expect(groupNote.hidden).toBe(false);
    expect(groupNote.closest("fieldset")).toBe(daypart);
    expect(groupNote.textContent).toContain("0 days turns this off");
    const [hoursNote] = describedBy(els.daypartHours);
    expect(hoursNote.textContent).toContain("separated by commas");
    expect(hoursNote.textContent).toContain("Hours 0–23");
    expect(hoursNote.textContent).not.toContain("morning drive");
    expect(hoursNote.nextElementSibling.textContent).toContain("morning drive");

    const hourpart = els.hourpartPlays.closest("fieldset");
    expect(hourpart).not.toBe(daypart);
    expect(els.hourpartPlays.labels[0].textContent).toBe("Plays");
    expect(els.hourpartPlays.value).toBe("0");
    expect(els.hourpartPlays.max).toBe("3");
    const playsNote = document.getElementById("repeat-hourpart-desc");
    expect(hourpart.hasAttribute("aria-describedby")).toBe(false);
    expect(playsNote.hidden).toBe(false);
    expect(playsNote.closest("fieldset")).toBe(hourpart);
    expect(playsNote.textContent).toContain("quarter of the hour");
    expect(playsNote.textContent).toContain("0 turns this off");
  });

  it("keeps Count versions as one song help visible without describing the checkbox", () => {
    const els = installPage();
    const group = els.versionsAsOneSong.closest("fieldset");
    expect(group).toBe(els.trackTracks.closest("fieldset"));
    expect(els.versionsAsOneSong.labels[0].textContent).toBe("Count versions as one song");
    expect(els.versionsAsOneSong.checked).toBe(false);
    const note = document.getElementById("repeat-versions-desc");
    expect(els.versionsAsOneSong.hasAttribute("aria-describedby")).toBe(false);
    expect(note.hidden).toBe(false);
    expect(note.closest("fieldset")).toBe(group);
    expect(note.textContent).toContain("same lead artist");
  });
});

describe("repeat settings", () => {
  it("can update file status without accepting the settings snapshot twice", () => {
    const { els } = setup();
    type(els.artistTracks, "7");
    const accept = vi.spyOn(els.draft, "accept");
    applyRepeatSettings({ playback: {
      repeat_rules: fullRules,
      related_artists_problem: "The related artists file is not valid.",
    } }, els, { acceptDraft: false });
    expect(accept).not.toHaveBeenCalled();
    expect(els.artistTracks.value).toBe("7");
    expect(els.fileStatus.textContent).toBe("The related artists file is not valid.");
    expect(els.save.getAttribute("aria-disabled")).toBe("false");
  });
  it("loads the shared settings snapshot and saves only changed repeat fields", async () => {
    const { els, requests } = setup();
    expect(els.trackTracks.value).toBe("50");
    expect(els.useRelatedArtists.checked).toBe(true);
    type(els.artistTracks, "7");
    type(els.daypartHours, " 19 ,6,0 ");
    expect(requests).toHaveLength(0);
    els.save.click();
    await vi.waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toEqual({ profile: "", revision: "one", changes: { playback: { repeat_rules: {
      ...fullRules, artist: { tracks: 7, minutes: 0 },
      daypart: { days: 0, start_hours: [0, 6, 19] },
    } } } });
  });

  it.each([
    ["daypartDays", "8", "Same daypart days must be a whole number from 0 to 7."],
    ["daypartDays", "1.5", "Same daypart days must be a whole number from 0 to 7."],
    ["daypartHours", "6, 24", "Daypart start hours must be whole numbers from 0 to 23, "
      + "separated by commas, with no hour given twice."],
    ["daypartHours", "6, 6", "Daypart start hours must be whole numbers from 0 to 23, "
      + "separated by commas, with no hour given twice."],
    ["daypartHours", "", "Daypart start hours must be whole numbers from 0 to 23, "
      + "separated by commas, with no hour given twice."],
    ["daypartHours", "6 10", "Daypart start hours must be whole numbers from 0 to 23, "
      + "separated by commas, with no hour given twice."],
    ["hourpartPlays", "4", "Same quarter hour plays must be a whole number from 0 to 3."],
    ["artistTracks", "5001", "Same artist tracks must be a whole number from 0 to 5000."],
  ])("keeps invalid %s %j, marks and focuses it, and says why", async (name, value, message) => {
    const { els, requests, settingsStatus } = setup();
    type(els[name], value);
    els.save.click();
    expect(els[name].getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(els[name]);
    expect(settingsStatus.textContent).toBe("");
    expect(requests).toHaveLength(0);
    expect(els.error.hidden).toBe(false);
    expect(els.error.textContent).toBe(message);
    expect(describedBy(els[name])).toContain(els.error);
    type(els[name], name === "daypartHours" ? "0, 12" : "1");
    expect(els[name].hasAttribute("aria-invalid")).toBe(false);
    expect(els.error.hidden).toBe(true);
  });

  it("keeps related file status separate and excludes file paths from repeat patches", async () => {
    const { els, requests } = setup();
    const problem = "The related tracks file is not valid: line 2.";
    applyRepeatSettings({ playback: {
      repeat_rules: fullRules, related_tracks_default_file: "C:/station/related-tracks.toml",
      related_tracks_problem: problem,
    } }, els);
    expect(els.tracksFileStatus.hidden).toBe(false);
    expect(els.tracksFileStatus.textContent).toBe(problem);
    expect(els.tracksDefaultFile.textContent).toBe("C:/station/related-tracks.toml");
    els.versionsAsOneSong.click();
    els.save.click();
    await vi.waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].changes.playback.repeat_rules.versions_as_one_song).toBe(true);
    expect(requests[0].changes.playback).not.toHaveProperty("related_tracks_default_file");
    expect(requests[0].changes.playback.repeat_rules).not.toHaveProperty("related_tracks_file");
  });

  it("keeps edits across old pushes and failed saves", async () => {
    const { els, requests } = setup(true);
    type(els.artistTracks, "7");
    applySettingsDrafts({ profile: "", revision: "old", values: { playback: { repeat_rules: fullRules } } });
    expect(els.artistTracks.value).toBe("7");
    els.save.click();
    await vi.waitFor(() => expect(requests).toHaveLength(1));
    await vi.waitFor(() => expect(els.panel.querySelector(".settings-save-status").textContent)
      .toContain("settings service is busy"));
    expect(els.artistTracks.value).toBe("7");
    expect(els.save.disabled).toBe(false);
  });

  it("resets controls after authenticated state is cleared", () => {
    const { els } = setup();
    applyRepeatSettings({ playback: {
      repeat_rules: fullRules,
      related_artists_default_file: "C:/station/related-artists.toml",
      related_artists_problem: "The related artists file is not valid.",
    } }, els);
    els.artistTracks.value = "9";
    els.artistTracks.setAttribute("aria-invalid", "true");
    resetRepeatSettings(els);
    expect(els.artistTracks.value).toBe("8");
    expect(els.artistTracks.hasAttribute("aria-invalid")).toBe(false);
    expect(els.useRelatedArtists.checked).toBe(false);
    expect(els.defaultFile.textContent).toBe("index/libraries/main/related-artists.toml");
    expect(els.fileStatus.hidden).toBe(true);
  });
});
