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
//   - app.js is an ES module (index.html loads it with type="module") and
//     the build keeps that format, so the bundle's own bindings stay
//     module-scoped instead of leaking onto window.
//   - Worklet files (bitcrusher-worklet.js etc.) are loaded by absolute
//     URL via AudioWorklet.addModule and must keep their filenames
//     stable so the FastAPI explicit routes keep working.  We copy them
//     unchanged in the closeBundle hook below.

import { defineConfig } from "vite";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
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

export function writeBuildInfo(out, version) {
  if (!existsSync(out)) mkdirSync(out, { recursive: true });
  writeFileSync(
    resolve(out, "build-info.json"),
    `${JSON.stringify({ version }, null, 2)}\n`,
    "utf8",
  );
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
