import { beforeEach, describe, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";
import { installPublicSettings } from "../../src/autodj/static/modules/public-settings.js";

const get = (id) => document.getElementById(`public-${id}`);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const state = {
  dmca_enabled: false, player_page_enabled: false, embed_enabled: false, embed_origins: [],
};

beforeEach(installIndexMarkup);

describe("public station settings", () => {
  it("keeps the Stream tab available and labels every control with short help", () => {
    expect(document.getElementById("stab-stream").hidden).toBe(false);
    for (const id of ["dmca", "page", "embed", "origins"]) {
      expect(document.querySelector(`label[for="public-${id}"]`)?.textContent.trim()).toBeTruthy();
      const help = document.getElementById(get(id).getAttribute("aria-describedby"));
      expect(help?.textContent.trim()).toBeTruthy();
    }
    expect(document.getElementById("np-show-next-dmca").textContent).toMatch(/Settings, Song picking/);
    expect(document.getElementById("np-show-next-dmca").textContent).not.toMatch(/config\.toml/);
  });

  it("loads and saves each group for the selected station", async () => {
    let saved = { ...state, embed_origins: ["https://example.org"] };
    const request = vi.fn(async (_url, options = {}) => {
      if (options.method === "PUT") saved = { ...saved, ...JSON.parse(options.body) };
      return saved;
    });
    const controls = installPublicSettings({ doc: document, request });
    await controls.refresh();
    expect(get("origins").value).toBe("https://example.org");
    get("dmca").checked = true;
    get("dmca").dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(saved.dmca_enabled).toBe(true);
    await vi.waitFor(() => expect(document.getElementById("settings-status").textContent)
      .toMatch(/DMCA mode on/));

    get("page").checked = true;
    get("embed").checked = true;
    get("origins").value = "https://one.example\nhttps://two.example";
    get("page-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await flush();
    expect(saved).toMatchObject({ player_page_enabled: true, embed_enabled: true,
      embed_origins: ["https://one.example", "https://two.example"] });
    expect(request).toHaveBeenCalledWith("/api/public-settings", expect.objectContaining({ method: "PUT" }));
    controls.clear();
    expect(get("dmca").checked).toBe(false);
    expect(get("page").checked).toBe(false);
  });

  it("marks the save busy and focuses an invalid site with its error", async () => {
    let rejectSave;
    const request = vi.fn((_url, options = {}) => options.method === "PUT"
      ? new Promise((_resolve, reject) => { rejectSave = reject; }) : Promise.resolve(state));
    const controls = installPublicSettings({ doc: document, request });
    await controls.refresh();
    get("origins").value = "ftp://bad.example";
    get("page-form").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(get("page-save").getAttribute("aria-busy")).toBe("true");
    expect(get("page-save").hasAttribute("aria-disabled")).toBe(false);
    rejectSave(Object.assign(new Error("Sites allowed to embed: 'ftp://bad.example' is not an http:// or https:// site address, such as https://radio.example.net."),
      { status: 400, field: "embed_origins" }));
    await flush();
    expect(get("page-save").hasAttribute("aria-busy")).toBe(false);
    expect(get("origins").getAttribute("aria-invalid")).toBe("true");
    expect(get("origins").getAttribute("aria-describedby")).toContain("public-origins-error");
    expect(document.activeElement).toBe(get("origins"));
    await vi.waitFor(() => expect(document.getElementById("settings-status").textContent)
      .toMatch(/Sites allowed to embed: 'ftp:\/\/bad.example'/));
    get("origins").dispatchEvent(new Event("input", { bubbles: true }));
    expect(get("origins").hasAttribute("aria-invalid")).toBe(false);
  });

  it("shows canonical sites returned after saving", async () => {
    const request = vi.fn(async (_url, options = {}) => options.method === "PUT"
      ? { ...state, embed_origins: ["https://radio.example.net"] } : state);
    const controls = installPublicSettings({ doc: document, request });
    await controls.refresh();
    get("origins").value = "HTTPS://RADIO.EXAMPLE.NET:443";
    get("page-form").dispatchEvent(new Event("submit", { cancelable: true }));
    await flush();
    expect(get("origins").value).toBe("https://radio.example.net");
  });

  it("reverts a DMCA change the server refuses", async () => {
    const request = vi.fn(async (_url, options = {}) => {
      if (options.method === "PUT") throw new Error("Could not save");
      return state;
    });
    const controls = installPublicSettings({ doc: document, request });
    await controls.refresh();
    get("dmca").checked = true;
    get("dmca").dispatchEvent(new Event("change", { bubbles: true }));
    await flush();
    expect(get("dmca").checked).toBe(false);
    await vi.waitFor(() => expect(document.getElementById("settings-status").textContent)
      .toMatch(/Could not save DMCA mode/));
  });

  it("keeps newer site edits made while a save is pending", async () => {
    let resolveSave;
    const request = vi.fn((_url, options = {}) => options.method === "PUT"
      ? new Promise((resolve) => { resolveSave = resolve; }) : Promise.resolve(state));
    const controls = installPublicSettings({ doc: document, request });
    await controls.refresh();
    get("origins").value = "https://first.example";
    get("page-form").dispatchEvent(new Event("submit", { cancelable: true }));
    get("origins").value = "https://newer.example";
    get("origins").dispatchEvent(new Event("input", { bubbles: true }));
    resolveSave({ ...state, embed_origins: ["https://first.example"] });
    await flush();
    expect(get("origins").value).toBe("https://newer.example");
  });
});
