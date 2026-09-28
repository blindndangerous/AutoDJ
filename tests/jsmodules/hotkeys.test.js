import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { installHotkeys } from "../../src/autodj/static/modules/hotkeys.js";

function keyEvent(target, key, options = {}) {
  const event = new window.KeyboardEvent("keydown", {
    key,
    bubbles: true,
    cancelable: true,
    ...options,
  });
  target.dispatchEvent(event);
  return event;
}

describe("native keyboard ownership", () => {
  let togglePlay;
  let keydown;
  let keyup;

  beforeAll(() => {
    document.body.innerHTML = '<section id="panel-now"></section>';
    togglePlay = vi.fn();
    // Capture the handlers without registering them, so they cannot latch
    // keys for the installs in later blocks.
    const addEventListener = vi
      .spyOn(window, "addEventListener")
      .mockImplementation(() => {});
    installHotkeys({ togglePlay });
    const handler = (type) => addEventListener.mock.calls.find(([t]) => t === type)[1];
    keydown = handler("keydown");
    keyup = handler("keyup");
    addEventListener.mockRestore();
  });

  // Whether Space pressed on *target* reaches the Play / Pause shortcut
  // (false when the target owns Space natively).
  function spaceReachesShortcut(target) {
    togglePlay.mockClear();
    keydown({
      key: " ",
      repeat: false,
      target,
      composedPath: () => [target],
      preventDefault: vi.fn(),
    });
    keyup({ key: " " });
    return togglePlay.mock.calls.length === 1;
  }

  it("recognizes native controls and their nested content", () => {
    const examples = [
      '<button><span data-target>Button label</span></button>',
      '<input data-target type="range">',
      '<select data-target><option>Choice</option></select>',
      '<textarea data-target></textarea>',
      '<a href="/library"><span data-target>Library</span></a>',
      '<details><summary><span data-target>More</span></summary></details>',
      '<div contenteditable="true"><span data-target>Edit</span></div>',
    ];

    for (const html of examples) {
      const host = document.createElement("div");
      host.innerHTML = html;
      expect(spaceReachesShortcut(host.querySelector("[data-target]"))).toBe(false);
    }
  });

  it("recognizes nested content in supported ARIA widgets", () => {
    const roles = [
      "button", "slider", "spinbutton", "combobox", "listbox",
      "menuitem", "option", "switch", "tab",
    ];

    for (const role of roles) {
      const widget = document.createElement("div");
      widget.setAttribute("role", role);
      const child = document.createElement("span");
      widget.appendChild(child);
      expect(spaceReachesShortcut(child)).toBe(false);
    }
  });

  it("treats non-elements and plain content as the page's", () => {
    const plain = document.createElement("div");
    expect(spaceReachesShortcut(null)).toBe(true);
    expect(spaceReachesShortcut(document)).toBe(true);
    expect(spaceReachesShortcut(document.createTextNode("text"))).toBe(true);
    expect(spaceReachesShortcut(plain)).toBe(true);
    expect(spaceReachesShortcut(document.createElement("a"))).toBe(true);
    expect(spaceReachesShortcut({ closest: () => plain })).toBe(true);
  });

  it("recognizes cross-realm-like elements and handles invalid closest safely", () => {
    const crossRealmButton = {
      nodeType: 1,
      closest: () => ({ role: "button" }),
    };
    const invalidElement = {
      nodeType: 1,
      closest: () => { throw new TypeError("invalid selector context"); },
    };

    expect(spaceReachesShortcut(crossRealmButton)).toBe(false);
    expect(spaceReachesShortcut(invalidElement)).toBe(true);
  });
});

describe("page shortcut scope", () => {
  let togglePlay;
  let skipClick;
  let shuffleClick;
  let muteClick;
  let volumeInput;
  let seekDelta;

  beforeAll(() => {
    document.body.innerHTML = `
      <section id="panel-now"></section>
      <dialog id="hotkey-help-modal">
        <button id="btn-shortcuts-close"><span id="dialog-close-label">Close</span></button>
      </dialog>
      <dialog id="confirm-dialog">
        <button id="confirm-cancel">Cancel</button>
      </dialog>
      <button id="btn-shortcuts">Keyboard shortcuts</button>
      <button id="native-button"><span id="native-button-label">Pause</span></button>
      <input id="volume" type="range" value="50">
      <select id="native-select"><option>Native choice</option></select>
      <div role="tab" id="native-tab"><span id="native-tab-label">Tab</span></div>
      <div role="slider" id="custom-seek-slider" tabindex="0"></div>
      <div id="shadow-host" tabindex="0">Shadow host</div>
      <div id="plain" tabindex="0">Plain content</div>
      <button id="page-skip">Page skip</button>
      <button id="page-shuffle">Shuffle</button>
      <button id="page-mute">Mute</button>
    `;
    const modal = document.querySelector("#hotkey-help-modal");
    modal.showModal = vi.fn(() => modal.setAttribute("open", ""));
    modal.close = vi.fn(() => modal.removeAttribute("open"));
    const skip = document.querySelector("#page-skip");
    const shuffle = document.querySelector("#page-shuffle");
    const mute = document.querySelector("#page-mute");
    const volume = document.querySelector("#volume");
    togglePlay = vi.fn();
    skipClick = vi.spyOn(skip, "click");
    shuffleClick = vi.spyOn(shuffle, "click");
    muteClick = vi.spyOn(mute, "click");
    seekDelta = vi.fn();
    volumeInput = vi.fn();
    volume.addEventListener("input", volumeInput);
    installHotkeys({
      togglePlay,
      btnSkip: skip,
      btnShuffle: shuffle,
      btnMute: mute,
      volSlider: volume,
      seekDelta,
      getBpm: () => 120,
    });
  });

  beforeEach(() => {
    togglePlay.mockClear();
    skipClick.mockClear();
    shuffleClick.mockClear();
    muteClick.mockClear();
    seekDelta.mockClear();
    volumeInput.mockClear();
    document.querySelector("#volume").value = "50";
    document.querySelector("#panel-now").removeAttribute("hidden");
    document.querySelector("#hotkey-help-modal").removeAttribute("open");
    window.dispatchEvent(new Event("blur"));
  });

  it("leaves button Space, range arrows, and select letters native", () => {
    const buttonEvent = keyEvent(
      document.querySelector("#native-button-label"), " ",
    );
    const rangeUp = keyEvent(document.querySelector("#volume"), "ArrowUp");
    const rangeDown = keyEvent(document.querySelector("#volume"), "ArrowDown");
    const selectEvent = keyEvent(document.querySelector("#native-select"), "n");
    const plainSkip = keyEvent(document.querySelector("#plain"), "n");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "n" }));

    expect(buttonEvent.defaultPrevented).toBe(false);
    expect(rangeUp.defaultPrevented).toBe(false);
    expect(rangeDown.defaultPrevented).toBe(false);
    expect(selectEvent.defaultPrevented).toBe(false);
    expect(plainSkip.defaultPrevented).toBe(true);
    expect(togglePlay).not.toHaveBeenCalled();
    expect(skipClick).toHaveBeenCalledOnce();
    expect(document.querySelector("#volume").value).toBe("50");
    expect(volumeInput).not.toHaveBeenCalled();
  });

  it("does not latch keys from a dialog button or nested tab widget", () => {
    document.querySelector("#hotkey-help-modal").setAttribute("open", "");
    const dialogEvent = keyEvent(
      document.querySelector("#dialog-close-label"), "n",
    );
    document.querySelector("#hotkey-help-modal").close();
    const plainSkip = keyEvent(document.querySelector("#plain"), "n");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "n" }));

    const tabEvent = keyEvent(
      document.querySelector("#native-tab-label"), "ArrowUp",
    );
    const plainVolume = keyEvent(document.querySelector("#plain"), "ArrowUp");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "ArrowUp" }));

    expect(dialogEvent.defaultPrevented).toBe(false);
    expect(plainSkip.defaultPrevented).toBe(true);
    expect(skipClick).toHaveBeenCalledOnce();
    expect(tabEvent.defaultPrevented).toBe(false);
    expect(plainVolume.defaultPrevented).toBe(true);
    expect(document.querySelector("#volume").value).toBe("55");
    expect(volumeInput).toHaveBeenCalledOnce();
  });

  it("allows skip and seek shortcuts while a native button has focus", () => {
    document.querySelector("#hotkey-help-modal").close();
    const button = document.querySelector("#native-button");
    button.focus();

    const skipEvent = keyEvent(document.activeElement, "n");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "n" }));
    const seekBack = keyEvent(document.activeElement, ",");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "," }));
    const seekForward = keyEvent(document.activeElement, ".");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "." }));

    expect(skipEvent.defaultPrevented).toBe(true);
    expect(seekBack.defaultPrevented).toBe(true);
    expect(seekForward.defaultPrevented).toBe(true);
    expect(skipClick).toHaveBeenCalledOnce();
    expect(seekDelta.mock.calls).toEqual([[-2], [2]]);
  });

  it("allows seek shortcuts from a custom ARIA slider while preserving its arrows", () => {
    document.querySelector("#hotkey-help-modal").close();
    const slider = document.querySelector("#custom-seek-slider");
    slider.focus();

    const skipEvent = keyEvent(document.activeElement, "n");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "n" }));
    const seekBack = keyEvent(document.activeElement, ",");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "," }));
    const seekForward = keyEvent(document.activeElement, ".");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "." }));
    const arrowUp = keyEvent(document.activeElement, "ArrowUp");

    expect(skipEvent.defaultPrevented).toBe(true);
    expect(seekBack.defaultPrevented).toBe(true);
    expect(seekForward.defaultPrevented).toBe(true);
    expect(arrowUp.defaultPrevented).toBe(false);
    expect(skipClick).toHaveBeenCalledOnce();
    expect(seekDelta.mock.calls).toEqual([[-2], [2]]);
    expect(document.querySelector("#volume").value).toBe("50");
    expect(volumeInput).not.toHaveBeenCalled();
  });

  it("allows mute from a focused range without taking over its arrow keys", () => {
    document.querySelector("#hotkey-help-modal").close();
    const volume = document.querySelector("#volume");
    volume.focus();

    const muteEvent = keyEvent(document.activeElement, "m");
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "m" }));
    const arrowUp = keyEvent(document.activeElement, "ArrowUp");

    expect(muteEvent.defaultPrevented).toBe(true);
    expect(muteClick).toHaveBeenCalledOnce();
    expect(arrowUp.defaultPrevented).toBe(false);
    expect(document.querySelector("#volume").value).toBe("50");
    expect(volumeInput).not.toHaveBeenCalled();
  });

  it("unlatches a key whose keyup arrives as Unidentified through NVDA", () => {
    document.querySelector("#hotkey-help-modal").close();
    const plain = document.querySelector("#plain");

    for (let press = 0; press < 3; press += 1) {
      keyEvent(plain, "m");
      window.dispatchEvent(new KeyboardEvent("keyup", { key: "Unidentified" }));
    }
    expect(muteClick).toHaveBeenCalledTimes(3);

    // A held key still fires once until some key is released.
    keyEvent(plain, "m");
    keyEvent(plain, "m");
    expect(muteClick).toHaveBeenCalledTimes(4);
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "Unidentified" }));
  });

  it("opens and closes help with ? from focused shortcut buttons", () => {
    const modal = document.querySelector("#hotkey-help-modal");
    const helpButton = document.querySelector("#btn-shortcuts");
    modal.close();
    helpButton.focus();

    const openEvent = keyEvent(document.activeElement, "?", { shiftKey: true });
    expect(openEvent.defaultPrevented).toBe(true);
    expect(modal.open).toBe(true);
    expect(document.activeElement).toBe(
      document.querySelector("#btn-shortcuts-close"),
    );

    window.dispatchEvent(new KeyboardEvent("keyup", { key: "?" }));
    const closeEvent = keyEvent(document.activeElement, "?", { shiftKey: true });
    expect(closeEvent.defaultPrevented).toBe(true);
    expect(modal.open).toBe(false);
  });

  it("works from any tab except Space and the volume arrows", () => {
    document.querySelector("#hotkey-help-modal").close();
    document.querySelector("#panel-now").setAttribute("hidden", "");
    const settingsTabLabel = document.querySelector("#native-tab-label");

    for (const key of ["T", "N", "R", "B", "K", "L", "E", "V", "Q", "J"]) {
      const event = keyEvent(settingsTabLabel, key, { shiftKey: true });
      expect(event.defaultPrevented).toBe(true);
      window.dispatchEvent(new KeyboardEvent("keyup", { key }));
    }

    const skipEvent = keyEvent(settingsTabLabel, "n");
    const seekEvent = keyEvent(settingsTabLabel, ",");
    expect(skipEvent.defaultPrevented).toBe(true);
    expect(seekEvent.defaultPrevented).toBe(true);
    expect(skipClick).toHaveBeenCalledOnce();
    expect(seekDelta).toHaveBeenCalledOnce();

    // Off Now Playing these scroll the page, so they stay the page's.
    const plain = document.querySelector("#plain");
    const arrowEvent = keyEvent(plain, "ArrowDown");
    const spaceEvent = keyEvent(plain, " ");
    expect(arrowEvent.defaultPrevented).toBe(false);
    expect(spaceEvent.defaultPrevented).toBe(false);
    expect(volumeInput).not.toHaveBeenCalled();
    expect(togglePlay).not.toHaveBeenCalled();
  });

  it("keeps every shortcut inactive while another dialog is open", () => {
    const confirm = document.querySelector("#confirm-dialog");
    confirm.setAttribute("open", "");
    const cancel = document.querySelector("#confirm-cancel");
    const plain = document.querySelector("#plain");

    for (const [target, key, init] of [
      [cancel, "n"], [cancel, "k"], [cancel, "m"], [cancel, "s"], [cancel, ","],
      [cancel, "T", { shiftKey: true }], [cancel, "?", { shiftKey: true }],
      [plain, "n"], [plain, " "], [plain, "ArrowUp"],
    ]) {
      const event = keyEvent(target, key, init);
      expect(event.defaultPrevented).toBe(false);
      window.dispatchEvent(new KeyboardEvent("keyup", { key }));
    }
    expect(skipClick).not.toHaveBeenCalled();
    expect(togglePlay).not.toHaveBeenCalled();
    expect(muteClick).not.toHaveBeenCalled();
    expect(shuffleClick).not.toHaveBeenCalled();
    expect(seekDelta).not.toHaveBeenCalled();
    expect(volumeInput).not.toHaveBeenCalled();
    expect(document.querySelector("#hotkey-help-modal").open).toBe(false);

    confirm.removeAttribute("open");
    expect(keyEvent(plain, "n").defaultPrevented).toBe(true);
    expect(skipClick).toHaveBeenCalledOnce();
  });

  it("does not latch keys suppressed inside the dialog when their keyup is missed", () => {
    const modal = document.querySelector("#hotkey-help-modal");
    const closeButton = document.querySelector("#btn-shortcuts-close");
    modal.setAttribute("open", "");
    closeButton.focus();

    const dialogEvent = keyEvent(document.activeElement, "n");
    modal.close();
    const pageEvent = keyEvent(document.querySelector("#plain"), "n");

    expect(dialogEvent.defaultPrevented).toBe(false);
    expect(pageEvent.defaultPrevented).toBe(true);
    expect(skipClick).toHaveBeenCalledOnce();
  });

  it("uses the composed path for shadow-native ownership without latching", () => {
    const host = document.querySelector("#shadow-host");
    const localToggle = vi.fn();
    const crossRealmButton = {
      nodeType: 1,
      closest: () => ({ role: "button" }),
    };
    const addEventListener = vi.spyOn(window, "addEventListener");
    installHotkeys({ togglePlay: localToggle });
    const keydownHandler = addEventListener.mock.calls.find(
      ([type]) => type === "keydown",
    )[1];
    addEventListener.mockRestore();
    const shadowEvent = {
      key: " ",
      repeat: false,
      target: host,
      composedPath: () => [crossRealmButton, host, document.body, window],
      preventDefault: vi.fn(),
    };
    const plainEvent = {
      key: " ",
      repeat: false,
      target: document.querySelector("#plain"),
      composedPath: () => [document.querySelector("#plain"), document.body, window],
      preventDefault: vi.fn(),
    };

    keydownHandler(shadowEvent);
    keydownHandler(plainEvent);
    window.dispatchEvent(new KeyboardEvent("keyup", { key: " " }));

    expect(shadowEvent.preventDefault).not.toHaveBeenCalled();
    expect(plainEvent.preventDefault).toHaveBeenCalledOnce();
    expect(localToggle).toHaveBeenCalledOnce();
  });

  it("sends Space and k to togglePlay", () => {
    const togglePlay = vi.fn();
    const addEventListener = vi.spyOn(window, "addEventListener");
    installHotkeys({ togglePlay });
    const keydownHandler = addEventListener.mock.calls.find(
      ([type]) => type === "keydown",
    )[1];
    addEventListener.mockRestore();
    const plain = document.querySelector("#plain");

    for (const key of [" ", "k"]) {
      const event = {
        key,
        repeat: false,
        target: plain,
        composedPath: () => [plain, document.body, window],
        preventDefault: vi.fn(),
      };
      keydownHandler(event);
      window.dispatchEvent(new KeyboardEvent("keyup", { key }));
      expect(event.preventDefault).toHaveBeenCalledOnce();
    }

    expect(togglePlay).toHaveBeenCalledTimes(2);
  });

  it("registers the page keydown handler in capture phase", () => {
    const addEventListener = vi.spyOn(window, "addEventListener");
    installHotkeys({});
    const registration = addEventListener.mock.calls.find(
      ([type]) => type === "keydown",
    );
    addEventListener.mockRestore();

    expect(registration[2]).toBe(true);
  });

  it.each(["", "plaintext-only"])(
    "uses the composed path for contenteditable=%s without latching",
    (contentEditable) => {
      const host = document.querySelector("#shadow-host");
      const editor = document.createElement("div");
      editor.setAttribute("contenteditable", contentEditable);
      Object.defineProperty(editor, "isContentEditable", { value: true });
      const localSkip = document.createElement("button");
      const localSkipClick = vi.spyOn(localSkip, "click");
      const addEventListener = vi.spyOn(window, "addEventListener");
      installHotkeys({ btnSkip: localSkip });
      const keydownHandler = addEventListener.mock.calls.find(
        ([type]) => type === "keydown",
      )[1];
      addEventListener.mockRestore();
      const editorEvent = {
        key: "n",
        repeat: false,
        target: host,
        composedPath: () => [editor, host, document.body, window],
        preventDefault: vi.fn(),
      };
      const plainEvent = {
        key: "n",
        repeat: false,
        target: document.querySelector("#plain"),
        composedPath: () => [document.querySelector("#plain"), document.body, window],
        preventDefault: vi.fn(),
      };

      keydownHandler(editorEvent);
      keydownHandler(plainEvent);
      window.dispatchEvent(new KeyboardEvent("keyup", { key: "n" }));

      expect(editorEvent.preventDefault).not.toHaveBeenCalled();
      expect(plainEvent.preventDefault).toHaveBeenCalledOnce();
      expect(localSkipClick).toHaveBeenCalledOnce();
    },
  );
});

describe("spoken status keys", () => {
  it("speaks the key in words, never the display label", async () => {
    document.body.innerHTML = '<div id="sr-status"></div><section id="panel-now"></section>';
    const addEventListener = vi.spyOn(window, "addEventListener");
    installHotkeys({
      getTrack: () => ({ key_label: "F#m", key_spoken: "F sharp minor" }),
    });
    const keydownHandler = addEventListener.mock.calls.find(
      ([type]) => type === "keydown",
    )[1];
    addEventListener.mockRestore();

    keydownHandler({
      key: "K", repeat: false, target: document.body, preventDefault: vi.fn(),
    });
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "K" }));

    await vi.waitFor(() => expect(document.querySelector("#sr-status").textContent)
      .toBe("F sharp minor"));
  });
});

describe("keyboard shortcut toggle (WCAG 2.1.4)", () => {
  const STORAGE_KEY = "autodj.keyboardShortcuts";
  // Node's own experimental localStorage shadows happy-dom's here, so the
  // tests supply a plain in-memory Storage.
  function memoryStorage(initial = {}) {
    const values = new Map(Object.entries(initial));
    return {
      getItem: vi.fn((key) => (values.has(key) ? values.get(key) : null)),
      setItem: vi.fn((key, value) => values.set(key, String(value))),
    };
  }
  afterEach(() => vi.unstubAllGlobals());

  it("turns every page shortcut off and back on, and saves the choice", async () => {
    document.body.innerHTML = `
      <section id="panel-now"></section>
      <input type="checkbox" id="toggle" checked>
      <div id="toggle-plain" tabindex="0">Plain</div>`;
    const storage = memoryStorage();
    vi.stubGlobal("localStorage", storage);
    vi.resetModules();
    const hotkeys = await import("../../src/autodj/static/modules/hotkeys.js");
    const click = vi.fn();
    const toggle = document.querySelector("#toggle");
    const plain = document.querySelector("#toggle-plain");
    const press = (key) => {
      keyEvent(plain, key);
      window.dispatchEvent(new window.KeyboardEvent("keyup", { key }));
    };
    hotkeys.installHotkeys({ togglePlay: click, shortcutToggle: toggle });
    expect(toggle.checked).toBe(true);

    toggle.checked = false;
    toggle.dispatchEvent(new Event("change"));
    expect(storage.setItem).toHaveBeenLastCalledWith(STORAGE_KEY, "off");
    press("k");
    expect(click).not.toHaveBeenCalled();

    toggle.checked = true;
    toggle.dispatchEvent(new Event("change"));
    expect(storage.setItem).toHaveBeenLastCalledWith(STORAGE_KEY, "on");
    press("k");
    expect(click).toHaveBeenCalledOnce();
  });

  it("starts with the saved choice and defaults on when storage throws", async () => {
    vi.stubGlobal("localStorage", memoryStorage({ [STORAGE_KEY]: "off" }));
    vi.resetModules();
    const saved = await import("../../src/autodj/static/modules/hotkeys.js");
    const offToggle = document.createElement("input");
    offToggle.type = "checkbox";
    saved.installHotkeys({ shortcutToggle: offToggle });
    expect(offToggle.checked).toBe(false);

    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
    });
    vi.resetModules();
    const blocked = await import("../../src/autodj/static/modules/hotkeys.js");
    const onToggle = document.createElement("input");
    onToggle.type = "checkbox";
    blocked.installHotkeys({ shortcutToggle: onToggle });
    expect(onToggle.checked).toBe(true);
    onToggle.checked = false;
    expect(() => onToggle.dispatchEvent(new Event("change"))).not.toThrow();
  });
});
