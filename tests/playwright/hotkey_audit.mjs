// Hotkey + transition-duration regression audit.
//
// Covers the JS-only fixes shipped after the bigger transition refactor:
//   - Single keydown listener (window capture) — duplicate handler removed
//   - _isTypingTarget narrowed to text-entry inputs only
//   - e.repeat guard + press-latch (NVDA/IME repeat suppression)
//   - <dialog> shortcuts modal w/ showModal + Close-button focus
//   - _effectDurationFor used uniformly by gain ramp, applyTransitionFx,
//     and cleanup setTimeout in startCrossfade.
//
// Run:
//   AUTODJ_URL=http://192.168.50.40:8082 node tests/playwright/hotkey_audit.mjs

import {
  hotkeySourceChecks,
  runAudit,
  validateHotkeyAudit,
} from "./audit_helpers.mjs";

const BASE = process.env.AUTODJ_URL || "http://localhost:8080";

export async function audit(name, launcher) {
  const browser = await launcher.launch({
    args: name === "chromium" ? ["--autoplay-policy=no-user-gesture-required"] : [],
  });
  try {
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`[console] ${msg.text()}`);
  });

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 20000 });

  // ----------------------------------------------------------------
  // 1. Static source checks — fixes deployed?
  // ----------------------------------------------------------------
  const source = await page.evaluate(async () => {
    const paths = [
      "/modules/hotkeys.js",
      "/modules/audio-engine.js",
      "/modules/dom-helpers.js",
    ];
    const [hotkeys, audio, domHelpers] = await Promise.all(paths.map(async (path) => {
      const response = await fetch(path);
      if (!response.ok) throw new Error(`${path} returned ${response.status}`);
      return response.text();
    }));
    return { hotkeys, audio, domHelpers };
  });

  const sourceChecks = hotkeySourceChecks(source);

  // ----------------------------------------------------------------
  // 2. DOM checks — modal in place?
  // ----------------------------------------------------------------
  const dom = await page.evaluate(() => ({
    modalExists: !!document.getElementById("hotkey-help-modal"),
    legacyDetailsGone: !document.getElementById("hotkey-help-card"),
    btnShortcutsExists: !!document.getElementById("btn-shortcuts"),
    btnShortcutsCloseExists: !!document.getElementById("btn-shortcuts-close"),
    modalAriaLabelledBy:
      document.getElementById("hotkey-help-modal") &&
      document.getElementById("hotkey-help-modal").getAttribute("aria-labelledby"),
  }));

  // ----------------------------------------------------------------
  // 3. Behavioural checks — modal open/close + hotkey latch
  // ----------------------------------------------------------------
  // Unlock the audio graph (most browsers need a user gesture).
  await page.click("#btn-pause").catch(() => {});
  await page.waitForTimeout(800);

  const behaviour = await page.evaluate(async () => {
    const modal = document.getElementById("hotkey-help-modal");
    const out = {};

    // Modal closed initially
    out.modalInitiallyClosed = !modal.open;

    // ? key opens it + focuses the Close button
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "?", bubbles: true, cancelable: true }),
    );
    await new Promise((r) => setTimeout(r, 200));
    out.modalOpenAfterQuestion = modal.open;
    out.closeBtnFocused =
      document.activeElement === document.getElementById("btn-shortcuts-close");

    // Close button closes it
    document.getElementById("btn-shortcuts-close").click();
    await new Promise((r) => setTimeout(r, 100));
    out.modalClosedAfterCloseBtn = !modal.open;

    // Toolbar trigger button opens it again
    document.getElementById("btn-shortcuts").click();
    await new Promise((r) => setTimeout(r, 100));
    out.modalOpenAfterTrigger = modal.open;
    if (modal.open) modal.close();

    // ----- Press-latch: 5 keydowns w/o keyup => 1 click -----
    let shuffleClicks = 0;
    const sBtn = document.getElementById("btn-shuffle");
    const orig = sBtn.click.bind(sBtn);
    sBtn.click = () => { shuffleClicks++; };
    for (let i = 0; i < 5; i++) {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "s", bubbles: true, cancelable: true }),
      );
    }
    await new Promise((r) => setTimeout(r, 100));
    out.shuffleClicksLatched = shuffleClicks;
    // Release + press again => +1
    document.dispatchEvent(
      new KeyboardEvent("keyup", { key: "s", bubbles: true, cancelable: true }),
    );
    await new Promise((r) => setTimeout(r, 50));
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "s", bubbles: true, cancelable: true }),
    );
    await new Promise((r) => setTimeout(r, 100));
    out.shuffleClicksAfterRelease = shuffleClicks;
    sBtn.click = orig;
    document.dispatchEvent(
      new KeyboardEvent("keyup", { key: "s", bubbles: true, cancelable: true }),
    );

    // ----- Native range owns keyboard input -----
    // Dispatch M from the focused range itself.  The page hotkey must
    // leave every key from native controls untouched.
    let muteClicks = 0;
    const mBtn = document.getElementById("btn-mute");
    const origM = mBtn.click.bind(mBtn);
    mBtn.click = () => { muteClicks++; };
    const vol = document.getElementById("vol");
    vol.focus();
    vol.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "m", bubbles: true, cancelable: true,
      }),
    );
    await new Promise((r) => setTimeout(r, 100));
    out.muteClicksFromSliderFocus = muteClicks;
    mBtn.click = origM;
    vol.dispatchEvent(
      new KeyboardEvent("keyup", { key: "m", bubbles: true, cancelable: true }),
    );

    // ----- Text input SHOULD suppress -----
    let pauseClicks = 0;
    const pBtn = document.getElementById("btn-pause");
    const origP = pBtn.click.bind(pBtn);
    pBtn.click = () => { pauseClicks++; };
    const search = document.getElementById("search-input");
    if (search) {
      search.focus();
      search.dispatchEvent(
        new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }),
      );
      await new Promise((r) => setTimeout(r, 100));
      out.pauseClicksFromSearchInput = pauseClicks;
      search.blur();
    }
    pBtn.click = origP;

    return out;
  });

  return { source: sourceChecks, dom, behaviour, errors };
  } finally {
    await browser.close();
  }
}

await runAudit({
  audit,
  report: "hotkey_audit_report.json",
  validate: validateHotkeyAudit,
});
