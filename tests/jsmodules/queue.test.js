import { describe, expect, it, vi } from "vitest";

import { applyQueueState, installQueueButtons, renderQueue } from
  "../../src/autodj/static/modules/queue.js";

describe("queue mutations", () => {
  it("does not collide queue keys containing delimiter characters", () => {
    document.body.innerHTML = '<span id="count"></span><ul id="queue"></ul>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.querySelector("#count"),
    };
    applyQueueState([
      { path: "a|b", display_name: "First shape" },
      { path: "c", display_name: "Tail" },
    ], els);
    applyQueueState([
      { path: "a", display_name: "Second shape" },
      { path: "b|c", display_name: "Other tail" },
    ], els);

    expect(els.queueList.textContent).toContain("Second shape");
    expect(els.queueList.textContent).not.toContain("First shape");
  });

  it("mutates the clicked duplicate row rather than the first matching path", async () => {
    document.body.innerHTML = '<p id="announce"></p><ul id="queue"></ul>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
    };
    renderQueue([
      { path: "dup.mp3", display_name: "First duplicate" },
      { path: "middle.mp3", display_name: "Middle" },
      { path: "dup.mp3", display_name: "Second duplicate" },
    ], els);
    installQueueButtons(els);
    let resolveRequest;
    const fetchImpl = vi.fn(() => new Promise((resolve) => {
      resolveRequest = resolve;
    }));
    vi.stubGlobal("fetch", fetchImpl);

    els.queueList.querySelectorAll(
      '[data-path="dup.mp3"][data-action="remove"]',
    )[1].click();

    expect(els.queueList.textContent).toContain("First duplicate");
    expect(els.queueList.textContent).not.toContain("Second duplicate");
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/queue/reorder");
    resolveRequest(new globalThis.Response('{"ok":true}', {
      headers: { "Content-Type": "application/json" },
    }));
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toContain("Removed"));
    vi.unstubAllGlobals();
  });

  it("does not roll back over a newer WebSocket queue render", async () => {
    document.body.innerHTML = '<p id="announce"></p><ul id="queue"></ul>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
    };
    renderQueue([
      { path: "old.mp3", display_name: "Old" },
      { path: "other.mp3", display_name: "Other" },
    ], els);
    installQueueButtons(els);
    let resolveRequest;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveRequest = resolve;
    })));
    els.queueList.querySelector('[data-path="old.mp3"][data-action="remove"]').click();
    applyQueueState([
      { path: "server.mp3", display_name: "Server state" },
    ], els);
    resolveRequest(new globalThis.Response('{"detail":"write failed"}', {
      status: 500,
      headers: { "Content-Type": "application/json" },
    }));
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toContain("write failed"));

    expect(els.queueList.textContent).toContain("Server state");
    expect(els.queueList.textContent).not.toContain("Old");
    vi.unstubAllGlobals();
  });

  it("locks the live queue transaction before optimistic rendering", async () => {
    document.body.innerHTML = '<p id="announce"></p><ul id="queue"></ul>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
    };
    renderQueue([
      { path: "one.mp3", display_name: "One" },
      { path: "two.mp3", display_name: "Two" },
      { path: "three.mp3", display_name: "Three" },
    ], els);
    installQueueButtons(els);
    const resolvers = [];
    const fetchImpl = vi.fn(() => new Promise((resolve) => resolvers.push(resolve)));
    vi.stubGlobal("fetch", fetchImpl);

    els.queueList.querySelector('[data-path="two.mp3"][data-action="down"]').click();
    els.queueList.querySelector('[data-path="one.mp3"][data-action="remove"]').click();
    expect(fetchImpl).toHaveBeenCalledOnce();

    for (const resolve of resolvers) {
      resolve(new globalThis.Response(JSON.stringify({ ok: true }), {
        headers: { "Content-Type": "application/json" },
      }));
    }
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toContain("Moved"));
    vi.unstubAllGlobals();
  });

  it("rolls back the exact snapshot and restores action focus after busy clears", async () => {
    document.body.innerHTML = '<p id="announce"></p><ul id="queue"></ul>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
    };
    const original = [
      { path: "one.mp3", display_name: "One" },
      { path: "two.mp3", display_name: "Two" },
    ];
    renderQueue(original, els);
    installQueueButtons(els);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new globalThis.Response(
      JSON.stringify({ detail: "Queue write failed" }),
      { status: 500, headers: { "Content-Type": "application/json" } },
    )));

    const clicked = els.queueList.querySelector(
      'li[data-path="one.mp3"] [data-action="remove"]',
    );
    clicked.focus();
    clicked.click();
    let busyWhenFocusReturned = null;
    els.queueList.addEventListener("focusin", () => {
      busyWhenFocusReturned = els.queueList.getAttribute("aria-busy");
    }, { once: true });
    await vi.waitFor(() => expect(els.queueAnnounce.textContent)
      .toContain("Queue write failed"));

    expect(Array.from(els.queueList.querySelectorAll("li[data-path]"))
      .map((li) => li.dataset.path)).toEqual(["one.mp3", "two.mp3"]);
    const restored = els.queueList.querySelector(
      'li[data-path="one.mp3"] [data-action="remove"]',
    );
    expect(restored.disabled).toBe(false);
    expect(document.activeElement).toBe(restored);
    expect(busyWhenFocusReturned).toBe("false");
    expect(els.queueList.getAttribute("aria-busy")).toBe("false");
    vi.unstubAllGlobals();
  });

  it("clears busy before focusing the queue list after final-row removal", async () => {
    document.body.innerHTML = '<p id="announce"></p><ol id="queue" tabindex="-1"></ol>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
    };
    renderQueue([{ path: "only.mp3", display_name: "Only" }], els);
    installQueueButtons(els);
    let resolveRequest;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveRequest = resolve;
    })));
    const focusSpy = vi.spyOn(els.queueList, "focus").mockImplementation(() => {
      expect(els.queueList.getAttribute("aria-busy")).toBe("false");
      globalThis.HTMLElement.prototype.focus.call(els.queueList);
    });

    els.queueList.querySelector('[data-action="remove"]').click();
    expect(els.queueList.getAttribute("aria-busy")).toBe("true");
    expect(focusSpy).not.toHaveBeenCalled();
    resolveRequest(new globalThis.Response('{"ok":true}', {
      headers: { "Content-Type": "application/json" },
    }));
    await vi.waitFor(() => expect(focusSpy).toHaveBeenCalledOnce());

    expect(document.activeElement).toBe(els.queueList);
    expect(els.queueList.getAttribute("aria-busy")).toBe("false");
    vi.unstubAllGlobals();
  });

  it("clears busy without moving focus after a newer authoritative render", async () => {
    document.body.innerHTML = '<p id="announce"></p><ol id="queue" tabindex="-1"></ol>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
    };
    renderQueue([
      { path: "old.mp3", display_name: "Old" },
      { path: "tail.mp3", display_name: "Tail" },
    ], els);
    installQueueButtons(els);
    let resolveRequest;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveRequest = resolve;
    })));
    const focusSpy = vi.spyOn(els.queueList, "focus");

    els.queueList.querySelector('[data-action="remove"]').click();
    applyQueueState([{ path: "server.mp3", display_name: "Server" }], els);
    resolveRequest(new globalThis.Response('{"ok":true}', {
      headers: { "Content-Type": "application/json" },
    }));
    await vi.waitFor(() => expect(els.queueList.getAttribute("aria-busy")).toBe("false"));

    expect(els.queueList.textContent).toContain("Server");
    expect(focusSpy).not.toHaveBeenCalled();
    expect(document.activeElement.closest?.("#queue")).toBeNull();
    vi.unstubAllGlobals();
  });

  it("clears busy without moving focus when authentication expires", async () => {
    document.body.innerHTML = '<p id="announce"></p><ol id="queue" tabindex="-1"></ol>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
    };
    renderQueue([{ path: "only.mp3", display_name: "Only" }], els);
    installQueueButtons(els);
    let resolveRequest;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => {
      resolveRequest = resolve;
    })));
    const { invalidateAuthenticatedRequestEpoch } = await import(
      "../../src/autodj/static/modules/api-client.js"
    );
    const focusSpy = vi.spyOn(els.queueList, "focus");

    els.queueList.querySelector('[data-action="remove"]').click();
    invalidateAuthenticatedRequestEpoch();
    resolveRequest(new globalThis.Response('{"ok":true}', {
      headers: { "Content-Type": "application/json" },
    }));
    await vi.waitFor(() => expect(els.queueList.getAttribute("aria-busy")).toBe("false"));

    expect(focusSpy).not.toHaveBeenCalled();
    expect(document.activeElement.closest?.("#queue")).toBeNull();
    vi.unstubAllGlobals();
  });
});
