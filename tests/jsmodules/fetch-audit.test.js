import { readFileSync, readdirSync } from "node:fs";
import { join, relative } from "node:path";

import { parse } from "espree";
import { describe, expect, it } from "vitest";

function staticPropertyName(member) {
  if (!member.computed && member.property.type === "Identifier") return member.property.name;
  if (member.computed && member.property.type === "Literal") return member.property.value;
  return null;
}

function findRawFetchCalls(source) {
  const ast = parse(source, {
    ecmaVersion: "latest",
    range: true,
    sourceType: "module",
  });
  const FETCH = { kind: "fetch" };
  const GLOBAL = { kind: "global" };
  const LOCAL = { kind: "local" };
  const rootScope = { bindings: new Map([
    ["fetch", FETCH], ["globalThis", GLOBAL], ["self", GLOBAL], ["window", GLOBAL],
  ]), parent: null, type: "program" };
  const calls = [];

  const lookup = (scope, name) => {
    for (let current = scope; current; current = current.parent) {
      if (current.bindings.has(name)) return current.bindings.get(name);
    }
    return LOCAL;
  };
  const bindPattern = (pattern, value, scope) => {
    if (!pattern) return;
    if (pattern.type === "Identifier") {
      scope.bindings.set(pattern.name, value);
      return;
    }
    if (pattern.type === "AssignmentPattern") {
      bindPattern(pattern.left, LOCAL, scope);
      return;
    }
    if (pattern.type === "RestElement") {
      bindPattern(pattern.argument, LOCAL, scope);
      return;
    }
    if (pattern.type === "ObjectPattern") {
      for (const property of pattern.properties) {
        if (property.type === "RestElement") {
          bindPattern(property.argument, LOCAL, scope);
          continue;
        }
        const key = property.computed ? property.key.value : property.key.name;
        const propertyValue = value.kind === "global" && key === "fetch"
          ? FETCH
          : value.kind === "object" && value.properties.has(key)
            ? value.properties.get(key)
            : LOCAL;
        bindPattern(property.value, propertyValue, scope);
      }
      return;
    }
    if (pattern.type === "ArrayPattern") {
      pattern.elements.forEach((element) => bindPattern(element, LOCAL, scope));
    }
  };
  const predeclareLexical = (statements, scope) => {
    for (const statement of statements || []) {
      if (statement.type === "VariableDeclaration" && statement.kind !== "var") {
        statement.declarations.forEach((declaration) => {
          bindPattern(declaration.id, LOCAL, scope);
        });
      } else if (statement.type === "FunctionDeclaration" && statement.id) {
        scope.bindings.set(statement.id.name, LOCAL);
      } else if (statement.type === "ClassDeclaration" && statement.id) {
        scope.bindings.set(statement.id.name, LOCAL);
      } else if (statement.type === "ImportDeclaration") {
        statement.specifiers.forEach((specifier) => {
          scope.bindings.set(specifier.local.name, LOCAL);
        });
      }
    }
  };
  const hoistVarBindings = (node, scope) => {
    if (!node || typeof node !== "object") return;
    if (["ArrowFunctionExpression", "FunctionDeclaration", "FunctionExpression"]
      .includes(node.type)) return;
    if (node.type === "VariableDeclaration" && node.kind === "var") {
      node.declarations.forEach((declaration) => bindPattern(declaration.id, LOCAL, scope));
    }
    for (const [key, value] of Object.entries(node)) {
      if (["range", "type"].includes(key)) continue;
      if (Array.isArray(value)) value.forEach((child) => hoistVarBindings(child, scope));
      else hoistVarBindings(value, scope);
    }
  };
  const nearestVarScope = (scope) => {
    let current = scope;
    while (current.type !== "function" && current.type !== "program") {
      current = current.parent;
    }
    return current;
  };
  const describe = (node, scope) => {
    if (!node) return LOCAL;
    if (node.type === "Identifier") return lookup(scope, node.name);
    if (node.type === "MemberExpression") {
      const object = describe(node.object, scope);
      const property = staticPropertyName(node);
      if (object.kind === "global" && property === "fetch") return FETCH;
      if (object.kind === "fetch" && ["apply", "bind", "call"].includes(property)) {
        return { kind: "invoker", method: property };
      }
      if (object.kind === "object") return object.properties.get(property) || LOCAL;
      return LOCAL;
    }
    if (node.type === "ObjectExpression") {
      const properties = new Map();
      for (const property of node.properties) {
        if (property.type !== "Property") continue;
        const key = property.computed ? property.key.value : property.key.name;
        properties.set(key, describe(property.value, scope));
      }
      return { kind: "object", properties };
    }
    if (node.type === "CallExpression") {
      const callee = describe(node.callee, scope);
      if (callee.kind === "invoker" && callee.method === "bind") return FETCH;
    }
    return LOCAL;
  };
  const assign = (left, value, scope) => {
    if (left.type === "Identifier") {
      for (let current = scope; current; current = current.parent) {
        if (current.bindings.has(left.name)) {
          current.bindings.set(left.name, value);
          return;
        }
      }
      scope.bindings.set(left.name, value);
    } else if (left.type === "MemberExpression") {
      const object = describe(left.object, scope);
      if (object.kind === "object") object.properties.set(staticPropertyName(left), value);
    }
  };
  const walk = (node, scope) => {
    if (!node || typeof node !== "object") return;
    if (node.type === "Program") {
      predeclareLexical(node.body, scope);
      node.body.forEach((statement) => hoistVarBindings(statement, scope));
      node.body.forEach((statement) => walk(statement, scope));
      return;
    }
    if (node.type === "BlockStatement") {
      const blockScope = { bindings: new Map(), parent: scope, type: "block" };
      predeclareLexical(node.body, blockScope);
      node.body.forEach((statement) => walk(statement, blockScope));
      return;
    }
    if (["ArrowFunctionExpression", "FunctionDeclaration", "FunctionExpression"].includes(node.type)) {
      const functionScope = { bindings: new Map(), parent: scope, type: "function" };
      if (node.type === "FunctionExpression" && node.id) {
        functionScope.bindings.set(node.id.name, LOCAL);
      }
      node.params.forEach((parameter) => bindPattern(parameter, LOCAL, functionScope));
      hoistVarBindings(node.body, functionScope);
      walk(node.body, functionScope);
      return;
    }
    if (node.type === "ImportDeclaration") return;
    if (node.type === "CatchClause") {
      const catchScope = { bindings: new Map(), parent: scope, type: "catch" };
      bindPattern(node.param, LOCAL, catchScope);
      walk(node.body, catchScope);
      return;
    }
    if (node.type === "VariableDeclaration") {
      const target = node.kind === "var" ? nearestVarScope(scope) : scope;
      for (const declaration of node.declarations) {
        walk(declaration.init, scope);
        bindPattern(declaration.id, describe(declaration.init, scope), target);
      }
      return;
    }
    if (node.type === "AssignmentExpression") {
      walk(node.right, scope);
      assign(node.left, describe(node.right, scope), scope);
      return;
    }
    if (node.type === "CallExpression") {
      const callee = describe(node.callee, scope);
      if (callee.kind === "fetch"
          || (callee.kind === "invoker" && ["apply", "call"].includes(callee.method))) {
        calls.push(node.range[0]);
      }
    }
    for (const [key, value] of Object.entries(node)) {
      if (key === "range" || key === "type") continue;
      if (Array.isArray(value)) value.forEach((child) => walk(child, scope));
      else walk(value, scope);
    }
  };
  walk(ast, rootScope);
  return calls;
}

function javascriptFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return javascriptFiles(path);
    return entry.name.endsWith(".js") ? [path] : [];
  });
}

describe("browser transport audit", () => {
  it("recognises global, computed, destructured, bound, and aliased raw fetch calls", () => {
    const mutations = `
      window.fetch("/window");
      globalThis["fetch"]("/computed");
      const request = globalThis.fetch;
      request("/alias");
      const { fetch: destructured } = window;
      destructured("/destructured");
      const root = globalThis;
      const bound = root.fetch.bind(root);
      bound("/bound");
    `;
    expect(findRawFetchCalls(mutations)).toHaveLength(5);
  });

  it("recognises self, call/apply, and property/object fetch aliases", () => {
    const mutations = `
      self.fetch("/self");
      fetch.call(self, "/call");
      self["fetch"].apply(self, ["/apply"]);
      const transport = { request: self.fetch };
      const alias = transport;
      alias["request"]("/property");
      const bound = alias.request.bind(self);
      bound("/bound-property");
    `;
    expect(findRawFetchCalls(mutations)).toHaveLength(5);
  });

  it("does not report locally shadowed or injected fetch functions", () => {
    const safe = `
      function injected(fetch) { fetch("/injected"); }
      function localObject() {
        const self = { fetch() {} };
        self.fetch("/local-object");
      }
      { const fetch = () => {}; fetch("/local"); }
    `;
    expect(findRawFetchCalls(safe)).toHaveLength(0);
  });

  it("does not report imported, caught, or function-scoped var fetch bindings", () => {
    const safe = `
      import fetch from "transport";
      fetch("/imported");
      try { throw new Error("offline"); }
      catch (fetch) { fetch("/caught"); }
      function hoisted() {
        fetch("/before-declaration");
        if (true) { var fetch = () => {}; }
        fetch("/after-declaration");
      }
    `;
    expect(findRawFetchCalls(safe)).toHaveLength(0);
  });

  it("keeps the sole raw fetch call in api-client and auth on injected fetchImpl", () => {
    const root = join(process.cwd(), "src/autodj/static");
    const files = javascriptFiles(root);
    const rawCalls = [];
    for (const file of files) {
      for (const offset of findRawFetchCalls(readFileSync(file, "utf8"))) {
        rawCalls.push(`${relative(root, file)}:${offset}`);
      }
    }

    expect(rawCalls).toHaveLength(1);
    expect(rawCalls[0]).toMatch(/^modules[\\/]api-client\.js:/);
    const auth = readFileSync(join(root, "modules", "auth.js"), "utf8");
    expect(findRawFetchCalls(auth)).toHaveLength(0);
    expect(auth).toContain("fetchImpl(");
  });
});
