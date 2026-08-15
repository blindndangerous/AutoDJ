import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as auditHelpers from "../playwright/audit_helpers.mjs";

const {
  check,
  equal,
  runAudit,
  selectedBrowsers,
} = auditHelpers;

const originalBrowsers = process.env.AUTODJ_BROWSERS;
const originalExitCode = process.exitCode;

afterEach(() => {
  if (originalBrowsers === undefined) delete process.env.AUTODJ_BROWSERS;
  else process.env.AUTODJ_BROWSERS = originalBrowsers;
  process.exitCode = originalExitCode;
  vi.restoreAllMocks();
});

describe("browser audit helpers", () => {
  it("selects only requested supported browsers in declared order", () => {
    process.env.AUTODJ_BROWSERS = " webkit, chromium ";

    expect(selectedBrowsers().map(([name]) => name)).toEqual([
      "webkit",
      "chromium",
    ]);
  });

  it("requires native range and search controls to keep their keyboard events", () => {
    const result = {
      source: Object.fromEntries(
        auditHelpers.REQUIRED_HOTKEY_SOURCE_KEYS.map((key) => [key, true]),
      ),
      dom: {
        modalExists: true,
        legacyDetailsGone: true,
        btnShortcutsExists: true,
        btnShortcutsCloseExists: true,
        modalAriaLabelledBy: "hotkey-help-title",
      },
      behaviour: {
        modalInitiallyClosed: true,
        modalOpenAfterQuestion: true,
        closeBtnFocused: true,
        modalClosedAfterCloseBtn: true,
        modalOpenAfterTrigger: true,
        shuffleClicksLatched: 1,
        shuffleClicksAfterRelease: 2,
        muteClicksFromSliderFocus: 0,
        pauseClicksFromSearchInput: 0,
      },
      errors: [],
    };

    expect(() => auditHelpers.validateHotkeyAudit("chromium", result)).not.toThrow();

    result.behaviour.muteClicksFromSliderFocus = 1;
    expect(() => auditHelpers.validateHotkeyAudit("chromium", result)).toThrow(
      "chromium slider native ownership: expected 0, got 1",
    );

    result.behaviour.muteClicksFromSliderFocus = 0;
    result.behaviour.pauseClicksFromSearchInput = 1;
    expect(() => auditHelpers.validateHotkeyAudit("chromium", result)).toThrow(
      "chromium input suppression: expected 0, got 1",
    );

    result.behaviour.pauseClicksFromSearchInput = 0;
    delete result.dom.btnShortcutsCloseExists;
    expect(() => auditHelpers.validateHotkeyAudit("chromium", result)).toThrow(
      "chromium shortcut close exists: expected true, got undefined",
    );
  });

  it("checks executable repick and narrowed typing helpers in deployed modules", () => {
    const sources = {
      app: readFileSync("src/autodj/static/app.js", "utf8"),
      hotkeys: readFileSync("src/autodj/static/modules/hotkeys.js", "utf8"),
      audio: readFileSync("src/autodj/static/modules/audio-engine.js", "utf8"),
      domHelpers: readFileSync("src/autodj/static/modules/dom-helpers.js", "utf8"),
    };

    const checks = auditHelpers.hotkeySourceChecks(sources);
    expect(checks.repickNextEndpoint).toBe(true);
    expect(checks.isTypingTargetNarrowed).toBe(true);
    expect(checks.windowCapturePhase).toBe(true);
    expect(checks.durationUnified).toBe(true);
    expect(checks.pauseBothDecks).toBe(true);
    expect(checks.shuffleCrossfadeOnUnexpectedChange).toBe(true);

    const commentedRepick = {
      ...sources,
      audio: sources.audio.replace(
        'requestJsonBestEffort("/api/repick-next", {',
        'requestJsonBestEffort(repickPath, { // "/api/repick-next"',
      ),
    };
    expect(auditHelpers.hotkeySourceChecks(commentedRepick).repickNextEndpoint).toBe(false);

    const broadenedTyping = {
      ...sources,
      domHelpers: sources.domHelpers.replace('"text", "search", "email"', '"range"'),
    };
    expect(auditHelpers.hotkeySourceChecks(broadenedTyping).isTypingTargetNarrowed).toBe(false);

    const commentsRemoved = {
      ...sources,
      audio: sources.audio
        .replace("Pause BOTH decks during a crossfade", "pause behavior")
        .replace("server changed current_track", "new state path differs"),
    };
    const commentFreeChecks = auditHelpers.hotkeySourceChecks(commentsRemoved);
    expect(commentFreeChecks.pauseBothDecks).toBe(true);
    expect(commentFreeChecks.shuffleCrossfadeOnUnexpectedChange).toBe(true);

    const serverPauseRemoved = {
      ...sources,
      audio: sources.audio.replace(
        /if \(s\.is_paused\) \{[\s\S]*?for \(const d of decks\) \{[\s\S]*?\n {8}\}\r?\n {6}\}/,
        "if (s.is_paused) { suppressAdvance = true; }",
      ),
    };
    expect(serverPauseRemoved.audio).not.toBe(sources.audio);
    expect(serverPauseRemoved.audio).toContain("function stopAllDecks");
    expect(auditHelpers.hotkeySourceChecks(serverPauseRemoved).pauseBothDecks).toBe(false);
  });

  it("rejects missing source and modal contracts", () => {
    const source = Object.fromEntries(
      auditHelpers.REQUIRED_HOTKEY_SOURCE_KEYS.map((key) => [key, true]),
    );
    const result = {
      source,
      dom: {
        modalExists: true,
        legacyDetailsGone: true,
        btnShortcutsExists: true,
        btnShortcutsCloseExists: true,
        modalAriaLabelledBy: "hotkey-help-title",
      },
      behaviour: {
        modalInitiallyClosed: true,
        modalOpenAfterQuestion: true,
        closeBtnFocused: true,
        modalClosedAfterCloseBtn: true,
        modalOpenAfterTrigger: true,
        shuffleClicksLatched: 1,
        shuffleClicksAfterRelease: 2,
        muteClicksFromSliderFocus: 0,
        pauseClicksFromSearchInput: 0,
      },
      errors: [],
    };

    delete result.source.durationUnified;
    expect(() => auditHelpers.validateHotkeyAudit("webkit", result)).toThrow(
      "webkit source keys",
    );

    result.source.durationUnified = false;
    expect(() => auditHelpers.validateHotkeyAudit("webkit", result)).toThrow(
      "webkit source.durationUnified",
    );

    result.source.durationUnified = true;
    result.dom.modalAriaLabelledBy = null;
    expect(() => auditHelpers.validateHotkeyAudit("webkit", result)).toThrow(
      'webkit modal label: expected "hotkey-help-title", got null',
    );

    result.dom.modalAriaLabelledBy = "hotkey-help-title";
    result.behaviour.modalOpenAfterTrigger = false;
    expect(() => auditHelpers.validateHotkeyAudit("webkit", result)).toThrow(
      "webkit modal trigger open: expected true, got false",
    );
  });

  it("validates health, regression, and transition diagnostics", () => {
    const health = {
      console: [], pageerrors: [], requestfailed: [], status_4xx_5xx: [],
      websockets: [{ opened: true, errors: [] }], unhandled: [],
      probe: { title: "AutoDJ", hasAudio: true },
    };
    expect(() => auditHelpers.validateHealthAudit("chromium", health)).not.toThrow();
    health.console.push({ type: "error", text: "boom" });
    expect(() => auditHelpers.validateHealthAudit("chromium", health)).toThrow(
      "chromium console errors: expected 0, got 1",
    );

    const regression = {
      dom: {
        lyricsCardInNow: true,
        lyricsCardInSettings: false,
        cueListSummaryGone: true,
        linerCheckboxOutsideTrigger: true,
        linerCheckboxDescribedBy: "ln-enabled-desc",
      },
      hotkeys: {
        volUnchangedOnSettingsTab: true,
        shortcutsDialogOpensFromAnyTab: true,
        volChangedOnNowTab: true,
      },
      liveRegion: { announced: "Volume 70 percent", cleared: true },
      errors: [],
    };
    expect(() => auditHelpers.validateRegressionAudit("firefox", regression)).not.toThrow();
    regression.dom.linerCheckboxOutsideTrigger = false;
    expect(() => auditHelpers.validateRegressionAudit("firefox", regression)).toThrow(
      "firefox liner checkbox placement: expected true, got false",
    );

    const transition = {
      workletReady: true,
      probe: { errors: [], worklets: { freeze: "ok" } },
      transitions: { freeze: { status: 200 } },
      logs: [{ type: "error", text: "console failed" }],
    };
    expect(() => auditHelpers.validateTransitionAudit("webkit", transition)).toThrow(
      "webkit console errors: expected 0, got 1",
    );
  });

  it("rejects unsupported and empty browser selections", () => {
    process.env.AUTODJ_BROWSERS = "netscape";
    expect(() => selectedBrowsers()).toThrow("Unsupported browser: netscape");

    process.env.AUTODJ_BROWSERS = " , ";
    expect(() => selectedBrowsers()).toThrow(
      "AUTODJ_BROWSERS must select at least one browser",
    );
  });

  it("throws useful assertion failures", () => {
    expect(() => check(false, "missing audio")).toThrow("missing audio");
    expect(() => equal(1, 2, "request failures")).toThrow(
      "request failures: expected 2, got 1",
    );
  });

  it("writes diagnostics and sets failure status when validation fails", async () => {
    const directory = mkdtempSync(join(tmpdir(), "autodj-browser-audit-"));
    const report = join(directory, "audit.json");
    process.env.AUTODJ_BROWSERS = "chromium,firefox";
    process.exitCode = 0;
    vi.spyOn(console, "error").mockImplementation(() => {});

    try {
      await runAudit({
        audit: async (name) => ({ healthy: name === "chromium" }),
        validate(name, result) {
          check(result.healthy, `${name} unhealthy`);
        },
        report,
      });

      expect(process.exitCode).toBe(1);
      expect(JSON.parse(readFileSync(report, "utf8"))).toEqual({
        chromium: { healthy: true },
        firefox: {
          healthy: false,
          validationError: "Error: firefox unhealthy",
        },
      });
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("writes a complete report atomically after every engine", async () => {
    const directory = mkdtempSync(join(tmpdir(), "autodj-browser-progress-"));
    const report = join(directory, "audit.json");
    process.env.AUTODJ_BROWSERS = "chromium,firefox";

    try {
      await runAudit({
        audit: async (name) => {
          if (name === "firefox") {
            expect(JSON.parse(readFileSync(report, "utf8"))).toEqual({
              chromium: { ok: true },
            });
          }
          return { ok: true };
        },
        validate: () => {},
        report,
      });
      expect(JSON.parse(readFileSync(report, "utf8"))).toEqual({
        chromium: { ok: true },
        firefox: { ok: true },
      });
      expect(readFileSync(report, "utf8").endsWith("\n")).toBe(true);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
});
