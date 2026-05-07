// ESLint flat config (v9+).  Vanilla JavaScript only -- no TypeScript,
// no JSX.  Source lives in src/autodj/static/ (browser-side ESM
// modules) plus tests/ (vitest unit tests + Playwright audit scripts
// running on Node).
//
// Wired alongside the Python toolchain (ruff + mypy + vulture +
// deptry) so the JS surface gets the same dead-code / unused-import
// scrutiny as the Python surface.  Catches what bundler tree-shaking
// silently drops, which is otherwise invisible to reviewers.

import js from "@eslint/js";

export default [
  {
    ignores: [
      "src/autodj/static_dist/**", // vite output
      "node_modules/**",
      "tmp/**",
      "**/*.min.js",
    ],
  },
  js.configs.recommended,
  {
    files: ["src/autodj/static/**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        // Browser globals.
        window: "readonly",
        document: "readonly",
        navigator: "readonly",
        location: "readonly",
        localStorage: "readonly",
        fetch: "readonly",
        AudioContext: "readonly",
        webkitAudioContext: "readonly",
        WebSocket: "readonly",
        URL: "readonly",
        URLSearchParams: "readonly",
        FormData: "readonly",
        File: "readonly",
        Blob: "readonly",
        Image: "readonly",
        Audio: "readonly",
        HTMLAudioElement: "readonly",
        HTMLElement: "readonly",
        Element: "readonly",
        Event: "readonly",
        CustomEvent: "readonly",
        AbortController: "readonly",
        WeakMap: "readonly",
        WeakSet: "readonly",
        Set: "readonly",
        Map: "readonly",
        Promise: "readonly",
        setTimeout: "readonly",
        setInterval: "readonly",
        clearTimeout: "readonly",
        clearInterval: "readonly",
        requestAnimationFrame: "readonly",
        cancelAnimationFrame: "readonly",
        queueMicrotask: "readonly",
        console: "readonly",
        alert: "readonly",
        getComputedStyle: "readonly",
        performance: "readonly",
        confirm: "readonly",
        MediaMetadata: "readonly",
        CSS: "readonly",
        HTMLMediaElement: "readonly",
        AudioWorkletNode: "readonly",
        WaveShaperNode: "readonly",
        OfflineAudioContext: "readonly",
        BiquadFilterNode: "readonly",
        AnalyserNode: "readonly",
        // AudioWorklet globals (used in the worklet processors).
        AudioWorkletProcessor: "readonly",
        registerProcessor: "readonly",
        currentTime: "readonly",
        sampleRate: "readonly",
        // Test runner globals (vitest + Playwright pull in their own).
        process: "readonly",
        Buffer: "readonly",
      },
    },
    rules: {
      // No unused vars, with the conventional underscore-prefix opt-out.
      "no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
      // Allow console for in-browser diagnostics (project intentionally
      // logs to console for browser-side debug -- see ?debug=1 flag).
      "no-console": "off",
      // Style.
      "prefer-const": "warn",
      "no-var": "error",
      eqeqeq: ["warn", "smart"],
      // Relaxed rules -- these patterns appear intentionally throughout
      // the codebase and tightening them would be a noisy churn pass:
      //   - empty catches: best-effort cleanup (try { node.disconnect() } catch {})
      //   - useless-assignment: defensive `let x = 0; if (cond) x = ...`
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-useless-assignment": "off",
    },
  },
  {
    files: ["tests/**/*.{js,mjs}", "vite.config.js", "eslint.config.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        // Node globals for build / test scripts.
        process: "readonly",
        Buffer: "readonly",
        __dirname: "readonly",
        __filename: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        setInterval: "readonly",
        clearTimeout: "readonly",
        clearInterval: "readonly",
        URL: "readonly",
        // Browser globals appear inside Playwright's page.evaluate
        // callbacks (executed in the page context, but ESLint can't
        // tell that from the .mjs lexer).
        window: "readonly",
        document: "readonly",
        Event: "readonly",
        CustomEvent: "readonly",
        AudioContext: "readonly",
        KeyboardEvent: "readonly",
        navigator: "readonly",
        fetch: "readonly",
        location: "readonly",
        localStorage: "readonly",
        // vitest globals (only when explicitly imported, but tolerate).
        describe: "readonly",
        it: "readonly",
        test: "readonly",
        expect: "readonly",
        beforeEach: "readonly",
        afterEach: "readonly",
        beforeAll: "readonly",
        afterAll: "readonly",
        vi: "readonly",
      },
    },
    rules: {
      "no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
];
