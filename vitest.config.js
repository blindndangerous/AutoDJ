// Vitest configuration for the JS module unit tests.
//
// Runs in jsdom so DOM-using modules (live-region, lyrics, cues) can
// assert against a fake document.  Pure-function modules
// (dom-helpers, camelot-wheel adjacency rules) work in either env.

import { defineConfig } from "vite";

export default defineConfig({
  test: {
    // happy-dom rather than jsdom: jsdom under Node 21+ shells localStorage
    // out to a native webstorage runtime that prints
    // "Warning: --localstorage-file was provided without a valid path"
    // every time DEBUG flag detection in dom-helpers.js touches
    // localStorage.  happy-dom uses a pure-JS shim with no such hook
    // so the test output stays clean.  jsdom is not installed.
    environment: "happy-dom",
    globals: true,
    include: ["tests/jsmodules/**/*.test.js"],
    // The a11y-contract stylesheet cases re-parse app.css many times and
    // land within a few percent of the 5 s default under CI load.
    testTimeout: 15000,
  },
});
