// Real browser controls and production markup; API replies stay in memory.
// This audit starts only a loopback static server and requires no AutoDJ backend.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
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

async function checkTable(page, host) {
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
      .filter(visible).map((element) => ({ label: element.getAttribute("aria-label") || element.textContent, width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height }))
      .filter(({ width, height }) => width < 24 || height < 24);
    const active = document.activeElement;
    const style = window.getComputedStyle(active);
    const bounds = active.getBoundingClientRect();
    const focusBackground = Number.parseFloat(style.outlineOffset) >= 0 ? active.parentElement : active;
    const textContrast = [...document.querySelectorAll(".collection-table button, .row-actions-menu [role=menuitem]")]
      .filter(visible).map((element) => contrast(rgba(window.getComputedStyle(element).color), background(element)));
    return { small, minimumButtonTextContrast: Math.min(...textContrast), focus: { label: active.getAttribute("aria-label") || active.textContent.trim(), contrast: contrast(rgba(style.outlineColor), background(focusBackground)), outlineStyle: style.outlineStyle, outlineWidth: Number.parseFloat(style.outlineWidth), bounds: bounds.toJSON(), viewport: { width: window.innerWidth, height: window.innerHeight }, visible: bounds.bottom > 0 && bounds.top < window.innerHeight && bounds.right > 0 && bounds.left < window.innerWidth } };
  });
  evidence.visual.push({ state, ...measurements });
  assert.deepEqual(measurements.small, [], `24px targets ${state}`);
  assert.ok(measurements.focus.outlineWidth >= 2 && measurements.focus.outlineStyle !== "none", `visible focus ${state}`);
  assert.equal(measurements.focus.visible, true, `focus in viewport ${state}`);
  assert.ok(measurements.focus.contrast >= 3, `focus contrast ${state}: ${measurements.focus.contrast}`);
  assert.ok(measurements.minimumButtonTextContrast >= 4.5, `button text contrast ${state}`);
}

async function menuJourney(page, row, evidence, state) {
  const cell = page.locator(`${row} td[data-action="options"]`);
  const button = page.locator(`${row} [aria-haspopup="menu"]`);
  await cell.focus();
  await page.keyboard.press("Enter");
  const menuId = await button.getAttribute("aria-controls");
  const menu = `#${menuId}`;
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  assert.equal(await button.getAttribute("aria-expanded"), "true");
  await page.keyboard.press("ArrowDown");
  await focused(page, `${menu} [role="menuitem"]:nth-child(2)`);
  await page.keyboard.press("End");
  await focused(page, `${menu} [role="menuitem"]:last-child`);
  await page.keyboard.press("Home");
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  await page.keyboard.press("d");
  await focused(page, `${menu} [role="menuitem"]`);
  assert.match(await page.evaluate(() => document.activeElement.textContent), /^d/i, "type-ahead chooses a matching item");
  await axe(page, evidence, `${state} menu`);
  await visuals(page, evidence, `${state} menu`);
  await page.keyboard.press("Escape");
  await focused(page, `${row} [aria-haspopup="menu"]`);
  await visuals(page, evidence, `${state} Options button`);
  assert.equal(await button.getAttribute("aria-expanded"), "false");
  await page.keyboard.press("ArrowUp");
  await focused(page, `${menu} [role="menuitem"]:last-child`);
  await page.keyboard.press("Escape");
  await page.keyboard.press("ArrowDown");
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  await page.keyboard.press("Escape");
  await page.keyboard.down("Space");
  assert.equal(await button.getAttribute("aria-expanded"), "false", "button Space waits for release");
  await page.keyboard.up("Space");
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  assert.equal(await button.getAttribute("aria-expanded"), "true", "Space release leaves popup open");
  await page.keyboard.press("Escape");
  await cell.focus();
  await page.keyboard.press("Space");
  await focused(page, `${menu} [role="menuitem"]:first-child`);
  await page.keyboard.press("Tab");
  assert.equal(await button.getAttribute("aria-expanded"), "false");
  assert.equal(await button.evaluate((element) => element.closest("table").contains(document.activeElement)), false, "Tab leaves collection");
  assert.equal(await page.locator(menu).evaluate((element) => element.contains(document.activeElement)), false, "Tab leaves popup");
  evidence.checks.push(`${state}: Enter/Space, button Space release, button Up/Down, arrows, Home/End, type-ahead, Escape and Tab`);
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
  await axe(page, evidence, "Station stopped menu");
  const before = await page.evaluate(() => window.collectionFixture.calls.length);
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
    assert.equal(await page.evaluate(() => window.collectionFixture.calls.length), before, `off-air ${method} sends no request`);
    assert.equal(await button.getAttribute("aria-expanded"), "true");
  }
  await page.keyboard.press("Escape");
  await page.evaluate(() => window.collectionFixture.scheduler.setOffAir(false));
  assert.equal(await item.getAttribute("aria-disabled"), "false");
  assert.equal(await item.getAttribute("aria-describedby"), null);
  evidence.checks.push("Station stopped: mouse, Enter, Space and type-ahead/Enter send no request; each announces the linked disabled reason");
}

async function journey(page, evidence) {
  for (const host of ["#schedule-events", "#schedule-upcoming", "#schedule-runs", "#clock-list", "#clock-slots"]) await checkTable(page, host);
  evidence.checks.push("All five tables: one Tab stop and actual Tab/Shift+Tab entry/exit");
  const eventRow = '#schedule-events tr[data-row-key="event-0"]';
  await menuJourney(page, eventRow, evidence, "Schedule");
  await stoppedStation(page, eventRow, evidence);
  await page.locator(`${eventRow} td[data-action="options"]`).focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => window.collectionFixture.calls.some(({ url }) => url.endsWith("/event-0/run")));
  assert.equal(await page.evaluate(() => window.collectionFixture.calls.filter(({ url }) => url.endsWith("/event-0/run")).length), 1);
  evidence.checks.push("Schedule: Enter invokes Run now through the real menu handler");
  await page.locator(`${eventRow} [aria-haspopup="menu"]`).focus();
  await page.keyboard.press("Space");
  await focused(page, `${eventRow} [role="menuitem"][data-action="run"]`);
  await page.keyboard.press("Space");
  await focused(page, `${eventRow} [aria-haspopup="menu"]`);
  assert.equal(await page.locator(`${eventRow} [aria-haspopup="menu"]`).getAttribute("aria-expanded"), "false");
  assert.equal(await page.evaluate(() => window.collectionFixture.calls.filter(({ url }) => url.endsWith("/event-0/run")).length), 2);
  evidence.checks.push("Schedule: Space opens on release and activates once without reopening on focus return");

  await page.locator(`${eventRow} td[data-action="edit"]`).focus();
  await page.keyboard.press("Enter");
  await focused(page, "#schedule-name");
  await axe(page, evidence, "Schedule editor");
  await page.keyboard.press("Escape");
  await focused(page, `${eventRow} td[data-action="edit"]`);
  await page.keyboard.press("Enter");
  await page.locator("#schedule-name").fill("Morning revised");
  await page.locator("#schedule-save").focus();
  await page.keyboard.press("Enter");
  await focused(page, `${eventRow} td[data-action="edit"]`);
  assert.equal(await page.locator(`${eventRow} td`).first().textContent(), "Morning revised");
  evidence.checks.push("Schedule editor: keyboard open, Escape cancel, save and return to Edit cell");

  const stepRow = '#clock-slots tr[data-index="0"]';
  await menuJourney(page, stepRow, evidence, "Clockwheel step");
  await page.locator(`${stepRow} td[data-action="options"]`).focus();
  await page.keyboard.press("Enter");
  await focused(page, `${stepRow} [data-action="move-up"]`);
  assert.equal(await page.locator(`${stepRow} button[data-action="move-up"]`).getAttribute("aria-disabled"), "true");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.getElementById("clock-status").textContent.includes("already the first step"));
  assert.equal(await page.locator('#clock-slots tr[data-index]').count(), 2);
  await page.keyboard.press("Escape");
  evidence.checks.push("Clockwheel step: disabled Move up stays reachable and announces its reason without mutation");
  await page.locator(`${stepRow} td[data-action="edit"]`).focus();
  await page.keyboard.press("Enter");
  await focused(page, "#clock-step-type");
  await axe(page, evidence, "Clockwheel step editor");
  await page.locator("#clock-step-selection").selectOption("random");
  await page.locator("#clock-add-step").focus();
  await page.keyboard.press("Enter");
  await focused(page, `${stepRow} td[data-action="edit"]`);
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
  await axe(page, evidence, "Clockwheel Rename editor");
  await page.locator("#clock-editor-name").fill("Evening revised");
  await page.locator('#clock-name-form button[type="submit"]').focus();
  await page.keyboard.press("Enter");
  await focused(page, '#clock-list tr[data-row-key="Evening revised"] td[data-action="open"]');
  assert.equal(await page.locator("#clock-open-name").textContent(), "Morning");
  evidence.checks.push("Saved Rename targets Evening while Morning stays open; editor returns to renamed row");

  await page.locator("#clock-create").focus();
  await page.keyboard.press("Enter");
  await focused(page, "#clock-editor-name");
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

  await page.locator(`${eventRow} td[data-action="edit"]`).focus();
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
  await page.keyboard.press("ArrowDown");
  await visuals(page, evidence, "Forced colors menu");
  await page.keyboard.press("Escape");
  await page.emulateMedia({ forcedColors: "none" });
  evidence.checks.push("320px page reflow and popup bounds; forced-colors menu focus and targets");

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.locator(`${eventRow} td[data-action="options"]`).focus();
  await page.keyboard.press("Enter");
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
  await focused(page, '#schedule-events tr[data-row-key="event-1"] td[data-action="options"]');
  assert.equal(await page.locator('[role="menu"]:visible').count(), 0, "removed native popover closes");
  await page.waitForFunction(() => document.getElementById("schedule-status").textContent.includes("Focus moved to Options for Evening"));
  await page.keyboard.press("Enter");
  await page.evaluate(async () => {
    window.collectionFixture.events.splice(0);
    await window.collectionFixture.scheduler.refresh();
  });
  await focused(page, "#schedule-add");
  assert.equal(await page.locator("#schedule-events .no-results").count(), 1);
  assert.equal(await page.locator('[role="menu"]:visible').count(), 0);
  await page.waitForFunction(() => document.getElementById("schedule-status").textContent.includes("Focus moved to Add event"));
  evidence.checks.push("Native open popup survives passive refresh; row removal closes it and announces adjacent-row/final Add fallback");
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
      try { await mount(page, theme); await journey(page, evidence); }
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
