import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyRepeatSettings, installRepeatSettings, resetRepeatSettings,
} from "../../src/autodj/static/modules/repeat-settings.js";
import { invalidateAuthenticatedRequestEpoch } from
  "../../src/autodj/static/modules/api-client.js";

afterEach(() => document.body.replaceChildren());

function setup(postSettings = vi.fn().mockResolvedValue(true)) {
  document.body.innerHTML = `
    <fieldset>
      <label for="artist-tracks">Prevent the same artist from repeating within Tracks</label>
      <input id="artist-tracks" type="number" value="3">
      <label for="artist-minutes">Artist Minutes</label><input id="artist-minutes" type="number" value="0">
      <label for="album-tracks">Album Tracks</label><input id="album-tracks" type="number" value="3">
      <label for="album-minutes">Album Minutes</label><input id="album-minutes" type="number" value="0">
      <label for="track-tracks">Track Tracks</label><input id="track-tracks" type="number" value="500">
      <label for="track-minutes">Track Minutes</label><input id="track-minutes" type="number" value="0">
      <label for="title-tracks">Title Tracks</label><input id="title-tracks" type="number" value="3">
      <label for="title-minutes">Title Minutes</label><input id="title-minutes" type="number" value="0">
      <input id="related" type="checkbox"><label for="related">Use related artists</label>
      <label for="file">Related artists file</label><input id="file" value="">
      <button id="save" type="button">Save repeat settings</button>
      <span id="default-file"></span>
    </fieldset><p id="settings-status"></p><div id="status-toast" hidden></div>`;
  const $ = (id) => document.getElementById(id);
  const els = {
    artistTracks: $("artist-tracks"), artistMinutes: $("artist-minutes"),
    albumTracks: $("album-tracks"), albumMinutes: $("album-minutes"),
    trackTracks: $("track-tracks"), trackMinutes: $("track-minutes"),
    titleTracks: $("title-tracks"), titleMinutes: $("title-minutes"),
    useRelatedArtists: $("related"), relatedArtistsFile: $("file"),
    defaultFile: $("default-file"), save: $("save"),
  };
  const settingsStatus = $("settings-status");
  installRepeatSettings(els, { postSettings, settingsStatus });
  return { els, settingsStatus, postSettings };
}

const fullRules = {
  artist: { tracks: 3, minutes: 0 }, album: { tracks: 3, minutes: 0 },
  track: { tracks: 500, minutes: 0 }, title: { tracks: 3, minutes: 0 },
  use_related_artists: true, related_artists_file: "config/related-artists.txt",
};

describe("repeat settings", () => {
  it("applies server values, preserves typed edits across pushes, and submits the full rules object", async () => {
    const { els, postSettings } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules, related_artists_default_file: "C:/station/related-artists.toml" } }, els);
    expect(els.trackTracks.value).toBe("500");
    expect(els.relatedArtistsFile.value).toBe("config/related-artists.txt");
    expect(els.defaultFile.textContent).toBe("C:/station/related-artists.toml");
    expect(document.querySelector("label[for=artist-tracks]").textContent)
      .toContain("Prevent the same artist");

    els.artistTracks.value = "7";
    els.artistTracks.dispatchEvent(new Event("input"));
    applyRepeatSettings({ playback: { repeat_rules: { ...fullRules, artist: { tracks: 9, minutes: 0 } } } }, els);
    expect(els.artistTracks.value).toBe("7");
    els.save.click();
    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledWith(
      "/api/playback-settings", { repeat_rules: { ...fullRules, artist: { tracks: 7, minutes: 0 } } }, els.save,
    ));
  });

  it("rejects invalid tracks and minutes without posting, and restores the last saved values", async () => {
    const { els, postSettings } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    els.artistTracks.value = "1.5";
    els.artistTracks.dispatchEvent(new Event("input"));
    els.save.click();
    expect(els.artistTracks.value).toBe("3");
    expect(postSettings).not.toHaveBeenCalled();
    expect(els.save.textContent).toBe("Save repeat settings");

    els.artistMinutes.value = "10080.1";
    els.artistMinutes.dispatchEvent(new Event("input"));
    els.save.click();
    expect(els.artistMinutes.value).toBe("0");
    expect(postSettings).not.toHaveBeenCalled();
  });

  it("resets controls after authenticated state is cleared", () => {
    const { els } = setup();
    applyRepeatSettings({ playback: { repeat_rules: fullRules, related_artists_default_file: "C:/station/related-artists.toml" } }, els);
    els.artistTracks.value = "9";
    resetRepeatSettings(els);
    expect(els.artistTracks.value).toBe("3");
    expect(els.useRelatedArtists.checked).toBe(false);
    expect(els.relatedArtistsFile.value).toBe("");
    expect(els.defaultFile.textContent).toBe("");
  });

  it("does not let a save from an old auth epoch overwrite new-session edits", async () => {
    let finishSave;
    const postSettings = vi.fn(() => new Promise((resolve) => { finishSave = resolve; }));
    const { els } = setup(postSettings);
    applyRepeatSettings({ playback: { repeat_rules: fullRules } }, els);
    els.artistTracks.value = "7";
    els.artistTracks.dispatchEvent(new Event("input"));
    els.save.click();

    invalidateAuthenticatedRequestEpoch();
    resetRepeatSettings(els);
    const newSessionRules = { ...fullRules, artist: { tracks: 10, minutes: 0 } };
    applyRepeatSettings({ playback: { repeat_rules: newSessionRules } }, els);
    els.artistTracks.value = "13";
    els.artistTracks.dispatchEvent(new Event("input"));

    finishSave(false);
    await vi.waitFor(() => expect(els.artistTracks.value).toBe("13"));
    applyRepeatSettings({ playback: { repeat_rules: newSessionRules } }, els);
    expect(els.artistTracks.value).toBe("13");
  });
});
