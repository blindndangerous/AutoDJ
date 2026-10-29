// Run only against a disposable server: this audit creates stopped stations.
import assert from "node:assert/strict";
import { runAudit } from "./audit_helpers.mjs";

const BASE = process.env.AUTODJ_URL || "http://127.0.0.1:8080";
const ORIGIN = new URL(BASE).origin;

async function audit(name, launcher) {
  const browser = await launcher.launch();
  try {
    const context = await browser.newContext();
    await context.addInitScript(() => {
      if (localStorage.getItem("settings-audit-migrated")) return;
      localStorage.setItem("autodj.announceTrackChangesEverywhere", "on");
      localStorage.setItem("settings-audit-migrated", "yes");
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(BASE, { waitUntil: "networkidle" });
    assert.equal(await page.evaluate(() => localStorage.getItem("autodj.announceTrackChanges")), "all");
    assert.equal(await page.evaluate(() => localStorage.getItem("autodj.announceTrackChangesEverywhere")), null);
    await page.reload({ waitUntil: "networkidle" });
    assert.equal(await page.locator("#announce-track-changes").inputValue(), "all");
    const slug = `audit-${name}-${Date.now().toString(36)}`;
    const created = await page.request.post(`${BASE}/api/stations`, {
      headers: { Origin: ORIGIN }, data: { slug, name: slug, autostart: false },
    });
    assert.equal(created.status(), 201, await created.text());
    await page.goto(`${BASE}/?station=${slug}#settings`, { waitUntil: "networkidle" });
    const endpoint = `${BASE}/api/stations/${slug}`;
    const runtimeSettings = async () => (await (await page.request.get(`${endpoint}/settings`)).json());
    const editingSettings = async (profile = "") => (await (await page.request.get(
      `${endpoint}/settings?profile=${encodeURIComponent(profile)}`)).json());
    await page.locator("#stab-sound").click();
    const panel = page.locator("#spanel-sound");
    assert.equal(await panel.locator("[data-settings-save]").count(), 1);
    const speed = page.locator("#pb-music-speed-number");
    const original = await editingSettings();
    const runtimeBefore = await runtimeSettings();
    const before = original.values.playback.music_speed_percent;
    assert.equal(runtimeBefore.values.playback.music_speed_percent, before);
    const wanted = before === 1 ? 2 : 1;
    await speed.fill(String(wanted));
    assert.equal((await editingSettings()).values.playback.music_speed_percent, before);
    // The result is said through a status region rendered before its words
    // arrive, and the pressed button keeps focus (WCAG 4.1.3, 2.4.3).
    const status = panel.locator(".settings-save-status");
    assert.equal(await status.getAttribute("role"), "status");
    assert.notEqual(await status.evaluate((node) => window.getComputedStyle(node).display), "none");
    const saving = page.waitForResponse((response) => response.url() === `${endpoint}/settings`
      && response.request().method() === "PATCH");
    await panel.locator("[data-settings-save]").focus();
    await page.keyboard.press("Enter");
    assert.equal((await saving).status(), 200);
    assert.equal((await editingSettings()).values.playback.music_speed_percent, wanted);
    assert.equal((await runtimeSettings()).values.playback.music_speed_percent, before);
    await page.waitForFunction(() => document.querySelector("#spanel-sound .settings-save-status")
      .textContent.startsWith("Settings saved for"));
    const focusAfter = (selector) => page.evaluate((target) => {
      const button = document.querySelector(target);
      return { kept: document.activeElement === button, disabled: button.disabled,
        unavailable: button.getAttribute("aria-disabled") };
    }, selector);
    assert.deepEqual(await focusAfter("#spanel-sound [data-settings-save]"),
      { kept: true, disabled: false, unavailable: "true" });
    await page.locator("#stab-access").click();
    const applyDefaults = page.waitForResponse((response) => response.url() === `${endpoint}/profiles/apply-defaults`
      && response.request().method() === "POST");
    await page.locator("#profile-apply-defaults").click();
    assert.equal((await applyDefaults).status(), 200);
    assert.equal((await runtimeSettings()).values.playback.music_speed_percent, wanted);
    const profileName = `Audit ${name}`;
    await page.locator("#profile-name").fill(profileName);
    const createProfile = page.waitForResponse((response) => response.url() === `${endpoint}/profiles`
      && response.request().method() === "POST");
    await page.locator("#profile-save").click();
    assert.equal((await createProfile).status(), 200);
    await page.locator("#settings-profile").selectOption(profileName);
    await page.locator("#stab-sound").click();
    await speed.fill("6");
    await panel.locator("[data-settings-save]").click();
    await page.waitForFunction(() => document.querySelector("#spanel-sound .settings-save-status")
      .textContent.startsWith("Settings saved for"));
    assert.equal((await editingSettings(profileName)).values.playback.music_speed_percent, 6);
    assert.equal((await runtimeSettings()).values.playback.music_speed_percent, wanted);
    await speed.fill("7");
    await page.locator("#settings-profile").selectOption("");
    await page.getByRole("button", { name: "Stay", exact: true }).click();
    assert.equal(await page.locator("#settings-profile").inputValue(), profileName);
    assert.equal(await speed.inputValue(), "7");
    await panel.locator("[data-settings-discard]").click();
    await page.waitForFunction(() => document.getElementById("pb-music-speed-number").value === "6");
    await page.locator("#stab-access").click();
    const profileApply = page.waitForResponse((response) => response.url() === `${endpoint}/profiles/${encodeURIComponent(profileName)}/apply`
      && response.request().method() === "POST");
    await page.getByRole("button", { name: `Apply profile ${profileName}` }).click();
    assert.equal((await profileApply).status(), 200);
    assert.equal((await runtimeSettings()).values.playback.music_speed_percent, 6);
    const profileReapply = page.waitForResponse((response) => response.url() === `${endpoint}/profiles/${encodeURIComponent(profileName)}/apply`
      && response.request().method() === "POST");
    await page.getByRole("button", { name: `Apply profile ${profileName}` }).click();
    assert.equal((await profileReapply).status(), 200);
    await page.locator("#settings-profile").selectOption("");
    await page.locator("#stab-sound").click();
    await speed.fill("3");
    await page.locator("#stab-stream").click();
    // The dialog's close event returns focus to its opener a task later, so
    // wait for it before focusing Discard.
    const closed = page.evaluate(() => new Promise((resolve) => {
      document.getElementById("confirm-dialog")
        .addEventListener("close", () => setTimeout(resolve, 0), { once: true });
    }));
    await page.getByRole("button", { name: "Stay", exact: true }).click();
    await closed;
    assert.equal(await panel.isVisible(), true);
    await panel.locator("[data-settings-discard]").focus();
    await page.keyboard.press("Enter");
    assert.equal(await speed.inputValue(), String(wanted));
    await page.waitForFunction(() => document.querySelector("#spanel-sound .settings-save-status")
      .textContent.startsWith("Changes discarded."));
    assert.deepEqual(await focusAfter("#spanel-sound [data-settings-discard]"),
      { kept: true, disabled: false, unavailable: "true" });

    await speed.fill("4");
    const current = await editingSettings();
    const concurrent = await page.request.patch(`${endpoint}/settings`, {
      headers: { Origin: ORIGIN },
      data: { revision: current.revision, profile: "", changes: { playback: { music_speed_percent: 5 } } },
    });
    assert.equal(concurrent.status(), 200);
    assert.equal((await runtimeSettings()).values.playback.music_speed_percent, 6);
    const conflict = page.waitForResponse((response) => response.url() === `${endpoint}/settings`
      && response.request().method() === "PATCH");
    await panel.locator("[data-settings-save]").click();
    assert.equal((await conflict).status(), 409);
    assert.equal(await speed.inputValue(), "4");
    await panel.locator("[data-settings-discard]").click();
    await page.waitForFunction(() => document.getElementById("pb-music-speed-number").value === "5");

    await page.locator("#stab-stream").click();
    await page.locator("#stream-relay-settings > summary").click();
    await page.locator("#stream-relay-add").click();
    await page.locator("#stream-relay-name").fill("Streammadness relay");
    // Public literal passes destination validation; the disabled relay never connects.
    await page.locator("#stream-relay-host").fill("8.8.8.8");
    await page.locator("#stream-relay-port").fill("8002");
    await page.locator("#stream-relay-mount").fill(`/${slug}`);
    await page.locator("#stream-relay-password").fill("test123");
    await page.locator("#stream-relay-enabled").uncheck();
    const relaySave = page.waitForResponse((response) => response.url() === `${endpoint}/stream/relays`
      && response.request().method() === "POST");
    await page.locator('#stream-relay-form button[type="submit"]').click();
    const relayResponse = await relaySave;
    assert.equal(relayResponse.status(), 200, await relayResponse.text());
    const relays = await (await page.request.get(`${endpoint}/stream/relays`)).json();
    assert.equal(relays.relays[0].name, "Streammadness relay");
    assert.equal(JSON.stringify(relays).includes("test123"), false);
    assert.equal(await page.locator("#stab-live").isVisible(), true);
    assert.deepEqual(errors, []);
    for (const canonical of ["off", null]) {
      const migration = await browser.newContext();
      await migration.addInitScript((canonical) => {
        if (window.sessionStorage.getItem("migration-tried")) return;
        window.sessionStorage.setItem("migration-tried", "yes");
        localStorage.setItem("autodj.announceTrackChangesEverywhere", "on");
        if (canonical) localStorage.setItem("autodj.announceTrackChanges", canonical);
        else {
          const set = window.Storage.prototype.setItem;
          window.Storage.prototype.setItem = function(key, value) {
            if (key === "autodj.announceTrackChanges") throw new Error("Storage blocked for audit");
            return set.call(this, key, value);
          };
        }
      }, canonical);
      const migrationPage = await migration.newPage();
      await migrationPage.goto(BASE, { waitUntil: "networkidle" });
      assert.equal(await migrationPage.locator("#announce-track-changes").inputValue(), canonical || "all");
      const legacy = await migrationPage.evaluate(() => localStorage.getItem("autodj.announceTrackChangesEverywhere"));
      assert.equal(legacy, canonical ? null : "on");
      await migrationPage.reload({ waitUntil: "networkidle" });
      assert.equal(await migrationPage.evaluate(() => localStorage.getItem("autodj.announceTrackChanges")), canonical || "all");
      assert.equal(await migrationPage.evaluate(() => localStorage.getItem("autodj.announceTrackChangesEverywhere")), null);
      await migration.close();
    }
    return { stoppedStation: slug, draftSaveDiscard: true, saveFocusAndStatus: true, conflict: true, spacedRelay: true, migration: true, errors };
  } finally {
    await browser.close();
  }
}

await runAudit({ audit, validate: (_name, result) => assert.deepEqual(result.errors, []),
  report: "settings_audit.json" });
