import { execFileSync } from "node:child_process";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

export async function finalizePrototypeReport({ root, report, encoded, cleanup, run = execFileSync }) {
  async function attempt(stage, action) {
    try {
      await action();
    } catch (error) {
      report.status = "failed";
      report.failure ??= String(error);
      (report.finalization_errors ??= []).push({ stage, error: String(error) });
    }
  }
  try {
    await attempt("cleanup", async () => { await cleanup?.(); });
    if (encoded.length) {
      await attempt("conversion", async () => {
        await writeFile(resolve(root, "broadcast.mp3"), Buffer.concat(encoded));
        await run("ffmpeg", ["-y", "-hide_banner", "-loglevel", "error", "-i", resolve(root, "broadcast.mp3"),
          "-ar", "44100", "-ac", "2", "-c:a", "pcm_f32le", resolve(root, "broadcast.wav")], { windowsHide: true });
      });
    }
    await attempt("ffmpeg_version", async () => {
      report.ffmpeg = (await run("ffmpeg", ["-version"], { encoding: "utf8", windowsHide: true })).split("\n")[0];
    });
    report.timing_available = await readFile(resolve(root, "timing.json"), "utf8").then(() => true, () => false);
    if (report.status === "passed") {
      // The signal measurement reads this report as one of its inputs.
      await writeFile(resolve(root, "audit.json"), JSON.stringify(report, null, 2));
      await attempt("measurement", async () => {
        report.signal = JSON.parse(await run("uv", ["run", "--frozen", "--no-sync", "python", "-m",
          "tests.playwright.local_programme_prototype_measure", root], {
          encoding: "utf8", windowsHide: true,
          env: { ...process.env, UV_CACHE_DIR: resolve(".codex-tmp/uv-cache") },
        }));
      });
    }
  } finally {
    await writeFile(resolve(root, "audit.json"), JSON.stringify(report, null, 2));
  }
}
