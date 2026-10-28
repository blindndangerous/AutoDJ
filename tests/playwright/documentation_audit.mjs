// Optional static documentation check; no AutoDJ server or external requests.
import { chromium, firefox, webkit } from "@playwright/test";
import { readFileSync, realpathSync, existsSync, writeFileSync } from "node:fs";
import { resolve, sep } from "node:path";
import { createHash } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";

const launchers = { chromium, firefox, webkit };
const args = process.argv.slice(2);
if (args.length < 1 || args.length > 2 || (args.length === 2 && args[1] !== "--write")) {
  throw new Error("Usage: node tests/playwright/documentation_audit.mjs RENDER_DIRECTORY [--write]");
}
const root = realpathSync(args[0]);
const repository = realpathSync(new URL("../..", import.meta.url));
if (root === repository || root.startsWith(repository + sep)) throw new Error("Render directory must be outside repository");
const manifest = JSON.parse(readFileSync(resolve(root, "manifest.json"), "utf8"));
if (!manifest.revision || !manifest.renderer || !manifest.scope || !manifest.candidates || !Object.keys(manifest.candidates).length || !Array.isArray(manifest.errors) || manifest.errors.length) {
  throw new Error("Missing, empty, or failed renderer manifest");
}
if (!manifest.rendered_sha256 || !Object.keys(manifest.rendered_sha256).length) throw new Error("Missing rendered hashes");
for (const [name, hash] of Object.entries(manifest.rendered_sha256)) {
  const target = realpathSync(resolve(root, name));
  if (!target.startsWith(root + sep) || createHash("sha256").update(readFileSync(target)).digest("hex") !== hash) throw new Error(`Invalid rendered hash: ${name}`);
}
const pages = Object.keys(manifest.candidates);
// Reader documentation has a page title; templates and project records may be fragments.
const standalone = file => file.startsWith("docs/") || ["README.md", "CONTRIBUTING.md", "SECURITY.md", "THREAT_MODEL.md", "CODE_OF_CONDUCT.md"].includes(file);
for (const name of pages) {
  if (!name.endsWith(".md") || !/^[a-f0-9]{64}$/.test(manifest.candidates[name])) throw new Error(`Invalid candidate: ${name}`);
  const path = realpathSync(resolve(root, name + ".html"));
  if (!manifest.rendered_sha256[name + ".html"] || !path.startsWith(root + sep)) throw new Error(`Candidate escapes render directory: ${name}`);
}
const names = (process.env.AUTODJ_BROWSERS || "chromium").split(",").map(value => value.trim());
if (!names.length || names.some(name => !launchers[name])) throw new Error("Invalid AUTODJ_BROWSERS selection");
const reportPath = resolve(root, "report.json");
if (args.includes("--write") && existsSync(reportPath)) throw new Error("report.json already exists; use a fresh render directory");
const report = { ...manifest, browsers: {}, errors: [] };
for (const name of names) {
  let browser;
  try {
    browser = await launchers[name].launch({ headless: true });
    const context = await browser.newContext({ javaScriptEnabled: false, serviceWorkers: "block" });
    const page = await context.newPage();
    await page.route("**/*", route => route.request().url().startsWith("file:") ? route.continue() : route.abort());
    const result = { version: browser.version(), pages: [] };
    report.browsers[name] = result;
    for (const file of pages) {
      await page.goto(pathToFileURL(resolve(root, file + ".html")).href);
      const findings = await page.evaluate(() => {
        const headings = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6")];
        const badLinks = [...document.querySelectorAll("a[href]")].filter(link => {
          // An image-only link, such as a badge, is named by its image's alt text.
          const label = (link.getAttribute("aria-label") || link.textContent.trim()
            || [...link.querySelectorAll("img[alt]")].map(image => image.alt).join(" ")).trim();
          return !label || /^(here|click here|link|read more)$/i.test(label);
        }).map(link => link.textContent);
        return {
          h1: document.querySelectorAll("h1").length,
          headingJumps: headings.filter((heading, i) => i && Number(heading.tagName.slice(1)) > Number(headings[i - 1].tagName.slice(1)) + 1).map(heading => heading.textContent),
          badLinks,
          localLinks: [...document.querySelectorAll("a[href]")].map(link => link.href).filter(href => href.startsWith("file:")),
        };
      });
      findings.brokenLinks = [];
      for (const href of new Set(findings.localLinks)) {
        const url = new URL(href);
        if (!url.pathname.endsWith(".md.html")) continue;
        const target = fileURLToPath(url);
        if (!target.startsWith(root + sep) || !existsSync(target) || !realpathSync(target).startsWith(root + sep)) {
          findings.brokenLinks.push(href);
        } else if (url.hash) {
          const found = await page.evaluate(({ content, fragment }) => {
            const document = new window.DOMParser().parseFromString(content, "text/html");
            return Boolean(document.getElementById(fragment) || [...document.getElementsByName(fragment)].length);
          }, { content: readFileSync(target, "utf8"), fragment: decodeURIComponent(url.hash.slice(1)) });
          if (!found) findings.brokenLinks.push(href);
        }
      }
      delete findings.localLinks;
      findings.overflow = [];
      for (const width of [375, 1280]) {
        await page.setViewportSize({ width, height: 900 });
        if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)) findings.overflow.push(width);
      }
      result.pages.push({ file, ...findings });
      if ((standalone(file) && findings.h1 !== 1) || findings.headingJumps.length || findings.badLinks.length || findings.brokenLinks.length || findings.overflow.length) report.errors.push({ browser: name, file, ...findings });
    }
  } catch (error) {
    report.errors.push({ browser: name, error: String(error) });
  } finally {
    await browser?.close();
  }
}
console.log(JSON.stringify(report, null, 2));
if (args.includes("--write")) writeFileSync(reportPath, JSON.stringify(report, null, 2) + "\n", { flag: "wx" });
if (report.errors.length) process.exitCode = 1;
