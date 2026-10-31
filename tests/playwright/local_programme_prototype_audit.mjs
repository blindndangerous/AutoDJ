// ADR-0020-09: native ownership, browser decks, real encoded stream and recovery.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { appendFile, mkdir, readFile } from "node:fs/promises";
import { once } from "node:events";
import os from "node:os";
import { resolve } from "node:path";
import { chromium } from "@playwright/test";
import { finalizePrototypeReport } from "./local_programme_report.mjs";

const root = resolve(process.env.LOCAL_PROGRAMME_AUDIT_ROOT || "test-results/local-programme-prototype");
await mkdir(root, { recursive: true });
const report = { date: new Date().toISOString(), os: `${os.type()} ${os.release()}`, node: process.version,
  fixture: "Real Player, SQLite ledger, MixBus, FFmpeg, production browser modules; synthetic catalog and audit page shell",
  output: "Chromium headless output muted by Playwright; no acoustic measurement", errors: [] };
const cpuRate = Number(process.env.LOCAL_PROGRAMME_CPU_RATE || 1);
assert.ok(Number.isFinite(cpuRate) && cpuRate >= 1 && cpuRate <= 20, "CPU throttle must be between 1 and 20");
report.cpu_throttling_rate = cpuRate;
async function throttle(page) {
  if (cpuRate === 1) return;
  const session = await page.context().newCDPSession(page);
  await session.send("Emulation.setCPUThrottlingRate", { rate: cpuRate });
}
const fixture = spawn("uv", ["run", "--frozen", "--no-sync", "python", "-m",
  "tests.playwright.local_programme_prototype_fixture", "--root", root], {
  env: { ...process.env, UV_CACHE_DIR: resolve(".codex-tmp/uv-cache") },
  stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
});
let browser, base, reader, page;
let encoded = [];
let finished;
const sleep = ms => new Promise(done => setTimeout(done, ms));
async function until(predicate, label, timeout = 8000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await predicate()) return; await sleep(50); }
  throw new Error(`Timed out: ${label}`);
}
const state = async () => (await fetch(`${base}/audit-state`, { signal: globalThis.AbortSignal.timeout(5000) })).json();
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
  report.base = base;
  await until(async () => fetch(`${base}/audit-state`).then(r => r.ok, () => false), "fixture listening");
  assert.equal(await (await fetch(`${base}/audit`)).text(),
    await readFile(resolve("src/autodj/static/index.html"), "utf8"));
  assert.match(await (await fetch(`${base}/app.js`)).text(), /window.audit =/);
  assert.equal(await (await fetch(`${base}/modules/theme-init.js`)).text(),
    await readFile(resolve("src/autodj/static/modules/theme-init.js"), "utf8"));
  for (const name of ["bitcrusher", "stutter", "freeze", "glitch", "programme"]) {
    assert.equal(await (await fetch(`${base}/${name}-worklet.js`)).text(),
      await readFile(resolve(`src/autodj/static/${name}-worklet.js`), "utf8"));
  }
  for (const name of ["unknown", "PROGRAMME", "..%2Fprogramme"]) {
    assert.equal((await fetch(`${base}/${name}-worklet.js`)).status, 404);
  }
  const stream = await fetch(`${base}/stream`);
  assert.equal(stream.status, 200);
  reader = stream.body.getReader();
  finished = (async () => { for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    encoded.push(chunk.value);
  } })();
  browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"] });
  report.chromium = browser.version();
  const context = await browser.newContext();
  page = await context.newPage();
  await throttle(page);
  page.on("pageerror", error => report.errors.push(String(error)));
  await page.goto(`${base}/audit`);
  await page.locator("#take-local-control").waitFor({ state: "visible" });
  await page.evaluate(() => {
    window.audit.changes = [];
    const change = window.audit.owner.change;
    window.audit.owner.change = (state, reason) => {
      window.audit.changes.push({ state, reason }); change(state, reason);
    };
  });
  await page.locator("#take-local-control").focus();
  await page.addScriptTag({ path: resolve("node_modules/axe-core/axe.min.js") });
  report.axe = {};
  for (const theme of ["light", "dark"]) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
    const audit = await page.evaluate(async () => window.axe.run(document, {
      runOnly: {type:"tag", values:["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]},
    }));
    report.axe[theme] = audit.violations;
    assert.equal(audit.violations.length, 0, `axe ${theme}`);
  }
  report.controls = await page.locator("#take-local-control").evaluate(el => ({
    width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height,
    outline: window.getComputedStyle(el).outlineStyle,
  }));
  assert.ok(report.controls.height >= 24 && report.controls.width >= 24);
  assert.notEqual(report.controls.outline, "none");
  await page.locator("#take-local-control").focus();
  await page.keyboard.press("Enter");
  await until(async () => (await state()).ownership.state === "local", "takeover");
  assert.equal(await page.evaluate(() => document.activeElement.id), "take-local-control");
  report.takeover = await state();
  if (process.env.LOCAL_PROGRAMME_ACTIVE_AXE === "1") {
    report.active_axe = {};
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      const audit = await page.evaluate(async () => window.axe.run(document, {
        runOnly: {type:"tag", values:["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]},
      }));
      report.active_axe[theme] = {violations: audit.violations, ownership: (await state()).ownership};
      assert.equal(audit.violations.length, 0, `active axe ${theme}`);
      assert.equal(report.active_axe[theme].ownership.state, "local", `ownership during active axe ${theme}`);
    }
  }
  const second = await browser.newContext();
  const other = await second.newPage();
  await other.goto(`${base}/audit`);
  report.second_context_close = await other.evaluate(() => new Promise(resolve => {
    const socket = new window.WebSocket(`ws://${location.host}/api/local-programme/ws`);
    socket.onopen = () => socket.send(JSON.stringify({ type: "claim", version: 1, rate: 44100 }));
    socket.onclose = e => resolve(e.code);
  }));
  assert.equal(report.second_context_close, 4409);
  await second.close();
  await sleep(1000);
  // Private audition is attached after the feed split, exactly as playBrowserClip.
  report.monitor = await page.evaluate(async () => {
    const { engine, owner } = window.audit;
    const context = engine._ctx;
    const analyser = context.createAnalyser(); analyser.fftSize = 2048;
    engine._master.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const rms = async () => {
      await new Promise(done => setTimeout(done, 150));
      analyser.getFloatTimeDomainData(samples);
      return Math.sqrt(samples.reduce((sum, x) => sum + x*x, 0) / samples.length);
    };
    const before = await rms();
    const previewFrame = owner.transport.nextFrame;
    const preview = context.createOscillator(); preview.frequency.value = 5000;
    const gain = context.createGain(); gain.gain.value = .2;
    preview.connect(gain).connect(engine._master); preview.start();
    const previewLevel = await rms();
    await new Promise(done => setTimeout(done, 1000)); preview.stop();
    const muteFrame = owner.transport.nextFrame;
    engine.setMuted(true);
    const muted = await rms();
    await new Promise(done => setTimeout(done, 1000));
    const muteEnd = owner.transport.nextFrame;
    engine.setMuted(false);
    const pings = [];
    for (let i = 0; i < 7; i++) {
      const before = window.performance.now();
      const remote = await (await fetch("/audit-clock")).json();
      const after = window.performance.now();
      pings.push({ rtt: after - before, offset: remote.monotonic_ms - (before + after) / 2 });
    }
    pings.sort((a, b) => a.rtt - b.rtt);
    const audioClock = { frame: owner.clock.frame,
      monotonic_ms: window.performance.now() + pings[0].offset - (context.currentTime - owner.clock.time) * 1000,
      uncertainty_ms: pings[0].rtt / 2 + 128 / 44.1 + context.baseLatency * 1000 };
    return { before, preview: previewLevel, muted, sample_rate: context.sampleRate,
      previewFrame, muteFrame, muteEnd, audioClock, pings };
  });
  assert.equal(report.monitor.sample_rate, 44100);
  assert.ok(report.monitor.before > .001 && report.monitor.muted < 1e-8);
  assert.ok(report.monitor.preview > report.monitor.before);
  await page.locator("#btn-skip").click();
  await until(async () => (await state()).history.filter(row => row.event_id === report.takeover.history.at(-1)?.event_id).length > 0
    && (await state()).history.length >= report.takeover.history.length + 1, "browser transition");
  await sleep(2500);
  report.transition = await state();
  assert.ok(report.transition.current.endsWith("programme-2.wav"));
  assert.equal(report.transition.queue.length, 0);
  assert.equal(new Set(report.transition.history.map(row => row.event_id)).size, report.transition.history.length);
  await page.locator("#return-to-automation").click();
  await until(async () => {
    const s = await state();
    return s.ownership.state === "automation" && s.current !== report.transition.current;
  }, "explicit return to next unheard song");
  report.returned = await state();
  assert.equal(report.returned.connections, 1);
  assert.equal(report.returned.open_connections, 1);
  assert.equal(report.returned.encoder_generation, 0);
  await sleep(500);
  await page.locator("#take-local-control").click();
  await until(async () => (await state()).ownership.state === "local", "second takeover");
  await sleep(1000);
  const beforeLoss = await state();
  const killed = Date.now();
  await context.close();
  await until(async () => {
    const s = await state();
    return s.ownership.state === "automation" && s.current !== beforeLoss.current;
  }, "owner loss recovery to next unheard song");
  report.loss_recovery_ms = Date.now() - killed;
  report.recovered = await state();
  assert.equal(report.recovered.connections, 1);
  assert.equal(report.recovered.open_connections, 1);
  assert.equal(report.recovered.encoder_generation, 0);
  report.failure_recovery = {};
  for (const fault of ["suspend", "stalled-feed", "session-revoked"]) {
    await until(async () => {
      const s = await state();
      return s.ownership.state === "automation" && s.reason === "Automation";
    }, `${fault} previous recovery settled`);
    const faultContext = await browser.newContext();
    const faultPage = await faultContext.newPage();
    await throttle(faultPage);
    page = faultPage;
    await faultPage.goto(`${base}/audit`);
    await faultPage.evaluate(() => {
      window.audit.changes = [];
      const change = window.audit.owner.change;
      window.audit.owner.change = (state, reason) => {
        window.audit.changes.push({state, reason}); change(state, reason);
      };
    });
    await until(async () => faultPage.locator("#take-local-control").getAttribute("aria-disabled").then(value => value === "false"),
      `${fault} control ready`);
    await faultPage.locator("#take-local-control").click();
    await until(async () => (await state()).ownership.state === "local", `${fault} takeover`);
    const started = Date.now();
    if (fault === "suspend") await faultPage.evaluate(() => window.audit.engine._ctx.suspend());
    else if (fault === "stalled-feed") await faultPage.evaluate(() => {
      // Continue recycling capture and sending heartbeat, but lose every PCM packet.
      window.audit.owner.transport.send = () => true;
    });
    else assert.equal((await fetch(`${base}/audit-revoke-session`, {method:"POST"})).status, 200);
    await until(async () => (await state()).ownership.state === "automation", `${fault} recovery`);
    report.failure_recovery[fault] = { milliseconds: Date.now() - started, state: await state() };
    assert.equal(report.failure_recovery[fault].state.connections, 1);
    assert.equal(report.failure_recovery[fault].state.open_connections, 1);
    assert.equal(report.failure_recovery[fault].state.encoder_generation, 0);
    await faultContext.close();
  }
  assert.equal(report.errors.length, 0);
  report.status = "passed";
} catch (error) {
  report.status = "failed"; report.failure = String(error); process.exitCode = 1;
  if (base) report.last_state = await state().catch(() => null);
  if (page && !page.isClosed()) report.browser_state = await page.evaluate(() => ({
    changes: window.audit?.changes, reason: window.audit?.owner.transport?.reason,
    status: document.getElementById("local-programme-status")?.textContent,
  })).catch(() => null);
} finally {
  await finalizePrototypeReport({ root, report, encoded, cleanup: async () => {
    await browser?.close();
    if (base) {
      const exited = once(fixture, "exit");
      await fetch(`${base}/shutdown`, { method: "POST" }).catch(() => {});
      await Promise.race([exited, sleep(10000)]);
    } else fixture.kill();
    if (finished) await finished.catch(() => {});
  } });
  if (report.status === "failed") process.exitCode = 1;
  console.log(JSON.stringify(report, null, 2));
}
