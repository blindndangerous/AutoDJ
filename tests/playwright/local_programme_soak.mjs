// Long-running platform evidence, separate from the bounded integration gate.
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { appendFile, mkdir, writeFile } from "node:fs/promises";
import { createWriteStream } from "node:fs";
import { once } from "node:events";
import os from "node:os";
import { resolve } from "node:path";
import { chromium } from "@playwright/test";
import { instrumentCapture, captureReport } from "./local_programme_diagnostics.mjs";

const mode = process.argv[2] || "idle";
assert.ok(["idle", "load"].includes(mode));
const seconds = Number(process.env.LOCAL_PROGRAMME_SOAK_SECONDS || 1800);
const root = resolve(process.env.LOCAL_PROGRAMME_SOAK_ROOT || `.codex-tmp/541/soak-${mode}`);
await mkdir(root, { recursive: true });
const report = { date: new Date().toISOString(), mode, requested_seconds: seconds,
  os: `${os.type()} ${os.release()}`, node: process.version,
  load: mode === "load" ? "Browser main thread busy 6 ms every 50 ms (12% nominal)" : "No injected load",
  samples: [], errors: [] };
const fixture = spawn("uv", ["run", "--frozen", "--no-sync", "python", "-m",
  "tests.playwright.local_programme_prototype_fixture", "--root", root], {
  env: { ...process.env, UV_CACHE_DIR: resolve(".codex-tmp/uv-cache"),
    LOCAL_PROGRAMME_DIAGNOSTICS_FILE: resolve(root, "worklet-instrumented.js") },
  stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
});
const sleep = ms => new Promise(done => setTimeout(done, ms));
let base, browser, page, copying, began;
const state = async () => (await fetch(`${base}/audit-state`, {signal:globalThis.AbortSignal.timeout(5000)})).json();
try {
  const port = await new Promise((done, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("Fixture startup timed out")), 30000);
    fixture.once("error", reject);
    fixture.once("exit", code => reject(new Error(`Fixture exited ${code}`)));
    fixture.stderr.on("data", data => { void appendFile(resolve(root, "fixture.stderr"), data); });
    fixture.stdout.on("data", data => {
      output += data;
      if (output.includes("\n")) { clearTimeout(timer); done(JSON.parse(output.split("\n")[0]).port); }
    });
  });
  base = `http://127.0.0.1:${port}`;
  const stream = await fetch(`${base}/stream`);
  assert.equal(stream.status, 200);
  const output = createWriteStream(resolve(root, "broadcast.mp3"));
  copying = (async () => {
    for await (const chunk of stream.body) {
      if (!output.write(chunk)) await once(output, "drain");
    }
    output.end(); await once(output, "finish");
  })();
  browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"] });
  report.chromium = browser.version();
  page = await browser.newPage();
  await instrumentCapture(page, resolve(root, "worklet-instrumented.js"));
  page.on("pageerror", error => report.errors.push(String(error)));
  await page.goto(`${base}/audit`);
  await page.evaluate(() => {
    window.audit.changes = [];
    const change = window.audit.owner.change;
    window.audit.owner.change = (state, reason) => {
      if (state === "recovering") window.captureDiagnostics.endedAt ??= window.performance.now();
      window.audit.changes.push({state, reason, time: window.performance.now()});
      change(state, reason);
    };
  });
  await page.locator("#take-local-control").click();
  const deadline = Date.now() + 8000;
  while ((await state()).ownership.state !== "local") {
    if (Date.now() > deadline) throw new Error("Takeover timed out");
    await sleep(50);
  }
  began = Date.now();
  await sleep(100);
  assert.ok((await captureReport(page)).recycle_ms.count > 0, "Capture instrumentation must be active");
  if (mode === "load") await page.evaluate(() => {
    window.audit.load = setInterval(() => {
      const end = window.performance.now() + 6;
      while (window.performance.now() < end) { /* Deliberate controlled load. */ }
    }, 50);
  });
  while (Date.now() - began < seconds * 1000) {
    await sleep(1000);
    const sample = await state();
    report.samples.push({ seconds: (Date.now() - began) / 1000,
      ownership: sample.ownership, high_water_frames: sample.high_water_frames,
      history_count: sample.history.length, current: sample.current,
      connections: sample.connections, encoder_generation: sample.encoder_generation });
    assert.equal(sample.ownership.state, "local", "Ownership lost during trial");
    assert.equal(sample.connections, 1);
    assert.equal(sample.open_connections, 1);
    assert.equal(sample.encoder_generation, 0);
    assert.ok(sample.high_water_frames <= 8820);
    if (report.samples.length % 30 === 0) {
      await writeFile(resolve(root, "progress.json"), JSON.stringify(report, null, 2));
      console.log(`${mode}: ${report.samples.at(-1).seconds.toFixed(1)} seconds, ${sample.history.length} starts`);
    }
  }
  report.status = "passed";
} catch (error) {
  report.status = "failed"; report.failure = String(error); process.exitCode = 1;
} finally {
  report.observed_seconds = began ? (Date.now() - began) / 1000 : 0;
  if (base && report.status === "failed") {
    const end = Date.now() + 4000;
    while (Date.now() < end) {
      const current = await state().catch(() => null);
      if (!current || current.ownership.state === "automation") break;
      await sleep(50);
    }
  }
  if (base && report.status === "passed") {
    try {
      report.trial_end_state = await state();
      // Large diagnostic exports must not stall the still-performing page.
      // Return normally first, keeping the existing HTTP listener connected.
      await page.evaluate(() => window.audit.owner.release());
      const end = Date.now() + 5000;
      while ((await state()).ownership.state !== "automation") {
        assert.ok(Date.now() <= end, "Post-trial return timed out");
        await sleep(50);
      }
      report.returned = await state();
      assert.equal(report.returned.connections, 1);
      assert.equal(report.returned.open_connections, 1);
      assert.equal(report.returned.encoder_generation, 0);
    } catch (error) {
      report.status = "failed"; report.failure = String(error); process.exitCode = 1;
    }
  }
  if (page && !page.isClosed()) report.browser = await page.evaluate(() => ({
    status: document.getElementById("local-programme-status")?.textContent,
    sender_reason: window.audit?.owner.transport?.reason,
    changes: window.audit?.changes,
  })).catch(() => null);
  if (page && !page.isClosed()) report.capture = await captureReport(page).catch(() => null);
  if (base) report.final_state = await state().catch(() => null);
  await browser?.close();
  if (base) {
    const exited = once(fixture, "exit");
    await fetch(`${base}/shutdown`, { method: "POST" }).catch(() => {});
    await Promise.race([exited, sleep(10000)]);
  } else fixture.kill();
  await copying?.catch(error => { report.errors.push(String(error)); });
  if (copying) {
    try {
      execFileSync("ffmpeg", ["-y", "-hide_banner", "-loglevel", "error", "-i", resolve(root, "broadcast.mp3"),
        "-ar", "44100", "-ac", "2", "-c:a", "pcm_f32le", resolve(root, "broadcast.wav")], {windowsHide:true});
      const measured = execFileSync("uv", ["run", "--frozen", "--no-sync", "python", "-m",
        "tests.playwright.local_programme_measure", "--endurance", resolve(root, "feed-1.wav"),
        resolve(root, "broadcast.wav")], {encoding:"utf8", windowsHide:true,
        env:{...process.env, UV_CACHE_DIR:resolve(".codex-tmp/uv-cache")}, maxBuffer:4*1024*1024});
      await writeFile(resolve(root, "signal.json"), measured);
      const {chunks: _chunks, ...signal} = JSON.parse(measured);
      report.signal = signal;
      assert.equal(signal.dropouts, 0, "Decoded broadcast dropouts");
      assert.equal(signal.unmatched_windows, 0, "Unmatched broadcast windows");
      assert.equal(signal.channel_swap, false, "Broadcast channel order");
      assert.ok(Math.abs(signal.drift_ppm) < 200, "Broadcast clock drift");
    } catch (error) {
      report.errors.push(String(error)); report.status = "failed"; process.exitCode = 1;
    }
  }
  report.ffmpeg = execFileSync("ffmpeg", ["-version"], { encoding: "utf8", windowsHide: true }).split("\n")[0];
  await writeFile(resolve(root, "audit.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({status:report.status, mode, observed_seconds:report.observed_seconds, failure:report.failure}));
}
