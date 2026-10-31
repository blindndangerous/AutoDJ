// Production markup, styles and voice tracker in Chromium; deterministic API fixtures.
// This isolates keyboard/focus/axe evidence from audio hardware and station scheduling.
import assert from "node:assert/strict";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { createServer } from "node:http";
import { resolve, extname, sep } from "node:path";
import { chromium, expect } from "@playwright/test";

const staticRoot = resolve("src/autodj/static");
const report = { fixture: "Production Voice tracks markup/CSS/module; deterministic API; no microphone or NVDA", themes: [], errors: [] };
const row = {
  target_id: "a".repeat(32), run_id: "b".repeat(32), revision: "snapshot",
  outgoing_occurrence_id: "out-one", incoming_occurrence_id: "in-one",
  ending: { id: "one", title: "First song", artist: "First artist" },
  upcoming: { id: "two", title: "Second song", artist: "Second artist" },
  selection: "predicted", songs_to_queue: 4, eligible: true,
  estimated_airtime: Date.now() / 1000 + 180,
};
let posts = 0;
// Served in place of /app.js, so the production page loads only the voice tracker.
const startup = `
  import { installVoiceTracker } from '/modules/voice-tracker.js';
  for (const panel of document.querySelectorAll('[role="tabpanel"]')) panel.hidden = panel.id !== 'panel-voice';
  for (const tab of document.querySelectorAll('[role="tab"]')) tab.setAttribute('aria-selected', String(tab.id === 'tab-voice'));
  window.voiceAudit = installVoiceTracker();
  await window.voiceAudit.refresh();
`;
const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url, "http://localhost");
    if (url.pathname.startsWith("/api/voice-tracks")) {
      response.setHeader("Content-Type", "application/json");
      if (request.method === "POST") {
        let body = "";
        for await (const chunk of request) body += chunk;
        assert.equal(JSON.parse(body).target_id, row.target_id);
        posts += 1;
        row.target_id = "c".repeat(32);
        row.selection = "confirmed";
        response.end(JSON.stringify(row));
        return;
      }
      const selected = url.searchParams.has("target_id");
      response.end(JSON.stringify({
        run_id: row.run_id, targets: [row], accepting: !selected || row.selection === "confirmed",
        slot: { ...row, target_id: selected ? row.target_id : undefined,
          slot_id: selected ? row.target_id : "d".repeat(32), transition_revision: "snapshot",
          closes_in_s: 120, airs_in_s: 180, ending_outro_s: 5, upcoming_intro_s: 8,
          transition_items: [], transition_order: [] },
        tracks: [], limits: { sample_rate: 44100, max_bytes: 67108864, max_seconds: 600 },
        recovery_notice: "Live recordings are not restored after a station restart. Previous airtime is unknown.",
      }));
      return;
    }
    if (url.pathname === "/app.js") {
      response.setHeader("Content-Type", "text/javascript");
      response.end(startup);
      return;
    }
    const path = resolve(staticRoot, `.${url.pathname === "/" ? "/index.html" : url.pathname}`);
    if (!path.startsWith(`${staticRoot}${sep}`)) { response.writeHead(403); response.end(); return; }
    const body = await readFile(path);
    response.setHeader("Content-Type", ({ ".html": "text/html", ".js": "text/javascript", ".css": "text/css" })[extname(path)] || "application/octet-stream");
    response.end(body);
  } catch (error) {
    report.errors.push(String(error));
    response.writeHead(500); response.end("Fixture error");
  }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
let browser;
try {
  browser = await chromium.launch({ headless: true });
  report.browser = browser.version();
  for (const theme of ["light", "dark"]) {
    row.target_id = "a".repeat(32); row.selection = "predicted";
    const page = await browser.newPage();
    page.on("pageerror", (error) => report.errors.push(String(error)));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => Boolean(window.voiceAudit));
    await page.evaluate((value) => document.documentElement.setAttribute("data-theme", value), theme);
    for (let count = 0; count < 180; count += 1) {
      await page.keyboard.press("Tab");
      if (await page.locator("#vt-target").evaluate((node) => node === document.activeElement)) break;
    }
    await expect(page.locator("#vt-target")).toBeFocused();
    const describedBy = await page.locator("#vt-target").getAttribute("aria-describedby");
    assert.ok(describedBy.split(/\s+/).includes("vt-selection"));
    assert.ok(describedBy.split(/\s+/).includes("vt-estimated-airtime"));
    await expect(page.locator("#vt-confirm")).toHaveAttribute("aria-disabled", "true");
    await expect(page.locator("#vt-confirm-count")).toContainText("Choose a predicted song change first");
    await page.keyboard.press("ArrowDown");
    await expect(page.locator("#vt-selection")).toHaveText("Selection: Predicted");
    const before = posts;
    await page.evaluate(() => window.voiceAudit.refresh());
    await expect(page.locator("#vt-target")).toBeFocused();
    assert.equal(posts, before);
    await page.keyboard.press("Tab");
    await expect(page.locator("#vt-confirm")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator("#vt-selection")).toHaveText("Selection: Confirmed");
    await expect(page.locator("#vt-confirm")).toBeFocused();
    assert.equal(posts, before + 1);
    await expect(page.locator("#vt-confirm")).toHaveAttribute("aria-disabled", "true");
    await expect(page.locator("#vt-confirm-count")).toContainText("already confirmed");
    for (let count = 0; count < 12; count += 1) {
      await page.keyboard.press("Tab");
      if (await page.locator("#vt-record").evaluate((node) => node === document.activeElement)) break;
    }
    await expect(page.locator("#vt-record")).toBeFocused();
    const sizes = await page.locator("#vt-target, #vt-confirm, #vt-record").evaluateAll((nodes) => nodes.map((node) => {
      const box = node.getBoundingClientRect();
      return { id: node.id, width: box.width, height: box.height };
    }));
    for (const size of sizes) assert.ok(size.width >= 24 && size.height >= 24, JSON.stringify(size));
    await page.addScriptTag({ path: resolve("node_modules/axe-core/axe.min.js") });
    const axe = await page.evaluate(async () => window.axe.run(document.querySelector("#panel-voice"), {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"] },
    }));
    report.themes.push({ theme, describedBy, disabledReasons: "Choose a predicted song change first; already confirmed", keyboard: "Tab, ArrowDown, Tab, Enter, Tab to Record; refresh preserves focus", sizes, violations: axe.violations, incomplete: axe.incomplete.map((item) => ({ id: item.id, nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })) })) });
    assert.equal(axe.violations.length, 0, JSON.stringify(axe.violations));
    await page.close();
  }
  assert.deepEqual(report.errors, []);
} catch (error) {
  report.failure = String(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
  await new Promise((done) => server.close(done));
  await mkdir(".codex-tmp", { recursive: true });
  await writeFile(".codex-tmp/voice-target-browser.json", `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report));
}
