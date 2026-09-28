import { afterEach, describe, expect, it, vi } from "vitest";

import { formatPlayedAt, historyNeedsDates } from
  "../../src/autodj/static/modules/history-format.js";
import { applyMixSettings, installMixSettings } from
  "../../src/autodj/static/modules/mix-settings.js";
import { installProfiles, profileFromSettings } from
  "../../src/autodj/static/modules/profiles.js";
import { installAccess, REVOKED_REASON, SIGNED_OUT_REASON } from
  "../../src/autodj/static/modules/devices.js";

const json = (body, status = 200) => Promise.resolve(new globalThis.Response(
  JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } },
));

const CONFIRM = `
  <dialog id="confirm-dialog"><h2 id="confirm-title"></h2><p id="confirm-message"></p>
    <button id="confirm-cancel"></button><button id="confirm-accept"></button></dialog>
  <div id="status-toast" hidden></div>`;

function confirmDialog() {
  const dialog = document.querySelector("#confirm-dialog");
  dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
  return async (value) => {
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalled());
    dialog.returnValue = value;
    dialog.removeAttribute("open");
    dialog.dispatchEvent(new Event("close"));
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("history dates", () => {
  const now = new Date(2026, 8, 27, 12, 0, 0);

  it("adds the date only when the rows are not all from today", () => {
    const today = { played_at: new Date(2026, 8, 27, 9, 0, 0).toISOString() };
    const yesterday = { played_at: new Date(2026, 8, 26, 23, 0, 0).toISOString() };
    expect(historyNeedsDates([today, today], now)).toBe(false);
    expect(historyNeedsDates([today, yesterday], now)).toBe(true);
    expect(formatPlayedAt(yesterday.played_at, true, "en-GB")).toContain("2026");
    expect(formatPlayedAt(yesterday.played_at, false, "en-GB")).not.toContain("2026");
  });
});

describe("mix settings", () => {
  function setup(postSettings) {
    document.body.innerHTML = `
      <input type="number" id="no-repeat" value="500">
      <input type="number" id="wet" value="100">
      <p id="settings-status"></p><div id="status-toast" hidden></div>`;
    const els = {
      pbNoRepeat: document.querySelector("#no-repeat"),
      txWetMix: document.querySelector("#wet"),
    };
    const settingsStatus = document.querySelector("#settings-status");
    installMixSettings(els, { postSettings, settingsStatus });
    applyMixSettings({ playback: { no_repeat_window: 200, transition_wet_mix: 0.5 } }, els);
    return { els, settingsStatus };
  }

  it("refuses a value the server would reject, says why and restores the saved one", async () => {
    const postSettings = vi.fn().mockResolvedValue(true);
    const { els, settingsStatus } = setup(postSettings);

    els.pbNoRepeat.value = "2.5";
    els.pbNoRepeat.dispatchEvent(new Event("change"));

    await vi.waitFor(() => expect(settingsStatus.textContent).toBe(
      "Could not save Tracks before a song can repeat: enter a whole number from 0 to 100000.  It is still 200.",
    ));
    expect(els.pbNoRepeat.value).toBe("200");
    expect(postSettings).not.toHaveBeenCalled();
  });

  it("sends the effect level as a fraction", async () => {
    const postSettings = vi.fn().mockResolvedValue(true);
    const { els } = setup(postSettings);
    expect(els.txWetMix.value).toBe("50");

    els.txWetMix.value = "75";
    els.txWetMix.dispatchEvent(new Event("change"));

    await vi.waitFor(() => expect(postSettings).toHaveBeenCalledWith(
      "/api/playback-settings", { transition_wet_mix: 0.75 }, els.txWetMix,
    ));
  });
});

describe("profiles", () => {
  function setup(fetchImpl) {
    document.body.innerHTML = `
      <details id="card"><summary>Profiles</summary>
        <input id="name"><button id="save">Save profile</button>
        <ul id="list"></ul><p id="status"></p></details>${CONFIRM}`;
    vi.stubGlobal("fetch", fetchImpl);
    const $ = (id) => document.getElementById(id);
    const els = {
      card: $("card"), nameInput: $("name"), saveButton: $("save"),
      list: $("list"), status: $("status"),
    };
    const settings = {
      preset: "chill", bpm_range: { lo: 90, hi: 120 }, djmix: { harmonic_mode: "strict" },
      playback: { crossfade_seconds: 4, liners_pick_mode: "random", library_size: 9 },
    };
    installProfiles(els, { getSettings: () => settings });
    const answer = confirmDialog();
    els.card.open = true;
    els.card.dispatchEvent(new Event("toggle"));
    return { els, answer };
  }

  it("saves only the fields a profile holds", () => {
    const body = profileFromSettings("Late", {
      preset: null, bpm_range: {}, djmix: {}, playback: { library_size: 9, crossfade_seconds: 2 },
    });
    expect(body).not.toHaveProperty("library_size");
    expect(body).toMatchObject({ name: "Late", crossfade_seconds: 2, preset: null });
  });

  it("asks before deleting and then moves focus to the next row", async () => {
    let names = ["Early", "Late"];
    const fetchImpl = vi.fn((url, init = {}) => {
      if (init.method === "DELETE") names = names.filter((n) => !url.endsWith(n));
      return json({ profiles: names });
    });
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));

    els.list.querySelector('[aria-label="Delete profile Early"]').click();
    await answer("confirm");

    await vi.waitFor(() => expect(els.status.textContent).toBe("Deleted profile Early."));
    expect(document.activeElement.getAttribute("aria-label")).toBe("Delete profile Late");
  });

  it("says why a profile could not be applied and keeps focus on Apply", async () => {
    const fetchImpl = vi.fn((url) => (url.endsWith("/apply")
      ? json({ detail: "Profile Late uses a preset that does not exist" }, 400)
      : json({ profiles: ["Late"] })));
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("button")).toHaveLength(2));
    const apply = els.list.querySelector('[aria-label="Apply profile Late"]');

    apply.click();

    await vi.waitFor(() => expect(els.status.textContent).toBe(
      "Could not apply profile Late: Profile Late uses a preset that does not exist",
    ));
    expect(document.activeElement).toBe(apply);
  });
});

describe("browser access", () => {
  function setup(fetchImpl) {
    document.body.innerHTML = `
      <details id="card"><summary>Browser access</summary>
        <p id="note" hidden></p>
        <div id="controls" hidden><button id="out">Sign out this browser</button>
          <button id="refresh">Refresh device list</button><ul id="list"></ul></div>
        <p id="status"></p></details>${CONFIRM}`;
    vi.stubGlobal("fetch", fetchImpl);
    const $ = (id) => document.getElementById(id);
    const onSignedOut = vi.fn();
    const els = {
      card: $("card"), note: $("note"), controls: $("controls"), signOut: $("out"),
      refresh: $("refresh"), list: $("list"), status: $("status"),
    };
    installAccess(els, { onSignedOut });
    const answer = confirmDialog();
    els.card.open = true;
    els.card.dispatchEvent(new Event("toggle"));
    return { els, onSignedOut, answer };
  }

  const devices = [
    { device_id: "a".repeat(32), name: "Phone", paired_at: 1, last_seen_at: 2, current: false },
    { device_id: "b".repeat(32), name: "Tablet", paired_at: 1, last_seen_at: 2, current: true },
  ];

  it("hides the controls when the server does not pair browsers", async () => {
    const { els } = setup(vi.fn(() => json({ pairing: false, devices: [] })));
    await vi.waitFor(() => expect(els.note.hidden).toBe(false));
    expect(els.controls.hidden).toBe(true);
  });

  it("signs this browser out after confirmation", async () => {
    const fetchImpl = vi.fn((url) => json(url === "/api/logout"
      ? { authenticated: false } : { pairing: true, devices }));
    const { els, onSignedOut, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.controls.hidden).toBe(false));

    els.signOut.click();
    await answer("confirm");

    await vi.waitFor(() => expect(onSignedOut).toHaveBeenCalledWith(SIGNED_OUT_REASON));
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/logout")).toBe(true);
  });

  it("signs this browser out when its own entry is revoked", async () => {
    const fetchImpl = vi.fn((url, init = {}) => json(init.method === "DELETE"
      ? { revoked: "b".repeat(32), signed_out: true } : { pairing: true, devices }));
    const { els, onSignedOut, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("button")).toHaveLength(2));

    els.list.querySelector('[aria-label="Revoke Tablet, this browser"]').click();
    await answer("confirm");

    await vi.waitFor(() => expect(onSignedOut).toHaveBeenCalledWith(REVOKED_REASON));
  });
});
