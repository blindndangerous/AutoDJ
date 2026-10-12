// app.css resolved for one theme, for tests that read colours through
// happy-dom, which does not evaluate light-dark().  Every
// light-dark(light, dark) becomes its light or its dark argument, so the
// computed style is the one a browser shows with that theme in force.

export const THEMES = ["light", "dark"];

function closingParen(source, open) {
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "(") depth += 1;
    else if (source[index] === ")" && --depth === 0) return index;
  }
  throw new Error("Unclosed light-dark(");
}

function topLevelComma(source) {
  let depth = 0;
  for (let index = 0; index < source.length; index += 1) {
    if (source[index] === "(") depth += 1;
    else if (source[index] === ")") depth -= 1;
    else if (source[index] === "," && depth === 0) return index;
  }
  return -1;
}

export function cssForTheme(css, theme) {
  if (!THEMES.includes(theme)) throw new Error(`Unknown theme ${theme}`);
  let out = "";
  let from = 0;
  for (;;) {
    const at = css.indexOf("light-dark(", from);
    if (at === -1) return out + css.slice(from);
    const open = at + "light-dark".length;
    const close = closingParen(css, open);
    const inner = css.slice(open + 1, close);
    const comma = topLevelComma(inner);
    if (comma === -1) {
      // "light-dark()" named in a comment, not a value.
      out += css.slice(from, close + 1);
      from = close + 1;
      continue;
    }
    const pick = theme === "light" ? inner.slice(0, comma) : inner.slice(comma + 1);
    out += css.slice(from, at) + pick.trim();
    from = close + 1;
  }
}

// The custom properties declared on the bare :root rule, resolved for
// one theme (var() references to other tokens included).
export function themeTokens(css, theme) {
  const resolved = cssForTheme(css, theme).replace(/\/\*[\s\S]*?\*\//g, "");
  const block = resolved.match(/(?:^|})\s*:root\s*\{([^}]*)\}/)?.[1];
  if (!block) throw new Error("No :root block");
  const tokens = new Map();
  for (const match of block.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) {
    tokens.set(match[1], match[2].trim());
  }
  const resolve = (value, depth = 0) => (depth > 8 ? value : value.replace(
    /var\((--[\w-]+)\)/g,
    (_, name) => resolve(tokens.get(name) ?? "", depth + 1),
  ));
  return new Map([...tokens].map(([name, value]) => [name, resolve(value)]));
}
