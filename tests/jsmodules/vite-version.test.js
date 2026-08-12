import {
  closeSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { readProjectVersion, writeBuildInfo } from "../../vite.config.js";

describe("product version build metadata", () => {
  it("parses project.version with TOML semantics", () => {
    const source = `
      title = "not the product"
      [tool.example]
      version = "9.9.9"
      [project] # product metadata
      name = "autodj"
      version = '0.15.0' # valid single-quoted TOML
    `;

    expect(readProjectVersion(source)).toBe("0.15.0");
  });

  it.each([
    ["malformed TOML", "[project\nversion = '0.15.0'"],
    ["missing version", "[project]\nname = 'autodj'"],
    ["wrong version type", "[project]\nname = 'autodj'\nversion = 15"],
    ["wrong project name", "[project]\nname = 'another-product'\nversion = '0.15.0'"],
  ])("rejects %s with an actionable error", (_label, source) => {
    expect(() => readProjectVersion(source)).toThrow(/project\.version.*pyproject\.toml/i);
  });

  it("atomically replaces a stale build stamp", () => {
    const out = mkdtempSync(join(tmpdir(), "autodj-vite-version-"));
    try {
      writeFileSync(join(out, "build-info.json"), '{"version":"0.14.0"}\n');

      writeBuildInfo(out, "0.15.0");

      expect(JSON.parse(readFileSync(join(out, "build-info.json"), "utf8"))).toEqual({
        version: "0.15.0",
      });
      expect(readdirSync(out).filter(name => name.endsWith(".tmp"))).toEqual([]);
      if (process.platform !== "win32") {
        expect(statSync(join(out, "build-info.json")).mode & 0o777).toBe(0o644);
      }
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  });

  it("sets a runtime-readable mode before publishing the stamp", () => {
    const out = mkdtempSync(join(tmpdir(), "autodj-vite-version-"));
    const requestedModes = [];
    try {
      writeBuildInfo(out, "0.15.0", renameSync, (_file, mode) => {
        requestedModes.push(mode);
      });

      expect(requestedModes).toEqual([0o644]);
      expect(readFileSync(join(out, "build-info.json"), "utf8")).toContain("0.15.0");
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  });

  it("cleans up its temporary file when replacement fails", () => {
    const out = mkdtempSync(join(tmpdir(), "autodj-vite-version-"));
    const target = join(out, "build-info.json");
    try {
      writeFileSync(target, '{"version":"0.14.0"}\n');

      expect(() => writeBuildInfo(out, "0.15.0", () => {
        throw new Error("injected rename failure");
      })).toThrow("injected rename failure");
      expect(readFileSync(target, "utf8")).toContain("0.14.0");
      expect(readdirSync(out).filter(name => name.endsWith(".tmp"))).toEqual([]);
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  });

  it("cleans up its temporary file when closing it reports failure", () => {
    const out = mkdtempSync(join(tmpdir(), "autodj-vite-version-"));
    try {
      expect(() => writeBuildInfo(
        out,
        "0.15.0",
        renameSync,
        undefined,
        file => {
          closeSync(file);
          throw new Error("injected close failure");
        },
      )).toThrow("injected close failure");
      expect(readdirSync(out).filter(name => name.endsWith(".tmp"))).toEqual([]);
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  });
});
