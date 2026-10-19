// Track info, Matched intros (#127): voice clips that play as a song starts,
// for this song or for any song by its artist.  Upload, Listen and Remove.
//
// The lists are drawn again after each change, keeping focus on a Listen or
// Remove button that is still there.  A refused field is marked
// aria-invalid, its error is linked to it with aria-describedby and it takes
// focus, which reads the error, so nothing else speaks.  Any other result is
// said once in #intros-status.  Listen is a toggle: aria-pressed says it is
// playing, and the clip plays on the Preview output device.
import { captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson } from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { nextRowControl, reconcileRows, rowButton } from "./dom-helpers.js";
import { installActionListNavigation } from "./action-list-navigation.js";
import { announceStatus } from "./live-region.js";
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

// One intro in words: its label, how often it plays, its dates, its length.
export function introText(item) {
  const parts = [`${item.label || "Untitled intro"}.`, item.rules_text];
  if (item.dates_text) parts.push(`${item.dates_text}.`);
  if (item.playable === false && item.unplayable_text) parts.push(`Skipped now: ${item.unplayable_text}.`);
  const seconds = Math.round(Number(item.speech_s) || 0);
  parts.push(`${seconds} second${seconds === 1 ? "" : "s"} of speech.`);
  return parts.filter(Boolean).join(" ");
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
  const navigation = Object.fromEntries(Object.entries(lists).map(([kind, list]) => [kind, installActionListNavigation(list)]));
  const empties = { song: el("intros-song-empty"), artist: el("intros-artist-empty") };
  const artistHeading = el("intros-artist-heading");
  const artistChoice = el("intro-applies-artist");
  const artistLabel = el("intro-applies-artist-label");
  const playback = el("intro-playback");
  const upload = el("intro-upload");
  const inputs = {
    file: el("intro-file"), applies: el("intro-applies-song"), label: el("intro-label"),
    chance: el("intro-chance"), cooldown: el("intro-cooldown"),
    "not-before": el("intro-not-before"), "not-after": el("intro-not-after"),
  };
  const errors = Object.fromEntries(FIELDS.map((field) => [field, el(`intro-${field}-error`)]));
  let track = null;  // { id, title, artist }
  let limits = {};
  let generation = 0;
  let busy = false;
  let playing = null;  // the Listen button pressed now
  let saved = { song: [], artist: [] };
  let listFocusAnnouncementSuppressed = false;

  function say(message, tone = "info") { announceStatus(status, message, { force: true, tone }); }
  function linkError(field, linked) {
    const input = inputs[field];
    const error = errors[field];
    const ids = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== error.id);
    if (linked) ids.push(error.id);
    if (ids.length) input.setAttribute("aria-describedby", ids.join(" "));
    else input.removeAttribute("aria-describedby");
  }
  function clearError(field) {
    inputs[field].removeAttribute("aria-invalid");
    errors[field].textContent = "";
    errors[field].hidden = true;
    linkError(field, false);
  }
  function clearErrors() { for (const field of FIELDS) clearError(field); }
  function showError(field, message) {
    inputs[field].setAttribute("aria-invalid", "true");
    errors[field].textContent = message;
    errors[field].hidden = false;
    linkError(field, true);
    inputs[field].focus();
  }

  function stopListening() {
    if (playing) playing.setAttribute("aria-pressed", "false");
    playing = null;
    if (playback && playback.getAttribute("src")) {
      playback.pause();
      playback.removeAttribute("src");
      playback.load();
    }
  }

  async function listen(item, button) {
    if (playing === button) { stopListening(); return; }
    stopListening();
    if (!playback) return;
    playing = button;
    button.setAttribute("aria-pressed", "true");
    // An element's own request, so it is sent to the page's station here.
    playback.src = stationApi(`/api/intros/${encodeURIComponent(item.id)}/audio`);
    try {
      await playback.play();
    } catch (_) {
      if (playing === button) stopListening();
      say("This browser could not play the intro.", "error");
    }
  }

  async function remove(item, button) {
    const list = button.closest("ul");
    const index = Array.from(list.querySelectorAll("button")).indexOf(button);
    const name = item.label || "the untitled intro";
    const epoch = captureAuthenticatedRequestEpoch();
    let result = null;
    let tone = "info";
    const confirmed = await confirmAction(doc, {
      title: `Remove intro ${name}?`,
      message: "Its audio is deleted from the station.",
      confirmLabel: "Remove intro",
      onConfirm: async () => {
        try {
          await request(`/api/intros/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        } catch (error) {
          if (!isAuthenticatedRequestCurrent(epoch)) return null;
          result = `Could not remove the intro: ${error.message}`;
          tone = "error";
          return button;
        }
        if (!isAuthenticatedRequestCurrent(epoch)) return null;
        if (playing && list.contains(playing)) stopListening();
        result = `Removed ${name}.`;
        listFocusAnnouncementSuppressed = true;
        try { await refresh(); } finally { listFocusAnnouncementSuppressed = false; }
        // Focus goes to the next row's button in that list, or the list's heading.
        const heading = doc.getElementById(list.getAttribute("aria-labelledby"));
        const target = nextRowControl(list, "button", Math.max(0, index), heading);
        if (target === heading) heading.setAttribute("tabindex", "-1");
        return target;
      },
    });
    if (!confirmed) return;
    if (result && isAuthenticatedRequestCurrent(epoch)) say(result, tone);
  }

  function renderList(kind, items) {
    const list = lists[kind];
    if (playing && list.contains(playing) && !items.some((item) => item.id === playing.dataset.intro)) stopListening();
    const heading = doc.getElementById(list.getAttribute("aria-labelledby"));
    if (heading && heading.tabIndex !== -1) heading.tabIndex = -1;
    reconcileRows(list, items, (item) => item.id, () => {
      const row = doc.createElement("li");
      const text = doc.createElement("span");
      text.className = "intro-text";
      const play = rowButton(doc, "Listen", "", (button) => {
        const current = saved[kind].find((entry) => entry.id === button.dataset.intro);
        if (current) void listen(current, button);
      });
      const drop = rowButton(doc, "Remove", "", (button) => {
        const current = saved[kind].find((entry) => entry.id === button.dataset.intro);
        if (current) void remove(current, button);
      });
      play.dataset.action = "listen";
      drop.dataset.action = "remove";
      row.append(text, play, drop);
      return row;
    }, (row, item) => {
      const name = item.label || "untitled intro";
      const description = introText(item);
      if (row.firstElementChild.textContent !== description) row.firstElementChild.textContent = description;
      const [play, drop] = row.querySelectorAll("button");
      for (const button of [play, drop]) if (button.dataset.intro !== item.id) button.dataset.intro = item.id;
      if (play.getAttribute("aria-label") !== `Listen to ${name}`) play.setAttribute("aria-label", `Listen to ${name}`);
      if (drop.getAttribute("aria-label") !== `Remove ${name}`) drop.setAttribute("aria-label", `Remove ${name}`);
      const pressed = playing === play ? "true" : "false";
      if (play.getAttribute("aria-pressed") !== pressed) play.setAttribute("aria-pressed", pressed);
    }, { fallback: heading, onFocusLost: ({ target }) => {
      if (!listFocusAnnouncementSuppressed) say(`The matched intro list changed. Focus moved to ${target?.getAttribute("aria-label") || target?.textContent.trim() || "its heading"}.`);
    } });
    navigation[kind].refresh();
    const emptyHidden = items.length > 0;
    if (empties[kind].hidden !== emptyHidden) empties[kind].hidden = emptyHidden;
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
      if (isCurrent()) say(`Could not load the matched intros: ${error.message}`, "error");
    });
  }

  function clear() {
    generation += 1;
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

  async function submit(event) {
    event.preventDefault();
    if (busy || !track) return;
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
    // busy holds off another press; no aria-disabled, or NVDA says
    // "unavailable" on the pressed button (#331).  aria-busy says it is busy.
    busy = true;
    upload.classList.add("is-pending");
    upload.setAttribute("aria-busy", "true");
    say("Uploading the intro.");
    try {
      const made = await request("/api/intros", { method: "POST", body });
      if (mine !== generation || !isAuthenticatedRequestCurrent(epoch) || !isCurrent()) return;
      resetForm();
      await refresh();
      const target = appliesTo === "artist" ? `any song by ${track.artist}` : "this song";
      say(`Added the intro ${made.label || "untitled intro"} for ${target}.`);
    } catch (error) {
      if (mine !== generation || !isAuthenticatedRequestCurrent(epoch)) return;
      const field = SERVER_FIELDS[error.field];
      if (field && inputs[field]) showError(field, error.message);
      else say(`Could not add the intro: ${error.message}`, "error");
    } finally {
      busy = false;
      upload.classList.remove("is-pending");
      upload.removeAttribute("aria-busy");
    }
  }

  const onInput = (event) => {
    const field = FIELDS.find((name) => inputs[name] === event.target || (name === "applies" && event.target?.name === "intro-applies"));
    if (field && (field === "applies" ? errors.applies.hidden === false : inputs[field].getAttribute("aria-invalid") === "true")) clearError(field);
  };
  const onEnded = () => stopListening();
  form.addEventListener("submit", submit);
  form.addEventListener("input", onInput);
  form.addEventListener("change", onInput);
  playback?.addEventListener("ended", onEnded);

  return {
    load,
    clear,
    // Another station's intros for the track shown (#70).
    refresh: () => refresh(),
    destroy() {
      form.removeEventListener("submit", submit);
      form.removeEventListener("input", onInput);
      form.removeEventListener("change", onInput);
      playback?.removeEventListener("ended", onEnded);
      clear();
    },
  };
}
