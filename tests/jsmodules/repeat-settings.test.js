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
};

describe("Repeats markup", () => {
  it("groups each rule under a legend and labels every box", () => {
    const els = installPage();
    const outer = els.save.closest("fieldset");
    expect(outer.querySelector(":scope > legend").textContent).toBe("Repeats");
    const groups = [...outer.querySelectorAll(":scope > fieldset")];
    expect(groups.map((group) => group.querySelector(":scope > legend").textContent)).toEqual([
      "Prevent the same artist from repeating within",
      "Related artists",
      "Prevent the same album from repeating within",
      "Prevent the same track from repeating within",
      "Prevent the same title from repeating within",
      "Related tracks",
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
    }
  });

  it("describes the rules once, on the Repeats group, not on each box", () => {
    const els = installPage();
    const outer = els.save.closest("fieldset");
    const [description] = describedBy(outer);
    expect(description.id).toBe("repeat-rules-desc");
    expect(description.textContent).toContain("0 turns a limit off");
    expect(description.textContent).toContain("5000");
    // Plain text before the groups too, for reading down the page.
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
    const [note, status] = describedBy(els.useRelatedArtists);
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
    const [note, status] = describedBy(els.useRelatedTracks);
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

  it("puts Count versions as one song in the same track group with its description", () => {
    const els = installPage();
    const group = els.versionsAsOneSong.closest("fieldset");
    expect(group).toBe(els.trackTracks.closest("fieldset"));
    expect(els.versionsAsOneSong.labels[0].textContent).toBe("Count versions as one song");
    expect(els.versionsAsOneSong.checked).toBe(false);
    const [note] = describedBy(els.versionsAsOneSong);
    expect(note.closest("fieldset")).toBe(group);
    expect(note.textContent).toContain("same lead artist");
    expect(note.textContent).toContain("all of its versions");
  });
});

describe("repeat settings", () => {
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
    type(els.artistTracks, "1.5");
    els.save.click();
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
