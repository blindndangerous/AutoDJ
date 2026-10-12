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
// Where the browser cannot choose an output device (Safari, Firefox with
// the setting turned off), the select is disabled and a sentence on the
// page says why; previews keep playing on the system default output.
//
// Browsers hide device names until the page has a media permission.
// Where navigator.mediaDevices.selectAudioOutput exists (Firefox), the
// button opens the browser's own device picker.  Elsewhere it asks for
// the microphone once and stops it at once: that is the only way those
// browsers reveal the names.  Without names the devices are numbered,
// and they still work.
//
// The choice is saved in this browser.  A saved device that is not
// available falls back to the system default without losing the choice,
// and is used again when it comes back.  Nothing here moves focus.

export const PREVIEW_SINK_KEY = "autodj.previewSinkId";

export const UNSUPPORTED_TEXT =
  "This browser cannot send a web page's sound to a chosen device, so previews play on the "
  + "system default output.  Chrome, Edge and Firefox can.";
export const MISSING_TEXT =
  "The saved preview device is not available right now, so previews play on the system "
  + "default output.  Connect it, or choose it again.";
export const DENIED_TEXT =
  "The browser did not give permission, so the devices are numbered instead of named.  They "
  + "still work.  To see names, allow the microphone for this site in the browser's site "
  + "settings, then press Show device names again.  AutoDJ never records sound.";

const DEFAULT_LABEL = "the system default output";

// Reading localStorage itself throws where site data is blocked.
function defaultStorage() {
  try {
    return globalThis.localStorage;
  } catch {
    return null;
  }
}

function readSavedSink(storage) {
  try {
    return storage?.getItem(PREVIEW_SINK_KEY) || "";
  } catch {
    return "";
  }
}

function writeSavedSink(storage, id) {
  try {
    if (id) storage?.setItem(PREVIEW_SINK_KEY, id);
    else storage?.removeItem(PREVIEW_SINK_KEY);
  } catch {
    // Private windows and blocked storage: the choice is not remembered.
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

export function installPreviewOutput({
  document: doc = globalThis.document,
  mediaDevices = globalThis.navigator?.mediaDevices,
  mediaElementPrototype = globalThis.HTMLMediaElement?.prototype,
  storage = defaultStorage(),
  announce = () => {},
} = {}) {
  const select = doc.getElementById("preview-device");
  if (!select) return null;
  const button = doc.getElementById("preview-device-choose");
  const note = doc.getElementById("preview-device-note");
  const help = doc.getElementById("preview-device-desc");
  const { supported, canChoose } = previewOutputSupport({ mediaDevices, mediaElementPrototype });
  const targets = () => [...doc.querySelectorAll("audio[data-preview-output]")]
    .filter((element) => typeof element.setSinkId === "function");
  let choices = [];
  let missing = false;
  let loaded = false;
  let busy = false;

  // A hidden description is still read, so the note is linked only while
  // it is shown.
  function showNote(text) {
    if (!note) return;
    note.textContent = text || "";
    note.hidden = !text;
    const ids = [help?.id, text ? note.id : null].filter(Boolean).join(" ");
    select.setAttribute("aria-describedby", ids);
  }

  function labelFor(id) {
    if (!id) return DEFAULT_LABEL;
    return choices.find((choice) => choice.id === id)?.label || "the chosen device";
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
    return failure;
  }

  function renderOptions() {
    const wanted = ["", ...choices.map((choice) => choice.id)];
    const current = [...select.options].map((option) => option.value);
    const same = wanted.length === current.length && wanted.every((id, i) => id === current[i])
      && choices.every((choice, i) => select.options[i + 1].textContent === choice.label);
    if (same) return;
    const value = select.value;
    const options = [{ id: "", label: "System default" }, ...choices].map((choice) => {
      const option = doc.createElement("option");
      option.value = choice.id;
      option.textContent = choice.label;
      return option;
    });
    select.replaceChildren(...options);
    select.value = wanted.includes(value) ? value : "";
  }

  function renderButton() {
    if (!button) return;
    const named = choices.length > 0 && choices.every((choice) => choice.named);
    button.textContent = canChoose ? "Choose preview device" : "Show device names";
    // Hiding a focused button would drop focus to the page.
    if (doc.activeElement !== button) button.hidden = !canChoose && named;
  }

  // Background refresh: page load, devicechange, a granted permission.
  // Says nothing unless the saved device has just gone away.
  async function refresh() {
    try {
      choices = outputChoices(await mediaDevices.enumerateDevices());
    } catch {
      choices = [];
    }
    renderOptions();
    renderButton();
    const saved = readSavedSink(storage);
    const present = !saved || choices.some((choice) => choice.id === saved);
    select.value = present ? saved : "";
    const failure = await route(present ? saved : "");
    const nowMissing = Boolean(saved) && (!present || Boolean(failure));
    // The page loading says nothing: the note on the page is enough.  A
    // device unplugged or plugged back in later is said once.
    if (nowMissing) {
      select.value = "";
      showNote(MISSING_TEXT);
      if (loaded && !missing) announce(MISSING_TEXT, { dwellMs: 8000 });
    } else if (note?.textContent === MISSING_TEXT) {
      showNote("");
      if (loaded && missing) announce(`Previews play on ${labelFor(saved)} again.`, { dwellMs: 4000 });
    }
    missing = nowMissing;
    loaded = true;
    return choices;
  }

  async function use(id, { userInitiated = true } = {}) {
    writeSavedSink(storage, id);
    const failure = await route(id);
    if (failure) {
      select.value = "";
      writeSavedSink(storage, "");
      announce(`Could not use ${labelFor(id)} for previews: ${errorWords(failure)}.  `
        + `Previews play on ${DEFAULT_LABEL}.`, { dwellMs: 8000, force: userInitiated, tone: "error" });
      return false;
    }
    missing = false;
    if (note?.textContent === MISSING_TEXT) showNote("");
    announce(`Previews play on ${labelFor(id)}.`, { dwellMs: 4000, force: userInitiated });
    return true;
  }

  async function choose() {
    if (busy) return;
    busy = true;
    try {
      if (canChoose) {
        let device = null;
        try {
          const saved = readSavedSink(storage);
          device = await mediaDevices.selectAudioOutput(saved ? { deviceId: saved } : undefined);
        } catch {
          // Cancelled, or the browser refused: nothing changes.
          announce(`No device was chosen.  Previews stay on ${labelFor(select.value)}.`,
            { dwellMs: 6000, force: true });
          return;
        }
        await refresh();
        if (device?.deviceId && !choices.some((choice) => choice.id === device.deviceId)) {
          choices.push({ id: device.deviceId, label: device.label || "the chosen device", named: true });
          renderOptions();
        }
        select.value = device?.deviceId || "";
        await use(select.value);
        return;
      }
      try {
        const stream = await mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach((track) => track.stop());
      } catch {
        showNote(DENIED_TEXT);
        announce(DENIED_TEXT, { dwellMs: 15000, force: true, tone: "error" });
        return;
      }
      if (note?.textContent === DENIED_TEXT) showNote("");
      await refresh();
      announce(choices.length
        ? `Device names are shown.  ${choices.length} preview ${choices.length === 1 ? "device" : "devices"} found.`
        : "No other output devices were found.", { dwellMs: 6000, force: true });
    } finally {
      busy = false;
    }
  }

  if (!supported) {
    select.disabled = true;
    if (button) button.hidden = true;
    showNote(UNSUPPORTED_TEXT);
    return { supported, refresh: async () => [], use: async () => false, choose: async () => {} };
  }

  select.addEventListener("change", () => { void use(select.value); });
  button?.addEventListener("click", () => { void choose(); });
  mediaDevices.addEventListener?.("devicechange", () => { void refresh(); });
  void refresh();
  return { supported, refresh, use, choose };
}
