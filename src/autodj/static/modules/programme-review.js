import {
  captureAuthenticatedRequestEpoch, isAuthenticatedRequestCurrent, isStaleStationReply,
  requestJson, withDisabled,
} from "./api-client.js";
import { announceStatus } from "./live-region.js";
import { confirmAction } from "./confirm-dialog.js";
import { stationApi, stationEpoch } from "./stations.js";

function time(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "Unknown";
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function createdDate(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(date)
    : "Unknown date";
}

export function installProgrammeReview({
  doc = document, request = requestJson, announce = null,
  setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id),
} = {}) {
  const get = (id) => doc.getElementById(`review-${id}`);
  if (!get("card")) return { clear() {}, refresh: async () => {} };
  const card = get("card");
  const audio = get("audio"), snapshots = get("snapshots"), form = get("form");
  let generation = 0, timer = null, job = null, result = null, selected = 0, submitting = false;
  // Cancel was accepted for the running job; its renderer stops at its next check.
  let cancelling = false;
  let refreshNumber = 0, loadNumber = 0;
  let reviewsLoaded = false;
  let editionRequest = null, editionOpener = null;
  const returnButton = doc.createElement("button");
  returnButton.type = "button"; returnButton.textContent = "Return to edition"; returnButton.hidden = true;
  get("result").append(returnButton);
  returnButton.addEventListener("click", () => {
    doc.getElementById("tab-library")?.click();
    if (editionOpener?.isConnected) editionOpener.focus();
  });
  const context = () => [generation, stationEpoch(), captureAuthenticatedRequestEpoch()];
  const current = ([mine, station, auth]) => mine === generation && station === stationEpoch()
    && isAuthenticatedRequestCurrent(auth);
  const say = (message) => {
    if (announce) { get("status").textContent = message; announce(message); }
    else announceStatus(get("status"), message);
  };
  const send = (path, method, body) => request(path, {
    method, headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  function fail(error, ticket, moveFocus = true) {
    if (!current(ticket) || isStaleStationReply(error)) return;
    const fields = { head_seconds: "head", tail_seconds: "tail", clip_threshold_seconds: "threshold",
      tone_seconds: "tone", show_id: "show", start: "start", selection_start: "selection-start",
      selection_end: "selection-end", minutes: "minutes" };
    const field = get(fields[error.field]);
    if (field) { field.setAttribute("aria-invalid", "true"); if (moveFocus) field.focus(); }
    say(error.message || "Could not complete the review request.");
  }
  function stopAudio() {
    audio.pause(); audio.removeAttribute("src"); audio.load();
  }
  // Cancel keeps focus while a cancellation is pending, so it is marked
  // aria-disabled rather than disabled, which would drop focus to the page.
  function markCancelling(value) {
    const cancel = get("cancel");
    cancelling = value;
    if (value) cancel.setAttribute("aria-disabled", "true");
    else cancel.removeAttribute("aria-disabled");
    cancel.classList.toggle("is-pending", value);
  }
  function busy(value) {
    const generate = get("generate"), cancel = get("cancel");
    if (value) {
      cancel.hidden = false;
      if (doc.activeElement === generate) cancel.focus();
    }
    generate.classList.toggle("is-pending", value);
    generate.disabled = value;
    if (!value) markCancelling(false);
    if (!value && doc.activeElement === cancel) generate.focus();
    cancel.hidden = !value;
  }
  function showResult(value) {
    stopAudio(); result = value; selected = 0;
    get("result").hidden = false;
    get("summary").textContent = [
      `Created ${createdDate(value.created_at)}.`, value.stale ? "Out of date." : "Snapshot of planned audio.",
      value.incomplete ? "Incomplete." : "Complete.", `Preview ${time(value.duration)}.`,
      `Effective programme range ${time(value.effective_range?.[0])} to ${time(value.effective_range?.[1])}.`,
      "Before broadcast mastering.",
      value.includes_pending_speech ? "Pending speech included for review; approval is explicit." : "",
    ].join(" ");
    get("omissions").replaceChildren(...(value.omissions || []).map((omission) => {
      const li = doc.createElement("li");
      li.textContent = typeof omission === "string" ? omission : omission.label || omission.reason;
      return li;
    }));
    const base = `/api/reviews/${encodeURIComponent(value.id)}/audio/`;
    audio.src = stationApi(`${base}mp3`);
    for (const format of ["mp3", "wav"]) get(format).href = stationApi(`${base}${format}`);
    get("items").replaceChildren(...(value.items || []).map((item, index) => {
      const li = doc.createElement("li"), button = doc.createElement("button");
      button.type = "button";
      button.textContent = `${item.label}. Programme ${time(item.programme_start)} to ${time(item.programme_end)}; preview ${time(item.preview_start)} to ${time(item.preview_end)}.`;
      button.addEventListener("click", () => jump(index)); li.append(button); return li;
    }));
    get("previous").disabled = !value.items?.length;
    get("next").disabled = !value.items?.length;
    if (value.source === "edition" && value.target_take_id && value.approval_review_complete) {
      doc.dispatchEvent(new CustomEvent("autodj:edition-review-ready", { detail: {
        edition_id: value.edition_id, take_id: value.target_take_id,
        review_id: value.id, revision: value.edition_revision,
      } }));
    }
  }
  function jump(index) {
    const items = result?.items || [];
    if (!items.length) return;
    selected = Math.max(0, Math.min(index, items.length - 1));
    audio.currentTime = items[selected].preview_start;
    say(`${items[selected].label}. Programme ${time(items[selected].programme_start)}; preview ${time(items[selected].preview_start)}.`);
  }
  async function load(id, focus = false) {
    const ticket = context(), number = ++loadNumber;
    try {
      const value = await request(`/api/reviews/${encodeURIComponent(id)}`);
      if (current(ticket) && number === loadNumber) {
        showResult(value); snapshots.value = id;
        if (focus) get("heading").focus();
        return true;
      }
    } catch (error) { if (number === loadNumber) fail(error, ticket); }
    return false;
  }
  async function follow(value, ticket) {
    if (!current(ticket)) return;
    job = value; busy(["pending", "queued", "running"].includes(value.status));
    if (["pending", "queued", "running"].includes(value.status)) {
      if (value.cancel_requested) markCancelling(true);
      timer = setTimer(async () => {
        timer = null;
        try {
          const answer = await request(`/api/reviews/jobs/${encodeURIComponent(value.id)}`);
          if (current(ticket) && job?.id === value.id) await follow(answer, ticket);
        } catch (error) { if (current(ticket)) { busy(false); job = null; fail(error, ticket); } }
      }, 1000);
      return;
    }
    job = null;
    if (value.status === "failed" || value.status === "error")
      fail({ message: value.error || "Review generation failed.", field: value.field }, ticket,
        get("card").contains(doc.activeElement) || doc.activeElement === doc.body);
    else if (value.status === "cancelled") say("Review cancelled. Previous results are retained.");
    else {
      await refresh();
      if (!current(ticket)) return;
      const id = value.result_id || value.result?.id || value.review_id;
      const loaded = id ? await load(id) : false;
      if (current(ticket) && loaded) say("Review ready. Listening and downloads use the same snapshot.");
    }
  }
  async function refresh({ ifOpen = false } = {}) {
    if (ifOpen && !get("card").open) return;
    const ticket = context(), number = ++refreshNumber;
    try {
      const [value, shows] = await Promise.all([request("/api/reviews"), request("/api/shows")]);
      if (!current(ticket) || number !== refreshNumber) return;
      const chosen = snapshots.value, show = get("show").value;
      snapshots.replaceChildren(...(value.results || []).map((item) => {
        const option = doc.createElement("option"); option.value = item.id;
        option.textContent = `${createdDate(item.created_at)}${item.stale ? " — Out of date" : ""}${item.incomplete ? " — Incomplete" : ""}`;
        return option;
      }));
      if ([...snapshots.options].some((o) => o.value === chosen)) snapshots.value = chosen;
      else snapshots.selectedIndex = !reviewsLoaded && snapshots.options.length ? 0 : -1;
      reviewsLoaded = true;
      get("show").replaceChildren(...(shows.shows || []).map((item) => {
        const option = doc.createElement("option"); option.value = item.id; option.textContent = item.name;
        return option;
      }));
      if ([...get("show").options].some((o) => o.value === show)) get("show").value = show;
      else get("show").selectedIndex = get("show").options.length ? 0 : -1;
      if (result) {
        const updated = value.results?.find((item) => item.id === result.id);
        if (updated?.stale && !result.stale) {
          result.stale = true;
          get("summary").textContent += " Out of date.";
        }
      }
      if (value.job && !job && !timer && ["pending", "queued", "running"].includes(value.job.status))
        await follow(value.job, ticket);
    } catch (error) { if (number === refreshNumber) fail(error, ticket); }
  }
  function invalid(field, message) {
    field.setAttribute("aria-invalid", "true");
    field.closest("details")?.setAttribute("open", ""); field.focus(); say(message);
    return null;
  }
  function body() {
    form.querySelectorAll("[aria-invalid]").forEach((field) => field.removeAttribute("aria-invalid"));
    for (const input of form.querySelectorAll("input")) {
      if (!input.checkValidity()) return invalid(input, `${doc.querySelector(`label[for="${input.id}"]`).textContent} is outside its supported range.`);
    }
    const left = get("selection-start").value === "" ? null : Number(get("selection-start").value);
    const right = get("selection-end").value === "" ? null : Number(get("selection-end").value);
    if ((left === null) !== (right === null) || (left !== null && right <= left))
      return invalid(get("selection-end"), "Set both range boundaries with the end after the start, or leave both blank.");
    if (get("source").value === "show" && !get("show").value)
      return invalid(get("show"), "Choose a saved show.");
    return {
      source: get("source").value, show_id: get("source").value === "show" ? get("show").value : null,
      start: get("start").value ? new Date(get("start").value).toISOString() : null,
      minutes: Number(get("minutes").value), selection_start: left, selection_end: right,
      head_seconds: Number(get("head").value), tail_seconds: Number(get("tail").value),
      clip_threshold_seconds: Number(get("threshold").value), tone_seconds: Number(get("tone").value),
      keep_complete: [],
      ...(editionRequest || {}),
    };
  }
  async function generate() {
    if (submitting || job) return;
    const value = body(); if (!value) return;
    const ticket = context(); submitting = true;
    await withDisabled(get("generate"), async () => {
      try {
        const answer = await send("/api/reviews", "POST", value);
        if (current(ticket)) { say("Generating review."); await follow(answer, ticket); }
      } catch (error) { fail(error, ticket); }
      finally { if (current(ticket)) submitting = false; }
    });
    if (current(ticket)) busy(Boolean(job));
  }
  form.addEventListener("submit", (event) => { event.preventDefault(); void generate(); });
  doc.addEventListener("autodj:review-edition", (event) => {
    if (!card.isConnected) return;
    if (submitting || job) { say("Wait for the current review, or cancel it first."); return; }
    const detail = event.detail;
    editionRequest = { source: "edition", show_id: null, edition_id: detail.edition_id,
      target_take_id: detail.take_id || null, include_pending: detail.include_pending === true };
    editionOpener = detail.opener; returnButton.hidden = false;
    if (![...get("source").options].some((option) => option.value === "edition")) {
      const option = doc.createElement("option"); option.value = "edition";
      option.textContent = "Prepared edition"; get("source").append(option);
    }
    get("source").value = "edition"; get("show-row").hidden = true;
    get("selection-start").value = ""; get("selection-end").value = "";
    doc.getElementById("tab-library")?.click(); card.open = true;
    get("generate").focus(); void generate();
  });
  // The server waits briefly for the renderer to stop, so the reply is
  // usually the cancelled job.  A renderer between checks is still running
  // (cancel_requested): Cancel stays marked and polling ends the job.
  get("cancel").addEventListener("click", async () => {
    if (!job) return;
    if (cancelling) { say("Cancellation already requested. Waiting for the review to stop."); return; }
    const ticket = context(), id = job.id;
    const mine = () => current(ticket) && job?.id === id;
    markCancelling(true);
    say("Cancelling review generation…");
    try {
      const answer = await send(`/api/reviews/jobs/${encodeURIComponent(id)}/cancel`, "POST");
      if (!mine()) return;
      if (timer) clearTimer(timer); timer = null;
      await follow(answer, ticket);
    } catch (error) {
      if (!mine() || isStaleStationReply(error)) return;
      if (error.status === 404) {
        if (timer) clearTimer(timer); timer = null; job = null;
        busy(false);
        say("The review had already stopped. Previous results are retained.");
        void refresh();
        return;
      }
      // Polling continues; Cancel can be pressed again.
      markCancelling(false);
      say(`Could not cancel the review: ${error.message || "try again"}`);
    }
  });
  get("delete").addEventListener("click", async () => {
    const id = snapshots.value;
    if (!id) { say("Choose a saved review first."); return; }
    const ticket = context();
    let outcome = "";
    const confirmed = await confirmAction(doc, {
      title: `Delete review ${snapshots.selectedOptions[0].textContent}?`,
      message: "The review and its audio will be permanently deleted. Reviews are excluded from backups. Keep downloaded exports if you need a copy.",
      confirmLabel: "Delete review", pendingText: "Deleting review…",
      onConfirm: async () => {
        if (!current(ticket)) return null;
        try {
          await send(`/api/reviews/${encodeURIComponent(id)}`, "DELETE");
          if (!current(ticket)) return null;
          loadNumber += 1;
          if (result?.id === id) { stopAudio(); result = null; get("result").hidden = true; }
          await refresh(); outcome = "Review deleted.";
          return get("snapshots");
        } catch (error) {
          outcome = error.message || "Could not delete the review.";
          return get("delete");
        }
      },
    });
    if (confirmed && current(ticket) && outcome) say(outcome);
  });
  get("load").addEventListener("click", () => {
    if (snapshots.value) void load(snapshots.value, true);
    else say("Choose a saved review first.");
  });
  get("refresh").addEventListener("click", () => void refresh());
  get("previous").addEventListener("click", () => jump(selected - 1));
  get("next").addEventListener("click", () => jump(selected + 1));
  audio.addEventListener("timeupdate", () => {
    const items = result?.items || [];
    const index = items.findLastIndex((item) => item.preview_start <= audio.currentTime);
    if (index >= 0) selected = index;
  });
  get("source").addEventListener("change", () => {
    get("show-row").hidden = get("source").value !== "show";
    if (get("source").value !== "edition") editionRequest = null;
  });
  get("card").addEventListener("toggle", () => { if (get("card").open) void refresh(); });
  return {
    refresh,
    clear() {
      generation += 1; refreshNumber += 1; loadNumber += 1;
      reviewsLoaded = false;
      editionRequest = null; editionOpener = null; returnButton.hidden = true;
      get("source").querySelector('option[value="edition"]')?.remove();
      if (timer) clearTimer(timer); timer = null; job = null; result = null; submitting = false;
      stopAudio(); busy(false); get("result").hidden = true;
      snapshots.replaceChildren(); get("show").replaceChildren(); get("items").replaceChildren();
      get("summary").textContent = ""; get("omissions").replaceChildren();
      get("status").textContent = "";
      for (const format of ["mp3", "wav"]) get(format).removeAttribute("href");
    },
  };
}
