import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installLibrarySettings } from "../../src/autodj/static/modules/library-settings.js";
import { chooseAction } from "../../src/autodj/static/modules/confirm-dialog.js";
import { resetSettingsDrafts } from "../../src/autodj/static/modules/settings-drafts.js";

const api = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../../src/autodj/static/modules/api-client.js", async (importOriginal) => ({
  ...await importOriginal(),
  requestJson: api.request,
  captureAuthenticatedRequestEpoch: () => 1,
  isAuthenticatedRequestCurrent: () => true,
}));
vi.mock("../../src/autodj/static/modules/confirm-dialog.js", () => ({ chooseAction: vi.fn() }));

const html = readFileSync(join(process.cwd(), "src/autodj/static/index.html"), "utf8");
const field = (id) => document.getElementById(id);
const libraryRequest = vi.fn();

function mount() {
  const page = document.createElement("template");
  page.innerHTML = html;
  document.body.replaceChildren(page.content.querySelector("#spanel-libraries").cloneNode(true));
  const selector = field("library-settings-library");
  const oldies = document.createElement("option");
  oldies.value = "oldies";
  oldies.textContent = "Oldies";
  selector.add(oldies);
  return selector;
}

function responseFor(url, options) {
  const library = url.match(/[?&]library=([^&]+)/)?.[1] || null;
  if (url.includes("advanced-length")) {
    if (options?.method === "PUT") return JSON.parse(options.body);
    return { minutes: library === "main" ? 20 : 40 };
  }
  if (url.includes("music-fade")) {
    if (options?.method === "PUT") return JSON.parse(options.body);
    return { drop_db: library === "main" ? 10 : 18 };
  }
  return {};
}

async function changedTo(selector, value) {
  selector.value = value;
  selector.dispatchEvent(new Event("change", { bubbles: true }));
  await vi.waitFor(() => expect(selector.hasAttribute("aria-busy")).toBe(false));
}

describe("per-library Settings", () => {
  beforeEach(() => {
    libraryRequest.mockReset();
    libraryRequest.mockImplementation(async (url, options) => responseFor(url, options));
    api.request.mockReset();
    api.request.mockResolvedValue({ revision: "one", profile: "", values: {} });
    vi.mocked(chooseAction).mockReset();
    resetSettingsDrafts(document);
  });
  afterEach(() => vi.useRealTimers());

  it("loads each settings value with an explicit library ID", async () => {
    const selector = mount();
    const controls = installLibrarySettings({ document, request: libraryRequest });
    await controls.load();
    expect(field("advanced-length").value).toBe("20");
    expect(field("music-fade-drop").value).toBe("10");
    expect(libraryRequest).toHaveBeenCalledWith("/api/library/advanced-length?library=main");
    expect(libraryRequest).toHaveBeenCalledWith("/api/library/music-fade?library=main");

    await changedTo(selector, "oldies");
    await vi.waitFor(() => expect(field("advanced-length").value).toBe("40"));
    expect(field("music-fade-drop").value).toBe("18");
    expect(libraryRequest).toHaveBeenCalledWith("/api/library/advanced-length?library=oldies");
    expect(libraryRequest).toHaveBeenCalledWith("/api/library/music-fade?library=oldies");
  });

  // ADR-0017-07
  it("cancels a library change when the settings draft should be kept", async () => {
    const selector = mount();
    const controls = installLibrarySettings({ document, request: libraryRequest });
    await controls.load();
    field("advanced-length").value = "30";
    field("advanced-length").dispatchEvent(new Event("input", { bubbles: true }));
    vi.mocked(chooseAction).mockResolvedValue("cancel");
    selector.value = "oldies";
    selector.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(selector.hasAttribute("aria-busy")).toBe(false));
    expect(selector.value).toBe("main");
    expect(libraryRequest.mock.calls.some(([url]) => url.includes("library=oldies"))).toBe(false);
  });

  // ADR-0017-07
  it("discards a library draft before loading the next library", async () => {
    const selector = mount();
    const controls = installLibrarySettings({ document, request: libraryRequest });
    await controls.load();
    field("advanced-length").value = "30";
    field("advanced-length").dispatchEvent(new Event("input", { bubbles: true }));
    vi.mocked(chooseAction).mockResolvedValue("alternate");
    await changedTo(selector, "oldies");
    expect(selector.value).toBe("oldies");
    expect(field("advanced-length").value).toBe("40");
    expect(libraryRequest.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
  });

  // ADR-0017-07
  it("saves the old library draft before loading the next library", async () => {
    const selector = mount();
    const controls = installLibrarySettings({ document, request: libraryRequest });
    await controls.load();
    field("advanced-length").value = "30";
    field("advanced-length").dispatchEvent(new Event("input", { bubbles: true }));
    expect(field("spanel-libraries").querySelector(".settings-save-bar > p").textContent).toBe("Library: Main");
    vi.mocked(chooseAction).mockResolvedValue("accept");
    await changedTo(selector, "oldies");
    const save = libraryRequest.mock.calls.find(([url, options]) =>
      url === "/api/library/advanced-length?library=main" && options?.method === "PUT");
    expect(save?.[1]?.body).toBe(JSON.stringify({ minutes: 30 }));
    expect(selector.value).toBe("oldies");
    expect(field("advanced-length").value).toBe("40");
  });

  // ADR-0017-07
  it("discards a stale library response and keeps the new library values", async () => {
    const selector = mount();
    const finishMain = [];
    const request = vi.fn((url) => url.includes("library=main")
      ? new Promise((resolve) => { finishMain.push(resolve); })
      : Promise.resolve(responseFor(url)));
    const controls = installLibrarySettings({ document, request });
    const oldLoad = controls.load();
    await changedTo(selector, "oldies");
    for (const finish of finishMain) finish({ minutes: 11 });
    await oldLoad;
    expect(field("advanced-length").value).toBe("40");
    expect(field("music-fade-drop").value).toBe("18");
  });

  it("reports a failed library load and keeps the previous values", async () => {
    const selector = mount();
    const request = vi.fn(async (url, options) => {
      if (url === "/api/library/music-fade?library=oldies") throw new Error("offline");
      return responseFor(url, options);
    });
    const controls = installLibrarySettings({ document, request });
    await controls.load();
    await changedTo(selector, "oldies");
    expect(selector.value).toBe("main");
    expect(field("advanced-length").value).toBe("20");
    expect(field("music-fade-drop").value).toBe("10");
    expect(field("libraries-status").textContent).toMatch(/load library settings/i);
  });

  it("keeps focus on the library selector while settings load", async () => {
    const selector = mount();
    const finish = [];
    const request = vi.fn((url) => url.includes("library=oldies")
      ? new Promise((resolve) => { finish.push(() => resolve(responseFor(url))); })
      : Promise.resolve(responseFor(url)));
    const controls = installLibrarySettings({ document, request });
    await controls.load();
    selector.focus();
    selector.value = "oldies";
    selector.dispatchEvent(new Event("change", { bubbles: true }));
    await vi.waitFor(() => expect(selector.getAttribute("aria-busy")).toBe("true"));
    expect(selector.disabled).toBe(false);
    expect(document.activeElement).toBe(selector);
    finish.forEach((resolve) => resolve());
    await vi.waitFor(() => expect(selector.hasAttribute("aria-busy")).toBe(false));
  });
});
