import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ANNOUNCE_DELAY_MS,
  THRESHOLD_ERROR,
  fitText,
  OFF_SUMMARY,
  installListenerRequests,
  parseThreshold,
  requestedText,
  savedText,
  songText,
  summaryText,
} from "../../src/autodj/static/modules/listener-requests.js";

// The markup index.html carries in Queue & Search.
function mount() {
  document.body.innerHTML = `
    <section id="panel-queue">
      <details class="card" id="requests-card" open hidden>
        <summary id="requests-heading"><h2>Listener requests</h2></summary>
        <p id="requests-summary">No requests waiting.</p>
        <div id="requests-status" role="status" aria-live="polite" aria-atomic="true"></div>
        <div id="requests-list" hidden></div>
        <h3 id="requests-recent-heading" hidden>Recent decisions</h3>
        <div id="requests-recent" hidden></div>
        <form id="requests-settings" novalidate>
          <input type="checkbox" id="requests-enabled" aria-describedby="requests-enabled-help">
          <input type="radio" name="requests-approval" id="requests-approval-manual" value="manual" checked>
          <input type="radio" name="requests-approval" id="requests-approval-auto" value="auto">
          <input type="checkbox" id="requests-bridges" aria-describedby="requests-bridges-help">
          <select id="requests-max-bridges"><option value="1">1</option><option value="2">2</option><option value="3">3</option></select>
          <input type="text" id="requests-threshold" aria-describedby="requests-threshold-help">
          <p id="requests-threshold-error" class="field-error" hidden></p>
          <input type="radio" name="requests-no-smooth" id="requests-no-smooth-best" value="best" checked>
          <input type="radio" name="requests-no-smooth" id="requests-no-smooth-wait" value="wait">
          <button type="submit" id="requests-settings-save">Save request settings</button>
        </form>
      </details>
    </section>
    <dialog id="confirm-dialog"><h1 id="confirm-title"></h1><p id="confirm-message"></p>
      <div><button id="confirm-cancel">Cancel</button><button id="confirm-accept"></button></div>
    </dialog>`;
  const dialog = byId("confirm-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value) => {
    dialog.returnValue = value;
    dialog.open = false;
    dialog.dispatchEvent(new Event("close"));
  };
}

const A = "a".repeat(32);
const B = "b".repeat(32);
const C = "c".repeat(32);
const WAITING = [
  { id: A, title: "Song", artist: "Artist", name: "Sam", requested_at: 100,
    fit: "Fits after Other: same key.", can_approve: true },
  { id: B, title: "Two", artist: "", name: "", requested_at: 200,
    fit: "No place in the window keeps the rules: same artist.", can_approve: false },
];
const STATE = {
  enabled: true, approval: "manual", today: 2, daily_limit: 50, pending: WAITING, recent: [],
  bridges_enabled: false, max_bridges: 3, bridge_cost_threshold: 2, no_smooth_spot: "best",
};

const byId = (id) => document.getElementById(id);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const requestRows = () => [...byId("requests-list").querySelectorAll("tbody tr[data-row-key]")];
const row = (id) => requestRows().find((item) => item.dataset.rowKey === id);
const menuAction = (id, action) => {
  const current = row(id);
  current.querySelector('[aria-haspopup="menu"]').click();
  return current.querySelector(`button[data-action="${action}"]`);
};
async function decideRequest(id, action) {
  const button = action === "approve" ? row(id).querySelector('button[data-action="approve"]') : menuAction(id, action);
  button.click();
  if (action === "reject") {
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
  }
  return button;
}

describe("listener request words", () => {
  it("names songs, requesters and the day's count in plain words", () => {
    expect(songText({ title: "Song", artist: "Artist" })).toBe("Song by Artist");
    expect(songText({ title: " ", artist: "" })).toBe("Unknown title");
    expect(requestedText({ name: "Sam", requested_at: 1 }, () => "2:05 PM"))
      .toBe("Requested by Sam at 2:05 PM.");
    expect(requestedText({ name: "", requested_at: 1 }, () => "2:05 PM"))
      .toBe("Requested at 2:05 PM.");
    expect(summaryText(STATE)).toBe("2 requests waiting. 2 of 50 taken today. Each request waits for you.");
    expect(summaryText({ ...STATE, pending: [WAITING[0]], approval: "auto" }))
      .toBe("1 request waiting. 2 of 50 taken today. Requests that keep the rules join the queue at once.");
    expect(summaryText({ ...STATE, pending: [] })).toMatch(/^No requests waiting\./);
    expect(summaryText({ ...STATE, enabled: false })).toBe(OFF_SUMMARY);
  });

  it("says whether a save opened or closed the request page", () => {
    expect(savedText({ enabled: false }, { enabled: true, approval: "manual" }))
      .toBe("Listener requests are on. The request page is open. Each request waits for you.");
    expect(savedText({ enabled: false }, { enabled: true, approval: "auto" }))
      .toBe("Listener requests are on. The request page is open. Requests that keep the rules join the queue at once.");
    expect(savedText({ enabled: true }, { enabled: false }))
      .toBe("Listener requests are off. The request page now says requests are closed.");
    expect(savedText({ enabled: true }, { enabled: true })).toBe("Request settings saved.");
  });
});

describe("listener requests card", () => {
  let request;
  let timers;
  let delayed;
  let enabled;

  const install = () => installListenerRequests({
    doc: document,
    request,
    isEnabled: () => enabled,
    setTimer: (callback) => { timers.push(callback); return 1; },
    later: (callback, ms) => { delayed.push({ callback, ms }); return 1; },
    formatTime: () => "2:05 PM",
  });
  const runDelayed = () => {
    const due = delayed;
    delayed = [];
    for (const { callback } of due) callback();
  };

  beforeEach(() => {
    mount();
    timers = [];
    delayed = [];
    enabled = true;
    request = vi.fn(async () => STATE);
  });

  it.each(["refresh", "settings", "action"])(
    "stops requests and ignores a pending %s after sign-out, but refreshes a station switch",
    async (pending) => {
      const controls = install();
      await flush();
      let finish;
      request.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
      const button = row(A).querySelector('button[data-action="approve"]');
      if (pending === "refresh") void controls.refresh();
      else if (pending === "settings") {
        byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
      } else button.click();
      const calls = request.mock.calls.length;
      enabled = false;
      controls.clear();
      expect(request).toHaveBeenCalledTimes(calls);
      finish(STATE);
      await flush();
      runDelayed();
      expect(byId("requests-card").hidden).toBe(true);
      expect(requestRows()).toHaveLength(0);
      expect(byId("requests-status").textContent).toBe("");
      timers[0]();
      byId("requests-card").dispatchEvent(new Event("toggle"));
      byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
      await controls.refresh();
      expect(request).toHaveBeenCalledTimes(calls);
      enabled = true;
      controls.clear();
      await flush();
      expect(request).toHaveBeenCalledTimes(calls + 1);
      expect(requestRows()).toHaveLength(2);
    },
  );

  it("shows while requests are off, with the switch off and no lists", async () => {
    request = vi.fn(async () => ({
      ...STATE, enabled: false, approval: "auto", pending: WAITING, recent: [],
    }));
    install();
    await flush();
    expect(byId("requests-card").hidden).toBe(false);
    expect(byId("requests-summary").textContent).toBe(OFF_SUMMARY);
    expect(byId("requests-enabled").checked).toBe(false);
    expect(byId("requests-approval-auto").checked).toBe(true);
    expect(byId("requests-list").hidden).toBe(true);
    expect(requestRows()).toHaveLength(0);
  });

  it("stays hidden when the station does not answer", async () => {
    request = vi.fn(async () => { throw new Error("401"); });
    install();
    await flush();
    expect(byId("requests-card").hidden).toBe(true);
  });

  it("turns requests on with automatic approval and says the page is open", async () => {
    request = vi.fn(async () => ({ ...STATE, enabled: false, pending: [] }));
    install();
    await flush();
    byId("requests-enabled").checked = true;
    byId("requests-approval-auto").checked = true;
    byId("requests-settings").dispatchEvent(new Event("change"));
    let answer;
    request.mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
    const save = byId("requests-settings-save");
    byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
    // Busy while it saves: aria-busy, never aria-disabled (#331); a second press waits.
    expect(save.getAttribute("aria-busy")).toBe("true");
    expect(save.classList.contains("is-pending")).toBe(true);
    expect(save.hasAttribute("aria-disabled")).toBe(false);
    expect(save.disabled).toBe(false);
    const calls = request.mock.calls.length;
    byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(request.mock.calls.length).toBe(calls);
    const body = JSON.parse(request.mock.calls.at(-1)[1].body);
    expect((({ enabled, approval }) => ({ enabled, approval }))(body))
      .toEqual({ enabled: true, approval: "auto" });
    answer({ ...STATE, approval: "auto", pending: [] });
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe(
      "Listener requests are on. The request page is open. Requests that keep the rules join the queue at once.",
    ));
    expect(save.hasAttribute("aria-busy")).toBe(false);
    expect(save.classList.contains("is-pending")).toBe(false);
    expect(byId("requests-summary").textContent).toMatch(/^No requests waiting\./);
  });

  it("turns requests off: the lists go and the summary says the page is closed", async () => {
    install();
    await flush();
    expect(requestRows()).toHaveLength(2);
    byId("requests-enabled").checked = false;
    byId("requests-settings").dispatchEvent(new Event("change"));
    request.mockResolvedValueOnce({ ...STATE, enabled: false, pending: [], recent: [] });
    byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe(
      "Listener requests are off. The request page now says requests are closed.",
    ));
    expect(JSON.parse(request.mock.calls.at(-1)[1].body).enabled).toBe(false);
    expect(byId("requests-summary").textContent).toBe(OFF_SUMMARY);
    expect(byId("requests-list").hidden).toBe(true);
    expect(byId("requests-card").hidden).toBe(false);
  });

  it("says why a save failed and keeps the button usable", async () => {
    install();
    await flush();
    byId("requests-threshold").value = "3";
    byId("requests-threshold").dispatchEvent(new Event("input", { bubbles: true }));
    request.mockRejectedValueOnce(new Error("Request failed (500)"));
    byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
    await vi.waitFor(() => expect(byId("requests-status").textContent)
      .toBe("Could not save the request settings: Request failed (500)"));
    expect(byId("requests-settings-save").hasAttribute("aria-busy")).toBe(false);
    expect(byId("requests-threshold").value).toBe("3");
  });

  it("ignores a settings reply after the station context is cleared", async () => {
    let finish;
    request.mockImplementation((url, options) => options?.method === "POST"
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve(STATE));
    const ui = install();
    await flush();
    byId("requests-threshold").value = "2";
    byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
    await flush();
    byId("requests-threshold").value = "3";
    ui.clear();
    await flush();
    finish({ ...STATE, bridge_cost_threshold: 4 });
    await flush();
    expect(byId("requests-threshold").value).not.toBe("4");
  });

  it("prevents a second settings save while the first is pending", async () => {
    let finish;
    request.mockImplementation((url, options) => options?.method === "POST"
      ? new Promise((resolve) => { finish = resolve; })
      : Promise.resolve(STATE));
    install();
    await flush();
    const form = byId("requests-settings");
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    form.dispatchEvent(new Event("submit", { cancelable: true }));
    expect(request.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
    expect(byId("requests-settings-save").getAttribute("aria-busy")).toBe("true");
    finish(STATE);
    await flush();
    expect(byId("requests-settings-save").hasAttribute("aria-busy")).toBe(false);
  });

  it.each(["mouse", "Enter", " ", "type-ahead"])("blocks off-air queue actions through %s but permits Turn down", async (activation) => {
    const controls = install();
    await flush();
    const reason = "Start the station to change the queue.";
    controls.setQueueDisabledReason(reason);
    const approve = row(A).querySelector('button[data-action="approve"]');
    const next = row(A).querySelector('button[data-action="play-next"]');
    for (const button of [approve, next]) {
      expect(button.disabled).toBe(false);
      expect(button.getAttribute("aria-disabled")).toBe("true");
      expect(byId(button.getAttribute("aria-describedby")).textContent).toBe(reason);
    }
    const key = (button, value) => button.dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true }));
    if (activation === "mouse") approve.click();
    else if (activation !== "type-ahead") {
      approve.closest("td").focus(); key(approve.closest("td"), activation);
    }
    row(A).querySelector('[aria-haspopup="menu"]').click();
    if (activation === "mouse") next.click();
    else {
      if (activation === "type-ahead") key(document.activeElement, "p");
      else next.focus();
      key(document.activeElement, activation === "type-ahead" ? "Enter" : activation);
    }
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe(reason));
    expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    const reject = row(A).querySelector('button[data-action="reject"]');
    expect(reject.getAttribute("aria-disabled")).toBe("false");
    reject.click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await flush();
    expect(request.mock.calls.filter(([, options]) => options?.method === "POST").map(([url]) => url))
      .toEqual([`/api/requests/${A}/reject`]);
    controls.setQueueDisabledReason("");
    expect(approve.getAttribute("aria-disabled")).toBe("false");
    expect(next.getAttribute("aria-disabled")).toBe("false");
    expect(row(B).querySelector('button[data-action="approve"]').getAttribute("aria-disabled")).toBe("true");
  });

  // ADR-0021-13
  it("uses shared pending and recent request tables with focusable unavailable approval", async () => {
    install();
    await flush();
    const table = byId("requests-list").querySelector("table.collection-table");
    expect(table).not.toBeNull();
    expect([...table.querySelectorAll("th")].map((cell) => cell.textContent))
      .toEqual(["Song", "Requester", "Time", "Fit", "Actions", "Options"]);
    expect(table.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    const second = table.querySelector(`tr[data-row-key="${B}"]`);
    const approve = second.querySelector('button[data-action="approve"]');
    expect(approve.disabled).toBe(false);
    expect(approve.getAttribute("aria-disabled")).toBe("true");
    approve.click();
    await vi.waitFor(() => expect(byId("requests-status").textContent).toContain("No place in the window"));
    expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    expect(second.querySelector('[aria-haspopup="menu"]').getAttribute("aria-label")).toBe("Options for Two");
    expect(byId("requests-recent").querySelector(".no-results").textContent).toBe("No recent decisions.");
  });

  it("confirms Turn down and keeps the request when canceled", async () => {
    install();
    await flush();
    const option = byId("requests-list").querySelector('[aria-haspopup="menu"]');
    expect(option).not.toBeNull();
    option.click();
    byId("requests-list").querySelector('[role="menuitem"][data-action="reject"]').click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Turn down this request?");
    byId("confirm-cancel").click();
    await flush();
    expect(request.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    expect(document.activeElement).toBe(option.closest("td"));
  });

  it("lists waiting requests with named actions and unavailable approval where it cannot fit", async () => {
    install();
    await flush();
    expect(byId("requests-card").hidden).toBe(false);
    expect(byId("requests-summary").textContent).toBe(summaryText(STATE));
    const items = requestRows();
    expect(items).toHaveLength(2);
    expect([...items[0].cells].slice(0, 3).map((cell) => cell.textContent)).toEqual(["Song by Artist", "Sam", "2:05 PM"]);
    expect(items[0].textContent).toContain("Fits after Other: same key.");
    const names = [...items[0].querySelectorAll("button")].map((b) => b.getAttribute("aria-label"));
    expect(names).toEqual(["Approve Song by Artist", "Options for Song by Artist", "Play next", "Turn down"]);
    const second = [...items[1].querySelectorAll("button")].map((b) => b.dataset.action);
    expect(second).toEqual(["approve", undefined, "play-next", "reject"]);
    // Plain text: the lists never speak on their own.
    expect(byId("requests-list").closest("[aria-live]")).toBeNull();
    expect(byId("requests-recent-heading").hidden).toBe(false);
  });

  it("updates rows in place on a refresh, never replacing what has focus", async () => {
    install();
    await flush();
    const first = row(A);
    const reject = menuAction(A, "reject");
    reject.focus();
    const list = byId("requests-list");
    const replace = vi.spyOn(list, "replaceChildren");
    const third = { ...WAITING[1], id: C, title: "Three", requested_at: 300 };
    request.mockResolvedValue({
      ...STATE,
      today: 3,
      pending: [{ ...WAITING[0], fit: "Fits after Else." }, { ...WAITING[1], can_approve: true }, third],
    });
    timers[0]();
    await flush();
    expect(replace).not.toHaveBeenCalled();
    expect(row(A)).toBe(first);
    expect(first.querySelector('button[data-action="reject"]')).toBe(reject);
    expect(document.activeElement).toBe(reject);
    expect(first.textContent).toContain("Fits after Else.");
    // Approve appears where it now fits, before the other buttons.
    expect([...row(B).querySelectorAll("button")].map((b) => b.dataset.action))
      .toEqual(["approve", undefined, "play-next", "reject"]);
    expect(requestRows().map((item) => item.dataset.rowKey)).toEqual([A, B, C]);
    expect(byId("requests-status").textContent).toBe("");
  });

  it("removes a decided row and announces the shared focus fallback", async () => {
    install();
    await flush();
    row(A).querySelector('button[data-action="approve"]').focus();
    request.mockResolvedValue({ ...STATE, pending: [WAITING[1]] });
    timers[0]();
    await flush();
    expect(row(A)).toBeUndefined();
    expect(document.activeElement.closest("tr")).toBe(row(B));
    await vi.waitFor(() => expect(byId("requests-status").textContent).toContain("Focus moved to"));
  });

  it("refreshes only while the card is open", async () => {
    install();
    await flush();
    byId("requests-card").open = false;
    const calls = request.mock.calls.length;
    timers[0]();
    await flush();
    expect(request.mock.calls.length).toBe(calls);
  });

  it("moves focus to the next request first, then says the result", async () => {
    const third = { ...WAITING[0], id: C, title: "Three" };
    request = vi.fn(async (url, options) => (options?.method === "POST"
      ? { ...STATE, pending: [WAITING[1], third], recent: [{ ...WAITING[0], status: "queued", note: "Fits after Other." }] }
      : { ...STATE, pending: [WAITING[0], WAITING[1], third] }));
    install();
    await flush();
    row(A).querySelector('button[data-action="approve"]').click();
    await vi.waitFor(() => expect(row(A)).toBeUndefined());
    expect(request).toHaveBeenCalledWith(`/api/requests/${A}/approve`, expect.objectContaining({ method: "POST" }));
    // Two cannot be approved, so focus lands on Options for its other actions.
    expect(document.activeElement.closest("tr").dataset.rowKey).toBe(B);
    expect(document.activeElement.dataset.action).toBe("options");
    // Said only after the move, so the focus speech does not cut it off.
    expect(byId("requests-status").textContent).toBe("");
    const said = delayed.find(({ ms }) => ms === ANNOUNCE_DELAY_MS);
    expect(said).toBeTruthy();
    said.callback();
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe("Queued Song by Artist."));
    expect([...byId("requests-recent").querySelector("tbody tr").cells].map((cell) => cell.textContent)).toEqual(["Song by Artist", "Sam", "2:05 PM", "Queued", "Fits after Other."]);
    expect(byId("requests-recent-heading").hidden).toBe(false);
  });

  it.each(["approve", "play-next", "reject"])("does not restore a decided request when an older poll resolves after %s", async (action) => {
    const controls = install();
    await flush();
    let finishPoll;
    request.mockImplementation((_url, options) => options?.method === "POST"
      ? Promise.resolve({ ...STATE, pending: [WAITING[1]], recent: [{ ...WAITING[0], status: action === "reject" ? "rejected" : "queued" }] })
      : new Promise((resolve) => { finishPoll = resolve; }));
    const poll = controls.refresh();
    await decideRequest(A, action);
    await vi.waitFor(() => expect(row(A)).toBeUndefined());
    finishPoll(STATE);
    await poll;
    expect(row(A)).toBeUndefined();
    expect(byId("requests-recent").textContent).toContain("Song by Artist");
    expect(byId("requests-summary").textContent).toContain("1 request waiting.");
  });

  it("lands on the next request's Approve where it is offered", async () => {
    const third = { ...WAITING[0], id: C, title: "Three" };
    request = vi.fn(async (url, options) => (options?.method === "POST"
      ? { ...STATE, pending: [third] }
      : { ...STATE, pending: [WAITING[0], third] }));
    install();
    await flush();
    await decideRequest(A, "reject");
    await vi.waitFor(() => expect(row(A)).toBeUndefined());
    expect(document.activeElement.closest("tr").dataset.rowKey).toBe(C);
    expect(document.activeElement.dataset.action).toBe("approve");
  });

  it("puts focus on the card's summary when the last request is decided", async () => {
    request = vi.fn(async (url, options) => (options?.method === "POST"
      ? { ...STATE, pending: [], recent: [] }
      : { ...STATE, pending: [WAITING[0]] }));
    install();
    await flush();
    await decideRequest(A, "reject");
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("requests-heading")));
    expect(byId("requests-list").querySelector(".no-results")).not.toBeNull();
    delayed.find(({ ms }) => ms === ANNOUNCE_DELAY_MS).callback();
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe("Turned down Song by Artist."));
  });

  it("says why when the server refuses, and keeps the request", async () => {
    install();
    await flush();
    request.mockRejectedValue(new Error("Song cannot be queued within the next 30 minutes: same album."));
    const button = row(A).querySelector('button[data-action="approve"]');
    button.click();
    await vi.waitFor(() => expect(byId("requests-status").textContent)
      .toBe("Song cannot be queued within the next 30 minutes: same album."));
    expect(button.getAttribute("aria-disabled")).toBe("false");
    expect(requestRows()).toHaveLength(2);
  });

  it("never says unavailable on the pressed button while it sends (#331)", async () => {
    install();
    await flush();
    let release;
    request.mockImplementation((url, options) => (options?.method === "POST"
      ? new Promise((resolve) => { release = () => resolve({ ...STATE, pending: [WAITING[1]] }); })
      : Promise.resolve(STATE)));
    const approve = row(A).querySelector('button[data-action="approve"]');
    approve.focus();
    approve.click();
    expect(approve.getAttribute("aria-disabled")).toBe("false");
    expect(approve.classList.contains("is-pending")).toBe(true);
    // Busy, a state NVDA can report, in place of "unavailable".
    expect(approve.getAttribute("aria-busy")).toBe("true");
    expect(document.activeElement).toBe(approve.closest("td"));
    // A second press while the first sends sends nothing.
    approve.click();
    expect(request.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
    release();
    await vi.waitFor(() => expect(row(A)).toBeUndefined());
  });

  it.each(["approve", "play-next"])(
    "blocks listener %s retry if the station stops during a voice choice", async (action) => {
      const controls = install();
      await flush();
      const conflict = Object.assign(new Error("Voice track changed."), {
        status: 409, detail: { code: "voice_track_conflict", revision: "rev-1" },
      });
      request.mockImplementation((_url, options) => options?.method !== "POST"
        ? Promise.resolve(STATE) : options.body ? Promise.resolve(STATE) : Promise.reject(conflict));
      await decideRequest(A, action);
      await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
      controls.setQueueDisabledReason("Start the station to change the queue.");
      byId("confirm-accept").click();
      await vi.waitFor(() => expect(byId("requests-status").textContent).toBe("Start the station to change the queue."));
      expect(request.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
      expect(row(A)).toBeTruthy();
    },
  );

  it.each(["approve", "play-next"])(
    "retries listener %s with the selected voice action and revision", async (action) => {
    install();
    await flush();
    const conflict = Object.assign(new Error("Voice track changed."), {
      status: 409,
      detail: { code: "voice_track_conflict", revision: "rev-1" },
    });
    request.mockImplementation((url, options) => {
      if (options?.method !== "POST") return Promise.resolve(STATE);
      if (!options.body) return Promise.reject(conflict);
      return Promise.resolve(STATE);
    });
    await decideRequest(A, action);
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(request.mock.calls.filter(([, options]) => options?.method === "POST"))
      .toHaveLength(2));
    const posts = request.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(posts[0][0]).toBe(`/api/requests/${A}/${action}`);
    expect(posts[1][1].body).toBe(JSON.stringify({ voice_action: "keep", voice_revision: "rev-1" }));
    },
  );

  it("says a short line when the reply is slow, and only the result when it is quick (#384)", async () => {
    install();
    await flush();
    vi.useFakeTimers();
    try {
      const status = byId("requests-status");
      const said = [];
      new window.MutationObserver(() => { if (status.textContent) said.push(status.textContent); })
        .observe(status, { childList: true, characterData: true, subtree: true });
      let release;
      request.mockImplementation((url, options) => (options?.method === "POST"
        ? new Promise((resolve) => { release = () => resolve({ ...STATE, pending: [WAITING[1]] }); })
        : Promise.resolve(STATE)));
      row(A).querySelector('button[data-action="approve"]').click();
      await vi.advanceTimersByTimeAsync(250);
      expect(said).toEqual([]);
      await vi.advanceTimersByTimeAsync(100);
      expect(said).toEqual(["Queueing Song by Artist…"]);
      release();
      await vi.advanceTimersByTimeAsync(0);
      runDelayed();
      await vi.advanceTimersByTimeAsync(10);
      expect(said).toEqual(["Queueing Song by Artist…", "Queued Song by Artist."]);

      // A quick reply: only its result.
      said.length = 0;
      request.mockImplementation(async (url, options) => (options?.method === "POST"
        ? { ...STATE, pending: [] } : STATE));
      await decideRequest(B, "reject");
      await vi.advanceTimersByTimeAsync(0);
      runDelayed();
      await vi.advanceTimersByTimeAsync(1000);
      expect(said).toEqual(["Turned down Two."]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does nothing without its markup", () => {
    document.body.innerHTML = "";
    expect(installListenerRequests({ doc: document, request }).refresh).toBeTypeOf("function");
    expect(request).not.toHaveBeenCalled();
  });
});

describe("smooth transitions (#49)", () => {
  const SETTINGS = {
    ...STATE, bridges_enabled: true, max_bridges: 2, bridge_cost_threshold: 1.5, no_smooth_spot: "wait",
  };
  let request;
  let delayed;
  const install = () => installListenerRequests({
    doc: document,
    request,
    setTimer: () => 1,
    later: (callback, ms) => { delayed.push({ callback, ms }); return 1; },
    formatTime: () => "2:05 PM",
  });

  beforeEach(() => {
    mount();
    delayed = [];
    request = vi.fn(async () => SETTINGS);
  });

  it("says where a request goes, why, and through which bridge songs", () => {
    expect(fitText({ reason: "Fits after A. No smooth spot within the next 30 minutes.", fit: "x" }))
      .toBe("Fits after A. No smooth spot within the next 30 minutes.");
    expect(fitText({ fit: "Fits after A.", bridges: ["B", "C"] })).toBe("Fits after A. Bridge songs: B, C.");
    expect(fitText({ reason: "Bridge to R through B.", bridges: ["B"] })).toBe("Bridge to R through B.");
    expect(parseThreshold(" 1.5 ")).toBe(1.5);
    expect(parseThreshold("2,5")).toBe(2.5);
    expect(parseThreshold("0")).toBe(0);
    for (const bad of ["", "-1", "10.5", "abc", "1e1", "NaN"]) expect(parseThreshold(bad)).toBeNull();
  });

  it("shows the saved settings and a bridged request in the recent list", async () => {
    request = vi.fn(async () => ({
      ...SETTINGS,
      recent: [{ id: C, status: "queued", title: "Ballad", artist: "Singer",
        note: "Bridge to Ballad — Singer through Middle — Band.", bridges: ["Middle — Band"] }],
    }));
    install();
    await flush();
    expect(byId("requests-bridges").checked).toBe(true);
    expect(byId("requests-max-bridges").value).toBe("2");
    expect(byId("requests-threshold").value).toBe("1.5");
    expect(byId("requests-no-smooth-wait").checked).toBe(true);
    expect(byId("requests-recent").querySelector("tbody tr").cells[4].textContent)
      .toBe("Bridge to Ballad — Singer through Middle — Band.");
  });

  it("saves the settings and says so once", async () => {
    install();
    await flush();
    byId("requests-bridges").checked = false;
    byId("requests-threshold").value = "3";
    byId("requests-no-smooth-best").checked = true;
    byId("requests-settings").dispatchEvent(new Event("change"));
    request.mockResolvedValueOnce({ ...SETTINGS, bridges_enabled: false, bridge_cost_threshold: 3, no_smooth_spot: "best" });
    byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
    await flush();
    const [url, options] = request.mock.calls.at(-1);
    expect(url).toBe("/api/requests/settings");
    expect(JSON.parse(options.body)).toEqual({
      enabled: true, approval: "manual",
      bridges_enabled: false, max_bridges: 2, bridge_cost_threshold: 3, no_smooth_spot: "best",
    });
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe("Request settings saved."));
  });

  it("refuses a roughness limit out of range: marked, linked, shown and focused", async () => {
    install();
    await flush();
    const field = byId("requests-threshold");
    field.value = "12";
    const calls = request.mock.calls.length;
    byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
    await flush();
    expect(request.mock.calls.length).toBe(calls);
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(field.getAttribute("aria-describedby")).toBe("requests-threshold-error requests-threshold-help");
    expect(byId("requests-threshold-error").hidden).toBe(false);
    expect(byId("requests-threshold-error").textContent).toBe(THRESHOLD_ERROR);
    expect(document.activeElement).toBe(field);
    field.value = "2";
    byId("requests-settings").dispatchEvent(new Event("submit", { cancelable: true }));
    await flush();
    expect(field.hasAttribute("aria-invalid")).toBe(false);
    expect(field.getAttribute("aria-describedby")).toBe("requests-threshold-help");
    expect(byId("requests-threshold-error").hidden).toBe(true);
  });

  it("does not overwrite settings being changed when the list refreshes", async () => {
    install();
    await flush();
    byId("requests-threshold").value = "4";
    byId("requests-settings").dispatchEvent(new Event("input"));
    request.mockResolvedValueOnce({ ...SETTINGS, today: 5 });
    byId("requests-card").dispatchEvent(new Event("toggle"));
    await flush();
    expect(byId("requests-threshold").value).toBe("4");
  });
});
