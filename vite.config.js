// Vite build pipeline for the AutoDJ web UI.
//
// Why vite (and not just shipping src/autodj/static/ as-is):
//   - Minifies app.js + app.css so a remote / mobile listener loads the
//     UI faster.
//   - Single artifact directory (src/autodj/static_dist/) we can ship in
//     a container without depending on Node at runtime.
//   - Source maps for production debugging.
//   - Future-proofs splitting app.js into ES modules without re-doing
//     the deployment story.
//
// Why not vite's full HTML pipeline:
//   - app.js is currently a single non-module script with top-level let
//     / const used as ad-hoc globals.  An ES-module rewrite is a future
//     refactor; today we wrap the script in an IIFE via rollup so the
//     existing globals stay scoped without leaking onto window.
//   - Worklet files (bitcrusher-worklet.js etc.) are loaded by absolute
//     URL via AudioWorklet.addModule and must keep their filenames
//     stable so the FastAPI explicit routes keep working.  We copy them
//     unchanged in the closeBundle hook below.

import { defineConfig } from "vite";
import {
  closeSync,
  copyFileSync,
  existsSync,
  fchmodSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import process from "node:process";
import { randomUUID } from "node:crypto";
import { parse } from "smol-toml";

const here = dirname(fileURLToPath(import.meta.url));
const SRC  = resolve(here, "src/autodj/static");
const OUT  = resolve(here, "src/autodj/static_dist");
const pyproject = readFileSync(resolve(here, "pyproject.toml"), "utf8");

export function readProjectVersion(source) {
  let document;
  try {
    document = parse(source);
  } catch (error) {
    throw new Error(`Unable to read project.version from pyproject.toml: ${error.message}`, {
      cause: error,
    });
  }
  if (document.project?.name !== "autodj") {
    throw new Error(
      "Unable to read project.version from pyproject.toml: project.name must be 'autodj'",
    );
  }
  const version = document.project?.version;
  if (typeof version !== "string" || !version.trim()) {
    throw new Error(
      "Unable to read project.version from pyproject.toml: expected a non-empty string",
    );
  }
  return version;
}

function syncDirectory(path) {
  let directory;
  try {
    directory = openSync(path, "r");
    fsyncSync(directory);
  } catch (error) {
    if (process.platform !== "win32" || !["EACCES", "EINVAL", "EPERM"].includes(error.code)) {
      throw error;
    }
  } finally {
    if (directory !== undefined) closeSync(directory);
  }
}

export function writeBuildInfo(
  out,
  version,
  replaceFile = renameSync,
  setMode = fchmodSync,
  closeFile = closeSync,
) {
  if (!existsSync(out)) mkdirSync(out, { recursive: true });
  const target = resolve(out, "build-info.json");
  const temporary = resolve(
    out,
    `.build-info.json.${process.pid}.${randomUUID()}.tmp`,
  );
  let file;
  try {
    file = openSync(temporary, "wx", 0o644);
    writeFileSync(file, `${JSON.stringify({ version }, null, 2)}\n`, "utf8");
    setMode(file, 0o644);
    fsyncSync(file);
    try {
      closeFile(file);
    } catch (error) {
      try {
        closeSync(file);
      } catch {
        // Preserve the original close failure; cleanup below still runs.
      }
      file = undefined;
      throw error;
    }
    file = undefined;
    replaceFile(temporary, target);
    syncDirectory(out);
  } finally {
    if (file !== undefined) {
      try {
        closeSync(file);
      } catch {
        // Preserve the operation failure while still attempting temp cleanup.
      }
    }
    rmSync(temporary, { force: true });
  }
}

const PRODUCT_VERSION = readProjectVersion(pyproject);

// Files we copy as-is into static_dist after the bundle step.
// Worklets MUST keep their filenames stable (the FastAPI server has
// explicit routes for /bitcrusher-worklet.js etc).  index.html and
// app.css are copied so the entire deployable site is self-contained
// in one directory.
const COPY_AS_IS = [
  "index.html",
  "app.css",
  "bitcrusher-worklet.js",
  "stutter-worklet.js",
  "freeze-worklet.js",
  "glitch-worklet.js",
];

export default defineConfig({
  build: {
    outDir: OUT,
    emptyOutDir: true,
    minify: "esbuild",
    sourcemap: true,
    target: "es2020",
    rollupOptions: {
      input: resolve(SRC, "app.js"),
      output: {
        // Stable filename so index.html's <script src="/app.js"> still
        // resolves without HTML rewriting.  Cache-busting handled by
        // FastAPI's _NO_CACHE headers, not by file hashing.
        entryFileNames: "app.js",
        // ES module output: the HTML loads app.js with type="module",
        // which means dev mode (no build, source modules served as-is)
        // and prod mode (vite-bundled single file) both work.  An IIFE
        // wrapper would clash with type="module" because the script
        // tag would still be expected to satisfy ES module semantics.
        format: "es",
        codeSplitting: false,
      },
    },
  },
  plugins: [
    {
      name: "copy-unbundled-assets",
      closeBundle() {
        if (!existsSync(OUT)) mkdirSync(OUT, { recursive: true });
        for (const f of COPY_AS_IS) {
          const from = resolve(SRC, f);
          const to   = resolve(OUT, f);
          if (existsSync(from)) copyFileSync(from, to);
        }
        writeBuildInfo(OUT, PRODUCT_VERSION);
      },
    },
  ],
});
