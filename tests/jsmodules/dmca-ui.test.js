import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installSearch } from "../../src/autodj/static/modules/search.js";

const htmlSource = readFileSync(
  join(process.cwd(), "src", "autodj", "static", "index.html"), "utf8",
);

const WARNING = "DMCA mode: playing this song at the end of the queue would mean "
  + "more than 3 songs from the album “Abbey Road” in 3 hours.";

function jsonResponse(body, status = 200) {
  return new globalThis.Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const byId = (id) => document.getElementById(id);

// The real page, with the search card wired as app.js wires it.
function mount({ playNow } = {}) {
  const template = document.createElement("template");
  template.innerHTML = htmlSource;
  template.content.querySelectorAll('script, link[rel="stylesheet"]')
    .forEach((element) => element.remove());
  document.body.replaceChildren(template.content.cloneNode(true));
  const dialog = byId("confirm-dialog");
  // Open and close as a browser would; the confirmation opens with show(),
  // and close fires a task after the dialog closes.
  dialog.show = vi.fn(() => { dialog.open = true; });
  dialog.close = vi.fn((value = "") => {
    if (!dialog.open) return;
    dialog.returnValue = value;
    dialog.open = false;
    setTimeout(() => dialog.dispatchEvent(new Event("close")), 0);
  });
  installSearch({
    searchInput: byId("search-input"),
    btnSearch: byId("btn-search"),
    searchResults: byId("search-results"),
    searchCount: byId("search-count"),
    queueAnnounce: byId("queue-announce"),
    ...(playNow ? { playNow } : {}),
  });
  return dialog;
}

// Search, then return the result button with this visible label.
async function resultButton(label) {
  byId("search-input").value = "come";
  byId("btn-search").click();
  await vi.waitFor(() => expect(byId("search-results").textContent).toContain("Come Together"));
  return [...byId("search-results").querySelectorAll("button")]
    .find((button) => button.textContent === label);
}

// Answers the search, then each queue request in turn.
function stubServer(...replies) {
  const fetchImpl = vi.fn((url) => {
    if (url.startsWith("/api/search")) {
      return Promise.resolve(jsonResponse({
        results: [{ path: "a.flac", title: "Come Together" }],
      }));
    }
    const [body, status] = replies.shift();
    return Promise.resolve(jsonResponse(body, status));
  });
  vi.stubGlobal("fetch", fetchImpl);
  return () => fetchImpl.mock.calls.filter(([url]) => !url.startsWith("/api/search"))
    .map(([url, init]) => [url, JSON.parse(init.body)]);
}

const announced = () => byId("queue-announce").textContent;

describe("queueing a song over a DMCA limit", () => {
  let dialog;

  beforeEach(() => {
    dialog = mount();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks in the shared dialog, with the limit in words, before queueing anyway", async () => {
    const posts = stubServer([{ detail: WARNING, field: "confirm" }, 409], [{ ok: true }, 200]);
    const opener = await resultButton("Add to queue");
    opener.focus();
    opener.click();

    await vi.waitFor(() => expect(dialog.open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Over the DMCA limit");
    expect(byId("confirm-message").textContent).toBe(`${WARNING} Queue it anyway?`);
    expect(byId("confirm-accept").textContent).toBe("Queue anyway");
    // Cancel has focus, so a stray Enter queues nothing.
    expect(document.activeElement).toBe(byId("confirm-cancel"));
    // Nothing is queued yet.
    expect(posts()).toEqual([["/api/queue/add", { path: "a.flac" }]]);

    byId("confirm-accept").click();
    await vi.waitFor(() => expect(announced())
      .toBe("Added Come Together to queue, over the DMCA limit."));
    expect(posts()).toEqual([
      ["/api/queue/add", { path: "a.flac" }],
      ["/api/queue/add", { path: "a.flac", confirm: true }],
    ]);
    expect(dialog.open).toBe(false);
    expect(document.activeElement).toBe(opener.closest("td"));
  });

  it("says the song was not queued when the operator cancels", async () => {
    const posts = stubServer([{ detail: WARNING, field: "confirm" }, 409]);
    (await resultButton("Play next")).click();
    await vi.waitFor(() => expect(dialog.open).toBe(true));

    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(announced()).toBe("Come Together was not queued."));
    expect(posts()).toEqual([["/api/play-next", { path: "a.flac", now: false }]]);
  });

  it("reports a confirmed request that fails", async () => {
    stubServer(
      [{ detail: WARNING, field: "confirm" }, 409],
      [{ detail: "That track is no longer in the library." }, 404],
    );
    (await resultButton("Add to queue")).click();
    await vi.waitFor(() => expect(dialog.open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(announced()).toBe(
      "Could not add Come Together to the queue. That track is no longer in the library.",
    ));
  });

  it("treats any other refusal as a failure, without the dialog", async () => {
    stubServer([{ detail: "Busy." }, 409]);
    (await resultButton("Add to queue")).click();
    await vi.waitFor(() => expect(announced())
      .toBe("Could not add Come Together to the queue. Busy."));
    expect(dialog.show).not.toHaveBeenCalled();
  });
});

describe("Play now over a DMCA limit", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("offers Play anyway and starts the page inside each click", async () => {
    const playNow = vi.fn(async (request) => { await request(); return true; });
    const dialog = mount({ playNow });
    const posts = stubServer([{ detail: WARNING, field: "confirm" }, 409], [{ ok: true }, 200]);
    (await resultButton("Play now")).click();
    await vi.waitFor(() => expect(dialog.open).toBe(true));
    expect(byId("confirm-message").textContent).toBe(`${WARNING} Play it now anyway?`);
    expect(byId("confirm-accept").textContent).toBe("Play anyway");

    byId("confirm-accept").click();
    // The confirmed request runs through playNow inside the dialog click.
    expect(playNow).toHaveBeenCalledTimes(2);
    await vi.waitFor(() => expect(announced())
      .toBe("Playing Come Together now, over the DMCA limit."));
    expect(posts()).toEqual([
      ["/api/play-next", { path: "a.flac", now: true }],
      ["/api/play-next", { path: "a.flac", now: true, confirm: true }],
    ]);
  });

  it("says nothing more when Play now already said why nothing plays", async () => {
    const playNow = vi.fn(async (request) => { await request(); return false; });
    mount({ playNow });
    stubServer([{ ok: true }, 200]);
    (await resultButton("Play now")).click();
    await vi.waitFor(() => expect(playNow).toHaveBeenCalledOnce());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(announced()).toBe("");
  });
});

describe("upcoming voice track choices", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("retries Play next with the chosen action and exact conflict revision", async () => {
    const dialog = mount();
    const posts = stubServer(
      [{ detail: { code: "voice_track_conflict", revision: "rev-1", message: "A voice track is due." } }, 409],
      [{ ok: true }, 200],
    );
    (await resultButton("Play next")).click();

    await vi.waitFor(() => expect(dialog.open).toBe(true));
    expect(byId("confirm-title").textContent).toBe("Upcoming voice track");
    expect(byId("confirm-message").textContent)
      .toBe("A voice track is due. Keep the voice track with this change?");
    expect([...dialog.querySelectorAll("button")].map((button) => button.textContent))
      .toEqual(["Keep voice track", "Discard voice track", "Cancel change"]);
    byId("confirm-accept").click();

    await vi.waitFor(() => expect(announced()).toBe("Come Together plays next."));
    expect(posts()).toEqual([
      ["/api/play-next", { path: "a.flac", now: false }],
      ["/api/play-next", {
        path: "a.flac", now: false, voice_action: "keep", voice_revision: "rev-1",
      }],
    ]);
    expect([...dialog.querySelectorAll("button")].map((button) => button.id))
      .toEqual(["confirm-accept", "confirm-cancel"]);
  });

  it("asks again for a newer conflict and preserves DMCA confirmation", async () => {
    const dialog = mount();
    const posts = stubServer(
      [{ detail: { code: "voice_track_conflict", revision: "rev-1" } }, 409],
      [{ detail: WARNING, field: "confirm" }, 409],
      [{ detail: { code: "voice_track_conflict", revision: "rev-2" } }, 409],
      [{ ok: true }, 200],
    );
    (await resultButton("Play next")).click();
    await vi.waitFor(() => expect(dialog.open).toBe(true));
    byId("confirm-accept").click();

    await vi.waitFor(() => expect(byId("confirm-title").textContent).toBe("Over the DMCA limit"));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(byId("confirm-title").textContent).toBe("Upcoming voice track"));
    expect(byId("confirm-message").textContent).toBe("Keep the voice track with this change?");
    [...dialog.querySelectorAll("button")].find((button) => button.textContent === "Discard voice track").click();

    await vi.waitFor(() => expect(announced()).toBe("Come Together plays next, over the DMCA limit."));
    expect(posts()).toEqual([
      ["/api/play-next", { path: "a.flac", now: false }],
      ["/api/play-next", {
        path: "a.flac", now: false, voice_action: "keep", voice_revision: "rev-1",
      }],
      ["/api/play-next", {
        path: "a.flac", now: false, confirm: true, voice_action: "keep", voice_revision: "rev-1",
      }],
      ["/api/play-next", {
        path: "a.flac", now: false, confirm: true, voice_action: "discard", voice_revision: "rev-2",
      }],
    ]);
  });

  it("cancels without retrying the mutation", async () => {
    const dialog = mount();
    const posts = stubServer(
      [{ detail: { code: "voice_track_conflict", revision: "rev-1" } }, 409],
    );
    (await resultButton("Play next")).click();
    await vi.waitFor(() => expect(dialog.open).toBe(true));
    byId("confirm-cancel").click();

    await vi.waitFor(() => expect(announced()).toBe("Come Together was not queued."));
    expect(posts()).toEqual([["/api/play-next", { path: "a.flac", now: false }]]);
  });
});
