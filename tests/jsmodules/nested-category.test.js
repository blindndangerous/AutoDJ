import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";
import { installCategoriesPanel } from "../../src/autodj/static/modules/categories-panel.js";
import { installClockwheelPanel } from "../../src/autodj/static/modules/clockwheel-panel.js";
import { installIntroRulesPanel } from "../../src/autodj/static/modules/intro-rules-panel.js";

const el = (id) => document.getElementById(id);
let panels;
beforeEach(() => { installIndexMarkup(); el("panel-library").hidden = false; panels = []; });
afterEach(() => panels.reverse().forEach((panel) => panel.destroy()));

async function setup(kind) {
  const categories = [{ id: "old", name: "Old" }];
  const sources = [{ id: "music", kind: "music", path: "D:/Music" }];
  let failure = false;
  let defer = null;
  const listing = () => ({ categories: [...categories], playlists: [], folders: sources, genres: [], beets_fields: [] });
  const request = vi.fn(async (url, options = {}) => {
    if (url === "/api/categories") {
      if (options.method === "POST") {
        if (failure) throw new Error("Save refused");
        if (defer) await defer;
        const category = { ...JSON.parse(options.body), id: "new", tracks: 3 };
        categories.push(category);
        return { category, ...listing() };
      }
      return listing();
    }
    if (url.endsWith("/check")) return { tracks: 3 };
    if (url === "/api/library/clock") return { names: ["Morning"], active: "Morning", sources, categories, presets: [], genres: [], beets_fields: [] };
    if (url === "/api/library/clock/Morning") return { name: "Morning", version: 1, resync_minutes: [0], steps: [] };
    if (url === "/api/intros/rules") return { rules: [], sources, categories, liners: [], liner_folders: [], genres: [], beets_fields: [] };
    throw new Error(`Unexpected request ${url}`);
  });
  const categoryPanel = installCategoriesPanel({ document, request });
  panels.push(categoryPanel);
  const createCategory = (options) => categoryPanel.create(options);
  const parent = kind === "clock"
    ? installClockwheelPanel({ document, request, createCategory })
    : installIntroRulesPanel({ document, request, createCategory });
  panels.push(parent);
  await vi.waitFor(() => expect(request.mock.calls.some(([url]) => url === (kind === "clock" ? "/api/library/clock/Morning" : "/api/intros/rules"))).toBe(true));
  const host = el(kind === "clock" ? "clock-step-conditions" : "intro-rules-conditions");
  el(kind === "clock" ? "clock-new-step" : "intro-rules-add").click();
  el(kind === "clock" ? "clock-add-condition" : "intro-rules-condition-add").click();
  for (const row of host.children) {
    const field = row.querySelector("select");
    field.value = "category";
    field.dispatchEvent(new Event("change", { bubbles: true }));
    row.querySelector('[data-condition-control="music-category"]').value = "old";
  }
  const row = host.lastElementChild;
  const select = row.querySelector('[data-condition-control="music-category"]');
  const opener = row.querySelector("[data-add-category]");
  const dialog = el(kind === "clock" ? "clock-step-dialog" : "intro-rules-form-dialog");
  opener.focus();
  opener.click();
  await vi.waitFor(() => expect(el("categories-form-dialog").open).toBe(true));
  const submit = () => el("categories-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  return { categories, parent, categoryPanel, request, host, row, select, opener, dialog, submit,
    fail: () => { failure = true; }, hold: (promise) => { defer = promise; } };
}

for (const kind of ["clock", "intro"]) {
  it(`${kind}: saves a nested category independently, selecting only its originating row`, async () => {
    const state = await setup(kind);
    const first = state.host.firstElementChild;
    el("categories-name").value = "New";
    el("categories-query").value = "genre:Rock";
    state.submit();
    await vi.waitFor(() => expect(el("categories-form-dialog").open).toBe(false));
    expect(state.dialog.open).toBe(true);
    expect(state.host.lastElementChild).toBe(state.row);
    expect(state.host.firstElementChild).toBe(first);
    expect(first.querySelector('[data-condition-control="music-category"]').value).toBe("old");
    expect(state.select.value).toBe("new");
    expect(document.activeElement).toBe(state.select);
    expect(state.request.mock.calls.filter(([, options]) => options?.method === "POST").every(([url]) => url === "/api/categories" || url.endsWith("/check"))).toBe(true);
    state.dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    await vi.waitFor(() => expect(el("confirm-dialog").open).toBe(true));
    el("confirm-accept").click();
    await vi.waitFor(() => expect(state.dialog.open).toBe(false));
    expect(state.categories.some((category) => category.id === "new")).toBe(true);
  });

  it.each(["cancel", "escape"])(`${kind}: %s returns to the original category action without changing the selection`, async (action) => {
    const state = await setup(kind);
    if (action === "cancel") el("categories-cancel").click();
    else el("categories-form-dialog").dispatchEvent(new Event("cancel", { cancelable: true }));
    expect(el("categories-form-dialog").open).toBe(false);
    expect(state.dialog.open).toBe(true);
    expect(state.select.value).toBe("old");
    expect(document.activeElement).toBe(state.opener);
  });

  it(`${kind}: keeps dirty category drafts and errors inside the nested editor`, async () => {
    const state = await setup(kind);
    state.submit();
    await vi.waitFor(() => expect(el("categories-name").getAttribute("aria-invalid")).toBe("true"));
    expect(document.activeElement).toBe(el("categories-name"));
    el("categories-name").value = "Draft";
    el("categories-query").value = "genre:Rock";
    state.fail();
    state.submit();
    await vi.waitFor(() => expect(el("categories-form-error").textContent).toBe("Save refused"));
    expect(state.dialog.open).toBe(true);
    el("categories-cancel").click();
    await vi.waitFor(() => expect(el("confirm-dialog").open).toBe(true));
    el("confirm-cancel").click();
    expect(el("categories-name").value).toBe("Draft");
    expect(el("categories-form-dialog").open).toBe(true);
  });

  it(`${kind}: closes nested editors and ignores late save results when the parent station is cleared`, async () => {
    const state = await setup(kind);
    let finish;
    state.hold(new Promise((resolve) => { finish = resolve; }));
    el("categories-name").value = "Late";
    el("categories-query").value = "genre:Rock";
    state.submit();
    state.submit();
    expect(state.request.mock.calls.filter(([, options]) => options?.method === "POST" && options.body.includes('"Late"'))).toHaveLength(1);
    state.parent.clearProtectedData();
    expect(state.dialog.open).toBe(false);
    expect(el("categories-form-dialog").open).toBe(false);
    finish();
    await Promise.resolve();
    await Promise.resolve();
    expect(state.select.value).toBe("old");
  });
}
