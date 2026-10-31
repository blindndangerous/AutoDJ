// Real browser, shared CSS and production module; mocked API, no DNS or CA writes.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { runAudit } from "./audit_helpers.mjs";

const require = createRequire(import.meta.url);
const staticRoot = new URL("../../src/autodj/static/", import.meta.url);
const html = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>AutoDJ certificate audit</title><link rel="stylesheet" href="/app.css"></head><body><main><h1>AutoDJ</h1><section id="secure-connections"></section><p id="secure-connections-status" role="status" aria-live="polite"></p></main><script type="module">import {installSecureConnections} from "/modules/secure-connections.js"; installSecureConnections();</script></body></html>';
const server = createServer(async (request, response) => {
  try {
    const path = new URL(request.url, "http://localhost").pathname;
    if (path === "/") { response.setHeader("Content-Type", "text/html"); response.end(html); return; }
    if (!/^\/(modules\/[a-z-]+\.js|app\.css)$/.test(path)) { response.writeHead(404).end(); return; }
    response.setHeader("Content-Type", path.endsWith(".css") ? "text/css" : "text/javascript");
    response.end(await readFile(fileURLToPath(new URL(path.slice(1), staticRoot))));
  } catch { response.writeHead(500).end(); }
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;

async function audit(name, launcher) {
  const browser = await launcher.launch({ headless: true });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors = [], calls = [], scans = [];
    let operation = null;
    page.on("pageerror", (error) => errors.push(error.message));
    const inventory = () => ({ certificates: [], can_manage: true, local: true, https: false, acme_available: true, hosts: ["localhost", "radio.example.org"], urls: [], acme_operations: operation ? [operation] : [] });
    await page.route("**/api/server/certificates**", async (route) => {
      const request = route.request(), path = new URL(request.url()).pathname;
      calls.push({ method: request.method(), path, body: request.postDataJSON() });
      if (path.endsWith("/preflight")) {
        operation = { id: "a".repeat(32), name: "Radio", domain: "radio.example.org", environment: "staging", phase: "ready", addresses: ["203.0.113.8"], confirmed: false, terms_url: "https://letsencrypt.org/terms" };
      } else if (path.endsWith("/issue")) operation = { ...operation, phase: "complete" };
      await route.fulfill({ json: path === "/api/server/certificates" ? inventory() : operation });
    });
    await page.goto(base);
    await page.locator("#sc-add").focus();
    await page.keyboard.press("Enter");
    await page.locator("#sc-cancel").waitFor({ state: "visible" });
    assert.equal(await page.locator("#sc-cancel").evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press("Tab");
    assert.equal(await page.locator("#sc-method").evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press("End");
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    assert.equal(await page.locator("#sc-domain").evaluate((el) => el === document.activeElement), true);
    await page.keyboard.type("radio.example.org");
    await page.keyboard.press("Tab"); await page.keyboard.type("b".repeat(32));
    await page.keyboard.press("Tab"); await page.keyboard.type("audit-dns-token");
    assert.equal(await page.locator("#sc-token").getAttribute("type"), "password");
    await page.keyboard.press("Tab");
    assert.equal(await page.locator("#sc-environment").inputValue(), "staging");
    await page.keyboard.press("End");
    assert.equal(await page.locator("#sc-ct-warning").isVisible(), true);
    await page.keyboard.press("Home");
    await page.addScriptTag({ path: require.resolve("axe-core/axe.min.js") });
    for (const theme of ["light", "dark"]) {
      await page.evaluate((value) => document.documentElement.dataset.theme = value, theme);
      const result = await page.evaluate(() => window.axe.run(".secure-connection-dialog", { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"] } }));
      scans.push({ theme, violations: result.violations.map(({ id, nodes }) => ({ id, targets: nodes.map((node) => node.target) })) });
      assert.deepEqual(scans.at(-1).violations, []);
    }
    await page.setViewportSize({ width: 320, height: 700 });
    assert.equal(await page.locator("dialog").evaluate((el) => el.scrollWidth <= el.clientWidth + 1), true, "dialog reflows at 320 CSS pixels");
    await page.emulateMedia({ forcedColors: "active", reducedMotion: "reduce" });
    await page.keyboard.press("Tab");
    assert.equal(await page.locator("#sc-next").evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press("Enter");
    await page.locator("#sc-terms").waitFor();
    assert.equal(await page.locator("#sc-token").count(), 0);
    assert.equal(await page.locator("#sc-terms").isChecked(), false);
    assert.equal(await page.locator("#sc-host-confirm").isChecked(), false);
    assert.equal(await page.locator("#sc-step-heading").evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press("Tab"); // Current terms link.
    await page.keyboard.press("Tab"); // Unchecked terms consent.
    assert.equal(await page.locator("#sc-terms").evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press("Space"); await page.keyboard.press("Tab"); await page.keyboard.press("Space");
    await page.keyboard.press("Tab"); // Check status.
    await page.keyboard.press("Tab"); // Remove saved setup.
    await page.keyboard.press("Tab"); // Run staging test.
    assert.equal(await page.locator("#sc-next").evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector("dialog").textContent.includes("No trusted certificate was saved"));
    assert.equal(await page.locator("#sc-activate").count(), 0);
    assert.equal(calls.filter((call) => call.path.endsWith("/issue")).length, 1);
    assert.equal(calls.find((call) => call.path.endsWith("/issue")).body.accept_terms, true);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#sc-add").evaluate((el) => el === document.activeElement), true);
    assert.deepEqual(errors, []);
    return { browser: name, keyboard: "passed", reflow320: "passed", forcedColors: "keyboard flow passed", scans, errors, limitation: "Mocked API; no live CA, operating-system trust, microphone, or screen-reader speech evidence." };
  } finally { await browser.close(); }
}
try {
  await runAudit({ audit, validate: (_name, result) => assert.deepEqual(result.errors, []), report: "acme_audit.json" });
} finally { await new Promise((resolve) => server.close(resolve)); }
