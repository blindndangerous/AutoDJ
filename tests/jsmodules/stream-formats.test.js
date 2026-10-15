import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  FORMAT_SETTLE_MS,
  formatLine,
  formatSavedText,
  installStreamFormats,
} from "../../src/autodj/static/modules/stream-formats.js";

const html = readFileSync(join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8");
const start = html.indexOf('<fieldset id="stream-formats"');
const fieldset = html.slice(start, html.indexOf("</fieldset>", start) + "</fieldset>".length);

const INFO = {
  formats: [
    { format: "mp3", label: "MP3", bitrate: 320, path: "/stream.mp3", listeners: 2 },
    { format: "aac", label: "AAC", bitrate: 96, path: "/stream.aac", listeners: 1 },
    { format: "opus", label: "Opus", bitrate: 0, path: "/stream.opus", listeners: 0 },
  ],
};

function setup(save = vi.fn(async () => true)) {
  document.body.innerHTML = `<div id="settings-status" role="status"></div>
    <div id="sr-status" role="status"></div><div id="status-toast" hidden></div>
    <span id="station-context">Default station</span>${fieldset}`;
  const refresh = vi.fn();
  const view = installStreamFormats({
    doc: document,
    save,
    refresh,
    srStatus: document.getElementById("sr-status"),
    settingsStatus: document.getElementById("settings-status"),
  });
  return { view, save, refresh };
}

const text = (id) => document.getElementById(id).textContent;

describe("stream formats", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("words each address and each save", () => {
    expect(formatLine(INFO.formats[1], "http://radio.local:8080"))
      .toBe("AAC, 96 kbps: http://radio.local:8080/stream.aac");
    expect(formatLine({ ...INFO.formats[1], error: "Encoder not found" }, "http://h"))
      .toBe("AAC, 96 kbps: http://h/stream.aac. Its encoder last said: Encoder not found");
    expect(formatSavedText("Opus", 64)).toBe("Opus stream on at 64 kbps.");
    expect(formatSavedText("AAC", 0)).toBe("AAC stream off.");
  });

  it("starts off, then shows each format that is on with a named Copy button", () => {
    const { view } = setup();
    expect(document.getElementById("stream-aac-bitrate").value).toBe("0");
    expect(document.getElementById("stream-formats-list").hidden).toBe(true);
    expect(document.getElementById("stream-formats-intro").hidden).toBe(true);
    view.fill(INFO);
    expect(document.getElementById("stream-aac-bitrate").value).toBe("96");
    expect(document.getElementById("stream-opus-bitrate").value).toBe("0");
    const items = [...document.querySelectorAll("#stream-formats-list li")];
    const origin = document.location.origin;
    expect(items.map((item) => item.querySelector("span").textContent.trim())).toEqual([
      `MP3, 320 kbps: ${origin}/stream.mp3`,
      `AAC, 96 kbps: ${origin}/stream.aac`,
    ]);
    expect(items.map((item) => item.querySelector("button").textContent))
      .toEqual(["Copy MP3 address", "Copy AAC address"]);
    // The same details again leave the list alone, so reading it is not disturbed.
    const first = items[0];
    view.fill(INFO);
    expect(document.querySelector("#stream-formats-list li")).toBe(first);
    expect(document.getElementById("stream-formats-list").hidden).toBe(false);
  });

  it("saves once the choice settles and says what happened", async () => {
    const { view, save, refresh } = setup();
    view.fill(INFO);
    const opus = document.getElementById("stream-opus-bitrate");
    for (const value of ["48", "64"]) {
      opus.value = value;
      opus.dispatchEvent(new Event("change"));
    }
    expect(save).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(FORMAT_SETTLE_MS + 10);
    expect(save).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledWith("opus", 64, opus);
    await vi.waitFor(() => expect(text("settings-status")).toBe("Opus stream on at 64 kbps."));
    expect(refresh).toHaveBeenCalled();
    // A pushed value never overwrites a select while its save is settling.
    opus.value = "0";
    opus.dispatchEvent(new Event("change"));
    view.apply({ settings: { playback: { stream_opus_bitrate: 64, stream_aac_bitrate: 128 } } });
    expect(opus.value).toBe("0");
    expect(document.getElementById("stream-aac-bitrate").value).toBe("128");
    await vi.advanceTimersByTimeAsync(FORMAT_SETTLE_MS + 10);
    await vi.waitFor(() => expect(text("settings-status")).toBe("Opus stream off."));
  });

  it("puts a refused choice back to what the server has", async () => {
    const { view, save } = setup(vi.fn(async () => false));
    view.fill(INFO);
    const aac = document.getElementById("stream-aac-bitrate");
    aac.value = "256";
    aac.dispatchEvent(new Event("change"));
    await vi.advanceTimersByTimeAsync(FORMAT_SETTLE_MS + 10);
    await vi.waitFor(() => expect(aac.value).toBe("96"));
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("copies an address and says so, or says the address when it cannot", async () => {
    const { view } = setup();
    view.fill(INFO);
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const button = document.querySelector("#stream-formats-list li:nth-child(2) button");
    button.focus();
    button.click();
    await vi.waitFor(() => expect(text("sr-status")).toBe("AAC address copied."));
    expect(writeText).toHaveBeenCalledWith(`${document.location.origin}/stream.aac`);
    expect(document.activeElement).toBe(button);
    writeText.mockRejectedValueOnce(new Error("denied"));
    document.execCommand = vi.fn(() => false);
    button.click();
    await vi.waitFor(() => expect(text("sr-status"))
      .toBe(`Could not copy. The AAC address is ${document.location.origin}/stream.aac`));
  });

  it("clears the list and the choices on reset", () => {
    const { view } = setup();
    view.fill(INFO);
    view.reset();
    expect(document.querySelectorAll("#stream-formats-list li")).toHaveLength(0);
    expect(document.getElementById("stream-aac-bitrate").value).toBe("0");
    expect(document.getElementById("stream-formats-list").hidden).toBe(true);
  });
});
