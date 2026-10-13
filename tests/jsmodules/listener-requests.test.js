import { beforeEach, describe, expect, it, vi } from "vitest";

import {
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

const WAITING = [
  { id: "a".repeat(32), title: "Song", artist: "Artist", name: "Sam", requested_at: 100,
    fit: "Fits after Other: same key.", can_approve: true },
  { id: "b".repeat(32), title: "Two", artist: "", name: "", requested_at: 200,
    fit: "No place in the window keeps the rules: same artist.", can_approve: false },
];
const STATE = {
  enabled: true, approval: "manual", today: 2, daily_limit: 50, pending: WAITING, recent: [],
};

const byId = (id) => document.getElementById(id);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

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

  const install = () => installListenerRequests({
    doc: document,
    request,
    setTimer: (callback) => { timers.push(callback); return 1; },
    formatTime: () => "2:05 PM",
  });

  beforeEach(() => {
    mount();
    timers = [];
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

  it("refreshes silently, only while open, and keeps focus on the same button", async () => {
    install();
    await flush();
    const button = byId("requests-list").querySelector('[data-action="reject"]');
    button.focus();
    const list = byId("requests-list");
    const replace = vi.spyOn(list, "replaceChildren");
    timers[0]();
    await flush();
    expect(replace).not.toHaveBeenCalled();
    request.mockResolvedValue({ ...STATE, today: 3 });
    timers[0]();
    await flush();
    expect(replace).toHaveBeenCalledTimes(1);
    expect(document.activeElement.dataset.action).toBe("reject");
    expect(document.activeElement.dataset.id).toBe("a".repeat(32));
    byId("requests-card").open = false;
    const calls = request.mock.calls.length;
    timers[0]();
    await flush();
    expect(request.mock.calls.length).toBe(calls);
    expect(byId("requests-status").textContent).toBe("");
  });

  it("says the result once and moves focus to the next request", async () => {
    install();
    await flush();
    request.mockImplementation(async (url, options) => (options?.method === "POST"
      ? { ...STATE, pending: [WAITING[1]], recent: [{ ...WAITING[0], status: "queued", note: "Fits after Other." }] }
      : STATE));
    byId("requests-list").querySelector('[data-action="approve"]').click();
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe("Queued Song by Artist."));
    expect(request).toHaveBeenCalledWith(`/api/requests/${"a".repeat(32)}/approve`, expect.objectContaining({ method: "POST" }));
    expect(document.activeElement.dataset.id).toBe("b".repeat(32));
    expect(byId("requests-recent").textContent).toBe("Queued: Song by Artist. Fits after Other.");
    expect(byId("requests-recent-heading").hidden).toBe(false);
  });

  it("puts focus on the heading when the last request is decided", async () => {
    request = vi.fn(async (url, options) => (options?.method === "POST"
      ? { ...STATE, pending: [], recent: [] }
      : { ...STATE, pending: [WAITING[0]] }));
    install();
    await flush();
    byId("requests-list").querySelector('[data-action="reject"]').click();
    await vi.waitFor(() => expect(byId("requests-status").textContent).toBe("Turned down Song by Artist."));
    expect(document.activeElement).toBe(byId("requests-heading"));
    expect(byId("requests-list").hidden).toBe(true);
  });

  it("says why when the server refuses, and keeps the request", async () => {
    install();
    await flush();
    request.mockRejectedValue(new Error("Song cannot be queued within the next 30 minutes: same album."));
    const button = byId("requests-list").querySelector('[data-action="approve"]');
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
