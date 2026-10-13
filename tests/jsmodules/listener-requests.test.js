import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ANNOUNCE_DELAY_MS,
  installListenerRequests,
  requestedText,
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
        <ul id="requests-list" aria-label="Waiting requests" hidden></ul>
        <h3 id="requests-recent-heading" hidden>Recent decisions</h3>
        <ul id="requests-recent" hidden></ul>
      </details>
    </section>`;
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
};

const byId = (id) => document.getElementById(id);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const row = (id) => [...byId("requests-list").children].find((li) => li.dataset.id === id);

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
  });
});

describe("listener requests card", () => {
  let request;
  let timers;
  let delayed;

  const install = () => installListenerRequests({
    doc: document,
    request,
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
    request = vi.fn(async () => STATE);
  });

  it("stays hidden while requests are off", async () => {
    request = vi.fn(async () => ({ enabled: false, pending: [], recent: [] }));
    install();
    await flush();
    expect(byId("requests-card").hidden).toBe(true);
  });

  it("lists waiting requests with named buttons, Approve only where it fits", async () => {
    install();
    await flush();
    expect(byId("requests-card").hidden).toBe(false);
    expect(byId("requests-summary").textContent).toBe(summaryText(STATE));
    const items = byId("requests-list").children;
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("Song by Artist Requested by Sam at 2:05 PM.");
    expect(items[0].textContent).toContain("Fits after Other: same key.");
    const names = [...items[0].querySelectorAll("button")].map((b) => b.textContent.trim());
    expect(names).toEqual(["Approve Song by Artist", "Play next Song by Artist", "Turn down Song by Artist"]);
    const second = [...items[1].querySelectorAll("button")].map((b) => b.dataset.action);
    expect(second).toEqual(["play-next", "reject"]);
    // Plain text: the lists never speak on their own.
    expect(byId("requests-list").closest("[aria-live]")).toBeNull();
    expect(byId("requests-recent-heading").hidden).toBe(true);
  });

  it("updates rows in place on a refresh, never replacing what has focus", async () => {
    install();
    await flush();
    const first = row(A);
    const reject = first.querySelector('[data-action="reject"]');
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
    expect(first.querySelector('[data-action="reject"]')).toBe(reject);
    expect(document.activeElement).toBe(reject);
    expect(first.textContent).toContain("Fits after Else.");
    // Approve appears where it now fits, before the other buttons.
    expect([...row(B).querySelectorAll("button")].map((b) => b.dataset.action))
      .toEqual(["approve", "play-next", "reject"]);
    expect([...list.children].map((li) => li.dataset.id)).toEqual([A, B, C]);
    expect(byId("requests-status").textContent).toBe("");
  });

  it("keeps a row that holds focus until focus leaves it", async () => {
    install();
    await flush();
    const held = row(A).querySelector('[data-action="approve"]');
    held.focus();
    // Decided elsewhere (another browser): gone from the list.
    request.mockResolvedValue({ ...STATE, pending: [WAITING[1]] });
    timers[0]();
    await flush();
    expect(row(A)).toBeTruthy();
    expect(document.activeElement).toBe(held);
    row(B).querySelector("button").focus();
    // As a browser does when focus moves on (happy-dom may not).
    held.dispatchEvent(new Event("focusout", { bubbles: true }));
    runDelayed();
    expect(row(A)).toBeUndefined();
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
    row(A).querySelector('[data-action="approve"]').click();
    await vi.waitFor(() => expect(row(A)).toBeUndefined());
    expect(request).toHaveBeenCalledWith(`/api/requests/${A}/approve`, expect.objectContaining({ method: "POST" }));
    // Two has no Approve (nothing fits), so focus lands on its first button.
    expect(document.activeElement.dataset.id).toBe(B);
    expect(document.activeElement.dataset.action).toBe("play-next");
    // Said only after the move, so the focus speech does not cut it off.
    expect(byId("requests-status").textContent).toBe("");
    const said = delayed.find(({ ms }) => ms === ANNOUNCE_DELAY_MS);
    expect(said).toBeTruthy();
    said.callback();
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe("Queued Song by Artist."));
    expect(byId("requests-recent").textContent).toBe("Queued: Song by Artist. Fits after Other.");
    expect(byId("requests-recent-heading").hidden).toBe(false);
  });

  it("lands on the next request's Approve where it is offered", async () => {
    const third = { ...WAITING[0], id: C, title: "Three" };
    request = vi.fn(async (url, options) => (options?.method === "POST"
      ? { ...STATE, pending: [third] }
      : { ...STATE, pending: [WAITING[0], third] }));
    install();
    await flush();
    row(A).querySelector('[data-action="reject"]').click();
    await vi.waitFor(() => expect(row(A)).toBeUndefined());
    expect(document.activeElement.dataset.id).toBe(C);
    expect(document.activeElement.dataset.action).toBe("approve");
  });

  it("puts focus on the card's summary when the last request is decided", async () => {
    request = vi.fn(async (url, options) => (options?.method === "POST"
      ? { ...STATE, pending: [], recent: [] }
      : { ...STATE, pending: [WAITING[0]] }));
    install();
    await flush();
    row(A).querySelector('[data-action="reject"]').click();
    await vi.waitFor(() => expect(document.activeElement).toBe(byId("requests-heading")));
    expect(byId("requests-list").hidden).toBe(true);
    delayed.find(({ ms }) => ms === ANNOUNCE_DELAY_MS).callback();
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe("Turned down Song by Artist."));
  });

  it("says why when the server refuses, and keeps the request", async () => {
    install();
    await flush();
    request.mockRejectedValue(new Error("Song cannot be queued within the next 30 minutes: same album."));
    const button = row(A).querySelector('[data-action="approve"]');
    button.click();
    await vi.waitFor(() => expect(byId("requests-status").textContent)
      .toBe("Song cannot be queued within the next 30 minutes: same album."));
    expect(button.hasAttribute("aria-disabled")).toBe(false);
    expect(byId("requests-list").children).toHaveLength(2);
  });

  it("does nothing without its markup", () => {
    document.body.innerHTML = "";
    expect(installListenerRequests({ doc: document, request }).refresh).toBeTypeOf("function");
    expect(request).not.toHaveBeenCalled();
  });
});
