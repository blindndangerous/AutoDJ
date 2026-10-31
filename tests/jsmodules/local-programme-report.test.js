// @vitest-environment node
import { afterEach, expect, it } from "vitest";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { finalizePrototypeReport, programmeFailure } from "../playwright/local_programme_report.mjs";

const roots = [];
afterEach(async () => {
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true });
});

async function directory() {
  const root = await mkdtemp(join(tmpdir(), "autodj-audit-report-"));
  roots.push(root);
  return root;
}

it("writes the original audit failure when conversion and version discovery fail", async () => {
  const root = await directory();
  const report = { status: "failed", failure: "Error: owner recovery failed", errors: [] };
  await finalizePrototypeReport({ root, report, encoded: [Buffer.from("partial stream")],
    run(command, args) {
      if (command !== "ffmpeg") throw new Error("Measurement must not run after failure");
      throw new Error(args.includes("-version") ? "version unavailable" : "truncated MP3");
    },
  });
  const saved = JSON.parse(await readFile(join(root, "audit.json"), "utf8"));
  expect(saved.status).toBe("failed");
  expect(saved.failure).toBe("Error: owner recovery failed");
  expect(saved.finalization_errors).toEqual([
    { stage: "conversion", error: "Error: truncated MP3" },
    { stage: "ffmpeg_version", error: "Error: version unavailable" },
  ]);
  expect(await readFile(join(root, "broadcast.mp3"), "utf8")).toBe("partial stream");
});

it("fails a successful browser audit when only version discovery fails", async () => {
  const root = await directory();
  const report = { status: "passed", errors: [] };
  await finalizePrototypeReport({ root, report, encoded: [], run() { throw new Error("FFmpeg missing"); } });
  const saved = JSON.parse(await readFile(join(root, "audit.json"), "utf8"));
  expect(saved.status).toBe("failed");
  expect(saved.failure).toBe("Error: FFmpeg missing");
  expect(saved.finalization_errors).toEqual([{ stage: "ffmpeg_version", error: "Error: FFmpeg missing" }]);
  expect(saved.signal).toBeUndefined();
});

it("writes the report even when browser cleanup rejects", async () => {
  const root = await directory();
  const report = { status: "failed", failure: "Error: takeover failed", errors: [] };
  await finalizePrototypeReport({ root, report, encoded: [],
    cleanup() { throw new Error("browser close failed"); },
    run() { return "ffmpeg test version"; },
  });
  const saved = JSON.parse(await readFile(join(root, "audit.json"), "utf8"));
  expect(saved.failure).toBe("Error: takeover failed");
  expect(saved.ffmpeg).toBe("ffmpeg test version");
  expect(saved.finalization_errors).toEqual([{ stage: "cleanup", error: "Error: browser close failed" }]);
});

it("writes prerequisite evidence before measurement and retains successful signal results", async () => {
  const root = await directory();
  await writeFile(join(root, "timing.json"), "{}");
  const report = { status: "passed", errors: [], monitor: { sample_rate: 44100 } };
  await finalizePrototypeReport({ root, report, encoded: [Buffer.from("encoded audio")],
    async run(command, args) {
      if (command === "ffmpeg") return args.includes("-version") ? "ffmpeg test version\nconfiguration" : "";
      const prerequisite = JSON.parse(await readFile(join(root, "audit.json"), "utf8"));
      expect(prerequisite.monitor.sample_rate).toBe(44100);
      expect(prerequisite.timing_available).toBe(true);
      return '{"decoded_stream":{"dropouts":0}}';
    },
  });
  const saved = JSON.parse(await readFile(join(root, "audit.json"), "utf8"));
  expect(saved.status).toBe("passed");
  expect(saved.ffmpeg).toBe("ffmpeg test version");
  expect(saved.signal).toEqual({ decoded_stream: { dropouts: 0 } });
});

it("persists a measurement failure after successful conversion", async () => {
  const root = await directory();
  const report = { status: "passed", errors: [] };
  await finalizePrototypeReport({ root, report, encoded: [Buffer.from("encoded audio")],
    run(command, args) {
      if (command === "ffmpeg") return args.includes("-version") ? "ffmpeg test version" : "";
      throw new Error("signal dropout");
    },
  });
  const saved = JSON.parse(await readFile(join(root, "audit.json"), "utf8"));
  expect(saved.status).toBe("failed");
  expect(saved.failure).toBe("Error: signal dropout");
  expect(saved.finalization_errors).toEqual([{ stage: "measurement", error: "Error: signal dropout" }]);
});

it("reports session loss instead of a later null transport error", () => {
  const state = { changes: [{ state: "preparing", reason: "Preparing" },
    { state: "local", reason: "Local control" },
    { state: "recovering", reason: "Programme credit exhausted" }] };
  expect(programmeFailure(new TypeError("Cannot read properties of null"), state))
    .toBe("Local programme session lost: Programme credit exhausted");
  expect(programmeFailure(new Error("Other audit failure"), { changes: [] }))
    .toBe("Error: Other audit failure");
});
