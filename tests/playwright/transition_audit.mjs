// Cross-browser audit of AutoDJ transition effects.
//
// Loads the live web UI, captures console output, waits for an <audio>
// element, loads each worklet module into a fresh AudioContext to see
// whether it loads, and posts each effect to /api/transition.  The page's
// own worklet state is module-private and not read.  Output is written as
// a JSON report so we can diff worklet-load success and crossfade trigger
// logs across Chromium / Firefox / WebKit.

import { runAudit, validateTransitionAudit } from "./audit_helpers.mjs";

// Set AUTODJ_URL=http://host:port before running, e.g.:
//   AUTODJ_URL=http://localhost:8080 node tests/playwright/transition_audit.mjs
const BASE = process.env.AUTODJ_URL || "http://localhost:8080";
const ORIGIN = new URL(BASE).origin;
const EFFECTS = [
  "highpass_sweep", "lowpass_sweep", "bitcrusher", "freeze",
  "glitch", "reverse_reverb", "forward_spin", "pitch_swell", "pitch_fall",
];

export async function audit(name, launcher) {
  const browser = await launcher.launch({
    args: name === "chromium" ? ["--autoplay-policy=no-user-gesture-required"] : [],
  });
  try {
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

  // Wait for the page's <audio> element (reported as workletReady).
  const workletReady = await page.waitForFunction(() => {
    if (typeof window === "undefined") return false;
    return new Promise((resolve) => {
      const check = () => {
        const audio = document.querySelector("audio");
        if (!audio) return false;
        return true;
      };
      if (check()) resolve(true); else setTimeout(() => resolve(check()), 2000);
    });
  }, null, { timeout: 10000 }).catch(() => null);

  // The page's worklet state is module-private, so probe by loading the
  // same worklet URLs into our own audio context.
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

  // page.request is not a browser fetch, so it does not supply Origin for
  // state-changing calls.  Match the trusted origin of the page under audit.
  const transitions = {};
  for (const fx of EFFECTS) {
    const res = await page.request.post(`${ORIGIN}/api/transition`, {
      headers: { "Content-Type": "application/json", Origin: ORIGIN },
      data: JSON.stringify({ effect: fx }),
    });
    transitions[fx] = { status: res.status() };
  }

  return { name, workletReady: !!workletReady, probe, transitions, logs };
  } finally {
    await browser.close();
  }
}

await runAudit({
  audit,
  report: "transition_audit.json",
  validate: validateTransitionAudit,
});
