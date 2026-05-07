// Vitest configuration for the JS module unit tests.
//
// Runs in jsdom so DOM-using modules (live-region, lyrics, cues) can
// assert against a fake document.  Pure-function modules
// (dom-helpers, camelot-wheel adjacency rules) work in either env.

import { defineConfig } from "vite";

export default defineConfig({
  test: {
    // happy-dom over jsdom: jsdom under Node 21+ shells localStorage out
    // to a native webstorage runtime that prints
    // "Warning: --localstorage-file was provided without a valid path"
    // every time DEBUG flag detection in dom-helpers.js touches
    // localStorage.  happy-dom uses a pure-JS shim with no such hook
    // so the test output stays clean.
    environment: "happy-dom",
    globals: true,
    include: ["tests/jsmodules/**/*.test.js"],
  },
});
