import { describe, expect, it, vi } from "vitest";

import { applyQueueState, installQueueButtons, installQueueClear, renderQueue } from
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

describe("repeated queue actions stay audible", () => {
  it("announces and shows a second Move up on the same track", async () => {
    document.body.innerHTML =
      '<p id="announce" role="status" aria-live="polite" aria-atomic="true"></p>'
      + '<ul id="queue"></ul>'
      + '<div id="status-toast" aria-hidden="true" hidden></div>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
    };
    renderQueue([
      { path: "a.mp3", display_name: "Alpha" },
      { path: "b.mp3", display_name: "Bravo" },
      { path: "c.mp3", display_name: "Charlie" },
    ], els);
    installQueueButtons(els);
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(
      new globalThis.Response("{}", { headers: { "Content-Type": "application/json" } }),
    )));

    const spoken = [];
    const shown = [];
    const region = document.querySelector("#announce");
    const toast = document.querySelector("#status-toast");
    new window.MutationObserver((r) => spoken.push(...r))
      .observe(region, { childList: true, characterData: true, subtree: true });
    new window.MutationObserver((r) => shown.push(...r))
      .observe(toast, { childList: true, characterData: true, subtree: true });

    const added = (records) => records.filter((r) => r.addedNodes.length > 0).length;
    const clickUpOn = async (name) => {
      // Count landings rather than text: the second announcement carries
      // the same sentence, so waiting on the text would pass instantly.
      const before = added(spoken);
      const row = [...els.queueList.querySelectorAll("li")]
        .find((li) => li.textContent.includes(name));
      row.querySelector('.queue-btn[data-action="up"]').click();
      await vi.waitFor(() => expect(added(spoken)).toBe(before + 1));
    };

    // Charlie moves 3 -> 2, then 2 -> 1, well inside the three-second
    // dwell, and each move says where the track landed.
    await clickUpOn("Charlie");
    expect(region.textContent).toBe("Moved Charlie to position 2 of 3.");
    await clickUpOn("Charlie");

    expect(added(spoken)).toBe(2);
    expect(added(shown)).toBe(2);
    expect(region.textContent).toBe("Moved Charlie to position 1 of 3.");
    expect(toast.textContent).toBe("Moved Charlie to position 1 of 3.");
    vi.unstubAllGlobals();
  });
});

describe("websocket queue renders keep keyboard focus", () => {
  function setup(queue) {
    document.body.innerHTML = '<ol id="queue" tabindex="-1"></ol>';
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
    };
    applyQueueState(queue, els);
    return els;
  }
  const track = (path) => ({ path, display_name: path });

  it("refocuses the same action on the same track after a rebuild", () => {
    const els = setup([track("a.mp3"), track("b.mp3"), track("c.mp3")]);
    els.queueList.querySelector('li[data-path="c.mp3"] [data-action="remove"]').focus();

    // The head of the queue started playing, so every row moved up one.
    applyQueueState([track("b.mp3"), track("c.mp3")], els);

    const active = document.activeElement;
    expect(active.dataset.action).toBe("remove");
    expect(active.closest("li").dataset.path).toBe("c.mp3");
  });

  it("follows the same duplicate, and skips an action that became disabled", () => {
    const els = setup([track("x.mp3"), track("dup.mp3"), track("dup.mp3")]);
    els.queueList.querySelectorAll('li[data-path="dup.mp3"] [data-action="up"]')[1].focus();

    applyQueueState([track("dup.mp3"), track("dup.mp3")], els);

    const active = document.activeElement;
    const rows = Array.from(els.queueList.querySelectorAll("li"));
    expect(rows.indexOf(active.closest("li"))).toBe(1);
    expect(active.dataset.action).toBe("up");

    rows[0].querySelector('[data-action="down"]').focus();
    applyQueueState([track("dup.mp3")], els);
    // Only row: Up and Down are disabled, so Remove takes focus.
    expect(document.activeElement.dataset.action).toBe("remove");
  });

  it("falls back to the same position, then to the list itself", () => {
    const els = setup([track("a.mp3"), track("b.mp3")]);
    els.queueList.querySelector('li[data-path="b.mp3"] [data-action="remove"]').focus();

    applyQueueState([track("z.mp3")], els);
    expect(document.activeElement.closest("li").dataset.path).toBe("z.mp3");

    applyQueueState([], els);
    expect(document.activeElement).toBe(els.queueList);
  });

  it("does not take focus when it was elsewhere on the page", () => {
    const els = setup([track("a.mp3")]);
    const outside = document.createElement("button");
    document.body.appendChild(outside);
    outside.focus();

    applyQueueState([track("b.mp3")], els);
    expect(document.activeElement).toBe(outside);
  });
});

describe("queue Top and Clear queue", () => {
  const ok = () => Promise.resolve(new globalThis.Response('{"ok":true}', {
    headers: { "Content-Type": "application/json" },
  }));

  function setup() {
    document.body.innerHTML = `
      <p id="announce"></p><ul id="queue"></ul><button id="clear">Clear queue</button>
      <dialog id="confirm-dialog"><h2 id="confirm-title"></h2><p id="confirm-message"></p>
        <button id="confirm-cancel"></button><button id="confirm-accept"></button></dialog>`;
    const dialog = document.querySelector("#confirm-dialog");
    dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
    const els = {
      queueList: document.querySelector("#queue"),
      queueCount: document.createElement("span"),
      queueAnnounce: document.querySelector("#announce"),
      queueClear: document.querySelector("#clear"),
    };
    applyQueueState([
      { path: "a.mp3", display_name: "Alpha" },
      { path: "b.mp3", display_name: "Bravo" },
      { path: "c.mp3", display_name: "Charlie" },
    ], els);
    installQueueButtons(els);
    installQueueClear(els);
    const answer = (value) => {
      dialog.returnValue = value;
      dialog.removeAttribute("open");
      dialog.dispatchEvent(new Event("close"));
    };
    return { els, dialog, answer };
  }

  it("moves a track to the top and says where it landed", async () => {
    const fetchImpl = vi.fn(ok);
    vi.stubGlobal("fetch", fetchImpl);
    const { els } = setup();

    els.queueList.querySelector('[data-path="c.mp3"][data-action="top"]').click();

    await vi.waitFor(() => expect(els.queueAnnounce.textContent)
      .toBe("Moved Charlie to position 1 of 3."));
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body))
      .toEqual({ paths: ["c.mp3", "a.mp3", "b.mp3"] });
    expect(document.activeElement.dataset.path).toBe("c.mp3");
    vi.unstubAllGlobals();
  });

  it("clears only after confirmation and keeps focus on the button", async () => {
    const fetchImpl = vi.fn(ok);
    vi.stubGlobal("fetch", fetchImpl);
    const { els, dialog, answer } = setup();

    els.queueClear.click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledOnce());
    answer("cancel");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(els.queueClear);

    els.queueClear.click();
    await vi.waitFor(() => expect(dialog.showModal).toHaveBeenCalledTimes(2));
    answer("confirm");
    await vi.waitFor(() => expect(els.queueAnnounce.textContent)
      .toBe("Cleared the queue.  Removed 3 tracks."));
    expect(fetchImpl.mock.calls[0][0]).toBe("/api/queue/reorder");
    expect(JSON.parse(fetchImpl.mock.calls[0][1].body)).toEqual({ paths: [] });
    expect(els.queueList.querySelectorAll("li[data-path]")).toHaveLength(0);
    expect(document.activeElement).toBe(els.queueClear);
    vi.unstubAllGlobals();
  });
});
