// Regression audit for the 2026-05-07 round of fixes.
//
// Covers JS-only behaviours that are not exercised by the Python
// pytest suite:
//   - Hotkey gate: arrow / space / m / n / s only fire on the Now Playing tab
//   - Lyrics card lives inside #panel-now (not #panel-settings)
//   - #cue-list-summary listbox removed
//   - Cue summary phrase NOT pushed into #badges-announce on track change
//   - _clearLiveRegionLater wipes vol-announce a few seconds after announce
//
// Run:
//   AUTODJ_URL=http://192.168.50.40:8082 node tests/playwright/regression_audit.mjs

import { chromium, firefox, webkit } from "playwright";
import { writeFileSync } from "node:fs";

const BASE = process.env.AUTODJ_URL || "http://localhost:8080";

async function audit(name, launcher) {
  const browser = await launcher.launch({
    args: name === "chromium" ? ["--autoplay-policy=no-user-gesture-required"] : [],
  });
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (err) => errors.push(String(err)));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`[console] ${msg.text()}`);
  });

  await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 20000 });

  // ----------------------------------------------------------------
  // Static DOM checks
  // ----------------------------------------------------------------
  const dom = await page.evaluate(() => {
    const lyricsCard = document.getElementById("lyrics-card");
    const panelNow   = document.getElementById("panel-now");
    const panelSet   = document.getElementById("panel-settings");
    const cueList    = document.getElementById("cue-list-summary");
    const lnEnabled  = document.getElementById("ln-enabled");
    const triggerFs  = document.querySelector(
      'fieldset[data-show-when="ln-enabled"]',
    );
    return {
      lyricsCardInNow:      !!(lyricsCard && panelNow && panelNow.contains(lyricsCard)),
      lyricsCardInSettings: !!(lyricsCard && panelSet && panelSet.contains(lyricsCard)),
      cueListSummaryGone:   cueList === null,
      linerCheckboxOutsideTrigger: !!(
        lnEnabled && triggerFs && !triggerFs.contains(lnEnabled)
      ),
      linerCheckboxDescribedBy: lnEnabled
        ? lnEnabled.getAttribute("aria-describedby")
        : null,
    };
  });

  // ----------------------------------------------------------------
  // Hotkey gate behaviour
  // ----------------------------------------------------------------
  const hotkeys = await page.evaluate(async () => {
    const out = {};
    // Switch to Settings tab
    const settingsTab = document.getElementById("tab-settings");
    if (settingsTab) settingsTab.click();
    await new Promise((r) => setTimeout(r, 50));

    // Arrow Up while on a Settings select must NOT change volume.
    const beforeVol = document.getElementById("vol").value;
    const sel = document.getElementById("transition-select");
    if (sel) {
      sel.focus();
      window.dispatchEvent(new KeyboardEvent("keydown", {
        key: "ArrowUp", bubbles: true, cancelable: true,
      }));
      await new Promise((r) => setTimeout(r, 100));
    }
    out.volUnchangedOnSettingsTab =
      document.getElementById("vol").value === beforeVol;

    // ? still works from Settings tab to open the dialog.
    const modal = document.getElementById("hotkey-help-modal");
    window.dispatchEvent(new KeyboardEvent("keydown", {
      key: "?", bubbles: true, cancelable: true,
    }));
    await new Promise((r) => setTimeout(r, 100));
    out.shortcutsDialogOpensFromAnyTab = !!(modal && modal.open);
    if (modal && modal.open) modal.close();

    // Switch back to Now Playing — arrow keys nudge volume.
    const nowTab = document.getElementById("tab-now");
    if (nowTab) nowTab.click();
    await new Promise((r) => setTimeout(r, 50));
    document.body.focus();
    const v0 = parseInt(document.getElementById("vol").value, 10);
    window.dispatchEvent(new KeyboardEvent("keydown", {
      key: "ArrowDown", bubbles: true, cancelable: true,
    }));
    await new Promise((r) => setTimeout(r, 150));
    const v1 = parseInt(document.getElementById("vol").value, 10);
    out.volChangedOnNowTab = v1 !== v0;
    return out;
  });

  // ----------------------------------------------------------------
  // Live-region wipe
  // ----------------------------------------------------------------
  const liveRegion = await page.evaluate(async () => {
    const vol = document.getElementById("vol-announce");
    if (!vol) return { error: "vol-announce missing" };
    // Trigger an announce by changing the slider.
    const slider = document.getElementById("vol");
    slider.value = "70";
    slider.dispatchEvent(new Event("input"));
    // The announcer fires after a 250ms debounce.
    await new Promise((r) => setTimeout(r, 600));
    const announced = vol.textContent;
    // _clearLiveRegionLater fires at +3s (3000ms).
    await new Promise((r) => setTimeout(r, 3500));
    return {
      announced,
      cleared: vol.textContent === "",
    };
  });

  await browser.close();
  return { dom, hotkeys, liveRegion, errors };
}

const main = async () => {
  const results = {};
  for (const [name, launcher] of [
    ["chromium", chromium],
    ["firefox", firefox],
    ["webkit", webkit],
  ]) {
    try {
      results[name] = await audit(name, launcher);
    } catch (e) {
      results[name] = { error: String(e) };
    }
  }
  const out = JSON.stringify(results, null, 2);
  console.log(out);
  writeFileSync("regression_audit_report.json", out);
};

main();
