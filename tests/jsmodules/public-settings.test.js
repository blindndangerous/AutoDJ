import { beforeEach, describe, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";
import { installPublicSettings } from "../../src/autodj/static/modules/public-settings.js";

const { groups } = vi.hoisted(() => ({ groups: [] }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", () => ({
  registerSettingsGroup: (spec) => {
    groups.push(spec);
    return { accept: (body) => spec.write(body), dirty: () => false };
  },
}));

const get = (id) => document.getElementById(`public-${id}`);
const state = {
  dmca_enabled: false, player_page_enabled: false, embed_enabled: false, embed_origins: [],
};

beforeEach(() => { groups.length = 0; installIndexMarkup(); });

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

  it("loads server settings into a public draft and reads the combined body", async () => {
    const saved = { ...state, embed_origins: ["https://example.org"] };
    const request = vi.fn(async () => saved);
    const controls = installPublicSettings({ doc: document, request });
    await controls.refresh();
    expect(get("origins").value).toBe("https://example.org");
    get("dmca").checked = true;
    get("dmca").dispatchEvent(new Event("change", { bubbles: true }));
    get("page").checked = true;
    get("embed").checked = true;
    get("origins").value = "https://one.example\nhttps://two.example";
    const draft = groups.find((group) => group.group === "public");
    expect(draft.read()).toEqual({ dmca_enabled: true, player_page_enabled: true, embed_enabled: true,
      embed_origins: ["https://one.example", "https://two.example"] });
    expect(request).toHaveBeenCalledTimes(1);
    controls.clear();
    expect(get("dmca").checked).toBe(false);
    expect(get("page").checked).toBe(false);
  });

  it("validates invalid site origins and focuses the field before saving", async () => {
    const request = vi.fn(async () => state);
    const controls = installPublicSettings({ doc: document, request });
    await controls.refresh();
    get("origins").value = "ftp://bad.example";
    const draft = groups.find((group) => group.group === "public");
    expect(draft.validate()).toBe(false);
    expect(get("origins").getAttribute("aria-invalid")).toBe("true");
    expect(get("origins").getAttribute("aria-describedby")).toContain("public-origins-error");
    expect(document.activeElement).toBe(get("origins"));
    await vi.waitFor(() => expect(document.getElementById("settings-status").textContent).toMatch(/HTTP\(S\)/));
    expect(request).toHaveBeenCalledTimes(1);
    get("origins").dispatchEvent(new Event("input", { bubbles: true }));
    expect(get("origins").hasAttribute("aria-invalid")).toBe(false);
  });

  it("accepts canonical sites returned by a background load", async () => {
    const request = vi.fn(async () => ({ ...state, embed_origins: ["https://radio.example.net"] }));
    const controls = installPublicSettings({ doc: document, request });
    await controls.refresh();
    expect(get("origins").value).toBe("https://radio.example.net");
  });
});
