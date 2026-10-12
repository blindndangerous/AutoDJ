import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DENIED_TEXT,
  MISSING_TEXT,
  PREVIEW_SINK_KEY,
  UNSUPPORTED_TEXT,
  installPreviewOutput,
  outputChoices,
  previewOutputSupport,
} from "../../src/autodj/static/modules/preview-output.js";

const DEVICES = [
  { kind: "audioinput", deviceId: "mic", label: "Microphone" },
  { kind: "audiooutput", deviceId: "default", label: "Default - Speakers" },
  { kind: "audiooutput", deviceId: "speakers", label: "Speakers" },
  { kind: "audiooutput", deviceId: "phones", label: "Headphones" },
];

function memoryStorage(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
    store,
  };
}

function installDom() {
  document.body.innerHTML = `
    <label for="preview-device">Preview output device</label>
    <select id="preview-device" aria-describedby="preview-device-desc">
      <option value="">System default</option>
    </select>
    <button type="button" id="preview-device-choose">Show device names</button>
    <span id="preview-device-desc">Help</span>
    <p id="preview-device-note" hidden></p>
    <audio id="points-preview" data-preview-output></audio>
    <audio id="browser-player"></audio>`;
  const preview = document.getElementById("points-preview");
  const deck = document.getElementById("browser-player");
  for (const element of [preview, deck]) {
    // happy-dom's sinkId is a getter only.
    Object.defineProperty(element, "sinkId", { value: "", writable: true });
    element.setSinkId = vi.fn(async function (id) { this.sinkId = id; });
  }
  return { preview, deck };
}

function fakeMediaDevices(devices = DEVICES, extra = {}) {
  const listeners = {};
  return {
    enumerateDevices: vi.fn(async () => devices),
    getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: vi.fn() }] })),
    addEventListener: (type, handler) => { listeners[type] = handler; },
    fire: (type) => listeners[type]?.(),
    ...extra,
  };
}

const supportedProto = { setSinkId() {} };

async function settle() {
  for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function setup({ devices, storage = memoryStorage(), mediaDevices, proto = supportedProto } = {}) {
  const dom = installDom();
  const md = mediaDevices || fakeMediaDevices(devices);
  const announce = vi.fn();
  const controls = installPreviewOutput({
    document,
    mediaDevices: md,
    mediaElementPrototype: proto,
    storage,
    announce,
  });
  return {
    ...dom,
    controls,
    md,
    announce,
    storage,
    select: document.getElementById("preview-device"),
    button: document.getElementById("preview-device-choose"),
    note: document.getElementById("preview-device-note"),
  };
}

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("previewOutputSupport", () => {
  it("needs both enumerateDevices and element setSinkId", () => {
    expect(previewOutputSupport({ mediaDevices: fakeMediaDevices(), mediaElementPrototype: supportedProto }))
      .toEqual({ supported: true, canChoose: false });
    expect(previewOutputSupport({ mediaDevices: fakeMediaDevices(), mediaElementPrototype: {} }).supported)
      .toBe(false);
    expect(previewOutputSupport({ mediaDevices: undefined, mediaElementPrototype: supportedProto }).supported)
      .toBe(false);
    expect(previewOutputSupport({
      mediaDevices: fakeMediaDevices(DEVICES, { selectAudioOutput: vi.fn() }),
      mediaElementPrototype: supportedProto,
    }).canChoose).toBe(true);
  });
});

describe("outputChoices", () => {
  it("lists outputs only, drops the duplicate default and numbers unnamed devices", () => {
    expect(outputChoices(DEVICES).map((choice) => choice.label)).toEqual(["Speakers", "Headphones"]);
    expect(outputChoices([
      { kind: "audiooutput", deviceId: "a", label: "" },
      { kind: "audiooutput", deviceId: "b", label: "" },
    ]).map((choice) => choice.label)).toEqual(["Output device 1", "Output device 2"]);
    expect(outputChoices(undefined)).toEqual([]);
  });
});

describe("installPreviewOutput", () => {
  it("lists devices and routes only the preview player to the chosen one", async () => {
    const { select, preview, deck, announce, storage } = setup();
    await settle();
    expect([...select.options].map((option) => option.textContent))
      .toEqual(["System default", "Speakers", "Headphones"]);

    select.focus();
    select.value = "phones";
    select.dispatchEvent(new Event("change"));
    await settle();

    expect(preview.setSinkId).toHaveBeenLastCalledWith("phones");
    expect(deck.setSinkId).not.toHaveBeenCalled();
    expect(storage.store.get(PREVIEW_SINK_KEY)).toBe("phones");
    expect(announce).toHaveBeenLastCalledWith("Previews play on Headphones.",
      expect.objectContaining({ force: true }));
    expect(document.activeElement).toBe(select);
  });

  it("restores the saved device on load without saying anything", async () => {
    const { select, preview, announce } = setup({
      storage: memoryStorage({ [PREVIEW_SINK_KEY]: "phones" }),
    });
    await settle();
    expect(select.value).toBe("phones");
    expect(preview.sinkId).toBe("phones");
    expect(announce).not.toHaveBeenCalled();
  });

  it("forgets nothing when storage throws", async () => {
    const throwing = {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
      removeItem: () => { throw new Error("blocked"); },
    };
    const { select, preview } = setup({ storage: throwing });
    await settle();
    select.value = "phones";
    select.dispatchEvent(new Event("change"));
    await settle();
    expect(preview.sinkId).toBe("phones");
  });

  it("explains in words and keeps the default output where setSinkId is missing", async () => {
    const { select, button, note, preview, announce } = setup({ proto: {} });
    await settle();
    expect(select.disabled).toBe(true);
    expect(button.hidden).toBe(true);
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe(UNSUPPORTED_TEXT);
    expect(select.getAttribute("aria-describedby")).toBe("preview-device-desc preview-device-note");
    expect(preview.setSinkId).not.toHaveBeenCalled();
    expect(announce).not.toHaveBeenCalled();
  });

  it("falls back to the default when the saved device is removed, then uses it again", async () => {
    const devices = [...DEVICES];
    const { select, preview, note, announce, storage, md } = setup({
      devices,
      storage: memoryStorage({ [PREVIEW_SINK_KEY]: "phones" }),
    });
    await settle();
    expect(preview.sinkId).toBe("phones");

    devices.splice(devices.findIndex((device) => device.deviceId === "phones"), 1);
    md.fire("devicechange");
    await settle();
    expect(preview.sinkId).toBe("");
    expect(select.value).toBe("");
    expect(note.textContent).toBe(MISSING_TEXT);
    expect(select.getAttribute("aria-describedby")).toContain("preview-device-note");
    expect(storage.store.get(PREVIEW_SINK_KEY)).toBe("phones");
    expect(announce).toHaveBeenCalledTimes(1);

    // A second devicechange while it is still gone says nothing more.
    md.fire("devicechange");
    await settle();
    expect(announce).toHaveBeenCalledTimes(1);

    devices.push({ kind: "audiooutput", deviceId: "phones", label: "Headphones" });
    md.fire("devicechange");
    await settle();
    expect(preview.sinkId).toBe("phones");
    expect(select.value).toBe("phones");
    expect(note.hidden).toBe(true);
    expect(select.getAttribute("aria-describedby")).toBe("preview-device-desc");
    expect(announce).toHaveBeenLastCalledWith("Previews play on Headphones again.", expect.anything());
  });

  it("returns to the default and says why when setSinkId refuses", async () => {
    const { select, preview, announce, storage } = setup();
    await settle();
    preview.setSinkId.mockImplementation(async function (id) {
      if (id) throw Object.assign(new Error("raw browser text"), { name: "NotAllowedError" });
      this.sinkId = id;
    });
    select.value = "phones";
    select.dispatchEvent(new Event("change"));
    await settle();
    expect(preview.sinkId).toBe("");
    expect(select.value).toBe("");
    expect(storage.store.has(PREVIEW_SINK_KEY)).toBe(false);
    const [message, options] = announce.mock.lastCall;
    expect(message).toContain("Could not use Headphones for previews: the browser did not allow it.");
    expect(message).not.toContain("raw browser text");
    expect(options).toMatchObject({ tone: "error", force: true });
  });

  it("explains a denied microphone prompt and keeps numbered devices usable", async () => {
    const unnamed = [
      { kind: "audiooutput", deviceId: "a", label: "" },
      { kind: "audiooutput", deviceId: "b", label: "" },
    ];
    const md = fakeMediaDevices(unnamed, {
      getUserMedia: vi.fn(async () => {
        throw Object.assign(new Error("denied"), { name: "NotAllowedError" });
      }),
    });
    const { button, note, select, announce, preview } = setup({ mediaDevices: md });
    await settle();
    expect(button.hidden).toBe(false);
    expect([...select.options].map((option) => option.textContent))
      .toEqual(["System default", "Output device 1", "Output device 2"]);

    button.focus();
    button.click();
    await settle();
    expect(note.textContent).toBe(DENIED_TEXT);
    expect(announce).toHaveBeenLastCalledWith(DENIED_TEXT, expect.objectContaining({ tone: "error" }));
    expect(document.activeElement).toBe(button);

    select.value = "b";
    select.dispatchEvent(new Event("change"));
    await settle();
    expect(preview.sinkId).toBe("b");
  });

  it("shows names after the microphone prompt and keeps focus on the button", async () => {
    let granted = false;
    const md = fakeMediaDevices([], {
      enumerateDevices: vi.fn(async () => [
        { kind: "audiooutput", deviceId: "phones", label: granted ? "Headphones" : "" },
      ]),
      getUserMedia: vi.fn(async () => {
        granted = true;
        return { getTracks: () => [{ stop: vi.fn() }] };
      }),
    });
    const { button, select, announce } = setup({ mediaDevices: md });
    await settle();
    button.focus();
    button.click();
    await settle();
    expect(select.options[1].textContent).toBe("Headphones");
    expect(document.activeElement).toBe(button);
    expect(button.hidden).toBe(false);
    expect(announce.mock.lastCall[0]).toMatch(/^Device names are shown\./);
  });

  it("uses the browser's own picker where selectAudioOutput exists", async () => {
    const md = fakeMediaDevices([], {
      selectAudioOutput: vi.fn(async () => ({ kind: "audiooutput", deviceId: "phones", label: "Headphones" })),
    });
    const { button, select, preview, announce, storage } = setup({ mediaDevices: md });
    await settle();
    expect(button.textContent).toBe("Choose preview device");
    button.click();
    await settle();
    expect(md.selectAudioOutput).toHaveBeenCalled();
    expect(select.value).toBe("phones");
    expect(preview.sinkId).toBe("phones");
    expect(storage.store.get(PREVIEW_SINK_KEY)).toBe("phones");
    expect(announce).toHaveBeenLastCalledWith("Previews play on Headphones.", expect.anything());
  });

  it("changes nothing when the browser's picker is cancelled", async () => {
    const md = fakeMediaDevices(DEVICES, {
      selectAudioOutput: vi.fn(async () => {
        throw Object.assign(new Error("cancelled"), { name: "NotAllowedError" });
      }),
    });
    const { button, preview, announce } = setup({ mediaDevices: md });
    await settle();
    button.click();
    await settle();
    expect(preview.setSinkId).not.toHaveBeenCalledWith("phones");
    expect(announce.mock.lastCall[0])
      .toBe("No device was chosen.  Previews stay on the system default output.");
  });
});
