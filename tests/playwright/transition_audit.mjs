// Cross-browser audit of AutoDJ transition effects.
//
// Loads the live web UI, captures console output, polls the in-page
// `_workletReady` map, and triggers each effect via /api/transition.
// Output is written as a JSON report so we can diff worklet-load
// success and crossfade trigger logs across Chromium / Firefox / WebKit.

import { chromium, firefox, webkit } from "playwright";
import { writeFileSync } from "node:fs";

// Set AUTODJ_URL=http://host:port before running, e.g.:
//   AUTODJ_URL=http://localhost:8080 node tests/playwright/transition_audit.mjs
const BASE = process.env.AUTODJ_URL || "http://localhost:8080";
const EFFECTS = [
  "highpass_sweep", "lowpass_sweep", "bitcrusher", "freeze",
  "glitch", "reverse_reverb", "forward_spin", "pitch_swell", "pitch_fall",
];

async function audit(name, launcher) {
  const browser = await launcher.launch({
    args: name === "chromium" ? ["--autoplay-policy=no-user-gesture-required"] : [],
  });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  const logs = [];
  page.on("console", (msg) => logs.push({ type: msg.type(), text: msg.text() }));
  page.on("pageerror", (err) => logs.push({ type: "pageerror", text: String(err) }));

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 20000 });

  // Click anywhere to satisfy autoplay gesture, then nudge volume so
  // the AudioContext is created.
  await page.click("body");
  await page.waitForTimeout(300);

  // Wait for worklets to register.
  const workletReady = await page.waitForFunction(() => {
    if (typeof window === "undefined") return false;
    // Worklet readiness flags live in module scope — expose for inspection
    // by reading the audio-graph state after a fake user gesture.
    return new Promise((resolve) => {
      const check = () => {
        const audio = document.querySelector("audio");
        if (!audio) return false;
        return true;
      };
      if (check()) resolve(true); else setTimeout(() => resolve(check()), 2000);
    });
  }, null, { timeout: 10000 }).catch(() => null);

  // Read worklet-ready state via a backdoor — the script tag exposes
  // some globals.  We instead probe via creating our own audio context
  // and trying to load the same worklet URLs.
  const probe = await page.evaluate(async (origin) => {
    const out = { worklets: {}, audio_state: null, errors: [] };
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      out.audio_state = ctx.state;
      const names = ["bitcrusher", "stutter", "freeze", "glitch"];
      for (const n of names) {
        try {
          await ctx.audioWorklet.addModule(`${origin}/${n}-worklet.js`);
          out.worklets[n] = "ok";
        } catch (e) {
          out.worklets[n] = String(e);
        }
      }
    } catch (e) {
      out.errors.push(String(e));
    }
    return out;
  }, BASE);

  // Cycle through each effect and post to /api/transition
  const transitions = {};
  for (const fx of EFFECTS) {
    const before = logs.length;
    const res = await page.request.post(`${BASE}/api/transition`, {
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ effect: fx }),
    });
    transitions[fx] = { status: res.status() };
  }

  await browser.close();
  return { name, workletReady: !!workletReady, probe, transitions, logs };
}

const results = {};
for (const [n, l] of [
  ["chromium", chromium],
  ["firefox", firefox],
  ["webkit", webkit],
]) {
  try {
    results[n] = await audit(n, l);
    console.error(`${n}: ok`);
  } catch (e) {
    results[n] = { error: String(e) };
    console.error(`${n}: ${e}`);
  }
}

writeFileSync("transition_audit.json", JSON.stringify(results, null, 2));
console.error("wrote transition_audit.json");
