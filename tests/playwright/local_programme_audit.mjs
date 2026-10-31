// Existing-engine measurement, before the ownership UI is connected.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import { resolve } from "node:path";
import { chromium } from "@playwright/test";

const root = resolve(".codex-tmp/541");
await mkdir(root, { recursive: true });
const server = spawn("uv", ["run", "--frozen", "--no-sync", "python", "-m",
  "tests.playwright.local_programme_fixture", "--root", root], {
  env: { ...process.env, UV_CACHE_DIR: resolve(".codex-tmp/uv-cache") },
  stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
});
let browser;
let base;
const report = { date: new Date().toISOString(), os: `${os.type()} ${os.release()}`,
  node: process.version, fixture: "real audio-engine.js, synthetic media, delayed HTTP, headless output muted by Playwright",
  revision: execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8", windowsHide: true }).trim(),
  dirty: execFileSync("git", ["status", "--porcelain"], { encoding: "utf8", windowsHide: true }).trim(),
  errors: [] };
try {
  const port = await new Promise((done, reject) => {
    let output = "";
    const timeout = setTimeout(() => reject(new Error("Fixture startup timed out")), 20000);
    server.once("error", reject);
    server.once("exit", (code) => reject(new Error(`Fixture exited ${code}`)));
    server.stderr.on("data", (data) => process.stderr.write(data));
    server.stdout.on("data", (data) => {
      output += data;
      if (output.includes("\n")) { clearTimeout(timeout); done(JSON.parse(output.split("\n")[0]).port); }
    });
  });
  base = `http://127.0.0.1:${port}`;
  assert.equal(await (await fetch(`${base}/audit`)).text(),
    await readFile(resolve("src/autodj/static/index.html"), "utf8"));
  assert.equal(await (await fetch(`${base}/app.js`)).text(),
    'import * as engine from "/modules/audio-engine.js"; window.auditEngine = engine;');
  assert.equal(await (await fetch(`${base}/modules/theme-init.js`)).text(),
    await readFile(resolve("src/autodj/static/modules/theme-init.js"), "utf8"));
  browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required"] });
  report.chromium = browser.version();
  const page = await browser.newPage();
  page.on("pageerror", (error) => report.errors.push(String(error)));
  await page.addInitScript(() => {
    const Original = window.AudioContext;
    window.AudioContext = class extends Original {
      constructor() { super({ sampleRate: 44100 }); }
    };
  });
  await page.goto(`http://127.0.0.1:${port}/audit`);
  const measurement = page.evaluate(async () => {
    const engine = window.auditEngine;
    const context = engine.ensureAudioGraph();
    if (context.sampleRate !== 44100) throw new Error("Local control requires 44,100 Hz");
    await context.resume();
    engine.setApplyState(() => {});
    const deck = engine.decks[0];
    engine.setSrcOnDeck(deck, "coded-tone.wav");
    engine.setVolume(1);
    await deck.audio.play();
    const seekStart = window.performance.now();
    const sought = new Promise((done) => deck.audio.addEventListener("seeked", done, { once: true }));
    deck.audio.currentTime = 1;
    await sought;
    const seekMs = window.performance.now() - seekStart;
    const code = `class Probe extends AudioWorkletProcessor {
      constructor() { super(); this.pending = null; this.port.onmessage = e => { this.pending = e.data; }; }
      process(inputs) {
        const a = inputs[0]?.[0], b = inputs[1]?.[0];
        if (a && b && this.pending) {
          let x = 0, y = 0;
          for (let i = 0; i < a.length; i++) { x += a[i]*a[i]; y += b[i]*b[i]; }
          const ratio = Math.sqrt(y / Math.max(x, 1e-20));
          if (Math.abs(ratio - this.pending.target) < 0.01) {
            this.port.postMessage({ ms: (currentTime - this.pending.at)*1000, ratio }); this.pending = null;
          }
        }
        return true;
      }
    } registerProcessor('audit-probe', Probe);`;
    const url = URL.createObjectURL(new window.Blob([code], { type: "text/javascript" }));
    await context.audioWorklet.addModule(url);
    URL.revokeObjectURL(url);
    const probe = new window.AudioWorkletNode(context, "audit-probe", { numberOfInputs: 2 });
    deck.source.connect(probe, 0, 0);
    engine._master.connect(probe, 0, 1);
    const silent = context.createGain(); silent.gain.value = 0;
    probe.connect(silent).connect(context.destination);
    const times = [];
    for (let i = 0; i < 100; i++) {
      const target = i % 2 ? 0.8 : 0.2;
      let rendered;
      const gesture = new Promise(done => {
        document.addEventListener("keydown", () => {
          rendered = new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(new Error("Gain render timed out")), 2000);
            probe.port.onmessage = e => { clearTimeout(timeout); resolve(e.data.ms); };
          });
          probe.port.postMessage({ target, at: context.currentTime });
          engine.setVolume(target);
          // The fixture handler calls the real engine while the network waits.
          void fetch("/api/volume", { method: "POST", body: JSON.stringify({ volume: target }) });
          done();
        }, { once: true });
      });
      window.auditWaiting = i;
      await gesture;
      times.push(await rendered);
      window.auditCompleted = i;
    }
    const standby = engine.decks[1];
    const originalPlay = standby.audio.play;
    let rejected = false;
    standby.audio.play = () => { rejected = true; return Promise.reject(new window.DOMException("Audit rejection", "NotAllowedError")); };
    engine.startCrossfade("rejected.wav", 0.1, true);
    await new Promise(done => setTimeout(done, 400));
    const failureAdvanced = engine.activeIdx === 1;
    standby.audio.play = originalPlay;
    engine.stopAllDecks();
    const next = engine.decks[engine.activeIdx];
    engine.setSrcOnDeck(next, "tone-a.wav");
    next.gain.gain.value = 1;
    await next.audio.play();
    engine.startCrossfade("tone-b.wav", 0.2, true);
    await new Promise(done => setTimeout(done, 700));
    const crossfadePlaying = !engine.decks[engine.activeIdx].audio.paused;
    engine.stopAllDecks();
    times.sort((a, b) => a - b);
    return { sample_rate: context.sampleRate, base_latency_ms: context.baseLatency * 1000,
      gesture: "trusted Playwright keyboard event; fixture handler calls real engine",
      audio_clock_quantum_ms: 128 / context.sampleRate * 1000,
      gain_trials: times.length, gain_p50_ms: times[49], gain_p95_ms: times[94], gain_max_ms: times[99],
      gain_audio_clock_ms: times, seek_ms: seekMs, rejected_play_exercised: rejected,
      failed_play_advanced_deck: failureAdvanced, crossfade_playing: crossfadePlaying };
  });
  // Handle failure immediately while the driver is waiting for the next trial.
  let measurementError;
  measurement.catch(error => { measurementError = error; });
  for (let i = 0; i < 100; i++) {
    if (measurementError) throw measurementError;
    await page.waitForFunction(index => window.auditWaiting === index, i);
    await page.keyboard.press("ArrowRight");
    await page.waitForFunction(index => window.auditCompleted === index, i);
  }
  report.measurement = await measurement;
  assert.equal(report.measurement.gain_trials, 100);
  assert.ok(report.measurement.crossfade_playing);
  assert.ok(report.measurement.rejected_play_exercised);
  assert.equal(report.errors.length, 0);
  report.status = "measured";
} catch (error) {
  report.status = "failed";
  report.failure = String(error);
  process.exitCode = 1;
} finally {
  await browser?.close();
  if (base) await fetch(`${base}/shutdown`, { method: "POST" }).catch(() => {});
  else server.kill();
  server.stdout.destroy();
  server.stderr.destroy();
  await writeFile(resolve(root, "existing-engine.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
}
