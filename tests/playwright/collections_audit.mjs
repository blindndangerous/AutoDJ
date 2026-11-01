// Real browser controls and production markup; API replies stay in memory.
// This audit starts only a loopback static server and requires no AutoDJ backend.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { expect } from "@playwright/test";
import { runAudit } from "./audit_helpers.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const STATIC = resolve(ROOT, "src/autodj/static");
const require = createRequire(import.meta.url);
const report = process.env.AUTODJ_COLLECTIONS_REPORT || resolve(ROOT, ".codex-tmp/collections-audit.json");
mkdirSync(dirname(report), { recursive: true });
process.env.AUTODJ_BROWSERS ||= "chromium";

const server = createServer((request, response) => {
  const path = new URL(request.url, "http://localhost").pathname;
  if (path === "/") {
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    response.end('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AutoDJ collection audit</title><link rel="stylesheet" href="/app.css"></head><body><main><h1>AutoDJ collection audit</h1><div id="fixture"></div></main></body></html>');
    return;
  }
  const file = path === "/axe.js" ? require.resolve("axe-core/axe.min.js")
    : resolve(STATIC, `.${path === "/source-index" ? "/index.html" : path}`);
  if (path !== "/axe.js" && !file.startsWith(`${STATIC}${sep}`)) {
    response.writeHead(403).end();
    return;
  }
  try {
    response.setHeader("Content-Type", ({ ".js": "text/javascript", ".css": "text/css", ".html": "text/html" })[extname(file)] || "application/octet-stream");
    response.end(readFileSync(file));
  } catch {
    response.writeHead(404).end();
  }
});
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const BASE = `http://127.0.0.1:${server.address().port}`;

async function mount(page, theme) {
  await page.goto(BASE);
  await page.evaluate(async (chosenTheme) => {
    document.documentElement.dataset.theme = chosenTheme;
    const source = new window.DOMParser().parseFromString(await (await fetch("/source-index")).text(), "text/html");
    const fixture = document.getElementById("fixture");
    const schedule = source.getElementById("spanel-schedule");
    const tablist = document.createElement("div");
    tablist.setAttribute("role", "tablist");
    tablist.setAttribute("aria-label", "Settings");
    tablist.append(source.getElementById("stab-schedule"));
    tablist.firstElementChild.setAttribute("aria-selected", "true");
    schedule.hidden = false;
    schedule.open = true;
    const library = document.createElement("section");
    library.id = "panel-library";
    library.setAttribute("aria-label", "Library tools");
    const wheels = source.getElementById("clockwheels-card");
    wheels.open = true;
    library.append(wheels);
    fixture.append(tablist, schedule, library);
    document.body.append(source.getElementById("clock-step-dialog"), source.getElementById("confirm-dialog"));

    const clone = (value) => JSON.parse(JSON.stringify(value));
    const events = ["Morning", "Evening"].map((name, index) => ({
      id: `event-${index}`, name, action: "profile", params: { name: "Morning" },
      repeat: "daily", time: "07:00:00", days: [], timing: "soft", enabled: true,
      repeat_text: "Every day at 07:00", action_text: "Switch profile: Morning",
      next_run: "2026-11-01T07:00:00Z",
    }));
    const runs = [{ time: "2026-10-31T07:00:00Z", event: "Morning", summary: "Switch profile: Morning", result: "ok" }];
    const music = { type: "music", source: "music", folder: "", recursive: false, selection: "least_recent", enforce_rules: true };
    const clocks = {
      Morning: { version: 1, name: "Morning", resync_minutes: [0], steps: [music, { type: "settings", preset: "Night" }] },
      Evening: { version: 1, name: "Evening", resync_minutes: [0], steps: [{ type: "settings", preset: "Night" }] },
    };
    const calls = [];
    let active = "Morning";
    const snapshot = () => ({
      names: Object.keys(clocks).sort(), active,
      sources: [{ id: "music", kind: "music", path: "Music", recursive: true }],
      presets: ["Morning", "Night"], categories: [], genres: ["Rock"], beets_fields: [],
      types: ["music", "settings", "track", "liner"], cursor: 0, notice: null,
    });
    const request = async (url, options = {}) => {
      const method = options.method || "GET";
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, method, body });
      if (url === "/api/schedule" && method === "GET") return clone({
        timezone: "UTC", actions: [{ name: "profile", description: "Switch profile", fields: [{ name: "name", label: "Profile", type: "select", options: [{ value: "Morning", label: "Morning" }] }] }],
        events, runs, upcoming: events.filter((event) => event.enabled).map((event) => ({ ...event, time: event.next_run })),
      });
      if (url === "/api/schedule" && method === "POST") {
        const event = { ...body, id: `event-${events.length}`, repeat_text: "Every day", action_text: "Switch profile: Morning" };
        events.push(event);
        return clone(event);
      }
      if (url.startsWith("/api/schedule/")) {
        const [, , , id, verb] = url.split("/");
        const index = events.findIndex((event) => event.id === id);
        if (index < 0) throw new Error(`Missing fixture event ${id}`);
        if (verb === "run") { const run = { time: "2026-10-31T12:00:00Z", event: events[index].name, summary: "Switch profile: Morning", result: "ok" }; runs.unshift(run); return clone(run); }
        if (verb === "enable") { events[index].enabled = body.enabled; return clone(events[index]); }
        if (method === "PUT") { events[index] = { ...events[index], ...body }; return clone(events[index]); }
        if (method === "DELETE") return { deleted: events.splice(index, 1)[0].id };
      }
      if (url === "/api/library/clock" && method === "GET") return clone(snapshot());
      if (url === "/api/library/clock/check") return { tracks: 12, error: null };
      if (url === "/api/library/clock/active") { active = body.name; return clone(snapshot()); }
      if (url === "/api/library/clock" && method === "POST") { clocks[body.name] = body; return clone(body); }
      const match = /^\/api\/library\/clock\/([^/?]+)(?:\/(\w+))?/.exec(url);
      if (match) {
        const name = decodeURIComponent(match[1]);
        if (method === "GET") return clone(clocks[name]);
        if (method === "PUT") { clocks[name] = body; return clone(body); }
        if (method === "DELETE") { delete clocks[name]; return clone(snapshot()); }
        if (["duplicate", "rename"].includes(match[2])) {
          clocks[body.name] = { ...clone(clocks[name]), name: body.name };
          if (match[2] === "rename") { delete clocks[name]; if (active === name) active = body.name; }
          return clone(clocks[body.name]);
        }
      }
      throw new Error(`Unexpected fixture request ${method} ${url}`);
    };
    const { installSchedulerPanel } = await import(new URL("/modules/scheduler-panel.js", window.location.href).href);
    const { installClockwheelPanel } = await import(new URL("/modules/clockwheel-panel.js", window.location.href).href);
    const scheduler = installSchedulerPanel({ request, now: () => Date.parse("2026-10-31T12:00:00Z") });
    const clockwheel = installClockwheelPanel({ request });
    window.collectionFixture = { scheduler, clockwheel, events, clocks, calls };
    await scheduler.refresh();
    await clockwheel.refresh();
  }, theme);
  await page.addScriptTag({ url: `${BASE}/axe.js` });
  await page.locator('#schedule-events tbody tr[data-row-key]').first().waitFor();
  await page.locator('#clock-slots tbody tr[data-row-key]').first().waitFor();
}

async function focused(page, selector) {
  await page.waitForFunction((value) => document.activeElement?.matches(value), selector);
}

// Assert the active element itself, then continue through the grid with arrows.
// Descendant containment would incorrectly accept the Options button.
async function cellReturn(page, selector) {
  const cell = page.locator(selector).first();
  await expect(cell).toBeFocused();
  await expect(cell).toHaveAttribute("role", "gridcell");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await cell.evaluate((element) => document.activeElement === element.previousElementSibling), true, `${selector}: Left continues grid navigation`);
  await page.keyboard.press("ArrowRight");
  await expect(cell).toBeFocused();
}

async function editorDescription(page) {
  const dialog = page.locator('dialog.editor-dialog[open]');
  const title = await dialog.evaluate((element) => document.getElementById(element.getAttribute("aria-labelledby")).textContent.trim());
  await expect(dialog).toHaveAccessibleName(title);
  await expect(dialog).toHaveAccessibleDescription(await dialog.getAttribute("id") === "clock-step-dialog"
    ? "Choose what the step plays, how it chooses and when it plays."
    : "Use Tab to move between fields and actions.");
}

// #712: until focus leaves, a moved-to data cell is named by its text, the
// column heading when the column changed and the row's name when the row did.
async function dataCellNames(page, host) {
  const cells = page.locator(`${host} tbody tr[data-row-key]:first-child > td`);
  if (!await cells.count()) return;
  const headings = (await page.locator(`${host} thead th`).allTextContents()).map((text) => text.trim());
  await cells.first().focus();
  for (let index = 0; index < await cells.count(); index += 1) {
    const cell = cells.nth(index);
    if (index) await page.keyboard.press("ArrowRight");
    if (await cell.locator("button, input, select").count()) continue;
    const content = (await cell.textContent()).replace(/\s+/g, " ").trim();
    await expect(cell).toHaveAccessibleName(index ? [content, headings[index]].filter(Boolean).join(", ") : content);
  }
  const rows = page.locator(`${host} tbody tr[data-row-key]`);
  if (await rows.count() < 2) return;
  const destination = rows.nth(1);
  const named = destination.locator("td[data-name]");
  if (!await named.count()) return;
  const rowName = (await named.textContent()).replace(/\s+/g, " ").trim();
  const secondCells = destination.locator(":scope > td");
  for (let index = 0; index < await secondCells.count(); index += 1) {
    const cell = secondCells.nth(index);
    if (await cell.locator("button, input, select").count() || await cell.getAttribute("data-name") !== null) continue;
    await cells.nth(index).focus();
    await page.keyboard.press("ArrowDown");
    await expect(cell).toBeFocused();
    const content = (await cell.textContent()).replace(/\s+/g, " ").trim();
    await expect(cell).toHaveAccessibleName([content, rowName].filter(Boolean).join(", "));
    await named.focus();
    await expect(cell).toHaveAccessibleName(content);
  }
}

async function traceEditorOpen(page, trigger, first, evidence, row = null) {
  if (row) {
    await page.locator(`${row} td[data-action="options"]`).focus();
    await page.keyboard.press("Enter");
  }
  await page.locator(trigger).focus();
  await page.evaluate(() => {
    window.editorFocusTrace = [];
    window.captureEditorFocus = (event) => window.editorFocusTrace.push({ type: event.type, target: event.target.id || event.target.tagName });
    for (const type of ["focus", "focusin"]) document.addEventListener(type, window.captureEditorFocus, true);
  });
  await page.keyboard.press("Enter");
  await focused(page, first);
  const events = await page.evaluate(() => {
    for (const type of ["focus", "focusin"]) document.removeEventListener(type, window.captureEditorFocus, true);
    return window.editorFocusTrace;
  });
  assert.deepEqual(events, [{ type: "focus", target: first.slice(1) }, { type: "focusin", target: first.slice(1) }]);
  evidence.checks.push(`Editor opening ${first}: exactly one focus and one focusin event, no intermediate focus`);
}

async function checkTable(page, host) {
  await dataCellNames(page, host);
  assert.equal(await page.locator(`${host} [tabindex="0"]`).count(), 1, `${host} single Tab stop`);
  await page.locator(host).evaluate((element) => {
    const before = document.createElement("button"), after = document.createElement("button");
    before.id = "audit-before"; before.textContent = "Before collection";
    after.id = "audit-after"; after.textContent = "After collection";
    element.before(before); element.after(after); before.focus();
  });
  await page.keyboard.press("Tab");
  assert.equal(await page.locator(host).evaluate((element) => element.contains(document.activeElement)), true, `${host} keyboard entry`);
  await page.keyboard.press("Tab");
  await focused(page, "#audit-after");
  await page.keyboard.press("Shift+Tab");
  assert.equal(await page.locator(host).evaluate((element) => element.contains(document.activeElement)), true, `${host} reverse entry`);
  await page.keyboard.press("Shift+Tab");
  await focused(page, "#audit-before");
  await page.evaluate(() => { document.getElementById("audit-before").remove(); document.getElementById("audit-after").remove(); });
}

async function axe(page, evidence, state) {
  const result = await page.evaluate(async () => {
    // axe cannot resolve every hidden popup reference. Verify actual DOM
    // ownership before recording those incomplete checks for review.
    const invalidPopupReferences = [...document.querySelectorAll('[aria-haspopup="menu"]')].filter((button) => {
      const popup = document.getElementById(button.getAttribute("aria-controls"));
      return !popup || popup.getAttribute("role") !== "menu" || !button.parentElement.contains(popup);
    }).map((button) => button.getAttribute("aria-label"));
    const checked = await window.axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] } });
    const details = ({ id, impact, nodes }) => ({ id, impact, nodes: nodes.map(({ target, failureSummary }) => ({ target, failureSummary })) });
    return { violations: checked.violations.map(details), incomplete: checked.incomplete.map(details), invalidPopupReferences };
  });
  evidence.axe.push({ state, ...result });
  assert.deepEqual(result.violations, [], `axe ${state}`);
  assert.deepEqual(result.invalidPopupReferences, [], `popup references ${state}`);
}

async function visuals(page, evidence, state) {
  // Focus scrolling and reduced-motion CSS transitions settle at rendering
  // updates; an immediate read can measure the old position or outline.
  await page.evaluate(async () => {
    await new Promise((done) => window.requestAnimationFrame(() => window.requestAnimationFrame(done)));
    await Promise.all(document.getAnimations().filter((animation) => animation instanceof window.CSSTransition)
      .map((animation) => animation.finished.catch(() => null)));
  });
  const measurements = await page.evaluate(() => {
    const rgba = (color) => {
      const channels = color.match(/[\d.]+/g).map(Number);
      return [...channels.slice(0, 3), channels[3] ?? 1];
    };
    const background = (element) => {
      const layers = [];
      for (let current = element; current; current = current.parentElement) layers.push(rgba(window.getComputedStyle(current).backgroundColor));
      return layers.reverse().reduce((base, layer) => base.map((channel, index) => layer[index] * layer[3] + channel * (1 - layer[3])), [255, 255, 255]);
    };
    const luminance = (color) => color.slice(0, 3).map((channel) => {
      const value = channel / 255;
      return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
    const contrast = (first, second) => {
      const values = [luminance(first), luminance(second)].sort((a, b) => b - a);
      return (values[0] + 0.05) / (values[1] + 0.05);
    };
    const visible = (element) => element.getClientRects().length && !element.closest("[hidden]");
    const small = [...document.querySelectorAll(".collection-table button, .row-actions-menu [role=menuitem], dialog[open] button")]
      .filter(visible).map((element) => ({ label: element.getAttribute("aria-label") || element.textContent, width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height,
        minimumHeight: element.matches('.collection-table button, .row-actions-menu [role="menuitem"]') ? 44 : 24 }))
      .filter(({ width, height, minimumHeight }) => width < 24 || height < minimumHeight);
    const active = document.activeElement;
    const style = window.getComputedStyle(active);
    const bounds = active.getBoundingClientRect();
    const focusBackground = Number.parseFloat(style.outlineOffset) >= 0 ? active.parentElement : active;
    const textContrast = [...document.querySelectorAll(".collection-table button, .row-actions-menu [role=menuitem]")]
      .filter(visible).map((element) => contrast(rgba(window.getComputedStyle(element).color), background(element)));
    return { small, minimumButtonTextContrast: Math.min(...textContrast), focus: { label: active.getAttribute("aria-label") || active.textContent.trim(), contrast: contrast(rgba(style.outlineColor), background(focusBackground)), outlineStyle: style.outlineStyle, outlineWidth: Number.parseFloat(style.outlineWidth), bounds: bounds.toJSON(), viewport: { width: window.innerWidth, height: window.innerHeight }, visible: bounds.bottom > 0 && bounds.top < window.innerHeight && bounds.right > 0 && bounds.left < window.innerWidth } };
  });
  evidence.visual.push({ state, ...measurements });
  assert.deepEqual(measurements.small, [], `24px targets; collection buttons/menu items 44px high ${state}`);
  assert.ok(measurements.focus.outlineWidth >= 2 && measurements.focus.outlineStyle !== "none", `visible focus ${state}`);
  assert.equal(measurements.focus.visible, true, `focus in viewport ${state}`);
  assert.ok(measurements.focus.contrast >= 3, `focus contrast ${state}: ${measurements.focus.contrast}`);
  assert.ok(measurements.minimumButtonTextContrast >= 4.5, `button text contrast ${state}`);
}

async function menuJourney(page, row, evidence, state, typeahead = "d") {
  const cell = page.locator(`${row} td[data-action="options"]`);
  const button = page.locator(`${row} [aria-haspopup="menu"]`);
  await cell.focus();
  await page.keyboard.press("Enter");
  const menuId = await button.getAttribute("aria-controls");
  const menu = `#${menuId}`;
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  assert.equal(await button.getAttribute("aria-expanded"), "true");
  await page.keyboard.press("ArrowDown");
  const second = await page.locator(`${menu} [role="menuitem"]`).count() > 1 ? 2 : 1;
  await focused(page, `${menu} [role="menuitem"]:nth-child(${second})`);
  await page.keyboard.press("End");
  await focused(page, `${menu} [role="menuitem"]:last-child`);
  await page.keyboard.press("Home");
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  await page.keyboard.press(typeahead);
  await focused(page, `${menu} [role="menuitem"]`);
  assert.equal((await page.evaluate(() => document.activeElement.textContent)).toLowerCase().startsWith(typeahead), true, "type-ahead chooses a matching item");
  await axe(page, evidence, `${state} menu`);
  await visuals(page, evidence, `${state} menu`);
  await page.keyboard.press("Escape");
  await cellReturn(page, `${row} td[data-action="options"]`);
  await visuals(page, evidence, `${state} Options cell`);
  assert.equal(await button.getAttribute("aria-expanded"), "false");
  await page.keyboard.press("Space");
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  await page.keyboard.press("ArrowUp");
  await focused(page, `${menu} [role="menuitem"]:last-child`);
  await page.keyboard.press("ArrowDown");
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  // Give even the document's final collection a real subsequent Tab target.
  // Otherwise Firefox can move focus to browser chrome while activeElement
  // still names the last document control, which is not a trapped menu.
  await page.locator(row).evaluate(async (element) => {
    const { collectionHost } = await import(new URL("/modules/collection-table.js", window.location.href).href);
    const host = collectionHost(element);
    const after = document.createElement("button");
    after.id = "audit-menu-after"; after.textContent = "After collection menu";
    host.after(after);
  });
  await page.keyboard.press("Tab");
  await focused(page, "#audit-menu-after");
  assert.equal(await button.getAttribute("aria-expanded"), "false");
  assert.equal(await button.evaluate((element) => element.closest("table").contains(document.activeElement)), false, "Tab leaves collection");
  assert.equal(await page.locator(menu).evaluate((element) => element.contains(document.activeElement)), false, "Tab leaves popup");
  await page.keyboard.press("Shift+Tab");
  await cellReturn(page, `${row} td[data-action="options"]`);
  await page.locator("#audit-menu-after").evaluate((element) => element.remove());
  evidence.checks.push(`${state}: Enter/Space, wrapping menu arrows, Home/End, type-ahead, Escape to exact cell and arrow continuation; Tab exit and Shift+Tab cell re-entry`);
}

// Each entry point opens the row's own menu; Escape returns to the cell it
// was opened from. A table without actions keeps the browser's menu.
async function contextMenuJourney(page, row, evidence, state) {
  const button = page.locator(`${row} [aria-haspopup="menu"]`);
  const menu = `#${await button.getAttribute("aria-controls")}`;
  const data = page.locator(`${row} > td:not([data-action="options"])`);
  const first = data.first(), last = data.last();
  await page.evaluate(() => {
    window.contextMenuEvents = [];
    window.recordContextMenu = (event) => window.contextMenuEvents.push(event.defaultPrevented);
    window.addEventListener("contextmenu", window.recordContextMenu);
    window.menuFocusTrace = [];
    window.traceMenuFocus = (event) => window.menuFocusTrace.push(event.target.getAttribute("role") || event.target.tagName);
    document.addEventListener("focusin", window.traceMenuFocus, true);
  });
  const synthetic = [];
  for (const [how, cell] of [["Shift+F10", first], ["ContextMenu", last], ["right-click", first]]) {
    const words = (await cell.textContent()).replace(/\s+/g, " ").trim();
    if (how === "right-click") await cell.click({ button: "right" });
    else {
      await cell.focus();
      const before = await page.evaluate(() => window.contextMenuEvents.length);
      await page.keyboard.press(how);
      // Playwright's Firefox key input bypasses the widget layer that turns
      // these keys into contextmenu, so send the event the browser would.
      if (await page.evaluate((count) => window.contextMenuEvents.length === count, before)) {
        synthetic.push(how);
        await page.evaluate(() => document.activeElement.dispatchEvent(new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true })));
      }
    }
    await focused(page, `${menu} [role="menuitem"]:first-child`);
    // #712: the menu opens inside the originating cell and only its item takes focus.
    assert.equal(await cell.evaluate((element, id) => element.contains(document.querySelector(id)), menu), true, `${state} ${how} menu sits in its cell`);
    // The menu inside the cell is not part of the cell's name, and sits beside it.
    await expect(cell).toHaveAccessibleName(words);
    const [cellBox, menuBox] = [await cell.boundingBox(), await page.locator(menu).boundingBox()];
    assert.ok(Math.abs(menuBox.x - Math.max(4, Math.min(cellBox.x, page.viewportSize().width - menuBox.width - 4))) < 2, `${state} ${how} menu is anchored to its cell`);
    assert.deepEqual(await page.evaluate(() => window.menuFocusTrace.splice(0).filter((role) => role !== "gridcell")), ["menuitem"], `${state} ${how} focuses only the item`);
    assert.equal(await button.getAttribute("aria-expanded"), "true", `${state} ${how} opens the row menu`);
    assert.equal(await page.locator('[role="menu"]:visible').count(), 1, `${state} ${how} opens one menu`);
    await page.keyboard.press("Escape");
    await expect(cell).toBeFocused();
    await expect(cell).toHaveAccessibleName(words);
    assert.equal(await button.getAttribute("aria-expanded"), "false");
  }
  const prevented = await page.evaluate(() => {
    document.removeEventListener("focusin", window.traceMenuFocus, true);
    window.removeEventListener("contextmenu", window.recordContextMenu);
    return window.contextMenuEvents;
  });
  assert.deepEqual(prevented, [true, true, true], `${state}: each entry point fires one contextmenu event and suppresses the browser menu`);
  evidence.checks.push(`${state}: Shift+F10, Applications key and right-click open the row's menu; Escape returns to the originating cell${
    synthetic.length ? ` (${synthetic.join(" and ")}: this engine's automated key input fired no contextmenu, so the audit dispatched it on the focused cell)` : ""}`);
}

async function stoppedStation(page, row, evidence) {
  const button = page.locator(`${row} [aria-haspopup="menu"]`);
  await button.focus();
  await page.keyboard.press("Enter");
  await page.evaluate(() => window.collectionFixture.scheduler.setOffAir(true));
  const item = page.locator(`${row} [role="menuitem"][data-action="run"]`);
  const reason = "Start the station to run this event now";
  assert.equal(await item.getAttribute("aria-disabled"), "true");
  const description = await item.getAttribute("aria-describedby");
  assert.ok(description, "disabled Run now has a description before activation");
  assert.equal(await page.locator(`#${description}`).textContent(), reason);
  await expect(item).toHaveAccessibleDescription(reason);
  await axe(page, evidence, "Station stopped menu");
  const before = await page.evaluate(() => window.collectionFixture.calls.length);
  // A background clockwheel read must not count as an attempted Run now.
  await page.evaluate(() => window.collectionFixture.clockwheel.refresh());
  for (const method of ["mouse", "Enter", "Space", "type-ahead then Enter"]) {
    await page.evaluate(() => { document.getElementById("schedule-status").textContent = ""; });
    if (method === "mouse") {
      const bounds = await item.boundingBox();
      // Playwright's locator.click correctly refuses aria-disabled controls;
      // use the mouse to exercise the user's attempt on this focusable item.
      await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    } else {
      await item.focus();
      if (method === "type-ahead then Enter") {
        await page.keyboard.press("End");
        await page.keyboard.press("r");
        await focused(page, `${row} [role="menuitem"][data-action="run"]`);
      }
      await page.keyboard.press(method === "Space" ? "Space" : "Enter");
    }
    await page.waitForFunction((text) => document.getElementById("schedule-status").textContent === text, reason);
    const delta = await page.evaluate((index) => window.collectionFixture.calls.slice(index), before);
    const actions = delta.filter(({ method, url }) => method !== "GET" || /\/run(?:\?|$)/.test(url));
    assert.deepEqual(actions, [], `off-air ${method} sends no action request: ${JSON.stringify(delta.map(({ method, url }) => ({ method, url })))}`);
    assert.equal(await button.getAttribute("aria-expanded"), "true");
  }
  await page.keyboard.press("Escape");
  await page.evaluate(() => window.collectionFixture.scheduler.setOffAir(false));
  assert.equal(await item.getAttribute("aria-disabled"), "false");
  assert.equal(await item.getAttribute("aria-describedby"), null);
  evidence.checks.push("Station stopped: mouse, Enter, Space and type-ahead/Enter send no Run now or mutation request during passive clockwheel refresh; each announces the linked disabled reason");
}

async function journey(page, evidence) {
  for (const host of ["#schedule-events", "#schedule-upcoming", "#schedule-runs", "#clock-list", "#clock-slots"]) await checkTable(page, host);
  await expect(page.locator("#clock-name")).toHaveAccessibleName("New clockwheel name");
  await expect(page.locator("#clock-name")).toHaveValue("");
  await page.locator("#clock-name").fill("Future wheel");
  await choose(page, '#clock-list tr[data-row-key="Evening"]', "open");
  await expect(page.locator("#clock-name")).toHaveValue("Future wheel");
  await choose(page, '#clock-list tr[data-row-key="Morning"]', "open");
  await expect(page.locator("#clock-name")).toHaveValue("Future wheel");
  await page.locator("#clock-name").fill("");
  assert.deepEqual(await page.locator("#schedule-events thead th").allTextContents(), ["Name", "When", "Action", "Enabled", "Actions"]);
  assert.deepEqual(await page.locator("#schedule-upcoming thead th").allTextContents(), ["Name", "Due", "Action"]);
  assert.deepEqual(await page.locator("#schedule-runs thead th").allTextContents(), ["Name", "When", "Action", "Result"]);
  assert.deepEqual(await page.locator("#clock-list thead th").allTextContents(), ["Name", "Active", "Actions"]);
  assert.deepEqual(await page.locator("#clock-slots thead th").allTextContents(), ["Step", "Actions"]);
  await expect(page.locator('#clock-slots tr[data-index="0"] td[data-action="step"]')).toHaveAccessibleName("1. music from Music, least recently played");
  evidence.checks.push("All five tables: horizontal moves add the column heading and vertical moves the row name to computed cell names, one Tab stop, actual Tab/Shift+Tab entry/exit; #712 column sets and numbered step sentences");
  const eventRow = '#schedule-events tr[data-row-key="event-0"]';
  await contextMenuJourney(page, eventRow, evidence, "Schedule event");
  await contextMenuJourney(page, '#clock-slots tr[data-index="0"]', evidence, "Clockwheel step");
  const nativeMenu = await page.locator("#schedule-upcoming tbody td").first().evaluate((cell) => {
    const event = new window.MouseEvent("contextmenu", { bubbles: true, cancelable: true });
    cell.dispatchEvent(event);
    return event.defaultPrevented;
  });
  assert.equal(nativeMenu, false, "a read-only table leaves the browser's own menu");
  evidence.checks.push("Upcoming: a table without actions does not suppress the browser's context menu");
  await menuJourney(page, eventRow, evidence, "Schedule");
  await stoppedStation(page, eventRow, evidence);
  await page.locator(`${eventRow} td[data-action="options"]`).focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => window.collectionFixture.calls.some(({ url }) => url.endsWith("/event-0/run")));
  assert.equal(await page.evaluate(() => window.collectionFixture.calls.filter(({ url }) => url.endsWith("/event-0/run")).length), 1);
  await cellReturn(page, `${eventRow} td[data-action="options"]`);
  evidence.checks.push("Schedule: Enter invokes Run now once and returns to exact Options cell with arrow continuation");
  await page.locator(`${eventRow} [aria-haspopup="menu"]`).focus();
  await page.keyboard.press("Space");
  await focused(page, `${eventRow} [role="menuitem"][data-action="edit"]`);
  await page.keyboard.press("ArrowDown");
  await focused(page, `${eventRow} [role="menuitem"][data-action="run"]`);
  await page.keyboard.press("Space");
  await cellReturn(page, `${eventRow} td[data-action="options"]`);
  assert.equal(await page.locator(`${eventRow} [aria-haspopup="menu"]`).getAttribute("aria-expanded"), "false");
  assert.equal(await page.evaluate(() => window.collectionFixture.calls.filter(({ url }) => url.endsWith("/event-0/run")).length), 2);
  evidence.checks.push("Schedule: Space opens and activates once without reopening on focus return");
  await choose(page, eventRow, "delete");
  await confirmationScan(page, evidence, "Delete schedule event");
  await expect(page.locator("#confirm-dialog")).toHaveAccessibleDescription(await page.locator("#confirm-message").textContent());
  await page.locator("#confirm-cancel").click();
  await cellReturn(page, `${eventRow} td[data-action="options"]`);

  await traceEditorOpen(page, `${eventRow} [role="menuitem"][data-action="edit"]`, "#schedule-name", evidence, eventRow);
  await editorDescription(page);
  await axe(page, evidence, "Schedule editor");
  await page.keyboard.press("Escape");
  await cellReturn(page, `${eventRow} td[data-action="options"]`);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await page.locator("#schedule-name").fill("Morning revised");
  await page.locator("#schedule-save").focus();
  await page.keyboard.press("Enter");
  await cellReturn(page, `${eventRow} td[data-action="options"]`);
  assert.equal(await page.locator(`${eventRow} td`).first().textContent(), "Morning revised");
  evidence.checks.push("Schedule editor: keyboard open from the menu's first item, Escape cancel, save and return to the Actions cell");

  const stepRow = '#clock-slots tr[data-index="0"]';
  await menuJourney(page, stepRow, evidence, "Clockwheel step");
  await page.locator(`${stepRow} td[data-action="options"]`).focus();
  await page.keyboard.press("Enter");
  await focused(page, `${stepRow} [data-action="edit"]`);
  await page.keyboard.press("ArrowDown");
  await focused(page, `${stepRow} [data-action="move-up"]`);
  assert.equal(await page.locator(`${stepRow} button[data-action="move-up"]`).getAttribute("aria-disabled"), "true");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.getElementById("clock-status").textContent.includes("already the first step"));
  assert.equal(await page.locator('#clock-slots tr[data-index]').count(), 2);
  await page.keyboard.press("Escape");
  evidence.checks.push("Clockwheel step: disabled Move up stays reachable and announces its reason without mutation");
  await traceEditorOpen(page, `${stepRow} [role="menuitem"][data-action="edit"]`, "#clock-step-type", evidence, stepRow);
  await editorDescription(page);
  await axe(page, evidence, "Clockwheel step editor");
  await page.locator("#clock-step-selection").selectOption("random");
  await page.locator("#clock-add-step").focus();
  await page.keyboard.press("Enter");
  await focused(page, `${stepRow} td[data-action="options"]`);
  await page.keyboard.press("Alt+ArrowDown");
  await page.waitForFunction(() => document.activeElement.closest("tr")?.dataset.index === "1");
  await page.keyboard.press("Control+z");
  await page.keyboard.press("Delete");
  assert.equal(await page.locator('#clock-slots tr[data-index]').count(), 1);
  assert.equal(await page.locator('#confirm-dialog[open]').count(), 0);
  await page.keyboard.press("Control+z");
  assert.equal(await page.locator('#clock-slots tr[data-index]').count(), 2);
  await page.locator("#clock-save").focus();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => window.collectionFixture.calls.some(({ url, method }) => url.includes("/clock/Morning") && method === "PUT"));
  evidence.checks.push("Clockwheel steps: editor save, Alt+Down, Delete without confirmation, Ctrl+Z Undo and explicit save");

  const savedRow = '#clock-list tr[data-row-key="Evening"]';
  await menuJourney(page, savedRow, evidence, "Saved clockwheel");
  await page.locator(`${savedRow} td[data-action="options"]`).focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("r");
  await page.keyboard.press("Enter");
  await focused(page, "#clock-editor-name");
  await editorDescription(page);
  await axe(page, evidence, "Clockwheel Rename editor");
  await page.locator("#clock-editor-name").fill("Evening revised");
  await page.locator('#clock-name-form button[type="submit"]').focus();
  await page.keyboard.press("Enter");
  await focused(page, '#clock-list tr[data-row-key="Evening revised"] td[data-action="options"]');
  assert.equal(await page.locator("#clock-open-name").textContent(), "Morning");
  evidence.checks.push("Saved Rename targets Evening while Morning stays open; editor returns to renamed row");

  await page.locator("#clock-create").focus();
  await page.keyboard.press("Enter");
  await focused(page, "#clock-editor-name");
  await editorDescription(page);
  await axe(page, evidence, "Clockwheel Create editor");
  await page.keyboard.press("Escape");
  await focused(page, "#clock-create");
  await page.keyboard.press("Enter");
  await page.locator("#clock-editor-name").fill("New wheel");
  await page.locator('#clock-name-form button[type="submit"]').focus();
  await page.keyboard.press("Enter");
  await focused(page, "#clock-new-step");
  assert.match(await page.locator("#clock-open-name").textContent(), /New wheel/);
  assert.equal(await page.locator("#clock-slots .no-results").count(), 1);
  evidence.checks.push("Create: modal cancel/save round trip, explicit unsaved wheel and empty steps state");

  await page.locator(`${eventRow} td[data-action="options"]`).focus();
  await axe(page, evidence, "Panels and empty steps");
  await visuals(page, evidence, "Panels");
  await page.setViewportSize({ width: 320, height: 800 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, "320px page reflow");
  await page.locator(`${eventRow} td[data-action="options"]`).focus();
  await page.keyboard.press("Enter");
  await visuals(page, evidence, "320px menu");
  const menuBounds = await page.locator('[role="menu"]:visible').boundingBox();
  assert.ok(menuBounds.x >= 0 && menuBounds.x + menuBounds.width <= 320, "320px popup stays in viewport");
  await page.keyboard.press("Escape");
  await page.emulateMedia({ forcedColors: "active" });
  await page.keyboard.press("Enter");
  await visuals(page, evidence, "Forced colors menu");
  await page.keyboard.press("Escape");
  await page.emulateMedia({ forcedColors: "none" });
  evidence.checks.push("320px page reflow and popup bounds; forced-colors menu focus and targets");

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.locator(`${eventRow} td[data-action="options"]`).focus();
  await page.keyboard.press("Enter");
  await focused(page, `${eventRow} [role="menuitem"]:first-child`);
  await page.evaluate(async () => {
    const fixture = window.collectionFixture;
    fixture.events[0].name = "Morning refreshed";
    window.collectionFocusedItem = document.activeElement;
    await fixture.scheduler.refresh();
  });
  assert.equal(await page.evaluate(() => document.activeElement === window.collectionFocusedItem), true, "passive update preserves native menu focus");
  await page.evaluate(async () => {
    window.collectionFixture.events.splice(0, 1);
    await window.collectionFixture.scheduler.refresh();
  });
  await cellReturn(page, '#schedule-events tr[data-row-key="event-1"] td[data-action="options"]');
  assert.equal(await page.locator('[role="menu"]:visible').count(), 0, "removed native popover closes");
  await expect(page.locator("#schedule-status")).toContainText("Focus moved to Options for Evening", { timeout: 10000 });
  await page.keyboard.press("Enter");
  await page.evaluate(async () => {
    window.collectionFixture.events.splice(0);
    await window.collectionFixture.scheduler.refresh();
  });
  await focused(page, "#schedule-add");
  assert.equal(await page.locator("#schedule-events .no-results").count(), 1);
  assert.equal(await page.locator('[role="menu"]:visible').count(), 0);
  await expect(page.locator("#schedule-status")).toContainText("Focus moved to Add event", { timeout: 10000 });
  evidence.checks.push("Native open popup survives passive refresh; row removal closes it and announces adjacent-row/final Add fallback");
}

// Each Batch 2 surface gets a fresh document and production card markup.
// Modules own the controls; only network data and timers are supplied here.
async function mountBatch(page, theme, surface) {
  await page.goto(BASE);
  await page.evaluate(async ({ theme, surface }) => {
    document.documentElement.dataset.theme = theme;
    const source = new window.DOMParser().parseFromString(await (await fetch("/source-index")).text(), "text/html");
    const panel = document.createElement("section");
    const library = ["shows", "editions"].includes(surface);
    panel.id = library ? "panel-library" : surface === "voice" ? "panel-voice" : "panel-queue";
    panel.setAttribute("aria-label", library ? "Library tools" : surface === "voice" ? "Voice tracks" : "Queue and requests");
    const card = library ? source.getElementById("shows-heading").closest("details")
      : surface === "voice" ? source.getElementById("voice-tracker")
        : source.getElementById(surface === "queue" ? "queue-card" : "requests-card");
    card.hidden = false;
    if (card.tagName === "DETAILS") card.open = true;
    if (surface === "voice") panel.append(source.getElementById("voice-heading"));
    panel.append(card);
    document.getElementById("fixture").append(panel);
    document.body.append(source.getElementById("confirm-dialog"));
    const clone = (value) => JSON.parse(JSON.stringify(value));
    const calls = [];
    const fixture = window.batchFixture = { calls };
    if (["shows", "queue"].includes(surface)) {
      const view = surface === "shows" ? "library" : "queue";
      panel.dataset.view = view;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", `tab-${view}`);
      panel.removeAttribute("aria-label");
      const nav = document.createElement("div");
      nav.id = "view-nav"; nav.setAttribute("role", "tablist"); nav.setAttribute("aria-label", "Views");
      nav.append(source.getElementById(`tab-${view}`), source.getElementById("tab-track-info"));
      document.getElementById("fixture").prepend(nav);
      document.getElementById("fixture").append(source.getElementById("panel-track-info"));
      // Library folders and the current-track trigger are dependencies of the
      // actual Track info controller. Keep their production markup hidden.
      const support = document.createElement("div"); support.hidden = true;
      support.append(source.getElementById("source-folders-heading").closest("section"), source.getElementById("edit-current-points"));
      document.body.append(support, source.getElementById("track-info-dialog"), source.getElementById("sr-status"));
      const libraryRequest = async (url) => {
        if (url.startsWith("/api/library/points?")) return {
          path: new URL(url, window.location.href).searchParams.get("path"), title: "Selected song", kind: "music", points: {},
        };
        if (url === "/api/library/folders") return { folders: [], similarity_available: false };
        if (url === "/api/library/auto-rescan") return { hours: 0, minutes: 0 };
        if (url === "/api/library/music-fade") return { db: 10 };
        if (url.startsWith("/api/intros?")) return { song: [], artist: [], active: true };
        throw new Error(`Unexpected Track info fixture request ${url}`);
      };
      const { initViewRouter } = await import(new URL("/modules/tabs.js", window.location.href).href);
      window.history.replaceState(null, "", `#${view}`);
      initViewRouter();
      const { installLibraryFolders } = await import(new URL("/modules/library-folders.js", window.location.href).href);
      fixture.library = installLibraryFolders({ request: libraryRequest });
    }
    document.addEventListener("autodj:edit-points", (event) => {
      fixture.trackInfo = { path: event.detail.path, kind: event.detail.kind, opener: event.detail.opener };
    });
    if (surface === "shows") {
      const song = (index) => ({ path: `/music/${index}.flac`, title: `Song ${index}`, seconds: 180, length_text: "3:00", missing: false });
      const show = { id: "sample-show", name: "Evening selection", source: "category", category: "music",
        source_text: "Category Music", clockwheel: "", target_minutes: 60, station_name: "", events: [], missing: 0,
        songs: [song(1), song(2)] };
      const view = () => ({ ...show, count: show.songs.length, total_seconds: show.songs.length * 180, total_text: `${show.songs.length * 3}:00` });
      fixture.shows = [show];
      const request = async (url, options = {}) => {
        const method = options.method || "GET", body = options.body ? JSON.parse(options.body) : {};
        calls.push({ url, method, body });
        if (url === "/api/shows") return clone({ shows: fixture.shows.map(() => view()), categories: [{ id: "music", name: "Music" }], clockwheels: [], limits: { min_minutes: 5, max_minutes: 360 }, on_air: null });
        if (url.startsWith("/api/editions?")) return { editions: [] };
        if (url.endsWith("/songs")) { show.songs = body.songs.map((path) => song(Number(path.match(/(\d+)\.flac$/)[1]))); return clone(view()); }
        if (url === "/api/shows/sample-show") {
          if (method === "PUT") Object.assign(show, body);
          if (method === "DELETE") fixture.shows = [];
          return clone(view());
        }
        if (url.startsWith("/api/search?")) return { results: fixture.emptySearch ? [] : [{ path: "/music/3.flac", title: "Song 3", length: 180 }] };
        throw new Error(`Unexpected shows fixture ${method} ${url}`);
      };
      const { installShowsPanel } = await import(new URL("/modules/shows-panel.js", window.location.href).href);
      fixture.controls = installShowsPanel({ request });
      await fixture.controls.refresh();
    } else if (surface === "editions") {
      document.getElementById("shows-editor").hidden = false;
      document.getElementById("edition-card").hidden = false;
      document.getElementById("edition-card").open = true;
      fixture.edition = { id: "sample-edition", show_id: "sample-show", name: "Evening edition", revision: 3,
        occurrences: [{ id: "first", kind: "song", path: "/music/1.flac" }, { id: "second", kind: "song", path: "/music/2.flac" }],
        assets: {}, takes: [{ id: "speech", asset_id: "recording", label: "Introduction", position: "transition",
          before_id: "first", after_id: "second", placement: "auto", status: "unattached", dependency_hash: "old" }] };
      const request = async (url, options = {}) => {
        const method = options.method || "GET", body = options.body ? JSON.parse(options.body) : {};
        calls.push({ url, method, body });
        const edition = fixture.edition;
        if (url.startsWith("/api/editions?")) return clone({ editions: [edition] });
        if (url.endsWith("/reattach")) {
          Object.assign(edition.takes[0], { before_id: body.before_id, after_id: body.after_id, status: "needs_review", dependency_hash: "new" });
          edition.revision += 1;
        } else if (method === "DELETE") { edition.takes = []; edition.revision += 1; }
        else if (method === "PUT") { edition.occurrences = body.occurrences; edition.revision += 1; }
        return clone(edition);
      };
      const { installPreparedEditions } = await import(new URL("/modules/prepared-editions.js", window.location.href).href);
      fixture.controls = installPreparedEditions({ request });
      await fixture.controls.openShow({ id: "sample-show", name: "Evening programme", songs: [
        { path: "/music/1.flac", title: "First song" }, { path: "/music/2.flac", title: "Second song" }] });
      document.getElementById("edition-load").click();
    } else if (surface === "queue") {
      const queue = await import(new URL("/modules/queue.js", window.location.href).href);
      const els = { queueList: document.getElementById("queue-list"), queueAnnounce: document.getElementById("queue-announce"), queueClear: document.getElementById("queue-clear") };
      queue.resetQueueState(els);
      queue.applyQueueState(["Alpha", "Bravo", "Charlie"].map((display_name, index) => ({ path: `/music/${index + 1}.flac`, display_name })), els);
      queue.installQueueButtons(els); queue.installQueueClear(els);
      fixture.restrict = (reason) => queue.setQueueDisabledReason(els, reason);
    } else if (surface === "requests") {
      fixture.state = { enabled: true, approval: "manual", today: 3, daily_limit: 20, bridges_enabled: false,
        max_bridges: 2, bridge_cost_threshold: 2, no_smooth_spot: "best",
        pending: ["Alpha", "Bravo", "Charlie", "Delta"].map((title, index) => ({ id: `request-${index}`, title, artist: "Artist", name: "Listener", requested_at: 100,
          fit: index === 1 ? "No smooth position is available." : "Fits the queue", can_approve: index !== 1 })),
        recent: [{ id: "previous", title: "Earlier song", name: "Listener", requested_at: 90, status: "queued", note: "Approved" }] };
      const request = async (url, options = {}) => {
        const method = options.method || "GET", body = options.body ? JSON.parse(options.body) : null;
        calls.push({ url, method, body });
        if (url === "/api/requests" && method === "GET") return clone(fixture.state);
        const action = /^\/api\/requests\/(request-\d+)\/(approve|play-next|reject)$/.exec(url);
        if (method === "POST" && action && body === null) {
          const index = fixture.state.pending.findIndex((item) => item.id === action[1]);
          if (index < 0) throw new Error(`Missing fixture request ${action[1]}`);
          const item = fixture.state.pending.splice(index, 1)[0];
          fixture.state.recent.unshift({ ...item, status: action[2] === "reject" ? "rejected" : "queued" });
          return clone(fixture.state);
        }
        throw new Error(`Unexpected listener request fixture ${method} ${url}`);
      };
      const { installListenerRequests } = await import(new URL("/modules/listener-requests.js", window.location.href).href);
      fixture.controls = installListenerRequests({ request, setTimer: () => 0, later: (callback) => setTimeout(callback, 0) });
      await fixture.controls.refresh();
    } else if (surface === "voice") {
      const { installVoiceTracker } = await import(new URL("/modules/voice-tracker.js", window.location.href).href);
      fixture.controls = installVoiceTracker(undefined, { interval: () => 0, media: () => ({ enumerateDevices: async () => [] }) });
      await fixture.controls.refresh();
    }
  }, { theme, surface });
  await page.addScriptTag({ url: `${BASE}/axe.js` });
}

async function choose(page, row, action) {
  await page.locator(`${row} [aria-haspopup="menu"]`).focus();
  await page.keyboard.press("Enter");
  await page.locator(`${row} button[data-action="${action}"]`).focus();
  await page.keyboard.press("Enter");
}

async function emptyTable(page, host, evidence) {
  await page.locator("#confirm-dialog").waitFor({ state: "hidden" });
  await page.locator(`${host} td.no-results`).waitFor();
  assert.equal(await page.locator(`${host} td.no-results`).count(), 1);
  assert.equal(Number(await page.locator(`${host} td.no-results`).getAttribute("colspan")), await page.locator(`${host} thead th`).count());
  await checkTable(page, host);
  evidence.checks.push(`${host}: one spanning empty row and one actual Tab stop`);
}

async function tableArrows(page, host) {
  const first = page.locator(`${host} tbody tr[data-row-key] td`).first();
  await first.focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(await first.evaluate((element) => document.activeElement === element.nextElementSibling), true, `${host} right cell navigation`);
  await page.keyboard.press("ArrowLeft");
  assert.equal(await first.evaluate((element) => document.activeElement === element), true, `${host} left cell navigation`);
}

async function confirmationScan(page, evidence, state) {
  await focused(page, "#confirm-cancel");
  await axe(page, evidence, `${state} confirmation`);
  await visuals(page, evidence, `${state} confirmation`);
}

async function trackInfoRoundtrip(page, row, evidence, state) {
  await focused(page, "#track-info-heading");
  assert.equal(await page.locator("#points-path").inputValue(), "/music/1.flac");
  await axe(page, evidence, `${state} real Track info`);
  await page.locator("#track-info-return").focus();
  await page.keyboard.press("Enter");
  await focused(page, `${row} td[data-action="options"]`);
  evidence.checks.push(`${state}: actual library Track info controller and tab router return directly to the originating cell`);
}

async function showsJourney(page, evidence) {
  const show = '#shows-list tr[data-row-key="sample-show"]';
  const viewport = page.viewportSize();
  await page.setViewportSize({ width: 320, height: 640 });
  // A row taller than the viewport must not move its button between pointer
  // down and up when the shared grid transfers focus to the containing cell.
  await page.locator(show).evaluate((row) => { row.style.height = "900px"; });
  assert.ok((await page.locator(show).boundingBox()).height > 640, "pointer regression uses a tall row");
  const beforeOpen = await page.evaluate(() => window.batchFixture.calls.filter((call) => call.url === "/api/shows/sample-show" && call.method === "GET").length);
  await page.locator(`${show} [aria-haspopup="menu"]`).click();
  await page.locator(`${show} [role="menuitem"][data-action="open"]`).click();
  await page.waitForFunction(() => !document.getElementById("shows-editor").hidden && document.querySelectorAll('#shows-songs tr[data-row-key]').length === 2);
  assert.equal(await page.evaluate(() => window.batchFixture.calls.filter((call) => call.url === "/api/shows/sample-show" && call.method === "GET").length), beforeOpen + 1, "real pointer opens the show once");
  await focused(page, "#shows-editor-heading");
  await page.locator("#shows-close").click();
  await page.locator(show).evaluate((row) => row.style.removeProperty("height"));
  await page.setViewportSize(viewport);
  evidence.checks.push("Show Options: real pointer opens the menu of a tall row at 320x640; Open list sends one detail GET and focuses its editor");
  await checkTable(page, "#shows-list"); await tableArrows(page, "#shows-list");
  await menuJourney(page, show, evidence, "Show", "d");
  await choose(page, show, "edit");
  await focused(page, "#shows-name");
  await editorDescription(page);
  await axe(page, evidence, "Show metadata editor"); await visuals(page, evidence, "Show metadata editor");
  await page.keyboard.press("Escape");
  await cellReturn(page, `${show} td[data-action="options"]`);
  await choose(page, show, "edit");
  await page.locator("#shows-name").fill("Evening revised");
  for (const id of ["shows-source-category", "shows-category", "shows-target", "shows-station-name", "shows-save"]) {
    await page.keyboard.press("Tab"); await focused(page, `#${id}`);
  }
  await page.keyboard.press("Enter");
  await focused(page, `${show} td[data-action="options"]`);
  assert.equal(await page.locator(`${show} td[data-action="name"]`).textContent(), "Evening revised");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await page.locator('#shows-songs tr[data-row-key]').first().waitFor();
  await checkTable(page, "#shows-songs"); await tableArrows(page, "#shows-songs");
  const song = '#shows-songs tr[data-row-key]:first-child';
  await menuJourney(page, song, evidence, "Show song", "r");
  await choose(page, song, "info");
  assert.deepEqual(await page.evaluate(() => ({ path: window.batchFixture.trackInfo.path, kind: window.batchFixture.trackInfo.kind,
    liveOpener: window.batchFixture.trackInfo.opener.isConnected })), { path: "/music/1.flac", kind: "music", liveOpener: true });
  await trackInfoRoundtrip(page, song, evidence, "Show song");
  await choose(page, song, "remove"); await confirmationScan(page, evidence, "Remove show song");
  await page.locator("#confirm-cancel").click();
  await cellReturn(page, `${song} td[data-action="options"]`);
  assert.equal(await page.locator('#shows-songs tr[data-row-key]').count(), 2);
  await choose(page, song, "remove"); await page.locator("#confirm-accept").click();
  await focused(page, '#shows-songs td[data-action="options"]');
  await page.locator("#shows-search").fill("song"); await page.locator("#shows-search-run").click();
  await page.locator('#shows-results tr[data-row-key]').waitFor();
  await checkTable(page, "#shows-results"); await tableArrows(page, "#shows-results");
  await menuJourney(page, '#shows-results tr[data-row-key]', evidence, "Show search result", "a");
  await choose(page, '#shows-results tr[data-row-key]', "add");
  await page.waitForFunction(() => document.querySelectorAll('#shows-songs tr[data-row-key]').length === 2);
  for (let count = 2; count > 0; count -= 1) {
    await choose(page, song, "remove"); await page.locator("#confirm-accept").click();
    await page.waitForFunction((wanted) => document.querySelectorAll('#shows-songs tr[data-row-key]').length === wanted, count - 1);
  }
  await emptyTable(page, "#shows-songs", evidence);
  await page.evaluate(() => { window.batchFixture.emptySearch = true; });
  await page.locator("#shows-search-run").click(); await emptyTable(page, "#shows-results", evidence);
  await choose(page, show, "delete"); await confirmationScan(page, evidence, "Delete show");
  await page.locator("#confirm-cancel").click();
  await cellReturn(page, `${show} td[data-action="options"]`);
  await choose(page, show, "delete"); await page.locator("#confirm-accept").click();
  await focused(page, "#shows-add"); await emptyTable(page, "#shows-list", evidence);
  await axe(page, evidence, "Empty shows");
  evidence.checks.push("Shows: metadata cancel/save; Track info dispatch with actual opener; confirmed song/show removal cancel/accept; search Add; three empty tables");
}

async function editionsJourney(page, evidence) {
  const occurrence = '#edition-occurrences tr[data-row-key="first"]', take = '#edition-takes tr[data-row-key="speech"]';
  await page.locator(occurrence).waitFor();
  for (const host of ["#edition-occurrences", "#edition-takes"]) { await checkTable(page, host); await tableArrows(page, host); }
  await menuJourney(page, occurrence, evidence, "Prepared occurrence", "r");
  await menuJourney(page, take, evidence, "Prepared take", "r");
  await choose(page, take, "review");
  assert.equal(await page.locator(`${take} button[data-action="review"]`).getAttribute("aria-disabled"), "true");
  await page.keyboard.press("Escape");
  await choose(page, take, "reattach"); await focused(page, "#edition-reattach-position");
  await editorDescription(page);
  await axe(page, evidence, "Reattach editor"); await visuals(page, evidence, "Reattach editor");
  await page.keyboard.press("Escape"); await cellReturn(page, `${take} td[data-action="options"]`);
  await choose(page, take, "reattach");
  await focused(page, "#edition-reattach-position");
  await page.keyboard.press("Tab"); await focused(page, "#edition-reattach-save");
  await page.keyboard.press("Enter");
  await cellReturn(page, `${take} td[data-action="options"]`);
  assert.deepEqual(await page.evaluate(() => window.batchFixture.calls.find((call) => call.url.endsWith("/reattach")).body),
    { expected_revision: 3, before_id: "first", after_id: "second" });
  await choose(page, take, "remove"); await confirmationScan(page, evidence, "Remove take");
  await page.locator("#confirm-cancel").click();
  await cellReturn(page, `${take} td[data-action="options"]`);
  await choose(page, take, "remove"); await page.locator("#confirm-accept").click();
  await emptyTable(page, "#edition-takes", evidence);
  await choose(page, occurrence, "remove"); await confirmationScan(page, evidence, "Remove occurrence");
  await page.locator("#confirm-cancel").click();
  await cellReturn(page, `${occurrence} td[data-action="options"]`);
  for (let count = 2; count > 0; count -= 1) {
    await choose(page, '#edition-occurrences tr[data-row-key]:first-child', "remove"); await page.locator("#confirm-accept").click();
    await page.waitForFunction((wanted) => document.querySelectorAll('#edition-occurrences tr[data-row-key]').length === wanted, count - 1);
  }
  await emptyTable(page, "#edition-occurrences", evidence); await axe(page, evidence, "Empty prepared tables");
  evidence.checks.push("Prepared editions: disabled review, reattach cancel/save preserving revision/bindings, occurrence/take removal cancel/accept and empty tables");
}

async function queueJourney(page, evidence, calls) {
  await checkTable(page, "#queue-list"); await tableArrows(page, "#queue-list");
  const row = '#queue-list tr[data-row-key]:first-child';
  await menuJourney(page, row, evidence, "Queued track", "r");
  await choose(page, row, "info");
  assert.equal(await page.evaluate(() => window.batchFixture.trackInfo.path), "/music/1.flac");
  await trackInfoRoundtrip(page, row, evidence, "Queued track");
  const last = '#queue-list tr[data-row-key]:last-child';
  const beforeMove = calls.length;
  await choose(page, last, "top");
  await expect.poll(() => calls.length).toBe(beforeMove + 1);
  await expect(page.locator("#queue-announce")).toContainText("Moved Charlie to position 1 of 3.");
  await expect(page.locator("#queue-list")).not.toHaveAttribute("aria-busy", "true");
  await cellReturn(page, `${row} td[data-action="options"]`);
  assert.deepEqual(calls.at(-1).body.order, [2, 0, 1]);
  await choose(page, row, "remove"); await confirmationScan(page, evidence, "Remove queued track");
  const count = calls.length;
  await page.locator("#confirm-cancel").click(); assert.equal(calls.length, count);
  await cellReturn(page, `${row} td[data-action="options"]`);
  await choose(page, row, "remove"); await page.locator("#confirm-accept").click();
  await expect.poll(() => calls.length).toBe(count + 1);
  await expect(page.locator("#confirm-dialog")).not.toBeVisible();
  await expect(page.locator('#queue-list tr[data-row-key]')).toHaveCount(2);
  await expect(page.locator("#queue-announce")).toContainText("Removed Charlie from queue.");
  await expect(page.locator("#queue-list")).not.toHaveAttribute("aria-busy", "true");
  await cellReturn(page, `${row} td[data-action="options"]`);
  await page.evaluate(() => window.batchFixture.restrict("Start the station to change its queue."));
  const restrictedCount = calls.length;
  const restrictedOrder = await page.locator('#queue-list tr[data-row-key]').evaluateAll((rows) => rows.map((item) => item.dataset.rowKey));
  await choose(page, row, "down");
  const reason = await page.locator(`${row} button[data-action="down"]`).getAttribute("aria-describedby");
  assert.equal(await page.locator(`#${reason}`).textContent(), "Start the station to change its queue.");
  await page.waitForFunction(() => document.getElementById("queue-announce").textContent === "Start the station to change its queue.");
  assert.equal(calls.length, restrictedCount, "restricted keyboard action sends no request");
  assert.deepEqual(await page.locator('#queue-list tr[data-row-key]').evaluateAll((rows) => rows.map((item) => item.dataset.rowKey)), restrictedOrder, "restricted keyboard action preserves queue order");
  await axe(page, evidence, "Restricted queue menu"); await page.keyboard.press("Escape");
  await page.evaluate(() => window.batchFixture.restrict(""));
  await page.locator("#queue-clear").focus(); await page.keyboard.press("Enter");
  await confirmationScan(page, evidence, "Clear queue");
  await page.locator("#confirm-cancel").click();
  await page.locator("#queue-clear").focus(); await page.keyboard.press("Enter");
  await page.locator("#confirm-accept").click();
  await focused(page, "#queue-clear"); await emptyTable(page, "#queue-list", evidence); await axe(page, evidence, "Empty queue");
  evidence.checks.push("Queue: Track info dispatch; reorder permutation; Remove/Clear cancel and accept; explained unavailable mutation; empty table");
}

async function requestsJourney(page, evidence) {
  for (const host of ["#requests-list", "#requests-recent"]) { await checkTable(page, host); await tableArrows(page, host); }
  const row = '#requests-list tr[data-row-key]:first-child';
  await menuJourney(page, row, evidence, "Listener request", "t");
  const offAirReason = "Start the station to change the queue.";
  await page.evaluate((reason) => window.batchFixture.controls.setQueueDisabledReason(reason), offAirReason);
  const stoppedCalls = await page.evaluate(() => window.batchFixture.calls.length);
  const stoppedOrder = await page.locator('#requests-list tr[data-row-key]').evaluateAll((rows) => rows.map((item) => item.dataset.rowKey));
  async function refused(button) {
    await page.waitForFunction((reason) => document.getElementById("requests-status").textContent.trim() === reason, offAirReason);
    assert.equal(await button.getAttribute("aria-disabled"), "true");
    const id = await button.getAttribute("aria-describedby");
    assert.ok(id, "stopped action has a persistent reason");
    assert.equal(await page.locator(`#${id}`).textContent(), offAirReason);
    await page.evaluate(() => { document.getElementById("requests-status").textContent = ""; });
    const bounds = await button.boundingBox();
    await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await page.waitForFunction((reason) => document.getElementById("requests-status").textContent === reason, offAirReason);
    assert.equal(await page.evaluate(() => window.batchFixture.calls.length), stoppedCalls, "stopped pointer action sends no request");
  }
  await choose(page, row, "approve");
  await refused(page.locator(`${row} button[data-action="approve"]`));
  await choose(page, row, "play-next");
  await refused(page.locator(`${row} button[data-action="play-next"]`));
  assert.deepEqual(await page.locator('#requests-list tr[data-row-key]').evaluateAll((rows) => rows.map((item) => item.dataset.rowKey)), stoppedOrder, "stopped queue actions preserve requests");
  await axe(page, evidence, "Stopped listener request actions"); await page.keyboard.press("Escape");
  await choose(page, '#requests-list tr[data-row-key="request-3"]', "reject");
  await confirmationScan(page, evidence, "Turn down request while stopped");
  await page.locator("#confirm-accept").click();
  await page.waitForFunction(() => document.querySelectorAll('#requests-list tr[data-row-key]').length === 3);
  const offAirPost = { url: "/api/requests/request-3/reject", method: "POST", body: null };
  assert.deepEqual(await page.evaluate(() => window.batchFixture.calls.filter((call) => call.method === "POST")), [offAirPost]);
  await page.evaluate(() => window.batchFixture.controls.setQueueDisabledReason(""));
  assert.equal(await page.locator(`${row} button[data-action="approve"]`).getAttribute("aria-disabled"), "false");
  await page.locator(`${row} [aria-haspopup="menu"]`).focus(); await page.keyboard.press("Enter");
  assert.equal(await page.locator(`${row} button[data-action="play-next"]`).getAttribute("aria-disabled"), "false");
  await page.keyboard.press("Escape");
  const unavailable = '#requests-list tr[data-row-key="request-1"]';
  const approve = page.locator(`${unavailable} button[data-action="approve"]`);
  assert.equal(await approve.getAttribute("aria-disabled"), "true");
  const reason = await approve.getAttribute("aria-describedby");
  assert.ok(reason, "unavailable Approve has an associated reason before activation");
  assert.equal(await page.locator(`#${reason}`).textContent(), "No smooth position is available.");
  const beforeRefusal = await page.evaluate(() => ({ count: window.batchFixture.calls.length,
    order: window.batchFixture.state.pending.map((item) => item.id) }));
  await choose(page, unavailable, "approve");
  await page.waitForFunction(() => document.getElementById("requests-status").textContent === "No smooth position is available.");
  assert.deepEqual(await page.evaluate(() => ({ count: window.batchFixture.calls.length,
    order: window.batchFixture.state.pending.map((item) => item.id) })), beforeRefusal, "unavailable Approve sends no request and preserves waiting requests");
  await focused(page, `${unavailable} [role="menuitem"][data-action="approve"]`);
  await axe(page, evidence, "Unavailable listener approval");
  await page.keyboard.press("Escape");
  await cellReturn(page, `${unavailable} td[data-action="options"]`);
  await choose(page, row, "approve");
  await page.waitForFunction(() => document.querySelectorAll('#requests-list tr[data-row-key]').length === 2);
  assert.deepEqual(await page.evaluate(() => window.batchFixture.calls.filter((call) => call.method === "POST")),
    [offAirPost, { url: "/api/requests/request-0/approve", method: "POST", body: null }], "Approve keeps its route and empty payload");
  await choose(page, row, "play-next");
  await page.waitForFunction(() => document.querySelectorAll('#requests-list tr[data-row-key]').length === 1);
  assert.deepEqual(await page.evaluate(() => window.batchFixture.calls.filter((call) => call.method === "POST")), [
    offAirPost,
    { url: "/api/requests/request-0/approve", method: "POST", body: null },
    { url: "/api/requests/request-1/play-next", method: "POST", body: null },
  ], "Play next keeps its distinct route and empty payload");
  await choose(page, row, "reject"); await confirmationScan(page, evidence, "Turn down request");
  await page.locator("#confirm-cancel").click();
  await cellReturn(page, `${row} td[data-action="options"]`);
  assert.equal(await page.locator('#requests-list tr[data-row-key]').count(), 1);
  await choose(page, row, "reject"); await page.locator("#confirm-accept").click();
  await emptyTable(page, "#requests-list", evidence);
  assert.deepEqual(await page.evaluate(() => window.batchFixture.calls.filter((call) => call.method === "POST").at(-1)),
    { url: "/api/requests/request-2/reject", method: "POST", body: null }, "Turn down keeps its route and empty payload");
  await page.evaluate(async () => { window.batchFixture.state.recent = []; await window.batchFixture.controls.refresh(); });
  await emptyTable(page, "#requests-recent", evidence); await axe(page, evidence, "Empty listener tables");
  evidence.checks.push("Listener requests: stopped queue mutations refuse mouse/keyboard with reasons and no POST; Turn down remains available; restart restores queue actions; exact action routes/payloads; unavailable Approve reason/keyboard refusal without mutation; confirmation cancel/accept; recent cell navigation and both empty states");
}

async function voiceJourney(page, evidence, state) {
  const host = "#vt-list", row = `${host} tr[data-row-key="${"a".repeat(32)}"]`, unavailable = `${host} tr[data-row-key="${"b".repeat(32)}"]`;
  await page.locator(row).waitFor();
  await checkTable(page, host); await tableArrows(page, host);
  await menuJourney(page, row, evidence, "Voice booking", "c");
  await choose(page, unavailable, "cancel");
  assert.equal(await page.locator(`${unavailable} button[data-action="cancel"]`).getAttribute("aria-disabled"), "true");
  assert.ok(await page.locator(`${unavailable} button[data-action="cancel"]`).getAttribute("aria-describedby"));
  assert.equal(await page.locator('#confirm-dialog[open]').count(), 0);
  await axe(page, evidence, "Noncancellable voice booking menu"); await page.keyboard.press("Escape");
  await choose(page, row, "cancel"); await confirmationScan(page, evidence, "Cancel voice booking");
  assert.equal(await page.locator("#confirm-title").textContent(), "Cancel this booking?");
  await page.locator("#confirm-cancel").click();
  await cellReturn(page, `${row} td[data-action="options"]`);
  assert.equal(state.calls.filter((call) => call.method === "DELETE").length, 0);
  await choose(page, row, "cancel"); await page.locator("#confirm-accept").click();
  await page.waitForFunction(() => document.getElementById("vt-status").textContent.includes("Cancelled"));
  assert.equal(state.calls.filter((call) => call.method === "DELETE").length, 1);
  state.tracks = [];
  await page.evaluate(async () => { await window.batchFixture.controls.refresh(); });
  await emptyTable(page, host, evidence); await axe(page, evidence, "Empty voice bookings");
  evidence.checks.push("Voice bookings: explained noncancellable action, Cancel confirmation cancel/accept and empty table; no microphone access");
}

async function batchSurface(page, theme, surface, evidence) {
  const calls = [];
  const voice = { calls, accepting: false, reason: "No open song change.", slot: null,
    limits: { max_seconds: 600, max_bytes: 67108864, sample_rate: 44100 },
    tracks: [{ break_id: "a".repeat(32), label: "Queued announcement", state: "queued", cancellable: true, source: "voice_track" },
      { break_id: "b".repeat(32), label: "Completed announcement", state: "played", cancellable: false, source: "voice_track" }] };
  if (surface === "queue") await page.route("**/api/queue/**", async (route) => {
    calls.push({ url: route.request().url(), body: route.request().postDataJSON() });
    await route.fulfill({ contentType: "application/json", body: '{"ok":true}' });
  });
  if (surface === "voice") await page.route("**/api/voice-tracks**", async (route) => {
    const method = route.request().method(); calls.push({ method, url: route.request().url() });
    if (method === "DELETE") { voice.tracks[0].state = "dropped"; voice.tracks[0].cancellable = false; }
    const body = method === "DELETE" ? voice.tracks[0] : { ...voice, calls: undefined };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await mountBatch(page, theme, surface);
  const firstCell = page.locator('.collection-table tbody tr[data-row-key] td').first();
  await firstCell.waitFor();
  await firstCell.focus();
  await axe(page, evidence, `${surface} loaded`);
  await visuals(page, evidence, `${surface} loaded`);
  await firstCell.evaluate((element) => element.blur());
  await page.setViewportSize({ width: 320, height: 800 });
  await firstCell.focus();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `${surface} 320px page reflow`);
  await visuals(page, evidence, `${surface} 320px`);
  await page.emulateMedia({ forcedColors: "active" });
  await visuals(page, evidence, `${surface} forced colors`);
  await page.emulateMedia({ forcedColors: "none" });
  await page.setViewportSize({ width: 1280, height: 900 });
  if (surface === "shows") await showsJourney(page, evidence);
  else if (surface === "editions") await editionsJourney(page, evidence);
  else if (surface === "queue") await queueJourney(page, evidence, calls);
  else if (surface === "requests") await requestsJourney(page, evidence);
  else await voiceJourney(page, evidence, voice);
}

// Batch 3 uses the same production markup and browser interactions. The small
// in-memory transport also serves requestJson callers (liners/profiles/devices).
async function mountBatchThree(page, theme, surface) {
  await page.goto(BASE);
  await page.evaluate(async ({ theme, surface }) => {
    document.documentElement.dataset.theme = theme;
    const source = new globalThis.DOMParser().parseFromString(await (await fetch("/source-index")).text(), "text/html");
    const fixture = document.getElementById("fixture");
    const ids = { categories: "categories-card", rules: "intro-rules", intros: "intros-editor",
      ads: "ads-panel", liners: "spanel-liners", profiles: "spanel-access" };
    let card;
    if (surface === "devices") {
      card = document.createElement("section");
      card.id = "audit-access";
      card.setAttribute("aria-label", "Browser access");
      for (const id of ["access-status", "access-note", "access-controls"]) card.append(source.getElementById(id));
      document.body.append(source.getElementById("device-rename-dialog"));
    } else card = source.getElementById(ids[surface]);
    card.hidden = false;
    card.open = true;
    // A standalone settings card has no surrounding Settings tablist.
    if (card.getAttribute("role") === "tabpanel") {
      card.removeAttribute("role"); card.removeAttribute("aria-labelledby");
    }
    for (const details of card.querySelectorAll("details")) details.open = true;
    if (["categories", "rules"].includes(surface)) {
      const library = document.createElement("section");
      library.id = "panel-library";
      library.setAttribute("aria-label", "Library tools");
      library.append(card); fixture.append(library);
    } else fixture.append(card);
    document.body.append(source.getElementById("confirm-dialog"));
    const clone = (value) => JSON.parse(JSON.stringify(value));
    const state = window.batchThree = { calls: [], controls: null };
    const categories = { categories: [{ id: "c1", name: "Rock songs", source: "query", query: "genre:Rock", tracks: 1, used_by: [] }],
      playlists: [], folders: [], genres: ["Rock"], beets_fields: [], beets: true };
    const rules = { rules: [{ id: "r1", label: "Rock introduction", match: "all", conditions: [{ field: "genre", operator: "is", value: "Rock" }],
      clip_ids: ["l1"], chance: 100, cooldown_minutes: 0 }], liners: [{ id: "l1", label: "sample.wav" }],
      liner_folders: [], sources: [], genres: ["Rock"], beets_fields: [], categories: [] };
    const intros = { track: { id: "track1", title: "Song", artist: "Artist" }, active: true, limits: { max_bytes: 41943040 },
      song: [{ id: "i1", label: "Song intro", rules_text: "Plays every time", speech_s: 3, playable: true }],
      artist: [{ id: "i2", label: "Artist intro", rules_text: "Plays every time", speech_s: 2, playable: true }] };
    const ads = { station: "default", enabled: true, reason: null, problem: null, timezone: "UTC",
      limits: { max_bytes: 1024, max_seconds: 120, max_spots: 200, max_breaks: 50, max_spots_per_break: 10 },
      settings: { separation_minutes: 15, late_minutes: 10, miss_after_minutes: 30 }, pending: [], recent: [],
      spots: [{ id: "spot1", title: "Autumn sale", advertiser: "Example Motors", campaign: "Autumn", category: "Cars",
        start_date: null, end_date: null, dates_text: "Any date", in_flight: true, max_per_day: 4, max_per_hour: 1,
        enabled: true, seconds: 30, plays_today: 1, booked: false, missing: false },
      { id: "spot2", title: "Booked sale", advertiser: "Second advertiser", campaign: "", category: "",
        start_date: null, end_date: null, dates_text: "Any date", in_flight: true, max_per_day: 0, max_per_hour: 0,
        enabled: true, seconds: 20, plays_today: 1, booked: true, missing: false }],
      breaks: [{ id: "break1", name: "Quarter past", minutes: [15, 45], days: [], from_hour: 0, until_hour: 24,
        start_date: null, end_date: null, max_spots: 3, max_seconds: 0, enabled: true,
        when_text: "At :15, :45 past the hour", next_text: "Tomorrow at 12:15" }] };
    const adReport = { from: "2026-10-01", to: "2026-10-31", count: 1, truncated: false, damaged: 0,
      totals: { aired: 1, late: 0, missed: 0, waiting: 0, missed_breaks: 0 },
      summary: [{ advertiser: "Example Motors", campaign: "Autumn", aired: 1, late: 0, missed: 0, interrupted: 0, waiting: 0, seconds: 30 }],
      rows: [{ id: "air1", scheduled_text: "31 October 2026 at 12:15:00", aired_text: "31 October 2026 at 12:15:00",
        delay_minutes: 0, status: "aired", status_text: "Aired", break_name: "Quarter past", source_text: "Minutes past the hour",
        spot_id: "spot1", title: "Autumn sale", advertiser: "Example Motors", campaign: "Autumn", seconds: 30, reason: "" }] };
    const liners = { config: {}, items: [{ id: "l1", name: "ID.wav", path: "/liners/ID.wav", editable: true,
      dates_text: "", playable: true, not_before: null, not_after: null }], files: ["ID.wav"] };
    const profiles = { profiles: ["Morning", "Evening"], active: "Morning" };
    const devices = { pairing: true, devices: [{ device_id: "d1", name: "Desk", paired_at: 1, last_seen_at: 2, current: false }] };
    Object.assign(state, { categories, rules, intros, ads, adReport, liners, profiles, devices });
    const request = async (url, options = {}) => {
      const method = options.method || "GET", body = options.body ? JSON.parse(options.body) : {};
      state.calls.push({ url, method, body });
      if (url === "/api/categories/check") return { tracks: 1 };
      if (url === "/api/categories") return clone(categories);
      if (url === "/api/categories/c1" && method === "PUT") {
        Object.assign(categories.categories[0], body);
        return clone({ ...categories, category: categories.categories[0] });
      }
      if (url === "/api/intros/rules") return clone(rules);
      if (url === "/api/intros/rules/r1" && method === "PUT") return clone(Object.assign(rules.rules[0], body));
      if (url.startsWith("/api/intros?")) return clone(intros);
      if (url === "/api/ads") return clone(ads);
      if (url.startsWith("/api/ads/report?")) return clone(adReport);
      if (url === "/api/ads/spots/spot1" && method === "PUT") return clone(Object.assign(ads.spots[0], body));
      if (url === "/api/ads/breaks/break1" && method === "PUT") return clone(Object.assign(ads.breaks[0], body));
      if (url === "/api/ads/breaks/break1/run") return { name: ads.breaks[0].name, spots: 1, seconds: 30 };
      if (url === "/api/liners") return clone(liners);
      if (url === "/api/liners/dates/l1" && method === "PUT") return clone(Object.assign(liners.items[0], body));
      if (url === "/api/profiles") return clone(profiles);
      if (url === "/api/profiles/Evening/apply") { profiles.active = "Evening"; return clone(profiles); }
      if (url === "/api/devices") return clone(devices);
      if (url === "/api/devices/d1" && method === "PATCH") return clone(Object.assign(devices.devices[0], body));
      throw new Error(`Unexpected Batch 3 fixture ${method} ${url}`);
    };
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (url, options) => String(url).startsWith("/api/")
      ? new globalThis.Response(JSON.stringify(await request(String(url), options)), { headers: { "Content-Type": "application/json" } })
      : nativeFetch(url, options);
    const get = (id) => document.getElementById(id);
    if (surface === "categories") {
      const { installCategoriesPanel } = await import(new URL("/modules/categories-panel.js", window.location.href).href);
      state.controls = installCategoriesPanel({ request });
    } else if (surface === "rules") {
      const { installIntroRulesPanel } = await import(new URL("/modules/intro-rules-panel.js", window.location.href).href);
      state.controls = installIntroRulesPanel({ request });
    } else if (surface === "intros") {
      const { installMatchedIntros } = await import(new URL("/modules/matched-intros.js", window.location.href).href);
      // Playback is a separate hardware concern; exercise the actual toggle.
      get("intro-playback").play = async () => {};
      get("intro-playback").pause = () => {};
      state.controls = installMatchedIntros({ request });
      await state.controls.load({ id: "track1", kind: "music", title: "Song", artist: "Artist" });
      return;
    } else if (surface === "ads") {
      const { installAdsPanel } = await import(new URL("/modules/ads-panel.js", window.location.href).href);
      state.controls = installAdsPanel({ request });
    } else if (surface === "liners") {
      const { installLiners } = await import(new URL("/modules/liners.js", window.location.href).href);
      state.controls = installLiners({ lnFileList: get("ln-file-list"), lnStatus: get("ln-status"), lnBrowseStatus: get("ln-browse-status") }, { canPlay: () => false });
    } else if (surface === "profiles") {
      const { installProfiles } = await import(new URL("/modules/profiles.js", window.location.href).href);
      state.controls = installProfiles({ card, nameInput: get("profile-name"), saveButton: get("profile-save"), list: get("profile-list"), status: get("profiles-status") });
    } else {
      const { installAccess } = await import(new URL("/modules/devices.js", window.location.href).href);
      state.controls = installAccess({ card, note: get("access-note"), controls: get("access-controls"), signOut: get("access-sign-out"),
        refresh: get("access-refresh"), list: get("device-list"), status: get("access-status") }, { onSignedOut: () => {} });
      return;
    }
    await state.controls.refresh();
  }, { theme, surface });
  await page.addScriptTag({ url: `${BASE}/axe.js` });
}

async function batchThreeEditor(page, evidence, { row, action = "edit", field, save, value, label }) {
  const cell = `${row} td[data-action="options"]`;
  await traceEditorOpen(page, `${row} [role="menuitem"][data-action="${action}"]`, field, evidence, row);
  await editorDescription(page);
  await axe(page, evidence, `${label} editor`);
  await visuals(page, evidence, `${label} editor`);
  await page.keyboard.press("Escape");
  await cellReturn(page, cell);
  // The former primary action is the first menu item.
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await page.locator(field).fill(value);
  await page.locator(save).focus();
  await page.keyboard.press("Enter");
  await cellReturn(page, cell);
  evidence.checks.push(`${label}: keyboard editor open from its menu, one focus event, Escape to the Actions cell, edit/save and grid navigation after return`);
}

async function batchThreeSurface(page, theme, surface, evidence) {
  await mountBatchThree(page, theme, surface);
  const hosts = { categories: ["#categories-list"], rules: ["#intro-rules-list"], intros: ["#intros-song-list", "#intros-artist-list"],
    ads: ["#ads-spots", "#ads-breaks"], liners: ["#ln-file-list"], profiles: ["#profile-list"], devices: ["#device-list"] }[surface];
  for (const host of hosts) {
    await page.locator(`${host} tr[data-row-key]`).first().waitFor();
    await checkTable(page, host); await tableArrows(page, host);
  }
  const first = page.locator(`${hosts[0]} tr[data-row-key] td`).first();
  await first.focus();
  await axe(page, evidence, `${surface} loaded`); await visuals(page, evidence, `${surface} loaded`);
  await first.evaluate((element) => element.blur());
  await page.setViewportSize({ width: 320, height: 800 });
  await first.focus();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, `${surface} 320px page reflow`);
  await visuals(page, evidence, `${surface} 320px`);
  await page.emulateMedia({ forcedColors: "active" });
  await visuals(page, evidence, `${surface} forced colors`);
  await page.emulateMedia({ forcedColors: "none" });
  await page.setViewportSize({ width: 1280, height: 900 });
  const specs = {
    categories: { row: '#categories-list tr[data-row-key="c1"]', field: "#categories-name", save: "#categories-save", value: "Rock revised", label: "Category" },
    rules: { row: '#intro-rules-list tr[data-row-key="r1"]', field: "#intro-rules-label", save: "#intro-rules-save", value: "Intro revised", label: "Intro rule" },
    liners: { row: '#ln-file-list tr[data-row-key="l1"]', action: "dates", field: "#ln-not-before", save: "#ln-dates-save", value: "2026-10-01", label: "Liner dates" },
    devices: { row: '#device-list tr[data-row-key="d1"]', action: "rename", field: "#device-rename-name", save: "#device-rename-save", value: "Desk revised", label: "Device rename" },
  };
  if (specs[surface]) {
    const spec = specs[surface];
    await menuJourney(page, spec.row, evidence, spec.label, surface === "devices" ? "r" : "d");
    await batchThreeEditor(page, evidence, spec);
    await choose(page, spec.row, surface === "devices" ? "revoke" : "delete");
    await confirmationScan(page, evidence, `${spec.label} delete`);
    await page.keyboard.press("Escape");
    await cellReturn(page, `${spec.row} td[data-action="options"]`);
  } else if (surface === "intros") {
    for (const host of hosts) {
      const row = `${host} tr[data-row-key]`;
      await menuJourney(page, row, evidence, host, "r");
      await choose(page, row, "listen");
      await expect(page.locator(`${row} button[data-action="listen"]`)).toHaveText("Stop listening");
      await focused(page, `${row} td[data-action="options"]`);
      await choose(page, row, "listen");
      await expect(page.locator(`${row} button[data-action="listen"]`)).toHaveText("Listen");
      await choose(page, row, "remove"); await confirmationScan(page, evidence, `${host} remove`);
      await page.keyboard.press("Escape"); await cellReturn(page, `${row} td[data-action="options"]`);
    }
    await traceEditorOpen(page, "#intro-add", "#intro-file", evidence);
    await editorDescription(page); await axe(page, evidence, "Matched intro upload editor");
    await page.keyboard.press("Escape"); await focused(page, "#intro-add");
    evidence.checks.push("Song and artist intros: Listen/Stop toggle, Remove cancellation and upload editor focus return; audio playback stubbed");
  } else if (surface === "profiles") {
    const row = '#profile-list tr[data-row-key="Evening"]';
    await menuJourney(page, row, evidence, "Profile");
    await choose(page, row, "activate");
    await expect(page.locator(`${row} td[data-action="active"]`)).toHaveText("Yes");
    await choose(page, row, "reset"); await confirmationScan(page, evidence, "Profile reset");
    await page.keyboard.press("Escape"); await cellReturn(page, `${row} td[data-action="options"]`);
    await choose(page, row, "delete"); await confirmationScan(page, evidence, "Profile delete");
    await page.keyboard.press("Escape"); await cellReturn(page, `${row} td[data-action="options"]`);
    evidence.checks.push("Profiles: keyboard Activate from the menu updates Active column; Reset/Delete confirmations cancel to exact Actions cell");
  } else {
    for (const spec of [
      { row: '#ads-spots tr[data-row-key="spot1"]', field: "#ads-spot-title", save: "#ads-spot-save", value: "Sale revised", label: "Ad spot" },
      { row: '#ads-breaks tr[data-row-key="break1"]', field: "#ads-break-name", save: "#ads-break-save", value: "Break revised", label: "Ad break" },
    ]) { await menuJourney(page, spec.row, evidence, spec.label); await batchThreeEditor(page, evidence, spec); }
    const booked = '#ads-spots tr[data-row-key="spot2"]';
    const deletesBefore = await page.evaluate(() => window.batchThree.calls.filter(({ method }) => method === "DELETE").length);
    await choose(page, booked, "delete");
    const disabled = page.locator(`${booked} button[data-action="delete"]`);
    await expect(disabled).toHaveAttribute("aria-disabled", "true");
    assert.ok(await disabled.getAttribute("aria-describedby"), "Booked spot deletion explains its restriction");
    assert.equal(await page.locator('#confirm-dialog[open]').count(), 0);
    assert.equal(await page.evaluate(() => window.batchThree.calls.filter(({ method }) => method === "DELETE").length), deletesBefore);
    await axe(page, evidence, "Booked spot menu"); await page.keyboard.press("Escape");
    await page.locator("#ads-report-from").fill("2026-10-01");
    await page.locator("#ads-report-to").fill("2026-10-31");
    await page.locator("#ads-report-show").focus(); await page.keyboard.press("Enter");
    // #712: plain native tables with row headers inside focusable scrolling
    // regions, read with browse-mode table keys rather than grid arrows.
    for (const host of ["#ads-totals", "#ads-rows"]) {
      await page.locator(`${host} tr`).first().waitFor();
      const rows = await page.locator(`${host} tr`).count();
      assert.equal(await page.locator(`${host} tr > th:first-child[scope="row"]`).count(), rows, `${host} rows start with a row header`);
      assert.equal(await page.locator(`${host} [tabindex], ${host} button, ${host} [role]`).count(), 0, `${host} is a plain read-only table`);
      const wrap = page.locator(`${host}-wrap`);
      await expect(wrap).toHaveAttribute("role", "region");
      await expect(wrap).toHaveAccessibleName(await page.locator(`${host}-wrap caption`).textContent());
    }
    await page.locator("#ads-report-export").focus();
    await page.keyboard.press("Tab");
    await focused(page, "#ads-totals-wrap");
    await page.keyboard.press("Tab");
    await focused(page, "#ads-rows-wrap");
    await axe(page, evidence, "Ad report"); await visuals(page, evidence, "Ad report");
    await page.locator("#ads-rows-wrap").evaluate((element) => element.blur());
    await page.setViewportSize({ width: 320, height: 800 });
    await page.locator("#ads-rows-wrap").focus();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, "Ad report 320px page reflow");
    await visuals(page, evidence, "Ad report 320px");
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.locator("#ads-report-show").focus();
    await page.evaluate(() => {
      Object.assign(window.batchThree.adReport, { rows: [], summary: [], count: 0 });
      document.getElementById("ads-report-form").requestSubmit();
    });
    await expect(page.locator("#ads-report-summary")).toContainText("No ad breaks ran");
    await expect(page.locator("#ads-totals-wrap")).toBeHidden();
    await expect(page.locator("#ads-rows-wrap")).toBeHidden();
    await axe(page, evidence, "Empty ad report");
    evidence.checks.push("Ads: both editors and menus, booked Delete linked reason/no request; report totals and outcomes are plain native tables with row headers in Tab-reachable scrolling regions, hidden when empty");
  }
  evidence.checks.push(`${surface}: collection Tab/Shift+Tab and cell arrows, axe, 320px reflow, target sizes and forced-colors focus`);
}

async function audit(name, launcher) {
  const browser = await launcher.launch({ headless: true });
  const results = { browser: name, version: browser.version(), fixture: "Production index/CSS/modules; in-memory API replies; loopback static server", themes: [], limitations: ["No live AutoDJ backend, station routing or persistence", "No screen-reader speech capture or human listening", "Chromium forced-colors emulation is not Windows high-contrast hardware evidence", "320 CSS pixels models reflow width; browser zoom and custom text spacing are not exercised", "Computed focus-ring and button-text contrast are measured against composited CSS backgrounds; pixel-level appearance still needs platform review"] };
  try {
    for (const theme of ["light", "dark"]) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: theme === "light" ? "dark" : "light", reducedMotion: "reduce" });
      const page = await context.newPage();
      page.setDefaultTimeout(10000);
      const evidence = { theme, checks: [], axe: [], visual: [], errors: [] };
      results.themes.push(evidence);
      page.on("pageerror", (error) => evidence.errors.push(error.message));
      console.log(`[${name}/${theme}] Schedule and clockwheels`);
      try { await mount(page, theme); await journey(page, evidence); }
      catch (error) { evidence.errors.push(error.stack || String(error)); console.error(`[${name}/${theme}] ${error.stack || error}`); }
      finally { await page.close(); }
      try {
        for (const surface of ["shows", "editions", "queue", "requests", "voice"]) {
          const batchPage = await context.newPage();
          console.log(`[${name}/${theme}] ${surface}`);
          batchPage.setDefaultTimeout(10000);
          batchPage.on("pageerror", (error) => evidence.errors.push(`${surface}: ${error.message}`));
          try { await batchSurface(batchPage, theme, surface, evidence); }
          catch (error) { evidence.errors.push(`${surface}: ${error.stack || String(error)}`); console.error(`[${name}/${theme}/${surface}] ${error.stack || error}`); }
          finally { await batchPage.close(); }
        }
        for (const surface of ["categories", "rules", "intros", "ads", "liners", "profiles", "devices"]) {
          const batchPage = await context.newPage();
          console.log(`[${name}/${theme}] ${surface}`);
          batchPage.setDefaultTimeout(10000);
          batchPage.on("pageerror", (error) => evidence.errors.push(`${surface}: ${error.message}`));
          try { await batchThreeSurface(batchPage, theme, surface, evidence); }
          catch (error) { evidence.errors.push(`${surface}: ${error.stack || String(error)}`); console.error(`[${name}/${theme}/${surface}] ${error.stack || error}`); }
          finally { await batchPage.close(); }
        }
      }
      catch (error) { evidence.errors.push(error.stack || String(error)); }
      finally { await context.close(); }
    }
    return results;
  } finally { await browser.close(); }
}

try {
  await runAudit({ audit, report, validate: (_name, result) => {
    assert.equal(result.themes.length, 2);
    for (const theme of result.themes) assert.deepEqual(theme.errors, [], `${theme.theme} audit errors`);
  } });
} finally {
  await new Promise((done) => server.close(done));
}
