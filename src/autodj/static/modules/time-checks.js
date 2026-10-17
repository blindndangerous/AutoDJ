// Settings > Time checks: the switches a paired browser may change, which
// voice packs speak (by name; the server keeps the folders), each pack's and
// the recorded clips' coverage, each pack's play dates, and "Play a time
// check now".  Voice packs are added from a zip and removed here, and the
// recorded clips folder and the intro and outro clips are chosen by name
// from the station's time checks folder, or a clip is uploaded (#388).
// After a pack is added, focus moves to its checkbox; after one is removed,
// to the next pack's checkbox, or the Voice packs group when none follows.
// A pending upload or remove keeps its button enabled and says aria-busy
// (no aria-disabled: NVDA would say "unavailable" for the press, #331).
//
// In browser playback this page also plays the time check after the song
// that crosses the hour, through the same voice liner path (duck and
// master gain) as liners.js.  When the server mixes, the server plays it.
// Each button or setting result is said once through #tc-status; the
// automatic check after the hour says nothing.

import {
  isStaleStationReply, requestBinary, requestJson, staleActionText, withDisabled,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { announceStatus } from "./live-region.js";

const MINUTES = 1440;
// The intro or outro option that means "as config.toml says": never a
// clip name, which must end in an audio extension and has no colon.
const FROM_CONFIG = ":config";

export function installTimeChecks({ doc = document, request = requestJson,
  binary = requestBinary, playLiner = async () => false,
  testOnServer = () => true, canPlay = () => false,
  prepareTest = () => null, now = () => new Date(), interval = setInterval,
  confirm = confirmAction } = {}) {
  const get = (name) => doc.getElementById(`tc-${name}`);
  const enabled = get("enabled");
  const format = get("hour-format");
  const rule = get("rule");
  const clips = get("clips-status");
  const status = get("status");
  const test = get("test");
  const order = get("pack-order");
  const packList = get("pack-list");
  const packsEmpty = get("packs-empty");
  const testVoice = get("test-voice");
  const panel = doc.getElementById("spanel-time-checks");
  const dates = {
    group: get("dates"), pack: get("dates-pack"), from: get("not-before"),
    until: get("not-after"), error: get("dates-error"), save: get("dates-save"),
  };
  const hasDates = Object.values(dates).every(Boolean);
  const packsGroup = get("packs");
  const files = {
    rescan: get("rescan"), packFile: get("pack-file"), packName: get("pack-name"),
    packAdd: get("pack-add"), clipFile: get("clip-file"), clipAdd: get("clip-add"),
    folder: get("recorded-folder"), intro: get("intro"), outro: get("outro"),
    packError: get("pack-error"), clipError: get("clip-error"),
  };
  const hasFiles = Object.values(files).every(Boolean);
  // Each file input, the error shown under it and its own help.
  const uploads = {
    pack: { input: files.packFile, error: files.packError, help: "tc-add-pack-desc" },
    clip: { input: files.clipFile, error: files.clipError, help: "tc-clip-file-desc" },
  };
  if (!enabled || !format || !rule || !clips || !status || !test || !order || !packList
      || !packsEmpty || !testVoice) {
    return { refresh: async () => {}, clear: () => {}, noteTrack: () => {} };
  }
  let settings = null;
  let previousTrack = null;
  let previousHour = null;
  let playing = false;
  let pending = false;
  // Counts loads, so a reply that lands after a newer load, or after
  // clear(), is dropped.
  let generation = 0;

  function say(message, tone = "info") {
    announceStatus(status, message, { force: true, dwellMs: tone === "error" ? 6000 : 4000, tone });
  }

  function hourKey(date) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: settings?.timezone || "UTC", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", hourCycle: "h23", timeZoneName: "shortOffset",
    }).formatToParts(date);
    return parts.filter((part) => part.type !== "literal").map((part) => part.value).join(":");
  }

  const packBoxes = () => [...packList.querySelectorAll("input[type=checkbox]")];
  let shownPacks = "[]";

  // One native checkbox per pack, its coverage as its description, and a
  // Remove button for a pack kept in the station's time checks folder.  The
  // rows are rebuilt only when the packs change, so a refresh never takes
  // focus away from a checkbox.
  function showPacks(packs) {
    const names = packs.map((pack) => pack.name);
    const key = JSON.stringify(packs.map((pack) => [pack.name, Boolean(pack.removable)]));
    if (key !== shownPacks) {
      shownPacks = key;
      packList.replaceChildren(...packs.map((pack, index) => {
        const row = doc.createElement("div");
        row.className = "settings-check";
        const box = doc.createElement("input");
        box.type = "checkbox";
        box.id = `tc-pack-${index}`;
        box.value = pack.name;
        box.setAttribute("aria-describedby", `tc-pack-${index}-desc`);
        box.addEventListener("change", save);
        const label = doc.createElement("label");
        label.htmlFor = box.id;
        label.textContent = pack.name;
        const desc = doc.createElement("span");
        desc.className = "setting-desc";
        desc.id = `tc-pack-${index}-desc`;
        row.append(box, label);
        if (pack.removable) {
          const remove = doc.createElement("button");
          remove.type = "button";
          remove.textContent = "Remove";
          // The visible word first, so a voice command "click Remove" works (WCAG 2.5.3).
          remove.setAttribute("aria-label", `Remove voice pack ${pack.name}`);
          remove.dataset.pack = pack.name;
          remove.addEventListener("click", () => removePack(pack.name, remove));
          row.append(remove);
        }
        return [row, desc];
      }).flat());
      const chosen = testVoice.value;
      testVoice.replaceChildren(testVoice.options[0], ...names.map((name) => {
        const option = doc.createElement("option");
        option.value = name;
        option.textContent = `Voice pack ${name}`;
        return option;
      }));
      testVoice.value = names.includes(chosen) ? chosen : "";
    }
    packBoxes().forEach((box, index) => {
      if (doc.activeElement !== box) box.checked = packs[index].enabled;
      get(`pack-${index}-desc`).textContent = packWords(packs[index]);
    });
    packsEmpty.hidden = packs.length > 0;
    showDates(packs);
  }

  // A pack's coverage, then its play dates and why it is skipped now, then
  // where it comes from when it cannot be removed here.
  function packWords(pack) {
    const parts = [pack.summary];
    if (pack.dates_text) parts.push(`${pack.dates_text}.`);
    if (pack.playable === false) parts.push(`Skipped now: ${pack.unplayable_text}.`);
    if (pack.source === "config") parts.push("Listed in config.toml.");
    if (pack.source === "shared") parts.push("From the library's shared time checks folder.");
    return parts.join(" ");
  }

  // Rebuilds a select's options only when they change, and sets its value
  // unless it has focus, so a refresh never moves a choice being made.
  function fillSelect(select, options, value) {
    const values = options.map(([optionValue]) => optionValue);
    const listed = [...select.options].map((option) => [option.value, option.textContent]);
    if (JSON.stringify(listed) !== JSON.stringify(options)) {
      select.replaceChildren(...options.map(([optionValue, text]) => {
        const option = doc.createElement("option");
        option.value = optionValue;
        option.textContent = text;
        return option;
      }));
    }
    if (doc.activeElement !== select) select.value = values.includes(value) ? value : values[0];
  }

  // A chosen name no longer in the folder still shows, so the choice is
  // never silently different from what the server uses.
  const missing = (name, names) => (name && !names.includes(name) ? [[name, `${name} (not found)`]] : []);

  function showClips() {
    if (!hasFiles) return;
    const folders = settings.folders || [];
    fillSelect(files.folder, [
      ["", "The time checks folder itself"],
      ...folders.map((name) => [name, `Folder ${name}`]),
      ...missing(settings.recorded_folder, folders),
    ], settings.recorded_folder || "");
    const names = settings.clip_files || [];
    for (const which of ["intro", "outro"]) {
      const configured = settings[`${which}_config`];
      const chosen = settings[which];
      fillSelect(files[which], [
        ["", "None"],
        ...(configured ? [[FROM_CONFIG, `${configured}, from config.toml`]] : []),
        ...names.map((name) => [name, name]),
        ...missing(chosen, names),
      ], chosen == null ? (configured ? FROM_CONFIG : "") : chosen);
    }
  }

  // null keeps "as config.toml says": the config option, or None while
  // config.toml names no clip, so a clip added there later still plays.
  function clipChoice(which) {
    const value = files[which].value;
    if (value === FROM_CONFIG) return null;
    if (value === "" && !settings?.[`${which}_config`] && settings?.[which] == null) return null;
    return value;
  }

  // The play dates form: one pack at a time, its options rebuilt only when
  // the configured packs change, and the dates filled in only when a pack is
  // chosen, so a refresh never undoes what is being typed.
  function showDates(packs) {
    if (!hasDates) return;
    dates.group.hidden = packs.length === 0;
    const names = packs.map((pack) => pack.name);
    const listed = [...dates.pack.options].map((item) => item.value);
    if (JSON.stringify(listed) !== JSON.stringify(names)) {
      const chosen = dates.pack.value;
      dates.pack.replaceChildren(...names.map((name) => {
        const item = doc.createElement("option");
        item.value = name;
        item.textContent = name;
        return item;
      }));
      dates.pack.value = names.includes(chosen) ? chosen : (names[0] ?? "");
      fillDates();
    }
  }

  function fillDates() {
    const pack = (settings?.packs || []).find((item) => item.name === dates.pack.value);
    dates.from.value = pack?.not_before || "";
    dates.until.value = pack?.not_after || "";
    clearDatesError();
  }

  function clearDatesError() {
    for (const field of [dates.from, dates.until]) {
      field.removeAttribute("aria-invalid");
      field.removeAttribute("aria-describedby");
    }
    dates.error.textContent = "";
    dates.error.hidden = true;
  }

  // Shown on the page, linked to the field and focused (WCAG 3.3.1), and said.
  function datesError(field, message) {
    dates.error.textContent = message;
    dates.error.hidden = false;
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", dates.error.id);
    field.focus();
    say(message, "error");
  }

  async function saveDates() {
    clearDatesError();
    const name = dates.pack.value;
    if (!name) return;
    const from = dates.from.value || null;
    const until = dates.until.value || null;
    if (from && until && until < from) {
      datesError(dates.until, "Do not play after must be on or after the Not before date.");
      return;
    }
    try {
      settings = await request(`/api/time-checks/packs/${encodeURIComponent(name)}/dates`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...(from ? { not_before: from } : {}), ...(until ? { not_after: until } : {}) }),
      });
      showSettings();
      const pack = settings.packs?.find((item) => item.name === name);
      say(pack?.dates_text ? `Saved. Voice pack ${name}: ${pack.dates_text}.` : `Saved. Voice pack ${name} plays on any day.`);
    } catch (error) {
      const left = staleActionText(error);
      if (left) {
        say(left, error.actionDone ? "info" : "error");
        return;
      }
      const field = { not_before: dates.from, not_after: dates.until }[error.field];
      const message = `Could not save the play dates. ${error.message}`;
      if (field) datesError(field, message);
      else say(message, "error");
    }
  }

  function showSettings() {
    if (doc.activeElement !== enabled) enabled.checked = settings.enabled;
    if (doc.activeElement !== format) format.value = settings.hour_format;
    if (doc.activeElement !== rule) rule.value = settings.rule;
    if (doc.activeElement !== order) order.value = settings.pack_order;
    showPacks(settings.packs || []);
    showClips();
    const recorded = MINUTES - settings.missing_minutes;
    const fallback = settings.generated_set
      ? `Generated set ${settings.generated_set} supplies local audio. Missing generated clips are skipped.`
      : settings.tts_available
      ? "Text to speech says the minutes without a clip."
      : "No text to speech is set up, so minutes without a clip are skipped.";
    clips.textContent = `${recorded} of ${MINUTES} minute clips recorded. ${fallback} `
      + `Times are in the station time zone, ${settings.timezone}.`;
  }

  // Background loads write into the panel; they are not announced.
  async function refresh() {
    const mine = ++generation;
    try {
      const loaded = await request("/api/time-checks");
      if (mine !== generation) return;
      settings = loaded;
      if (previousTrack && previousHour === null) previousHour = hourKey(now());
      showSettings();
    } catch (error) {
      // A reply for the station the page has just left says nothing (#70).
      if (mine !== generation || isStaleStationReply(error)) return;
      clips.textContent = `Could not load time checks: ${error.message}`;
    }
  }

  // A station switch: what this panel showed was the old station's.
  function clear() {
    generation += 1;
    settings = null;
    previousTrack = null;
    previousHour = null;
    pending = false;
    clips.textContent = "";
    status.textContent = "";
    showPacks([]);
    packsEmpty.hidden = true;
    if (hasFiles) {
      for (const select of [files.folder, files.intro, files.outro]) {
        select.replaceChildren(select.options[0]);
      }
    }
  }

  async function save() {
    // Before the first load (or after a failed one) the pack list is
    // empty: a save now would untick every pack on the server.
    if (!settings) {
      say("Time check settings are still loading; try again in a moment.", "error");
      return;
    }
    const body = { enabled: enabled.checked, hour_format: format.value, rule: rule.value,
      enabled_packs: packBoxes().filter((box) => box.checked).map((box) => box.value),
      pack_order: order.value };
    if (hasFiles) {
      body.recorded_folder = files.folder.value;
      body.intro = clipChoice("intro");
      body.outro = clipChoice("outro");
    }
    try {
      settings = await request("/api/time-checks", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!settings.enabled || settings.rule === "off") pending = false;
      showSettings();
      say("Time check settings saved.");
    } catch (error) {
      // Answered after a station switch: the form now shows the new
      // station's settings, so it is left alone (#70).
      const left = staleActionText(error);
      if (left) {
        say(left, error.actionDone ? "info" : "error");
        return;
      }
      if (settings) {
        enabled.checked = settings.enabled;
        format.value = settings.hour_format;
        rule.value = settings.rule;
        order.value = settings.pack_order;
        packBoxes().forEach((box, index) => { box.checked = Boolean(settings.packs?.[index]?.enabled); });
        if (hasFiles) {
          files.folder.value = settings.recorded_folder || "";
          for (const which of ["intro", "outro"]) {
            const chosen = settings[which];
            files[which].value = chosen == null
              ? (settings[`${which}_config`] ? FROM_CONFIG : "") : chosen;
          }
        }
      }
      say(`Could not save time check settings: ${error.message}`, "error");
    }
  }

  // The pressed button keeps focus and stays enabled; withDisabled swallows
  // a second press, and aria-busy says it is working.
  async function busyWhile(button, operation) {
    return withDisabled(button, async () => {
      button.setAttribute("aria-busy", "true");
      try {
        return await operation();
      } finally {
        button.removeAttribute("aria-busy");
      }
    });
  }

  // A stale reply (the page moved to another station) or a failure, said once.
  function sayFailure(error, prefix) {
    const left = staleActionText(error);
    if (left) say(left, error.actionDone ? "info" : "error");
    else say(`${prefix} ${error.message}`, "error");
  }

  // A refused upload is shown under its file input, linked to it and
  // focused (WCAG 3.3.1), like a refused play date, and said.  Choosing
  // another file clears it.
  function uploadError(which, message) {
    const { input, error, help } = uploads[which];
    error.textContent = message;
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.setAttribute("aria-describedby", `${error.id} ${help}`);
    input.focus();
    say(message, "error");
  }

  function clearUploadError(which) {
    const { input, error, help } = uploads[which];
    error.textContent = "";
    error.hidden = true;
    input.removeAttribute("aria-invalid");
    input.setAttribute("aria-describedby", help);
  }

  // A failure answered after a station switch is only said; any other is
  // shown at the file input.
  function uploadFailure(which, error, prefix) {
    const left = staleActionText(error);
    if (left) say(left, error.actionDone ? "info" : "error");
    else uploadError(which, `${prefix} ${error.message}`);
  }

  const nameKey = (name) => name.normalize("NFC").toLowerCase();
  // The default station's packs are every station's when its time checks
  // folder is the library's shared one; the server says so per pack.
  const sharedNote = (pack) => (pack?.shared_with_stations ? " Other stations use this pack too." : "");

  // *target* names the pack being replaced when the page knows it; the
  // server's own refusal names it only in its words.
  async function askReplacePack(message, file, target = null) {
    const packs = settings?.packs || [];
    const shared = target
      ? packs.find((pack) => nameKey(pack.name) === nameKey(target))
      : packs.find((pack) => pack.shared_with_stations);
    return confirm(doc, {
      station: true,
      title: "Replace the voice pack?",
      message: `${message}. Its files are deleted and replaced by the files in ${file.name}. Its tick and play dates stay.${sharedNote(shared)}`,
      confirmLabel: "Replace voice pack",
    });
  }

  // Sends the chosen zip.  A typed name a pack added here already has asks
  // before anything is sent.  With no typed name the folder in the zip names
  // the pack, which the page cannot see, so the server's refusal asks then,
  // and the zip is sent again with replace.
  async function addPack(replace = false) {
    const file = files.packFile.files?.[0];
    if (!file) {
      uploadError("pack", "Choose a zip file first.");
      return;
    }
    clearUploadError("pack");
    const name = files.packName.value.trim();
    if (!replace && name) {
      const taken = (settings?.packs || []).find(
        (pack) => pack.removable && nameKey(pack.name) === nameKey(name));
      if (taken) {
        if (!(await askReplacePack(`A voice pack named ${taken.name} already exists`, file, taken.name))) {
          say("The voice pack was not added.");
          return;
        }
        replace = true;
      }
    }
    const query = new URLSearchParams();
    if (name) query.set("name", name);
    if (replace) query.set("replace", "true");
    const form = new FormData();
    form.append("file", file, file.name);
    const suffix = query.toString() ? `?${query}` : "";
    say("Uploading the voice pack. This can take a few minutes.");
    let reply;
    try {
      reply = await request(`/api/time-checks/packs${suffix}`, { method: "POST", body: form });
    } catch (error) {
      if (error.status === 409 && error.field === "replace" && !replace) {
        if (await askReplacePack(error.message, file)) await addPack(true);
        else say("The voice pack was not added.");
        return;
      }
      uploadFailure("pack", error, "Could not add the voice pack.");
      return;
    }
    settings = reply;
    showSettings();
    files.packFile.value = "";
    files.packName.value = "";
    const added = reply.packs?.find((pack) => pack.name === reply.added);
    if (!added) {
      // A folder of minute clips is not a voice pack; it is chosen as the
      // recorded clips folder instead.
      doc.getElementById("tc-recorded-folder")?.focus();
      say(`Added folder ${reply.added} of minute clips. Choose it under Recorded minute clips folder.`);
      return;
    }
    packBoxes().find((box) => box.value === reply.added)?.focus();
    const ticked = added?.enabled ? "It is ticked." : "Tick it to use it.";
    say(`${replace ? "Replaced" : "Added"} voice pack ${reply.added}. ${ticked}`);
  }

  // Asks first; the remove runs while the dialog is open, so focus goes
  // straight to the next pack's checkbox, or the Voice packs group when
  // none follows (see confirmAction).
  async function removePack(name, button) {
    const index = packBoxes().findIndex((box) => box.value === name);
    const pack = (settings?.packs || []).find((item) => item.name === name);
    let result = null;
    let tone = "info";
    const confirmed = await confirm(doc, {
      station: true,
      title: `Remove voice pack ${name}?`,
      message: `Its folder and audio files are deleted from the time checks folder, with its tick and play dates.${sharedNote(pack)}`,
      confirmLabel: "Remove voice pack",
      onConfirm: async () => {
        try {
          settings = await request(`/api/time-checks/packs/${encodeURIComponent(name)}`, { method: "DELETE" });
        } catch (error) {
          const left = staleActionText(error);
          result = left || `Could not remove voice pack ${name}. ${error.message}`;
          tone = left && error.actionDone ? "info" : "error";
          return left ? null : button;
        }
        showSettings();
        result = `Removed voice pack ${name}.`;
        return packBoxes()[index] || packsGroup;
      },
    });
    if (!confirmed) {
      button.focus();
      return;
    }
    if (result) say(result, tone);
  }

  // Sends one clip; a taken name asks before it is replaced.
  async function addClip(replace = false) {
    const file = files.clipFile.files?.[0];
    if (!file) {
      uploadError("clip", "Choose a clip file first.");
      return;
    }
    clearUploadError("clip");
    const form = new FormData();
    form.append("file", file, file.name);
    say("Uploading the clip.");
    let reply;
    try {
      reply = await request(`/api/time-checks/clips${replace ? "?replace=true" : ""}`, {
        method: "POST", body: form,
      });
    } catch (error) {
      if (error.status === 409 && error.field === "replace" && !replace) {
        const confirmed = await confirm(doc, {
          station: true,
          title: `Replace ${file.name}?`,
          message: `${error.message}. It is replaced by the file you chose.`,
          confirmLabel: "Replace clip",
        });
        if (confirmed) await addClip(true);
        else say("The clip was not uploaded.");
        return;
      }
      uploadFailure("clip", error, "Could not upload the clip.");
      return;
    }
    settings = reply;
    showSettings();
    files.clipFile.value = "";
    say(/^\d{2}_\d{2}\.[a-z0-9]+$/i.test(reply.uploaded)
      ? `Uploaded ${reply.uploaded} to the recorded minute clips folder.`
      : `Uploaded ${reply.uploaded}. Choose it as the intro or outro clip to use it.`);
  }

  // Asks the server to look again; says which packs are new.
  async function rescan() {
    const before = new Set((settings?.packs || []).map((pack) => pack.name));
    try {
      settings = await request("/api/time-checks");
    } catch (error) {
      sayFailure(error, "Could not look for voice packs.");
      return;
    }
    showSettings();
    const found = (settings.packs || []).filter((pack) => !before.has(pack.name));
    const total = settings.packs?.length || 0;
    const count = `${total} voice pack${total === 1 ? "" : "s"} listed.`;
    say(found.length
      ? `Found ${found.map((pack) => pack.name).join(", ")}. ${count}`
      : `No new voice packs. ${count}`);
  }

  async function playHere(pack = "") {
    const query = pack ? `?pack=${encodeURIComponent(pack)}` : "";
    const audio = await binary(`/api/time-checks/preview${query}`);
    return playLiner(audio, settings?.duck_db ?? -3);
  }

  async function testNow() {
    const pack = testVoice.value;
    try {
      if (testOnServer()) {
        const options = pack ? { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pack }) } : { method: "POST" };
        const result = await request("/api/time-checks/test", options);
        say(`${result?.message || "Time check played"}.`);
        return;
      }
      const refusal = prepareTest();
      if (refusal) {
        say(refusal);
        return;
      }
      if (await playHere(pack)) say("Time check played.");
      else say("Time check was not played.");
    } catch (error) {
      const left = staleActionText(error);
      if (left) say(left, error.actionDone ? "info" : "error");
      else say(`Time check failed: ${error.message}`, "error");
    }
  }

  // The check after the hour, once, when the browser can play it (not
  // during a crossfade, a pause or before the audio has started).
  function playPending() {
    if (!pending || playing || testOnServer() || !canPlay()) return;
    pending = false;
    playing = true;
    void playHere().catch(() => {}).finally(() => { playing = false; });
  }

  for (const control of [enabled, format, rule, order]) control.addEventListener("change", save);
  test.addEventListener("click", () => withDisabled(test, testNow));
  if (hasFiles) {
    for (const select of [files.folder, files.intro, files.outro]) {
      select.addEventListener("change", save);
    }
    files.packAdd.addEventListener("click", () => busyWhile(files.packAdd, () => addPack()));
    files.clipAdd.addEventListener("click", () => busyWhile(files.clipAdd, () => addClip()));
    files.rescan.addEventListener("click", () => busyWhile(files.rescan, rescan));
    for (const which of ["pack", "clip"]) {
      uploads[which].input.addEventListener("change", () => clearUploadError(which));
    }
  }
  if (hasDates) {
    dates.pack.addEventListener("change", fillDates);
    dates.save.addEventListener("click", () => withDisabled(dates.save, saveDates));
    for (const field of [dates.from, dates.until]) {
      field.addEventListener("input", () => {
        field.removeAttribute("aria-invalid");
        field.removeAttribute("aria-describedby");
      });
    }
  }
  panel?.addEventListener("toggle", () => {
    if (panel.open) void refresh();
  });
  interval(playPending, 1000);
  void refresh();

  return {
    refresh,
    clear,
    noteTrack(state) {
      const path = state?.current_track?.path;
      if (!path || path === previousTrack) return;
      if (!settings) {
        previousTrack = path;
        return;
      }
      const currentHour = hourKey(now());
      const crossed = previousHour !== null && previousHour !== currentHour;
      previousTrack = path;
      previousHour = currentHour;
      if (crossed && settings.enabled && settings.rule === "after_hour_song"
          && !testOnServer()) {
        pending = true;
        playPending();
      }
    },
  };
}
