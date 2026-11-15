// Real wheel gestures against the shared dialog guard and stylesheet (#766).
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { chromium, expect, firefox, webkit } from "@playwright/test";

const STATIC = new URL("../../src/autodj/static/", import.meta.url);
const FIXTURE = `<!doctype html><html><body>
  <button id="opener" style="position:absolute;top:550px">Open</button>
  <div style="height:3000px"></div>
  <dialog id="outer" class="editor-dialog" style="width:400px;height:400px;margin:auto">
    <button id="outer-cancel" autofocus>Cancel</button>
    <button id="nested-opener">Open another dialog</button>
    <div id="content" style="height:1400px">Scrollable content</div>
  </dialog>
  <dialog id="inner" class="editor-dialog" style="width:300px;height:200px;margin:auto">
    <button id="inner-cancel" autofocus>Cancel</button>
  </dialog>
</body></html>`;

export async function auditDialogScroll(browser) {
  const context = await browser.newContext({ viewport: { width: 1000, height: 700 }, reducedMotion: "reduce" });
  try {
    const page = await context.newPage();
    await page.route("http://dialog-scroll.test/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/") return route.fulfill({ contentType: "text/html", body: FIXTURE });
      if (!/^\/modules\/[\w-]+\.js$/.test(path)) return route.abort();
      return route.fulfill({ contentType: "text/javascript", body: await readFile(new URL(`.${path}`, STATIC), "utf8") });
    });
    await page.goto("http://dialog-scroll.test/");
    await page.addStyleTag({ content: await readFile(new URL("app.css", STATIC), "utf8") });
    await page.evaluate(async () => {
      window.dialogGuard = await import(new URL("/modules/dialog-guard.js", window.location.href).href);
      window.scrollTo(0, 500);
      document.getElementById("opener").focus();
      window.dialogGuard.openDialog(document.getElementById("outer"), { home: document.getElementById("outer-cancel") });
    });
    const outer = page.locator("#outer");
    await expect(outer).toBeVisible();
    await expect(page.locator("#outer-cancel")).toBeFocused();
    expect(await outer.evaluate((node) => node.matches(":modal"))).toBe(false);
    const origin = await page.evaluate(() => window.scrollY);
    expect(origin).toBe(500);

    // Wheel dispatch returns before asynchronous scrolling; allow the gesture
    // to settle before asserting that neither the page nor focus moved.
    const wheel = async (x, y, delta) => {
      await page.mouse.move(x, y);
      await page.mouse.wheel(0, delta);
      await page.waitForTimeout(250);
    };
    const backdropWheel = async () => {
      expect(await page.evaluate(() => document.elementFromPoint(10, 10)?.id)).toBe("dialog-backdrop");
      await wheel(10, 10, 350);
      expect(await page.evaluate(() => window.scrollY)).toBe(origin);
    };
    await backdropWheel();
    await expect(page.locator("#outer-cancel")).toBeFocused();

    const bounds = await outer.boundingBox();
    await wheel(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2, 200);
    expect(await outer.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
    expect(await page.evaluate(() => window.scrollY)).toBe(origin);
    for (const [position, delta] of [[10000, 350], [0, -350]]) {
      await outer.evaluate((node, top) => { node.scrollTop = top; }, position);
      await wheel(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2, delta);
      expect(await page.evaluate(() => window.scrollY)).toBe(origin);
    }

    await page.locator("#nested-opener").focus();
    await page.evaluate(() => window.dialogGuard.openDialog(document.getElementById("inner"), {
      home: document.getElementById("inner-cancel"),
    }));
    await backdropWheel();
    await page.keyboard.press("Escape");
    await expect(page.locator("#inner")).not.toBeVisible();
    await expect(page.locator("#nested-opener")).toBeFocused();
    await backdropWheel();

    // A short dialog has no internal overflow to absorb a wheel gesture.
    await page.locator("#content").evaluate((node) => { node.style.height = "20px"; });
    await outer.evaluate((node) => { node.scrollTop = 0; });
    await wheel(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2, 350);
    expect(await page.evaluate(() => window.scrollY)).toBe(origin);
    await page.keyboard.press("Tab");
    await expect(page.locator("#outer-cancel")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(outer).not.toBeVisible();
    await expect(page.locator("#opener")).toBeFocused();
    expect(await page.evaluate(() => window.scrollY)).toBe(origin);
    await wheel(10, 10, 350);
    expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(origin);
  } finally {
    await context.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  for (const name of (process.env.AUTODJ_BROWSERS || "chromium,firefox,webkit").split(",")) {
    const browser = await { chromium, firefox, webkit }[name].launch();
    try {
      await auditDialogScroll(browser);
      console.log(`${name}: dialog scrolling passed`);
    } finally {
      await browser.close();
    }
  }
}
