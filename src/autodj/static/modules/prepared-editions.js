// Prepared editions belong to a saved show; audition belongs to programme review.
import {
  captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, requestJson, withDisabled,
} from "./api-client.js";
import { confirmAction } from "./confirm-dialog.js";
import { fmtTime } from "./dom-helpers.js";
import { announceStatus } from "./live-region.js";
import { stationApi, stationEpoch } from "./stations.js";
import {
  INSECURE_TEXT, NO_RECORDER_TEXT, microphoneProblem, recordingToWav,
} from "./voice-tracker.js";

const RECORDING_SECONDS = 180;

export function installPreparedEditions({
  doc = document, request = requestJson, announce = null, onChange = () => {},
  confirm = (options) => confirmAction(doc, options),
  media = () => globalThis.navigator?.mediaDevices, Recorder = () => globalThis.MediaRecorder,
  toWav = recordingToWav, objectUrl = (blob) => URL.createObjectURL(blob),
  revokeUrl = (url) => URL.revokeObjectURL(url),
  setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id),
} = {}) {
  const get = (id) => doc.getElementById(`edition-${id}`);
  const root = get("card"), panel = doc.getElementById("panel-library");
  if (!root) return { openShow: async () => {}, confirmLeave: async () => true, clear() {}, active: () => false, addSong: async () => {}, destroy() {} };
  let show = null, edition = null, generation = 0, recording = null, starting = false, localTake = null;
  let busy = false, rerecord = null, timer = null, clock = null;
  let loadNumber = 0, refreshNumber = 0;
  const receipts = new Map(), requested = new Map();
  const context = () => [generation, stationEpoch(), captureAuthenticatedRequestEpoch()];
  const current = ([mine, station, auth]) => mine === generation && station === stationEpoch()
    && isAuthenticatedRequestCurrent(auth);
  const endpoint = (tail = "") => `/api/editions/${encodeURIComponent(edition.id)}${tail}`;
  const say = (text) => {
    if (edition?.committed_audio_notice && !text.includes(edition.committed_audio_notice))
      text += ` ${edition.committed_audio_notice}`;
    if (announce) { get("status").textContent = text; announce(text); }
    else announceStatus(get("status"), text);
  };
  const send = (url, method, value) => request(url, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
  });
  function fail(error, ticket, control = null) {
    if (!current(ticket)) return;
    if (control) {
      if (control.matches("input, select, textarea")) control.setAttribute("aria-invalid", "true");
      control.focus();
    }
    say(error.message || "The edition request failed. Reload the edition and try again.");
  }
  function button(action, label, handler) {
    const value = doc.createElement("button"); value.type = "button";
    value.dataset.action = action; value.textContent = label;
    value.addEventListener("click", handler); return value;
  }
  function assets() {
    return Array.isArray(edition.assets) ? edition.assets : Object.values(edition.assets || {});
  }
  function asset(id) { return assets().find((value) => value.id === id); }
  function label(item, index) {
    if (item.label || item.title) return item.label || item.title;
    const song = show?.songs?.find((value) => value.path === item.path);
    return song?.title || asset(item.asset_id)?.label || `${item.kind === "segment" ? "Segment" : "Song"} ${index + 1}`;
  }
  function option(value, text) {
    const node = doc.createElement("option"); node.value = value; node.textContent = text; return node;
  }
  function stopAudio() { get("audio").pause(); get("audio").removeAttribute("src"); get("audio").load(); }
  function dropLocal() {
    if (localTake) { revokeUrl(localTake.url); localTake = null; }
    get("save-take").disabled = true;
  }
  async function confirmRecordingReplacement() {
    if (!localTake && !recording && !starting) return true;
    const ticket = context(), take = localTake;
    const accepted = await confirm({ title: "Discard unsaved recording?",
      message: "This action will discard the local recording. Save recording first if you want to keep it. Saved takes are unchanged.",
      confirmLabel: "Discard recording" });
    return accepted && current(ticket) && localTake === take;
  }
  function release(session) {
    if (session.released) return;
    session.released = true; session.stream.getTracks().forEach((track) => track.stop());
  }
  function stopRecording(cancelled = false) {
    if (!recording) return;
    recording.cancelled ||= cancelled;
    if (cancelled) { get("record").textContent = "Record"; get("record").setAttribute("aria-pressed", "false"); }
    release(recording);
    if (recording.stopping) return;
    recording.stopping = true;
    try { recording.recorder.stop(); } catch { finish(recording); }
  }
  function finish(session) {
    if (session.done) return;
    session.done = true; release(session);
    if (timer !== null) clearTimer(timer); timer = null;
    if (clock !== null) clearInterval(clock); clock = null;
    if (recording === session) recording = null;
    if (!current(session.ticket) || session.cancelled) return;
    get("record").textContent = "Record"; get("record").setAttribute("aria-pressed", "false");
    const blob = new Blob(session.chunks, { type: session.recorder.mimeType || "audio/webm" });
    if (session.failed || !blob.size) { say("No recording was saved. Check the microphone and record again."); return; }
    dropLocal(); localTake = { blob, url: objectUrl(blob), binding: session.binding };
    get("audio").src = localTake.url; get("save-take").disabled = false;
    say(`Recording stopped for ${session.binding.position}. Listen privately, then Save recording; approval requires contextual review.`);
  }
  function binding() {
    const position = get("position").value;
    const result = { position, placement: get("placement").value, allow_outro: get("allow-outro").checked };
    if (rerecord) result.take_id = rerecord.id;
    if (position === "opening" && edition.occurrences.length) result.after_id = edition.occurrences[0].id;
    if (position === "closing" && edition.occurrences.length) result.before_id = edition.occurrences.at(-1).id;
    if (position === "transition") {
      const index = Number(get("transition").value);
      if (!edition.occurrences[index + 1]) throw new Error("Choose a transition between two programme items.");
      result.before_id = edition.occurrences[index].id; result.after_id = edition.occurrences[index + 1].id;
    }
    if (position === "segment") result.index = Number(get("insert").value);
    return result;
  }
  function positions() {
    get("transition-row").hidden = get("position").value !== "transition";
    get("insert-row").hidden = get("position").value !== "segment";
  }
  function render(value) {
    const sameEdition = edition?.id === value.id;
    const hadResumeChoice = sameEdition && get("resume-item").options.length > 0;
    const resumeChoice = hadResumeChoice ? get("resume-item").value : value.run?.resume_occurrence;
    edition = value;
    root.querySelectorAll("[aria-invalid]").forEach((control) => control.removeAttribute("aria-invalid"));
    for (const [id, receipt] of receipts) {
      const take = edition.takes.find((item) => item.id === id);
      if (!take || take.dependency_hash !== receipt.dependency_hash || take.asset_id !== receipt.asset_id) receipts.delete(id);
    }
    get("editor").hidden = false; get("name").value = edition.name;
    const estimate = edition.estimated_seconds ?? edition.estimate_seconds;
    get("summary").textContent = `${edition.name}. Revision ${edition.revision}. `
      + (estimate == null ? "Estimated duration unavailable." : `Estimated duration ${fmtTime(estimate)}${edition.estimate_known === false ? "; joins may shorten it" : ""}.`)
      + " Hard scheduled events retain priority; unusable speech is skipped and reported.";
    if (edition.likely_overrun) get("summary").textContent += ` Likely overrun: estimated duration exceeds the reusable show's ${fmtTime(edition.target_seconds)} target.`;
    const run = edition.run || edition.on_air || edition.status;
    get("run-status").textContent = typeof run === "object" && run
      ? run.message || (run.interrupted ? "Interrupted. Choose an item and explicitly resume." : run.name || "Edition playback state is available.")
      : "Recording and review work without a running station. Playback uses server audio or streaming.";
    if (edition.committed_audio_notice) get("run-status").textContent += ` ${edition.committed_audio_notice}`;
    get("omissions").replaceChildren(...[...(edition.omissions || []), ...(edition.warnings || [])].map((item) => {
      const li = doc.createElement("li"); li.textContent = typeof item === "string" ? item : item.reason || item.label;
      return li;
    }));
    get("occurrences").replaceChildren(...edition.occurrences.map((item, index) => {
      const li = doc.createElement("li"), text = doc.createElement("span");
      text.textContent = `${index + 1}. ${label(item, index)}. `;
      const move = (step) => void mutate({ occurrences: edition.occurrences.map((entry) => ({ ...entry })) }, (body) => {
        [body.occurrences[index], body.occurrences[index + step]] = [body.occurrences[index + step], body.occurrences[index]];
      }, `Moved ${label(item, index)}.`, `occurrence-${item.id}-${step > 0 ? "down" : "up"}`);
      const up = button("up", "Move up", () => { if (index > 0) move(-1); else say("This is already the first item."); });
      const down = button("down", "Move down", () => { if (index < edition.occurrences.length - 1) move(1); else say("This is already the last item."); });
      up.setAttribute("aria-disabled", String(index === 0)); down.setAttribute("aria-disabled", String(index === edition.occurrences.length - 1));
      up.id = `occurrence-${item.id}-up`; down.id = `occurrence-${item.id}-down`;
      const remove = button("remove", "Remove", () => void mutate({
        occurrences: edition.occurrences.filter((entry) => entry.id !== item.id),
      }, null, `Removed ${label(item, index)}.`, "heading"));
      for (const control of [up, down, remove]) control.setAttribute("aria-label", `${control.textContent} ${label(item, index)}`);
      li.append(text, up, down, remove); return li;
    }));
    const transition = get("transition").value, insert = get("insert").value;
    get("transition").replaceChildren(...edition.occurrences.slice(0, -1).map((item, index) =>
      option(String(index), `Item ${index + 1}, ${label(item, index)} into item ${index + 2}, ${label(edition.occurrences[index + 1], index + 1)}`)));
    get("insert").replaceChildren(...edition.occurrences.map((item, index) => option(String(index), `Before item ${index + 1}, ${label(item, index)}`)),
      option(String(edition.occurrences.length), edition.occurrences.length ? `At the end (after item ${edition.occurrences.length})` : "At the end"));
    if ([...get("transition").options].some((item) => item.value === transition)) get("transition").value = transition;
    else get("transition").selectedIndex = get("transition").options.length ? 0 : -1;
    if ([...get("insert").options].some((item) => item.value === insert)) get("insert").value = insert;
    else get("insert").selectedIndex = get("insert").options.length - 1;
    get("resume-item").replaceChildren(...edition.occurrences.map((item, index) => option(item.id, `${index + 1}. ${label(item, index)}`)));
    if (resumeChoice && edition.occurrences.some((item) => item.id === resumeChoice)) get("resume-item").value = resumeChoice;
    else get("resume-item").selectedIndex = hadResumeChoice || run?.interrupted || resumeChoice ? -1 : edition.occurrences.length ? 0 : -1;
    renderTakes(); positions(); onChange();
  }
  function renderTakes() {
    get("takes").replaceChildren(...edition.takes.map((take) => {
      const li = doc.createElement("li"), text = doc.createElement("p");
      const itemName = (id) => {
        const index = edition.occurrences.findIndex((item) => item.id === id);
        return index < 0 ? "missing item" : `item ${index + 1}, ${label(edition.occurrences[index], index)}`;
      };
      const placement = take.position === "transition"
        ? `transition from ${itemName(take.before_id)} to ${itemName(take.after_id)}`
        : take.position === "opening" ? `opening before ${itemName(take.after_id)}`
          : take.position === "closing" ? `closing after ${itemName(take.before_id)}`
            : `standalone segment, ${itemName(take.occurrence_id)}`;
      const description = `${take.label || "Speech"}; ${placement}`;
      li.dataset.takeId = take.id;
      text.textContent = `${description}. ${take.status === "needs_review" ? "Needs review" : take.status === "unattached" ? "Unattached" : take.status}.`;
      const listen = button("listen", "Listen to take", () => {
        stopAudio(); get("audio").src = stationApi(endpoint(`/assets/${encodeURIComponent(take.asset_id)}`));
      });
      const record = button("rerecord", "Re-record", async () => {
        if (!await confirmRecordingReplacement()) return;
        stopRecording(true); dropLocal(); rerecord = take; get("position").value = take.position;
        get("placement").value = take.placement || "auto"; get("allow-outro").checked = Boolean(take.allow_outro);
        const index = edition.occurrences.findIndex((item) => item.id === take.before_id);
        if (index >= 0) get("transition").value = String(index);
        positions(); get("record").focus(); say(`Replacing ${description}. Record or import the replacement.`);
      });
      const review = button("review", "Review pending speech", () => reviewTake(take));
      const approve = button("approve", "Approve reviewed take", () => void withDisabled(approve, () => approveTake(take)));
      const remove = button("remove", "Remove take", async () => {
        const ticket = context();
        if (!await confirm({ title: "Remove this take?", message: `Remove ${description} from the edition? Its saved source audio is retained.`, confirmLabel: "Remove take" }) || !current(ticket)) return;
        try {
          const value = await request(endpoint(`/takes/${encodeURIComponent(take.id)}?expected_revision=${edition.revision}`), { method: "DELETE" });
          if (current(ticket)) { render(value); get("heading").focus(); say("Take removed; saved source audio retained."); }
        } catch (error) { fail(error, ticket); }
      });
      listen.disabled = ["missing", "invalid"].includes(take.status);
      review.disabled = ["missing", "invalid", "unattached"].includes(take.status);
      approve.disabled = !receipts.has(take.id) || review.disabled;
      for (const control of [listen, record, review, approve, remove]) control.setAttribute("aria-label", `${control.textContent} ${description}`);
      li.append(text, listen, record, review, approve, remove);
      if (take.status === "unattached" && take.position !== "segment") {
        const select = doc.createElement("select"), selectLabel = doc.createElement("label");
        select.id = `edition-reattach-${take.id}`; selectLabel.htmlFor = select.id;
        selectLabel.textContent = `Position for ${description}`;
        const rows = edition.occurrences;
        const pairs = take.position === "transition"
          ? rows.slice(0, -1).map((item, index) => [item.id, rows[index + 1].id])
          : rows.length ? [take.position === "opening" ? [null, rows[0].id] : [rows.at(-1).id, null]] : [];
        select.append(...pairs.map(([before, after], index) => option(String(index),
          before && after ? `${itemName(before)} into ${itemName(after)}` : before ? `After ${itemName(before)}` : `Before ${itemName(after)}`)));
        const reattach = button("reattach", "Reattach", () => void withDisabled(reattach, async () => {
          const ticket = context(), pair = pairs[Number(select.value)];
          if (!pair) return;
          try {
            const value = await send(endpoint(`/takes/${encodeURIComponent(take.id)}/reattach`), "POST", {
              expected_revision: edition.revision, before_id: pair[0], after_id: pair[1],
            });
            if (current(ticket)) {
              render(value); get("takes").querySelector(`[data-take-id="${take.id}"] button[data-action='review']`)?.focus();
              say("Take reattached. Review and approve its new position before broadcast.");
            }
          } catch (error) { fail(error, ticket, select); }
        }));
        reattach.setAttribute("aria-label", `Reattach ${description}`); reattach.disabled = !pairs.length;
        li.append(selectLabel, select, reattach);
      }
      return li;
    }));
  }
  function reviewTake(take = null) {
    if (take) requested.set(take.id, { revision: edition.revision, dependency_hash: take.dependency_hash, asset_id: take.asset_id });
    doc.dispatchEvent(new CustomEvent("autodj:review-edition", { detail: {
      edition_id: edition.id, take_id: take?.id || null, revision: edition.revision,
      include_pending: Boolean(take), opener: doc.activeElement,
    } }));
  }
  function reviewReady(event) {
    const detail = event.detail;
    if (!edition || detail?.edition_id !== edition.id || !detail.take_id) return;
    const pending = requested.get(detail.take_id), take = edition.takes.find((item) => item.id === detail.take_id);
    if (!pending || !take || pending.revision !== detail.revision || pending.dependency_hash !== take.dependency_hash || pending.asset_id !== take.asset_id) return;
    receipts.set(take.id, { ...pending, review_id: detail.review_id });
    const approve = get("takes").querySelector(`[data-take-id="${take.id}"] button[data-action='approve']`);
    if (approve) approve.disabled = false;
    say("Contextual review is available. Listen to the full take and joins, then explicitly approve it here.");
  }
  async function approveTake(take) {
    const receipt = receipts.get(take.id); if (!receipt) return;
    const ticket = context();
    try {
      const value = await send(endpoint(`/takes/${encodeURIComponent(take.id)}/approve`), "POST", { review_id: receipt.review_id });
      if (current(ticket)) {
        receipts.delete(take.id); render(value);
        get("takes").querySelector(`[data-take-id="${take.id}"] button[data-action='listen']`)?.focus();
        say("Take approved for its reviewed placement.");
      }
    } catch (error) {
      if (current(ticket)) { receipts.delete(take.id); renderTakes(); }
      fail(error, ticket);
    }
  }
  async function mutate(fields, change = null, message = "Edition saved.", focus = null) {
    if (!edition || busy) return;
    const ticket = context(), body = { expected_revision: edition.revision, ...fields };
    if (change) change(body);
    if (body.occurrences) body.occurrences = body.occurrences.map(({ id, kind, path = "", asset_id = null, take_id = null }) => ({ id, kind, path, asset_id, take_id }));
    busy = true;
    try {
      const value = await send(endpoint(), "PUT", body);
      if (current(ticket)) { render(value); if (focus) (doc.getElementById(focus) || get(focus))?.focus(); say(message); }
    } catch (error) { fail(error, ticket, error.status === 409 ? get("load") : null); }
    finally { if (current(ticket)) busy = false; }
  }
  async function refresh() {
    if (!show) return;
    const ticket = context(), wanted = get("list").value, number = ++refreshNumber;
    try {
      const value = await request(`/api/editions?show_id=${encodeURIComponent(show.id)}`);
      if (!current(ticket) || number !== refreshNumber) return;
      get("list").replaceChildren(...value.editions.map((item) => option(item.id, `${item.name}${item.previously_started ? " — Previously started" : ""}`)));
      if ([...get("list").options].some((item) => item.value === wanted)) get("list").value = wanted;
      else get("list").selectedIndex = value.editions.length ? 0 : -1;
    } catch (error) { if (number === refreshNumber) fail(error, ticket); }
  }
  async function load({ preserveRecording = false } = {}) {
    const id = preserveRecording ? edition?.id : get("list").value; if (!id) return;
    if (!preserveRecording && !await confirmRecordingReplacement()) return;
    const ticket = context(), number = ++loadNumber;
    try {
      const value = await request(`/api/editions/${encodeURIComponent(id)}`);
      if (!current(ticket) || number !== loadNumber) return;
      if (edition?.id !== value.id) { generation += 1; busy = false; receipts.clear(); requested.clear(); }
      if (!preserveRecording) {
        starting = false; stopRecording(true); stopAudio(); dropLocal(); rerecord = null;
      }
      render(value); get("heading").focus(); say(`Opened ${value.name}.`);
    } catch (error) { if (number === loadNumber) fail(error, ticket); }
  }
  async function startRecording() {
    if (recording) { stopRecording(); return; }
    if (!edition || starting) return;
    const ticket = context();
    let target;
    try { target = binding(); } catch (error) { fail(error, ticket, get("transition")); return; }
    if (globalThis.isSecureContext === false) { say(INSECURE_TEXT); return; }
    const devices = media(), Rec = Recorder();
    if (typeof devices?.getUserMedia !== "function" || typeof Rec !== "function") { say(NO_RECORDER_TEXT); return; }
    if (!await confirmRecordingReplacement() || starting || recording) return;
    starting = true;
    let stream;
    try { stream = await devices.getUserMedia({ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false } }); }
    catch (error) { if (current(ticket)) { starting = false; fail(new Error(microphoneProblem(error)), ticket, get("record")); } return; }
    if (!current(ticket) || panel?.hidden || doc.visibilityState === "hidden") {
      stream.getTracks().forEach((track) => track.stop()); if (current(ticket)) starting = false; return;
    }
    try {
      const recorder = new Rec(stream), session = { recorder, stream, chunks: [], ticket, binding: target };
      recorder.addEventListener("dataavailable", (event) => { if (event.data?.size) session.chunks.push(event.data); });
      recorder.addEventListener("stop", () => finish(session));
      recorder.addEventListener("error", () => { session.failed = true; finish(session); });
      stopAudio(); recording = session; recorder.start(1000); starting = false;
      get("record").textContent = "Stop recording"; get("record").setAttribute("aria-pressed", "true");
      timer = setTimer(() => stopRecording(), RECORDING_SECONDS * 1000);
      const began = Date.now();
      get("elapsed").textContent = "0:00 / 3:00";
      clock = setInterval(() => {
        get("elapsed").textContent = `${fmtTime(Math.min(RECORDING_SECONDS, (Date.now() - began) / 1000))} / 3:00`;
      }, 1000);
      say("Recording. Stop recording when finished; the microphone stops at 3:00.");
    } catch (error) {
      stream.getTracks().forEach((track) => track.stop()); recording = null; starting = false; fail(error, ticket, get("record"));
    }
  }
  async function upload(file, target, filename, control = null) {
    if (!edition || busy || recording || starting) return;
    const ticket = context(), query = new URLSearchParams({ expected_revision: edition.revision, filename });
    for (const [key, value] of Object.entries(target)) if (value != null) query.set(key, String(value));
    busy = true;
    try {
      const value = await request(endpoint(`/takes?${query}`), { method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
      if (current(ticket)) {
        const saved = value.takes.find((take) => !edition.takes.some((old) => old.id === take.id && old.asset_id === take.asset_id));
        render(value);
        const review = saved && get("takes").querySelector(`[data-take-id="${saved.id}"] button[data-action='review']`);
        (review && !review.disabled ? review : get("heading")).focus();
        stopAudio(); dropLocal(); rerecord = null; get("file").value = ""; say("Take saved. Contextual review and explicit approval are required before broadcast.");
      }
    } catch (error) { fail(error, ticket, error.status === 409 ? get("load") : control); }
    finally { if (current(ticket)) busy = false; }
  }
  async function start(resume = false, now = false) {
    if (!edition) return;
    const ticket = context();
    if (resume && !get("resume-item").value) {
      fail(new Error("Choose an occurrence to resume this edition."), ticket, get("resume-item")); return;
    }
    const confirmed = !edition.previously_started || await confirm({ title: "Air this edition again?", message: "This edition has started before. Its speech may refer to an earlier date.", confirmLabel: "Air again" });
    if (!current(ticket) || !confirmed) return;
    try {
      await send(endpoint("/start"), "POST", { now, confirm_reair: Boolean(edition.previously_started), from_occurrence: resume ? get("resume-item").value : null });
      if (current(ticket)) { say(resume ? "Explicit edition resumption requested." : "Edition start requested."); await load({ preserveRecording: true }); }
    } catch (error) { fail(error, ticket); }
  }
  async function schedule() {
    if (!edition) return;
    const ticket = context(), when = get("schedule-at").value;
    if (!when) { fail(new Error("Choose an air date and time in the station time zone."), ticket, get("schedule-at")); return; }
    const confirmed = !edition.previously_started || await confirm({ title: "Schedule this edition again?", message: "This edition has started before. Confirm its dated speech is appropriate for this airing.", confirmLabel: "Schedule again" });
    if (!current(ticket) || !confirmed) return;
    try {
      const [date, time] = when.split("T");
      const selected = `${edition.previously_started ? "edition-confirmed" : "edition"}:${edition.id}`;
      await send("/api/schedule", "POST", { name: `${edition.name.slice(0, 80)} ${date} ${time}`, action: "show", params: { show: selected }, repeat: "once", date, time, timing: get("schedule-timing").value, enabled: true, wait_seconds: 0 });
      if (current(ticket)) say("Edition scheduled. Review or change the event in Settings, Schedule.");
    } catch (error) { fail(error, ticket, get("schedule-at")); }
  }
  function clear() {
    generation += 1; loadNumber += 1; refreshNumber += 1; starting = false; stopRecording(true); stopAudio(); dropLocal();
    receipts.clear(); requested.clear(); edition = null; show = null; busy = false; rerecord = null;
    get("editor").hidden = true; get("list").replaceChildren(); get("occurrences").replaceChildren(); get("takes").replaceChildren();
    get("summary").textContent = ""; get("run-status").textContent = ""; get("status").textContent = ""; get("omissions").replaceChildren();
    get("name").value = ""; get("new-name").value = ""; get("file").value = "";
    for (const id of ["transition", "insert", "resume-item"]) get(id).replaceChildren();
    get("record").textContent = "Record"; get("record").setAttribute("aria-pressed", "false"); onChange();
    get("elapsed").textContent = "0:00 / 3:00";
  }
  get("refresh").addEventListener("click", () => void refresh());
  get("load").addEventListener("click", () => void load());
  get("create").addEventListener("click", () => void withDisabled(get("create"), async () => {
    if (!show || !await confirmRecordingReplacement()) return; const ticket = context();
    try {
      const value = await send("/api/editions", "POST", { show_id: show.id, name: get("new-name").value.trim() || undefined });
      if (current(ticket)) {
        generation += 1; starting = false; stopRecording(true); stopAudio(); dropLocal(); rerecord = null;
        receipts.clear(); requested.clear(); render(value); await refresh(); get("list").value = value.id;
        say("Prepared edition created. The reusable show stays independent.");
      }
    } catch (error) { fail(error, ticket, get("new-name")); }
  }));
  get("save-name").addEventListener("click", () => {
    if (!get("name").value.trim()) { fail(new Error("Enter an edition name."), context(), get("name")); return; }
    void mutate({ name: get("name").value.trim() });
  });
  get("delete").addEventListener("click", async () => {
    if (!edition) return; const ticket = context();
    if (!await confirm({ title: "Delete prepared edition?", message: `Delete ${edition.name} and its recordings?`, confirmLabel: "Delete edition" }) || !current(ticket)) return;
    try {
      await request(endpoint(`?expected_revision=${edition.revision}`), { method: "DELETE" });
      if (current(ticket)) {
        generation += 1; loadNumber += 1; busy = false; starting = false;
        stopRecording(true); stopAudio(); dropLocal(); receipts.clear(); requested.clear();
        get("list").focus(); edition = null; get("editor").hidden = true; onChange(); await refresh(); say("Edition deleted.");
      }
    } catch (error) { fail(error, ticket); }
  });
  get("record").addEventListener("click", () => void startRecording());
  get("position").addEventListener("change", () => { rerecord = null; positions(); });
  get("save-take").addEventListener("click", () => void withDisabled(get("save-take"), async () => {
    if (!localTake) return; const ticket = context(), take = localTake;
    try { const wav = await toWav(take.blob, 44100); if (current(ticket) && localTake === take) await upload(wav, take.binding, "recording.wav", get("save-take")); }
    catch (error) { fail(error, ticket); }
  }));
  get("import").addEventListener("click", () => void withDisabled(get("import"), async () => {
    const ticket = context(), file = get("file").files?.[0];
    if (!file) { fail(new Error("Choose an audio file to import."), ticket, get("file")); return; }
    if (!await confirmRecordingReplacement()) return;
    try { await upload(file, binding(), file.name, get("file")); } catch (error) { fail(error, ticket); }
  }));
  get("discard").addEventListener("click", async () => { if (!await confirmRecordingReplacement()) return; stopRecording(true); stopAudio(); dropLocal(); rerecord = null; say("Local recording discarded; saved takes are unchanged."); });
  get("review").addEventListener("click", () => reviewTake());
  get("start").addEventListener("click", () => void start());
  get("start-now").addEventListener("click", () => void start(false, true));
  get("resume").addEventListener("click", () => void start(true));
  get("schedule").addEventListener("click", () => void schedule());
  root.addEventListener("toggle", () => { if (root.open) void refresh(); });
  doc.addEventListener("autodj:edition-review-ready", reviewReady);
  const visible = () => { if (panel?.hidden || doc.visibilityState === "hidden") stopRecording(); };
  doc.addEventListener("visibilitychange", visible);
  const observer = new globalThis.MutationObserver(visible);
  if (panel) observer.observe(panel, { attributes: true, attributeFilter: ["hidden"] });
  return {
    active: () => Boolean(edition), refresh, confirmLeave: confirmRecordingReplacement,
    async openShow(value, { refresh: loadNow = true } = {}) {
      if (show?.id !== value.id) {
        if (!await confirmRecordingReplacement()) return false;
        clear();
      }
      show = value;
      const ticket = context();
      if (loadNow || root.open) await refresh();
      return current(ticket) && show?.id === value.id;
    },
    async addSong(song) {
      if (!edition) return;
      await mutate({ occurrences: [...edition.occurrences, { id: globalThis.crypto.randomUUID().replaceAll("-", ""), kind: "song", path: song.path, asset_id: null, take_id: null }] }, null, `Added ${song.title || "song"} to the edition.`);
    },
    clear,
    destroy() { clear(); observer.disconnect(); doc.removeEventListener("visibilitychange", visible); doc.removeEventListener("autodj:edition-review-ready", reviewReady); },
  };
}
