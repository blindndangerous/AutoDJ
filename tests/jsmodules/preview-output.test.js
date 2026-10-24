import { afterEach, describe, expect, it, vi } from "vitest";

const { groups } = vi.hoisted(() => ({ groups: [] }));
vi.mock("../../src/autodj/static/modules/settings-drafts.js", () => ({
  registerSettingsGroup: (spec) => {
    spec.baseline = JSON.stringify(spec.read());
    groups.push(spec);
    return { accept: (value) => spec.write(value), dirty: () => JSON.stringify(spec.read()) !== spec.baseline };
  },
  settingsDraftHolds: (control) => groups.some((spec) => spec.controls.includes(control)
    && JSON.stringify(spec.read()) !== spec.baseline),
}));

import {
  CHOOSE_DESC_TEXT,
  DENIED_TEXT,
  FOCUS_SETTLE_MS,
  MICROPHONE_BUSY_TEXT,
  MISSING_TEXT,
  NAMES_DESC_TEXT,
  NAMES_FAILED_TEXT,
  NO_MICROPHONE_TEXT,
  PREVIEW_LABEL_KEY,
  PREVIEW_SINK_KEY,
  UNAVAILABLE_TEXT,
  UNSUPPORTED_TEXT,
  installPreviewOutput,
  microphoneErrorText,
  outputChoices,
  previewOutputSupport,
} from "../../src/autodj/static/modules/preview-output.js";

const DEVICES = [
  { kind: "audioinput", deviceId: "mic", label: "Microphone" },
  { kind: "audiooutput", deviceId: "default", label: "Default - Speakers" },
  { kind: "audiooutput", deviceId: "speakers", label: "Speakers" },
  { kind: "audiooutput", deviceId: "phones", label: "Headphones" },
];
const SAVED_PHONES = { [PREVIEW_SINK_KEY]: "phones", [PREVIEW_LABEL_KEY]: "Headphones" };

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
    <select id="preview-device">
      <option value="">System default</option>
    </select>
    <button type="button" id="preview-device-choose">Show preview device names</button>
    <span id="preview-device-desc">Help <span id="preview-device-choose-desc"></span></span>
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
  for (let i = 0; i < 10; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function setup({
  devices, storage = memoryStorage(), mediaDevices, proto = supportedProto, focusWaitMs,
} = {}) {
  const dom = installDom();
  const md = mediaDevices || fakeMediaDevices(devices);
  const announce = vi.fn();
  const onRoute = vi.fn();
  const controls = installPreviewOutput({
    document,
    mediaDevices: md,
    mediaElementPrototype: proto,
    storage,
    announce,
    onRoute,
    ...(focusWaitMs === undefined ? {} : { focusWaitMs }),
  });
  return {
    ...dom,
    controls,
    md,
    announce,
    onRoute,
    storage,
    select: document.getElementById("preview-device"),
    button: document.getElementById("preview-device-choose"),
    note: document.getElementById("preview-device-note"),
  };
}

const optionTexts = (select) => [...select.options].map((option) => option.textContent);

function choose(select, value) {
  select.value = value;
  select.dispatchEvent(new Event("change"));
}

async function saveChoice(select) {
  const draft = groups.find((group) => group.controls.includes(select));
  await draft.commit(draft.read());
  await settle();
}

afterEach(() => {
  groups.length = 0;
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

describe("microphoneErrorText", () => {
  it("says what each getUserMedia failure means", () => {
    const named = (name) => Object.assign(new Error("raw"), { name });
    expect(microphoneErrorText(named("NotAllowedError"))).toBe(DENIED_TEXT);
    expect(microphoneErrorText(named("SecurityError"))).toBe(DENIED_TEXT);
    expect(microphoneErrorText(named("NotFoundError"))).toBe(NO_MICROPHONE_TEXT);
    expect(microphoneErrorText(named("NotReadableError"))).toBe(MICROPHONE_BUSY_TEXT);
    expect(microphoneErrorText(named("AbortError"))).toBe(MICROPHONE_BUSY_TEXT);
    expect(microphoneErrorText(new TypeError("raw"))).toBe(NAMES_FAILED_TEXT);
  });
});

describe("installPreviewOutput", () => {
  it("lists devices and routes only the preview player to the chosen one", async () => {
    const { select, preview, deck, announce, storage, md, onRoute } = setup();
    await settle();
    expect(optionTexts(select)).toEqual(["System default", "Speakers", "Headphones"]);
    // Nothing asks for the microphone until the button is pressed.
    expect(md.getUserMedia).not.toHaveBeenCalled();

    select.focus();
    choose(select, "phones");
    await settle();

    expect(preview.setSinkId).not.toHaveBeenCalled();
    expect(storage.store.has(PREVIEW_SINK_KEY)).toBe(false);
    const draft = groups.find((group) => group.controls.includes(select));
    await draft.commit(draft.read());
    await settle();

    expect(preview.setSinkId).toHaveBeenLastCalledWith("phones");
    expect(deck.setSinkId).not.toHaveBeenCalled();
    expect(storage.store.get(PREVIEW_SINK_KEY)).toBe("phones");
    expect(storage.store.get(PREVIEW_LABEL_KEY)).toBe("Headphones");
    expect(onRoute).toHaveBeenCalled();
    expect(announce).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(select);
  });

  it("restores the saved device on load without saying anything", async () => {
    const { select, preview, announce } = setup({ storage: memoryStorage(SAVED_PHONES) });
    await settle();
    expect(select.value).toBe("phones");
    expect(preview.sinkId).toBe("phones");
    expect(announce).not.toHaveBeenCalled();
  });

  it("keeps the preview on its saved device when a browser storage write fails", async () => {
    const throwing = {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
      removeItem: () => { throw new Error("blocked"); },
    };
    const { select, preview } = setup({ storage: throwing });
    await settle();
    choose(select, "phones");
    await expect(saveChoice(select)).rejects.toThrow("blocked");
    expect(preview.sinkId).toBe("");
    expect(select.value).toBe("phones");
  });

  it("explains in words and keeps the default output where setSinkId is missing", async () => {
    const { select, button, note, preview, announce } = setup({ proto: {} });
    await settle();
    expect(select.disabled).toBe(true);
    expect(button.hidden).toBe(true);
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe(UNSUPPORTED_TEXT);
    expect(select.hasAttribute("aria-describedby")).toBe(false);
    expect(preview.setSinkId).not.toHaveBeenCalled();
    expect(announce).not.toHaveBeenCalled();
  });

  it("keeps a removed device listed as not connected, then uses it again", async () => {
    const devices = [...DEVICES];
    const { select, preview, note, announce, storage, md } = setup({
      devices,
      storage: memoryStorage(SAVED_PHONES),
    });
    await settle();
    expect(preview.sinkId).toBe("phones");

    devices.splice(devices.findIndex((device) => device.deviceId === "phones"), 1);
    md.fire("devicechange");
    await settle();
    expect(preview.sinkId).toBe("");
    expect(optionTexts(select)).toEqual(["System default", "Speakers", "Headphones (not connected)"]);
    expect(select.value).toBe("phones");
    expect(note.textContent).toBe(MISSING_TEXT);
    expect(select.hasAttribute("aria-describedby")).toBe(false);
    expect(storage.store.get(PREVIEW_SINK_KEY)).toBe("phones");
    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce).toHaveBeenLastCalledWith(MISSING_TEXT, expect.anything());

    // A second devicechange while it is still gone says nothing more.
    md.fire("devicechange");
    await settle();
    expect(announce).toHaveBeenCalledTimes(1);

    devices.push({ kind: "audiooutput", deviceId: "phones", label: "Headphones" });
    md.fire("devicechange");
    await settle();
    expect(preview.sinkId).toBe("phones");
    expect(optionTexts(select)).toEqual(["System default", "Speakers", "Headphones"]);
    expect(select.value).toBe("phones");
    expect(note.hidden).toBe(true);
    expect(select.getAttribute("aria-describedby")).toBeNull();
    expect(announce).toHaveBeenLastCalledWith("Previews play on Headphones again.", expect.anything());
  });

  it("forgets a device that is not connected when System default is chosen", async () => {
    const devices = DEVICES.filter((device) => device.deviceId !== "phones");
    const { select, note, storage, announce } = setup({
      devices,
      storage: memoryStorage(SAVED_PHONES),
    });
    await settle();
    expect(select.value).toBe("phones");
    expect(note.textContent).toBe(MISSING_TEXT);
    // On page load the note on the page is enough.
    expect(announce).not.toHaveBeenCalled();

    choose(select, "");
    await saveChoice(select);
    expect(storage.store.has(PREVIEW_SINK_KEY)).toBe(false);
    expect(storage.store.has(PREVIEW_LABEL_KEY)).toBe(false);
    expect(optionTexts(select)).toEqual(["System default", "Speakers"]);
    expect(note.hidden).toBe(true);
    expect(announce).not.toHaveBeenCalled();
  });

  it("announces a missing device once when refreshes overlap", async () => {
    const devices = [...DEVICES];
    const { controls, announce, md } = setup({ devices, storage: memoryStorage(SAVED_PHONES) });
    await settle();
    devices.splice(devices.findIndex((device) => device.deviceId === "phones"), 1);
    md.fire("devicechange");
    md.fire("devicechange");
    await Promise.all([controls.refresh(), controls.refresh()]);
    await settle();
    expect(announce.mock.calls.filter(([text]) => text === MISSING_TEXT)).toHaveLength(1);
  });

  it("treats an unreadable or nameless device list as unknown, not missing", async () => {
    const failing = fakeMediaDevices([], {
      enumerateDevices: vi.fn(async () => { throw new Error("no"); }),
    });
    const first = setup({ mediaDevices: failing, storage: memoryStorage(SAVED_PHONES) });
    await settle();
    expect(first.note.hidden).toBe(true);
    expect(first.announce).not.toHaveBeenCalled();
    // The saved device is still tried: the browser may well accept it.
    expect(first.preview.sinkId).toBe("phones");
    expect(first.select.value).toBe("phones");

    const nameless = [{ kind: "audiooutput", deviceId: "x", label: "" }];
    const second = setup({ devices: nameless, storage: memoryStorage(SAVED_PHONES) });
    await settle();
    second.md.fire("devicechange");
    await settle();
    expect(second.note.textContent).not.toBe(MISSING_TEXT);
    expect(second.announce).not.toHaveBeenCalled();
  });

  it("returns to the default and reports a refused choice through the settings save status", async () => {
    const { select, preview, announce, storage, note } = setup();
    await settle();
    preview.setSinkId.mockImplementation(async function (id) {
      if (id) throw Object.assign(new Error("raw browser text"), { name: "NotAllowedError" });
      this.sinkId = id;
    });
    choose(select, "phones");
    const draft = groups.find((group) => group.controls.includes(select));
    await expect(draft.commit(draft.read())).rejects.toThrow(
      "Could not use Headphones for previews: the browser did not allow it.",
    );
    await settle();
    expect(preview.sinkId).toBe("");
    expect(select.value).toBe("phones");
    expect(storage.store.has(PREVIEW_SINK_KEY)).toBe(false);
    expect(note.textContent).toContain("Could not use Headphones for previews: the browser did not allow it.");
    expect(note.textContent).not.toContain("raw browser text");
    expect(announce).not.toHaveBeenCalled();
  });

  it("falls back once when setSinkId refuses the saved device on a devicechange", async () => {
    const { preview, note, announce, storage, md, select } = setup({
      storage: memoryStorage(SAVED_PHONES),
    });
    await settle();
    expect(preview.sinkId).toBe("phones");
    preview.setSinkId.mockImplementation(async function (id) {
      if (id) throw Object.assign(new Error("gone"), { name: "NotFoundError" });
      this.sinkId = id;
    });
    preview.sinkId = "";
    md.fire("devicechange");
    await settle();
    md.fire("devicechange");
    await settle();
    expect(preview.sinkId).toBe("");
    expect(note.textContent).toBe(UNAVAILABLE_TEXT);
    expect(storage.store.get(PREVIEW_SINK_KEY)).toBe("phones");
    expect(select.value).toBe("phones");
    expect(announce.mock.calls.filter(([text]) => text === UNAVAILABLE_TEXT)).toHaveLength(1);
  });

  it("lets the user's newer choice win over a slow refresh", async () => {
    let release;
    const { select, preview, md } = setup({ storage: memoryStorage(SAVED_PHONES) });
    await settle();
    md.enumerateDevices.mockImplementationOnce(() => new Promise((resolve) => {
      release = () => resolve(DEVICES);
    }));
    md.fire("devicechange");
    await settle();
    choose(select, "speakers");
    await settle();
    release();
    await settle();
    await saveChoice(select);
    expect(preview.sinkId).toBe("speakers");
    expect(preview.setSinkId).toHaveBeenLastCalledWith("speakers");
    expect(select.value).toBe("speakers");
  });

  it("says why device names could not be shown, and numbered devices still work", async () => {
    const unnamed = [
      { kind: "audiooutput", deviceId: "a", label: "" },
      { kind: "audiooutput", deviceId: "b", label: "" },
    ];
    for (const [name, text] of [
      ["NotAllowedError", DENIED_TEXT],
      ["NotFoundError", NO_MICROPHONE_TEXT],
      ["NotReadableError", MICROPHONE_BUSY_TEXT],
    ]) {
      const md = fakeMediaDevices(unnamed, {
        getUserMedia: vi.fn(async () => {
          throw Object.assign(new Error("raw"), { name });
        }),
      });
      const { button, note, select, announce, preview } = setup({ mediaDevices: md });
      await settle();
      expect(button.hidden).toBe(false);
      expect(button.textContent).toBe("Show preview device names");
      expect(optionTexts(select)).toEqual(["System default", "Output device 1", "Output device 2"]);

      button.focus();
      button.click();
      await settle();
      expect(note.textContent).toBe(text);
      expect(select.hasAttribute("aria-describedby")).toBe(false);
      expect(announce).toHaveBeenLastCalledWith(text, expect.objectContaining({ tone: "error" }));
      expect(document.activeElement).toBe(button);

      choose(select, "b");
      await settle();
      await saveChoice(select);
      expect(preview.sinkId).toBe("b");
    }
  });

  it("stops every microphone track, shows names and keeps focus on the button", async () => {
    let granted = false;
    const tracks = [{ stop: vi.fn() }, { stop: vi.fn() }];
    const md = fakeMediaDevices([], {
      enumerateDevices: vi.fn(async () => [
        { kind: "audiooutput", deviceId: "phones", label: granted ? "Headphones" : "" },
      ]),
      getUserMedia: vi.fn(async () => {
        granted = true;
        return { getTracks: () => tracks };
      }),
    });
    const { button, select, announce } = setup({ mediaDevices: md });
    await settle();
    expect(md.getUserMedia).not.toHaveBeenCalled();
    button.focus();
    button.click();
    await settle();
    expect(md.getUserMedia).toHaveBeenCalledTimes(1);
    for (const track of tracks) expect(track.stop).toHaveBeenCalledTimes(1);
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
    expect(md.getUserMedia).not.toHaveBeenCalled();
    expect(select.value).toBe("phones");
    expect(preview.sinkId).toBe("");
    expect(storage.store.has(PREVIEW_SINK_KEY)).toBe(false);
    expect(announce).toHaveBeenLastCalledWith("Preview device selected. Save sound settings to apply it.", expect.anything());
    const draft = groups.find((group) => group.controls.includes(select));
    await draft.commit(draft.read());
    await settle();
    expect(preview.sinkId).toBe("phones");
    expect(storage.store.get(PREVIEW_SINK_KEY)).toBe("phones");
    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce).toHaveBeenLastCalledWith(
      "Preview device selected. Save sound settings to apply it.", expect.anything(),
    );
  });

  it("keeps a picked device the list leaves out, without calling it missing", async () => {
    // Named devices are listed, but not the one the picker granted.
    const md = fakeMediaDevices(DEVICES.filter((device) => device.deviceId !== "phones"), {
      selectAudioOutput: vi.fn(async () => ({ kind: "audiooutput", deviceId: "phones", label: "Headphones" })),
    });
    const { button, select, preview, note } = setup({ mediaDevices: md });
    await settle();
    button.click();
    await settle();
    md.fire("devicechange");
    await settle();
    expect(optionTexts(select)).toEqual(["System default", "Speakers", "Headphones"]);
    expect(select.value).toBe("phones");
    expect(preview.sinkId).toBe("");
    const draft = groups.find((group) => group.controls.includes(select));
    await draft.commit(draft.read());
    await settle();
    expect(preview.sinkId).toBe("phones");
    expect(note.hidden).toBe(true);
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

  it("describes the button by what it does in each browser", async () => {
    const names = setup({ devices: [{ kind: "audiooutput", deviceId: "a", label: "" }] });
    await settle();
    const desc = () => document.getElementById("preview-device-choose-desc");
    expect(names.button.hasAttribute("aria-describedby")).toBe(false);
    expect(desc().textContent).toBe(NAMES_DESC_TEXT);
    expect(desc().hidden).toBe(false);

    setup({ mediaDevices: fakeMediaDevices(DEVICES, { selectAudioOutput: vi.fn() }) });
    await settle();
    expect(desc().textContent).toBe(CHOOSE_DESC_TEXT);

    // Names all shown: the button goes, and so does its sentence.
    setup();
    await settle();
    expect(document.getElementById("preview-device-choose").hidden).toBe(true);
    expect(desc().hidden).toBe(true);

    // No button, no sentence about it in the select's help.
    setup({ proto: {} });
    await settle();
    expect(desc().hidden).toBe(true);
  });

  it("waits for focus to come back from the picker before saying it was cancelled", async () => {
    const md = fakeMediaDevices(DEVICES, {
      selectAudioOutput: vi.fn(async () => {
        throw Object.assign(new Error("cancelled"), { name: "NotAllowedError" });
      }),
    });
    const { button, announce } = setup({ mediaDevices: md });
    await settle();
    const hasFocus = vi.spyOn(document, "hasFocus").mockReturnValue(false);
    button.click();
    await settle();
    expect(announce).not.toHaveBeenCalled();

    hasFocus.mockReturnValue(true);
    window.dispatchEvent(new Event("focus"));
    await new Promise((resolve) => setTimeout(resolve, FOCUS_SETTLE_MS + 50));
    await settle();
    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce.mock.lastCall[0]).toMatch(/^No device was chosen\./);
  });

  it("stages a picked device and waits for focus before saying it is selected", async () => {
    const md = fakeMediaDevices(DEVICES, {
      selectAudioOutput: vi.fn(async () => ({ kind: "audiooutput", deviceId: "phones", label: "Headphones" })),
    });
    const { button, announce, preview } = setup({ mediaDevices: md });
    await settle();
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    button.click();
    await settle();
    expect(preview.sinkId).toBe("");
    expect(announce).not.toHaveBeenCalled();

    window.dispatchEvent(new Event("focus"));
    await new Promise((resolve) => setTimeout(resolve, FOCUS_SETTLE_MS + 50));
    await settle();
    expect(announce).toHaveBeenLastCalledWith(
      "Preview device selected. Save sound settings to apply it.", expect.anything(),
    );
    const select = document.getElementById("preview-device");
    const draft = groups.find((group) => group.controls.includes(select));
    await draft.commit(draft.read());
    await settle();
    expect(preview.sinkId).toBe("phones");
    expect(announce).toHaveBeenCalledTimes(1);
    expect(announce).toHaveBeenLastCalledWith(
      "Preview device selected. Save sound settings to apply it.", expect.anything(),
    );
  });

  it("says it anyway if focus never comes back", async () => {
    const md = fakeMediaDevices(DEVICES, {
      selectAudioOutput: vi.fn(async () => {
        throw Object.assign(new Error("cancelled"), { name: "AbortError" });
      }),
    });
    const { button, announce } = setup({ mediaDevices: md, focusWaitMs: 20 });
    await settle();
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    button.click();
    await new Promise((resolve) => setTimeout(resolve, 20 + FOCUS_SETTLE_MS + 50));
    await settle();
    expect(announce).toHaveBeenCalledTimes(1);
  });

  it("waits for focus after the microphone prompt too", async () => {
    const md = fakeMediaDevices([{ kind: "audiooutput", deviceId: "a", label: "" }], {
      getUserMedia: vi.fn(async () => {
        throw Object.assign(new Error("denied"), { name: "NotAllowedError" });
      }),
    });
    const { button, announce, note } = setup({ mediaDevices: md });
    await settle();
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    button.click();
    await settle();
    // The words are on the page at once; only the spoken status waits.
    expect(note.textContent).toBe(DENIED_TEXT);
    expect(announce).not.toHaveBeenCalled();
    window.dispatchEvent(new Event("focus"));
    await new Promise((resolve) => setTimeout(resolve, FOCUS_SETTLE_MS + 50));
    await settle();
    expect(announce).toHaveBeenLastCalledWith(DENIED_TEXT, expect.anything());
  });
});
