import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyRepeatSettings, installRepeatSettings, resetRepeatSettings,
} from "../../src/autodj/static/modules/repeat-settings.js";
import { invalidateAuthenticatedRequestEpoch } from
  "../../src/autodj/static/modules/api-client.js";

const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");

afterEach(() => {
  vi.restoreAllMocks();
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
    save: $("repeat-save"),
  };
}

function setup(postSettings = vi.fn().mockResolvedValue(true)) {
  const els = installPage();
  const settingsStatus = document.getElementById("settings-status");
  installRepeatSettings(els, { postSettings, settingsStatus });
  return { els, settingsStatus, postSettings };
}

// A postSettings whose request fails with *status* and *message*, the way
// settings-panel.js hands a failure to onError.
function failingPost(status, message) {
  return vi.fn(async (_url, _body, _control, { onError } = {}) => {
    const error = new Error(message);
    error.status = status;
    onError?.(error);
    return false;
  });
}

function type(control, value) {
  control.value = value;
  control.dispatchEvent(new Event("input"));
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
    const outer = els.save.closest("fieldset");
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
    const outer = els.save.closest("fieldset");
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
    expect(note.textContent).toContain("related-artists.toml in the index folder");
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
    expect(note.textContent).toContain("related-tracks.toml in the index folder");
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
  it("applies and saves the daypart and quarter-hour rules, hours in order", async () => {
    const { els, postSettings } = setup();
    const timed = { ...fullRules, daypart: { days: 2, start_hours: [0, 5, 9] },
      hourpart: { plays: 1 } };
    applyRepeatSettings({ playback: { repeat_rules: timed } }, els);
    expect(els.daypartDays.value).toBe("2");
    expect(els.daypartHours.value).toBe("0, 5, 9");
    expect(els.hourpartPlays.value).toBe("1");

    type(els.daypartHours, " 19 ,6,0 ");
    type(els.hourpartPlays, "3");
    els.save.click();
    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledOnce());
    expect(postSettings.mock.calls[0][1]).toEqual({ repeat_rules: {
      ...timed, daypart: { days: 2, start_hours: [0, 6, 19] }, hourpart: { plays: 3 },
    } });
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
  ])("refuses %s %j, marks and focuses it, and names the rule", async (name, value, message) => {
    const { els, postSettings, settingsStatus } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    type(els[name], value);
    expect(els[name].getAttribute("aria-invalid")).toBe(null);
    els.save.click();
    expect(els[name].getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(els[name]);
    await vi.waitFor(() => expect(settingsStatus.textContent).toBe(message));
    expect(postSettings).not.toHaveBeenCalled();
    // The message is also on the page, under the field and linked to it.
    expect(els.error.hidden).toBe(false);
    expect(els.error.textContent).toBe(message);
    expect(els.error.previousElementSibling).toBe(els[name].closest(".settings-row"));
    expect(describedBy(els[name])).toContain(els.error);
    expect(els.error.hasAttribute("aria-live")).toBe(false);
    type(els[name], name === "daypartHours" ? "0, 12" : "1");
    expect(els[name].hasAttribute("aria-invalid")).toBe(false);
    expect(els.error.hidden).toBe(true);
    expect(describedBy(els[name])).not.toContain(els.error);
    if (name === "daypartHours") {
      // Its own format help stays linked.
      expect(describedBy(els[name]).map((node) => node.id)).toEqual(["repeat-daypart-hours-desc"]);
    } else {
      expect(els[name].hasAttribute("aria-describedby")).toBe(false);
    }
  });

  it("moves the field error to the next refused box and clears it on a good save", async () => {
    const { els, postSettings } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    type(els.artistTracks, "5001");
    els.save.click();
    expect(describedBy(els.artistTracks)).toContain(els.error);
    expect(els.error.textContent).toBe("Same artist tracks must be a whole number from 0 to 5000.");
    type(els.artistTracks, "8");
    type(els.trackMinutes, "-1");
    els.save.click();
    expect(els.artistTracks.hasAttribute("aria-describedby")).toBe(false);
    expect(describedBy(els.trackMinutes)).toContain(els.error);
    expect(els.error.previousElementSibling).toBe(els.trackMinutes.closest(".settings-row"));
    // Editing another box leaves the error with its own field.
    type(els.albumTracks, "3");
    expect(els.error.hidden).toBe(false);
    els.trackMinutes.value = "0";
    els.save.click();
    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(els.error.hidden).toBe(true));
    expect(els.trackMinutes.hasAttribute("aria-describedby")).toBe(false);
  });

  it("applies server values, keeps typed edits across pushes, and saves no file path", async () => {
    const { els, postSettings } = setup();
    applyRepeatSettings({ playback: {
      repeat_rules: fullRules,
      related_artists_default_file: "C:/station/related-artists.toml",
      related_artists_problem: null,
    } }, els);
    expect(els.trackTracks.value).toBe("50");
    expect(els.useRelatedArtists.checked).toBe(true);
    expect(els.defaultFile.textContent).toBe("C:/station/related-artists.toml");
    expect(els.fileStatus.hidden).toBe(true);

    type(els.artistTracks, "7");
    applyRepeatSettings({ playback: { repeat_rules: { ...fullRules, artist: { tracks: 9, minutes: 0 } } } }, els);
    expect(els.artistTracks.value).toBe("7");
    els.save.click();
    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledOnce());
    const [url, body, control] = postSettings.mock.calls[0];
    expect(url).toBe("/api/playback-settings");
    expect(body).toEqual({ repeat_rules: { ...fullRules, artist: { tracks: 7, minutes: 0 } } });
    expect(Object.keys(body.repeat_rules)).not.toContain("related_artists_file");
    expect(control).toBe(els.save);
  });

  it("applies and saves the related artist and related track limits", async () => {
    const { els, postSettings } = setup();
    applyRepeatSettings({ playback: {
      repeat_rules: { ...fullRules, use_related_tracks: true,
        related_track: { tracks: 6, minutes: 45 } },
      related_tracks_default_file: "C:/station/related-tracks.toml",
      related_tracks_problem: null,
    } }, els);
    expect(els.useRelatedTracks.checked).toBe(true);
    expect(els.relatedTrackTracks.value).toBe("6");
    expect(els.relatedTrackMinutes.value).toBe("45");
    expect(els.tracksDefaultFile.textContent).toBe("C:/station/related-tracks.toml");

    type(els.relatedArtistMinutes, "90");
    els.save.click();
    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledOnce());
    expect(postSettings.mock.calls[0][1]).toEqual({ repeat_rules: {
      ...fullRules, use_related_tracks: true, related_track: { tracks: 6, minutes: 45 },
      related_artist: { tracks: 8, minutes: 90 },
    } });
  });

  it("applies and saves Count versions as one song", async () => {
    const { els, postSettings } = setup();
    applyRepeatSettings({ playback: { repeat_rules: { ...fullRules, versions_as_one_song: true } } }, els);
    expect(els.versionsAsOneSong.checked).toBe(true);
    els.versionsAsOneSong.click();
    els.save.click();
    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledOnce());
    expect(postSettings.mock.calls[0][1]).toEqual({ repeat_rules: fullRules });
    // A push that still holds the old value does not undo the change on screen.
    applyRepeatSettings({ playback: { repeat_rules: { ...fullRules, versions_as_one_song: true } } }, els);
    expect(els.versionsAsOneSong.checked).toBe(false);
  });

  it("names the related rule in an invalid limit's message", async () => {
    const { els, postSettings, settingsStatus } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    type(els.relatedTrackTracks, "5001");
    els.save.click();
    expect(document.activeElement).toBe(els.relatedTrackTracks);
    await vi.waitFor(() => expect(settingsStatus.textContent)
      .toBe("Related tracks tracks must be a whole number from 0 to 5000."));
    expect(postSettings).not.toHaveBeenCalled();
  });

  it("shows a related tracks problem found at startup in its own status line", () => {
    const { els } = setup();
    const problem = "The related tracks file is not valid: line 2. Related tracks are off "
      + "until the file is fixed and repeat settings are saved.";
    applyRepeatSettings({ playback: { repeat_rules: fullRules, related_tracks_problem: problem } }, els);
    expect(els.tracksFileStatus.hidden).toBe(false);
    expect(els.tracksFileStatus.textContent).toBe(problem);
    expect(els.fileStatus.hidden).toBe(true);
    applyRepeatSettings({ playback: { repeat_rules: fullRules, related_tracks_problem: null } }, els);
    expect(els.tracksFileStatus.hidden).toBe(true);
  });

  it("focuses Use related tracks when the server refuses the related tracks file", async () => {
    const message = "The related tracks file is not valid.";
    const { els, settingsStatus } = setup(failingPost(400, message));
    applyRepeatSettings({ playback: { repeat_rules: { ...fullRules, use_related_tracks: true } } }, els);
    els.save.click();
    await vi.waitFor(() => expect(els.useRelatedTracks.getAttribute("aria-invalid")).toBe("true"));
    expect(document.activeElement).toBe(els.useRelatedTracks);
    expect(els.useRelatedArtists.hasAttribute("aria-invalid")).toBe(false);
    expect(els.tracksFileStatus.textContent).toBe(message);
    expect(els.fileStatus.hidden).toBe(true);
    await vi.waitFor(() => expect(settingsStatus.textContent).toBe(
      `Use related tracks: ${message} Fix the file or turn this off, then save again.`));
  });

  it("shows a related artists problem found at startup in the status line", () => {
    const { els } = setup();
    const problem = "The related artists file is not valid: line 3. Related artists are off "
      + "until the file is fixed and repeat settings are saved.";
    applyRepeatSettings({ playback: { repeat_rules: fullRules, related_artists_problem: problem } }, els);
    expect(els.fileStatus.hidden).toBe(false);
    expect(els.fileStatus.textContent).toBe(problem);
    expect(describedBy(els.useRelatedArtists)).toContain(els.fileStatus);
    applyRepeatSettings({ playback: { repeat_rules: fullRules, related_artists_problem: null } }, els);
    expect(els.fileStatus.hidden).toBe(true);
    expect(els.fileStatus.textContent).toBe("");
  });

  it("keeps invalid edits, marks and focuses the field, names its rule, and clears when fixed", async () => {
    const { els, postSettings, settingsStatus } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    // The Repeats card is closed by default; focusing a refused box opens it.
    const card = els.save.closest("details");
    expect(card.open).toBe(false);
    type(els.artistTracks, "1.5");
    els.save.click();
    expect(card.open).toBe(true);
    expect(els.artistTracks.value).toBe("1.5");
    expect(els.artistTracks.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(els.artistTracks);
    await vi.waitFor(() => expect(settingsStatus.textContent)
      .toBe("Same artist tracks must be a whole number from 0 to 5000."));
    expect(postSettings).not.toHaveBeenCalled();

    type(els.artistTracks, "5001");
    expect(els.artistTracks.getAttribute("aria-invalid")).toBe("true");
    type(els.artistTracks, "8");
    expect(els.artistTracks.hasAttribute("aria-invalid")).toBe(false);

    type(els.trackMinutes, "10080.1");
    els.save.click();
    expect(els.trackMinutes.value).toBe("10080.1");
    expect(els.trackMinutes.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(els.trackMinutes);
    await vi.waitFor(() => expect(settingsStatus.textContent)
      .toBe("Same track minutes must be a number from 0 to 10080."));

    type(els.trackMinutes, "0");
    type(els.titleTracks, "");
    els.save.click();
    expect(els.titleTracks.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(els.titleTracks);
    // The track rule allows more than the others.
    type(els.titleTracks, "0");
    type(els.trackTracks, "100000");
    els.save.click();
    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledOnce());
  });

  it("announces a successful save and clears earlier errors", async () => {
    const { els, settingsStatus } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    els.useRelatedArtists.setAttribute("aria-invalid", "true");
    els.fileStatus.textContent = "The related artists file is not valid: line 3.";
    els.fileStatus.hidden = false;
    els.save.focus();
    els.save.click();
    await vi.waitFor(() => expect(settingsStatus.textContent).toBe("Repeat settings saved."));
    expect(els.useRelatedArtists.hasAttribute("aria-invalid")).toBe(false);
    expect(els.fileStatus.hidden).toBe(true);
    expect(document.activeElement).toBe(els.save);
  });

  it("keeps edits and focuses Use related artists when the server refuses the file", async () => {
    const message = "The related artists file is not valid: line 3.";
    const { els, settingsStatus } = setup(failingPost(400, message));
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    type(els.artistTracks, "7");
    els.save.click();
    await vi.waitFor(() => expect(els.useRelatedArtists.getAttribute("aria-invalid")).toBe("true"));
    expect(document.activeElement).toBe(els.useRelatedArtists);
    expect(els.artistTracks.value).toBe("7");
    expect(els.useRelatedArtists.checked).toBe(true);
    expect(els.fileStatus.hidden).toBe(false);
    expect(els.fileStatus.textContent).toBe(message);
    await vi.waitFor(() => expect(settingsStatus.textContent).toBe(
      `Use related artists: ${message} Fix the file or turn this off, then save again.`));

    // A push of the old rules does not undo the edits.
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    expect(els.artistTracks.value).toBe("7");
    // Turning it off clears the mark.
    els.useRelatedArtists.click();
    expect(els.useRelatedArtists.hasAttribute("aria-invalid")).toBe(false);
  });

  it("keeps edits and focus on Save when the save fails for another reason", async () => {
    const { els, settingsStatus } = setup(failingPost(503, "The server is busy."));
    applyRepeatSettings({ playback: { repeat_rules: { ...fullRules, use_related_artists: false } } }, els);
    type(els.albumTracks, "4");
    els.save.focus();
    els.save.click();
    await vi.waitFor(() => expect(settingsStatus.textContent).toBe(
      "Repeat settings were not saved. The server is busy. Your changes are still in the form."));
    expect(els.albumTracks.value).toBe("4");
    expect(document.activeElement).toBe(els.save);
    expect(document.querySelectorAll("[aria-invalid]")).toHaveLength(0);
  });

  it("holds old server pushes while saving and after, until the save's echo arrives", async () => {
    let finishSave;
    const postSettings = vi.fn(() => new Promise((resolve) => { finishSave = resolve; }));
    const { els, settingsStatus } = setup(postSettings);
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    type(els.artistTracks, "7");
    els.save.click();
    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledOnce());
    // A push that left the server before the save arrived.
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    expect(els.artistTracks.value).toBe("7");

    finishSave(true);
    await vi.waitFor(() => expect(settingsStatus.textContent).toBe("Repeat settings saved."));
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    expect(els.artistTracks.value).toBe("7");

    // The echo, numbers dumped the way the server sends them.
    const saved = { ...fullRules, artist: { tracks: 7, minutes: 0.0 } };
    applyRepeatSettings({ playback: { repeat_rules: saved } }, els);
    expect(els.echo).toBeNull();
    // Later pushes apply again.
    applyRepeatSettings({ playback: { repeat_rules: { ...saved, title: { tracks: 2, minutes: 0 } } } }, els);
    expect(els.titleTracks.value).toBe("2");
  });

  it("stops holding pushes when the echo never comes", async () => {
    const { els, settingsStatus } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    type(els.artistTracks, "7");
    els.save.click();
    await vi.waitFor(() => expect(settingsStatus.textContent).toBe("Repeat settings saved."));
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now + 5001);
    applyRepeatSettings({ playback: { repeat_rules: { ...fullRules, artist: { tracks: 6, minutes: 0 } } } }, els);
    expect(els.artistTracks.value).toBe("6");
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
    expect(els.defaultFile.textContent).toBe("index/<name>/related-artists.toml");
    expect(els.fileStatus.hidden).toBe(true);
  });

  it("does not let a save from an old auth epoch overwrite new-session edits", async () => {
    let finishSave;
    const postSettings = vi.fn(() => new Promise((resolve) => { finishSave = resolve; }));
    const { els } = setup(postSettings);
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    type(els.artistTracks, "7");
    els.save.click();

    invalidateAuthenticatedRequestEpoch();
    resetRepeatSettings(els);
    const newSessionRules = { ...fullRules, artist: { tracks: 10, minutes: 0 } };
    applyRepeatSettings({ playback: { repeat_rules: newSessionRules } }, els);
    type(els.artistTracks, "13");

    finishSave(false);
    await vi.waitFor(() => expect(els.artistTracks.value).toBe("13"));
    applyRepeatSettings({ playback: { repeat_rules: newSessionRules } }, els);
    expect(els.artistTracks.value).toBe("13");
  });
});
