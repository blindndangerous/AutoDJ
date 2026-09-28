import { chromium, firefox, webkit } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { renameSync, rmSync, writeFileSync } from "node:fs";

const launchers = { chromium, firefox, webkit };

function selectedBrowsers() {
  const names = (process.env.AUTODJ_BROWSERS || "chromium,firefox,webkit")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (!names.length) {
    throw new Error("AUTODJ_BROWSERS must select at least one browser");
  }
  for (const name of names) {
    if (!launchers[name]) throw new Error(`Unsupported browser: ${name}`);
  }
  return names.map((name) => [name, launchers[name]]);
}

function check(condition, message) {
  if (!condition) throw new Error(message);
}

function equal(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(
      `${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
    );
  }
}

export function validateHealthAudit(name, result) {
  equal(result.console.filter((entry) => entry.type === "error").length, 0,
    `${name} console errors`);
  equal(result.pageerrors.length, 0, `${name} page errors`);
  equal(result.requestfailed.length, 0, `${name} failed requests`);
  equal(result.status_4xx_5xx.length, 0, `${name} HTTP errors`);
  equal(result.unhandled.length, 0, `${name} interaction errors`);
  check(result.websockets.some((socket) => socket.opened), `${name} websocket did not open`);
  equal(result.websockets.flatMap((socket) => socket.errors).length, 0,
    `${name} websocket errors`);
  check(result.probe && !result.probe.error, `${name} page probe failed`);
  check(result.probe.title.includes("AutoDJ"), `${name} title missing AutoDJ`);
  check(result.probe.hasAudio, `${name} audio element missing`);
}

export function validateTransitionAudit(name, result) {
  check(result.workletReady, `${name} audio element/worklet readiness failed`);
  equal(result.probe.errors.length, 0, `${name} probe errors`);
  for (const [worklet, status] of Object.entries(result.probe.worklets)) {
    equal(status, "ok", `${name} ${worklet} worklet`);
  }
  for (const [effect, response] of Object.entries(result.transitions)) {
    equal(response.status, 200, `${name} ${effect} transition response`);
  }
  equal(result.logs.filter((entry) => entry.type === "pageerror").length, 0,
    `${name} page errors`);
  equal(result.logs.filter((entry) => entry.type === "error").length, 0,
    `${name} console errors`);
}

function writeReportAtomically(report, results) {
  const temporary = `${report}.${process.pid}.${randomUUID()}.tmp`;
  try {
    writeFileSync(temporary, `${JSON.stringify(results, null, 2)}\n`, "utf8");
    renameSync(temporary, report);
  } finally {
    rmSync(temporary, { force: true });
  }
}

export async function runAudit({ audit, validate, report }) {
  const results = {};
  const failures = [];
  for (const [name, launcher] of selectedBrowsers()) {
    let result;
    try {
      result = await audit(name, launcher);
    } catch (error) {
      results[name] = { error: String(error) };
      failures.push(`${name}: ${error}`);
      writeReportAtomically(report, results);
      continue;
    }
    try {
      validate(name, result);
      results[name] = result;
    } catch (error) {
      const diagnostic = result && typeof result === "object" && !Array.isArray(result)
        ? { ...result }
        : { result };
      diagnostic.validationError = String(error);
      results[name] = diagnostic;
      failures.push(`${name}: ${error}`);
    }
    writeReportAtomically(report, results);
  }
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exitCode = 1;
  }
}
