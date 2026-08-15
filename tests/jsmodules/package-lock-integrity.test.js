import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

import { afterEach, describe, expect, it } from "vitest";

const script = join(process.cwd(), "scripts", "check-package-lock-integrity.mjs");
const temporaryDirectories = [];

function runIntegrityCheck(lock) {
  const directory = mkdtempSync(join(tmpdir(), "autodj-lock-integrity-"));
  temporaryDirectories.push(directory);
  const lockPath = join(directory, "package-lock.json");
  writeFileSync(lockPath, `${JSON.stringify(lock, null, 2)}\n`);
  return spawnSync(process.execPath, [script, lockPath], { encoding: "utf8" });
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { force: true, recursive: true });
  }
});

describe("package-lock registry integrity gate", () => {
  it("accepts registry metadata plus explicit link and bundled exceptions", () => {
    const result = runIntegrityCheck({
      lockfileVersion: 3,
      packages: {
        "": { name: "fixture", version: "1.0.0" },
        "node_modules/registry-package": {
          integrity: "sha512-Zml4dHVyZQ==",
          resolved: "https://registry.npmjs.org/registry-package/-/registry-package-1.0.0.tgz",
          version: "1.0.0",
        },
        "node_modules/workspace-link": { link: true, resolved: "packages/workspace" },
        "node_modules/bundled-package": { inBundle: true, version: "1.0.0" },
      },
    });

    expect(result.status, result.stderr).toBe(0);
  });

  it("rejects a registry package without resolved URL and integrity", () => {
    const result = runIntegrityCheck({
      lockfileVersion: 3,
      packages: {
        "": { name: "fixture", version: "1.0.0" },
        "node_modules/incomplete": { version: "1.0.0" },
      },
    });

    expect(result.status).toBe(1);
    expect(result.stderr).toContain("node_modules/incomplete");
    expect(result.stderr).toContain("resolved registry URL");
    expect(result.stderr).toContain("integrity");
  });
});
