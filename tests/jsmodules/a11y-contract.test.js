import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { THEMES, cssForTheme, themeTokens } from "./theme-css.js";

const staticPath = (...parts) => join(
  process.cwd(), "src", "autodj", "static", ...parts,
);
const cssSource = readFileSync(staticPath("app.css"), "utf8");
const htmlSource = readFileSync(staticPath("index.html"), "utf8");
const appSource = readFileSync(staticPath("app.js"), "utf8");
const tokensByTheme = new Map(THEMES.map((theme) => [theme, themeTokens(cssSource, theme)]));
const token = (theme, name) => tokensByTheme.get(theme).get(name);

function stripCssComments(source) {
  let result = "";
  let quote = null;
  for (let index = 0; index < source.length; index += 1) {
    const current = source[index];
    const next = source[index + 1];
    if (quote) {
      result += current;
      if (current === "\\") {
        result += next ?? "";
        index += 1;
      } else if (current === quote) {
        quote = null;
      }
    } else if (current === "\"" || current === "'") {
      quote = current;
      result += current;
    } else if (current === "/" && next === "*") {
      index += 2;
      while (index < source.length
        && !(source[index] === "*" && source[index + 1] === "/")) {
        index += 1;
      }
      index += 1;
    } else {
      result += current;
    }
  }
  return result;
}

function findBoundary(source, start, targets) {
  let quote = null;
  let parentheses = 0;
  let brackets = 0;
  for (let index = start; index < source.length; index += 1) {
    const current = source[index];
    if (quote) {
      if (current === "\\") index += 1;
      else if (current === quote) quote = null;
      continue;
    }
    if (current === "\"" || current === "'") quote = current;
    else if (current === "(") parentheses += 1;
    else if (current === ")") parentheses -= 1;
    else if (current === "[") brackets += 1;
    else if (current === "]") brackets -= 1;
    else if (parentheses === 0 && brackets === 0 && targets.has(current)) {
      return index;
    }
  }
  return -1;
}

function findClosingBrace(source, openIndex) {
  let depth = 1;
  let quote = null;
  for (let index = openIndex + 1; index < source.length; index += 1) {
    const current = source[index];
    if (quote) {
      if (current === "\\") index += 1;
      else if (current === quote) quote = null;
      continue;
    }
    if (current === "\"" || current === "'") quote = current;
    else if (current === "{") depth += 1;
    else if (current === "}" && --depth === 0) return index;
  }
  throw new Error("Unclosed CSS block");
}

function splitTopLevel(source, separator) {
  const values = [];
  let start = 0;
  while (start <= source.length) {
    const boundary = findBoundary(source, start, new Set([separator]));
    if (boundary === -1) {
      values.push(source.slice(start).trim());
      break;
    }
    values.push(source.slice(start, boundary).trim());
    start = boundary + 1;
  }
  return values.filter(Boolean);
}

function parseDeclarations(block) {
  const declarations = new Map();
  for (const declaration of splitTopLevel(block, ";")) {
    const colon = findBoundary(declaration, 0, new Set([":"]));
    if (colon === -1) continue;
    const property = declaration.slice(0, colon).trim().toLowerCase();
    if (declarations.has(property)) declarations.delete(property);
    declarations.set(property, declaration.slice(colon + 1).trim());
  }
  return declarations;
}

function parseCss(source, media = [], rules = []) {
  const clean = stripCssComments(source);
  let index = 0;
  while (index < clean.length) {
    while (/\s/.test(clean[index] ?? "")) index += 1;
    if (index >= clean.length) break;
    const boundary = findBoundary(clean, index, new Set(["{", ";"]));
    if (boundary === -1) break;
    const prelude = clean.slice(index, boundary).trim();
    if (clean[boundary] === ";") {
      index = boundary + 1;
      continue;
    }
    const close = findClosingBrace(clean, boundary);
    const block = clean.slice(boundary + 1, close);
    if (/^@media\b/i.test(prelude)) {
      parseCss(block, [...media, prelude.replace(/^@media\s*/i, "").trim()], rules);
    } else if (!prelude.startsWith("@")) {
      rules.push({
        declarations: parseDeclarations(block),
        media,
        selectors: splitTopLevel(prelude, ",")
          .map((selector) => selector.replace(/\s+/g, " ")),
      });
    }
    index = close + 1;
  }
  return rules;
}

const cssRules = parseCss(cssSource);
const normalizeMedia = (value) => value.replace(/\s+/g, " ").trim();
const specificityBySelector = new Map();
let matchesByElement = new WeakMap();
const expandedByDeclaration = new Map();
const mediaMatchesByRule = new WeakMap();
const candidatesByRules = new WeakMap();
let cascadeContractDocumentInstalled = false;

function declarationsFor(selector, media = null, rules = cssRules) {
  return rules.filter((rule) => rule.selectors.includes(selector)
    && (media === null
      ? rule.media.length === 0
      : rule.media.length === 1
        && normalizeMedia(rule.media[0]) === media));
}

function lastValue(selector, property, media = null, rules = cssRules) {
  const values = declarationsFor(selector, media, rules)
    .map((rule) => rule.declarations.get(property))
    .filter((value) => value !== undefined);
  return values.at(-1);
}

function selectorSpecificity(selector) {
  if (specificityBySelector.has(selector)) return specificityBySelector.get(selector);
  const ids = selector.match(/#[\w-]+/g)?.length || 0;
  const attributes = selector.match(/\[[^\]]+\]/g)?.length || 0;
  const classes = selector.match(/\.[\w-]+/g)?.length || 0;
  const pseudoClasses = selector.match(/:(?!:)[\w-]+(?:\([^)]*\))?/g)?.length || 0;
  const typeSource = selector
    .replace(/::[\w-]+/g, " ")
    .replace(/#[\w-]+|\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+(?:\([^)]*\))?/g, " ");
  const types = typeSource.match(/(?:^|[\s>+~])([a-z][\w-]*)/gi)?.length || 0;
  const pseudoElements = selector.match(/::[\w-]+/g)?.length || 0;
  const specificity = [
    ids,
    attributes + classes + pseudoClasses,
    types + pseudoElements,
  ];
  specificityBySelector.set(selector, specificity);
  return specificity;
}

function compareSpecificity(first, second) {
  for (let index = 0; index < first.length; index += 1) {
    if (first[index] !== second[index]) return first[index] - second[index];
  }
  return 0;
}

function selectorMatches(element, selector, pseudoElement) {
  let matches = matchesByElement.get(element);
  if (!matches) {
    matches = new Map();
    matchesByElement.set(element, matches);
  }
  const cacheKey = `${pseudoElement}\0${selector}`;
  if (matches.has(cacheKey)) return matches.get(cacheKey);
  const pseudoMatch = selector.match(/(::[\w-]+)$/);
  if ((pseudoMatch?.[1] || "") !== pseudoElement) {
    matches.set(cacheKey, false);
    return false;
  }
  const elementSelector = selector
    .slice(0, pseudoMatch ? -pseudoMatch[1].length : undefined)
    .replaceAll(":focus-visible", ".a11y-focus-visible") || "*";
  try {
    const result = element.matches(elementSelector);
    matches.set(cacheKey, result);
    return result;
  } catch {
    matches.set(cacheKey, false);
    return false;
  }
}

function mediaFeatureMatches(feature, value, context) {
  const normalizedFeature = feature.trim().toLowerCase();
  const normalizedValue = value.trim().toLowerCase();
  if (normalizedFeature === "prefers-reduced-motion") {
    return normalizedValue === (context.reducedMotion ? "reduce" : "no-preference");
  }
  if (normalizedFeature === "forced-colors") {
    return normalizedValue === (context.forcedColors ? "active" : "none");
  }
  if (["min-width", "max-width"].includes(normalizedFeature)) {
    const width = Number.parseFloat(normalizedValue);
    return normalizedFeature === "min-width"
      ? context.viewportWidth >= width
      : context.viewportWidth <= width;
  }
  if (["min-height", "max-height"].includes(normalizedFeature)) {
    const height = Number.parseFloat(normalizedValue);
    return normalizedFeature === "min-height"
      ? context.viewportHeight >= height
      : context.viewportHeight <= height;
  }
  if (normalizedFeature === "orientation") {
    const orientation = context.viewportWidth >= context.viewportHeight
      ? "landscape" : "portrait";
    return normalizedValue === orientation;
  }
  return false;
}

function mediaQueryMatches(query, context) {
  return splitTopLevel(query, ",").some((branch) => {
    const normalized = branch.trim().toLowerCase();
    const negate = /^not\b/.test(normalized);
    const features = [...normalized.matchAll(/\(([^:()]+):\s*([^()]+)\)/g)];
    const mediaType = normalized
      .replace(/^not\s+|^only\s+/, "")
      .split(/\s+and\s+/)[0]
      .trim();
    const typeMatches = mediaType.startsWith("(")
      || ["", "all", "screen"].includes(mediaType);
    const matches = typeMatches && features.every((match) => (
      mediaFeatureMatches(match[1], match[2], context)
    ));
    return negate ? !matches : matches;
  });
}

function ruleApplies(rule, context) {
  let contextMatches = mediaMatchesByRule.get(rule);
  if (!contextMatches) {
    contextMatches = new WeakMap();
    mediaMatchesByRule.set(rule, contextMatches);
  }
  if (!contextMatches.has(context)) {
    contextMatches.set(
      context,
      rule.media.every((query) => mediaQueryMatches(query, context)),
    );
  }
  return contextMatches.get(context);
}

function representativeAxisValues(rules, dimension, fallback) {
  const values = new Set([fallback]);
  const breakpointPattern = new RegExp(
    `\\((?:min|max)-${dimension}\\s*:\\s*(-?\\d+(?:\\.\\d+)?)`, "gi",
  );
  for (const rule of rules) {
    for (const query of rule.media) {
      for (const match of query.matchAll(breakpointPattern)) {
        const breakpoint = Number.parseFloat(match[1]);
        values.add(Math.max(0, breakpoint - 1));
        values.add(Math.max(0, breakpoint));
        values.add(breakpoint + 1);
      }
    }
  }
  return [...values].sort((first, second) => first - second);
}

function representativeMediaContexts(rules, {
  forcedColors = [false],
  reducedMotion = [false],
} = {}) {
  const widths = representativeAxisValues(rules, "width", 1024);
  const heights = representativeAxisValues(rules, "height", 768);
  const contexts = [];
  for (const forcedColorState of forcedColors) {
    for (const reducedMotionState of reducedMotion) {
      for (const viewportWidth of widths) {
        for (const viewportHeight of heights) {
          contexts.push({
            forcedColors: forcedColorState,
            reducedMotion: reducedMotionState,
            viewportHeight,
            viewportWidth,
          });
        }
      }
    }
  }
  const uniqueCascades = new Map();
  for (const context of contexts) {
    const signature = rules.map((rule) => Number(ruleApplies(rule, context))).join("");
    if (!uniqueCascades.has(signature)) uniqueCascades.set(signature, context);
  }
  return [...uniqueCascades.values()];
}

function splitCssValue(value) {
  const tokens = [];
  let start = 0;
  let parentheses = 0;
  for (let index = 0; index <= value.length; index += 1) {
    const character = value[index];
    if (character === "(") parentheses += 1;
    else if (character === ")") parentheses -= 1;
    if ((index === value.length || (/\s/.test(character) && parentheses === 0))
      && index > start) {
      tokens.push(value.slice(start, index));
      start = index + 1;
    } else if (/\s/.test(character) && parentheses === 0) {
      start = index + 1;
    }
  }
  return tokens;
}

function boxSideValues(tokens) {
  if (tokens.length === 1) return [tokens[0], tokens[0], tokens[0], tokens[0]];
  if (tokens.length === 2) return [tokens[0], tokens[1], tokens[0], tokens[1]];
  if (tokens.length === 3) return [tokens[0], tokens[1], tokens[2], tokens[1]];
  return tokens.slice(0, 4);
}

function borderComponents(value) {
  const tokens = splitCssValue(value);
  const styles = new Set([
    "none", "hidden", "dotted", "dashed", "solid", "double",
    "groove", "ridge", "inset", "outset",
  ]);
  const widthPattern = /^(?:0|thin|medium|thick|(?:\d*\.)?\d+(?:px|r?em|%))$/i;
  const style = tokens.find((token) => styles.has(token.toLowerCase())) || "none";
  const width = tokens.find((token) => widthPattern.test(token)) || "medium";
  const color = tokens.filter((token) => !styles.has(token.toLowerCase())
    && !widthPattern.test(token)).join(" ") || "currentcolor";
  return { color, style, width };
}

function backgroundColorFromShorthand(value) {
  const finalLayer = splitTopLevel(value, ",").at(-1) || "";
  const tokens = splitCssValue(finalLayer);
  const explicitColor = tokens.find((token) => (
    /^(?:#[\da-f]{3,8}|(?:rgb|hsl)a?\(|transparent$|currentcolor$)/i.test(token)
      || /^(?:Canvas|CanvasText|Highlight)$/i.test(token)
  ));
  if (explicitColor) return explicitColor;
  return tokens.length === 1 && !/^(?:url|(?:repeating-)?(?:linear|radial)-gradient)\(/i.test(tokens[0])
    ? tokens[0] : "transparent";
}

function expandedDeclaration(property, value) {
  const cacheKey = `${property}\0${value}`;
  if (expandedByDeclaration.has(cacheKey)) return expandedByDeclaration.get(cacheKey);
  const expanded = new Map([[property, value]]);
  const sides = ["top", "right", "bottom", "left"];
  if (["border-color", "border-style", "border-width"].includes(property)) {
    const component = property.slice("border-".length);
    boxSideValues(splitCssValue(value)).forEach((sideValue, index) => {
      expanded.set(`border-${sides[index]}-${component}`, sideValue);
    });
  } else if (property === "border") {
    const components = borderComponents(value);
    for (const side of sides) {
      for (const [component, componentValue] of Object.entries(components)) {
        expanded.set(`border-${side}-${component}`, componentValue);
      }
    }
  } else {
    const sideMatch = property.match(/^border-(top|right|bottom|left)$/);
    if (sideMatch) {
      const components = borderComponents(value);
      for (const [component, componentValue] of Object.entries(components)) {
        expanded.set(`border-${sideMatch[1]}-${component}`, componentValue);
      }
    }
  }
  if (property === "outline") {
    const components = borderComponents(value);
    for (const [component, componentValue] of Object.entries(components)) {
      expanded.set(`outline-${component}`, componentValue);
    }
  }
  if (property === "background") {
    expanded.set("background-color", backgroundColorFromShorthand(value));
  }
  expandedByDeclaration.set(cacheKey, expanded);
  return expanded;
}

function declarationCandidates(rules) {
  if (candidatesByRules.has(rules)) return candidatesByRules.get(rules);
  const byProperty = new Map();
  rules.forEach((rule, order) => {
    [...rule.declarations].forEach(([declaredProperty, rawValue], declarationOrder) => {
      const important = /\s*!important\s*$/i.test(rawValue);
      const declaredValue = rawValue.replace(/\s*!important\s*$/i, "").trim();
      for (const [property, value] of expandedDeclaration(
        declaredProperty, declaredValue,
      )) {
        if (!byProperty.has(property)) byProperty.set(property, []);
        for (const selector of rule.selectors) {
          byProperty.get(property).push({
            important,
            order: order * 1000 + declarationOrder,
            rule,
            selector,
            specificity: selectorSpecificity(selector),
            value,
          });
        }
      }
    });
  });
  candidatesByRules.set(rules, byProperty);
  return byProperty;
}

function effectiveMediaDeclaration(
  element, pseudoElement, property, context, rules,
) {
  let winner;
  for (const candidate of declarationCandidates(rules).get(property) || []) {
    if (!ruleApplies(candidate.rule, context)
      || !selectorMatches(element, candidate.selector, pseudoElement)) continue;
    const outranks = !winner
      || Number(candidate.important) > Number(winner.important)
      || (candidate.important === winner.important
        && (compareSpecificity(candidate.specificity, winner.specificity) > 0
          || (compareSpecificity(candidate.specificity, winner.specificity) === 0
            && candidate.order >= winner.order)));
    if (outranks) winner = candidate;
  }
  return winner;
}

function reducedMotionMeetsContract(rules = cssRules) {
  installCascadeContractDocument();
  const elements = [...document.querySelectorAll("*")];
  const expected = new Map([
    ["animation-duration", "0.01ms"],
    ["animation-iteration-count", "1"],
    ["scroll-behavior", "auto"],
    ["transition-duration", "0.01ms"],
  ]);
  return representativeMediaContexts(rules, {
    forcedColors: [false, true],
    reducedMotion: [true],
  })
    .every((context) => {
      const motionRulesPass = elements.every((element) => (
        ["", "::before", "::after"].every((pseudoElement) => (
          [...expected].every(([property, value]) => {
            const declaration = effectiveMediaDeclaration(
              element, pseudoElement, property, context, rules,
            );
            return declaration?.important === true && declaration.value === value;
          })
        ))
      ));
      const htmlScroll = effectiveMediaDeclaration(
        document.documentElement, "", "scroll-behavior", context, rules,
      );
      return motionRulesPass && htmlScroll?.value === "auto";
    });
}

function forcedColorsMeetsContract(rules = cssRules) {
  installCascadeContractDocument();
  const focusElements = focusContractControls();
  for (const selector of ["details.card > summary", "section[data-view] > h2"]) {
    focusElements.push(document.querySelector(selector));
  }
  for (const element of focusElements) element.classList.add("a11y-focus-visible");
  matchesByElement = new WeakMap();
  return representativeMediaContexts(rules, {
    forcedColors: [true],
    reducedMotion: [false, true],
  })
    .every((context) => {
      const bordersPass = contractControls().every((element) => (
        ["top", "right", "bottom", "left"].every((side) => {
          const borderColor = effectiveMediaDeclaration(
            element, "", `border-${side}-color`, context, rules,
          )?.value;
          const borderStyle = effectiveMediaDeclaration(
            element, "", `border-${side}-style`, context, rules,
          )?.value;
          const borderWidth = effectiveMediaDeclaration(
            element, "", `border-${side}-width`, context, rules,
          )?.value;
          return borderColor === "CanvasText"
            && borderStyle === "solid"
            && Number.parseFloat(borderWidth) >= 1;
        })
      ));
      const progressRail = effectiveMediaDeclaration(
        document.querySelector("#progress-track"), "::before",
        "background-color", context, rules,
      )?.value;
      const progressFill = effectiveMediaDeclaration(
        document.querySelector("#progress-fill"), "", "background-color", context, rules,
      )?.value;
      const focusPass = focusElements.every((element) => {
        const outlineColor = effectiveMediaDeclaration(
          element, "", "outline-color", context, rules,
        )?.value;
        const outlineStyle = effectiveMediaDeclaration(
          element, "", "outline-style", context, rules,
        )?.value;
        const outlineWidth = effectiveMediaDeclaration(
          element, "", "outline-width", context, rules,
        )?.value;
        return outlineColor === "Highlight" && outlineStyle === "solid"
          && Number.parseFloat(outlineWidth) >= 2;
      });
      return bordersPass && progressRail === "CanvasText"
        && progressFill === "Highlight" && focusPass;
    });
}

function parseColorChannel(value) {
  return value.endsWith("%")
    ? Number.parseFloat(value) * 2.55
    : Number.parseFloat(value);
}

function parseAlpha(value = "1") {
  return value.endsWith("%")
    ? Number.parseFloat(value) / 100
    : Number.parseFloat(value);
}

function parseCssColor(value) {
  const color = value.trim().toLowerCase();
  if (color === "transparent") return [0, 0, 0, 0];
  const hex = color.match(/^#([\da-f]{3,4}|[\da-f]{6}|[\da-f]{8})$/i)?.[1];
  if (hex) {
    const expanded = hex.length <= 4
      ? [...hex].map((character) => character.repeat(2)).join("")
      : hex;
    return [
      Number.parseInt(expanded.slice(0, 2), 16),
      Number.parseInt(expanded.slice(2, 4), 16),
      Number.parseInt(expanded.slice(4, 6), 16),
      expanded.length === 8 ? Number.parseInt(expanded.slice(6, 8), 16) / 255 : 1,
    ];
  }
  const functional = color.match(/^rgba?\((.*)\)$/i)?.[1];
  if (!functional) return null;
  const [channelsPart, slashAlpha] = functional.split("/").map((part) => part.trim());
  const parts = channelsPart.includes(",")
    ? channelsPart.split(",").map((part) => part.trim())
    : channelsPart.split(/\s+/);
  const alpha = slashAlpha ?? parts[3] ?? "1";
  if (parts.length < 3) return null;
  return [
    parseColorChannel(parts[0]),
    parseColorChannel(parts[1]),
    parseColorChannel(parts[2]),
    parseAlpha(alpha),
  ];
}

function compositeColor(foreground, background) {
  const alpha = foreground[3] + background[3] * (1 - foreground[3]);
  if (alpha === 0) return [0, 0, 0, 0];
  return [
    ...foreground.slice(0, 3).map((channel, index) => (
      (channel * foreground[3]
        + background[index] * background[3] * (1 - foreground[3])) / alpha
    )),
    alpha,
  ];
}

function colorToHex(color) {
  return `#${color.slice(0, 3).map((channel) => Math.round(channel).toString(16)
    .padStart(2, "0")).join("")}`;
}

function luminance(color) {
  const channels = parseCssColor(color).slice(0, 3).map((channel) => {
    const srgb = channel / 255;
    return srgb <= 0.04045
      ? srgb / 12.92
      : ((srgb + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1]
    + 0.0722 * channels[2];
}

function contrast(first, second) {
  const lighter = Math.max(luminance(first), luminance(second));
  const darker = Math.min(luminance(first), luminance(second));
  return (lighter + 0.05) / (darker + 0.05);
}

// Parsing the page and its stylesheet is much of these checks' work, and most
// cases install the same ones: keep the parsed page, and keep each <style>
// whose text is unchanged so happy-dom does not parse the stylesheet again.
const pageTemplates = new Map();

function pageTemplate(html) {
  let template = pageTemplates.get(html);
  if (!template) {
    template = document.createElement("template");
    template.innerHTML = html;
    template.content.querySelectorAll('script, link[rel="stylesheet"]')
      .forEach((element) => element.remove());
    // Only the real page is reused; one-off variants are parsed once.
    if (html === htmlSource) pageTemplates.set(html, template);
  }
  return template;
}

function installStyles(sheets) {
  const installed = [...document.head.querySelectorAll("style[data-a11y-contract]")];
  sheets.forEach((sheet, index) => {
    if (installed[index]?.textContent === sheet) return;
    const style = document.createElement("style");
    style.dataset.a11yContract = "";
    style.textContent = sheet;
    if (installed[index]) installed[index].replaceWith(style);
    else document.head.append(style);
  });
  installed.slice(sheets.length).forEach((element) => element.remove());
}

const userAgentCss = "pre { margin: 1em 0; }";

// ``extraCss`` goes in its own sheet after ``css``: the same cascade as
// appending it to ``css``, without parsing ``css`` again.
function installDocument({ css = cssSource, html = htmlSource, extraCss = "" } = {}) {
  document.body.replaceChildren(pageTemplate(html).content.cloneNode(true));
  installStyles([userAgentCss, css, extraCss]);
}

function installCascadeContractDocument() {
  if (cascadeContractDocumentInstalled) return;
  installDocument({ css: "" });
  cascadeContractDocumentInstalled = true;
}

function normalizedColor(value, background = "#fff") {
  const foreground = parseCssColor(value);
  const backdrop = parseCssColor(background);
  if (!foreground || !backdrop) return value.toLowerCase();
  return colorToHex(compositeColor(foreground, backdrop));
}

function isTransparentColor(value) {
  return parseCssColor(value)?.[3] === 0;
}

function actualBackgroundColor(element) {
  const layers = [];
  for (let current = element; current; current = current.parentElement) {
    const layer = parseCssColor(window.getComputedStyle(current).backgroundColor);
    if (layer) layers.push(layer);
  }
  return layers.reverse().reduce(
    (background, layer) => compositeColor(layer, background),
    parseCssColor("#fff"),
  );
}

function indicatorColorMeetsContract(element, value, expected) {
  const foreground = parseCssColor(value);
  if (!foreground || foreground[3] === 0) return false;
  const background = actualBackgroundColor(element.parentElement);
  const visibleColor = compositeColor(foreground, background);
  const visibleHex = colorToHex(visibleColor);
  const backgroundHex = colorToHex(background);
  return visibleHex === expected && contrast(visibleHex, backgroundHex) >= 3;
}

function numericCssValues(value) {
  return [...value.matchAll(/-?\d+(?:\.\d+)?/g)]
    .map((match) => Number.parseFloat(match[0]));
}

function transformTranslations(transform) {
  if (["", "none"].includes(transform)) return { x: 0, y: 0 };
  const matrix3d = transform.match(/^matrix3d\(([^)]+)\)$/i);
  if (matrix3d) {
    const values = numericCssValues(matrix3d[1]);
    return { x: values[12] || 0, y: values[13] || 0 };
  }
  const matrix = transform.match(/^matrix\(([^)]+)\)$/i);
  if (matrix) {
    const values = numericCssValues(matrix[1]);
    return { x: values[4] || 0, y: values[5] || 0 };
  }
  const translation = { x: 0, y: 0 };
  for (const match of transform.matchAll(/translate(3d|x|y)?\(([^)]+)\)/gi)) {
    const values = numericCssValues(match[2]);
    const axis = match[1]?.toLowerCase();
    if (axis === "y") translation.y += values[0] || 0;
    else if (axis === "x") translation.x += values[0] || 0;
    else {
      translation.x += values[0] || 0;
      translation.y += values[1] || 0;
    }
  }
  return translation;
}

function inferredAxisBounds(startValue, endValue, sizeValue, viewportSize) {
  const start = Number.parseFloat(startValue);
  const end = Number.parseFloat(endValue);
  const size = Number.parseFloat(sizeValue);
  let lower = Number.isFinite(start) ? start : null;
  let upper = Number.isFinite(end) ? viewportSize - end : null;
  if (lower !== null && Number.isFinite(size)) upper = lower + size;
  else if (upper !== null && Number.isFinite(size)) lower = upper - size;
  return { lower, upper };
}

function boundsAreFullyOutside(bounds, viewportSize) {
  return (bounds.upper !== null && bounds.upper <= 0)
    || (bounds.lower !== null && bounds.lower >= viewportSize);
}

function boundsAreComplete(bounds) {
  return bounds.lower !== null && bounds.upper !== null;
}

function translatedBounds(bounds, translation) {
  return {
    lower: bounds.lower === null ? null : bounds.lower + translation,
    upper: bounds.upper === null ? null : bounds.upper + translation,
  };
}

function hasViewportDisplacement(element, computed) {
  const viewportWidth = window.innerWidth || 1024;
  const viewportHeight = window.innerHeight || 768;
  const rect = element.getBoundingClientRect();
  const hasMeasuredRect = rect.width > 0 || rect.height > 0
    || [rect.left, rect.right, rect.top, rect.bottom].some((value) => value !== 0);
  if (hasMeasuredRect) {
    return rect.right <= 0 || rect.bottom <= 0
      || rect.left >= viewportWidth || rect.top >= viewportHeight;
  }

  const translation = transformTranslations(computed.transform);
  let horizontalBoundsResolved = false;
  let verticalBoundsResolved = false;
  if (["absolute", "fixed"].includes(computed.position)) {
    const horizontalBounds = translatedBounds(inferredAxisBounds(
      computed.left, computed.right, computed.width, viewportWidth,
    ), translation.x);
    const verticalBounds = translatedBounds(inferredAxisBounds(
      computed.top, computed.bottom, computed.height, viewportHeight,
    ), translation.y);
    horizontalBoundsResolved = boundsAreComplete(horizontalBounds);
    verticalBoundsResolved = boundsAreComplete(verticalBounds);
    if (boundsAreFullyOutside(horizontalBounds, viewportWidth)
      || boundsAreFullyOutside(verticalBounds, viewportHeight)) {
      return true;
    }
    const horizontalOffsets = [computed.left, computed.right]
      .map((value) => Number.parseFloat(value))
      .filter(Number.isFinite);
    const verticalOffsets = [computed.top, computed.bottom]
      .map((value) => Number.parseFloat(value))
      .filter(Number.isFinite);
    if ((!horizontalBoundsResolved
        && horizontalOffsets.some((offset) => Math.abs(offset) >= viewportWidth))
      || (!verticalBoundsResolved
        && verticalOffsets.some((offset) => Math.abs(offset) >= viewportHeight))) {
      return true;
    }
  }
  if ((!horizontalBoundsResolved && Math.abs(translation.x) >= viewportWidth)
    || (!verticalBoundsResolved && Math.abs(translation.y) >= viewportHeight)) {
    return true;
  }
  return false;
}

function hasVisuallyHiddenGeometry(element, computed) {
  const clipped = !["", "auto", "none"].includes(computed.clip)
    || !["", "none"].includes(computed.clipPath);
  const tinyAndClipped = Number.parseFloat(computed.width) <= 1
    && Number.parseFloat(computed.height) <= 1
    && (clipped || computed.overflow === "hidden");
  return (["absolute", "fixed"].includes(computed.position) && tinyAndClipped)
    || hasViewportDisplacement(element, computed);
}

// Most status cases change only the stylesheet.  Cloning the whole page for
// each was most of the test's time, so the page stays while its markup is
// the same and it is still the installed one; the only changes a check makes
// are the "connected" class, which every check adds, and a measured box,
// which it takes away again.
let statusPage = null;

function installStatusPage(html, extraCss) {
  const installed = document.querySelector("#conn-status");
  if (statusPage?.html === html && installed && statusPage.status === installed) {
    installStyles([userAgentCss, cssSource, extraCss]);
  } else {
    installDocument({ extraCss, html });
    statusPage = { html, status: document.querySelector("#conn-status") };
  }
  return document.querySelector("#conn-status");
}

function connectedStatusIsVisible({
  extraCss = "",
  html = htmlSource,
  statusRect,
} = {}) {
  const status = installStatusPage(html, extraCss);
  if (!status || status.textContent.trim() === "") return false;
  if (statusRect) status.getBoundingClientRect = () => statusRect;
  try {
    status.classList.add("connected");
    for (let current = status; current; current = current.parentElement) {
      const computed = window.getComputedStyle(current);
      if (current.hidden
        || current.getAttribute("aria-hidden") === "true"
        || current.classList.contains("visually-hidden")
        || computed.display === "none"
        || ["hidden", "collapse"].includes(computed.visibility)
        || Number.parseFloat(computed.opacity) === 0
        || hasVisuallyHiddenGeometry(current, computed)) {
        return false;
      }
    }
    const statusStyle = window.getComputedStyle(status);
    return !isTransparentColor(statusStyle.color)
      && !isTransparentColor(statusStyle.backgroundColor);
  } finally {
    delete status.getBoundingClientRect;
  }
}

function contractControls() {
  if (!document.querySelector("textarea")) {
    const textarea = document.createElement("textarea");
    textarea.id = "a11y-textarea-fixture";
    document.body.append(textarea);
  }
  return [...new Set(document.querySelectorAll([
    "button",
    "#progress-track",
    'input:not([type="checkbox"]):not([type="radio"])',
    "select",
    "textarea",
    '#view-nav [role="tab"]',
  ].join(", ")))];
}

function focusContractControls() {
  contractControls();
  return [...new Set(document.querySelectorAll([
    "button",
    "#progress-track",
    "input",
    "select",
    "textarea",
    '#view-nav [role="tab"]',
  ].join(", ")))];
}

function nativeChoiceControlsRemainNative(extraCss = "") {
  const rules = extraCss ? [...cssRules, ...parseCss(extraCss)] : cssRules;
  installDocument({ extraCss });
  for (const type of ["checkbox", "radio"]) {
    for (const checked of [false, true]) {
      const fixture = document.createElement("input");
      fixture.type = type;
      fixture.checked = checked;
      fixture.id = `a11y-${type}-${checked ? "checked" : "unchecked"}-fixture`;
      document.body.append(fixture);
    }
  }
  const controls = [...document.querySelectorAll('input[type="checkbox"], input[type="radio"]')];
  const isCustomBoundaryProperty = (property) => property === "appearance"
    || property === "-webkit-appearance"
    || property === "border"
    || property.startsWith("border-");
  const defaultColorContexts = representativeMediaContexts(rules, {
    reducedMotion: [false, true],
  });
  return controls.every((control) => {
    const computedAppearance = window.getComputedStyle(control).appearance;
    return ["", "auto"].includes(computedAppearance)
      && defaultColorContexts.every((context) => {
        const authoredProperties = rules
          .filter((rule) => ruleApplies(rule, context)
            && rule.selectors.some((selector) => selectorMatches(control, selector, "")))
          .flatMap((rule) => [...rule.declarations.keys()]);
        return authoredProperties.every((property) => !isCustomBoundaryProperty(property));
      });
  });
}

// A rejection case names the control its rule breaks (``only``).  Checking
// every other control again would cost a full style pass per case, and the
// unmodified stylesheet already has its own test over every control.
function onlyMatching(elements, only) {
  return only ? elements.filter((element) => element.matches(only)) : elements;
}

// app.css resolved once per theme (light-dark() picked), for the contracts
// that read colours through happy-dom.
const cssByTheme = new Map(THEMES.map((theme) => [theme, cssForTheme(cssSource, theme)]));

function boundariesMeetContract({ extraCss = "", only = null, theme = "dark" } = {}) {
  installDocument({ css: cssByTheme.get(theme), extraCss: cssForTheme(extraCss, theme) });
  const expected = token(theme, "--control-border");
  const controls = onlyMatching(contractControls(), only);
  if (controls.length === 0) throw new Error(`No control matches ${only}`);
  return controls.every((control) => {
    const computed = window.getComputedStyle(control);
    return Number.parseFloat(computed.borderTopWidth) >= 1
      && computed.borderTopStyle === "solid"
      && indicatorColorMeetsContract(control, computed.borderTopColor, expected);
  });
}

const focusVisibleAsClass = (css) => css.replaceAll(":focus-visible", ".a11y-focus-visible");
const focusCssByTheme = new Map(
  THEMES.map((theme) => [theme, focusVisibleAsClass(cssByTheme.get(theme))]),
);

function focusOutlinesMeetContract({ extraCss = "", only = null, theme = "dark" } = {}) {
  installDocument({
    css: focusCssByTheme.get(theme),
    extraCss: focusVisibleAsClass(cssForTheme(extraCss, theme)),
  });
  const accent = token(theme, "--accent");
  const controls = focusContractControls().filter((control) => !control.disabled);
  const expectedOutlines = new Map([
    ["details.card > summary", 3],
    ["section[data-view] > h2", 2],
  ].filter(([selector]) => onlyMatching([document.querySelector(selector)], only).length));
  for (const control of controls) control.classList.add("a11y-focus-visible");
  const checked = onlyMatching(controls, only);
  if (checked.length + expectedOutlines.size === 0) {
    throw new Error(`No control matches ${only}`);
  }
  const controlsPass = checked.every((control) => {
    const computed = window.getComputedStyle(control);
    return Number.parseFloat(computed.outlineWidth) === 2
      && computed.outlineStyle === "solid"
      && indicatorColorMeetsContract(control, computed.outlineColor, accent);
  });
  const specialFocusPass = [...expectedOutlines].every(([selector, width]) => {
    const element = document.querySelector(selector);
    element.classList.add("a11y-focus-visible");
    const computed = window.getComputedStyle(element);
    return Number.parseFloat(computed.outlineWidth) === width
      && computed.outlineStyle === "solid"
      && indicatorColorMeetsContract(element, computed.outlineColor, accent);
  });
  return controlsPass && specialFocusPass;
}

function libraryLogOwnershipIsScoped(html = htmlSource) {
  installDocument({ html });
  const status = document.querySelector("#lib-job-status");
  // Folder browsing has its own announcement; scope this check to the job card.
  const panel = status.closest("details");
  const log = document.querySelector("#library-log");
  const liveOwners = new Set(panel.querySelectorAll([
    '[aria-live]:not([aria-live="off"])',
    '[role="status"]',
    '[role="log"]',
  ].join(", ")));
  return status?.getAttribute("aria-live") === "polite"
    && status.getAttribute("aria-atomic") === "true"
    && status.textContent.trim() !== ""
    && liveOwners.size === 1
    && liveOwners.has(status)
    && log?.tagName === "DIV"
    // Not focusable: NVDA read a focused log whole, in one utterance.
    && !log.hasAttribute("tabindex")
    && !log.hasAttribute("aria-live")
    // Named region (a bare <div> cannot carry a name), never a live role.
    && log.getAttribute("role") === "region"
    && log.getAttribute("aria-labelledby") === "library-log-heading";
}

function functionBody(source, name) {
  const signature = new RegExp(`function\\s+${name}\\s*\\([^)]*\\)\\s*\\{`, "g");
  const match = signature.exec(source);
  if (!match) throw new Error(`Missing function ${name}`);
  const open = source.indexOf("{", match.index);
  const close = findClosingBrace(source, open);
  return source.slice(open + 1, close);
}

describe("static accessibility contracts", () => {
  it("keeps track editing in its own tab with a named Save or Discard dialog", () => {
    installDocument();
    const panel = document.querySelector("#panel-track-info");
    expect(panel.getAttribute("aria-labelledby")).toBe("tab-track-info");
    expect(panel.contains(document.querySelector("#points-editor"))).toBe(true);
    expect(document.querySelector("#panel-now #points-editor")).toBeNull();
    expect(document.querySelector("#track-info-details").hasAttribute("aria-live")).toBe(false);
    const dialog = document.querySelector("#track-info-dialog");
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog.getAttribute("aria-labelledby")).toBe("track-info-dialog-title");
    expect([...dialog.querySelectorAll("button")].map(button => button.textContent)).toEqual(["Save", "Discard", "Keep editing"]);
    expect([...dialog.querySelectorAll("button")].every((button) => button.getAttribute("type") === "button")).toBe(true);
    expect(dialog.querySelector("#track-info-error").getAttribute("role")).toBe("status");
    // The explanation is read when the dialog opens, after its name.
    const description = document.getElementById(dialog.getAttribute("aria-describedby"));
    expect(description.tagName).toBe("P");
    expect(description.textContent).toBe("Save your changes before choosing another track, or discard them to switch.");
    expect(dialog.contains(description)).toBe(true);
    // Keep editing takes the shared button size (44 px, above the 24 px floor)
    // and no colour of its own.
    const css = stripCssComments(cssSource);
    expect(css).not.toMatch(/#track-info-keep/);
    expect(css.match(/(^|[\s,}])button\s*\{[^{}]*min-height:\s*44px/)).not.toBeNull();
    // Three buttons wrap rather than run off a narrow window.
    expect(lastValue("#track-info-dialog .dialog-actions", "flex-wrap")).toBe("wrap");
    expect(lastValue("#track-info-dialog", "max-width")).toBe("min(32rem, calc(100vw - 2rem))");
  });
  it("keeps connected status text visible without relying on color alone", () => {
    const background = lastValue("#conn-status.connected", "background");
    const color = lastValue("#conn-status.connected", "color");
    expect(background).toBe("#4ade80");
    expect(color).toBe("#081f12");
    expect(contrast(background, color)).toBeGreaterThanOrEqual(4.5);

    expect(connectedStatusIsVisible()).toBe(true);
    expect(connectedStatusIsVisible({
      html: htmlSource.replace(
        'id="conn-status"',
        'id="conn-status" class="visually-hidden"',
      ),
    })).toBe(false);
    expect(connectedStatusIsVisible({
      html: htmlSource.replace(
        '<header role="banner">',
        '<header role="banner" style="display:none">',
      ),
    })).toBe(false);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected { color: transparent; }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected { background: transparent; }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; width: 1px; height: 1px;
        overflow: hidden; clip: rect(0, 0, 0, 0);
      }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; left: -9999px;
      }`,
    })).toBe(false);
    for (const offset of ["right", "top", "bottom"]) {
      expect(connectedStatusIsVisible({
        extraCss: `#conn-status.connected {
          position: fixed; ${offset}: -9999px;
        }`,
      })).toBe(false);
    }
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        transform: translateX(-9999px);
      }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: relative; left: -1px;
      }`,
    })).toBe(true);
    for (const [offset, sizeProperty] of [
      ["left", "width"], ["right", "width"],
      ["top", "height"], ["bottom", "height"],
    ]) {
      expect(connectedStatusIsVisible({
        extraCss: `#conn-status.connected {
          position: absolute; ${offset}: -100px; ${sizeProperty}: 20px;
        }`,
      }), `${offset} fully outside`).toBe(false);
      expect(connectedStatusIsVisible({
        extraCss: `#conn-status.connected {
          position: absolute; ${offset}: -10px; ${sizeProperty}: 20px;
        }`,
      }), `${offset} partially visible`).toBe(true);
    }
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; left: 0; width: 20px; transform: translateX(-100px);
      }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; left: 0; width: 20px; transform: translateX(-10px);
      }`,
    })).toBe(true);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; left: -2000px; width: 20px;
        transform: translateX(2000px);
      }`,
    })).toBe(true);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; left: -2010px; width: 20px;
        transform: translateX(2000px);
      }`,
    })).toBe(true);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; left: -2000px; width: 20px; top: -2000px;
        transform: translateX(2000px);
      }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; left: -2000px; width: 20px;
        transform: translate(2000px, -2000px);
      }`,
    })).toBe(false);
    for (const offset of ["left", "top"]) {
      expect(connectedStatusIsVisible({
        extraCss: `#conn-status.connected {
          position: absolute; ${offset}: 9999px;
        }`,
      })).toBe(false);
    }

    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    for (const [offset, distance] of [
      ["left", viewportWidth + 1],
      ["right", viewportWidth + 1],
      ["top", viewportHeight + 1],
      ["bottom", viewportHeight + 1],
    ]) {
      for (const direction of [-1, 1]) {
        expect(connectedStatusIsVisible({
          extraCss: `#conn-status.connected {
            position: absolute; ${offset}: ${direction * distance}px;
          }`,
        }), `${offset}:${direction}`).toBe(false);
      }
    }
    const outsideRects = [
      { left: -20, right: 0, top: 20, bottom: 40, width: 20, height: 20 },
      {
        left: viewportWidth,
        right: viewportWidth + 20,
        top: 20,
        bottom: 40,
        width: 20,
        height: 20,
      },
      { left: 20, right: 40, top: -20, bottom: 0, width: 20, height: 20 },
      {
        left: 20,
        right: 40,
        top: viewportHeight,
        bottom: viewportHeight + 20,
        width: 20,
        height: 20,
      },
    ];
    for (const statusRect of outsideRects) {
      expect(connectedStatusIsVisible({ statusRect })).toBe(false);
    }
    const partiallyVisibleRects = [
      { left: -10, right: 10, top: 20, bottom: 40, width: 20, height: 20 },
      {
        left: viewportWidth - 10,
        right: viewportWidth + 10,
        top: 20,
        bottom: 40,
        width: 20,
        height: 20,
      },
      { left: 20, right: 40, top: -10, bottom: 10, width: 20, height: 20 },
      {
        left: 20,
        right: 40,
        top: viewportHeight - 10,
        bottom: viewportHeight + 10,
        width: 20,
        height: 20,
      },
    ];
    for (const statusRect of partiallyVisibleRects) {
      expect(connectedStatusIsVisible({ statusRect })).toBe(true);
    }
    expect(connectedStatusIsVisible({
      extraCss: `#conn-status.connected {
        position: absolute; left: -9999px;
      }`,
      statusRect: partiallyVisibleRects[0],
    })).toBe(true);
    expect(connectedStatusIsVisible({
      html: htmlSource.replace(
        'id="conn-status"',
        'id="conn-status" style="display:none"',
      ),
    })).toBe(false);
    expect(connectedStatusIsVisible({
      html: htmlSource.replace(
        'id="conn-status"',
        'id="conn-status" aria-hidden="true"',
      ),
    })).toBe(false);

    installDocument();
    const status = document.querySelector("#conn-status");
    expect(status).not.toBeNull();
    expect(status.hidden).toBe(false);
    expect(status.closest("[hidden]")).toBeNull();
    expect(status.textContent.trim()).not.toBe("");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");

    const statusWriter = functionBody(appSource, "setConnStatus");
    expect(statusWriter).toMatch(/connStatus\.textContent\s*=\s*label\s*;/);
    expect(appSource).toMatch(/setConnStatus\("connected",\s*"[^"\r\n]+"\)/);
  });

  it("gives controls real high-contrast boundaries and retains focus", () => {
    expect(normalizedColor("#abc")).toBe("#aabbcc");
    expect(normalizedColor("#abcf")).toBe("#aabbcc");
    expect(normalizedColor("#aabbcc")).toBe("#aabbcc");
    expect(normalizedColor("#aabbccff")).toBe("#aabbcc");
    expect(normalizedColor("rgba(170, 187, 204, 1)")).toBe("#aabbcc");
    expect(normalizedColor("rgba(255, 255, 255, 0.5)", "#000"))
      .toBe("#808080");
    const expectedBoundary = "1px solid var(--control-border)";
    for (const selector of [
      "button",
      "#progress-track",
      'input:not([type="checkbox"]):not([type="radio"])',
      "#auth-token",
      "#auth-device-name",
      "#search-input",
      "select",
      "textarea",
      '#view-nav [role="tab"]',
    ]) {
      expect(lastValue(selector, "border"), selector).toBe(expectedBoundary);
    }
    for (const theme of THEMES) {
      for (const surface of ["--bg-card", "--bg"]) {
        expect(contrast(token(theme, "--control-border"), token(theme, surface)),
          `${theme} boundary on ${surface}`).toBeGreaterThanOrEqual(3);
        expect(contrast(token(theme, "--accent"), token(theme, surface)),
          `${theme} focus ring on ${surface}`).toBeGreaterThanOrEqual(3);
      }
    }
    expect(lastValue(":focus-visible", "outline")).toBe(
      "2px solid var(--accent)",
    );
  });

  it.each(THEMES)("draws a solid high-contrast boundary on every control, %s theme", (theme) => {
    expect(boundariesMeetContract({ theme })).toBe(true);
  });

  it("leaves checkboxes and radio buttons native", () => {
    expect(nativeChoiceControlsRemainNative()).toBe(true);
  });

  it.each(THEMES)("draws a high-contrast focus outline on every control, %s theme", (theme) => {
    expect(focusOutlinesMeetContract({ theme })).toBe(true);
  });

  it.each([
    ["checkbox appearance", 'input[type="checkbox"] { appearance: none; }', false],
    ["radio border", 'input[type="radio"] { border: 1px solid #8aa4d6; }', false],
    ["checkbox border-left", 'input[type="checkbox"] { border-left: 1px solid #8aa4d6; }', false],
    ["checked checkbox", 'input[type="checkbox"]:checked { appearance: none; }', false],
    ["checked radio", 'input[type="radio"]:checked { appearance: none; }', false],
    ["small-screen checkbox", '@media (max-width: 600px) { input[type="checkbox"]:checked { appearance: none; } }', false],
    ["large-screen radio", '@media (min-width: 1000px) { input[type="radio"]:not(:checked) { border-left: 1px solid #8aa4d6; } }', false],
    ["nested reduced motion", '@media (max-width: 600px) { @media (prefers-reduced-motion: reduce) { input[type="checkbox"]:not(:checked) { appearance: none; } } }', false],
    ["forced-colors none", '@media (forced-colors: none) and (min-width: 1000px) { input[type="radio"]:checked { appearance: none; } }', false],
    ["print checkbox", '@media print { input[type="checkbox"]:checked { appearance: none; } }', true],
    ["bounded screen width", '@media (min-width: 800px) and (max-width: 900px) { input[type="radio"]:checked { appearance: none; } }', false],
  ])("keeps native choice controls for %s", (_label, rule, expected) => {
    expect(nativeChoiceControlsRemainNative(rule)).toBe(expected);
  });

  // Each theme's own colour, written with an alpha channel: opaque passes,
  // fully transparent fails.  The dark theme's colour in the light theme
  // fails as well: it is not the light theme's colour, and it is under 3:1
  // on the light card.
  const themeCases = (name, selector, property) => THEMES.flatMap((theme) => {
    const hex = token(theme, name);
    return [
      [theme, "opaque", `${selector} { ${property}: ${hex}ff; }`, true],
      [theme, "transparent", `${selector} { ${property}: ${hex}00; }`, false],
    ];
  });

  it.each([
    ...themeCases("--control-border", "#btn-skip", "border-color"),
    ["light", "dark-theme", "#btn-skip { border-color: #8aa4d6; }", false],
  ])("checks the %s theme's %s border colour", (theme, _label, rule, expected) => {
    expect(boundariesMeetContract({ extraCss: rule, only: "#btn-skip", theme })).toBe(expected);
  });

  it.each(THEMES.flatMap((theme) => [
    ["button", "#btn-skip"],
    ["range", "#eq-mid"],
    ["number", "#bpm-hi"],
    ["file", "#ln-upload"],
    ["select", "#transition-select"],
    ["textarea", "#a11y-textarea-fixture"],
    ["tab", "#view-nav #tab-queue"],
  ].map((row) => [theme, ...row])))("rejects, in the %s theme, missing boundary on %s", (theme, _label, selector) => {
    expect(boundariesMeetContract({ extraCss: `${selector} { border: none; }`, only: selector, theme }))
      .toBe(false);
  });

  it.each([
    ...themeCases("--accent", "#btn-skip:focus-visible", "outline-color"),
    ["light", "dark-theme", "#btn-skip:focus-visible { outline-color: #7eb3e8; }", false],
  ])("checks the %s theme's %s outline colour", (theme, _label, rule, expected) => {
    expect(focusOutlinesMeetContract({ extraCss: rule, only: "#btn-skip", theme })).toBe(expected);
  });

  it.each(THEMES.flatMap((theme) => [
    ["button", "#btn-skip"],
    ["tab", "#tab-queue"],
    ["auth token", "#auth-token"],
    ["auth device", "#auth-device-name"],
    ["transition", "#transition-select"],
    ["textarea", "#a11y-textarea-fixture"],
    ["equalizer", "#eq-mid"],
    ["phrase alignment", "#dj-phrase-align"],
  ].map((row) => [theme, ...row])))("rejects, in the %s theme, missing focus outline on %s", (theme, _label, selector) => {
    expect(focusOutlinesMeetContract({
      extraCss: `${selector}:focus-visible { outline: none; }`,
      only: selector,
      theme,
    })).toBe(false);
  });

  it("honors the complete reduced-motion preference", () => {
    const media = "(prefers-reduced-motion: reduce)";
    expect(lastValue("html", "scroll-behavior", media)).toBe("auto");
    for (const selector of ["*", "*::before", "*::after"]) {
      expect(lastValue(selector, "animation-duration", media), selector)
        .toBe("0.01ms !important");
      expect(lastValue(selector, "animation-iteration-count", media), selector)
        .toBe("1 !important");
      expect(lastValue(selector, "scroll-behavior", media), selector)
        .toBe("auto !important");
      expect(lastValue(selector, "transition-duration", media), selector)
        .toBe("0.01ms !important");
    }
    const nestedRules = parseCss(`
      @media (max-width: 1px) {
        @media (prefers-reduced-motion: reduce) {
          html { scroll-behavior: auto; }
        }
      }
    `);
    expect(lastValue("html", "scroll-behavior", media, nestedRules))
      .toBeUndefined();
    expect(reducedMotionMeetsContract()).toBe(true);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (prefers-reduced-motion: reduce) {
        body * { animation-duration: 1s !important; }
      }
    `))).toBe(false);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (prefers-reduced-motion: reduce) {
        body * { transition-duration: 1s; }
      }
    `))).toBe(true);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (prefers-reduced-motion: reduce) {
        * { scroll-behavior: smooth !important; }
      }
    `))).toBe(false);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      body * { animation-duration: 1s !important; }
    `))).toBe(false);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (min-width: 1px) {
        body * { animation-duration: 1s !important; }
      }
    `))).toBe(false);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (min-width: 1px) {
        @media (prefers-reduced-motion: reduce) {
          body * { animation-duration: 1s !important; }
        }
      }
    `))).toBe(false);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (prefers-reduced-motion: reduce) and (min-width: 1px) {
        body * { animation-duration: 1s !important; }
      }
    `))).toBe(false);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (prefers-reduced-motion: reduce)
        and (min-width: 800px) and (max-width: 900px)
        and (min-height: 500px) and (max-height: 600px) {
        body * { animation-duration: 1s !important; }
      }
    `))).toBe(false);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (prefers-reduced-motion: reduce) and (max-width: 600px) {
        body * { animation-duration: 1s !important; }
      }
    `))).toBe(false);
    expect(reducedMotionMeetsContract(parseCss(`${cssSource}\n
      @media (prefers-reduced-motion: reduce) and (forced-colors: active) {
        body * { animation-duration: 1s !important; }
      }
    `))).toBe(false);
  });

  it("uses system colors for forced-color controls and custom progress", () => {
    const media = "(forced-colors: active)";
    for (const selector of [
      "button", "#progress-track", "input", "select", "textarea",
      '#view-nav [role="tab"]',
    ]) {
      expect(lastValue(selector, "border-color", media), selector)
        .toBe("CanvasText");
    }
    expect(lastValue("#progress-track::before", "background", media))
      .toBe("CanvasText");
    expect(lastValue("#progress-fill", "background", media)).toBe("Highlight");
    // Selected tab and pressed toggles must stay distinguishable once the
    // author fills are forced away.
    for (const selector of [
      '#view-nav [role="tab"][aria-selected="true"]',
      'button[aria-pressed="true"]',
    ]) {
      expect(lastValue(selector, "background", media), selector).toBe("Highlight");
      expect(lastValue(selector, "color", media), selector).toBe("HighlightText");
      expect(lastValue(selector, "border", media), selector).toBe("3px solid CanvasText");
    }
    expect(lastValue(":focus-visible", "outline-color", media)).toBe("Highlight");
    expect(lastValue(":focus-visible", "outline", media)).not.toMatch(
      /\b(?:none|0)\b/,
    );
    const nestedRules = parseCss(`
      @media (max-width: 1px) {
        @media (forced-colors: active) {
          :focus-visible { outline-color: Highlight; }
        }
      }
    `);
    expect(lastValue(":focus-visible", "outline-color", media, nestedRules))
      .toBeUndefined();
    expect(forcedColorsMeetsContract()).toBe(true);
    expect(forcedColorsMeetsContract(parseCss(`
      @media (forced-colors: active) {
        #progress-fill { background: CanvasText !important; }
      }
      ${cssSource}
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) {
        #view-nav #tab-queue { border-color: Canvas; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) {
        #progress-fill { background: CanvasText; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) {
        #btn-skip:focus-visible { outline: none; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) {
        #btn-skip { border: none; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) {
        #view-nav #tab-queue { border: 1px solid Canvas; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) {
        #btn-skip:focus-visible { outline: 2px solid CanvasText; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      #progress-fill { background: CanvasText !important; }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (min-width: 1px) {
        #progress-fill { background: CanvasText; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (min-width: 1px) {
        @media (forced-colors: active) {
          #progress-fill { background: CanvasText; }
        }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) and (min-width: 1px) {
        #progress-fill { background: CanvasText; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active)
        and (min-width: 800px) and (max-width: 900px) {
        #progress-fill { background: CanvasText; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) {
        body #progress-fill { background-color: CanvasText; }
      }
    `))).toBe(false);
    expect(forcedColorsMeetsContract(parseCss(`${cssSource}\n
      @media (forced-colors: active) and (prefers-reduced-motion: reduce) {
        body #progress-fill { background-color: CanvasText; }
      }
    `))).toBe(false);
  });

  it("keeps selected and pressed states readable without colour", () => {
    const forced = "(forced-colors: active)";
    // Chromium's forced-colours backplate is Canvas: HighlightText on it
    // was invisible, so the state rule opts out of forcing.
    for (const selector of [
      '#view-nav [role="tab"][aria-selected="true"]',
      '.subtabs [role="tab"][aria-selected="true"]',
      'button[aria-pressed="true"]',
    ]) {
      expect(lastValue(selector, "forced-color-adjust", forced), selector).toBe("none");
    }
    // Pressed differs from not pressed by a tick as well as by colour
    // (WCAG 1.4.1), and the tick has empty alt text so it is not spoken.
    expect(lastValue('button[aria-pressed="true"]::before', "content"))
      .toBe('"\\2713" / ""');
    // A transition on the background alone left a tab just left with
    // light text on the fading light fill.
    expect(lastValue("button", "transition")).toBeUndefined();
    // The cue ticks differ by hue only, so their text is shown with the key.
    expect(lastValue(
      "#cue-legend:not([hidden]) ~ #cue-details.visually-hidden", "clip",
    )).toBe("auto");
    // The phone layout's slider track meets 3:1 against the card.
    for (const theme of THEMES) {
      expect(contrast(token(theme, "--range-track"), token(theme, "--bg-card")), theme)
        .toBeGreaterThanOrEqual(3);
    }
    // Compatible Camelot wedges carry an accent edge, not just a 1.8:1 fill.
    expect(lastValue("#camelot-wheel .sector.compat", "stroke")).toBe("var(--accent)");
  });

  // WCAG 2.2 AA in the light and the dark theme alike: text 4.5:1,
  // boundaries, focus rings, meters, ticks and wheel edges 3:1.  A pair is
  // [foreground, background, minimum]; "a over b" lays a translucent token
  // on an opaque one, as the page does.
  it.each(THEMES)("meets WCAG 2.2 AA contrast with every %s theme token", (theme) => {
    const layered = (spec) => colorToHex(spec.split(" over ").reverse()
      .map((part) => parseCssColor(part.startsWith("--") ? token(theme, part) : part))
      .reduce((below, above) => (below ? compositeColor(above, below) : above), null));
    const text = [
      ["--text", "--bg"], ["--text", "--bg-card"],
      ["--text-dim", "--bg"], ["--text-dim", "--bg-card"],
      ["--text-desc", "--bg"], ["--text-desc", "--bg-card"],
      ["--accent", "--bg-card"], ["--accent", "--bg"],
      ["--on-accent", "--accent"],
      ["--btn-text", "--btn-bg"], ["--btn-text", "--btn-hover"],
      ["--tab-hover-text", "--tab-hover-bg"],
      ["--error-text", "--bg-card"], ["--error-text", "--bg"],
      ["--log-text", "--log-bg"], ["--text-dim", "--log-bg"],
      ["--text", "--toast-error-bg"],
      ["--stretch", "--tint-3 over --bg-card"],
      ["--text-dim", "--tint-3 over --bg-card"],
      ["--accent", "--tint-3 over --bg-card"],
      ["--text-dim", "--border"],
      ["--text", "--tint-2 over --bg-card"],
      ["--text-dim", "--tint over --bg-card"],
      ["--text", "--notice-bg over --bg-card"],
      ["--text", "--wheel-fill over --bg-card"],
      ["--text", "--wheel-compat over --bg-card"],
      ["#ffffff", "--danger"],
    ];
    const nonText = [
      ["--control-border", "--bg-card"], ["--control-border", "--bg"],
      ["--accent", "--bg-card"], ["--accent", "--bg"],
      ["--badge-border", "--bg-card"], ["--range-track", "--bg-card"],
      ["--danger-edge", "--bg-card"], ["--danger-edge", "--toast-error-bg"],
      ["--accent", "--border"],
      ["--wheel-edge", "--wheel-fill over --bg-card"],
      ["--accent", "--wheel-compat over --bg-card"],
      ["--accent", "--wheel-fill over --bg-card"],
      ["--text", "--wheel-compat over --bg-card"],
      ...["--cue-drop", "--cue-breakdown", "--cue-downbeat", "--cue-outro",
        "--cue-user"].map((cue) => [cue, "--bg-card"]),
      ["--cue-phrase over --bg-card", "--bg-card"],
    ];
    for (const [pairs, minimum] of [[text, 4.5], [nonText, 3]]) {
      for (const [foreground, background] of pairs) {
        const measured = contrast(layered(foreground), layered(background));
        expect(measured, `${theme}: ${foreground} on ${background}`)
          .toBeGreaterThanOrEqual(minimum);
      }
    }
  });

  it("offers System, Light and Dark, System first and checked", () => {
    installDocument();
    const group = document.querySelector("#spanel-display fieldset#theme-choice");
    expect(group).not.toBeNull();
    expect(group.querySelector("legend").textContent.trim()).toBe("Theme");
    const radios = [...group.querySelectorAll('input[type="radio"][name="theme"]')];
    expect(radios.map((radio) => radio.value)).toEqual(["system", "light", "dark"]);
    expect(radios.map((radio) => document.querySelector(`label[for="${radio.id}"]`)
      .textContent.trim())).toEqual(["System", "Light", "Dark"]);
    expect(radios.filter((radio) => radio.hasAttribute("checked"))
      .map((radio) => radio.value)).toEqual(["system"]);
    const description = document.getElementById(group.getAttribute("aria-describedby"));
    expect(description.textContent).toMatch(/System follows/);
    // The stored theme is applied by a blocking classic script ahead of
    // the stylesheet, so nothing is painted in the other theme first.
    const head = htmlSource.slice(0, htmlSource.indexOf("</head>"));
    const script = head.indexOf('<script src="/modules/theme-init.js"></script>');
    expect(script).toBeGreaterThan(-1);
    expect(script).toBeLessThan(head.indexOf('href="/app.css"'));
    expect(htmlSource).not.toMatch(/<html[^>]*data-theme/);
    // System is the root's "light dark"; a choice pins one scheme.
    expect(lastValue(":root", "color-scheme")).toBe("light dark");
    expect(lastValue(':root[data-theme="light"]', "color-scheme")).toBe("light");
    expect(lastValue(':root[data-theme="dark"]', "color-scheme")).toBe("dark");
  });

  it("gives the status message a Dismiss button that is never announced with it", () => {
    installDocument();
    const toast = document.querySelector("#status-toast");
    const box = document.querySelector("#status-toast-box");
    const dismiss = document.querySelector("#status-toast-dismiss");
    expect(toast.parentElement).toBe(box);
    expect(dismiss.parentElement).toBe(box);
    expect(toast.contains(dismiss)).toBe(false);
    expect(dismiss.getAttribute("type")).toBe("button");
    expect(dismiss.textContent.replace(/\s+/g, " ").trim()).toBe("Dismiss message");
    expect(dismiss.querySelector(".visually-hidden").textContent.trim()).toBe("message");
    expect(dismiss.hasAttribute("aria-label")).toBe(false);
    // Reachable, and outside every live region and aria-hidden subtree.
    for (let node = dismiss; node; node = node.parentElement) {
      expect(node.getAttribute("aria-hidden"), node.id).not.toBe("true");
      expect(node.hasAttribute("aria-live"), node.id).toBe(false);
      expect(["status", "alert", "log"]).not.toContain(node.getAttribute("role"));
    }
    expect(box.hasAttribute("hidden")).toBe(true);
    // 24 px minimum target (WCAG 2.5.8).
    expect(lastValue("#status-toast-dismiss", "min-width")).toBe("2rem");
    expect(lastValue("#status-toast-dismiss", "min-height")).toBe("2rem");
    // Only the message and the button take the pointer (hover holds the
    // toast, WCAG 1.4.13); the frame round them passes clicks through to
    // the page, so it never blocks the controls underneath.
    expect(lastValue("#status-toast-box", "pointer-events")).toBe("none");
    expect(lastValue("#status-toast", "pointer-events")).toBe("auto");
    expect(lastValue("#status-toast-dismiss", "pointer-events")).toBe("auto");
  });

  it("draws the Camelot wheel in system colours under forced colours", () => {
    const forced = "(forced-colors: active)";
    expect(lastValue("#camelot-wheel", "forced-color-adjust", forced)).toBe("none");
    expect(lastValue("#camelot-wheel .sector", "stroke", forced)).toBe("CanvasText");
    expect(lastValue("#camelot-wheel .sector.compat", "stroke", forced)).toBe("Highlight");
    expect(lastValue("#camelot-wheel .sector.active", "fill", forced)).toBe("Highlight");
    expect(lastValue("#camelot-wheel .label.active", "fill", forced)).toBe("HighlightText");
    expect(lastValue("#cue-strip .cue-mark", "box-shadow", forced))
      .toBe("0 0 0 1px CanvasText");
  });

  it("keeps targets, fields and text inside the column", () => {
    // WCAG 2.5.8: every checkbox and radio button is 24 x 24 px.
    for (const selector of ['input[type="checkbox"]', 'input[type="radio"]']) {
      expect(lastValue(selector, "width"), selector).toBe("1.5rem");
      expect(lastValue(selector, "height"), selector).toBe("1.5rem");
    }
    // WCAG 1.4.10 / 1.4.4: no field or fieldset outgrows its column, long
    // paths break, and the column widens with the reader's text size.
    for (const selector of ["input", "select", "textarea"]) {
      expect(lastValue(selector, "max-width"), selector).toBe("100%");
    }
    expect(lastValue("fieldset", "min-width")).toBe("0");
    expect(lastValue("body", "overflow-wrap")).toBe("break-word");
    expect(lastValue("main", "max-width")).toMatch(/rem$/);
    expect(lastValue(".settings-row", "flex-wrap")).toBe("wrap");
    // Typeless, time and date fields get the same dark 44 px field as text.
    for (const selector of ["input:not([type])", 'input[type="time"]', 'input[type="date"]']) {
      expect(lastValue(selector, "min-height"), selector).toBe("44px");
    }
    // The forms that were bare label / field / button siblings use the
    // source-order form layout.
    installDocument();
    for (const id of [
      "source-folder-path", "source-folder-override-tags", "search-input", "clock-name",
      "clock-step-type", "listener-ban-address", "token-secret",
    ]) {
      expect(document.getElementById(id).closest(".flow-form"), id).not.toBeNull();
    }
    expect(document.getElementById("source-folder-override-tags").checked).toBe(false);
  });

  it("keeps durable playback evidence separate from scoped announcements", () => {
    expect(libraryLogOwnershipIsScoped()).toBe(true);
    expect(libraryLogOwnershipIsScoped(htmlSource.replace(
      ' id="lib-job-status" aria-live="polite"',
      ' id="lib-job-status"',
    ))).toBe(false);
    expect(libraryLogOwnershipIsScoped(htmlSource.replace(
      ' aria-atomic="true">Idle.</p>',
      '>Idle.</p>',
    ))).toBe(false);
    expect(libraryLogOwnershipIsScoped(htmlSource.replace(
      '>Idle.</p>',
      '></p>',
    ))).toBe(false);
    expect(libraryLogOwnershipIsScoped(htmlSource.replace(
      '<div id="library-log"',
      '<div id="library-log" aria-live="polite"',
    ))).toBe(false);
    expect(libraryLogOwnershipIsScoped(htmlSource.replace(
      ' role="region"',
      ' role="log"',
    ))).toBe(false);

    installDocument();
    for (const selector of [
      "#queue-announce", "#settings-status", "#ln-status", "#vol-announce",
      "#sr-status",
    ]) {
      const status = document.querySelector(selector);
      expect(status.getAttribute("role"), selector).toBe("status");
      expect(status.getAttribute("aria-live"), selector).toBe("polite");
      expect(status.getAttribute("aria-atomic"), selector).toBe("true");
    }

    for (const selector of [
      "#status-toast", "#playback-stale-note",
      "#cue-legend", "#camelot-legend", "#cue-summary",
    ]) {
      expect(document.querySelector(selector).getAttribute("aria-hidden"), selector)
        .toBe("true");
    }
    // Silent but readable: the job clock can be reached with the cursor.
    expect(document.querySelector("#lib-job-elapsed").getAttribute("aria-live"))
      .toBe("off");
    expect(document.querySelector("#lib-job-elapsed").hasAttribute("aria-hidden"))
      .toBe(false);

    const progress = document.querySelector("#progress-track");
    const describedBy = progress.getAttribute("aria-describedby").split(/\s+/);
    expect(describedBy).toContain("cue-summary");
    expect(describedBy).not.toContain("cue-details");
    // The concise summary is reachable ONLY as the slider's description --
    // aria-describedby reads through aria-hidden -- so browsing the page no
    // longer meets the summary and the full list one after the other, each
    // naming the same cues.  Exactly one of the pair is browsable.
    expect(document.querySelector("#cue-summary").getAttribute("aria-hidden"))
      .toBe("true");
    expect(document.querySelector("#cue-details").hasAttribute("aria-hidden"))
      .toBe(false);

    for (const selector of [
      "#now-playing-meta", "#cue-summary", "#cue-details", "#queue-list",
      "#library-log", "#version-stamp", "#ver-build",
      // The sighted status mirror and the silent job clock must never
      // become a second announcement path for the same message.
      "#status-toast", "#playback-stale-note", "#cue-legend", "#camelot-legend",
    ]) {
      const node = document.querySelector(selector);
      expect(node, selector).not.toBeNull();
      expect(node.hasAttribute("aria-live"), selector).toBe(false);
    }
    for (const selector of [
      "#now-playing-meta", "#cue-summary", "#cue-details", "#version-stamp", "#ver-build",
      "#status-toast", "#playback-stale-note", "#lib-job-elapsed",
      "#cue-legend", "#camelot-legend",
    ]) {
      expect(document.querySelector(selector).hasAttribute("role"), selector)
        .toBe(false);
    }
  });

  it("gives stream mode a fixed-name toggle and a silent idle note", () => {
    installDocument();
    // Text a screen reader would use for the name: aria-hidden subtrees
    // (the glyph) contribute nothing.
    const spokenText = (node) => {
      if (node.nodeType === 3) return node.textContent;
      if (node.nodeType !== 1 || node.getAttribute("aria-hidden") === "true") return "";
      return [...node.childNodes].map(spokenText).join("");
    };

    const listen = document.querySelector("#btn-listen");
    expect(listen.tagName).toBe("BUTTON");
    expect(listen.getAttribute("type")).toBe("button");
    expect(listen.getAttribute("aria-pressed")).toBe("false");
    expect(listen.hasAttribute("aria-label")).toBe(false);
    expect(spokenText(listen).replace(/\s+/g, " ").trim()).toBe("Listen here");
    expect(listen.querySelector('[aria-hidden="true"]')).not.toBeNull();
    // Browser mode renders no Listen here button until stream mode shows it.
    expect(listen.hidden).toBe(true);
    // Sits in the transport group straight after Play / Pause.
    expect(listen.previousElementSibling.id).toBe("btn-pause");
    expect(listen.closest('[role="group"]').getAttribute("aria-label"))
      .toBe("Playback controls");

    const note = document.querySelector("#stream-idle-note");
    expect(note.hidden).toBe(true);
    expect(note.hasAttribute("role")).toBe(false);
    expect(note.hasAttribute("aria-live")).toBe(false);
    expect(note.closest("[aria-live], [role=status], [role=alert]")).toBeNull();
    expect(note.closest("#now-playing-card")).not.toBeNull();

    const audio = document.querySelector("#stream-audio");
    expect(audio.tagName).toBe("AUDIO");
    expect(audio.hidden).toBe(true);
    expect(audio.hasAttribute("controls")).toBe(false);
    expect(audio.getAttribute("preload")).toBe("none");
  });

  it("gives the Stream settings labelled controls and a silent count", () => {
    installDocument();
    const labelText = (id) => document.querySelector(`label[for="${id}"]`)
      ?.textContent.replace(/\s+/g, " ").trim();
    const describedBy = (element) => (element.getAttribute("aria-describedby") || "")
      .split(/\s+/).filter(Boolean).map((id) => document.getElementById(id));

    // The Stream subtab's panel, under its heading; the fieldset and its
    // tab stay hidden until the server reports stream mode.
    const fieldset = document.querySelector("#stream-settings");
    expect(fieldset.tagName).toBe("FIELDSET");
    expect(fieldset.hidden).toBe(true);
    expect(fieldset.querySelector("legend").textContent.trim()).toBe("Stream");
    expect(fieldset.parentElement.matches("#panel-settings > #spanel-stream")).toBe(true);
    expect(fieldset.previousElementSibling.tagName).toBe("H3");
    expect(fieldset.previousElementSibling.textContent).toBe("Stream");
    expect(document.querySelector("#stab-stream").hidden).toBe(true);
    expect([...fieldset.querySelectorAll("h4")].map((heading) => heading.textContent.trim()))
      .toEqual(["Relays", "Listeners", "Bans"]);

    const name = document.querySelector("#stream-station-name");
    expect(name.getAttribute("type")).toBe("text");
    expect(name.getAttribute("maxlength")).toBe("100");
    expect(labelText("stream-station-name")).toBe("Station name");
    // Its help, then which station it changes (#70).
    expect(describedBy(name).map((element) => element?.id))
      .toEqual(["stream-station-name-desc", "station-context"]);

    const address = document.querySelector("#stream-url");
    expect(address.tagName).toBe("INPUT");
    expect(address.getAttribute("type")).toBe("text");
    expect(address.hasAttribute("readonly")).toBe(true);
    expect(labelText("stream-url")).toBe("Stream address");
    expect(describedBy(address).every(Boolean)).toBe(true);
    expect(describedBy(address)).toHaveLength(1);

    const copy = document.querySelector("#stream-copy");
    expect(copy.tagName).toBe("BUTTON");
    expect(copy.getAttribute("type")).toBe("button");
    expect(copy.textContent.trim()).toBe("Copy address");

    const playlist = document.querySelector("#stream-m3u");
    expect(playlist.tagName).toBe("A");
    expect(playlist.textContent.trim()).toBe("Download playlist file (.m3u)");
    // Downloads rather than navigating away from the remote.
    expect(playlist.hasAttribute("download")).toBe(true);

    const quality = document.querySelector("#stream-bitrate");
    expect(quality.tagName).toBe("SELECT");
    expect(labelText("stream-bitrate")).toBe("Quality");
    expect([...quality.options].map((option) => option.value))
      .toEqual(["128", "192", "256", "320"]);
    expect(describedBy(quality).every(Boolean)).toBe(true);

    // Visible text only: a live count would speak every connect and drop.
    const count = document.querySelector("#stream-listeners");
    expect(count.hasAttribute("aria-live")).toBe(false);
    expect(count.hasAttribute("role")).toBe(false);
    expect(count.closest("[aria-live], [role=status], [role=alert], [role=log]")).toBeNull();
    expect(fieldset.contains(count)).toBe(true);
    expect(count.closest("section").getAttribute("aria-labelledby"))
      .toBe("listener-control-title");
    expect(labelText("stream-per-address")).toContain("Connections per address");
    expect(document.getElementById("listener-ban-new").textContent.trim()).toBe("Add address ban");
    for (const id of ["listener-ban-address", "listener-ban-duration", "listener-ban-note"]) {
      expect(labelText(id)).toBeTruthy();
    }

    // The general settings, the now-playing group and broadcast recording
    // come first; the Listeners and Bans sections close the fieldset, so
    // heading navigation never puts a general setting under "Bans".
    const order = ["#stream-relays", "#stream-station-name", "#stream-bitrate",
      "#stream-continuous", "#np-settings", "#recording-controls", "#listener-control-title",
      "#listener-bans-title"]
      .map((selector) => document.querySelector(selector));
    for (let at = 1; at < order.length; at += 1) {
      expect(order[at - 1].compareDocumentPosition(order[at])
        & document.defaultView.Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    const bans = document.querySelector("#listener-bans-title").closest("section");
    expect(bans.nextElementSibling).toBeNull();
    expect(bans.parentElement).toBe(fieldset);

    // The "not loaded yet" note is visible text, silent like the count.
    const note = document.querySelector("#stream-url-note");
    expect(fieldset.contains(note)).toBe(true);
    expect(note.hidden).toBe(true);
    expect(note.hasAttribute("aria-live")).toBe(false);
    expect(note.hasAttribute("role")).toBe(false);
    expect(note.hasAttribute("aria-hidden")).toBe(false);

  });

  it("groups the now-playing feed in the Stream settings with labelled native controls", () => {
    installDocument();
    const labelText = (id) => document.querySelector(`label[for="${id}"]`)
      ?.textContent.replace(/\s+/g, " ").trim();
    const describedBy = (element) => (element.getAttribute("aria-describedby") || "")
      .split(/\s+/).filter(Boolean).map((id) => document.getElementById(id));

    // Nested in the Stream fieldset, so it hides and hands focus away
    // with it; its own legend names the group.
    const group = document.querySelector("#np-settings");
    expect(group.tagName).toBe("FIELDSET");
    expect(group.parentElement.id).toBe("stream-settings");
    expect(group.querySelector(":scope > legend").textContent.trim()).toBe("Now-playing feed");
    expect(group.querySelector("h1, h2, h3, h4, h5, h6")).toBeNull();

    const link = document.querySelector("#np-url");
    expect(link.getAttribute("type")).toBe("text");
    expect(link.hasAttribute("readonly")).toBe(true);
    expect(labelText("np-url")).toBe("Now-playing link");
    expect(describedBy(link)).toHaveLength(1);
    expect(describedBy(link).every(Boolean)).toBe(true);

    const copy = document.querySelector("#np-copy");
    expect(copy.getAttribute("type")).toBe("button");
    // Distinct from the stream's Copy address in a list of buttons.
    expect(copy.textContent.trim()).toBe("Copy now-playing link");

    const statusLink = document.querySelector("#np-status-url");
    expect(statusLink.hasAttribute("readonly")).toBe(true);
    expect(labelText("np-status-url")).toBe("Status page link");
    expect(describedBy(statusLink).every(Boolean)).toBe(true);
    const statusCopy = document.querySelector("#np-status-copy");
    expect(statusCopy.getAttribute("type")).toBe("button");
    expect(statusCopy.textContent.trim()).toBe("Copy status page link");
    // A link that opens a new tab says so in its own text.
    const open = document.querySelector("#np-status-open");
    expect(open.getAttribute("href")).toBe("/status");
    expect(open.getAttribute("target")).toBe("_blank");
    expect(open.textContent).toContain("(new tab)");

    const showNext = document.querySelector("#np-show-next");
    expect(showNext.getAttribute("type")).toBe("checkbox");
    expect(showNext.hasAttribute("checked")).toBe(false);
    expect(labelText("np-show-next")).toBe("Show the next song publicly");
    expect(describedBy(showNext).every(Boolean)).toBe(true);

    // The list and its notes are plain text; results go to status regions.
    const list = document.querySelector("#np-webhooks-list");
    expect(list.tagName).toBe("UL");
    expect(document.getElementById(list.getAttribute("aria-labelledby")).textContent.trim())
      .toBe("Track-change webhooks");
    for (const id of ["np-webhooks-list", "np-webhooks-empty", "np-loopback-note"]) {
      const element = document.getElementById(id);
      expect(element.hidden).toBe(true);
      expect(element.closest("[aria-live], [role=status], [role=alert], [role=log]")).toBeNull();
    }
  });

  it("groups broadcast recording in the Stream settings with native controls", () => {
    installDocument();
    const labelText = (id) => document.querySelector(`label[for="${id}"]`)
      ?.textContent.replace(/\s+/g, " ").trim();
    const describedBy = (element) => (element.getAttribute("aria-describedby") || "")
      .split(/\s+/).filter(Boolean).map((id) => document.getElementById(id));

    // Nested in the Stream fieldset after the now-playing group, so it hides
    // with it and heading navigation never files it under Listeners or Bans.
    const group = document.querySelector("#recording-controls");
    expect(group.tagName).toBe("FIELDSET");
    expect(group.parentElement.id).toBe("stream-settings");
    expect(group.hidden).toBe(true);
    expect(group.querySelector(":scope > legend").textContent.trim()).toBe("Broadcast recording");
    expect(group.querySelector("h1, h2, h3, h4, h5, h6")).toBeNull();
    expect(group.querySelector("[aria-controls]")).toBeNull();
    expect(group.querySelector("[disabled]")).toBeNull();

    // A toggle with a fixed name; aria-pressed carries the state.
    const toggle = document.querySelector("#recording-toggle");
    expect(toggle.tagName).toBe("BUTTON");
    expect(toggle.getAttribute("type")).toBe("button");
    expect(toggle.textContent.trim()).toBe("Record broadcast");
    expect(toggle.getAttribute("aria-pressed")).toBe("false");
    expect(describedBy(toggle).map((element) => element?.id))
      .toEqual(["recording-status", "recording-error", "station-context"]);

    // Split stays focusable while unavailable, so pressing it is answered.
    const split = document.querySelector("#recording-split-now");
    expect(split.getAttribute("type")).toBe("button");
    expect(split.getAttribute("aria-disabled")).toBe("true");
    expect(split.textContent.trim()).toBe("Split recording now");

    // Status and error are plain text: results go to the status regions.
    for (const id of ["recording-status", "recording-error"]) {
      const element = document.getElementById(id);
      expect(element.tagName).toBe("P");
      expect(element.hasAttribute("role")).toBe(false);
      expect(element.hasAttribute("aria-live")).toBe(false);
      expect(element.closest("[aria-live], [role=status], [role=alert], [role=log]")).toBeNull();
    }

    const mode = document.querySelector("#recording-split");
    expect(mode.tagName).toBe("SELECT");
    expect(labelText("recording-split")).toBe("Start a new file");
    expect([...mode.options].map((option) => option.value)).toEqual(["hour", "show"]);
    expect(describedBy(mode).every(Boolean)).toBe(true);
    for (const [id, label] of [
      ["recording-retention-days", "Days to keep recordings"],
      ["recording-free-space-mb", "Minimum free space in MB"],
    ]) {
      const input = document.getElementById(id);
      expect(labelText(id)).toBe(label);
      expect(input.getAttribute("type")).toBe("number");
      expect(input.getAttribute("min")).toBe("0");
      expect(input.getAttribute("step")).toBe("1");
      expect(input.hasAttribute("required")).toBe(true);
      expect(describedBy(input)).toHaveLength(1);
      expect(describedBy(input).every(Boolean)).toBe(true);
    }
    const save = document.querySelector("#recording-save");
    expect(save.getAttribute("type")).toBe("button");
    expect(save.textContent.trim()).toBe("Save recording settings");
    // Nothing on the page names or edits the recordings folder.
    expect(group.querySelector("input[type=text], input[type=file]")).toBeNull();
  });

  it("keeps the shared confirmation dialog accessible", () => {
    installDocument();
    const dialog = document.querySelector("#confirm-dialog");
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog.closest("main, section, details, [hidden]")).toBeNull();
    const title = document.getElementById(dialog.getAttribute("aria-labelledby"));
    expect(title.tagName).toBe("H2");
    expect(dialog.contains(title)).toBe(true);
    const message = document.getElementById(dialog.getAttribute("aria-describedby"));
    expect(dialog.contains(message)).toBe(true);
    expect(dialog.hasAttribute("role")).toBe(false);
    expect(dialog.hasAttribute("closedby")).toBe(false);
    const form = dialog.querySelector("form");
    expect(form.getAttribute("method")).toBe("dialog");
    const buttons = [...form.querySelectorAll("button")];
    expect(buttons.map((button) => [button.id, button.getAttribute("type"), button.value]))
      .toEqual([
        ["confirm-cancel", "submit", "cancel"],
        ["confirm-accept", "submit", "confirm"],
      ]);
    expect(buttons[0].textContent.trim()).toBe("Cancel");
  });

  it("never nests a dialog inside a tab panel", () => {
    // A <dialog> inside a `[role="tabpanel"]` that is `hidden` on every
    // other tab is not rendered and is invisible to the accessibility
    // tree, yet dialog.showModal() still makes the rest of the page
    // inert -- a screen-reader user opening it from another tab is left
    // with nothing to read.  Every dialog must live outside all panels.
    installDocument();
    const dialogs = [...document.querySelectorAll("dialog")];
    expect(dialogs.length).toBeGreaterThan(0);
    for (const dialog of dialogs) {
      expect(dialog.closest('[role="tabpanel"]'), dialog.id).toBeNull();
    }

    const regressed = htmlSource.replace(
      '<section data-view="history" id="panel-history" role="tabpanel" hidden aria-labelledby="tab-history">',
      '<section data-view="history" id="panel-history" role="tabpanel" hidden aria-labelledby="tab-history">'
        + '<dialog id="a11y-regression-modal"></dialog>',
    );
    installDocument({ html: regressed });
    const regressedDialog = document.querySelector("#a11y-regression-modal");
    expect(regressedDialog).not.toBeNull();
    expect(regressedDialog.closest('[role="tabpanel"]')).not.toBeNull();
  });

  it("writes option text in words, with no aria-label to lean on", () => {
    // NVDA drops these symbols below its "some" punctuation level, so "±1"
    // was heard as "1" and "major/minor" as "major minor".  An aria-label
    // in words was not enough: Firefox's open drop-down list reads the
    // option text, not its aria-label.
    installDocument({ html: htmlSource });
    const options = [...document.querySelectorAll("option")];
    expect(options.length).toBeGreaterThan(0);
    for (const option of options) {
      expect(option.textContent, option.value).toMatch(/^[^±+×/#→]+$/);
      expect(option.hasAttribute("aria-label"), option.value).toBe(false);
    }
  });

  it("gives every key in the shortcuts dialog a spoken name", () => {
    // "↑", "," and "." are silent in NVDA at low punctuation levels, which
    // left the Volume and Seek rows with no key at all.
    installDocument({ html: htmlSource });
    for (const kbd of document.querySelectorAll("#hotkey-help-modal kbd")) {
      const spoken = [...kbd.querySelectorAll("*")]
        .filter((el) => el.closest('[aria-hidden="true"]'))
        .reduce((text, el) => text.replace(el.textContent, ""), kbd.textContent);
      expect(spoken, kbd.textContent).toMatch(/[A-Za-z]/);
    }
  });

  it("gives liner and cart play dates native labelled date inputs and visible error text", () => {
    installDocument({ html: htmlSource });
    const fields = [
      ["ln-not-before", "Not before", "ln-dates-desc"],
      ["ln-not-after", "Do not play after", "ln-dates-desc"],
      ["cart-not-before", "Not before", "cart-dates-desc"],
      ["cart-not-after", "Do not play after", "cart-dates-desc"],
    ];
    for (const [id, label, desc] of fields) {
      const input = document.getElementById(id);
      expect(input.getAttribute("type")).toBe("date");
      expect(input.hasAttribute("required")).toBe(false);
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim()).toBe(label);
      expect(input.getAttribute("aria-describedby")).toBe(desc);
      expect(document.getElementById(desc).textContent).toMatch(/Optional|optional/);
    }
    // Errors are words on the page, in an element a field can be described by.
    for (const id of ["ln-dates-error", "cart-edit-error"]) {
      const error = document.getElementById(id);
      expect(error.classList.contains("field-error")).toBe(true);
      expect(error.hidden).toBe(true);
    }
    const form = document.getElementById("ln-dates-form");
    expect(form.hasAttribute("novalidate")).toBe(true);
    expect(form.querySelector("fieldset > legend").id).toBe("ln-dates-legend");
    // Date inputs share the 44 px native control height (2.5.8).
    const rule = stripCssComments(cssSource).match(/([^{}]*)\{[^{}]*min-height:\s*44px[^{}]*\}/g)
      .find((block) => block.includes('input[type="text"]') && block.includes("padding"));
    expect(rule).toContain('input[type="date"]');
  });

  it("gives category dayparting labelled native day and time controls", () => {
    installDocument({ html: htmlSource });
    const group = document.querySelector("#categories-daypart");
    expect(group.tagName).toBe("FIELDSET");
    expect(group.querySelector(":scope > legend").textContent.trim()).toBe("When the songs may play");
    const help = document.getElementById(group.getAttribute("aria-describedby"));
    expect(help.textContent).toMatch(/^\s*Optional\./);
    expect(help.hidden).toBe(false);
    const days = group.querySelector("#categories-days");
    expect(days.querySelector(":scope > legend").textContent).toBe("Days");
    const boxes = [...days.querySelectorAll('input[type="checkbox"]')];
    expect(boxes.map((box) => document.querySelector(`label[for="${box.id}"]`).textContent))
      .toEqual(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]);
    for (const [id, label] of [["categories-hours-from", "Allowed from"], ["categories-hours-until", "Allowed until"]]) {
      const input = document.getElementById(id);
      expect(input.getAttribute("type")).toBe("time");
      expect(input.hasAttribute("required")).toBe(false);
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim()).toBe(label);
    }
    // Time inputs share the 44 px native control height (2.5.8).
    const rule = stripCssComments(cssSource).match(/([^{}]*)\{[^{}]*min-height:\s*44px[^{}]*\}/g)
      .find((block) => block.includes('input[type="text"]') && block.includes("padding"));
    expect(rule).toContain('input[type="time"]');
  });

  it("gives a category season labelled native date inputs inside its dayparting fieldset", () => {
    installDocument({ html: htmlSource });
    const group = document.querySelector("#categories-daypart");
    for (const [id, label] of [["categories-not-before", "Not before"], ["categories-not-after", "Do not play after"]]) {
      const input = document.getElementById(id);
      expect(input.closest("#categories-daypart")).toBe(group);
      expect(input.getAttribute("type")).toBe("date");
      expect(input.hasAttribute("required")).toBe(false);
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim()).toBe(label);
    }
    expect(document.getElementById(group.getAttribute("aria-describedby")).textContent).toContain("dates");
  });

  it("gives a clockwheel step labelled native day and time controls and an error to link", () => {
    installDocument({ html: htmlSource });
    const group = document.querySelector("#clock-step-hours");
    expect(group.tagName).toBe("FIELDSET");
    expect(group.querySelector(":scope > legend").textContent.trim()).toBe("When this step plays");
    expect(document.getElementById(group.getAttribute("aria-describedby")).textContent).toMatch(/^\s*Optional\./);
    const days = group.querySelector("#clock-step-days");
    expect(days.querySelector(":scope > legend").textContent).toBe("Days");
    const boxes = [...days.querySelectorAll('input[type="checkbox"]')];
    expect(boxes.map((box) => document.querySelector(`label[for="${box.id}"]`).textContent))
      .toEqual(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]);
    for (const [id, label] of [["clock-step-hours-from", "Allowed from"], ["clock-step-hours-until", "Allowed until"]]) {
      const input = document.getElementById(id);
      expect(input.getAttribute("type")).toBe("time");
      expect(input.hasAttribute("required")).toBe(false);
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim()).toBe(label);
    }
    const error = document.querySelector("#clock-step-hours-error");
    expect(error.classList.contains("field-error")).toBe(true);
    expect(error.hasAttribute("aria-live")).toBe(false);
    // It sits before Add step, so the fields are reached before the button.
    expect(group.compareDocumentPosition(document.querySelector("#clock-add-step")) & 4).toBeTruthy(); // DOCUMENT_POSITION_FOLLOWING
  });

  it("gives the schedule date range native labelled date inputs and a target-size rule", () => {
    installDocument({ html: htmlSource });
    const group = document.querySelector("#schedule-range");
    expect(group.tagName).toBe("FIELDSET");
    expect(group.querySelector("legend").textContent.trim()).toBe("Date range");
    for (const [id, label] of [["schedule-start-date", "Start date"], ["schedule-end-date", "End date"]]) {
      const input = document.getElementById(id);
      expect(input.getAttribute("type")).toBe("date");
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim()).toBe(label);
      // Optional: no required attribute, and the description says so in words.
      expect(input.hasAttribute("required")).toBe(false);
      const desc = document.getElementById(input.getAttribute("aria-describedby"));
      expect(desc.textContent).toMatch(/^\s*Optional\./);
    }
    // The form's error text has an ID to be linked from the field it names.
    expect(document.querySelector("#schedule-form-error").classList.contains("field-error")).toBe(true);
    // The days description is not linked in the markup: the script links it
    // only while it is shown, because a hidden description is still read.
    expect(document.querySelector("#schedule-days").hasAttribute("aria-describedby")).toBe(false);
    expect(document.querySelector("#schedule-days-desc").dataset.repeat).toBe("interval");
    // Date and time inputs share the 44 px native control height (2.5.8).
    const rule = stripCssComments(cssSource).match(/([^{}]*)\{[^{}]*min-height:\s*44px[^{}]*\}/g)
      .find((block) => block.includes('input[type="text"]') && block.includes("padding"));
    expect(rule).toContain('input[type="date"]');
    expect(rule).toContain('input[type="time"]');
  });

  it("gives voice pack play dates a fieldset, labelled native date inputs and an error to link", () => {
    installDocument({ html: htmlSource });
    const group = document.querySelector("#tc-dates");
    expect(group.tagName).toBe("FIELDSET");
    expect(group.closest("#spanel-time-checks")).not.toBeNull();
    expect(group.querySelector(":scope > legend").textContent.trim()).toBe("Voice pack play dates");
    expect(document.getElementById(group.getAttribute("aria-describedby")).textContent)
      .toMatch(/^\s*Optional\./);
    expect(document.querySelector('label[for="tc-dates-pack"]').textContent.trim()).toBe("Voice pack");
    for (const [id, label] of [["tc-not-before", "Not before"], ["tc-not-after", "Do not play after"]]) {
      const input = document.getElementById(id);
      expect(input.getAttribute("type")).toBe("date");
      expect(input.hasAttribute("required")).toBe(false);
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim()).toBe(label);
    }
    // The error is text on the page with an ID, not a live region of its own:
    // the status region says it once and the field links to it.
    const error = document.querySelector("#tc-dates-error");
    expect(error.classList.contains("field-error")).toBe(true);
    expect(error.hasAttribute("aria-live")).toBe(false);
    expect(document.querySelector("#tc-dates-save").getAttribute("type")).toBe("button");
  });

  it("hides the schedule date range for one-time events in the script, not the markup", () => {
    installDocument({ html: htmlSource });
    // Shown in the markup, so it never hides a range a saved event has.
    expect(document.querySelector("#schedule-range").hidden).toBe(false);
    expect(document.querySelector("#schedule-range").hasAttribute("data-repeat")).toBe(false);
  });

  it("gives the rotation test labelled native controls, one polite status and list results", () => {
    installDocument({ html: htmlSource });
    const group = document.querySelector("#rotation-test");
    expect(group.tagName).toBe("FIELDSET");
    expect(group.closest("#spanel-picking")).not.toBeNull();
    expect(group.querySelector(":scope > legend").textContent.trim()).toBe("Test rotation");
    const help = document.getElementById(group.getAttribute("aria-describedby"));
    expect(help.textContent).toContain("Nothing on air changes.");
    for (const [id, label, type] of [
      ["rotation-span", "Simulate", "SELECT"],
      ["rotation-start", "Start", "datetime-local"],
      ["rotation-seed", "Seed", "text"],
    ]) {
      const control = document.getElementById(id);
      expect(control.tagName === "SELECT" ? "SELECT" : control.getAttribute("type")).toBe(type);
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim()).toBe(label);
      expect(control.hasAttribute("required")).toBe(false);
    }
    // The spans are said in words, not abbreviations.
    expect([...document.querySelectorAll("#rotation-span option")].map((option) => option.textContent))
      .toEqual(["24 hours", "3 days", "7 days", "14 days", "30 days", "100 songs", "1,000 songs"]);
    for (const id of ["rotation-start", "rotation-seed"]) {
      const desc = document.getElementById(document.getElementById(id).getAttribute("aria-describedby"));
      expect(desc.textContent).toMatch(/^\s*Optional\./);
    }
    for (const [id, name] of [["rotation-run", "Test rotation"], ["rotation-cancel", "Cancel test"]]) {
      const button = document.getElementById(id);
      expect(button.getAttribute("type")).toBe("button");
      expect(button.textContent.trim()).toBe(name);
      expect(button.disabled).toBe(false);
    }
    // One polite status for the start and the end; progress is a labelled
    // native bar and plain text, never live.
    const live = [...group.querySelectorAll("[aria-live], [role='status'], [role='alert']")];
    expect(live.map((node) => node.id)).toEqual(["rotation-status"]);
    expect(live[0].getAttribute("aria-live")).toBe("polite");
    const bar = document.getElementById("rotation-progress");
    expect(bar.tagName).toBe("PROGRESS");
    expect(document.querySelector('label[for="rotation-progress"]').textContent.trim()).toBe("Progress");
    // Results: a section named by its heading, hidden until a run ends, no table.
    const results = document.getElementById("rotation-results");
    expect(results.tagName).toBe("SECTION");
    expect(results.hidden).toBe(true);
    expect(document.getElementById(results.getAttribute("aria-labelledby")).tagName).toBe("H4");
    expect(results.querySelector("table")).toBeNull();
    // Colours come from the theme tokens only, so the light theme follows.
    const rules = stripCssComments(cssSource).match(/#rotation-[^{]*\{[^}]*\}/g);
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) expect(rule).not.toMatch(/#[0-9a-f]{3,8}\b|rgb|hsl/i);
    expect(rules.join("\n")).toContain("accent-color: var(--accent)");
  });

  it("gives the daypart and quarter-hour repeat rules labelled native fields and silent help", () => {
    installDocument({ html: htmlSource });
    const repeats = document.querySelector("#repeat-save").closest("fieldset");
    expect(repeats.closest("#spanel-picking")).not.toBeNull();
    for (const [id, label, type, legend] of [
      ["repeat-daypart-days", "Days", "number", "Prevent a song from playing in the same daypart"],
      ["repeat-daypart-hours", "Daypart start hours", "text",
        "Prevent a song from playing in the same daypart"],
      ["repeat-hourpart-plays", "Plays", "number",
        "Prevent a song from playing in the same quarter of the hour"],
    ]) {
      const input = document.getElementById(id);
      expect(input.getAttribute("type")).toBe(type);
      expect(input.getAttribute("inputmode")).toBe("numeric");
      expect(input.hasAttribute("required")).toBe(false);
      expect(document.querySelector(`label[for="${id}"]`).textContent.trim()).toBe(label);
      const group = input.closest("fieldset");
      expect(group.parentElement.closest("fieldset")).toBe(repeats);
      expect(group.querySelector(":scope > legend").textContent.trim()).toBe(legend);
      // The group's help is read once, on entering it, and never announced.
      const help = document.getElementById(group.getAttribute("aria-describedby"));
      expect(help.closest("fieldset")).toBe(group);
      expect(help.textContent).toMatch(/turns this off/);
      expect(help.hasAttribute("aria-live")).toBe(false);
    }
    const format = document.getElementById(
      document.getElementById("repeat-daypart-hours").getAttribute("aria-describedby"));
    expect(format.textContent).toContain("separated by commas");
    // A refused value is said in words on the page, not only in the status
    // message; the script links it to the field (repeat-settings.test.js).
    const error = document.getElementById("repeat-error");
    expect(error.classList.contains("field-error")).toBe(true);
    expect(error.hidden).toBe(true);
    expect(error.closest("fieldset")).toBe(repeats);
    expect(error.hasAttribute("aria-live")).toBe(false);
    // Text and number fields share the 44 px native control height (2.5.8) and
    // the invalid edge, which is not colour alone: aria-invalid and the message say it.
    const css = stripCssComments(cssSource);
    const rule = css.match(/([^{}]*)\{[^{}]*min-height:\s*44px[^{}]*\}/g)
      .find((block) => block.includes('input[type="text"]') && block.includes("padding"));
    expect(rule).toContain('input[type="number"]');
    expect(css).toContain('input[aria-invalid="true"]');
  });

  it("gives the hour preview a headed table, one polite status and token colours", () => {
    installDocument({ html: htmlSource });
    const card = document.querySelector("#hour-preview-card");
    expect(card.tagName).toBe("DETAILS");
    expect(card.closest("#panel-now")).not.toBeNull();
    // Closed until opened: each preview runs a dry run on the server.
    expect(card.open).toBe(false);
    expect(card.querySelector(":scope > summary > h2").textContent.trim()).toBe("Hour preview");
    const span = document.getElementById("hour-preview-span");
    expect(span.tagName).toBe("SELECT");
    expect(document.querySelector('label[for="hour-preview-span"]').textContent.trim()).toBe("Show");
    expect([...span.options].map((option) => option.textContent))
      .toEqual(["Next 30 minutes", "Next hour", "Next 2 hours", "Next 3 hours"]);
    expect(span.value).toBe("60");
    const help = document.getElementById(span.getAttribute("aria-describedby"));
    expect(help.textContent).toContain("Fixed items");
    expect(help.textContent).toContain("Predicted items");
    expect(help.textContent).toContain("Nothing on air changes.");
    const button = document.getElementById("hour-preview-refresh");
    expect(button.getAttribute("type")).toBe("button");
    expect(button.textContent.trim()).toBe("Refresh preview");
    // One polite status, for Refresh preview; the summary, the backtiming
    // list and the table refresh silently.
    const live = [...card.querySelectorAll("[aria-live], [role='status'], [role='alert'], [role='log']")];
    expect(live.map((node) => node.id)).toEqual(["hour-preview-status"]);
    expect(live[0].getAttribute("aria-live")).toBe("polite");
    for (const id of ["hour-preview-summary", "hour-preview-points", "hour-preview-table"]) {
      expect(document.getElementById(id).closest("[aria-live], [role=status]")).toBeNull();
    }
    // A real table: a caption and a header for each column.
    const table = document.getElementById("hour-preview-table");
    expect(table.querySelector("caption").textContent.trim()).toBe("What plays next");
    expect([...table.querySelectorAll("thead th")].map((th) => [th.textContent.trim(), th.getAttribute("scope")]))
      .toEqual([["Starts", "col"], ["What", "col"], ["Length", "col"], ["Status", "col"]]);
    const backtiming = document.getElementById("hour-preview-backtiming");
    expect(document.getElementById(backtiming.getAttribute("aria-labelledby")).tagName).toBe("H3");
    // Shift+H is in the shortcut list.
    expect(document.querySelector("#hotkey-help-modal").textContent).toContain("Shift+H");
    // Colours come from the theme tokens only, so both themes follow.
    const rules = stripCssComments(cssSource).match(/#hour-preview-[^{]*\{[^}]*\}/g);
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) expect(rule).not.toMatch(/#[0-9a-f]{3,8}\b|rgb|hsl/i);
    // Predicted rows look different, but the Status column says so in words too.
    expect(rules.join("\n")).toMatch(/tr\.is-predicted td\s*\{[^}]*font-style: italic/);
  });

  it("gives Track info's tag editor labelled native fields, linkable errors and one status", () => {
    installDocument({ html: htmlSource });
    const section = document.querySelector("#panel-track-info #tags-editor");
    expect(section.getAttribute("aria-labelledby")).toBe("tags-heading");
    expect(document.getElementById("tags-heading").tagName).toBe("H3");
    const form = section.querySelector("form#tags-form");
    expect(form.hasAttribute("novalidate")).toBe(true);
    const fields = [
      ["title", "Title"], ["artist", "Artist"], ["album", "Album"], ["genre", "Genre"],
      ["year", "Year"], ["bpm", "BPM"], ["key", "Key"], ["isrc", "ISRC"],
    ];
    for (const [name, label] of fields) {
      const input = document.getElementById(`tag-${name}`);
      expect(input.getAttribute("type")).toBe("text");
      expect(input.dataset.tag).toBe(name);
      expect(input.hasAttribute("aria-label")).toBe(false);
      expect(form.querySelector(`label[for="tag-${name}"]`).textContent.trim()).toBe(label);
      // The error is words on the page, hidden until there is one, with an
      // ID the script adds to the field's aria-describedby.
      const error = document.getElementById(`tag-${name}-error`);
      expect(error.classList.contains("field-error")).toBe(true);
      expect(error.hidden).toBe(true);
      expect(error.hasAttribute("role")).toBe(false);
      expect(error.closest("[aria-live]")).toBeNull();
      const described = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      expect(described).not.toContain(error.id);
      for (const id of described) expect(document.getElementById(id).textContent).toMatch(/^\s*Optional\./);
    }
    // Title is the one required field, and says so natively.
    expect(document.getElementById("tag-title").hasAttribute("required")).toBe(true);
    expect(fields.filter(([name]) => document.getElementById(`tag-${name}`).hasAttribute("required"))).toHaveLength(1);
    const beets = document.getElementById("tags-beets");
    expect(beets.getAttribute("type")).toBe("checkbox");
    expect(beets.closest("label").textContent.trim()).toBe("Also update the beets library");
    const save = document.getElementById("tags-save");
    expect(save.getAttribute("type")).toBe("submit");
    expect(save.textContent).toBe("Save tags");
    // One polite status, not nested in another live region.
    const status = document.getElementById("tags-status");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.parentElement.closest("[aria-live], [role=status]")).toBeNull();
    // The layout uses the shared tokens and controls only: no colour of its own.
    const rules = stripCssComments(cssSource).match(/[^{}]*\.tag-field[^{}]*\{[^{}]*\}/g);
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) expect(rule).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|color\s*:/i);
    // Text inputs keep the 44 px control height (2.5.8).
    const height = stripCssComments(cssSource).match(/([^{}]*)\{[^{}]*min-height:\s*44px[^{}]*\}/g)
      .find((block) => block.includes('input[type="text"]') && block.includes("padding"));
    expect(height).toBeTruthy();
  });

  it("gives Track info's matched intros labelled native fields, named lists and one status", () => {
    installDocument({ html: htmlSource });
    const section = document.querySelector("#panel-track-info #intros-editor");
    expect(section.hidden).toBe(true);
    expect(section.getAttribute("aria-labelledby")).toBe("intros-heading");
    expect(document.getElementById("intros-heading").tagName).toBe("H3");
    // Two lists, each named by its own heading, and still lists in Safari.
    for (const kind of ["song", "artist"]) {
      const list = document.getElementById(`intros-${kind}-list`);
      expect(list.getAttribute("role")).toBe("list");
      const heading = document.getElementById(list.getAttribute("aria-labelledby"));
      expect(heading.tagName).toBe("H4");
      expect(list.closest("[aria-live], [role=status]")).toBeNull();
    }
    const form = section.querySelector("form#intros-form");
    expect(form.hasAttribute("novalidate")).toBe(true);
    expect(form.querySelector("legend").textContent.trim()).toBe("Add an intro");
    const fields = [
      ["file", "Audio file", "file"], ["label", "Label", "text"], ["chance", "Chance (percent)", "text"],
      ["cooldown", "Wait between plays (minutes)", "text"], ["not-before", "Not before", "date"],
      ["not-after", "Do not play after", "date"],
    ];
    for (const [name, label, type] of fields) {
      const input = document.getElementById(`intro-${name}`);
      expect(input.getAttribute("type"), name).toBe(type);
      expect(input.hasAttribute("aria-label"), name).toBe(false);
      expect(form.querySelector(`label[for="intro-${name}"]`).textContent.trim()).toBe(label);
      // Help is linked; the error is words on the page, hidden, linked only
      // while it is shown, and never live.
      const described = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      expect(described.length, name).toBeGreaterThan(0);
      for (const id of described) expect(document.getElementById(id).classList.contains("setting-desc")).toBe(true);
      const error = document.getElementById(`intro-${name}-error`);
      expect(error.classList.contains("field-error")).toBe(true);
      expect(error.hidden).toBe(true);
      expect(error.hasAttribute("role")).toBe(false);
      expect(error.closest("[aria-live]")).toBeNull();
      expect(described).not.toContain(error.id);
    }
    expect(document.getElementById("intro-file").getAttribute("accept")).toMatch(/\.mp3.*\.aac/);
    // Where it plays is a native radio group with a legend.
    const applies = document.getElementById("intro-applies");
    expect(applies.tagName).toBe("FIELDSET");
    expect(applies.querySelector("legend").textContent.trim()).toBe("Plays on");
    const radios = [...applies.querySelectorAll('input[type="radio"]')];
    expect(radios.map((radio) => [radio.value, document.querySelector(`label[for="${radio.id}"]`).textContent.trim()]))
      .toEqual([["song", "This song"], ["artist", "Any song by the artist"]]);
    expect(radios[0].checked).toBe(true);
    for (const radio of radios) expect(radio.parentElement.classList.contains("settings-check")).toBe(true);
    const upload = document.getElementById("intro-upload");
    expect(upload.getAttribute("type")).toBe("submit");
    expect(upload.textContent).toBe("Upload intro");
    // Listen plays a hidden preview player, never shown with its own controls.
    const player = document.getElementById("intro-playback");
    expect(player.tagName).toBe("AUDIO");
    expect(player.hidden).toBe(true);
    expect(player.hasAttribute("controls")).toBe(false);
    expect(player.hasAttribute("data-preview-output")).toBe(true);
    // Intros the file skips are said in words on the page, hidden until
    // there are some, and never live.
    const problem = document.getElementById("intros-problem");
    expect(problem.classList.contains("field-error")).toBe(true);
    expect(problem.hidden).toBe(true);
    expect(problem.hasAttribute("role")).toBe(false);
    // One polite status, not nested in another live region.
    const live = [...section.querySelectorAll("[aria-live], [role='status'], [role='alert']")];
    expect(live.map((node) => node.id)).toEqual(["intros-status"]);
    expect(live[0].getAttribute("aria-live")).toBe("polite");
    expect(live[0].getAttribute("aria-atomic")).toBe("true");
    expect(live[0].parentElement.closest("[aria-live], [role=status]")).toBeNull();
    // The layout uses the shared tokens only: no colour of its own.
    const rules = stripCssComments(cssSource).match(/[^{}]*(?:\.intro-list|#intro-applies|#intros-form)[^{}]*\{[^{}]*\}/g);
    expect(rules.length).toBeGreaterThan(0);
    for (const rule of rules) expect(rule).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
  });

  it("gives the preview output device a labelled native select and silent help", () => {
    installDocument({ html: htmlSource });
    const select = document.getElementById("preview-device");
    expect(select.tagName).toBe("SELECT");
    expect(document.querySelector('label[for="preview-device"]').textContent.trim())
      .toBe("Preview output device");
    expect(select.querySelector("option").textContent).toBe("System default");
    // The help is linked and says, in words, where previews never go.
    expect(select.getAttribute("aria-describedby")).toBe("preview-device-desc");
    const help = document.getElementById("preview-device-desc");
    expect(help.hidden).toBe(false);
    expect(help.textContent).toMatch(/never on the stream/);
    expect(help.textContent).toMatch(/saved in this browser/);
    // The unsupported or permission note starts hidden and unlinked: the
    // script links it only while it is shown.
    const note = document.getElementById("preview-device-note");
    expect(note.hidden).toBe(true);
    expect(note.classList.contains("setting-desc")).toBe(true);
    expect(note.hasAttribute("aria-live")).toBe(false);
    const button = document.getElementById("preview-device-choose");
    expect(button.getAttribute("type")).toBe("button");
    // Its own visible name, distinct from the main output's button in the
    // same group (2.4.6, 2.5.3).
    expect(button.textContent.trim()).toBe("Show preview device names");
    expect(button.hasAttribute("aria-label")).toBe(false);
    // What the button does is said with it, and is part of the help text.
    const buttonDesc = document.getElementById(button.getAttribute("aria-describedby"));
    expect(buttonDesc.id).toBe("preview-device-choose-desc");
    expect(buttonDesc.textContent).toMatch(/microphone once/);
    expect(buttonDesc.closest("#preview-device-desc")).not.toBeNull();
    expect(document.getElementById("audio-device-refresh").textContent.trim())
      .not.toBe(button.textContent.trim());
    // Changes are said through the polite Settings status region.
    const status = document.getElementById("settings-status");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(appSource).toMatch(/installPreviewOutput\(\{\s*announce: \(message, options\) => announceStatus\(settingsStatus/);
    // Only the cue point player, Listen on a matched intro and the voice
    // tracker's Listen back are previews; the decks and Listen here are not.
    expect([...document.querySelectorAll("audio[data-preview-output]")].map((audio) => audio.id))
      .toEqual(["points-preview", "intro-playback", "vt-playback"]);
    // The module never moves focus, and the note lines up under the select.
    const moduleSource = readFileSync(staticPath("modules", "preview-output.js"), "utf8");
    expect(moduleSource).not.toMatch(/\.focus\(/);
    expect(stripCssComments(cssSource))
      .toMatch(/\.settings-row \+ \.setting-desc \+ \.setting-desc\s*\{\s*margin-left: 10rem;/);
  });

  it("gives machine tokens a labelled use and a station choice hidden until it applies", () => {
    installDocument({ html: htmlSource });
    const fieldset = document.getElementById("token-name").closest("fieldset");
    expect(fieldset.querySelector("legend").textContent.trim()).toBe("Create a token");
    const scope = document.getElementById("token-scope");
    const station = document.getElementById("token-station");
    for (const [select, label] of [[scope, "Token use"], [station, "Station"]]) {
      expect(select.tagName).toBe("SELECT");
      expect(select.closest("fieldset")).toBe(fieldset);
      expect(document.querySelector(`label[for="${select.id}"]`).textContent.trim()).toBe(label);
      expect(select.hasAttribute("aria-label")).toBe(false);
      const help = document.getElementById(select.getAttribute("aria-describedby"));
      expect(help.classList.contains("setting-desc")).toBe(true);
      expect(help.hasAttribute("aria-live")).toBe(false);
    }
    // Both uses are written out, and what each token cannot do is said.
    expect([...scope.options].map((option) => option.textContent))
      .toEqual(["Analysis worker", "Announcer"]);
    expect(document.getElementById("token-scope-desc").textContent)
      .toMatch(/Neither can play, change settings or read anything else/);
    // Token use says the Station choice appears, and Station starts with a
    // placeholder, so no station is chosen silently.
    expect(document.getElementById("token-scope-desc").textContent)
      .toMatch(/choosing Announcer shows a Station choice below/);
    expect([...station.options].map((option) => [option.value, option.textContent]))
      .toEqual([["", "Choose a station"]]);
    // The station choice and its help start hidden, together.
    expect(document.getElementById("token-station-row").hidden).toBe(true);
    expect(document.getElementById("token-station-desc").hidden).toBe(true);
    // Reading order is name, use, station, then the button that acts on them.
    const order = [...fieldset.querySelectorAll("input, select, button")].map((node) => node.id);
    expect(order).toEqual(["token-name", "token-scope", "token-station", "token-create"]);
    expect(document.getElementById("token-create").getAttribute("type")).toBe("button");
  });

  it("gives the voice tracker named native controls, one polite status and a silent timer", () => {
    installDocument({ html: htmlSource });
    const panel = document.getElementById("panel-voice");
    expect(panel.getAttribute("role")).toBe("tabpanel");
    expect(panel.getAttribute("aria-labelledby")).toBe("tab-voice");
    expect(document.getElementById("tab-voice").textContent).toBe("Voice tracks");
    expect([...panel.children].find((child) => child.tagName === "H2").id).toBe("voice-heading");
    const tracker = document.getElementById("voice-tracker");
    for (const button of tracker.querySelectorAll("button")) {
      expect(button.getAttribute("type"), button.id).toBe("button");
      expect(button.textContent.trim(), button.id).not.toBe("");
    }
    // Record is a toggle with a fixed name: the pressed state says recording.
    const record = document.getElementById("vt-record");
    expect(record.textContent).toBe("Record");
    expect(record.getAttribute("aria-pressed")).toBe("false");
    expect(document.getElementById(record.getAttribute("aria-describedby")).textContent).toMatch(/Alt\+R/);
    // One polite region says the recording state; the timer, the time left
    // and the open song change are on the page and never live.
    const live = [...tracker.querySelectorAll("[aria-live], [role='status'], [role='alert'], [role='log']")];
    expect(live.map((node) => node.id)).toEqual(["vt-status"]);
    expect(live[0].getAttribute("aria-live")).toBe("polite");
    expect(live[0].getAttribute("aria-atomic")).toBe("true");
    for (const id of ["vt-timer", "vt-slot", "vt-closes", "vt-airs", "vt-unavailable", "vt-error", "vt-list"]) {
      expect(document.getElementById(id).closest("[aria-live], [role=status], [role=alert]"), id).toBeNull();
    }
    // An error is words on the page that a control can be described by.
    const error = document.getElementById("vt-error");
    expect(error.classList.contains("field-error")).toBe(true);
    expect(error.hidden).toBe(true);
    const placement = document.getElementById("vt-placement");
    expect(document.querySelector('label[for="vt-placement"]').textContent).toBe("Placement");
    expect(placement.getAttribute("aria-describedby")).toBe("vt-placement-desc");
    expect([...placement.options].map((option) => option.value)).toEqual(["auto", "talk_up", "standalone"]);
    const outro = document.getElementById("vt-allow-outro");
    expect(outro.getAttribute("type")).toBe("checkbox");
    expect(outro.parentElement.classList.contains("settings-check")).toBe(true);
    expect(document.querySelector('label[for="vt-allow-outro"]').textContent).toBe("Start over the ending song's outro");
    expect(outro.getAttribute("aria-describedby")).toBe("vt-allow-outro-desc");
    // Listen back is a named toggle; the player itself is never shown.
    const player = document.getElementById("vt-playback");
    expect(player.tagName).toBe("AUDIO");
    expect(player.hasAttribute("controls")).toBe(false);
    expect(player.hidden).toBe(true);
    expect(document.getElementById("vt-listen").getAttribute("aria-pressed")).toBe("false");
    expect([...tracker.querySelectorAll(".vt-actions button")].map((button) => button.textContent))
      .toEqual(["Listen back", "Book voice track", "Discard recording"]);
    expect(document.getElementById("vt-list").getAttribute("role")).toBe("list");
    // The shortcut is listed with the others, in words.
    const keys = [...document.querySelectorAll("#hotkey-help-modal dt kbd")].map((kbd) => kbd.textContent);
    expect(keys).toContain("Alt+R");
  });

  it("keeps the transition effect description to one sentence", () => {
    // The per-effect list, about 450 words, was read on every focus.
    installDocument({ html: htmlSource });
    const desc = document.querySelector("#transition-desc").textContent.trim();
    expect(desc.split(/\s+/).length).toBeLessThan(20);
  });
});

describe("public status page contracts", () => {
  const statusCss = stripCssComments(readFileSync(staticPath("modules", "status-page.css"), "utf8"));
  const statusScript = readFileSync(staticPath("modules", "status-page.js"), "utf8");

  it("draws every colour from the system palette, so light, dark and forced colours all hold", () => {
    // No literal colours: Canvas, CanvasText and LinkText follow the theme
    // the reader or the web UI chose.  Not ButtonFace and ButtonText:
    // WebKit keeps them light under a dark colour scheme.
    expect(statusCss).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
    expect(statusCss).toMatch(/:root\[data-theme="light"\]\s*\{\s*color-scheme: light;/);
    expect(statusCss).toMatch(/:root\[data-theme="dark"\]\s*\{\s*color-scheme: dark;/);
    expect(statusCss).not.toMatch(/ButtonFace|ButtonText/);
  });

  it("keeps a visible focus ring and a large enough target on every control", () => {
    expect(statusCss).toMatch(/button:focus-visible,\s*a:focus-visible\s*\{\s*outline: 3px solid currentcolor;/);
    expect(statusCss).toMatch(/button\s*\{[^}]*min-height: 44px;/);
    // One column on a phone, so nothing scrolls sideways at 320 px.
    expect(statusCss).toMatch(/@media \(max-width: 24rem\)\s*\{\s*dl\s*\{\s*grid-template-columns: 1fr;/);
  });

  it("polls only the station's own same-site feed and never moves focus", () => {
    expect(statusScript).toMatch(/url\.origin !== new URL\(here\)\.origin/);
    expect(statusScript).not.toMatch(/\.focus\(/);
    // Off air is said in the one live line, not a second region.
    expect(statusScript).not.toMatch(/aria-live/);
  });
});

describe("listener request contracts", () => {
  const requestCss = stripCssComments(readFileSync(staticPath("modules", "request-page.css"), "utf8"));
  const requestsScript = readFileSync(staticPath("modules", "listener-requests.js"), "utf8")
    .replace(/^\s*\/\/.*$/gm, "");

  it("draws the public request page from the system palette, with visible focus and big targets", () => {
    expect(requestCss).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
    expect(requestCss).toMatch(/:root\[data-theme="dark"\]\s*\{\s*color-scheme: dark;/);
    expect(requestCss).not.toMatch(/ButtonFace|ButtonText/);
    expect(requestCss).toMatch(
      /button:focus-visible,\s*a:focus-visible,\s*input:focus-visible\s*\{\s*outline: 3px solid currentcolor;/,
    );
    expect(requestCss).toMatch(/button\s*\{[^}]*min-height: 44px;/);
    expect(requestCss).toMatch(/input\[type="text"\]\s*\{[^}]*min-height: 44px;[^}]*border: 2px solid CanvasText;/);
    // An error is marked by weight and a bar, not by colour.
    expect(requestCss).toMatch(/\.error\s*\{[^}]*border-left: 4px solid CanvasText;[^}]*font-weight: 600;/);
  });

  it("keeps the operator's request card to one status region, labelled lists and named buttons", () => {
    const card = htmlSource.slice(htmlSource.indexOf('id="requests-card"'), htmlSource.indexOf("</details>", htmlSource.indexOf('id="requests-card"')));
    expect(card.match(/aria-live=/g)).toHaveLength(1);
    expect(card).toMatch(/id="requests-status"\s+role="status"\s+aria-live="polite"\s+aria-atomic="true"/);
    expect(card).toMatch(/<ul id="requests-list" aria-label="Waiting requests"/);
    expect(card).toMatch(/<ul id="requests-recent" aria-labelledby="requests-recent-heading"/);
    // Each button starts with its visible word, then names its song.
    expect(requestsScript).toMatch(/button\.append\(label\);/);
    expect(requestsScript).toMatch(/which\.className = "visually-hidden";/);
    expect(requestsScript).not.toMatch(/aria-live/);
  });
});

describe("public player page contracts", () => {
  const playerCss = stripCssComments(readFileSync(staticPath("modules", "player-page.css"), "utf8"));
  // The code only: its comments explain these rules in the same words.
  const playerScript = readFileSync(staticPath("modules", "player-page.js"), "utf8")
    .replace(/^\s*\/\/.*$/gm, "");

  it("draws every colour from the system palette, so light, dark and forced colours all hold", () => {
    expect(playerCss).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
    expect(playerCss).toMatch(/:root\s*\{\s*color-scheme: light dark;/);
    expect(playerCss).toMatch(/:root\[data-theme="light"\]\s*\{\s*color-scheme: light;/);
    expect(playerCss).toMatch(/:root\[data-theme="dark"\]\s*\{\s*color-scheme: dark;/);
    expect(playerCss).not.toMatch(/ButtonFace|ButtonText/);
    // The slider keeps its own system colours under forced colours, so its
    // thumb is never repainted to the background colour.
    expect(playerCss).toMatch(/input\[type="range"\]\s*\{[^}]*forced-color-adjust: none;/);
  });

  it("keeps a visible focus ring and a large enough target on every control", () => {
    expect(playerCss).toMatch(
      /button:focus-visible,\s*a:focus-visible,\s*input:focus-visible\s*\{\s*outline: 3px solid currentcolor;/,
    );
    expect(playerCss).toMatch(/button\s*\{[^}]*min-height: 44px;/);
    // The slider itself is the 24 px target (2.5.8), outlined 3:1 or more (1.4.11).
    expect(playerCss).toMatch(/input\[type="range"\]\s*\{[^}]*height: 24px;[^}]*border: 2px solid CanvasText;/);
    // hidden wins over the flex layouts, so the browser's controls and the
    // script's never show together.
    expect(playerCss).toMatch(/\[hidden\]\s*\{\s*display: none !important;/);
  });

  it("never plays, moves focus or adds a live region on its own", () => {
    expect(playerScript).not.toMatch(/\.focus\(/);
    expect(playerScript).not.toMatch(/aria-live|autoplay/);
    expect(playerScript).not.toMatch(/aria-pressed/);
    // play() is called from one place: the Play button's handler.
    expect(playerScript.match(/\.play\(\)/g)).toHaveLength(1);
    expect(playerScript).toMatch(/url\.origin !== new URL\(here\)\.origin/);
  });
});

describe("station switcher and station context contracts (#70)", () => {
  const ids = (element, attribute) => (element.getAttribute(attribute) || "")
    .split(/\s+/).filter(Boolean);

  it("is an APG menu button in the banner, before the Sections tabs", () => {
    installDocument();
    const wrap = document.getElementById("station-switcher");
    const button = document.getElementById("station-button");
    const menu = document.getElementById("station-menu");
    // Hidden until the server lists a second station.
    expect(wrap.hidden).toBe(true);
    expect(wrap.closest('header[role="banner"]')).not.toBeNull();
    expect(wrap.compareDocumentPosition(document.getElementById("view-nav"))
      & window.Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("type")).toBe("button");
    expect(button.getAttribute("aria-haspopup")).toBe("menu");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.getAttribute("aria-controls")).toBe("station-menu");
    // Named by its visible text, "Station: <name>"; the caret is not read.
    expect(button.hasAttribute("aria-label")).toBe(false);
    expect(button.querySelector(".station-switcher-caret").getAttribute("aria-hidden")).toBe("true");
    const visible = [...button.childNodes]
      .filter((node) => !(node instanceof window.Element && node.getAttribute("aria-hidden") === "true"))
      .map((node) => node.textContent).join("").replace(/\s+/g, " ").trim();
    expect(visible).toBe("Station: AutoDJ");
    expect(menu.tagName).toBe("UL");
    expect(menu.getAttribute("role")).toBe("menu");
    expect(menu.getAttribute("aria-labelledby")).toBe("station-button");
    expect(menu.hidden).toBe(true);
  });

  it("describes every control that changes what is on air with the station", () => {
    installDocument();
    const context = document.getElementById("station-context");
    expect(context.hidden).toBe(true);
    expect(context.textContent).toBe("");
    for (const id of [
      "btn-pause", "btn-skip", "queue-clear", "recording-toggle", "stream-station-name",
      "stream-bitrate", "stream-continuous", "station-stop", "station-start",
    ]) {
      const control = document.getElementById(id);
      expect(control, id).not.toBeNull();
      expect(ids(control, "aria-describedby"), id).toContain("station-context");
      // Every id it names exists, so nothing is silently lost.
      for (const ref of ids(control, "aria-describedby")) {
        expect(document.getElementById(ref), `${id} -> ${ref}`).not.toBeNull();
      }
    }
  });

  it("describes the shared controls as shared by all stations", () => {
    installDocument();
    const shared = document.getElementById("shared-context");
    expect(shared.hidden).toBe(true);
    expect(shared.textContent).toBe("");
    for (const id of ["tab-library", "points-save"]) {
      expect(ids(document.getElementById(id), "aria-describedby"), id).toContain("shared-context");
    }
  });

  it("gives queue rows, search results, carts, listeners, profiles and Run now the station", () => {
    const sources = {
      queue: readFileSync(staticPath("modules", "queue.js"), "utf8"),
      search: readFileSync(staticPath("modules", "search.js"), "utf8"),
      carts: readFileSync(staticPath("modules", "cart-wall.js"), "utf8"),
      listeners: readFileSync(staticPath("modules", "listener-controls.js"), "utf8"),
      profiles: readFileSync(staticPath("modules", "profiles.js"), "utf8"),
      schedule: readFileSync(staticPath("modules", "scheduler-panel.js"), "utf8"),
    };
    expect(sources.queue.match(/aria-describedby="station-context"/g)).toHaveLength(4);
    for (const [name, source] of Object.entries(sources)) {
      if (name === "queue") continue;
      expect(source, name).toMatch(/setAttribute\("aria-describedby", "station-context"\)/);
    }
  });

  it("styles the menu with theme tokens, 44 px items and a tick for the checked one", () => {
    const css = stripCssComments(cssSource);
    const rule = (selector) => {
      const start = css.indexOf(`${selector} {`);
      expect(start, selector).toBeGreaterThanOrEqual(0);
      return css.slice(start, css.indexOf("}", start));
    };
    const menu = rule("#station-menu");
    expect(menu).toMatch(/background: var\(--bg-card\)/);
    expect(menu).toMatch(/color: var\(--text\)/);
    expect(menu).toMatch(/border: 1px solid var\(--control-border\)/);
    const item = rule(".station-switcher-item");
    expect(item).toMatch(/min-height: 44px/);
    expect(css).toMatch(/\.station-switcher-item\[aria-checked="true"\]::before \{ content: "\\2713" \/ ""; \}/);
    // No literal colours anywhere in the switcher's rules.
    const switcherRules = css.slice(css.indexOf(".station-switcher {"), css.indexOf(".station-off-air[hidden]"));
    expect(switcherRules).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(/i);
    // The banner wraps, so the switcher never scrolls the page sideways.
    expect(css).toMatch(/header \{ flex-wrap: wrap; \}/);
  });

  it("lists the station keys in the shortcut help", () => {
    installDocument();
    const help = document.getElementById("hotkey-help-modal").textContent.replace(/\s+/g, " ");
    expect(help).toMatch(/\bc ?Move to the station switcher\. Only when the server has several stations\./);
    expect(help).toMatch(/Shift\+C ?Speak which station this page controls/);
  });
});
