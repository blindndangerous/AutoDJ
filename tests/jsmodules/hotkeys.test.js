import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import {
  installHotkeys,
  ownsNativeKeyboardBehavior,
} from "../../src/autodj/static/modules/hotkeys.js";

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
      expect(ownsNativeKeyboardBehavior(host.querySelector("[data-target]")))
        .toBe(true);
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
      expect(ownsNativeKeyboardBehavior(child)).toBe(true);
    }
  });

  it("rejects non-elements and plain content", () => {
    const plain = document.createElement("div");
    expect(ownsNativeKeyboardBehavior(null)).toBe(false);
    expect(ownsNativeKeyboardBehavior(document)).toBe(false);
    expect(ownsNativeKeyboardBehavior(document.createTextNode("text"))).toBe(false);
    expect(ownsNativeKeyboardBehavior(plain)).toBe(false);
    expect(ownsNativeKeyboardBehavior(document.createElement("a"))).toBe(false);
    expect(ownsNativeKeyboardBehavior({ closest: () => plain })).toBe(false);
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

    expect(ownsNativeKeyboardBehavior(crossRealmButton)).toBe(true);
    expect(ownsNativeKeyboardBehavior(invalidElement)).toBe(false);
  });

  it("keeps other contenteditable modes outside the exact native selector", () => {
    for (const value of ["", "plaintext-only"]) {
      const editor = document.createElement("div");
      editor.setAttribute("contenteditable", value);
      expect(ownsNativeKeyboardBehavior(editor)).toBe(false);
    }
  });
});

describe("page shortcut scope", () => {
  let pauseClick;
  let skipClick;
  let volumeInput;

  beforeAll(() => {
    document.body.innerHTML = `
      <section id="panel-now"></section>
      <dialog id="hotkey-help-modal" open>
        <button id="dialog-close"><span id="dialog-close-label">Close</span></button>
      </dialog>
      <button id="native-button"><span id="native-button-label">Pause</span></button>
      <input id="volume" type="range" value="50">
      <select id="native-select"><option>Native choice</option></select>
      <div role="tab" id="native-tab"><span id="native-tab-label">Tab</span></div>
      <div id="shadow-host" tabindex="0">Shadow host</div>
      <div id="plain" tabindex="0">Plain content</div>
      <button id="page-pause">Page pause</button>
      <button id="page-skip">Page skip</button>
    `;
    const pause = document.querySelector("#page-pause");
    const skip = document.querySelector("#page-skip");
    const volume = document.querySelector("#volume");
    pauseClick = vi.spyOn(pause, "click");
    skipClick = vi.spyOn(skip, "click");
    volumeInput = vi.fn();
    volume.addEventListener("input", volumeInput);
    installHotkeys({
      btnPause: pause,
      btnSkip: skip,
      volSlider: volume,
    });
  });

  beforeEach(() => {
    pauseClick.mockClear();
    skipClick.mockClear();
    volumeInput.mockClear();
    document.querySelector("#volume").value = "50";
    window.dispatchEvent(new Event("blur"));
  });

  it("leaves button Space, range arrows, and select letters native", () => {
    const buttonEvent = keyEvent(
      document.querySelector("#native-button-label"), " ",
    );
    const rangeUp = keyEvent(document.querySelector("#volume"), "ArrowUp");
    const rangeDown = keyEvent(document.querySelector("#volume"), "ArrowDown");
    const selectEvent = keyEvent(document.querySelector("#native-select"), "n");

    expect(buttonEvent.defaultPrevented).toBe(false);
    expect(rangeUp.defaultPrevented).toBe(false);
    expect(rangeDown.defaultPrevented).toBe(false);
    expect(selectEvent.defaultPrevented).toBe(false);
    expect(pauseClick).not.toHaveBeenCalled();
    expect(skipClick).not.toHaveBeenCalled();
    expect(document.querySelector("#volume").value).toBe("50");
    expect(volumeInput).not.toHaveBeenCalled();
  });

  it("does not latch keys from a dialog button or nested tab widget", () => {
    const dialogEvent = keyEvent(
      document.querySelector("#dialog-close-label"), "n",
    );
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

  it("uses the composed path for shadow-native ownership without latching", () => {
    const host = document.querySelector("#shadow-host");
    const localSkip = document.createElement("button");
    const localSkipClick = vi.spyOn(localSkip, "click");
    const crossRealmButton = {
      nodeType: 1,
      closest: () => ({ role: "button" }),
    };
    const addEventListener = vi.spyOn(window, "addEventListener");
    installHotkeys({ btnSkip: localSkip });
    const keydownHandler = addEventListener.mock.calls.find(
      ([type]) => type === "keydown",
    )[1];
    addEventListener.mockRestore();
    const shadowEvent = {
      key: "n",
      repeat: false,
      target: host,
      composedPath: () => [crossRealmButton, host, document.body, window],
      preventDefault: vi.fn(),
    };
    const plainEvent = {
      key: "n",
      repeat: false,
      target: document.querySelector("#plain"),
      composedPath: () => [document.querySelector("#plain"), document.body, window],
      preventDefault: vi.fn(),
    };

    keydownHandler(shadowEvent);
    keydownHandler(plainEvent);
    window.dispatchEvent(new KeyboardEvent("keyup", { key: "n" }));

    expect(shadowEvent.preventDefault).not.toHaveBeenCalled();
    expect(plainEvent.preventDefault).toHaveBeenCalledOnce();
    expect(localSkipClick).toHaveBeenCalledOnce();
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

      expect(ownsNativeKeyboardBehavior(editor)).toBe(false);
      expect(editorEvent.preventDefault).not.toHaveBeenCalled();
      expect(plainEvent.preventDefault).toHaveBeenCalledOnce();
      expect(localSkipClick).toHaveBeenCalledOnce();
    },
  );
});
