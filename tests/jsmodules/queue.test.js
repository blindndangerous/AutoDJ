import { afterEach, describe, expect, it, vi } from "vitest";
import * as queueModule from "../../src/autodj/static/modules/queue.js";
import { selectStation, setKnownStations } from "../../src/autodj/static/modules/stations.js";
import { invalidateAuthenticatedRequestEpoch } from "../../src/autodj/static/modules/api-client.js";

const { applyQueueState, installQueueButtons, installQueueClear, resetQueueState } = queueModule;
const track = (path, display_name = path) => ({ path, display_name });
const initial = () => [track("a.mp3", "Alpha"), track("b.mp3", "Bravo"), track("c.mp3", "Charlie")];
const response = (body = { ok: true }, status = 200) => new globalThis.Response(JSON.stringify(body), {
  status, headers: { "Content-Type": "application/json" },
});
const press = (key, extra = {}) => document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {
  key, bubbles: true, cancelable: true, ...extra,
}));
function setup(tracks = initial()) {
  document.body.innerHTML = `<p id="announce" role="status"></p><div id="queue" tabindex="-1" aria-label="Queued tracks"></div>
    <div id="status-toast" aria-hidden="true" hidden></div>
    <button id="clear">Clear queue</button><button id="outside">Search</button>
    <dialog id="confirm-dialog"><h2 id="confirm-title"></h2><p id="confirm-message"></p>
    <button id="confirm-accept"></button><button id="confirm-cancel">Cancel</button></dialog>`;
  const dialog = document.getElementById("confirm-dialog");
  dialog.show = vi.fn(() => dialog.setAttribute("open", ""));
  dialog.close = vi.fn(() => {
    dialog.removeAttribute("open");
    dialog.dispatchEvent(new Event("close"));
  });
  const els = { queueList: document.getElementById("queue"), queueCount: document.createElement("span"),
    queueAnnounce: document.getElementById("announce"), queueClear: document.getElementById("clear") };
  resetQueueState(els);
  applyQueueState(tracks, els);
  installQueueButtons(els);
  installQueueClear(els);
  watchBusy(els.queueList);
  const rows = () => [...els.queueList.querySelectorAll("tbody tr[data-row-key]")];
  const options = (index) => rows()[index].querySelector('[aria-haspopup="menu"]');
  const action = (index, id) => {
    options(index).focus();
    options(index).click();
    rows()[index].querySelector(`[role="menuitem"][data-action="${id}"]`).click();
  };
  return { els, dialog, rows, options, action, answer: (yes = true) =>
    document.getElementById(yes ? "confirm-accept" : "confirm-cancel").click() };
}
// The aria-busy values the list has left since setup or the last settled().
// Each record's old value is kept, so a busy state set and removed in one
// task is still seen.
const busyHistory = new WeakMap();
function watchBusy(list) {
  const seen = [];
  busyHistory.set(list, seen);
  new window.MutationObserver((records) => seen.push(...records.map((record) => record.oldValue)))
    .observe(list, { attributes: true, attributeFilter: ["aria-busy"], attributeOldValue: true });
}
// The action marked the list busy while it ran, and the shared helper then
// removed the attribute again.
async function settled(els) {
  const seen = busyHistory.get(els.queueList);
  await vi.waitFor(() => {
    expect(seen).toContain("true");
    expect(els.queueList.hasAttribute("aria-busy")).toBe(false);
  });
  seen.length = 0;
}
const bodies = (fetch) => fetch.mock.calls.map(([, init]) => JSON.parse(init.body));
afterEach(() => { selectStation("default"); vi.unstubAllGlobals(); });

describe("queue collection", () => {
  it("exposes state updates without a second public rendering entrypoint", () => {
    expect(queueModule).not.toHaveProperty("renderQueue");
  });

  // ADR-0021-12
  it("uses the shared queue table with Track info and confirmed removal", async () => {
    const fetch = vi.fn(async () => response()); vi.stubGlobal("fetch", fetch);
    const { els, rows, action, answer, dialog } = setup();
    expect(els.queueList.querySelector("table.collection-table[role=grid]")).not.toBeNull();
    expect([...els.queueList.querySelectorAll("th")].map((cell) => cell.textContent))
      .toEqual(["Position", "Track", "Bridge", "Actions", "Options"]);
    expect(els.queueList.querySelector('[role="rowheader"]')).toBeNull();
    expect([...rows()[0].querySelectorAll('button:not([role="menuitem"])')].map((button) => button.textContent))
      .toEqual(["Track info", "Options"]);
    expect([...rows()[0].querySelectorAll('[role="menuitem"]')].map((button) => button.textContent))
      .toEqual(["Move to top", "Move up", "Move down", "Remove"]);
    const info = vi.fn(); document.addEventListener("autodj:edit-points", info, { once: true });
    const button = rows()[0].querySelector('button[data-action="info"]'); button.click();
    expect(info.mock.calls[0][0].detail).toEqual({ path: "a.mp3", kind: "music", opener: button });
    action(1, "remove");
    expect(dialog.open).toBe(true); expect(fetch).not.toHaveBeenCalled();
    answer(false); expect(rows()).toHaveLength(3);
    action(1, "remove"); answer();
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe("Removed Bravo from queue. Focus moved to Charlie."));
    await settled(els);
    expect(fetch.mock.calls[0][0]).toBe("/api/queue/remove");
    expect(bodies(fetch)).toEqual([{ path: "b.mp3", position: 1 }]);
    expect(rows().map((row) => row.cells[1].textContent)).toEqual(["Alpha", "Charlie"]);
    expect(rows()[1].contains(document.activeElement)).toBe(true);
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe("Removed Bravo from queue. Focus moved to Charlie."));
    expect(document.getElementById("status-toast").textContent).toBe("Removed Bravo from queue. Focus moved to Charlie.");
    expect(document.getElementById("status-toast").hidden).toBe(false);
  });

  it("uses one table Tab stop with cell arrows and menu opening keys", () => {
    const { els, rows, options } = setup();
    expect(els.queueList.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    rows()[0].cells[1].focus(); press("ArrowDown"); expect(document.activeElement).toBe(rows()[1].cells[1]);
    press("End"); expect(document.activeElement).toBe(rows()[1].cells[4]);
    press("Enter"); expect(document.activeElement.dataset.action).toBe("top");
    press("Escape"); expect(document.activeElement).toBe(options(1).closest("td"));
    press("ArrowUp"); expect(document.activeElement).toBe(options(0).closest("td"));
    press("Enter");
    press("ArrowUp"); expect(document.activeElement.dataset.action).toBe("remove");
    press("Escape"); expect(els.queueList.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
  });

  it("keeps disabled boundary actions reachable and describes their reasons", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const { rows, options, els } = setup([track("a.mp3")]);
    options(0).focus(); press("Enter");
    expect(document.activeElement.dataset.action).toBe("top");
    for (const id of ["top", "up", "down"]) {
      const item = rows()[0].querySelector(`[role=menuitem][data-action=${id}]`);
      expect(item.getAttribute("aria-disabled")).toBe("true");
      expect(document.getElementById(item.getAttribute("aria-describedby")).textContent).toMatch(/already/i);
    }
    press("Enter"); await vi.waitFor(() => expect(els.queueAnnounce.textContent).toMatch(/already/i));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("blocks off-air menu keyboard actions and rechecks an open removal confirmation", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const { els, options, action, answer, rows } = setup();
    action(1, "remove");
    queueModule.setQueueDisabledReason(els, "Start the station to change its queue.");
    answer();
    await vi.waitFor(() => expect(document.getElementById("confirm-dialog").open).toBe(false));
    options(1).focus(); press("Enter"); press("Enter");
    expect(document.activeElement.getAttribute("aria-disabled")).toBe("true");
    expect(fetch).not.toHaveBeenCalled();
    const info = vi.fn(); document.addEventListener("autodj:edit-points", info, { once: true });
    rows()[1].querySelector('button[data-action="info"]').click(); expect(info).toHaveBeenCalledOnce();
    queueModule.setQueueDisabledReason(els, "");
    expect(rows()[1].querySelector('[data-action="up"]').getAttribute("aria-disabled")).toBe("false");
  });

  it("refreshes metadata and bridge names without replacing focused rows or open menus", () => {
    const { els, rows, options } = setup([track("a.mp3", "Alpha"), track("b.mp3", "Bravo")]);
    const row = rows()[1]; options(1).focus(); press("Enter"); const focused = document.activeElement;
    expect(focused.getAttribute("role")).toBe("menuitem");
    applyQueueState([track("a.mp3", "Alpha"), { ...track("b.mp3", "Renamed"), bridge_to: "Request" }], els);
    expect(rows()[1]).toBe(row); expect(document.activeElement).toBe(focused);
    expect(row.cells[2].textContent).toBe("Bridge to Request");
    expect(options(1).getAttribute("aria-label")).toBe("Options for Renamed, bridge to Request");
    expect(row.querySelector('button[data-action="info"]').getAttribute("aria-label")).toContain("bridge to Request");
  });

  it("uses current row positions after a refresh while a menu is open", async () => {
    const fetch = vi.fn(async () => response()); vi.stubGlobal("fetch", fetch);
    const { els, options } = setup(); options(2).focus(); press("Enter");
    expect(document.activeElement.getAttribute("role")).toBe("menuitem");
    applyQueueState([initial()[2], initial()[0], initial()[1]], els);
    press("ArrowDown"); press("ArrowDown"); press("Enter"); await settled(els);
    expect(bodies(fetch)[0]).toEqual({ paths: ["a.mp3", "c.mp3", "b.mp3"],
      from_paths: ["c.mp3", "a.mp3", "b.mp3"], order: [1, 0, 2] });
  });

  it.each([["top", 2, ["c.mp3", "a.mp3", "b.mp3"], [2, 0, 1], "Moved Charlie to position 1 of 3."],
    ["up", 1, ["b.mp3", "a.mp3", "c.mp3"], [1, 0, 2], "Moved Bravo to position 1 of 3."],
    ["down", 1, ["a.mp3", "c.mp3", "b.mp3"], [0, 2, 1], "Moved Bravo to position 3 of 3."]])("optimistically moves %s with occurrence order and position feedback", async (id, index, paths, order, message) => {
    let reply; const fetch = vi.fn(() => new Promise((resolve) => { reply = resolve; })); vi.stubGlobal("fetch", fetch);
    const { els, rows, action } = setup(); const old = rows(); const selected = old[index];
    action(index, id);
    expect(rows()).toEqual(order.map((at) => old[at]));
    expect(selected.contains(document.activeElement)).toBe(true);
    expect(bodies(fetch)[0]).toEqual({ paths, from_paths: ["a.mp3", "b.mp3", "c.mp3"], order });
    reply(response()); await settled(els);
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe(message));
    expect(document.getElementById("status-toast").textContent).toBe(message);
    expect(document.getElementById("status-toast").hidden).toBe(false);
  });

  it("announces and mirrors repeated Move up actions with their new positions", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response()));
    const { els, action } = setup();
    action(2, "up"); await settled(els);
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe("Moved Charlie to position 2 of 3."));
    expect(document.getElementById("status-toast").textContent).toBe("Moved Charlie to position 2 of 3.");
    action(1, "up"); await settled(els);
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe("Moved Charlie to position 1 of 3."));
    expect(document.getElementById("status-toast").textContent).toBe("Moved Charlie to position 1 of 3.");
  });

  it("preserves distinct duplicate occurrences during optimistic moves and confirmed removal", async () => {
    const fetch = vi.fn(async () => response()); vi.stubGlobal("fetch", fetch);
    const { rows, action, answer, els } = setup([track("dup.mp3", "First"), track("x.mp3", "Middle"), track("dup.mp3", "Second")]);
    const [first, middle, second] = rows(); action(0, "down");
    expect(rows()).toEqual([middle, first, second]); await settled(els);
    action(2, "remove"); answer(); await settled(els);
    expect(rows()).toEqual([middle, first]);
    expect(fetch.mock.calls[1][0]).toBe("/api/queue/reorder");
    expect(bodies(fetch)[1]).toEqual({ paths: ["x.mp3", "dup.mp3"],
      from_paths: ["x.mp3", "dup.mp3", "dup.mp3"], order: [0, 1] });
  });

  it("cancels removal if the queue changes before confirmation", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const { els, action, answer } = setup(); action(1, "remove");
    applyQueueState([initial()[1], initial()[2]], els); answer();
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toMatch(/changed/i));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("refuses a second mutation while a change is pending", async () => {
    let reply; const fetch = vi.fn(() => new Promise((resolve) => { reply = resolve; })); vi.stubGlobal("fetch", fetch);
    const { els, action } = setup(); action(1, "down"); action(0, "remove"); action(0, "remove");
    expect(fetch).toHaveBeenCalledOnce(); expect(document.getElementById("confirm-dialog").open).toBe(false);
    reply(response()); await settled(els);
  });

  it("keeps a new session's mutation pending when an older mutation finishes", async () => {
    const replies = [];
    const fetch = vi.fn(() => new Promise((resolve) => replies.push(resolve)));
    vi.stubGlobal("fetch", fetch);
    const { els, action, rows } = setup();
    action(1, "down");
    invalidateAuthenticatedRequestEpoch();
    resetQueueState(els);
    applyQueueState(initial(), els);
    action(2, "top");
    expect(fetch).toHaveBeenCalledTimes(2);
    replies[0](response());
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(els.queueList.getAttribute("aria-busy")).toBe("true");
    applyQueueState(initial(), els);
    expect(rows().map((row) => row.cells[1].textContent)).toEqual(["Charlie", "Alpha", "Bravo"]);
    action(1, "down");
    expect(fetch).toHaveBeenCalledTimes(2);
    replies[1](response());
    await vi.waitFor(() => expect(els.queueList.getAttribute("aria-busy")).not.toBe("true"));
    action(1, "down");
    expect(fetch).toHaveBeenCalledTimes(3);
    replies[2](response());
    await vi.waitFor(() => expect(els.queueList.getAttribute("aria-busy")).not.toBe("true"));
  });

  it("ignores a push of the original queue while the reorder is pending", async () => {
    let reply; vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { reply = resolve; })));
    const { els, rows, action } = setup(); action(2, "top"); applyQueueState(initial(), els);
    expect(rows()[0].cells[1].textContent).toBe("Charlie"); reply(response()); await settled(els);
  });

  it.each([200, 500])("keeps newer websocket rows and focus when a pending response is %s", async (status) => {
    let reply; vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { reply = resolve; })));
    const { els, rows, action } = setup(); action(2, "up");
    applyQueueState([initial()[2], track("d.mp3", "Delta")], els);
    const focused = document.activeElement;
    reply(response(status === 200 ? { ok: true } : { detail: "write failed" }, status)); await settled(els);
    expect(rows().map((row) => row.cells[1].textContent)).toEqual(["Charlie", "Delta"]);
    expect(document.activeElement).toBe(focused);
  });

  it("rolls back a failed mutation without stealing focus moved outside the queue", async () => {
    let reply; vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { reply = resolve; })));
    const { els, rows, action } = setup(); action(1, "down");
    document.getElementById("outside").focus(); reply(response({ detail: "write failed" }, 500)); await settled(els);
    expect(rows().map((row) => row.cells[1].textContent)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect(document.activeElement.id).toBe("outside"); expect(els.queueAnnounce.textContent).toContain("write failed");
  });

  it("restores the moved row and focus after a rejected reorder", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ detail: "write failed" }, 500)));
    const { els, rows, action } = setup(); const row = rows()[1]; action(1, "down"); await settled(els);
    expect(rows()[1]).toBe(row); expect(row.contains(document.activeElement)).toBe(true);
  });

  it("restores the removed occurrence and its focus after a rejected removal", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ detail: "write failed" }, 500)));
    const { els, rows, action, answer } = setup(); action(1, "remove"); answer();
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toContain("write failed"));
    await settled(els);
    expect(rows().map((row) => row.cells[1].textContent)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect(rows()[1].contains(document.activeElement)).toBe(true);
  });

  it("reports old-station results without restoring its queue after a station switch", async () => {
    setKnownStations([{ slug: "default", name: "Main" }, { slug: "night", name: "Night" }]);
    let reply; vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { reply = resolve; })));
    const { els, rows, action } = setup(); action(1, "down"); selectStation("night"); resetQueueState(els);
    reply(response());
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toContain("Done on Main"));
    await settled(els);
    expect(rows()).toHaveLength(0);
  });

  it("does not send a removal confirmed after station or auth context changed", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const { action, answer } = setup(); action(1, "remove"); invalidateAuthenticatedRequestEpoch(); answer();
    await vi.waitFor(() => expect(document.getElementById("confirm-dialog").open).toBe(false));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("clears busy without announcing stale authenticated replies", async () => {
    let reply; vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { reply = resolve; })));
    const { els, action } = setup(); action(1, "down"); invalidateAuthenticatedRequestEpoch();
    document.getElementById("outside").focus(); reply(response()); await settled(els);
    expect(document.activeElement.id).toBe("outside"); expect(els.queueAnnounce.textContent).not.toContain("Moved");
  });

  it("uses a shared empty row and named fallback after removing the last occurrence", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response()));
    const { els, action, answer } = setup([track("a.mp3", "Alpha")]); action(0, "remove"); answer();
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe("Removed Alpha from queue. Focus moved to Queued tracks."));
    await settled(els);
    const empty = els.queueList.querySelector("td.no-results"); expect(empty.colSpan).toBe(5);
    expect(document.activeElement).toBe(els.queueList);
  });

  it("recovers to an adjacent cell on a passive removal and leaves outside focus alone", () => {
    const { els, rows } = setup(); rows()[1].cells[1].focus(); applyQueueState([initial()[0], initial()[2]], els);
    expect(document.activeElement).toBe(rows()[1].cells[1]);
    document.getElementById("outside").focus(); applyQueueState([], els); expect(document.activeElement.id).toBe("outside");
  });

  it("keeps bridge duplicates separate and refreshes changed labels", () => {
    const { els, rows } = setup([{ ...track("same.mp3", "Mid"), bridge_to: "Ballad" }, track("same.mp3", "Mid")]);
    expect(rows()[0].cells[2].textContent).toBe("Bridge to Ballad"); expect(rows()[1].cells[2].textContent).toBe("");
    applyQueueState([track("same.mp3", "Mid"), track("same.mp3", "Mid")], els);
    expect(rows()[0].cells[2].textContent).toBe("");
  });

  it("does not collide queue keys containing delimiter characters", () => {
    const { els, rows } = setup([track("a|b", "First"), track("c", "Tail")]);
    applyQueueState([track("a", "Second"), track("b|c", "Other")], els);
    expect(rows().map((row) => row.cells[1].textContent)).toEqual(["Second", "Other"]);
  });

  it("reasks on new voice revisions and retries the exact confirmed removal", async () => {
    const conflict = (revision) => response({ detail: { code: "voice_track_conflict", revision } }, 409);
    const fetch = vi.fn().mockResolvedValueOnce(conflict("one")).mockResolvedValueOnce(conflict("two")).mockResolvedValueOnce(response());
    vi.stubGlobal("fetch", fetch); const { els, action, answer } = setup(); action(1, "remove"); answer();
    await vi.waitFor(() => expect(document.getElementById("confirm-title").textContent).toBe("Upcoming voice track")); answer();
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(document.getElementById("confirm-dialog").open).toBe(true));
    [...document.querySelectorAll("#confirm-dialog button")].find((button) => button.textContent === "Discard voice track").click();
    await settled(els);
    expect(bodies(fetch)).toEqual([{ path: "b.mp3", position: 1 },
      { path: "b.mp3", position: 1, voice_action: "keep", voice_revision: "one" },
      { path: "b.mp3", position: 1, voice_action: "discard", voice_revision: "two" }]);
  });

  it("restores the queue when a voice conflict is canceled", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ detail: { code: "voice_track_conflict", revision: "one" } }, 409)));
    const { els, rows, action, answer } = setup(); action(2, "top");
    await vi.waitFor(() => expect(document.getElementById("confirm-dialog").open).toBe(true)); answer(false); await settled(els);
    expect(rows().map((row) => row.cells[1].textContent)).toEqual(["Alpha", "Bravo", "Charlie"]);
    expect(els.queueAnnounce.textContent).toBe("Queue change canceled.");
  });

  it("does not retry or claim success when the station changes during a voice choice", async () => {
    setKnownStations([{ slug: "default", name: "Main" }, { slug: "night", name: "Night" }]);
    const fetch = vi.fn(async () => response({ detail: { code: "voice_track_conflict", revision: "one" } }, 409));
    vi.stubGlobal("fetch", fetch);
    const { els, rows, action, answer } = setup(); action(2, "top");
    await vi.waitFor(() => expect(document.getElementById("confirm-dialog").open).toBe(true));
    selectStation("night"); resetQueueState(els); answer(); await settled(els);
    expect(fetch).toHaveBeenCalledOnce(); expect(rows()).toHaveLength(0);
    expect(els.queueAnnounce.textContent).not.toContain("Moved");
  });

  it("confirms Clear and returns directly to its button after clearing", async () => {
    const fetch = vi.fn(async () => response()); vi.stubGlobal("fetch", fetch);
    const { els, rows, answer } = setup(); els.queueClear.click(); answer(false);
    expect(rows()).toHaveLength(3); expect(fetch).not.toHaveBeenCalled();
    els.queueClear.click(); answer(); await vi.waitFor(() => expect(rows()).toHaveLength(0));
    await vi.waitFor(() => expect(document.activeElement).toBe(els.queueClear));
    expect(bodies(fetch)).toEqual([{ paths: [] }]);
  });

  it("keeps Clear voice-conflict cancellation intact", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ detail: { code: "voice_track_conflict", revision: "one" } }, 409)));
    const { els, rows, answer } = setup(); els.queueClear.click(); answer();
    await vi.waitFor(() => expect(document.getElementById("confirm-title").textContent).toBe("Upcoming voice track")); answer(false);
    await vi.waitFor(() => expect(els.queueAnnounce.textContent).toBe("Queue clear canceled.")); expect(rows()).toHaveLength(3);
  });
});
