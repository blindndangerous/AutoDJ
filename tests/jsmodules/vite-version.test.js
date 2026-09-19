import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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

  it("replaces a stale build stamp", () => {
    const out = mkdtempSync(join(tmpdir(), "autodj-vite-version-"));
    try {
      writeFileSync(join(out, "build-info.json"), '{"version":"0.14.0"}\n');

      writeBuildInfo(out, "0.15.0");

      expect(readFileSync(join(out, "build-info.json"), "utf8")).toBe(
        '{\n  "version": "0.15.0"\n}\n',
      );
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  });
});
