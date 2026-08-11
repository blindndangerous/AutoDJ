import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const staticPath = (...parts) => join(
  process.cwd(), "src", "autodj", "static", ...parts,
);
const cssSource = readFileSync(staticPath("app.css"), "utf8");
const htmlSource = readFileSync(staticPath("index.html"), "utf8");
const appSource = readFileSync(staticPath("app.js"), "utf8");

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
  const ids = selector.match(/#[\w-]+/g)?.length || 0;
  const attributes = selector.match(/\[[^\]]+\]/g)?.length || 0;
  const classes = selector.match(/\.[\w-]+/g)?.length || 0;
  const pseudoClasses = selector.match(/:(?!:)[\w-]+(?:\([^)]*\))?/g)?.length || 0;
  const typeSource = selector
    .replace(/::[\w-]+/g, " ")
    .replace(/#[\w-]+|\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+(?:\([^)]*\))?/g, " ");
  const types = typeSource.match(/(?:^|[\s>+~])([a-z][\w-]*)/gi)?.length || 0;
  const pseudoElements = selector.match(/::[\w-]+/g)?.length || 0;
  return [ids, attributes + classes + pseudoClasses, types + pseudoElements];
}

function compareSpecificity(first, second) {
  for (let index = 0; index < first.length; index += 1) {
    if (first[index] !== second[index]) return first[index] - second[index];
  }
  return 0;
}

function selectorMatches(element, selector, pseudoElement) {
  const pseudoMatch = selector.match(/(::[\w-]+)$/);
  if ((pseudoMatch?.[1] || "") !== pseudoElement) return false;
  const elementSelector = selector
    .slice(0, pseudoMatch ? -pseudoMatch[1].length : undefined)
    .replaceAll(":focus-visible", ".a11y-focus-visible") || "*";
  try {
    return element.matches(elementSelector);
  } catch {
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
  return rule.media.every((query) => mediaQueryMatches(query, context));
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
  return expanded;
}

function effectiveMediaDeclaration(
  element, pseudoElement, property, context, rules,
) {
  let winner;
  rules.forEach((rule, order) => {
    if (!ruleApplies(rule, context)) return;
    [...rule.declarations].forEach(([declaredProperty, rawValue], declarationOrder) => {
      const important = /\s*!important\s*$/i.test(rawValue);
      const declaredValue = rawValue.replace(/\s*!important\s*$/i, "").trim();
      const value = expandedDeclaration(declaredProperty, declaredValue).get(property);
      if (value === undefined) return;
      for (const selector of rule.selectors) {
        if (!selectorMatches(element, selector, pseudoElement)) continue;
        const candidate = {
          important,
          order: order * 1000 + declarationOrder,
          specificity: selectorSpecificity(selector),
          value,
        };
        const outranks = !winner
          || Number(candidate.important) > Number(winner.important)
          || (candidate.important === winner.important
            && (compareSpecificity(candidate.specificity, winner.specificity) > 0
              || (compareSpecificity(candidate.specificity, winner.specificity) === 0
                && candidate.order >= winner.order)));
        if (outranks) winner = candidate;
      }
    });
  });
  return winner;
}

function reducedMotionMeetsContract(rules = cssRules) {
  installDocument();
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
  installDocument();
  const focusElements = focusContractControls();
  for (const selector of ["details.card > summary", "section[data-view] h2"]) {
    focusElements.push(document.querySelector(selector));
  }
  for (const element of focusElements) element.classList.add("a11y-focus-visible");
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

function installDocument({ css = cssSource, html = htmlSource } = {}) {
  const template = document.createElement("template");
  template.innerHTML = html;
  template.content.querySelectorAll('script, link[rel="stylesheet"]')
    .forEach((element) => element.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  document.head.querySelectorAll("style[data-a11y-contract]")
    .forEach((element) => element.remove());
  const userAgentStyle = document.createElement("style");
  userAgentStyle.dataset.a11yContract = "";
  userAgentStyle.textContent = "pre { margin: 1em 0; }";
  document.head.append(userAgentStyle);
  const style = document.createElement("style");
  style.dataset.a11yContract = "";
  style.textContent = css;
  document.head.append(style);
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

function connectedStatusIsVisible({
  css = cssSource,
  html = htmlSource,
  statusRect,
} = {}) {
  installDocument({ css, html });
  const status = document.querySelector("#conn-status");
  if (!status || status.textContent.trim() === "") return false;
  if (statusRect) status.getBoundingClientRect = () => statusRect;
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

function nativeChoiceControlsRemainNative(css = cssSource) {
  const rules = parseCss(css);
  installDocument({ css });
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

function boundariesMeetContract(css = cssSource) {
  installDocument({ css });
  return contractControls().every((control) => {
    const computed = window.getComputedStyle(control);
    return Number.parseFloat(computed.borderTopWidth) >= 1
      && computed.borderTopStyle === "solid"
      && indicatorColorMeetsContract(control, computed.borderTopColor, "#8aa4d6");
  });
}

function focusOutlinesMeetContract(css = cssSource) {
  installDocument({
    css: css.replaceAll(":focus-visible", ".a11y-focus-visible"),
  });
  const controls = focusContractControls().filter((control) => !control.disabled);
  const expectedOutlines = new Map([
    ["details.card > summary", 3],
    ["section[data-view] h2", 2],
  ]);
  for (const control of controls) control.classList.add("a11y-focus-visible");
  const controlsPass = controls.every((control) => {
    const computed = window.getComputedStyle(control);
    return Number.parseFloat(computed.outlineWidth) === 2
      && computed.outlineStyle === "solid"
      && indicatorColorMeetsContract(control, computed.outlineColor, "#7eb3e8");
  });
  const specialFocusPass = [...expectedOutlines].every(([selector, width]) => {
    const element = document.querySelector(selector);
    element.classList.add("a11y-focus-visible");
    const computed = window.getComputedStyle(element);
    return Number.parseFloat(computed.outlineWidth) === width
      && computed.outlineStyle === "solid"
      && indicatorColorMeetsContract(element, computed.outlineColor, "#7eb3e8");
  });
  return controlsPass && specialFocusPass;
}

function libraryLogOwnershipIsScoped(html = htmlSource) {
  installDocument({ html });
  const panel = document.querySelector("#panel-library");
  const status = document.querySelector("#lib-job-status");
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
    && log?.tagName === "PRE"
    && !log.hasAttribute("aria-live")
    && !log.hasAttribute("role");
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
      css: `${cssSource}\n#conn-status.connected { color: transparent; }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected { background: transparent; }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: absolute; width: 1px; height: 1px;
        overflow: hidden; clip: rect(0, 0, 0, 0);
      }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: absolute; left: -9999px;
      }`,
    })).toBe(false);
    for (const offset of ["right", "top", "bottom"]) {
      expect(connectedStatusIsVisible({
        css: `${cssSource}\n#conn-status.connected {
          position: fixed; ${offset}: -9999px;
        }`,
      })).toBe(false);
    }
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        transform: translateX(-9999px);
      }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: relative; left: -1px;
      }`,
    })).toBe(true);
    for (const [offset, sizeProperty] of [
      ["left", "width"], ["right", "width"],
      ["top", "height"], ["bottom", "height"],
    ]) {
      expect(connectedStatusIsVisible({
        css: `${cssSource}\n#conn-status.connected {
          position: absolute; ${offset}: -100px; ${sizeProperty}: 20px;
        }`,
      }), `${offset} fully outside`).toBe(false);
      expect(connectedStatusIsVisible({
        css: `${cssSource}\n#conn-status.connected {
          position: absolute; ${offset}: -10px; ${sizeProperty}: 20px;
        }`,
      }), `${offset} partially visible`).toBe(true);
    }
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: absolute; left: 0; width: 20px; transform: translateX(-100px);
      }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: absolute; left: 0; width: 20px; transform: translateX(-10px);
      }`,
    })).toBe(true);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: absolute; left: -2000px; width: 20px;
        transform: translateX(2000px);
      }`,
    })).toBe(true);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: absolute; left: -2010px; width: 20px;
        transform: translateX(2000px);
      }`,
    })).toBe(true);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: absolute; left: -2000px; width: 20px; top: -2000px;
        transform: translateX(2000px);
      }`,
    })).toBe(false);
    expect(connectedStatusIsVisible({
      css: `${cssSource}\n#conn-status.connected {
        position: absolute; left: -2000px; width: 20px;
        transform: translate(2000px, -2000px);
      }`,
    })).toBe(false);
    for (const offset of ["left", "top"]) {
      expect(connectedStatusIsVisible({
        css: `${cssSource}\n#conn-status.connected {
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
          css: `${cssSource}\n#conn-status.connected {
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
      css: `${cssSource}\n#conn-status.connected {
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
    const expectedBoundary = "1px solid #8aa4d6";
    for (const selector of [
      "button",
      "#progress-track",
      'input:not([type="checkbox"]):not([type="radio"])',
      "#auth-token",
      "#search-input",
      "select",
      "textarea",
      '#view-nav [role="tab"]',
    ]) {
      expect(lastValue(selector, "border"), selector).toBe(expectedBoundary);
    }
    expect(contrast("#8aa4d6", "#16213e")).toBeGreaterThanOrEqual(3);
    expect(boundariesMeetContract()).toBe(true);
    expect(nativeChoiceControlsRemainNative()).toBe(true);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      input[type="checkbox"] { appearance: none; }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      input[type="radio"] { border: 1px solid #8aa4d6; }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      input[type="checkbox"] { border-left: 1px solid #8aa4d6; }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      input[type="checkbox"]:checked { appearance: none; }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      input[type="radio"]:checked { appearance: none; }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      @media (max-width: 600px) {
        input[type="checkbox"]:checked { appearance: none; }
      }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      @media (min-width: 1000px) {
        input[type="radio"]:not(:checked) { border-left: 1px solid #8aa4d6; }
      }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      @media (max-width: 600px) {
        @media (prefers-reduced-motion: reduce) {
          input[type="checkbox"]:not(:checked) { appearance: none; }
        }
      }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      @media (forced-colors: none) and (min-width: 1000px) {
        input[type="radio"]:checked { appearance: none; }
      }
    `)).toBe(false);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      @media print {
        input[type="checkbox"]:checked { appearance: none; }
      }
    `)).toBe(true);
    expect(nativeChoiceControlsRemainNative(`${cssSource}\n
      @media (min-width: 800px) and (max-width: 900px) {
        input[type="radio"]:checked { appearance: none; }
      }
    `)).toBe(false);
    expect(boundariesMeetContract(
      `${cssSource}\n#btn-skip { border-color: #8aa4d6ff; }`,
    )).toBe(true);
    expect(boundariesMeetContract(
      `${cssSource}\n#btn-skip { border-color: rgba(138, 164, 214, 0); }`,
    )).toBe(false);
    for (const [label, selector] of [
      ["button", "#btn-skip"],
      ["range", "#eq-mid"],
      ["number", "#bpm-hi"],
      ["file", "#ln-upload"],
      ["select", "#transition-select"],
      ["textarea", "#a11y-textarea-fixture"],
      ["tab", "#view-nav #tab-queue"],
    ]) {
      expect(boundariesMeetContract(
        `${cssSource}\n${selector} { border: none; }`,
      ), label).toBe(false);
    }

    expect(lastValue(":focus-visible", "outline")).toBe(
      "2px solid var(--accent)",
    );
    expect(contrast("#7eb3e8", "#16213e")).toBeGreaterThanOrEqual(3);
    expect(focusOutlinesMeetContract()).toBe(true);
    expect(focusOutlinesMeetContract(
      `${cssSource}\n#btn-skip:focus-visible { outline-color: #7eb3e8ff; }`,
    )).toBe(true);
    expect(focusOutlinesMeetContract(
      `${cssSource}\n#btn-skip:focus-visible {
        outline-color: rgba(126, 179, 232, 0);
      }`,
    )).toBe(false);
    for (const selector of [
      "#btn-skip",
      "#tab-queue",
      "#auth-token",
      "#transition-select",
      "#a11y-textarea-fixture",
      "#eq-mid",
      "#dj-phrase-align",
    ]) {
      expect(focusOutlinesMeetContract(
        `${cssSource}\n${selector}:focus-visible { outline: none; }`,
      ), selector).toBe(false);
    }
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
      '<pre id="library-log"',
      '<pre id="library-log" aria-live="polite"',
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

    const progress = document.querySelector("#progress-track");
    const describedBy = progress.getAttribute("aria-describedby").split(/\s+/);
    expect(describedBy).toContain("cue-summary");
    expect(describedBy).not.toContain("cue-details");

    for (const selector of [
      "#now-playing-meta", "#cue-summary", "#cue-details", "#queue-list",
      "#library-log", "#version-stamp",
    ]) {
      const node = document.querySelector(selector);
      expect(node, selector).not.toBeNull();
      expect(node.hasAttribute("aria-live"), selector).toBe(false);
    }
    for (const selector of [
      "#now-playing-meta", "#cue-summary", "#cue-details", "#version-stamp",
    ]) {
      expect(document.querySelector(selector).hasAttribute("role"), selector)
        .toBe(false);
    }
    const libraryLog = document.querySelector("#library-log");
    expect(libraryLog.tagName).toBe("PRE");
    expect(libraryLog.style.margin).toBe("0px");
    const libraryLogStyle = window.getComputedStyle(libraryLog);
    for (const property of [
      "marginTop", "marginRight", "marginBottom", "marginLeft",
    ]) {
      expect(libraryLogStyle[property], property).toBe("0px");
    }
  });
});
