import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { readProjectVersion, webSourceHash, writeBuildInfo } from "../../vite.config.js";

// Pinned in tests/unit/test_version.py too, for the same tree, so the
// build stamp and the Python check (autodj.version.web_source_hash)
// cannot drift apart.
const PINNED_WEB_SOURCE_HASH =
  "c2ec7628d7bfbb824855fdaaae7f91d053ff6cdf57d5d3b53cb7f8d67ac71491";

function writeWebCheckout(root) {
  const statics = join(root, "src", "autodj", "static");
  mkdirSync(join(statics, "modules"), { recursive: true });
  writeFileSync(join(root, "vite.config.js"), "export default {};\n");
  writeFileSync(join(statics, "app.js"), "import './modules/a.js';\r\n");
  writeFileSync(join(statics, "index.html"), "<!doctype html>\n");
  writeFileSync(join(statics, "modules", "a.js"), "export const a = 1;\n");
}

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

      writeBuildInfo(out, "0.15.0", "abc123");

      expect(readFileSync(join(out, "build-info.json"), "utf8")).toBe(
        '{\n  "version": "0.15.0",\n  "source_hash": "abc123"\n}\n',
      );
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  });

  it("hashes the web sources exactly as the Python check does", () => {
    const root = mkdtempSync(join(tmpdir(), "autodj-vite-hash-"));
    try {
      writeWebCheckout(root);

      expect(webSourceHash(root)).toBe(PINNED_WEB_SOURCE_HASH);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
