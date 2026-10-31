// Actual edition/review APIs and production controls, deterministic local audio.
// Normal app startup and real microphone/NVDA evidence are separate rehearsals.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { expect } from "@playwright/test";
import { runAudit } from "./audit_helpers.mjs";

async function audit(name, launcher) {
  const root = await mkdtemp(join(tmpdir(), "autodj-edition-browser-"));
  const port = process.env.AUTODJ_EDITION_AUDIT_PORT || "8895";
  const base = `http://127.0.0.1:${port}`;
  const python = process.env.AUTODJ_PYTHON || resolve(process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
  const server = spawn(python, ["tests/playwright/prepared_edition_fixture.py", "--root", root, "--port", port], {
    env: { ...process.env, PYTHONPATH: [resolve("src"), resolve(".")].join(delimiter), TMPDIR: root,
      PATH: [dirname(python), process.env.PATH].join(delimiter) },
    stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
  });
  let logs = "", browser;
  server.stdout.on("data", (data) => { logs += data; });
  server.stderr.on("data", (data) => { logs += data; });
  server.on("error", (error) => { logs += String(error); });
  const report = { browser: name, environment: "Synthetic stopped station; normal lifespan disabled",
    steps: [], page_errors: [] };

  function wav() {
    const frames = 44100, bytes = Buffer.alloc(44 + frames * 2);
    bytes.write("RIFF"); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write("WAVEfmt ", 8);
    bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22);
    bytes.writeUInt32LE(44100, 24); bytes.writeUInt32LE(88200, 28);
    bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34);
    bytes.write("data", 36); bytes.writeUInt32LE(frames * 2, 40);
    for (let i = 0; i < frames; i += 1) bytes.writeInt16LE(Math.round(4000 * Math.sin(2 * Math.PI * 880 * i / 44100)), 44 + i * 2);
    return bytes;
  }

  try {
    await expect.poll(async () => {
      if (server.exitCode !== null) throw new Error(logs);
      try { return (await fetch(`${base}/edition-audit`)).status; } catch { return 0; }
    }, { timeout: 20000 }).toBe(200);
    browser = await launcher.launch();
    report.browser_version = browser.version();
    const page = await browser.newPage({ acceptDownloads: true });
    async function takeAction(action) {
      await page.locator("#edition-takes button[aria-haspopup='menu']").click();
      return page.locator(`#edition-takes button[data-action='${action}']`);
    }
    async function takeCellReturn(action = "options") {
      const cell = page.locator(`#edition-takes td[data-action="${action}"]`);
      await expect(cell).toBeFocused();
      await page.keyboard.press("ArrowLeft");
      assert.equal(await cell.evaluate((element) => document.activeElement === element.previousElementSibling), true);
      await page.keyboard.press("ArrowRight");
      await expect(cell).toBeFocused();
    }
    page.on("pageerror", (error) => report.page_errors.push(String(error)));
    await page.goto(`${base}/edition-audit`);
    await page.waitForFunction(() => Boolean(window.editionAudit));
    await page.locator("#shows-card > summary").click();
    await page.locator('#shows-list td[data-action="options"]').focus();
    await page.keyboard.press("Enter");
    await page.locator('#shows-list button[data-action="edit"]').click();
    const editor = page.locator('#shows-form-dialog');
    await expect(editor).toHaveAccessibleName(await editor.evaluate((element) => document.getElementById(element.getAttribute("aria-labelledby")).textContent.trim()));
    await expect(editor).toHaveAccessibleDescription("Use Tab to move between fields and actions.");
    await page.keyboard.press("Escape");
    await expect(page.locator('#shows-list td[data-action="options"]')).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(page.locator('#shows-list td[data-action="open"]')).toBeFocused();
    await page.locator("#shows-list button[data-action='open']").click();
    await expect(page.locator("#shows-editor")).toBeVisible();
    await page.locator("#edition-card > summary").click();
    await page.locator("#edition-new-name").fill("Tomorrow browser edition");
    await page.locator("#edition-create").click();
    await expect(page.locator("#edition-editor")).toBeVisible();
    await expect(page.locator("#edition-summary")).toContainText("Revision 1");
    const editions = await (await page.request.get(`${base}/api/editions`)).json();
    const identity = editions.editions[0].id, url = `${base}/api/editions/${identity}`;
    async function view() { return (await page.request.get(url)).json(); }
    async function control(path, action) {
      const response = await page.request.post(`${base}/${path}`, { headers: { Origin: base }, data: { action } });
      assert.equal(response.status(), 200, await response.text());
    }
    report.steps.push("stopped station: computed Show editor name/description; Escape to exact Options cell and arrow continuation; open saved show and create independent prepared edition");
    await page.locator("#edition-position").selectOption("transition");
    await expect(page.locator("#edition-transition option").first()).toHaveText(/Item 1, .* into item 2,/);
    await expect(page.locator("#edition-insert option").first()).toHaveText(/^Before item 1,/);
    await page.locator("#edition-placement").selectOption("standalone");
    const source = wav();
    await page.locator("#edition-file").setInputFiles({ name: "speech.wav", mimeType: "audio/wav", buffer: source });
    await page.locator("#edition-import").click();
    await expect(page.locator("#edition-takes")).toContainText("Needs review");
    const imported = await view(), take = imported.takes[0];
    assert.equal(take.approval, null);
    await page.addScriptTag({ path: resolve("node_modules/axe-core/axe.min.js") });
    const accessibility = await page.evaluate(async () => {
      const result = await window.axe.run("#edition-editor", { runOnly: {
        type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"],
      } });
      return result.violations.map((entry) => ({ id: entry.id, nodes: entry.nodes.map((node) => node.target) }));
    });
    assert.deepEqual(accessibility, []);
    const cells = page.locator('#edition-occurrences tr[data-row-key]:first-child > td');
    await cells.first().focus();
    for (let index = 0; index < await cells.count(); index += 1) {
      if (index) await page.keyboard.press("ArrowRight");
      const cell = cells.nth(index);
      if (!await cell.locator("button, input, select").count()) {
        await expect(cell).toHaveAccessibleName((await cell.textContent()).replace(/\s+/g, " ").trim());
      }
    }
    await takeAction("reattach");
    await page.keyboard.press("Escape");
    await takeCellReturn();
    report.steps.push("production edition: axe WCAG2.2 A/AA; computed data names; Escape returns to exact Options cell with arrow continuation");
    await page.locator("#edition-takes button[data-action='listen']").click();
    await expect(page.locator("#edition-audio")).toHaveAttribute("src", new RegExp(`/assets/${take.asset_id}$`));
    await page.locator("#edition-audio").focus(); await page.keyboard.press("Space");
    await expect.poll(() => page.locator("#edition-audio").evaluate((audio) => audio.currentTime)).toBeGreaterThan(0);
    await page.keyboard.press("Space");
    const asset = await page.request.get(`${url}/assets/${take.asset_id}`);
    assert.equal(asset.status(), 200);
    assert.equal((await asset.body()).subarray(0, 4).toString(), "RIFF");
    report.steps.push("import real WAV, listen through native audio, retrieve source asset; audition never approves");
    // Stub only microphone capture; exercise the production recording/dialog UI.
    await page.evaluate((bytes) => {
      navigator.mediaDevices.getUserMedia = async () => ({ getTracks: () => [{ stop() {} }] });
      window.MediaRecorder = class extends window.EventTarget {
        mimeType = "audio/wav";
        start() {}
        stop() {
          const event = new Event("dataavailable");
          event.data = new window.Blob([new Uint8Array(bytes)], { type: "audio/wav" });
          this.dispatchEvent(event); this.dispatchEvent(new Event("stop"));
        }
      };
    }, [...source]);
    await page.locator("#edition-record").click();
    await expect(page.locator("#edition-record")).toHaveText("Stop recording");
    await page.locator("#edition-record").click();
    await expect(page.locator("#edition-save-take")).toBeEnabled();
    await page.locator("#edition-takes button[data-action='listen']").click();
    await expect(page.locator("#edition-save-take")).toBeEnabled();
    await (await takeAction("rerecord")).click();
    await expect(page.locator("#confirm-title")).toHaveText("Discard unsaved recording?");
    await expect(page.locator("#confirm-cancel")).toBeFocused();
    await page.locator("#confirm-cancel").click();
    await takeCellReturn();
    await expect(page.locator("#edition-save-take")).toBeEnabled();
    await page.locator("#shows-close").click();
    await expect(page.locator("#confirm-dialog")).toBeVisible();
    await page.locator("#confirm-cancel").click();
    await expect(page.locator("#edition-editor")).toBeVisible();
    await expect(page.locator("#edition-save-take")).toBeEnabled();
    await page.locator("#edition-discard").click();
    await page.locator("#confirm-accept").click();
    await expect(page.locator("#edition-save-take")).toBeDisabled();
    report.steps.push("synthetic recording survives saved-take listening and cancelled re-record/Close show; explicit discard confirms with safe focus");
    await (await takeAction("review")).click();
    await expect(page.locator("#review-status")).toContainText("Review ready", { timeout: 20000 });
    await expect(page.locator("#review-summary")).toContainText("Pending speech included");
    assert.equal((await view()).takes[0].approval, null);
    await page.getByRole("button", { name: "Return to edition", exact: true }).click();
    const approve = await takeAction("approve");
    await expect(approve).toHaveAttribute("aria-disabled", "false"); await approve.click();
    await expect(page.locator("#edition-takes")).toContainText("ready");
    await takeCellReturn("listen");
    assert.ok((await view()).takes[0].approval);
    report.steps.push("pending target full-context real OVC generation returns to edition; only explicit approval marks ready");
    await page.locator("#edition-name").fill("Unrelated renamed edition");
    await page.locator("#edition-save-name").click();
    await expect(page.locator("#edition-summary")).toContainText("Unrelated renamed edition");
    assert.equal((await view()).takes[0].status, "ready");
    await control("edition-audit-control", "cue");
    await page.locator("#edition-load").click();
    await expect(page.locator("#edition-takes")).toContainText("Needs review");
    report.steps.push("unrelated edition rename preserves approval; relevant adjacent cue change invalidates it");
    const fresh = await view();
    const edited = await page.request.put(url, { headers: { Origin: base }, data: {
      expected_revision: fresh.revision, name: "External edition edit",
    } });
    assert.equal(edited.status(), 200, await edited.text());
    await page.locator("#edition-name").fill("Stale browser edit");
    await page.locator("#edition-save-name").click();
    await expect(page.locator("#edition-status")).toContainText("reload");
    await expect(page.locator("#edition-load")).toBeFocused();
    await expect(page.locator("#edition-load")).not.toHaveAttribute("aria-invalid");
    assert.equal((await view()).name, "External edition edit");
    await page.locator("#edition-load").click();
    await expect(page.locator("#edition-name")).toHaveValue("External edition edit");
    report.steps.push("stale revision rejected with reload focus; existing edition/audio preserved");
    await control("audit-control", "slow");
    const submitted = page.waitForResponse((response) => response.url() === `${base}/api/reviews`
      && response.request().method() === "POST");
    await (await takeAction("review")).click();
    const pendingJob = await (await submitted).json();
    await expect(page.locator("#review-cancel")).toBeVisible();
    await page.evaluate(() => window.editionAudit.clear());
    await control("audit-control", "fast");
    await expect.poll(async () => (await (await page.request.get(`${base}/api/reviews/jobs/${pendingJob.id}`)).json()).status,
      { timeout: 15000 }).toBe("done");
    await expect(page.locator("#review-audio")).not.toHaveAttribute("src");
    await expect(page.locator("#edition-audio")).not.toHaveAttribute("src");
    await expect(page.locator("#edition-editor")).toBeHidden();
    report.steps.push("protected-context clear during delayed generation drops old result and stops private players");
    assert.deepEqual(report.page_errors, []);
    report.passed = true;
  } catch (error) {
    report.passed = false; report.error = String(error); report.server_log = logs;
  } finally {
    await browser?.close(); server.kill("SIGTERM");
    await new Promise((done) => { if (server.exitCode !== null || !server.pid) done(); else server.once("exit", done); });
    assert.equal(dirname(resolve(root)), resolve(tmpdir()));
    await rm(root, { recursive: true, force: true });
  }
  return report;
}

const report = process.env.AUTODJ_EDITION_AUDIT_REPORT || ".coord/prepared_edition_audit.json";
await mkdir(dirname(resolve(report)), { recursive: true });
process.env.AUTODJ_BROWSERS ||= "chromium,firefox";
await runAudit({ audit, report, validate: (name, result) => assert.equal(result.passed, true, `${name}: ${result.error || "edition audit failed"}`) });
