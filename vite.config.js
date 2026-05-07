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
import { copyFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const SRC  = resolve(here, "src/autodj/static");
const OUT  = resolve(here, "src/autodj/static_dist");

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
        format: "iife",
        // Keep top-level identifiers unmangled so any inline
        // <script> in index.html that calls into them keeps working.
        // (Today there is none, but cheap insurance.)
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
      },
    },
  ],
});
