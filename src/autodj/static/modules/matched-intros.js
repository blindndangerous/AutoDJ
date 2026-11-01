// Track info, Matched intros (#127): voice clips that play as a song starts,
// for this song or for any song by its artist.  Upload, Listen and Remove.
//
// Shared collection tables preserve row and menu identity on updates.  A refused field is marked
// aria-invalid, its error is linked to it with aria-describedby and it takes
// focus, which reads the error, so nothing else speaks.  Any other result is
// said once in #intros-status.  Listen is a toggle: while an intro plays, its
// menu item says Stop listening, and the clip plays on the Preview output device.
import { createEditorDialog } from "./editor-dialog.js";
import { createCollectionTable } from "./collection-table.js";
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson, failureText } from "./api-client.js";
import { deleteCollectionItem } from "./collection-delete.js";
import { clearFieldError, sentences, showFieldError, applyServerFieldError, focusAfterDialog } from "./dom-helpers.js";
import { makeSay } from "./live-region.js";
import { stationApi } from "./stations.js";

const FIELDS = ["file", "applies", "label", "chance", "cooldown", "not-before", "not-after"];
// The server's field names, mapped to the page's.
const SERVER_FIELDS = {
  file: "file", applies_to: "applies", track_id: "file", label: "label", chance: "chance",
  cooldown_minutes: "cooldown", not_before: "not-before", not_after: "not-after",
};
const EXTENSIONS = [".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"];
const MAX_COOLDOWN = 7 * 24 * 60;

// The error for the upload form's values, as [field, message], or null.
// The server checks the same rules again and names the field it refuses.
export function introFormError({ file, chance, cooldown, notBefore, notAfter }, maxBytes = 0) {
  if (!file) return ["file", "Choose an audio file for the intro."];
  const name = String(file.name || "").toLowerCase();
  if (!EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return ["file", "Choose an MP3, WAV, Ogg, M4A, FLAC or AAC file."];
  }
  if (maxBytes > 0 && file.size > maxBytes) {
    return ["file", `That file is too large: an intro file may be at most ${Math.floor(maxBytes / (1024 * 1024))} MB.`];
  }
  const chanceText = String(chance ?? "").trim();
  if (chanceText && (!/^\d{1,3}$/.test(chanceText) || Number(chanceText) < 1 || Number(chanceText) > 100)) {
    return ["chance", "Enter the chance as a whole number from 1 to 100."];
  }
  const waitText = String(cooldown ?? "").trim();
  if (waitText && (!/^\d{1,5}$/.test(waitText) || Number(waitText) > MAX_COOLDOWN)) {
    return ["cooldown", `Enter the wait as a whole number of minutes from 0 to ${MAX_COOLDOWN}.`];
  }
  if (notBefore && notAfter && notAfter < notBefore) {
    return ["not-after", "Do not play after must be on or after the Not before date."];
  }
  return null;
}

const NO_EDITOR = { load() {}, clear() {}, destroy() {} };

export function installMatchedIntros({ document: doc = document, request = requestJson, isCurrent = () => true } = {}) {
  const section = doc.getElementById("intros-editor");
  const form = doc.getElementById("intros-form");
  if (!section || !form) return NO_EDITOR;
  const el = (id) => doc.getElementById(id);
  const status = el("intros-status");
  const inactive = el("intros-inactive");
  const problem = el("intros-problem");
  const lists = { song: el("intros-song-list"), artist: el("intros-artist-list") };
  const artistHeading = el("intros-artist-heading");
  const artistChoice = el("intro-applies-artist");
  const artistLabel = el("intro-applies-artist-label");
  const playback = el("intro-playback");
  const addButton = el("intro-add");
  const inputs = {
    file: el("intro-file"), applies: el("intro-applies-song"), label: el("intro-label"),
    chance: el("intro-chance"), cooldown: el("intro-cooldown"),
    "not-before": el("intro-not-before"), "not-after": el("intro-not-after"),
  };
  const errors = Object.fromEntries(FIELDS.map((field) => [field, el(`intro-${field}-error`)]));
  let track = null;  // { id, title, artist }
  let limits = {};
  let generation = 0;
  let playing = null;  // { kind, id } of the intro playing now
  let saved = { song: [], artist: [] };
  const listSignatures = { song: null, artist: null };
  let listFocusAnnouncementSuppressed = false;

  const editor = createEditorDialog({ form, heading: el("intros-form-legend"), cancel: el("intro-cancel"), fallback: addButton,
    save: submit, pendingText: null, onCancel: () => { resetForm(); say("Cancelled. Nothing was saved."); } });
  const say = makeSay(() => editor.region(status));
  const collections = Object.fromEntries(Object.entries(lists).map(([kind, host]) => {
    const heading = el(host.getAttribute("aria-labelledby"));
    heading.tabIndex = -1;
    return [kind, createCollectionTable({
      host, label: kind === "song" ? "song intros" : "artist intros", keyOf: (item) => item.id,
      nameOf: (item) => item.label || "Untitled intro",
      columns: [
        { key: "name", label: "Name", value: (item) => item.label || "Untitled intro" },
        { key: "details", label: "Details", value: (item) => {
          const seconds = Math.round(Number(item.speech_s) || 0);
          return sentences(item.rules_text, item.dates_text,
            item.playable === false && `Skipped now: ${item.unplayable_text || "Unavailable"}`,
            `${seconds} second${seconds === 1 ? "" : "s"} of speech`);
        } },
      ],
      actions: (item) => [
        { id: "listen", label: isPlaying(kind, item) ? "Stop listening" : "Listen", run: () => void listen(kind, item) },
        { id: "remove", label: "Remove", run: (button) => void remove(kind, item, button) },
      ],
      fallback: heading, status, emptyText: kind === "song" ? "No intros for this song." : "No intros for the artist.",
    })];
  }));
  function clearError(field) {
    clearFieldError(inputs[field], errors[field]);
  }
  function clearErrors() { for (const field of FIELDS) clearError(field); }
  function showError(field, message) {
    showFieldError(inputs[field], message, errors[field], editor.region(status));
  }

  // Listen's label follows *playing*; refresh the menus of these lists.
  function relabel(...kinds) {
    for (const kind of new Set(kinds)) if (kind) collections[kind].update(saved[kind], { announceFocus: false });
  }

  // One id can be in both lists; the list it plays from tells them apart.
  function isPlaying(kind, item) {
    return playing?.kind === kind && playing.id === item.id;
  }

  function stopListening() {
    playing = null;
    if (playback && playback.getAttribute("src")) {
      playback.pause();
      playback.removeAttribute("src");
      playback.load();
    }
  }

  async function listen(kind, item) {
    const was = playing;
    stopListening();
    if ((was?.kind === kind && was.id === item.id) || !playback) { relabel(was?.kind); return; }
    playing = { kind, id: item.id };
    relabel(was?.kind, kind);
    // An element's own request, so it is sent to the page's station here.
    playback.src = stationApi(`/api/intros/${encodeURIComponent(item.id)}/audio`);
    try {
      await playback.play();
    } catch (_) {
      if (isPlaying(kind, item)) { stopListening(); relabel(kind); }
      say("This browser could not play the intro.", "error");
    }
  }

  async function remove(kind, item, button) {
    const list = lists[kind];
    const index = saved[kind].findIndex((entry) => entry.id === item.id);
    const name = item.label || "the untitled intro";
    const epoch = captureAuthenticatedRequestEpoch();
    let result = null;
    let tone = "info";
    const confirmed = await deleteCollectionItem(doc, {
      title: `Remove intro ${name}?`,
      message: "Its audio is deleted from the station.",
      confirmLabel: "Remove intro",
      onConfirm: async () => {
        try {
          await request(`/api/intros/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        } catch (error) {
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          result = failureText(`remove the intro`, error);
          tone = "error";
          return button.closest("td");
        }
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        if (playing?.kind === kind) stopListening();
        result = `Removed ${name}.`;
        listFocusAnnouncementSuppressed = true;
        try { await refresh(); } finally { listFocusAnnouncementSuppressed = false; }
        // Focus goes to the next row's button in that list, or the list's heading.
        const heading = doc.getElementById(list.getAttribute("aria-labelledby"));
        const target = collections[kind].target(saved[kind][Math.min(index, saved[kind].length - 1)]?.id, "options") || heading;
        if (target === heading) heading.setAttribute("tabindex", "-1");
        return target;
      },
    });
    if (!confirmed) return;
    if (result && isAuthenticatedRequestCurrent(epoch)) say(result, tone);
  }

  function renderList(kind, items) {
    const list = lists[kind];
    if (playing?.kind === kind && !items.some((item) => item.id === playing.id)) stopListening();
    const heading = doc.getElementById(list.getAttribute("aria-labelledby"));
    if (heading && heading.tabIndex !== -1) heading.tabIndex = -1;
    const signature = JSON.stringify(items);
    if (signature === listSignatures[kind]) return;
    listSignatures[kind] = signature;
    collections[kind].update(items, { announceFocus: !listFocusAnnouncementSuppressed });
  }

  function apply(data) {
    saved = { song: data.song || [], artist: data.artist || [] };
    limits = data.limits || {};
    track = data.track || track;
    const artist = track?.artist || "";
    artistHeading.textContent = artist ? `For any song by ${artist}` : "For the artist";
    artistLabel.textContent = artist ? `Any song by ${artist}` : "Any song by the artist (this song has no artist)";
    artistChoice.disabled = !artist;
    if (!artist && artistChoice.checked) inputs.applies.checked = true;
    inactive.textContent = data.active === false ? data.reason || "" : "";
    inactive.hidden = data.active !== false;
    if (problem) {
      problem.textContent = data.problem || "";
      problem.hidden = !data.problem;
    }
    renderList("song", data.song || []);
    renderList("artist", data.artist || []);
  }

  async function refresh() {
    if (!track) return;
    const mine = generation;
    const epoch = captureAuthenticatedRequestEpoch();
    const data = await request(`/api/intros?track_id=${encodeURIComponent(track.id)}`);
    if (mine !== generation || !isAuthenticatedRequestCurrent(epoch) || !isCurrent()) return;
    apply(data);
  }

  function resetForm() {
    clearErrors();
    inputs.file.value = "";
    inputs.label.value = "";
    inputs.chance.value = "100";
    inputs.cooldown.value = "60";
    inputs["not-before"].value = "";
    inputs["not-after"].value = "";
  }

  function load(record) {
    generation += 1;
    editor.close();
    stopListening();
    const music = !!record?.id && (record.kind || "music") === "music";
    if (!music) { clear(); return; }
    track = { id: String(record.id), title: record.title || "", artist: record.artist || "" };
    resetForm();
    inputs.applies.checked = true;
    status.textContent = "";
    apply({ track, song: [], artist: [], active: true });
    section.hidden = false;
    void refresh().catch((error) => {
      if (isCurrent()) say(failureText(`load the matched intros`, error), "error");
    });
  }

  function clear() {
    generation += 1;
    editor.close();
    stopListening();
    track = null;
    resetForm();
    status.textContent = "";
    listFocusAnnouncementSuppressed = true;
    try { for (const kind of ["song", "artist"]) renderList(kind, []); }
    finally { listFocusAnnouncementSuppressed = false; }
    if (problem) { problem.textContent = ""; problem.hidden = true; }
    section.hidden = true;
  }

  async function submit(event, active) {
    event.preventDefault();
    if (!track) return;
    clearErrors();
    const file = inputs.file.files?.[0] || null;
    const values = {
      file,
      chance: inputs.chance.value,
      cooldown: inputs.cooldown.value,
      notBefore: inputs["not-before"].value,
      notAfter: inputs["not-after"].value,
    };
    const problem = introFormError(values, Number(limits.max_bytes) || 0);
    if (problem) { showError(problem[0], problem[1]); return; }
    const appliesTo = artistChoice.checked && !artistChoice.disabled ? "artist" : "song";
    const body = new FormData();
    body.append("track_id", track.id);
    body.append("applies_to", appliesTo);
    body.append("label", inputs.label.value.trim());
    body.append("chance", values.chance.trim());
    body.append("cooldown_minutes", values.cooldown.trim());
    body.append("not_before", values.notBefore);
    body.append("not_after", values.notAfter);
    body.append("file", file, file.name);
    const mine = generation;
    const epoch = captureAuthenticatedRequestEpoch();
    say("Uploading the intro.");
    const stale = () => mine !== generation || !isAuthenticatedRequestCurrent(epoch) || !isCurrent() || !active();
    let made;
    try {
      made = await request("/api/intros", { method: "POST", body });
    } catch (error) {
      if (mine !== generation || !isAuthenticatedRequestCurrent(epoch)) return;
      const field = SERVER_FIELDS[error.field];
      if (!field || !applyServerFieldError(error, { [error.field]: inputs[field] }, errors[field], editor.region(status))) {
        say(failureText(`add the intro`, error), "error");
      }
      return;
    }
    if (stale()) return;
    // The intro is on the server now.  A list that cannot be reloaded must
    // not keep the form open, or a second Upload would add it twice.
    resetForm();
    let reloadFailure = null;
    try { await refresh(); } catch (error) { reloadFailure = error; }
    if (stale()) return;
    editor.close();
    focusAfterDialog(collections[appliesTo].target(made.id) || addButton);
    const target = appliesTo === "artist" ? `any song by ${track.artist}` : "this song";
    const added = `Added the intro ${made.label || "untitled intro"} for ${target}.`;
    if (reloadFailure) say(`${added} ${failureText(`load the matched intros`, reloadFailure)}`, "error");
    else say(added);
  }

  const onInput = (event) => {
    const field = FIELDS.find((name) => inputs[name] === event.target || (name === "applies" && event.target?.name === "intro-applies"));
    if (field && (field === "applies" ? errors.applies.hidden === false : inputs[field].getAttribute("aria-invalid") === "true")) clearError(field);
  };
  const onEnded = () => {
    const was = playing;
    stopListening();
    relabel(was?.kind);
  };
  const openForm = () => { resetForm(); editor.open(inputs.file, addButton); };
  addButton.addEventListener("click", openForm);
  form.addEventListener("input", onInput);
  form.addEventListener("change", onInput);
  playback?.addEventListener("ended", onEnded);

  return {
    load,
    clear,
    // Another station's intros for the track shown (#70).
    refresh: () => refresh(),
    destroy() {
      addButton.removeEventListener("click", openForm);
      form.removeEventListener("input", onInput);
      form.removeEventListener("change", onInput);
      playback?.removeEventListener("ended", onEnded);
      clear();
      editor.destroy();
    },
  };
}
