import { registerSettingsGroup, settingsDraftHolds } from "./settings-drafts.js";
// Settings > Sound > Browser playback: "Preview output device".
//
// Previews are the cue point player in Track info, which Queue, Search,
// History and the library all open.  That player is a plain <audio>
// marked data-preview-output: it never joins the Web Audio graph the
// decks, liners and carts play through, and nothing it plays is sent to
// the server.  This module sends it to an output device of its own with
// HTMLMediaElement.setSinkId, so a DJ can hear a song in headphones while
// the browser mix plays on the speakers.
//
// Support (MDN browser-compat-data, October 2026): setSinkId is in desktop
// Chrome and Edge, Firefox 116 and later, and Safari 18.4 and later, but
// not in Chrome or Firefox on Android.  Where it is missing, the select is
// disabled, a sentence on the page says why, and previews keep playing on
// the system default output.
//
// Browsers hide device names until the page has a media permission.  Only
// Firefox (116 and later) has navigator.mediaDevices.selectAudioOutput, the
// browser's own device picker; Chrome, Edge and Safari do not.  There the
// button asks for the microphone once and stops it at once, the only way
// those browsers reveal the names.  Without names the devices are
// numbered, and they still work.
//
// The choice is saved in this browser.  A saved device that is not
// connected stays in the list as "(not connected)", so choosing System
// default really clears it; previews use the system default meanwhile and
// go back to the device when it returns.  A device list the browser will
// not give (an error, or no permission yet) proves nothing, so it never
// counts as "not connected".
//
// Every change of device runs in order on one queue, so a slow refresh
// can never land after the user's newer choice.  Nothing here moves focus.

import { defaultStorage, readStorage } from "./safe-storage.js";

export const PREVIEW_SINK_KEY = "autodj.previewSinkId";
export const PREVIEW_LABEL_KEY = "autodj.previewSinkLabel";

export const UNSUPPORTED_TEXT =
  "This browser cannot send a web page's sound to a chosen device, so previews play on the "
  + "system default output.  Chrome, Edge, Firefox and Safari 18.4 or later on a computer can; "
  + "browsers on Android cannot.";
export const MISSING_TEXT =
  "The saved preview device is not connected, so previews play on the system default output "
  + "until it is.  Choose System default to forget it.";
export const UNAVAILABLE_TEXT =
  "The browser would not use the saved preview device, so previews play on the system default "
  + "output.  Choose it again, or choose another device.";
export const DENIED_TEXT =
  "The browser did not give permission, so the devices are numbered instead of named.";
export const NO_MICROPHONE_TEXT =
  "This browser shows device names only after microphone permission, and it found no "
  + "microphone to ask about.  The devices are numbered instead; they still work.";
export const MICROPHONE_BUSY_TEXT =
  "The microphone could not be opened, perhaps because another program is using it, so device "
  + "names could not be shown.  The devices are numbered instead; they still work.  Close the "
  + "other program and press Show preview device names again.";
export const NAMES_FAILED_TEXT =
  "The browser could not show device names.  The devices are numbered instead; they still work.";

export const CHOOSE_DESC_TEXT =
  "Choose preview device opens the browser's own list of speakers and headphones.";
export const NAMES_DESC_TEXT =
  "Show preview device names asks for the microphone once, only to read the device names, and "
  + "stops it straight away.";

// After a browser prompt (Firefox's device picker, a microphone prompt)
// focus comes back to the page a moment after the prompt's promise
// settles.  A status said before then is cut off by the focus speech, so
// it waits for the window to have focus, then a beat more.
export const FOCUS_SETTLE_MS = 150;
const FOCUS_WAIT_MS = 3000;

const DEFAULT_LABEL = "the system default output";
const NOTES = new Set([MISSING_TEXT, UNAVAILABLE_TEXT]);

function readSaved(storage) {
  return {
    id: readStorage(storage, PREVIEW_SINK_KEY) || "",
    label: readStorage(storage, PREVIEW_LABEL_KEY) || "",
  };
}

function writeSaved(storage, id, label = "") {
  if (!storage) throw new Error("Browser storage is unavailable.");
  const previous = readSaved(storage);
  try {
    if (id) storage.setItem(PREVIEW_SINK_KEY, id);
    else storage.removeItem(PREVIEW_SINK_KEY);
    if (id && label) storage.setItem(PREVIEW_LABEL_KEY, label);
    else storage.removeItem(PREVIEW_LABEL_KEY);
  } catch (error) {
    try {
      if (previous.id) storage.setItem(PREVIEW_SINK_KEY, previous.id);
      else storage.removeItem(PREVIEW_SINK_KEY);
      if (previous.label) storage.setItem(PREVIEW_LABEL_KEY, previous.label);
      else storage.removeItem(PREVIEW_LABEL_KEY);
    } catch { /* Keep the original storage failure. */ }
    throw error;
  }
}

export function previewOutputSupport({ mediaDevices, mediaElementPrototype }) {
  const supported = typeof mediaDevices?.enumerateDevices === "function"
    && typeof mediaElementPrototype?.setSinkId === "function";
  return {
    supported,
    canChoose: supported && typeof mediaDevices.selectAudioOutput === "function",
  };
}

// The output devices to offer, after "System default".  Chromium's
// "default" entry is that same device under a second name, so it is left
// out.  A device without a name (no permission yet) is numbered.
export function outputChoices(devices) {
  const outputs = (devices || []).filter((device) => device?.kind === "audiooutput"
    && device.deviceId && device.deviceId !== "default");
  return outputs.map((device, index) => ({
    id: device.deviceId,
    label: device.label || `Output device ${index + 1}`,
    named: Boolean(device.label),
  }));
}

function errorWords(error) {
  if (error?.name === "NotAllowedError") return "the browser did not allow it";
  if (error?.name === "NotFoundError") return "the device is not available";
  // Raw browser error text is never spoken.
  return "the browser refused";
}

// What a failed getUserMedia means, in words.
export function microphoneErrorText(error) {
  switch (error?.name) {
    case "NotAllowedError":
    case "SecurityError":
      return DENIED_TEXT;
    case "NotFoundError":
    case "OverconstrainedError":
      return NO_MICROPHONE_TEXT;
    case "NotReadableError":
    case "AbortError":
      return MICROPHONE_BUSY_TEXT;
    default:
      return NAMES_FAILED_TEXT;
  }
}

export function installPreviewOutput({
  document: doc = globalThis.document,
  mediaDevices = globalThis.navigator?.mediaDevices,
  mediaElementPrototype = globalThis.HTMLMediaElement?.prototype,
  storage = defaultStorage(),
  announce = () => {},
  onRoute = () => {},
  focusWaitMs = FOCUS_WAIT_MS,
} = {}) {
  const select = doc.getElementById("preview-device");
  if (!select) return null;
  const button = doc.getElementById("preview-device-choose");
  const note = doc.getElementById("preview-device-note");
  const { supported, canChoose } = previewOutputSupport({ mediaDevices, mediaElementPrototype });
  const targets = () => [...doc.querySelectorAll("audio[data-preview-output]")]
    .filter((element) => typeof element.setSinkId === "function");
  // Devices the browser listed, and those picked in its own picker this
  // session (Firefox may leave those out of the list).
  let listed = [];
  const picked = new Map();
  // Whether the list can prove a device is gone: it loaded and has names.
  let known = false;
  // Whether the saved device is not in use and the user has been told.
  let fellBack = false;
  let loaded = false;
  let busy = false;
  let queue = Promise.resolve();
  // The saved choice.  Kept here as well, so blocked storage only means
  // it is not remembered after a reload.
  let saved = readSaved(storage);
  const current = () => ({ ...saved });
  function setSaved(id, label = "") {
    writeSaved(storage, id, label);
    saved = { id, label: id ? label : "" };
  }

  const connected = () => [...listed,
    ...[...picked.values()].filter((device) => !listed.some((item) => item.id === device.id))];

  function serial(task) {
    const run = queue.then(task, task);
    queue = run.catch(() => {});
    return run;
  }

  function showNote(text) {
    if (!note) return;
    note.textContent = text || "";
    note.hidden = !text;
  }

  function clearFallbackNote() {
    if (NOTES.has(note?.textContent)) showNote("");
  }

  function labelFor(id) {
    if (!id) return DEFAULT_LABEL;
    const device = connected().find((choice) => choice.id === id);
    if (device) return device.label;
    return saved.id === id && saved.label ? saved.label : "the chosen device";
  }

  // Every preview player follows one device.  Returns the error that
  // stopped it, after putting every player back on the system default.
  async function route(id) {
    let failure = null;
    for (const element of targets()) {
      try {
        if (element.sinkId !== id) await element.setSinkId(id);
      } catch (error) {
        failure = error;
      }
    }
    if (failure && id) await route("");
    else onRoute();
    return failure;
  }

  // The saved device that is not in the list keeps an option of its own,
  // so the select says what is saved and System default can clear it.
  function renderOptions(choice, suffix = "") {
    const draftValue = settingsDraftHolds(select) ? select.value : null;
    const choices = [{ id: "", label: "System default" }, ...connected()];
    if (choice.id && !choices.some((item) => item.id === choice.id)) {
      const name = choice.label || "Saved device";
      choices.push({ id: choice.id, label: suffix ? `${name} (${suffix})` : name });
    }
    const same = choices.length === select.options.length
      && choices.every((choice, i) => select.options[i].value === choice.id
        && select.options[i].textContent === choice.label);
    if (!same) {
      select.replaceChildren(...choices.map((item) => {
        const option = doc.createElement("option");
        option.value = item.id;
        option.textContent = item.label;
        return option;
      }));
    }
    select.value = draftValue ?? choice.id;
    if (select.value !== (draftValue ?? choice.id)) select.value = "";
  }

  // Resolves once the page has focus again (or after focusWaitMs, if it
  // never comes back), plus FOCUS_SETTLE_MS.
  function afterFocus() {
    const view = doc.defaultView;
    const pause = () => new Promise((resolve) => setTimeout(resolve, FOCUS_SETTLE_MS));
    if (!view || typeof doc.hasFocus !== "function" || doc.hasFocus()) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer);
        view.removeEventListener("focus", done);
        resolve(pause());
      };
      const timer = setTimeout(done, focusWaitMs);
      view.addEventListener("focus", done);
    });
  }

  async function say(gate, text, options) {
    if (gate) await gate;
    announce(text, options);
  }

  function renderButton() {
    if (!button) return;
    const desc = doc.getElementById("preview-device-choose-desc");
    if (desc) desc.textContent = canChoose ? CHOOSE_DESC_TEXT : NAMES_DESC_TEXT;
    const choices = connected();
    const named = choices.length > 0 && choices.every((choice) => choice.named);
    button.textContent = canChoose ? "Choose preview device" : "Show preview device names";
    // Hiding a focused button would drop focus to the page.
    if (doc.activeElement !== button) button.hidden = !canChoose && named;
    // The sentence about the button is part of the select's help too, so
    // it goes when the button does.
    if (desc) desc.hidden = button.hidden;
  }

  // The saved device could not be used: say so once, not on page load
  // (the note is enough then) and not on every devicechange.
  function fallBack(text, choice, { force = false, quiet = false } = {}) {
    renderOptions(choice, text === MISSING_TEXT ? "not connected" : "not available");
    showNote(text);
    if (!quiet && ((loaded && !fellBack) || force)) announce(text, { dwellMs: 8000, force });
    fellBack = true;
  }

  function settle(choice) {
    renderOptions(choice);
    clearFallbackNote();
    const wasFallen = fellBack;
    fellBack = false;
    return wasFallen;
  }

  // Background refresh: page load, devicechange, a granted permission.
  async function doRefresh() {
    try {
      await refreshOnce();
    } finally {
      loaded = true;
    }
  }

  async function refreshOnce() {
    let devices = null;
    try {
      devices = await mediaDevices.enumerateDevices();
    } catch {
      devices = null;
    }
    listed = outputChoices(devices);
    known = listed.some((choice) => choice.named);
    renderButton();
    const choice = current();
    const present = !choice.id || connected().some((item) => item.id === choice.id);
    if (!present && known) {
      await route("");
      // A newer choice was saved while this one waited: it decides.
      if (saved.id !== choice.id) return;
      fallBack(MISSING_TEXT, choice);
    } else {
      const failure = await route(choice.id);
      if (saved.id !== choice.id) return;
      if (failure && choice.id) {
        fallBack(UNAVAILABLE_TEXT, choice);
      } else if (settle(choice) && loaded && choice.id) {
        announce(`Previews play on ${labelFor(choice.id)} again.`, { dwellMs: 4000 });
      }
    }
  }

  async function doUse(id) {
    const choice = current();
    const device = connected().find((item) => item.id === id);
    const label = device?.named ? device.label : (choice.id === id ? choice.label : "");
    if (id && known && !connected().some((item) => item.id === id)) {
      await route("");
      fallBack(MISSING_TEXT, choice, { quiet: true });
      throw new Error(MISSING_TEXT);
    }
    // Persist the requested choice before routing. A failed route restores the
    // previous saved choice and leaves the draft available for correction.
    setSaved(id, label);
    const failure = await route(id);
    if (failure) {
      const words = errorWords(failure);
      setSaved(choice.id, choice.label);
      settle(current());
      showNote(`Could not use ${label || "the chosen device"} for previews: ${words}.`);
      throw new Error(`Could not use ${label || "the chosen device"} for previews: ${words}.`);
    }
    settle(current());
    showNote("");
    return true;
  }

  const refresh = () => serial(doRefresh);
  const use = (id) => serial(() => doUse(id));

  async function chooseInPicker() {
    let device = null;
    let failure = null;
    try {
      device = await mediaDevices.selectAudioOutput(saved.id ? { deviceId: saved.id } : undefined);
    } catch (error) {
      failure = error;
    }
    const gate = afterFocus();
    if (failure || !device?.deviceId) {
      const why = !failure || failure.name === "NotAllowedError" || failure.name === "AbortError"
        ? "No device was chosen."
        : "The browser could not open its device picker.";
      await say(gate, `${why}  Previews stay on ${labelFor(select.value)}.`,
        { dwellMs: 6000, force: true });
      return;
    }
    picked.set(device.deviceId, {
      id: device.deviceId, label: device.label || "the chosen device", named: true,
    });
    renderOptions({ id: device.deviceId, label: device.label });
    select.value = device.deviceId;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await say(gate, "Preview device selected. Save sound settings to apply it.", { force: true });
  }

  async function showNames() {
    let gate = null;
    try {
      const stream = await mediaDevices.getUserMedia({ audio: true });
      gate = afterFocus();
      for (const track of stream.getTracks()) track.stop();
    } catch (error) {
      const text = microphoneErrorText(error);
      showNote(text);
      await say(afterFocus(), text, { dwellMs: 15000, force: true, tone: "error" });
      return;
    }
    if (!NOTES.has(note?.textContent)) showNote("");
    await refresh();
    const count = connected().length;
    await say(gate, count
      ? `Device names are shown.  ${count} preview ${count === 1 ? "device" : "devices"} found.`
      : "No other output devices were found.", { dwellMs: 6000, force: true });
  }

  async function choose() {
    if (busy) return;
    busy = true;
    try {
      await (canChoose ? chooseInPicker() : showNames());
    } finally {
      busy = false;
    }
  }

  if (!supported) {
    select.disabled = true;
    if (button) button.hidden = true;
    const desc = doc.getElementById("preview-device-choose-desc");
    if (desc) desc.hidden = true;
    showNote(UNSUPPORTED_TEXT);
    return { supported, refresh: async () => {}, use: async () => false, choose: async () => {} };
  }

  renderButton();
  renderOptions(current());
  registerSettingsGroup({ panel: select.closest('[id^="spanel-"]'), controls: [select],
    read: () => ({ device: select.value }), write: (value) => { select.value = value.device; },
    async commit(value) { await use(value.device); },
  });
  button?.addEventListener("click", () => { void choose(); });
  mediaDevices.addEventListener?.("devicechange", () => { void refresh(); });
  void refresh();
  return { supported, refresh, use, choose };
}
