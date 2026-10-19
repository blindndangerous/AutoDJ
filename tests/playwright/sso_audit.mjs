// Run against a disposable trusted-proxy server with a configured local logout
// landing page. The proxy supplies identity headers; this audit never does.
// AUTODJ_URL=http://127.0.0.1:2891 AUTODJ_BROWSERS=chromium npm run audit:sso
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import { runAudit } from "./audit_helpers.mjs";

const BASE = process.env.AUTODJ_URL || "http://127.0.0.1:2891";

async function readJson(context, path) {
  const response = await context.request.get(new URL(path, BASE).href);
  assert.equal(response.status(), 200, `${path} must be authorized`);
  return response.json();
}

async function accessPanel(page) {
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  await page.getByRole("tab", { name: "Profiles and access", exact: true }).click();
  await expect(page.locator("#spanel-access")).toBeVisible();
}

export async function audit(_name, launcher) {
  const browser = await launcher.launch();
  try {
    const first = await browser.newContext();
    // Observe cleanup before navigation rather than accepting document teardown
    // as evidence that the app explicitly closed its own socket.
    await first.addInitScript(() => {
      const NativeSocket = window.WebSocket;
      window.WebSocket = class extends NativeSocket {
        close(...args) {
          window.sessionStorage.setItem("sso-audit-socket-closed", "yes");
          return super.close(...args);
        }
      };
      const nativeFetch = window.fetch.bind(window);
      let loggedOut = false;
      window.fetch = async (...args) => {
        const path = new URL(typeof args[0] === "string" ? args[0] : args[0].url,
          location.href).pathname;
        if (loggedOut && path.startsWith("/api/")) {
          window.sessionStorage.setItem("sso-audit-post-logout-request", path);
        }
        const response = await nativeFetch(...args);
        if (path === "/api/logout" && response.ok) loggedOut = true;
        return response;
      };
    });
    const page = await first.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    await page.goto(BASE, { waitUntil: "networkidle" });
    await expect(page.locator("#conn-status")).toHaveText("Live");
    await expect(page.locator("#auth-dialog")).not.toBeVisible();
    const initial = await readJson(first, "/api/auth/status");
    assert.equal(initial.mode, "trusted_proxy");
    assert.equal(initial.pairing, false);
    assert.equal(initial.authenticated, true);
    assert.ok(initial.device_id);
    assert.ok(initial.logout_url, "SSO audit needs a configured logout URL");
    const logout = new URL(initial.logout_url, BASE);
    assert.equal(logout.origin, new URL(BASE).origin,
      "Use a disposable local logout landing page, never a live SSO provider");
    const firstCookie = (await first.cookies()).find((cookie) => cookie.name === "autodj_browser");
    assert.ok(firstCookie?.value, "auth status must automatically register a browser");

    await accessPanel(page);
    await expect(page.locator("#access-note")).toContainText(`Signed in as ${initial.user.name}`);
    await expect(page.getByRole("heading", { name: "Your browsers", exact: true })).toBeVisible();
    await expect(page.locator("#access-pairing")).not.toBeVisible();
    await expect(page.getByRole("button", { name: "Show pairing code", exact: true })).not.toBeVisible();
    const renamed = `SSO audit ${randomUUID().slice(0, 8)}`;
    await page.getByRole("button", { name: `Rename ${initial.device_name}, this browser`, exact: true }).click();
    const rename = page.getByRole("dialog", { name: `Rename ${initial.device_name}, this browser`, exact: true });
    await rename.getByRole("textbox", { name: "Device name", exact: true }).fill(renamed);
    await rename.getByRole("button", { name: "Save", exact: true }).click();
    await expect(rename).not.toBeVisible();
    await expect(page.getByRole("button", { name: `Rename ${renamed}, this browser`, exact: true })).toBeVisible();

    const second = await browser.newContext();
    const secondPage = await second.newPage();
    await secondPage.goto(BASE, { waitUntil: "networkidle" });
    await expect(secondPage.locator("#conn-status")).toHaveText("Live");
    const other = await readJson(second, "/api/auth/status");
    const secondCookie = (await second.cookies()).find((cookie) => cookie.name === "autodj_browser");
    assert.ok(secondCookie?.value);
    assert.notEqual(secondCookie.value, firstCookie.value);
    assert.notEqual(other.device_id, initial.device_id);
    const inventory = await readJson(first, "/api/devices");
    assert.ok(inventory.devices.some((device) => device.device_id === other.device_id));
    assert.ok(inventory.devices.some((device) => device.device_id === initial.device_id && device.name === renamed));
    await page.getByRole("button", { name: "Refresh device list", exact: true }).click();
    const forget = page.locator(`#device-list button[data-action="revoke"][data-device-id="${other.device_id}"]`);
    await expect(forget).toHaveAccessibleName(`Forget ${other.device_name}`);
    await forget.click();
    const confirmation = page.getByRole("dialog", { name: `Forget ${other.device_name}?`, exact: true });
    await expect(confirmation).toContainText("does not revoke SSO access");
    await confirmation.getByRole("button", { name: "Forget", exact: true }).click();
    await expect(forget).toHaveCount(0);
    const afterForget = await readJson(first, "/api/devices");
    assert.ok(!afterForget.devices.some((device) => device.device_id === other.device_id));
    await readJson(second, "/api/status");
    await expect(secondPage.locator("#conn-status")).toHaveText("Live");

    await page.evaluate(() => {
      window.sessionStorage.removeItem("sso-audit-socket-closed");
      window.sessionStorage.removeItem("sso-audit-post-logout-request");
    });
    await page.getByRole("button", { name: "Sign out through SSO", exact: true }).click();
    const signOut = page.getByRole("dialog", { name: "Sign out this browser?", exact: true });
    await expect(signOut).not.toContainText("pairing code");
    await Promise.all([
      page.waitForURL(logout.href),
      signOut.getByRole("button", { name: "Sign out", exact: true }).click(),
    ]);
    const cleanup = await page.evaluate(() => ({
      socket: window.sessionStorage.getItem("sso-audit-socket-closed"),
      request: window.sessionStorage.getItem("sso-audit-post-logout-request"),
    }));
    assert.equal(cleanup.socket, "yes", "AutoDJ must explicitly close its socket before logout navigation");
    assert.equal(cleanup.request, null, "AutoDJ must stop protected requests before logout navigation");
    assert.deepEqual(errors, []);
    return { registration: true, identity: true, rename: true, distinctBrowsers: true,
      forgetPreservesAccess: true, logoutCleanupAndNavigation: true, pageErrors: errors };
  } finally {
    await browser.close();
  }
}

await runAudit({
  audit,
  report: "sso_audit.json",
  validate: (_name, result) => assert.equal(result.logoutCleanupAndNavigation, true),
});
