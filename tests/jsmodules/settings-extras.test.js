import { afterEach, describe, expect, it, vi } from "vitest";

import { formatPlayedAt } from "../../src/autodj/static/modules/history-format.js";
import { applyMixSettings, installMixSettings, resetMixSettings } from
  "../../src/autodj/static/modules/mix-settings.js";
import { applySettingsDrafts, resetSettingsDrafts, saveSettingsPanel, setSettingsProfile } from
  "../../src/autodj/static/modules/settings-drafts.js";
import { installProfiles } from
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

// The confirmation as a browser runs it: closing puts focus back on the
// element that had it when the dialog opened, if that is still in the page.
// answer() presses the dialog's own button.
function confirmDialog() {
  const dialog = document.querySelector("#confirm-dialog");
  let opener = null;
  dialog.show = vi.fn(() => {
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
    await vi.waitFor(() => expect(dialog.show).toHaveBeenCalled());
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
  resetSettingsDrafts(document);
  document.body.replaceChildren();
});

describe("history dates", () => {
  it("formats a play as a time or a date and time for last-played views", () => {
    const yesterday = new Date(2026, 8, 26, 23, 0, 0).toISOString();
    expect(formatPlayedAt(yesterday, true, "en-GB")).toContain("2026");
    expect(formatPlayedAt(yesterday, false, "en-GB")).not.toContain("2026");
  });
});

describe("mix settings", () => {
  function setup(initial, fail = false) {
    const panel = document.createElement("div");
    panel.id = "spanel-djmix";
    panel.innerHTML = `<input type="number" id="wet"><input type="number" id="stretch">
      <input type="number" id="crossfade"><input type="number" id="temperature">
      <p id="settings-status"></p>`;
    document.body.append(panel);
    const els = {
      txWetMix: panel.querySelector("#wet"), djMaxStretch: panel.querySelector("#stretch"),
      pbCrossfade: panel.querySelector("#crossfade"), pbPickTemperature: panel.querySelector("#temperature"),
    };
    const settingsStatus = panel.querySelector("#settings-status");
    const requests = [];
    vi.stubGlobal("fetch", vi.fn(async (_url, options = {}) => {
      const body = JSON.parse(options.body);
      requests.push(body);
      if (fail) return new globalThis.Response(JSON.stringify({ detail: "The settings service is busy." }), {
        status: 503, headers: { "Content-Type": "application/json" },
      });
      const values = globalThis.structuredClone(initial);
      for (const [group, changes] of Object.entries(body.changes)) values[group] = { ...values[group], ...changes };
      return new globalThis.Response(JSON.stringify({ revision: "two", settings: { revision: "two", values },
        applied: Object.keys(body.changes), failed: {} }), { headers: { "Content-Type": "application/json" } });
    }));
    installMixSettings(els, { settingsStatus });
    applySettingsDrafts({ profile: "", revision: "one", values: initial });
    return { panel, els, settingsStatus, requests };
  }

  it("stages mix fields and saves only changed fields, retaining percent storage", async () => {
    const initial = { playback: { crossfade_seconds: 4, transition_wet_mix: 0.5 },
      djmix: { beatmatch_max_stretch: 0.125 } };
    const { panel, els, requests } = setup(initial);
    expect(els.txWetMix.value).toBe("50");
    expect(els.djMaxStretch.value).toBe("12.5");
    els.txWetMix.value = "75";
    els.djMaxStretch.value = "6";
    els.txWetMix.dispatchEvent(new Event("input", { bubbles: true }));
    els.djMaxStretch.dispatchEvent(new Event("change", { bubbles: true }));
    expect(requests).toHaveLength(0);
    await saveSettingsPanel(panel);
    expect(requests).toEqual([{ profile: "", revision: "one", changes: {
      playback: { transition_wet_mix: 0.75 }, djmix: { beatmatch_max_stretch: 0.06 },
    } }]);
    expect(els.pbCrossfade.value).toBe("4");
  });

  it("keeps an invalid mix value and makes no request until corrected", async () => {
    const { panel, els, requests, settingsStatus } = setup({ playback: { crossfade_seconds: 4, pick_temperature: 1 } });
    els.pbCrossfade.value = "99";
    els.pbCrossfade.dispatchEvent(new Event("input", { bubbles: true }));
    expect(await saveSettingsPanel(panel)).toBe(false);
    expect(els.pbCrossfade.value).toBe("99");
    expect(els.pbCrossfade.getAttribute("aria-invalid")).toBe("true");
    await vi.waitFor(() => expect(settingsStatus.textContent).toContain("0 to 20"));
    expect(requests).toHaveLength(0);
  });

  it("retains staged edits after a failed settings patch", async () => {
    const { panel, els, requests } = setup({ playback: { pick_temperature: 1 } }, true);
    els.pbPickTemperature.value = "5";
    els.pbPickTemperature.dispatchEvent(new Event("input", { bubbles: true }));
    expect(await saveSettingsPanel(panel)).toBe(false);
    expect(requests[0].changes).toEqual({ playback: { pick_temperature: 5 } });
    expect(els.pbPickTemperature.value).toBe("5");
    await vi.waitFor(() => expect(panel.querySelector(".settings-save-status").textContent)
      .toContain("The settings service is busy."));
  });

  it("saves changed sound controls through the tab bar and retains validation", async () => {
    const panel = document.createElement("div");
    panel.id = "spanel-sound";
    panel.innerHTML = `
      <select id="mode"><option>off</option><option>replaygain</option><option>smooth</option><option>stereotool</option></select>
      <input type="number" id="lift" step="any" value="4"><input type="number" id="target" step="any" value="-14">
      <input type="number" id="clip" step="any" value="1"><input type="number" id="trim" step="any" value="0">
      <input type="number" id="boost" step="any" value="7"><input type="number" id="speed" step="any" value="0">
      <select id="preset"><option value="included">Included</option><option value="Night.sts">Night</option></select>
      <input type="checkbox" id="prefetch" checked><input type="checkbox" id="silence" checked>
      <p id="settings-status"></p>`;
    document.body.append(panel);
    const els = {
      pbLoudness: document.querySelector("#mode"),
      pbIntroLift: document.querySelector("#lift"),
      pbRgTarget: document.querySelector("#target"),
      pbRgClip: document.querySelector("#clip"),
      pbStereoTrim: document.querySelector("#trim"),
      pbStereoAgcDrive: document.querySelector("#boost"),
      pbMusicSpeed: document.querySelector("#speed"),
      stereoPreset: document.querySelector("#preset"),
      pbPrefetch: document.querySelector("#prefetch"),
      pbSilenceTrigger: document.querySelector("#silence"),
    };
    const initial = { playback: {
      loudness_processing: "stereotool", intro_lift_max_db: 4,
      replaygain_target_db: -14, replaygain_max_clip_safe_gain: 1,
      stereotool_input_trim_db: 0, stereotool_agc_drive_db: 7,
      music_speed_percent: 0, stereotool_preset: "included",
      prefetch_next_track: true, silence_trigger_crossfade: true,
    } };
    const requests = [];
    vi.stubGlobal("fetch", vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body);
      requests.push(body);
      const values = globalThis.structuredClone(initial);
      values.playback = { ...values.playback, ...body.changes.playback };
      return new globalThis.Response(JSON.stringify({ revision: "two", settings: { revision: "two", values },
        applied: ["playback"], failed: {} }), { headers: { "Content-Type": "application/json" } });
    }));
    installMixSettings(els, { settingsStatus: panel.querySelector("#settings-status") });
    applySettingsDrafts({ profile: "", revision: "one", values: initial });
    els.pbLoudness.value = "replaygain";
    els.pbIntroLift.value = "5";
    els.pbRgTarget.value = "-12";
    els.pbRgClip.value = "1.2";
    els.pbStereoTrim.value = "-2";
    els.pbStereoAgcDrive.value = "5.5";
    els.pbMusicSpeed.value = "2.5";
    els.stereoPreset.value = "Night.sts";
    els.pbPrefetch.checked = false;
    els.pbSilenceTrigger.checked = false;
    for (const control of Object.values(els)) control.dispatchEvent(new Event("input", { bubbles: true }));
    expect(requests).toHaveLength(0);

    els.pbStereoAgcDrive.value = "12.1";
    els.pbStereoAgcDrive.dispatchEvent(new Event("input", { bubbles: true }));
    expect(await saveSettingsPanel(panel)).toBe(false);
    expect(els.pbStereoAgcDrive.getAttribute("aria-invalid")).toBe("true");
    expect(requests).toHaveLength(0);
    els.pbStereoAgcDrive.value = "5.5";
    els.pbStereoAgcDrive.dispatchEvent(new Event("input", { bubbles: true }));

    await saveSettingsPanel(panel);
    expect(requests).toEqual([{ profile: "", revision: "one", changes: { playback: {
      loudness_processing: "replaygain", intro_lift_max_db: 5,
      replaygain_target_db: -12, replaygain_max_clip_safe_gain: 1.2,
      stereotool_input_trim_db: -2, stereotool_agc_drive_db: 5.5,
      music_speed_percent: 2.5, stereotool_preset: "Night.sts",
      prefetch_next_track: false, silence_trigger_crossfade: false,
    } } }]);
    applyMixSettings({ playback: initial.playback }, els);
    expect(els.pbStereoAgcDrive.value).toBe("5.5");
    expect(els.pbLoudness.value).toBe("replaygain");

    expect(els.pbStereoAgcDrive.value).toBe("5.5");
  });

  it("keeps legacy reset support for the registered fields", () => {
    const panel = document.createElement("div");
    panel.id = "spanel-sound";
    panel.innerHTML = '<input type="number" id="boost" value="7"><p id="status"></p>';
    document.body.append(panel);
    const els = { pbStereoAgcDrive: panel.querySelector("#boost") };
    installMixSettings(els, { settingsStatus: panel.querySelector("#status") });
    els.pbStereoAgcDrive.value = "5";
    resetMixSettings(els);
    expect(els.pbStereoAgcDrive.value).toBe("7");
  });
});

describe("profiles", () => {
  function setup(fetchImpl) {
    document.body.innerHTML = `
      <details id="card"><summary>Profiles</summary>
        <input id="name"><button id="save">Create profile</button>
        <select id="profile"><option value="">Station defaults</option></select>
        <button id="apply-defaults">Activate station defaults</button>
        <ul id="list"></ul><p id="status"></p></details>
      <div id="spanel-picking" role="tabpanel"><select id="clock"></select></div>
      <div id="spanel-announcements" role="tabpanel"><select id="cart"></select></div>${CONFIRM}`;
    vi.stubGlobal("fetch", fetchImpl);
    const $ = (id) => document.getElementById(id);
    const els = {
      card: $("card"), nameInput: $("name"), saveButton: $("save"),
      list: $("list"), status: $("status"), profileSelect: $("profile"),
      clockSelect: $("clock"), cartSelect: $("cart"),
      applyDefaultsButton: $("apply-defaults"),
    };
    const controls = installProfiles(els);
    const answer = confirmDialog();
    els.card.open = true;
    els.card.dispatchEvent(new Event("toggle"));
    return { els, answer, controls };
  }

  it("creates an empty profile and offers clock and cart references", async () => {
    const fetchImpl = vi.fn((url, init = {}) => {
      if (init.method === "POST") return json({ profiles: ["Late"], active: null, clockwheels: ["Morning"], cart_pages: ["Main"] });
      return json({ profiles: [], active: null, clockwheels: ["Morning"], cart_pages: ["Main"] });
    });
    const { els } = setup(fetchImpl);
    els.nameInput.value = "Late";
    els.saveButton.click();
    await vi.waitFor(() => expect(els.status.textContent).toBe("Created profile Late."));
    expect(JSON.parse(fetchImpl.mock.calls.find(([, init]) => init?.method === "POST")[1].body)).toEqual({ name: "Late" });
    expect([...els.clockSelect.options].map((option) => option.textContent)).toEqual(["No clockwheel", "Morning"]);
    expect([...els.cartSelect.options].map((option) => option.textContent)).toEqual(["Main"]);
  });

  it("saves clock and cart drafts from their own tabs without a Profiles save bar", async () => {
    const values = { clock: { name: "Morning" }, cart: { page: "Main" } };
    const writes = [];
    const fetchImpl = vi.fn((url, init = {}) => {
      if (init.method === "PATCH") {
        const body = JSON.parse(init.body);
        writes.push(body.changes);
        Object.assign(values, body.changes);
        return json({ revision: "two", settings: { revision: "two", values },
          applied: Object.keys(body.changes), failed: {} });
      }
      if (url.startsWith("/api/settings")) return json({ profile: "", revision: "one", values });
      return json({ profiles: [], clockwheels: ["Morning", "Evening"], cart_pages: ["Main", "Night"] });
    });
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.clockSelect.value).toBe("Morning"));
    expect(els.card.querySelector("[data-settings-save]")).toBeNull();
    els.clockSelect.value = "Evening";
    els.clockSelect.dispatchEvent(new Event("change", { bubbles: true }));
    els.cartSelect.value = "Night";
    els.cartSelect.dispatchEvent(new Event("change", { bubbles: true }));
    await saveSettingsPanel(document.getElementById("spanel-picking"));
    expect(writes).toEqual([{ clock: { name: "Evening" } }]);
    expect(els.cartSelect.value).toBe("Night");
    await saveSettingsPanel(document.getElementById("spanel-announcements"));
    expect(writes).toEqual([{ clock: { name: "Evening" } }, { cart: { page: "Night" } }]);
  });

  it("activates station defaults explicitly", async () => {
    let active = "Late";
    const fetchImpl = vi.fn((url, init = {}) => {
      if (url === "/api/profiles/apply-defaults" && init.method === "POST") active = null;
      return json({ profiles: ["Late"], active });
    });
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.applyDefaultsButton.disabled).toBe(false));
    els.applyDefaultsButton.click();
    await vi.waitFor(() => expect(els.status.textContent).toBe("Activated station defaults."));
    expect(fetchImpl.mock.calls.some(([url, init]) => url === "/api/profiles/apply-defaults" && init?.method === "POST")).toBe(true);
  });

  it("moves the active marker without changing the profile editing target", async () => {
    const fetchImpl = vi.fn(() => json({ profiles: ["Early", "Late"], active: "Early" }));
    const { els, controls } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.profileSelect.querySelector('option[value="Late"]')).not.toBeNull());

    const defaultsOption = els.profileSelect.querySelector('option[value=""]');
    const earlyOption = els.profileSelect.querySelector('option[value="Early"]');
    const lateOption = els.profileSelect.querySelector('option[value="Late"]');
    els.profileSelect.value = "Early";
    els.profileSelect.focus();
    const changes = vi.fn();
    els.profileSelect.addEventListener("change", changes);

    controls.updateActive("Late");
    expect(earlyOption.textContent).toBe("Early");
    expect(lateOption.textContent).toBe("Late (active)");
    expect(els.profileSelect.value).toBe("Early");
    expect(document.activeElement).toBe(els.profileSelect);
    expect(els.profileSelect.querySelector('option[value=""]')).toBe(defaultsOption);
    expect(els.profileSelect.querySelector('option[value="Early"]')).toBe(earlyOption);
    expect(els.profileSelect.querySelector('option[value="Late"]')).toBe(lateOption);

    controls.updateActive(null);
    expect(defaultsOption.textContent).toBe("Station defaults (active)");
    expect(els.profileSelect.value).toBe("Early");
    expect(document.activeElement).toBe(els.profileSelect);

    controls.updateActive("External profile");
    const externalOption = els.profileSelect.querySelector('option[value="External profile"]');
    expect(externalOption.textContent).toBe("External profile (active)");
    expect(els.profileSelect.querySelector('option[value=""]')).toBe(defaultsOption);
    expect(els.profileSelect.querySelector('option[value="Early"]')).toBe(earlyOption);
    expect(els.profileSelect.querySelector('option[value="Late"]')).toBe(lateOption);
    expect(els.profileSelect.value).toBe("Early");
    expect(document.activeElement).toBe(els.profileSelect);
    expect(changes).not.toHaveBeenCalled();
  });

  // ADR-0018-03
  it("keeps Apply available for the active profile so it can be reapplied", async () => {
    const fetchImpl = vi.fn(() => json({ profiles: ["Late"], active: "Late" }));
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(1));

    const row = els.list.querySelector("li");
    const apply = row.querySelector("button");
    expect(row.querySelector(".profile-name").textContent).toBe("Late (active)");
    expect(apply.textContent).toBe("Activate");
    expect(apply.getAttribute("aria-label")).toBe("Activate profile Late");
    expect(apply.disabled).toBe(false);

    apply.click();

    await vi.waitFor(() => expect(els.status.textContent).toBe("Activated profile Late."));
    expect(fetchImpl.mock.calls.some(([url, init]) =>
      url === "/api/profiles/Late/apply" && init?.method === "POST")).toBe(true);
  });

  it("asks before deleting and then moves focus to the next row", async () => {
    let names = ["Early", "Late"];
    const fetchImpl = vi.fn((url, init = {}) => {
      if (init.method === "DELETE") names = names.filter((n) => !url.endsWith(n));
      return json({ profiles: names });
    });
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    expect(els.list.querySelectorAll("button[tabindex='0']")).toHaveLength(1);
    const firstApply = els.list.querySelector('[aria-label="Activate profile Early"]');
    firstApply.focus();
    firstApply.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    expect(document.activeElement.getAttribute("aria-label")).toBe("Activate profile Late");

    els.list.querySelector('[aria-label="Delete profile Early"]').click();
    await answer("confirm");

    await vi.waitFor(() => expect(els.status.textContent).toBe("Deleted profile Early."));
    expect(document.activeElement.getAttribute("aria-label")).toBe("Delete profile Late");
  });

  it("confirms reset before removing a profile's overrides", async () => {
    const fetchImpl = vi.fn(() => json({ profiles: ["Late"], active: null }));
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(1));
    els.list.querySelector('[aria-label="Reset profile Late to defaults"]').click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));
    await answer("cancel");
    expect(fetchImpl.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
    els.list.querySelector('[aria-label="Reset profile Late to defaults"]').click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));
    await answer("confirm");
    await vi.waitFor(() => expect(fetchImpl.mock.calls.some(([url, init]) => url.endsWith("/Late/reset") && init?.method === "POST")).toBe(true));
    await vi.waitFor(() => expect(els.status.textContent).toBe("Reset profile Late to station defaults."));
  });

  it("reloads the selected target after reset without applying it", async () => {
    const fetchImpl = vi.fn((url) => {
      if (url.startsWith("/api/settings")) return json({
        revision: "profile-revision", profile: "Late",
        values: { clock: { name: null }, cart: { page: null } },
      });
      return json({ profiles: ["Late"], active: null, clockwheels: [], cart_pages: [] });
    });
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.profileSelect.querySelector('option[value="Late"]')).not.toBeNull());
    els.profileSelect.value = "Late";
    els.list.querySelector('[aria-label="Reset profile Late to defaults"]').click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));
    await answer("confirm");
    await vi.waitFor(() => expect(els.status.textContent).toBe("Reset profile Late to station defaults."));
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/settings?profile=Late")).toBe(true);
    expect(fetchImpl.mock.calls.some(([url]) => url.endsWith("/Late/apply"))).toBe(false);
  });

  it("keeps profile controls on an unchanged refresh", async () => {
    const fetchImpl = vi.fn(() => json({ profiles: ["Early", "Late"] }));
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    const apply = els.list.querySelector('[aria-label="Activate profile Early"]');
    const rows = [...els.list.children];
    const mutations = [];
    const observer = new window.MutationObserver((records) => mutations.push(...records));
    observer.observe(els.list, { subtree: true, childList: true, characterData: true, attributes: true });
    const callsBeforeRefresh = fetchImpl.mock.calls.length;
    els.card.dispatchEvent(new Event("toggle"));
    await vi.waitFor(() => expect(fetchImpl.mock.calls.length).toBeGreaterThan(callsBeforeRefresh));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect([...els.list.children]).toEqual(rows);
    expect(els.list.querySelector('[aria-label="Activate profile Early"]')).toBe(apply);
    expect([...mutations, ...observer.takeRecords()]).toEqual([]);
    observer.disconnect();
  });

  it("keeps a deleted editing target and its draft selected on refresh", async () => {
    let names = ["Late"];
    const values = { clock: { name: "Morning" }, cart: { page: null } };
    const fetchImpl = vi.fn((url) => {
      if (url.startsWith("/api/settings")) return json({ profile: "Late", revision: "one", values });
      return json({ profiles: names, clockwheels: ["Morning", "Evening"], cart_pages: [] });
    });
    const { els, controls } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.profileSelect.querySelector('option[value="Late"]')).not.toBeNull());
    els.profileSelect.value = "Late";
    await setSettingsProfile("Late");
    els.clockSelect.value = "Evening";
    els.clockSelect.dispatchEvent(new Event("change", { bubbles: true }));

    names = [];
    await controls.refresh();

    expect(els.profileSelect.value).toBe("Late");
    expect(els.profileSelect.selectedOptions[0].textContent).toBe("Late (deleted)");
    expect(els.clockSelect.value).toBe("Evening");
    expect(document.querySelector('[data-settings-save]').getAttribute("aria-disabled")).toBe("false");
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/settings?profile=Late")).toBe(true);
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
    await vi.waitFor(() => expect(els.list.querySelectorAll("button")).toHaveLength(3));
    const apply = els.list.querySelector('[aria-label="Activate profile Late"]');
    els.nameInput.focus();

    apply.click();

    await vi.waitFor(() => expect(els.status.textContent).toBe(
      "Could not activate profile Late: Profile Late uses a preset that does not exist",
    ));
    expect(document.activeElement).toBe(els.nameInput);
  });
});

describe("browser access", () => {
  function setup(fetchImpl, authState = {}) {
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
        <button type="submit" id="device-rename-save">Save</button>
        <button type="button" id="device-rename-cancel">Cancel</button></form></dialog>`;
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
    rename.show = vi.fn(() => {
      opener = document.activeElement;
      rename.setAttribute("open", "");
    });
    rename.close = vi.fn(() => {
      if (!rename.hasAttribute("open")) return;
      rename.removeAttribute("open");
      if (opener?.isConnected) opener.focus();
      rename.dispatchEvent(new Event("close"));
    });
    installAccess(els, { onSignedOut, getAuthState: () => authState });
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

  // #631: show() and aria-modal, not showModal(), so NVDA keeps its place on
  // the page; Cancel last; Escape puts focus straight back on Rename.
  it("opens Rename with show() and aria-modal, Cancel last, and Escape returns focus straight to Rename", async () => {
    const fetchImpl = vi.fn(() => json({ pairing: true, devices }));
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    const dialog = document.getElementById("device-rename-dialog");
    const button = els.list.querySelector('[aria-label="Rename Phone"]');
    button.focus();
    button.click();
    expect(dialog.show).toHaveBeenCalledOnce();
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect([...dialog.querySelectorAll("button")].map((item) => item.id))
      .toEqual(["device-rename-save", "device-rename-cancel"]);
    const cancel = document.getElementById("device-rename-cancel");
    const key = (target, name, init = {}) => {
      const event = new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true, ...init });
      target.dispatchEvent(event);
      return event;
    };
    cancel.focus();
    expect(key(cancel, "Tab").defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(document.getElementById("device-rename-name"));
    const left = [];
    document.activeElement.addEventListener("blur", (event) => left.push(event.relatedTarget?.getAttribute("aria-label") || "body"));
    expect(key(document.activeElement, "Escape").defaultPrevented).toBe(true);
    expect(dialog.hasAttribute("open")).toBe(false);
    expect(document.activeElement).toBe(button);
    expect(left).toEqual(["Rename Phone"]);
    expect(fetchImpl.mock.calls.some(([, init = {}]) => init.method === "PATCH")).toBe(false);
  });

  it("keeps paired-device controls on an unchanged refresh and provides list navigation", async () => {
    const fetchImpl = vi.fn(() => json({ pairing: true, devices }));
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    const rows = [...els.list.children];
    const rename = els.list.querySelector('[aria-label="Rename Phone"]');
    rename.focus();
    rename.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true }));
    expect(document.activeElement.getAttribute("aria-label")).toBe("Revoke access Phone");
    document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(document.activeElement.getAttribute("aria-label")).toBe("Revoke access Tablet, this browser");

    const mutations = [];
    const observer = new window.MutationObserver((records) => mutations.push(...records));
    observer.observe(els.list, { subtree: true, childList: true, characterData: true, attributes: true });
    const callsBeforeRefresh = fetchImpl.mock.calls.length;
    els.card.dispatchEvent(new Event("toggle"));
    await vi.waitFor(() => expect(fetchImpl.mock.calls.length).toBeGreaterThan(callsBeforeRefresh));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect([...els.list.children]).toEqual(rows);
    expect(els.list.querySelector('[aria-label="Rename Phone"]')).toBe(rename);
    expect([...mutations, ...observer.takeRecords()]).toEqual([]);
    observer.disconnect();
  });

  it("moves focus and announces when a polled paired device disappears", async () => {
    let listed = devices;
    const fetchImpl = vi.fn(() => json({ pairing: true, devices: listed }));
    const { els } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    els.list.querySelector('[aria-label="Revoke access Phone"]').focus();
    listed = [devices[1]];
    els.card.dispatchEvent(new Event("toggle"));
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(1));
    await vi.waitFor(() => expect(document.activeElement.getAttribute("aria-label")).toBe("Revoke access Tablet, this browser"));
    await vi.waitFor(() => expect(els.status.textContent).toContain("The paired devices list changed. Focus moved to Revoke access Tablet, this browser."));
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
      expect(said).toEqual(["Pairing code 1 2 3 4, 5 6 7 8, valid for 8:20."]);
      expect(els.pairCode.textContent).toContain("1234 5678");
      expect(els.pairCountdown.textContent).toBe("Valid for 8:20.");
      const live = '[aria-live]:not([aria-live="off"]), [role="status"], [role="alert"]';
      expect(els.pairCountdown.closest(live)).toBeNull();
      expect(els.pairCode.closest(live)).toBeNull();

      // The code rotates while shown: new digits, nothing said.
      code = "87654321";
      await vi.advanceTimersByTimeAsync(3_000);
      expect(els.pairCountdown.textContent).toBe("Valid for 8:17.");
      await vi.advanceTimersByTimeAsync(197_000);
      expect(els.pairCode.textContent).toContain("8765 4321");
      expect(said).toHaveLength(1);

      // Refresh code says the current code again, once.
      els.pairRefresh.click();
      await vi.advanceTimersByTimeAsync(0);
      expect(said.at(-1)).toMatch(/^Pairing code 8 7 6 5, 4 3 2 1, valid for \d+:\d{2}\.$/);
      expect(said).toHaveLength(2);

      // Another device pairs while the code is shown: said once, and the
      // code, which pairs one device, is hidden with focus kept in reach.
      els.pairCopy.focus();
      listed = [...devices, { ...devices[0], device_id: "c".repeat(32), name: "Laptop" }];
      await vi.advanceTimersByTimeAsync(10_000);
      expect(els.list.querySelectorAll("li")).toHaveLength(3);
      expect(said.at(-1)).toBe("New device paired: Laptop.  That code is used up.  "
        + "Choose Show pairing code to pair another device.");
      expect(said).toHaveLength(3);
      expect(els.pairPanel.hidden).toBe(true);
      expect(els.pairCode.textContent).toBe("");
      expect(els.pairToggle.getAttribute("aria-expanded")).toBe("false");
      expect(document.activeElement).toBe(els.pairToggle);

      // Showing it again asks the server for a code for the next device.
      els.pairToggle.click();
      await vi.advanceTimersByTimeAsync(0);
      expect(els.pairPanel.hidden).toBe(false);
      expect(said).toHaveLength(4);

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
    expect(els.signOut.textContent).toBe("Sign out this browser");

    els.signOut.click();
    await answer("confirm");

    await vi.waitFor(() => expect(onSignedOut).toHaveBeenCalledWith(SIGNED_OUT_REASON));
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/logout")).toBe(true);
  });

  it("signs this browser out when its own entry is revoked", async () => {
    const fetchImpl = vi.fn((_url, init = {}) => json(init.method === "DELETE"
      ? { revoked: "b".repeat(32), signed_out: true } : { pairing: true, devices }));
    const { els, onSignedOut, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));

    els.list.querySelector('[aria-label="Revoke access Tablet, this browser"]').click();
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
    const revoke = els.list.querySelector('[aria-label="Revoke access Phone"]');
    revoke.focus();
    revoke.click();
    await vi.waitFor(() => expect(document.activeElement.id).toBe("confirm-cancel"));

    const { focused, stop } = recordFocus();
    await answer("confirm");
    await vi.waitFor(() => expect(els.status.textContent).toBe("Revoked Phone."));
    stop();

    expect(focused.map((element) => element.getAttribute("aria-label")))
      .toEqual(["Revoke access Tablet, this browser"]);
    expect(revoke.isConnected).toBe(false);
    expect(els.list.querySelectorAll("li")).toHaveLength(1);
  });

  it("puts focus back on Revoke and says why when the revoke fails", async () => {
    const fetchImpl = vi.fn((url, init = {}) => (init.method === "DELETE"
      ? json({ detail: "Server busy" }, 503)
      : json({ pairing: true, devices })));
    const { els, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    const revoke = els.list.querySelector('[aria-label="Revoke access Phone"]');
    revoke.focus();
    revoke.click();

    await answer("confirm");
    await vi.waitFor(() => expect(els.status.textContent).toBe("Could not revoke Phone: Server busy"));
    expect(document.activeElement).toBe(revoke);
  });

  it("shows SSO identity and browser names without offering pairing or unmanaged logout", async () => {
    const fetchImpl = vi.fn(() => json({
      pairing: false, mode: "trusted_proxy", devices, user: { id: "owner", name: "Alice" },
    }));
    const { els } = setup(fetchImpl, { mode: "trusted_proxy", logout_url: null });
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    expect(els.controls.hidden).toBe(false);
    expect(els.note.textContent).toContain("Signed in as Alice");
    expect(els.note.textContent).toContain("managed externally");
    expect(els.signOut.hidden).toBe(true);
    expect(els.pairToggle.hidden).toBe(true);
    expect(els.list.querySelector('[aria-label="Forget Phone"]')).not.toBeNull();
    expect(els.list.textContent).not.toContain("Paired");
    expect(fetchImpl.mock.calls.some(([url]) => url === "/api/pairing-code")).toBe(false);
  });

  it("forgets an SSO browser without claiming to revoke access or signing out", async () => {
    let listed = devices;
    const fetchImpl = vi.fn((url, init = {}) => {
      if (init.method === "DELETE") {
        listed = devices.slice(1);
        return json({ forgotten: devices[0].device_id, signed_out: false });
      }
      return json({ pairing: false, mode: "trusted_proxy", devices: listed, user: { name: "Alice" } });
    });
    const { els, onSignedOut, answer } = setup(fetchImpl);
    await vi.waitFor(() => expect(els.list.querySelectorAll("li")).toHaveLength(2));
    els.list.querySelector('[aria-label="Forget Phone"]').click();
    expect(document.getElementById("confirm-message").textContent).toContain("does not revoke");
    await answer("confirm");
    await vi.waitFor(() => expect(els.status.textContent).toContain("Forgot Phone"));
    expect(onSignedOut).not.toHaveBeenCalled();
    expect(els.list.querySelectorAll("li")).toHaveLength(1);
  });

  it("hands SSO logout to session cleanup with the server's configured URL", async () => {
    const logoutUrl = "https://login.example.test/logout";
    const fetchImpl = vi.fn((url) => json(url === "/api/logout"
      ? { authenticated: false, logout_url: logoutUrl }
      : { pairing: false, mode: "trusted_proxy", devices, user: { name: "Alice" } }));
    const { els, onSignedOut, answer } = setup(fetchImpl, {
      mode: "trusted_proxy", logout_url: logoutUrl,
    });
    await vi.waitFor(() => expect(els.controls.hidden).toBe(false));
    expect(els.signOut.hidden).toBe(false);
    expect(els.signOut.textContent).toBe("Sign out through SSO");
    els.signOut.click();
    expect(document.getElementById("confirm-message").textContent).not.toContain("pairing code");
    await answer("confirm");
    await vi.waitFor(() => expect(onSignedOut).toHaveBeenCalledWith("", logoutUrl));
  });

});
