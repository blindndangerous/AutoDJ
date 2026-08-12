import { chromium, firefox, webkit } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { parse } from "espree";
import { renameSync, rmSync, writeFileSync } from "node:fs";

const launchers = { chromium, firefox, webkit };

export function selectedBrowsers() {
  const names = (process.env.AUTODJ_BROWSERS || "chromium,firefox,webkit")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (!names.length) {
    throw new Error("AUTODJ_BROWSERS must select at least one browser");
  }
  for (const name of names) {
    if (!launchers[name]) throw new Error(`Unsupported browser: ${name}`);
  }
  return names.map((name) => [name, launchers[name]]);
}

export function check(condition, message) {
  if (!condition) throw new Error(message);
}

export function equal(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(
      `${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
    );
  }
}

function syntaxNodes(source) {
  const root = parse(source, { ecmaVersion: "latest", sourceType: "module" });
  const nodes = [];
  const pending = [root];
  while (pending.length) {
    const node = pending.pop();
    if (!node || typeof node !== "object") continue;
    if (typeof node.type === "string") nodes.push(node);
    for (const [key, value] of Object.entries(node)) {
      if (key !== "parent" && (Array.isArray(value) || value?.type)) {
        if (Array.isArray(value)) pending.push(...value);
        else pending.push(value);
      }
    }
  }
  return nodes;
}

function isIdentifier(node, name) {
  return node?.type === "Identifier" && node.name === name;
}

function isLiteral(node, value) {
  return node?.type === "Literal" && node.value === value;
}

function subtreeHas(node, predicate) {
  const pending = [node];
  while (pending.length) {
    const current = pending.pop();
    if (!current || typeof current !== "object") continue;
    if (predicate(current)) return true;
    for (const value of Object.values(current)) {
      if (Array.isArray(value)) pending.push(...value);
      else if (value?.type) pending.push(value);
    }
  }
  return false;
}

function isNamedCall(node, name) {
  return node?.type === "CallExpression" && isIdentifier(node.callee, name);
}

function hasCapturedWindowKeydown(source) {
  return syntaxNodes(source).some((node) => {
    if (
      node.type !== "CallExpression" ||
      node.callee.type !== "MemberExpression" ||
      node.callee.computed ||
      !isIdentifier(node.callee.object, "window") ||
      !isIdentifier(node.callee.property, "addEventListener") ||
      !isLiteral(node.arguments[0], "keydown")
    ) return false;
    const options = node.arguments[2];
    if (isLiteral(options, true)) return true;
    return options?.type === "ObjectExpression" && options.properties.some((property) =>
      property.type === "Property" &&
      (isIdentifier(property.key, "capture") || isLiteral(property.key, "capture")) &&
      isLiteral(property.value, true)
    );
  });
}

function hasExecutableRepickRequest(source) {
  return syntaxNodes(source).some((node) =>
    node.type === "CallExpression" &&
    node.callee.type === "Identifier" &&
    node.callee.name === "requestJsonBestEffort" &&
    node.arguments[0]?.type === "Literal" &&
    node.arguments[0].value === "/api/repick-next"
  );
}

function hasNarrowTypingInputTypes(source) {
  const expected = [
    "date", "datetime-local", "email", "month", "number", "password",
    "search", "tel", "text", "time", "url", "week",
  ];
  return syntaxNodes(source).some((node) => {
    if (
      node.type !== "CallExpression" ||
      node.callee.type !== "MemberExpression" ||
      node.callee.computed ||
      node.callee.property.name !== "includes" ||
      node.callee.object.type !== "ArrayExpression" ||
      node.arguments[0]?.type !== "Identifier" ||
      node.arguments[0].name !== "t"
    ) return false;
    const types = node.callee.object.elements.map((element) => element?.value).sort();
    return JSON.stringify(types) === JSON.stringify(expected);
  });
}

function hasUnifiedEffectDuration(source) {
  const nodes = syntaxNodes(source);
  const resolvesDuration = nodes.some((node) =>
    node.type === "VariableDeclarator" &&
    isIdentifier(node.id, "effectDur") &&
    isNamedCall(node.init, "_effectDurationFor")
  );
  const appliesDuration = nodes.some((node) =>
    isNamedCall(node, "applyTransitionFx") && isIdentifier(node.arguments[1], "effectDur")
  );
  const rampsDuration = nodes.some((node) =>
    node.type === "CallExpression" &&
    node.callee.type === "MemberExpression" &&
    isIdentifier(node.callee.property, "linearRampToValueAtTime") &&
    subtreeHas(node.arguments[1], (part) => isIdentifier(part, "effectDur"))
  );
  const timesCleanup = nodes.some((node) =>
    isNamedCall(node, "setTimeout") &&
    subtreeHas(node.arguments[1], (part) => isIdentifier(part, "effectDur"))
  );
  return resolvesDuration && appliesDuration && rampsDuration && timesCleanup;
}

function hasPauseAllDecks(source) {
  return syntaxNodes(source).some((node) => {
    const isServerPaused = node.type === "IfStatement" &&
      node.test.type === "MemberExpression" &&
      !node.test.computed &&
      isIdentifier(node.test.object, "s") &&
      isIdentifier(node.test.property, "is_paused");
    if (!isServerPaused) return false;
    return subtreeHas(node.consequent, (part) =>
      part.type === "ForOfStatement" &&
      isIdentifier(part.right, "decks") &&
      subtreeHas(part.body, (bodyPart) =>
        bodyPart.type === "CallExpression" &&
        bodyPart.callee.type === "MemberExpression" &&
        isIdentifier(bodyPart.callee.property, "pause") &&
        bodyPart.callee.object?.type === "MemberExpression" &&
        isIdentifier(bodyPart.callee.object.object, "d") &&
        isIdentifier(bodyPart.callee.object.property, "audio")
      )
    );
  });
}

function hasServerLedTrackChangeCrossfade(source) {
  return syntaxNodes(source).some((node) =>
    isNamedCall(node, "startCrossfade") &&
    isIdentifier(node.arguments[0], "path") &&
    isIdentifier(node.arguments[1], "_crossfadeSecondsCache") &&
    isLiteral(node.arguments[2], true)
  );
}

export const REQUIRED_HOTKEY_SOURCE_KEYS = Object.freeze([
  "duplicateListenerRemoved",
  "windowCapturePhase",
  "pressLatch",
  "eRepeatGuard",
  "isTypingTargetNarrowed",
  "modalShowModal",
  "closeBtnFocus",
  "durationUnified",
  "silenceTrigger95pct",
  "silenceMs2000",
  "repickNextEndpoint",
  "pauseBothDecks",
  "shuffleCrossfadeOnUnexpectedChange",
]);

export function hotkeySourceChecks(source) {
  return {
    duplicateListenerRemoved:
      !/document\.addEventListener\("keydown"/.test(source.hotkeys) &&
      (source.hotkeys.match(/window\.addEventListener\("keydown"/g) || []).length === 1,
    windowCapturePhase: hasCapturedWindowKeydown(source.hotkeys),
    pressLatch: /const _pressed = new Set\(\)/.test(source.hotkeys),
    eRepeatGuard: /if \(e\.repeat\) return;/.test(source.hotkeys),
    isTypingTargetNarrowed: hasNarrowTypingInputTypes(source.domHelpers),
    modalShowModal: /modal\.showModal\(\)/.test(source.hotkeys),
    closeBtnFocus: /closeBtn\.focus\(\)/.test(source.hotkeys),
    durationUnified: hasUnifiedEffectDuration(source.audio),
    silenceTrigger95pct: /currentTime > dur \* 0\.95/.test(source.audio),
    silenceMs2000: /silenceMs >= 2000/.test(source.audio),
    repickNextEndpoint: hasExecutableRepickRequest(source.audio),
    pauseBothDecks: hasPauseAllDecks(source.audio),
    shuffleCrossfadeOnUnexpectedChange: hasServerLedTrackChangeCrossfade(source.audio),
  };
}

export function validateHotkeyAudit(name, result) {
  equal(
    Object.keys(result.source).sort().join(","),
    [...REQUIRED_HOTKEY_SOURCE_KEYS].sort().join(","),
    `${name} source keys`,
  );
  for (const key of REQUIRED_HOTKEY_SOURCE_KEYS) {
    check(result.source[key] === true, `${name} source.${key}`);
  }
  equal(result.dom.modalExists, true, `${name} modal exists`);
  equal(result.dom.legacyDetailsGone, true, `${name} legacy details gone`);
  equal(result.dom.btnShortcutsExists, true, `${name} shortcut trigger exists`);
  equal(result.dom.btnShortcutsCloseExists, true, `${name} shortcut close exists`);
  equal(result.dom.modalAriaLabelledBy, "hotkey-help-title", `${name} modal label`);
  equal(result.behaviour.modalInitiallyClosed, true, `${name} modal initially closed`);
  equal(result.behaviour.modalOpenAfterQuestion, true, `${name} modal question open`);
  equal(result.behaviour.closeBtnFocused, true, `${name} modal close focus`);
  equal(result.behaviour.modalClosedAfterCloseBtn, true, `${name} modal close button`);
  equal(result.behaviour.modalOpenAfterTrigger, true, `${name} modal trigger open`);
  equal(result.behaviour.shuffleClicksLatched, 1, `${name} press latch`);
  equal(result.behaviour.shuffleClicksAfterRelease, 2, `${name} release latch`);
  equal(result.behaviour.muteClicksFromSliderFocus, 0, `${name} slider native ownership`);
  equal(result.behaviour.pauseClicksFromSearchInput, 0, `${name} input suppression`);
  equal(result.errors.length, 0, `${name} browser errors`);
}

export function validateHealthAudit(name, result) {
  equal(result.console.filter((entry) => entry.type === "error").length, 0,
    `${name} console errors`);
  equal(result.pageerrors.length, 0, `${name} page errors`);
  equal(result.requestfailed.length, 0, `${name} failed requests`);
  equal(result.status_4xx_5xx.length, 0, `${name} HTTP errors`);
  equal(result.unhandled.length, 0, `${name} interaction errors`);
  check(result.websockets.some((socket) => socket.opened), `${name} websocket did not open`);
  equal(result.websockets.flatMap((socket) => socket.errors).length, 0,
    `${name} websocket errors`);
  check(result.probe && !result.probe.error, `${name} page probe failed`);
  check(result.probe.title.includes("AutoDJ"), `${name} title missing AutoDJ`);
  check(result.probe.hasAudio, `${name} audio element missing`);
}

export function validateRegressionAudit(name, result) {
  equal(result.dom.lyricsCardInNow, true, `${name} lyrics location`);
  equal(result.dom.lyricsCardInSettings, false, `${name} lyrics absent from settings`);
  equal(result.dom.cueListSummaryGone, true, `${name} legacy cue list`);
  equal(result.dom.linerCheckboxOutsideTrigger, true, `${name} liner checkbox placement`);
  equal(result.dom.linerCheckboxDescribedBy, "ln-enabled-desc",
    `${name} liner checkbox description`);
  equal(result.hotkeys.volUnchangedOnSettingsTab, true, `${name} settings hotkey gate`);
  equal(result.hotkeys.shortcutsDialogOpensFromAnyTab, true, `${name} help shortcut`);
  equal(result.hotkeys.volChangedOnNowTab, true, `${name} now-playing hotkey`);
  check(Boolean(result.liveRegion.announced), `${name} live region did not announce`);
  equal(result.liveRegion.cleared, true, `${name} live region did not clear`);
  equal(result.errors.length, 0, `${name} browser errors`);
}

export function validateTransitionAudit(name, result) {
  check(result.workletReady, `${name} audio element/worklet readiness failed`);
  equal(result.probe.errors.length, 0, `${name} probe errors`);
  for (const [worklet, status] of Object.entries(result.probe.worklets)) {
    equal(status, "ok", `${name} ${worklet} worklet`);
  }
  for (const [effect, response] of Object.entries(result.transitions)) {
    equal(response.status, 200, `${name} ${effect} transition response`);
  }
  equal(result.logs.filter((entry) => entry.type === "pageerror").length, 0,
    `${name} page errors`);
  equal(result.logs.filter((entry) => entry.type === "error").length, 0,
    `${name} console errors`);
}

function writeReportAtomically(report, results) {
  const temporary = `${report}.${process.pid}.${randomUUID()}.tmp`;
  try {
    writeFileSync(temporary, `${JSON.stringify(results, null, 2)}\n`, "utf8");
    renameSync(temporary, report);
  } finally {
    rmSync(temporary, { force: true });
  }
}

export async function runAudit({ audit, validate, report }) {
  const results = {};
  const failures = [];
  for (const [name, launcher] of selectedBrowsers()) {
    let result;
    try {
      result = await audit(name, launcher);
    } catch (error) {
      results[name] = { error: String(error) };
      failures.push(`${name}: ${error}`);
      writeReportAtomically(report, results);
      continue;
    }
    try {
      validate(name, result);
      results[name] = result;
    } catch (error) {
      const diagnostic = result && typeof result === "object" && !Array.isArray(result)
        ? { ...result }
        : { result };
      diagnostic.validationError = String(error);
      results[name] = diagnostic;
      failures.push(`${name}: ${error}`);
    }
    writeReportAtomically(report, results);
  }
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exitCode = 1;
  }
}
