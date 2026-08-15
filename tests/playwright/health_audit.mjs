// Deeper health audit — captures console + network + uncaught errors
// across every browser engine, exercises the UI, and dumps a per-engine
// report so we can spot 404s, broken assets, WS failures, and JS errors.

import { runAudit, validateHealthAudit } from "./audit_helpers.mjs";

// Set AUTODJ_URL=http://host:port before running, e.g.:
//   AUTODJ_URL=http://localhost:8080 node tests/playwright/health_audit.mjs
const BASE = process.env.AUTODJ_URL || "http://localhost:8080";

export async function audit(name, launcher) {
  const browser = await launcher.launch({
    args: name === "chromium" ? ["--autoplay-policy=no-user-gesture-required"] : [],
  });
  try {
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  const out = {
    console: [], pageerrors: [], requestfailed: [], status_4xx_5xx: [],
    websockets: [], unhandled: [],
  };
  page.on("console", (m) => out.console.push({ type: m.type(), text: m.text(),
    loc: m.location() }));
  page.on("pageerror", (e) => out.pageerrors.push({ message: e.message,
    stack: (e.stack || "").split("\n").slice(0, 4).join("\n") }));
  page.on("requestfailed", (req) => out.requestfailed.push({
    url: req.url(), method: req.method(), failure: req.failure() }));
  page.on("response", (r) => {
    if (r.status() >= 400) out.status_4xx_5xx.push({
      url: r.url(), status: r.status(), method: r.request().method() });
  });
  page.on("websocket", (ws) => {
    const log = { url: ws.url(), opened: true, closed: false, frames: 0,
      errors: [] };
    out.websockets.push(log);
    ws.on("close", () => { log.closed = true; });
    ws.on("framereceived", () => { log.frames++; });
    ws.on("socketerror", (e) => log.errors.push(String(e)));
  });

  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });

  // Drive the UI a bit so dormant code paths execute
  await page.click("body");
  await page.waitForTimeout(500);
  // The transition control belongs to the Settings tab.  Open the same tab a
  // user would before selecting it so this audit only drives visible controls.
  try {
    await page.locator("#tab-settings").click();
    await page.locator("#panel-settings").waitFor({ state: "visible" });
    if (await page.locator("#tab-settings").getAttribute("aria-selected") !== "true") {
      throw new Error("Settings tab did not become selected");
    }
    await page.locator("#transition-select").waitFor({ state: "visible" });
    // Cycle the transition select to fire postSettings.
    await page.selectOption("#transition-select", "highpass_sweep");
    await page.waitForTimeout(200);
    await page.selectOption("#transition-select", "freeze");
    await page.waitForTimeout(200);
    await page.selectOption("#transition-select", "pitch_fall");
    await page.waitForTimeout(200);
  } catch (e) { out.unhandled.push("select " + e); }

  // Click each visible tab so panels render
  for (const sel of ["[role='tab']"]) {
    const tabs = await page.$$(sel);
    for (const t of tabs) {
      try { await t.click(); await page.waitForTimeout(150); }
      catch (e) { out.unhandled.push("tab " + e); }
    }
  }

  // Probe page-side health
  out.probe = await page.evaluate(() => {
    return {
      title: document.title,
      hasAudio: !!document.querySelector("audio"),
      isSecureContext: window.isSecureContext,
      audioWorkletAvailable: !!(window.AudioContext
        && new (window.AudioContext)().audioWorklet),
      userAgent: navigator.userAgent,
    };
  }).catch((e) => ({ error: String(e) }));

  await page.waitForTimeout(800);
  return out;
  } finally {
    await browser.close();
  }
}

await runAudit({
  audit,
  report: "health_audit.json",
  validate: validateHealthAudit,
});
