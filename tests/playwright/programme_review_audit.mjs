// Real Chromium controls -> native review API -> real render/encode -> downloads.
// Fixture isolates hardware/model startup; it does not mock browser requests or controls.
// Run: PLAYWRIGHT_BROWSERS_PATH=<installed path> node tests/playwright/programme_review_audit.mjs
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { chromium, expect } from "@playwright/test";

const root = await mkdtemp(join(tmpdir(), "autodj-review-browser-"));
const port = process.env.AUTODJ_REVIEW_AUDIT_PORT || "8893";
const base = `http://127.0.0.1:${port}`;
const python = process.env.AUTODJ_PYTHON || resolve(".venv/bin/python");
const fixture = "tests/playwright/programme_review_fixture.py";
const server = spawn(python, [fixture, "--root", root, "--port", port], {
  env: { ...process.env, PYTHONPATH: ["src", "."].join(delimiter), TMPDIR: root,
    PATH: `${dirname(python)}${delimiter}${process.env.PATH}` },
  stdio: ["ignore", "pipe", "pipe"],
});
let logs = "";
server.stdout.on("data", (data) => { logs += data; });
server.stderr.on("data", (data) => { logs += data; });
const report = { environment: "Chromium headless; local synthetic station; normal app lifespan disabled",
  steps: [], page_errors: [], audio_verification: null };
let browser;
try {
  await expect.poll(async () => {
    if (server.exitCode !== null) throw new Error(`Fixture server exited: ${logs}`);
    try { return (await fetch(`${base}/review-audit`)).status; } catch { return 0; }
  }, { timeout: 20000 }).toBe(200);
  browser = process.env.AUTODJ_REVIEW_CDP_URL
    ? await chromium.connectOverCDP(process.env.AUTODJ_REVIEW_CDP_URL)
    : await chromium.launch({ args: ["--no-sandbox"] });
  report.browser_version = browser.version();
  const page = await browser.newPage({ acceptDownloads: true });
  async function control(action) {
    const response = await page.request.post(`${base}/audit-control`, {
      headers: { Origin: base }, data: { action },
    });
    assert.equal(response.status(), 200, await response.text());
  }
  page.on("pageerror", (error) => report.page_errors.push(String(error)));
  await page.goto(`${base}/review-audit`);
  await page.waitForFunction(() => Boolean(window.reviewAudit));
  await page.getByRole("tab", { name: "Library tools", exact: true }).click();
  await expect(page.locator("#panel-library > details.card").last()).toHaveAttribute("id", "review-card");
  await expect(page.locator("#review-card")).not.toHaveAttribute("open");
  await page.locator("#review-card > summary").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#review-card")).toHaveAttribute("open");
  await expect(page.locator("#review-show option")).toHaveCount(1);
  await page.locator("#review-card details > summary").click();
  await page.locator("#review-head").fill("0.2");
  await page.locator("#review-tail").fill("0.2");
  async function generate({ moveFocus = false } = {}) {
    const submitted = page.waitForResponse((response) => response.url() === `${base}/api/reviews`
      && response.request().method() === "POST");
    await page.getByRole("button", { name: "Generate review", exact: true }).focus();
    await page.keyboard.press("Enter");
    const response = await submitted;
    assert.equal(response.status(), 200, await response.text());
    const job = await response.json();
    await expect(page.locator("#review-generate")).toBeDisabled();
    await expect(page.locator("#review-cancel")).toBeFocused();
    if (moveFocus) await page.locator("#review-head").focus();
    await expect(page.locator("#review-audio")).toHaveAttribute("src", new RegExp(job.id), { timeout: 20000 });
    await expect(page.locator("#review-status")).toContainText("Review ready", { timeout: 20000 });
    await expect(page.locator("#review-result")).toBeVisible();
    await expect(page.locator(moveFocus ? "#review-head" : "#review-generate")).toBeFocused();
    await expect(page.locator("#review-generate")).toBeEnabled();
  }
  await generate();
  await page.getByRole("button", { name: "Load review", exact: true }).click();
  await expect(page.locator("#review-heading")).toBeFocused();
  report.steps.push("keyboard Generate moves focus to enabled Cancel; completion returns focus unless moved elsewhere; explicit loading focuses the result");
  const original = await page.locator("#review-audio").getAttribute("src");
  assert.ok(original?.endsWith("/audio/mp3"));
  await expect(page.locator("#review-audio")).not.toHaveAttribute("autoplay");
  await page.locator("#review-audio").focus();
  await page.keyboard.press("Space");
  await expect.poll(() => page.locator("#review-audio").evaluate((audio) => audio.currentTime)).toBeGreaterThan(0);
  await page.keyboard.press("Space");
  await page.getByRole("button", { name: "Next item", exact: true }).click();
  const nextTime = await page.locator("#review-audio").evaluate((audio) => audio.currentTime);
  assert.ok(nextTime > 0);
  await expect(page.locator("#review-items")).toContainText("Programme");
  await expect(page.locator("#review-items")).toContainText("preview");
  report.steps.push("saved-show generation, native listening, keyboard item navigation and both time axes");
  for (const format of ["mp3", "wav"]) {
    const downloaded = page.waitForEvent("download");
    await page.getByRole("link", { name: `Download ${format.toUpperCase()}` }).click();
    await (await downloaded).saveAs(join(root, `download.${format}`));
  }
  const checked = spawnSync(python, [fixture, "--verify", join(root, "download.wav"), join(root, "download.mp3")], {
    env: { ...process.env, PYTHONPATH: ["src", "."].join(delimiter), PATH: `${dirname(python)}${delimiter}${process.env.PATH}` },
    encoding: "utf8",
  });
  assert.equal(checked.status, 0, checked.stderr);
  report.audio_verification = JSON.parse(checked.stdout.trim());
  report.steps.push("actual MP3/WAV downloads decoded and compared with identifiable source frequencies");
  await page.locator("#review-selection-start").fill("3.7");
  await page.locator("#review-selection-end").fill("3.8");
  await generate({ moveFocus: true });
  const detailURL = (await page.locator("#review-audio").getAttribute("src")).replace(/\/audio\/mp3$/, "");
  const detail = await (await page.request.get(`${base}${detailURL}`)).json();
  assert.ok(detail.effective_range[0] < 3.7 || detail.effective_range[1] > 3.8);
  report.steps.push("selected transition range expands to preserve rendered join");
  await control("edit");
  await page.getByRole("button", { name: "Refresh reviews", exact: true }).click();
  await expect(page.locator("#review-summary")).toContainText("Out of date");
  const staleURL = await page.locator("#review-audio").getAttribute("src");
  await generate();
  assert.notEqual(await page.locator("#review-audio").getAttribute("src"), staleURL);
  assert.notEqual(staleURL, original);
  report.steps.push("source edit marks old review stale; explicit regeneration creates another result");
  await control("slow");
  await page.getByRole("button", { name: "Generate review", exact: true }).click();
  await expect(page.locator("#review-cancel")).toBeVisible();
  await page.locator("#review-cancel").click();
  await expect(page.locator("#review-status")).toContainText("cancelled", { timeout: 10000 });
  await control("fast");
  report.steps.push("cancel real worker job and retain previous playable snapshot");
  await page.locator("#review-selection-start").fill("");
  await page.locator("#review-selection-end").fill("");
  await control("missing");
  await generate();
  await expect(page.locator("#review-summary")).toContainText("Incomplete");
  await expect(page.locator("#review-omissions li")).not.toHaveCount(0);
  const incompleteURL = await page.locator("#review-audio").getAttribute("src");
  await control("tts-failure");
  await page.getByRole("button", { name: "Generate review", exact: true }).click();
  await expect(page.locator("#review-status")).toContainText(/TTS|text.to.speech/i, { timeout: 10000 });
  assert.equal(await page.locator("#review-audio").getAttribute("src"), incompleteURL);
  report.steps.push("unresolved content uses configured local TTS fixture; missing TTS fails and preserves previous result");
  const selected = await page.locator("#review-snapshots").inputValue();
  const selectedLabel = await page.locator("#review-snapshots option:checked").textContent();
  await page.getByRole("button", { name: "Delete review", exact: true }).click();
  await expect(page.locator("#confirm-title")).toContainText(selectedLabel);
  await expect(page.locator("#confirm-message")).toContainText("backups");
  await expect(page.locator("#confirm-cancel")).toBeFocused();
  await page.locator("#confirm-cancel").click();
  await expect(page.locator("#review-snapshots")).toHaveValue(selected);
  await page.getByRole("button", { name: "Delete review", exact: true }).click();
  await page.locator("#confirm-accept").click();
  await expect(page.locator("#review-status")).toContainText("Review deleted.");
  await expect(page.locator("#review-snapshots")).toHaveValue("");
  await expect(page.locator("#review-result")).toBeHidden();
  report.steps.push("dated deletion confirms irrecoverable audio loss; cancelling preserves it; deletion leaves no replacement selected");
  assert.deepEqual(report.page_errors, []);
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.error = String(error);
  report.server_log = logs;
  process.exitCode = 1;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await new Promise((done) => { if (server.exitCode !== null) done(); else server.once("exit", done); });
  const reportPath = process.env.AUTODJ_REVIEW_AUDIT_REPORT || ".coord/programme_review_audit.json";
  await mkdir(resolve(reportPath, ".."), { recursive: true });
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  console.log(await readFile(reportPath, "utf8"));
  await rm(root, { recursive: true, force: true });
}
