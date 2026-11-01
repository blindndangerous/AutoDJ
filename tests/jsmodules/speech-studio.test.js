import { afterEach, describe, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";
import { installSpeechStudio } from "../../src/autodj/static/modules/speech-studio.js";

describe("speech studio", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("offers only this station's configured command with fixed controls", async () => {
    installIndexMarkup();
    let available = true;
    const request = async (url) => url.endsWith("providers") ? { connections: [] }
      : { command_available: available, batches: {}, saved: {}, presets: {}, sets: {} };
    const controls = installSpeechStudio(document.getElementById("speech-studio"), { request });
    await controls.refresh();
    const connection = document.querySelector("#speech-candidates select");
    connection.value = "station-command";
    connection.dispatchEvent(new Event("change"));
    for (const name of ["voice-id", "model", "speed", "delivery-instructions"]) {
      expect(document.getElementById(`speech-candidate-0-${name}`).disabled).toBe(true);
    }
    expect(document.getElementById("speech-connections").textContent).not.toContain("Edit connection");
    available = false;
    await controls.refresh();
    expect([...connection.options].some((o) => o.value === "station-command")).toBe(false);
    controls.pause();
  });

  it("retains submitted settings when editing a preflight failure", async () => {
    installIndexMarkup();
    const settings = { connection: "cloud", voice: "alloy", model: "tts-1", speed: 1, instructions: "" };
    const request = async (url) => url.endsWith("providers")
      ? { connections: [{ id: "cloud", engine: "openai" }] }
      : { batches: { failed: { text: "Original script", candidates: [{ state: "failed", error: "Missing credential", request: settings }] } }, saved: {}, presets: {}, sets: {} };
    const controls = installSpeechStudio(document.getElementById("speech-studio"), { request });
    await controls.refresh();
    [...document.querySelectorAll("#speech-results button")].find((b) => b.textContent === "Edit comparison").click();
    expect(document.getElementById("speech-text").value).toBe("Original script");
    expect(document.querySelector("#speech-candidates select").value).toBe("cloud");
    expect(document.getElementById("speech-candidate-0-voice-id").value).toBe("alloy");
    expect(document.getElementById("speech-candidate-0-model").value).toBe("tts-1");
    controls.pause();
  });

  it("reuses an admission token after an uncertain HTTP LAN response", async () => {
    vi.stubGlobal("crypto", { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) });
    installIndexMarkup();
    const submissions = [];
    const request = vi.fn(async (url, options) => {
      if (url.endsWith("/batches")) {
        submissions.push(JSON.parse(options.body));
        if (submissions.length === 1) throw new Error("Connection lost");
        return { id: "admitted" };
      }
      return url.endsWith("providers") ? { connections: [] } : { batches: {}, saved: {}, presets: {}, sets: {} };
    });
    const controls = installSpeechStudio(document.getElementById("speech-studio"), { request });
    await controls.refresh();
    document.getElementById("speech-text").value = "Station ID";
    document.getElementById("speech-fresh").checked = true;
    const form = document.getElementById("speech-compare-form");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(document.getElementById("speech-status").textContent).toContain("Connection lost"));
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(submissions).toHaveLength(2));
    expect(submissions[0].token).toBe(submissions[1].token);
    controls.pause();
  });

  it("keeps focused preflight busy without dropping focus or sending duplicate requests", async () => {
    installIndexMarkup();
    let finish;
    const request = vi.fn((url) => url.endsWith("/preflight")
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve(url.endsWith("providers") ? { connections: [] } : { batches: {}, saved: {}, presets: {}, sets: {} }));
    const controls = installSpeechStudio(document.getElementById("speech-studio"), { request });
    await controls.refresh();
    const button = document.getElementById("speech-preflight");
    button.focus();
    button.click();
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect(document.activeElement).toBe(button);
    button.click();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/preflight"))).toHaveLength(1);
    finish({ candidates: [] });
    await vi.waitFor(() => expect(button.hasAttribute("aria-busy")).toBe(false));
    expect(document.activeElement).toBe(button);
    controls.pause();
  });

  it("opening and refreshing never starts synthesis", async () => {
    installIndexMarkup();
    const request = vi.fn(async (url) => url.endsWith("providers")
      ? { connections: [], engines: ["openai", "speechify", "fish", "chatterbox", "gpt-sovits"] }
      : { batches: {}, saved: {}, presets: {}, sets: {}, default_preset: null, active_set: null });
    const controls = installSpeechStudio(document.getElementById("speech-studio"), { request });
    await controls.refresh();
    expect(request.mock.calls.every(([, options]) => !options || options.method === "GET")).toBe(true);
    controls.pause();
  });

  // ADR-0013-08
  it("keeps successes playable beside failures without publishing or auto retry", async () => {
    installIndexMarkup();
    const request = vi.fn(async (url) => url.endsWith("providers")
      ? { connections: [], engines: ["chatterbox"] }
      : { batches: { comparison: { text: "Station ID", candidates: [
        { state: "succeeded", duration: 2, connection: "local", plan: { settings: { voice: "good" }, controls: {} } },
        { state: "failed", error: "Unavailable", connection: "local", plan: { settings: { voice: "bad" }, controls: {} } },
      ] } }, saved: {}, presets: {}, sets: {} });
    const controls = installSpeechStudio(document.getElementById("speech-studio"), { request });
    await controls.refresh();
    const root = document.getElementById("speech-studio");
    expect(root.querySelectorAll("audio"), document.getElementById("speech-status").textContent).toHaveLength(1);
    expect(root.textContent).toContain("Unavailable");
    expect([...root.querySelectorAll("button")].some((b) => b.textContent === "Retry failed sample")).toBe(true);
    expect(request.mock.calls.every(([, options]) => !options || options.method === "GET")).toBe(true);
    controls.pause();
  });
});
