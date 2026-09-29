import { afterEach, describe, expect, it, vi } from "vitest";

import { formatPlayedAt, historyNeedsDates } from
  "../../src/autodj/static/modules/history-format.js";
import { applyMixSettings, installMixSettings } from
  "../../src/autodj/static/modules/mix-settings.js";
import { installProfiles, profileFromSettings } from
  "../../src/autodj/static/modules/profiles.js";
import {
  installAccess, lastSeenWords, REVOKED_REASON, SIGNED_OUT_REASON, spokenCode,
} from "../../src/autodj/static/modules/devices.js";

const json = (body, status = 200) => Promise.resolve(new globalThis.Response(
  JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } },
));

const CONFIRM = `
  <dialog id="confirm-dialog"><h2 id="confirm-title"></h2><p id="confirm-message"></p>
    <button id="confirm-cancel"></button><button id="confirm-accept"></button></dialog>
  <div id="status-toast" hidden></div>`;

// The modal dialog as a browser runs it: closing puts focus back on the
// element that had it when the dialog opened, if that is still in the page.
// answer() presses the dialog's own button.
function confirmDialog() {
  const dialog = document.querySelector("#confirm-dialog");
  let opener = null;
  dialog.showModal = vi.fn(() => {
    opener = document.activeElement;
    dialog.setAttribute("open", "");
  });
  dialog.close = vi.fn((value) => {
    if (!dialog.hasAttribute("open")) return;
    if (value !== undefined) dialog.returnValue = value;
    dialog.removeAttribute("open");
    if (opener?.isConnected) opener.focus();
    dialog.dispatchEvent(new Event("close"));
  });
  return async (value) => {
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalled());
    document.getElementById(value === "confirm" ? "confirm-accept" : "confirm-cancel").click();
  };
}

// Every element that gets focus from now on, until stop().
function recordFocus() {
  const focused = [];
  const listener = (event) => focused.push(event.target);
  document.addEventListener("focusin", listener);
  return { focused, stop: () => document.removeEventListener("focusin", listener) };
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

  it("moves focus from the dialog straight to the name field when the last profile goes", async () => {
    let names = ["Only"];
    const fetchImpl = vi.fn((url, init = {}) => {
      if (init.method === "DELETE") names = [];
      return json({ profiles: names });
    });
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(1));
    const remove = els.list.querySelector('[aria-label="Delete profile Only"]');
    remove.focus();
    remove.click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));

    // NVDA read a line from the top of the page and then the Delete button
    // before the name field when focus went back to the button first (D14).
    const { focused, stop } = recordFocus();
    await answer("confirm");
    await vi.waitFor(() => expect(els.status.textContent).toBe("Deleted profile Only."));
    stop();

    expect(focused).toEqual([els.nameInput]);
    expect(els.list.textContent).toBe("No saved profiles yet.");
  });

  it("puts focus back on Delete when the confirmation is cancelled", async () => {
    const fetchImpl = vi.fn(() => json({ profiles: ["Only"] }));
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(1));
    const remove = els.list.querySelector('[aria-label="Delete profile Only"]');
    remove.focus();
    remove.click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));

    await answer("cancel");
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(document.activeElement).toBe(remove);
    expect(fetchImpl.mock.calls.some(([, init = {}]) => init.method === "DELETE")).toBe(false);
  });

  it("says why a profile could not be applied and leaves focus alone", async () => {
    const fetchImpl = vi.fn((url) => (url.endsWith("/apply")
      ? json({ detail: "Profile Late uses a preset that does not exist" }, 400)
      : json({ profiles: ["Late"] })));
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("button")).toHaveLength(2));
    const apply = els.list.querySelector('[aria-label="Apply profile Late"]');
    els.nameInput.focus();

    apply.click();

    await vi.waitFor(() => expect(els.status.textContent).toBe(
      "Could not apply profile Late: Profile Late uses a preset that does not exist",
    ));
    expect(document.activeElement).toBe(els.nameInput);
  });
});

describe("browser access", () => {
  function setup(fetchImpl) {
    document.body.innerHTML = `
      <details id="card"><summary>Browser access</summary>
        <p id="note" hidden></p>
        <div id="controls" hidden><button id="out">Sign out this browser</button>
          <button id="refresh">Refresh device list</button><ul id="list"></ul>
          <button id="pair-toggle" aria-expanded="false">Show pairing code</button>
          <div id="pair-panel" hidden><p>Pairing code <strong id="pair-code"></strong></p>
            <p id="pair-countdown" aria-live="off"></p>
            <button id="pair-copy">Copy code</button><button id="pair-refresh">Refresh code</button>
          </div></div>
        <p id="status" role="status" aria-live="polite"></p></details>${CONFIRM}
      <dialog id="device-rename-dialog"><form id="device-rename-form">
        <h2 id="device-rename-title"></h2><input id="device-rename-name">
        <p id="device-rename-error" role="status"></p>
        <button type="button" id="device-rename-cancel">Cancel</button>
        <button type="submit" id="device-rename-save">Save</button></form></dialog>`;
    vi.stubGlobal("fetch", fetchImpl);
    const $ = (id) => document.getElementById(id);
    const onSignedOut = vi.fn();
    const els = {
      card: $("card"), note: $("note"), controls: $("controls"), signOut: $("out"),
      refresh: $("refresh"), list: $("list"), status: $("status"),
      pairToggle: $("pair-toggle"), pairPanel: $("pair-panel"), pairCode: $("pair-code"),
      pairCountdown: $("pair-countdown"), pairCopy: $("pair-copy"), pairRefresh: $("pair-refresh"),
    };
    // The rename dialog as a browser runs it: closing puts focus back on
    // the element that had it when the dialog opened.
    const rename = $("device-rename-dialog");
    let opener = null;
    rename.showModal = vi.fn(() => {
      opener = document.activeElement;
      rename.setAttribute("open", "");
    });
    rename.close = vi.fn(() => {
      if (!rename.hasAttribute("open")) return;
      rename.removeAttribute("open");
      if (opener?.isConnected) opener.focus();
      rename.dispatchEvent(new Event("close"));
    });
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

  // Every non-empty text the status region takes, in order.
  function spoken(region) {
    const said = [];
    new globalThis.MutationObserver(() => {
      if (region.textContent) said.push(region.textContent);
    }).observe(region, { childList: true, characterData: true, subtree: true });
    return said;
  }

  it("says the last-seen time and the code in words a screen reader reads clearly", () => {
    const now = 1_000_000_000;
    expect(lastSeenWords(now / 1000 - 30, now)).toBe("just now");
    expect(lastSeenWords(now / 1000 - 60, now)).toBe("1 minute ago");
    expect(lastSeenWords(now / 1000 - 5 * 3600, now)).toBe("5 hours ago");
    expect(spokenCode("12345678")).toBe("1 2 3 4, 5 6 7 8");
  });

  it("renames a device, says so once and puts focus back on its Rename", async () => {
    let listed = devices;
    const fetchImpl = vi.fn((url, init = {}) => {
      if (init.method === "PATCH") {
        const { name } = JSON.parse(init.body);
        listed = [{ ...devices[0], name }, devices[1]];
        return json({ device_id: devices[0].device_id, name });
      }
      return json({ pairing: true, devices: listed });
    });
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    const said = spoken(els.status);
    const button = els.list.querySelector('[aria-label="Rename Phone"]');
    button.focus();
    button.click();
    const input = document.getElementById("device-rename-name");
    expect(document.getElementById("device-rename-title").textContent).toBe("Rename Phone");
    expect(document.activeElement).toBe(input);
    expect(input.value).toBe("Phone");

    input.value = "  Hall speaker ";
    document.getElementById("device-rename-save").click();

    await vi.waitFor(() => expect(said).toEqual(["Renamed to Hall speaker."]));
    expect(document.getElementById("device-rename-dialog").hasAttribute("open")).toBe(false);
    expect(document.activeElement).toBe(button);
    expect(button.getAttribute("aria-label")).toBe("Rename Hall speaker");
    expect(els.list.querySelector("li").textContent).toMatch(/^Hall speaker\. {2}Paired /);
    const patch = fetchImpl.mock.calls.find(([, init = {}]) => init.method === "PATCH");
    expect(JSON.parse(patch[1].body)).toEqual({ name: "Hall speaker" });
  });

  it("keeps the rename dialog open and says why when the name is refused", async () => {
    const fetchImpl = vi.fn((url, init = {}) => (init.method === "PATCH"
      ? json({ detail: "device name must contain 1 to 64 printable characters" }, 422)
      : json({ pairing: true, devices })));
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    els.list.querySelector('[aria-label="Rename Phone"]').click();
    const input = document.getElementById("device-rename-name");
    const error = document.getElementById("device-rename-error");

    input.value = "x".repeat(65);
    document.getElementById("device-rename-save").click();

    await vi.waitFor(() => expect(error.textContent)
      .toBe("The name must be 1 to 64 printable characters."));
    expect(document.getElementById("device-rename-dialog").hasAttribute("open")).toBe(true);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(document.activeElement).toBe(input);
    expect(els.status.textContent).toBe("");
  });

  it("shows the pairing code, says it once and keeps the countdown quiet", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "setInterval", "clearInterval", "Date"] });
    try {
      let code = "12345678";
      let listed = devices;
      const fetchImpl = vi.fn((url) => (url === "/api/pairing-code"
        ? json({ code, valid_seconds: 500, next_code_seconds: 200 })
        : json({ pairing: true, devices: listed })));
      const { els } = setup(fetchImpl);
      await vi.advanceTimersByTimeAsync(0);
      expect(els.list.querySelectorAll("li")).toHaveLength(2);
      const said = spoken(els.status);

      els.pairToggle.click();
      await vi.advanceTimersByTimeAsync(0);
      expect(els.pairPanel.hidden).toBe(false);
      expect(els.pairToggle.getAttribute("aria-expanded")).toBe("true");
      expect(said).toEqual(["Pairing code 1 2 3 4, 5 6 7 8, valid for 8 minutes."]);
      expect(els.pairCode.textContent).toContain("1234 5678");
      expect(els.pairCountdown.textContent).toBe("Valid for 8 minutes 20 seconds.");
      const live = '[aria-live]:not([aria-live="off"]), [role="status"], [role="alert"]';
      expect(els.pairCountdown.closest(live)).toBeNull();
      expect(els.pairCode.closest(live)).toBeNull();

      // The code rotates while shown: new digits, nothing said.
      code = "87654321";
      await vi.advanceTimersByTimeAsync(3_000);
      expect(els.pairCountdown.textContent).toBe("Valid for 8 minutes 17 seconds.");
      await vi.advanceTimersByTimeAsync(197_000);
      expect(els.pairCode.textContent).toContain("8765 4321");
      expect(said).toHaveLength(1);

      // Another device pairs while the code is shown: said once.
      listed = [...devices, { ...devices[0], device_id: "c".repeat(32), name: "Laptop" }];
      await vi.advanceTimersByTimeAsync(10_000);
      expect(els.list.querySelectorAll("li")).toHaveLength(3);
      expect(said).toEqual([
        "Pairing code 1 2 3 4, 5 6 7 8, valid for 8 minutes.",
        "New device paired: Laptop.",
      ]);

      // Refresh code says the current code again, once.
      els.pairRefresh.click();
      await vi.advanceTimersByTimeAsync(0);
      expect(said.at(-1)).toMatch(/^Pairing code 8 7 6 5, 4 3 2 1, valid for \d+ minutes\.$/);
      expect(said).toHaveLength(3);

      els.pairToggle.click();
      expect(els.pairPanel.hidden).toBe(true);
      expect(els.pairCode.textContent).toBe("");
      const calls = fetchImpl.mock.calls.length;
      await vi.advanceTimersByTimeAsync(30_000);
      expect(fetchImpl.mock.calls.length).toBe(calls);
    } finally {
      vi.useRealTimers();
    }
  });

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
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));

    els.list.querySelector('[aria-label="Revoke Tablet, this browser"]').click();
    await answer("confirm");

    await vi.waitFor(() => expect(onSignedOut).toHaveBeenCalledWith(REVOKED_REASON));
  });

  it("moves focus from the dialog straight to the next Revoke", async () => {
    let listed = devices;
    const fetchImpl = vi.fn((url, init = {}) => {
      if (init.method === "DELETE") {
        listed = devices.slice(1);
        return json({ revoked: "a".repeat(32), signed_out: false });
      }
      return json({ pairing: true, devices: listed });
    });
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    const revoke = els.list.querySelector('[aria-label="Revoke Phone"]');
    revoke.focus();
    revoke.click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));

    const { focused, stop } = recordFocus();
    await answer("confirm");
    await vi.waitFor(() => expect(els.status.textContent).toBe("Revoked Phone."));
    stop();

    expect(focused.map((element) => element.getAttribute("aria-label")))
      .toEqual(["Revoke Tablet, this browser"]);
    expect(revoke.isConnected).toBe(false);
    expect(els.list.querySelectorAll("li")).toHaveLength(1);
  });

  it("puts focus back on Revoke and says why when the revoke fails", async () => {
    const fetchImpl = vi.fn((url, init = {}) => (init.method === "DELETE"
      ? json({ detail: "Server busy" }, 503)
      : json({ pairing: true, devices })));
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    const revoke = els.list.querySelector('[aria-label="Revoke Phone"]');
    revoke.focus();
    revoke.click();

    await answer("confirm");
    await vi.waitFor(() => expect(els.status.textContent).toBe("Could not revoke Phone: Server busy"));
    expect(document.activeElement).toBe(revoke);
  });
});
