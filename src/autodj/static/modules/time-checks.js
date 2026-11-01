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

import { failureText,
  isStaleStationReply, requestBinary, requestJson, staleActionText, withDisabled,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { makeSay } from "./live-region.js";
import { clearFieldError, showFieldError, applyServerFieldError } from "./dom-helpers.js";
import { onCardOpen } from "./card-state.js";
import { createSettingsSave } from "./standalone-settings.js";
import { registerSettingsGroup, settingsDraftHolds } from "./settings-drafts.js";

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
    pack: { input: files.packFile, error: files.packError },
    clip: { input: files.clipFile, error: files.clipError },
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

  const say = makeSay(status, { dwellMs: 4000, errorDwellMs: 6000 });

  function hourKey(date) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: settings?.timezone || "UTC", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", hourCycle: "h23", timeZoneName: "shortOffset",
    }).formatToParts(date);
    return parts.filter((part) => part.type !== "literal").map((part) => part.value).join(":");
  }

  const packBoxes = () => [...packList.querySelectorAll("input[type=checkbox]")];
  let shownPacks = "[]";
  let targetPackChoices = null;
  let targetPackOrder = null;

  // One native checkbox per pack, its coverage as its description, and a
  // Remove button for a pack kept in the station's time checks folder.  The
  // rows are rebuilt only when the packs change, so a refresh never takes
  // focus away from a checkbox.
  function showPacks(packs, { preserveChoices = false } = {}) {
    const names = packs.map((pack) => pack.name);
    const existingBoxes = packBoxes();
    const selected = preserveChoices
      ? new Set(existingBoxes.filter((box) => box.checked).map((box) => box.value))
      : null;
    if (preserveChoices) {
      const present = new Set(existingBoxes.map((box) => box.value));
      for (const name of targetPackChoices || []) if (!present.has(name)) selected.add(name);
    }
    const key = JSON.stringify(packs.map((pack) => [pack.name, Boolean(pack.removable)]));
    const rebuilt = key !== shownPacks;
    if (rebuilt) {
      shownPacks = key;
      packList.replaceChildren(...packs.map((pack, index) => {
        const row = doc.createElement("div");
        row.className = "settings-check";
        const box = doc.createElement("input");
        box.type = "checkbox";
        box.id = `tc-pack-${index}`;
        box.value = pack.name;
        box.setAttribute("aria-describedby", `tc-pack-${index}-desc`);
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
      if (preserveChoices) {
        if (rebuilt) box.checked = selected.has(box.value);
      } else if (doc.activeElement !== box) box.checked = packs[index].enabled;
      get(`pack-${index}-desc`).textContent = packWords(packs[index]);
    });
    packsEmpty.hidden = packs.length > 0;
    showDates(packs);
    if (targetPackChoices?.length) {
      const present = new Set(names);
      targetPackChoices = targetPackChoices.filter((name) => !present.has(name));
      if (!targetPackChoices.length) targetPackChoices = null;
    }
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

  const clearDatesError = () => clearFieldError([dates.from, dates.until], dates.error);
  const datesError = (field, message) => showFieldError(field, message, dates.error, status);

  async function saveDates(current) {
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
      const saved = await request(`/api/time-checks/packs/${encodeURIComponent(name)}/dates`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...(from ? { not_before: from } : {}), ...(until ? { not_after: until } : {}) }),
      });
      if (!current()) return;
      settings = saved;
      if (!draft.dirty()) draft.accept(settings);
      const pack = settings.packs?.find((item) => item.name === name);
      say(pack?.dates_text ? `Saved. Voice pack ${name}: ${pack.dates_text}.` : `Saved. Voice pack ${name} plays on any day.`);
    } catch (error) {
      if (!current()) return;
      const left = staleActionText(error);
      if (left) {
        say(left, error.actionDone ? "info" : "error");
        return;
      }
      const message = failureText("save the play dates.", error);
      if (!applyServerFieldError({ field: error.field, message }, { not_before: dates.from, not_after: dates.until }, dates.error, status)) say(message, "error");
    }
  }

  const datesSaver = createSettingsSave({ button: dates.save, save: saveDates, isCurrent: () => Boolean(settings) });

  function showSettings({ preserveControls = false } = {}) {
    if (!preserveControls) {
      if (doc.activeElement !== enabled) enabled.checked = settings.enabled;
      if (doc.activeElement !== format) format.value = settings.hour_format;
      if (doc.activeElement !== rule) rule.value = settings.rule;
      if (doc.activeElement !== order) order.value = settings.pack_order;
    }
    showPacks(settings.packs || [], { preserveChoices: preserveControls });
    if (!preserveControls) showClips();
    const recorded = MINUTES - settings.missing_minutes;
    const fallback = settings.generated_set
      ? `Generated set ${settings.generated_set} supplies local audio. Missing generated clips are skipped.`
      : settings.tts_available
      ? "Text to speech says the minutes without a clip."
      : "No text to speech is set up, so minutes without a clip are skipped.";
    clips.textContent = `${recorded} of ${MINUTES} minute clips recorded. ${fallback} `
      + `Times are in the station time zone, ${settings.timezone}.`;
  }

  const read = () => {
    const checkedPacks = packBoxes().filter((box) => box.checked).map((box) => box.value);
    const selectedPacks = new Set([...checkedPacks, ...(targetPackChoices || [])]);
    const body = { enabled: enabled.checked, hour_format: format.value, rule: rule.value,
      enabled_packs: [...new Set([
        ...(targetPackOrder || []).filter((name) => selectedPacks.has(name)),
        ...checkedPacks,
        ...(targetPackChoices || []),
      ])],
      pack_order: order.value };
    if (hasFiles) {
      body.recorded_folder = files.folder.value;
      body.intro = clipChoice("intro");
      body.outro = clipChoice("outro");
    }
    return body;
  };
  const draft = registerSettingsGroup({
    panel, group: "time_checks",
    controls: () => [enabled, format, rule, order, ...packBoxes(),
      ...(hasFiles ? [files.folder, files.intro, files.outro] : [])],
    read,
    write(value) {
      settings = { ...settings, ...value };
      if (Array.isArray(value.enabled_packs)) {
        targetPackOrder = [...value.enabled_packs];
        const rendered = new Set(packBoxes().map((box) => box.value));
        targetPackChoices = value.enabled_packs.filter((name) => !rendered.has(name));
        if (!targetPackChoices.length) targetPackChoices = null;
      }
      if (Array.isArray(value.enabled_packs) && Array.isArray(settings.packs)) {
        const selected = new Set(value.enabled_packs);
        settings.packs = settings.packs.map((pack) => ({ ...pack, enabled: selected.has(pack.name) }));
      }
      if (value.enabled === false || value.rule === "off") pending = false;
      showSettings();
    },
    validate() {
      if (!settings) {
        say("Time check settings are still loading; try again in a moment.", "error");
        return false;
      }
      return true;
    },
  });

  function acceptSettings(value) {
    settings = value;
    draft.accept(value);
    if (!draft.dirty()) showSettings({ preserveControls: settingsDraftHolds(enabled) });
  }

  // Background loads write into the panel; they are not announced.
  async function refresh() {
    const mine = ++generation;
    try {
      const loaded = await request("/api/time-checks");
      if (mine !== generation) return;
      if (previousTrack && previousHour === null) previousHour = hourKey(now());
      acceptSettings(loaded);
    } catch (error) {
      // A reply for the station the page has just left says nothing (#70).
      if (mine !== generation || isStaleStationReply(error)) return;
      clips.textContent = failureText("load time checks", error);
    }
  }

  // A station switch: what this panel showed was the old station's.
  function clear() {
    datesSaver.clear();
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

  // A refused upload is shown under its file input, linked to it and
  // focused (WCAG 3.3.1), like a refused play date, and said.  Choosing
  // another file clears it.
  function uploadError(which, message) {
    const { input, error } = uploads[which];
    showFieldError(input, message, error, status);
  }

  function clearUploadError(which) {
    const { input, error } = uploads[which];
    clearFieldError(input, error);
  }

  // A failure answered after a station switch is only said; any other is
  // shown at the file input.
  function uploadFailure(which, error, action) {
    const left = staleActionText(error);
    if (left) say(left, error.actionDone ? "info" : "error");
    else uploadError(which, failureText(action, error));
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
      uploadFailure("pack", error, "add the voice pack.");
      return;
    }
    acceptSettings(reply);
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
      message: `Its folder and audio files are deleted from the time checks folder, with its tick, play dates and saved profile/default selections.${sharedNote(pack)}`,
      confirmLabel: "Remove voice pack",
      onConfirm: async () => {
        try {
          settings = await request(`/api/time-checks/packs/${encodeURIComponent(name)}`, { method: "DELETE" });
        } catch (error) {
          const left = staleActionText(error);
          result = left || failureText(`remove voice pack ${name}.`, error);
          tone = left && error.actionDone ? "info" : "error";
          return left ? null : button;
        }
        acceptSettings(settings);
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
      uploadFailure("clip", error, "upload the clip.");
      return;
    }
    acceptSettings(reply);
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
      say(failureText("look for voice packs.", error), error.actionDone ? "info" : "error");
      return;
    }
    acceptSettings(settings);
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

  test.addEventListener("click", () => withDisabled(test, testNow));
  if (hasFiles) {
    files.packAdd.addEventListener("click", () => withDisabled(files.packAdd, () => addPack()));
    files.clipAdd.addEventListener("click", () => withDisabled(files.clipAdd, () => addClip()));
    files.rescan.addEventListener("click", () => withDisabled(files.rescan, rescan));
    for (const which of ["pack", "clip"]) {
      uploads[which].input.addEventListener("change", () => clearUploadError(which));
    }
  }
  if (hasDates) {
    dates.pack.addEventListener("change", fillDates);
    dates.save.addEventListener("click", datesSaver.run);
    for (const field of [dates.from, dates.until]) {
      field.addEventListener("input", () => {
        clearFieldError(field, dates.error);
      });
    }
  }
  onCardOpen(panel, refresh);
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
