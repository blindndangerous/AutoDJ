// Visual accessibility audit: what a sighted, low-vision or keyboard-only
// user meets in every view and Settings subtab.  Per browser engine it
// checks, with every card opened:
//
//   - axe-core's WCAG 2.2 A/AA rules at 1280 px: text contrast (1.4.3),
//     target size (2.5.8), label in name (2.5.3) and the rest;
//   - reflow at 320 CSS px, the width of 400 % zoom on a 1280 px window,
//     and 200 % text at 1280 px: the page must not scroll sideways
//     (1.4.10, 1.4.4);
//   - every native checkbox and radio button is at least 24 x 24 px (2.5.8);
//   - Tab through the view: each stop draws a solid focus outline at least
//     2 px wide, scrolled fully into the window (2.4.7, 2.4.11);
//   - Settings > Sound > Preview output device: a named select with its
//     help, 24 px targets, a visible reason where the browser cannot
//     choose a device, and a change said politely without moving focus.
//
// Set AUTODJ_URL=http://host:port before running, e.g.:
//   AUTODJ_URL=http://localhost:8080 node tests/playwright/visual_audit.mjs

import { fileURLToPath } from "node:url";

import { runAudit } from "./audit_helpers.mjs";

const BASE = process.env.AUTODJ_URL || "http://localhost:8080";
const AXE = fileURLToPath(import.meta.resolve("axe-core/axe.min.js"));
const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const VIEWS = ["now", "queue", "carts", "track-info", "history", "settings", "library"];
const MAX_TAB_STOPS = 80;

async function openEveryCard(page) {
  await page.evaluate(() => {
    for (const details of document.querySelectorAll("details")) details.open = true;
  });
}

// Every view, and with Settings every subtab this server shows (Stream only
// in stream mode).  `visit` runs once per target with that target shown.
async function eachTarget(page, visit) {
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  for (const view of VIEWS) {
    await page.locator(`#tab-${view}`).click();
    await page.locator(`#panel-${view}`).waitFor({ state: "visible" });
    // Under reduced motion every change still runs a 0.01 ms transition,
    // which takes a frame; let it end before colours are measured.
    await page.waitForTimeout(100);
    if (view !== "settings") {
      await openEveryCard(page);
      await visit(view);
      continue;
    }
    const ids = await page.$$eval("#settings-tabs [role='tab']:not([hidden])",
      (tabs) => tabs.map((tab) => tab.id));
    for (const id of ids) {
      await page.locator(`#${id}`).click();
      await page.waitForTimeout(150);
      await openEveryCard(page);
      await visit(`settings/${id.replace(/^stab-/, "")}`);
    }
  }
}

function horizontalOverflow(page) {
  return page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const scrollWidth = document.documentElement.scrollWidth;
    if (scrollWidth <= width) return null;
    const widest = [...document.querySelectorAll("body *")]
      .filter((element) => element.getBoundingClientRect().right > width + 1)
      .map((element) => element.id ? `#${element.id}` : element.tagName.toLowerCase())
      .slice(0, 5);
    return { scrollWidth, width, widest };
  });
}

function smallChoiceControls(page) {
  return page.evaluate(() => [...document.querySelectorAll(
    'input[type="checkbox"], input[type="radio"]',
  )]
    .filter((input) => input.getClientRects().length)
    .map((input) => ({ id: input.id, ...input.getBoundingClientRect().toJSON() }))
    .filter(({ width, height }) => width < 23.5 || height < 23.5)
    .map(({ id, width, height }) => `${id || "input"} ${Math.round(width)}x${Math.round(height)}`));
}

async function axeViolations(page) {
  if (!await page.evaluate(() => Boolean(window.axe))) {
    await page.addScriptTag({ path: AXE });
  }
  return page.evaluate(async (tags) => {
    const result = await window.axe.run(document, { runOnly: { type: "tag", values: tags } });
    return result.violations.map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      targets: violation.nodes.slice(0, 5).map((node) => node.target.join(" ")),
    }));
  }, AXE_TAGS);
}

// Tab from the panel's own tab through the view.  Stops at the first
// element met twice (focus wrapped) or after MAX_TAB_STOPS.
async function focusProblems(page, startSelector) {
  await page.locator(startSelector).focus();
  const problems = [];
  // The start counts as met: focus coming back to it means the view is done.
  const seen = new Set([await page.evaluate(
    () => [...document.querySelectorAll("*")].indexOf(document.activeElement),
  )]);
  for (let stop = 0; stop < MAX_TAB_STOPS; stop += 1) {
    await page.keyboard.press("Tab");
    await page.waitForTimeout(30);
    const info = await page.evaluate(() => {
      const element = document.activeElement;
      if (!element || element === document.body) return null;
      const all = [...document.querySelectorAll("*")];
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return {
        key: all.indexOf(element),
        name: element.id ? `#${element.id}` : element.tagName.toLowerCase(),
        outline: style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2,
        inView: rect.top >= -1 && rect.bottom <= window.innerHeight + 1
          && rect.left >= -1 && rect.right <= window.innerWidth + 1,
      };
    });
    // Focus left the page (past the last control): the view is done.
    if (!info) { if (seen.size) break; continue; }
    if (seen.has(info.key)) break;
    seen.add(info.key);
    if (!info.outline) problems.push(`${info.name} has no focus outline`);
    if (!info.inView) problems.push(`${info.name} not scrolled into view`);
  }
  return problems;
}

// The preview output device setting, in whatever this engine supports.
async function previewOutputProblems(page) {
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 20000 });
  await page.locator("#tab-settings").click();
  await page.locator("#stab-sound").click();
  const select = page.locator("#preview-device");
  await select.waitFor({ state: "visible" });
  // Let the device list load.
  await page.waitForTimeout(300);
  const problems = [];
  const info = await page.evaluate(() => {
    const element = document.getElementById("preview-device");
    const button = document.getElementById("preview-device-choose");
    const note = document.getElementById("preview-device-note");
    const box = (node) => node && node.getClientRects().length
      ? node.getBoundingClientRect().toJSON() : null;
    return {
      label: element.labels?.[0]?.textContent.trim(),
      describedBy: element.getAttribute("aria-describedby") || "",
      disabled: element.disabled,
      select: box(element),
      button: box(button),
      noteShown: Boolean(note && !note.hidden && note.textContent.trim()),
    };
  });
  if (info.label !== "Preview output device") problems.push(`label is ${JSON.stringify(info.label)}`);
  if (!info.describedBy.split(" ").includes("preview-device-desc")) problems.push("help is not linked");
  for (const [what, rect] of [["select", info.select], ["button", info.button]]) {
    if (rect && (rect.width < 24 || rect.height < 24)) {
      problems.push(`${what} is ${Math.round(rect.width)}x${Math.round(rect.height)}`);
    }
  }
  if (info.disabled) {
    // No setSinkId: the reason is words on the page, linked to the select.
    if (!info.noteShown) problems.push("unsupported browser shows no reason");
    if (!info.describedBy.includes("preview-device-note")) problems.push("reason is not linked");
    return problems;
  }
  await select.focus();
  await select.selectOption("");
  try {
    await page.waitForFunction(() => /Previews play on/.test(
      document.getElementById("settings-status").textContent), null, { timeout: 3000 });
  } catch {
    problems.push("a change was not said in the status region");
  }
  if (!await page.evaluate(() => document.activeElement?.id === "preview-device")) {
    problems.push("a change moved focus");
  }
  return problems;
}

export async function audit(name, launcher) {
  const browser = await launcher.launch();
  const out = {
    axe: {}, reflow320: {}, text200: {}, smallChoices: {}, focus: {}, previewOutput: {},
    pageerrors: [],
  };
  try {
    const wide = await browser.newContext({ viewport: { width: 1280, height: 800 },
      reducedMotion: "reduce", bypassCSP: true });
    const page = await wide.newPage();
    page.on("pageerror", (error) => out.pageerrors.push(error.message));
    await eachTarget(page, async (target) => {
      const violations = await axeViolations(page);
      if (violations.length) out.axe[target] = violations;
      const small = await smallChoiceControls(page);
      if (small.length) out.smallChoices[target] = small;
      const view = target.split("/")[0];
      const sub = target.split("/")[1];
      const problems = await focusProblems(page, sub ? `#stab-${sub}` : `#tab-${view}`);
      if (problems.length) out.focus[target] = problems;
    });
    const preview = await previewOutputProblems(page);
    if (preview.length) out.previewOutput["settings/sound"] = preview;
    // 200 % text at the same window width.
    await wide.addInitScript(() => {
      document.addEventListener("DOMContentLoaded", () => {
        document.documentElement.style.fontSize = "200%";
      });
    });
    await eachTarget(page, async (target) => {
      const overflow = await horizontalOverflow(page);
      if (overflow) out.text200[target] = overflow;
    });
    await wide.close();

    const narrow = await browser.newContext({ viewport: { width: 320, height: 640 },
      reducedMotion: "reduce" });
    const phone = await narrow.newPage();
    phone.on("pageerror", (error) => out.pageerrors.push(error.message));
    await eachTarget(phone, async (target) => {
      const overflow = await horizontalOverflow(phone);
      if (overflow) out.reflow320[target] = overflow;
    });
    await narrow.close();
    return out;
  } finally {
    await browser.close();
  }
}

export function validateVisualAudit(name, result) {
  const failures = [];
  for (const key of ["axe", "reflow320", "text200", "smallChoices", "focus", "previewOutput"]) {
    const entries = Object.entries(result[key]);
    if (entries.length) failures.push(`${key}: ${JSON.stringify(Object.fromEntries(entries))}`);
  }
  if (result.pageerrors.length) failures.push(`page errors: ${result.pageerrors.join("; ")}`);
  if (failures.length) throw new Error(`${name} visual audit\n${failures.join("\n")}`);
}

await runAudit({
  audit,
  report: "visual_audit.json",
  validate: validateVisualAudit,
});
