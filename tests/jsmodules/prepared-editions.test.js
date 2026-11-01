import { setImmediate } from "node:timers/promises";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";
import { installPreparedEditions } from "../../src/autodj/static/modules/prepared-editions.js";
import { invalidateAuthenticatedRequestEpoch } from "../../src/autodj/static/modules/api-client.js";
import { currentStation, selectStation } from "../../src/autodj/static/modules/stations.js";
const { Blob, File, EventTarget } = globalThis;

const get = (id) => document.getElementById(`edition-${id}`);
const installed = [];
const show = { id: "show", name: "Tomorrow", songs: [{ path: "/a", title: "A" }, { path: "/b", title: "B" }] };
const edition = {
  id: "edition", show_id: "show", name: "Tomorrow edition", revision: 3,
  occurrences: [{ id: "first", kind: "song", path: "/a" }, { id: "second", kind: "song", path: "/b" }],
  assets: {}, takes: [], previously_started: false,
};
const take = { id: "take", asset_id: "asset", label: "Introduction", position: "transition",
  before_id: "first", after_id: "second", placement: "auto", status: "needs_review" };
function takeAction(action, index = 0) {
  const row = get("takes").querySelectorAll("tbody tr")[index];
  if (action !== "listen") {
    const menu = row.querySelector("button[aria-haspopup='menu']");
    if (menu.getAttribute("aria-expanded") !== "true") menu.click();
  }
  return row.querySelector(`button[data-action='${action}']`);
}
function submitReattach() {
  get("reattach-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}
async function settle() { for (let i = 0; i < 30; i += 1) await Promise.resolve(); }

// ADR-0021-11 ADR-0021-39
it("uses shared occurrence and take tables with dialog reattachment", async () => {
  const detached = { ...edition, takes: [{ ...take, status: "unattached" }] };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [detached] }
    : url.endsWith("/reattach") ? { ...detached, revision: 4, takes: [take] } : detached);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  for (const name of ["occurrences", "takes"]) {
    expect(get(name).querySelector("table.collection-table")).not.toBeNull();
    expect(get(name).querySelector("[role='rowheader']")).toBeNull();
  }
  expect([...get("occurrences").querySelectorAll("th")].map((cell) => cell.textContent)).toEqual(["Item", "Actions"]);
  expect(get("occurrences").querySelector("tbody tr").cells[0].textContent).toMatch(/^1\. /);
  expect([...get("takes").querySelectorAll("th")].map((cell) => cell.textContent)).toEqual(["Name", "Placement", "Status", "Actions"]);
  const row = get("takes").querySelector("tbody tr");
  expect(row.querySelector("button[data-action='listen']").textContent).toBe("Listen");
  const options = row.querySelector("button[aria-haspopup='menu']");
  options.click();
  expect([...row.querySelectorAll("[role='menuitem']")].map((item) => item.textContent))
    .toEqual(["Listen", "Re-record", "Review", "Approve", "Reattach", "Remove"]);
  const review = row.querySelector("button[data-action='review']");
  expect(review.getAttribute("aria-disabled")).toBe("true");
  expect(review.disabled).toBe(false);
  expect(document.getElementById(review.getAttribute("aria-describedby")).textContent).toContain("Reattach");
  row.querySelector("button[data-action='reattach']").click();
  const dialog = get("reattach-form-dialog");
  expect(dialog.open).toBe(true);
  expect(document.activeElement).toBe(get("reattach-position"));
  expect(get("takes").querySelector("select")).toBeNull();
  get("reattach-cancel").click();
  expect(dialog.open).toBe(false);
  expect(document.activeElement.closest("td")).toBe(options.closest("td"));
  options.click(); row.querySelector("button[data-action='reattach']").click();
  get("reattach-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await settle();
  expect(dialog.open).toBe(false);
  const saved = request.mock.calls.find(([url]) => url.endsWith("/reattach"));
  expect(JSON.parse(saved[1].body)).toEqual({ expected_revision: 3, before_id: "first", after_id: "second" });
  expect(document.activeElement).toBe(options.closest("td"));
});

it("confirms occurrence removal and keeps the selected occurrence after cancellation", async () => {
  const { controls, request } = harness();
  await controls.openShow(show); get("load").click(); await settle();
  const row = get("occurrences").querySelector("tbody tr");
  row.querySelector("button[aria-haspopup='menu']").click();
  row.querySelector("button[data-action='remove']").click(); await settle();
  expect(document.getElementById("confirm-dialog").open).toBe(true);
  expect(request.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
  document.getElementById("confirm-cancel").click(); await settle();
  expect(get("occurrences").querySelectorAll("tbody tr")).toHaveLength(2);
  row.querySelector("button[aria-haspopup='menu']").click();
  row.querySelector("button[data-action='remove']").click();
  document.getElementById("confirm-accept").click(); await settle();
  const saved = request.mock.calls.find(([, init]) => init?.method === "PUT");
  expect(JSON.parse(saved[1].body).occurrences.map((item) => item.id)).toEqual(["second"]);
  expect(document.activeElement.closest("tr").dataset.rowKey).toBe("second");
  expect(get("status").textContent).toContain("Focus moved to Options for Item 1, B.");
});

it("confirms take removal while retaining source audio and restores the empty table fallback", async () => {
  const value = { ...edition, takes: [take], assets: { asset: { id: "asset" } } };
  const request = vi.fn(async (url, init) => {
    if (init?.method === "DELETE") return { ...value, takes: [], revision: 4 };
    return url.includes("?") ? { editions: [value] } : value;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("remove").click(); await settle();
  expect(document.getElementById("confirm-message").textContent).toContain("saved source audio is retained");
  expect(request.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  document.getElementById("confirm-cancel").click();
  takeAction("remove").click(); document.getElementById("confirm-accept").click(); await settle();
  expect(request.mock.calls.find(([, init]) => init?.method === "DELETE")[0])
    .toBe("/api/editions/edition/takes/take?expected_revision=3");
  expect(get("takes").querySelector(".no-results").textContent).toBe("No takes.");
  expect(document.activeElement).toBe(get("heading"));
  expect(get("status").textContent).toContain(`Focus moved to ${get("heading").textContent}.`);
});

it("drops a reattachment response after the protected edition context is cleared", async () => {
  const value = { ...edition, takes: [{ ...take, status: "unattached" }] };
  let reply;
  const request = vi.fn(async (url) => url.endsWith("/reattach") ? new Promise((resolve) => { reply = resolve; })
    : url.includes("?") ? { editions: [value] } : value);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("reattach").click(); submitReattach(); await settle();
  controls.clear(); reply({ ...value, revision: 4, takes: [take] }); await settle();
  expect(get("reattach-form-dialog").open).toBe(false);
  expect(controls.active()).toBe(false);
  expect(get("takes").querySelector("tbody").children).toHaveLength(0);
});

it("does not remove an occurrence while an edition save is pending", async () => {
  let finish;
  const request = vi.fn(async (url, init) => {
    if (init?.method === "PUT") return new Promise((resolve) => { finish = resolve; });
    return url.includes("?") ? { editions: [edition] } : edition;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  get("save-name").click(); await settle();
  const row = get("occurrences").querySelector("tbody tr");
  row.querySelector("button[aria-haspopup='menu']").click();
  const remove = row.querySelector("button[data-action='remove']");
  expect(remove.getAttribute("aria-disabled")).toBe("true");
  expect(document.getElementById(remove.getAttribute("aria-describedby")).textContent).toContain("save");
  row.querySelector("button[data-action='remove']").click(); await settle();
  document.getElementById("confirm-accept").click(); await settle();
  const count = request.mock.calls.filter(([, init]) => init?.method === "PUT").length;
  finish(edition); await settle();
  expect(count).toBe(1);
});

// ADR-0021-11
it("does not remove a take while an edition save is pending", async () => {
  const value = { ...edition, takes: [take] };
  let finish;
  const request = vi.fn(async (url, init) => {
    if (init?.method === "PUT") return new Promise((resolve) => { finish = resolve; });
    return url.includes("?") ? { editions: [value] } : value;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  get("save-name").click(); await settle();
  const remove = takeAction("remove");
  expect(remove.getAttribute("aria-disabled")).toBe("true");
  expect(document.getElementById(remove.getAttribute("aria-describedby")).textContent)
    .toBe("Wait for the edition save to finish.");
  remove.click(); await settle();
  expect(document.getElementById("confirm-dialog").open).toBe(false);
  expect(request.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  finish(value); await settle();
  expect(takeAction("remove").getAttribute("aria-disabled")).toBe("false");
});

it("keeps take removal unavailable until an imported take finishes saving", async () => {
  const value = { ...edition, takes: [take] };
  let finish;
  const request = vi.fn(async (url, init) => {
    if (init?.method === "POST") return new Promise((resolve) => { finish = resolve; });
    return url.includes("?") ? { editions: [value] } : value;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  Object.defineProperty(get("file"), "files", { value: [new File(["speech"], "take.wav")] });
  get("import").click(); await settle();
  const remove = takeAction("remove");
  expect(remove.getAttribute("aria-disabled")).toBe("true");
  expect(document.getElementById(remove.getAttribute("aria-describedby")).textContent)
    .toBe("Wait for the edition save to finish.");
  remove.click(); await settle();
  expect(request.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  finish(value); await settle();
  expect(takeAction("remove").getAttribute("aria-disabled")).toBe("false");
});

it("rechecks an edition save before confirming take removal", async () => {
  const value = { ...edition, takes: [take] };
  let finish;
  const request = vi.fn(async (url, init) => {
    if (init?.method === "PUT") return new Promise((resolve) => { finish = resolve; });
    if (init?.method === "DELETE") return { ...value, takes: [], revision: 4 };
    return url.includes("?") ? { editions: [value] } : value;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("remove").click(); await settle();
  const saving = controls.addSong({ path: "/c", title: "C" }); await settle();
  document.getElementById("confirm-accept").click(); await settle();
  expect(request.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
  expect(get("takes").querySelectorAll("tbody tr[data-row-key]")).toHaveLength(1);
  finish(value); await saving;
});

it.each(["authentication", "station"])("allows saving the same edition after reloading across a %s epoch change", async (change) => {
  let finish;
  const request = vi.fn(async (url, init) => {
    if (init?.method === "PUT") return new Promise((resolve) => { finish = resolve; });
    return url.includes("?") ? { editions: [edition] } : edition;
  });
  const { controls } = harness({ request });
  const previousStation = currentStation();
  try {
    await controls.openShow(show); get("load").click(); await settle();
    get("save-name").click(); await settle();
    if (change === "authentication") invalidateAuthenticatedRequestEpoch();
    else selectStation(previousStation === "other-station" ? "default" : "other-station");
    finish({ ...edition, name: "Old response", revision: 4 }); await settle();
    get("load").click(); await settle();
    expect(get("name").value).toBe(edition.name);
    get("save-name").click(); await settle();
    expect(request.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(2);
    finish({ ...edition, name: "Current response", revision: 4 }); await settle();
    expect(get("name").value).toBe("Current response");
  } finally { selectStation(previousStation); }
});

it("clears a previous reattachment error when reopening the editor", async () => {
  const value = { ...edition, takes: [{ ...take, status: "unattached" }] };
  const request = vi.fn(async (url) => {
    if (url.endsWith("/reattach")) throw new Error("Edition changed; reload before reattaching.");
    return url.includes("?") ? { editions: [value] } : value;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("reattach").click(); submitReattach(); await settle();
  expect(get("reattach-form-dialog").open).toBe(true);
  expect(get("reattach-position").getAttribute("aria-invalid")).toBe("true");
  expect(get("reattach-error").textContent).toContain("Edition changed");
  get("reattach-cancel").click(); takeAction("reattach").click();
  expect(get("reattach-position").hasAttribute("aria-invalid")).toBe(false);
  expect(get("reattach-error").textContent).toBe("");
  expect(get("reattach-position").hasAttribute("aria-describedby")).toBe(false);
});

it.each(["occurrences", "takes"])("reports refused %s removal after confirmation closes and retains row focus", async (collection) => {
  const value = { ...edition, takes: [take] };
  const request = vi.fn(async (url, init) => {
    if (["PUT", "DELETE"].includes(init?.method)) throw new Error("The edition changed; reload before removing this item.");
    return url.includes("?") ? { editions: [value] } : value;
  });
  const { controls, say } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  const row = get(collection).querySelector("tbody tr");
  const options = row.querySelector("button[aria-haspopup='menu']");
  options.click(); row.querySelector("button[data-action='remove']").click(); await settle();
  let openAtError = null;
  say.mockImplementation(() => { openAtError = document.getElementById("confirm-dialog").open; });
  document.getElementById("confirm-accept").click(); await settle();
  expect(document.getElementById("confirm-dialog").open).toBe(false);
  expect(row.isConnected).toBe(true);
  expect(get("status").textContent).toBe("The edition changed; reload before removing this item.");
  expect(document.activeElement).toBe(options.closest("td"));
  expect(openAtError).toBe(false);
});

it.each([false, true])("explains an already approved take before checking its review receipt (%s)", async (hasReceipt) => {
  const value = { ...edition, takes: [{ ...take, status: "approved" }] };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [value] } : value);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  if (hasReceipt) {
    takeAction("review").click();
    document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", { detail: {
      edition_id: "edition", take_id: "take", review_id: "review", revision: 3,
    } }));
  }
  const approve = takeAction("approve");
  expect(approve.getAttribute("aria-disabled")).toBe("true");
  expect(approve.disabled).toBe(false);
  expect(document.getElementById(approve.getAttribute("aria-describedby")).textContent)
    .toBe("This take is already approved.");
  approve.focus();
  approve.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await vi.waitFor(() => expect(get("status").textContent).toBe("This take is already approved."));
  expect(document.activeElement).toBe(approve);
  expect(request.mock.calls.some(([url]) => url.endsWith("/approve"))).toBe(false);
});

it("keeps approval pending across Options keyboard activation and submits only once", async () => {
  const value = { ...edition, takes: [take] };
  const replies = [];
  const request = vi.fn(async (url) => url.endsWith("/approve") ? new Promise((resolve) => replies.push(resolve))
    : url.includes("?") ? { editions: [value] } : value);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("review").click();
  document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", { detail: {
    edition_id: "edition", take_id: "take", review_id: "review", revision: 3,
  } }));
  takeAction("approve").click(); await settle();
  const row = get("takes").querySelector("tbody tr");
  const options = row.querySelector("button[aria-haspopup='menu']");
  options.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  const approve = row.querySelector("button[data-action='approve']");
  approve.focus(); approve.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await settle();
  const count = replies.length;
  const pending = approve.getAttribute("aria-disabled");
  for (const reply of replies) reply({ ...value, revision: 4, takes: [{ ...take, status: "ready" }] });
  await settle();
  expect(count).toBe(1);
  expect(pending).toBe("true");
  expect(options.disabled).toBe(false);
});

it("leaves focus where the user moved it while an approval was saving", async () => {
  const value = { ...edition, takes: [take] };
  const replies = [];
  const request = vi.fn(async (url) => url.endsWith("/approve") ? new Promise((resolve) => replies.push(resolve))
    : url.includes("?") ? { editions: [value] } : value);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("review").click();
  document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", { detail: {
    edition_id: "edition", take_id: "take", review_id: "review", revision: 3,
  } }));
  takeAction("approve").click(); await settle();
  get("takes").querySelector("tbody tr td").focus();
  for (const reply of replies) reply({ ...value, revision: 4, takes: [{ ...take, status: "ready" }] });
  await settle();
  expect(get("status").textContent).toBe("Take approved for its reviewed placement.");
  expect(document.activeElement).toBe(get("takes").querySelector("tbody tr td"));
});

it("returns focus to the approved take when focus was lost while saving", async () => {
  const value = { ...edition, takes: [take] };
  const replies = [];
  const request = vi.fn(async (url) => url.endsWith("/approve") ? new Promise((resolve) => replies.push(resolve))
    : url.includes("?") ? { editions: [value] } : value);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("review").click();
  document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", { detail: {
    edition_id: "edition", take_id: "take", review_id: "review", revision: 3,
  } }));
  takeAction("approve").click(); await settle();
  document.activeElement?.blur();
  for (const reply of replies) reply({ ...value, revision: 4, takes: [{ ...take, status: "ready" }] });
  await settle();
  expect(document.activeElement?.closest("tr[data-row-key]")?.dataset.rowKey).toBe("take");
});

it("resolves a prepared start conflict without changing the saved edition", async () => {
  const attempts = [];
  const request = vi.fn(async (url, options) => {
    if (url.endsWith("/start")) {
      const body = JSON.parse(options.body); attempts.push(body);
      if (!body.voice_action) throw Object.assign(new Error("Voice choice needed"), {
        status: 409, detail: { code: "voice_track_conflict", revision: "edition-choice" },
      });
      return { message: "Started" };
    }
    return url.includes("?") ? { editions: [edition] } : edition;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  const dialog = document.getElementById("confirm-dialog");
  dialog.show = () => { dialog.open = true; };
  dialog.close = () => { dialog.open = false; dialog.dispatchEvent(new Event("close")); };
  get("start").focus(); get("start").click();
  await vi.waitFor(() => expect(dialog.open).toBe(true));
  expect(document.activeElement).toBe(document.getElementById("confirm-cancel"));
  document.getElementById("confirm-accept").click();
  await vi.waitFor(() => expect(attempts).toHaveLength(2));
  expect(attempts[1]).toMatchObject({ voice_action: "keep", voice_revision: "edition-choice" });
  expect(request.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
});
function harness(options = {}) {
  const request = vi.fn(async (url, init) => {
    if (url.startsWith("/api/editions?")) return { editions: [edition] };
    if (init?.method === "PUT") return { ...edition, ...JSON.parse(init.body), revision: 4 };
    return edition;
  });
  const say = vi.fn();
  const controls = installPreparedEditions({ doc: document, request, announce: say, ...options });
  installed.push(controls);
  return { request, controls, say };
}
beforeEach(() => { installIndexMarkup(); document.getElementById("panel-library").hidden = false; });
afterEach(async () => {
  for (const controls of installed.splice(0)) controls.destroy();
  // Let HappyDOM release WeakRef targets between full-page fixtures.
  await setImmediate();
});

it("loads editions separately from the reusable show and preserves occurrence identities on reorder", async () => {
  const { controls, request } = harness();
  await controls.openShow(show);
  get("load").click(); await settle();
  const row = get("occurrences").querySelector("tbody tr");
  row.querySelector("button[aria-haspopup='menu']").click();
  row.querySelector("button[data-action='down']").click(); await settle();
  const [, init] = request.mock.calls.find(([, value]) => value?.method === "PUT");
  const body = JSON.parse(init.body);
  expect(body.expected_revision).toBe(3);
  expect(body.occurrences.map((item) => item.id)).toEqual(["second", "first"]);
  expect(show.songs.map((item) => item.path)).toEqual(["/a", "/b"]);
  expect(request.mock.calls.every(([url]) => url.startsWith("/api/editions"))).toBe(true);
});

it("uses an explicit full-target review receipt and never approves on playback or review completion", async () => {
  const withTake = { ...edition, takes: [take], assets: { asset: { id: "asset", seconds: 8 } } };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [withTake] } : withTake);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  expect(get("takes").querySelector("button[data-action='review']").getAttribute("aria-label"))
    .toBe("Review");
  expect(get("takes").querySelector("button[aria-haspopup='menu']").getAttribute("aria-label"))
    .toBe("Options for Introduction; transition from item 1, A to item 2, B");
  const events = [];
  document.addEventListener("autodj:review-edition", (event) => events.push(event.detail), { once: true });
  takeAction("review").click();
  expect(events[0]).toMatchObject({ edition_id: "edition", take_id: "take", revision: 3, include_pending: true });
  document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", {
    detail: { edition_id: "edition", take_id: "take", review_id: "review", revision: 3 },
  }));
  expect(request.mock.calls.some(([url]) => url.includes("/approve"))).toBe(false);
  takeAction("approve").click(); await settle();
  const approval = request.mock.calls.find(([url]) => url.endsWith("/takes/take/approve"));
  expect(JSON.parse(approval[1].body)).toEqual({ review_id: "review" });
});

it("invalidates receipts on edits and ignores another edition's review", async () => {
  const withTake = { ...edition, takes: [take] };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [withTake] } : withTake);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", {
    detail: { edition_id: "other", take_id: "take", review_id: "review", revision: 3 },
  }));
  expect(get("takes").querySelector("button[data-action='approve']").getAttribute("aria-disabled")).toBe("true");
});

it("stops late microphone grants and drops responses after clearing protected context", async () => {
  let grant;
  const pending = new Promise((done) => { grant = done; });
  const stop = vi.fn();
  const { controls } = harness({ media: () => ({ getUserMedia: () => pending }), Recorder: () => class {} });
  await controls.openShow(show); get("load").click(); await settle();
  get("record").click(); await settle();
  controls.clear(); grant({ getTracks: () => [{ stop }] }); await settle();
  expect(stop).toHaveBeenCalledOnce();
  expect(get("editor").hidden).toBe(true);
  expect(get("audio").hasAttribute("src")).toBe(false);
});

it.each(["needs_review", "missing", "invalid"])("saves a recording and preserves focus when the saved take is %s", async (status) => {
  const stop = vi.fn(), timers = [];
  const request = vi.fn(async (url, init) => {
    if (url.startsWith("/api/editions?")) return { editions: [edition] };
    return init?.method === "POST" && url.includes("/takes?")
      ? { ...edition, takes: [{ ...take, status }] } : edition;
  });
  class Recorder extends EventTarget {
    mimeType = "audio/webm";
    start() {}
    stop() {
      const event = new Event("dataavailable");
      event.data = new Blob(["recorded"], { type: "audio/webm" });
      this.dispatchEvent(event); this.dispatchEvent(new Event("stop"));
    }
  }
  const { controls } = harness({
    request,
    media: () => ({ getUserMedia: async () => ({ getTracks: () => [{ stop }] }) }),
    Recorder: () => Recorder, objectUrl: () => "blob:take", revokeUrl: vi.fn(),
    setTimer: (fn, milliseconds) => { timers.push({ fn, milliseconds }); return 1; }, clearTimer: vi.fn(),
    toWav: async () => new Blob(["wav"], { type: "audio/wav" }),
  });
  await controls.openShow(show); get("load").click(); await settle();
  get("record").click(); await settle();
  expect(timers[0].milliseconds).toBe(180000);
  timers[0].fn(); await settle();
  expect(stop).toHaveBeenCalledOnce();
  get("save-take").focus();
  get("save-take").click(); await settle();
  const upload = request.mock.calls.find(([url, init]) => url.includes("/takes?") && init?.method === "POST");
  expect(upload[0]).toContain("position=opening");
  expect(upload[0]).toContain("expected_revision=3");
  expect(upload[1].body).toBeInstanceOf(Blob);
  expect(document.activeElement).toBe(status === "needs_review"
    ? get("takes").querySelector("td[data-action='options']") : get("heading"));
});

it("focuses the edition list after deleting the open edition", async () => {
  const { controls } = harness({ confirm: async () => true });
  await controls.openShow(show); get("load").click(); await settle();
  get("delete").focus(); get("delete").click(); await settle();
  expect(get("editor").hidden).toBe(true);
  expect(document.activeElement).toBe(get("list"));
});

it("reattaches an unattached take to the selected pair and focuses its options cell", async () => {
  const detached = { ...edition, takes: [{ ...take, status: "unattached" }] };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [detached] }
    : url.endsWith("/reattach") ? { ...detached, revision: 4, takes: [take] } : detached);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  expect(get("takes").textContent).toContain("Unattached");
  expect(get("takes").querySelector("button[data-action='review']").getAttribute("aria-disabled")).toBe("true");
  takeAction("reattach").click(); submitReattach(); await settle();
  const upload = request.mock.calls.find(([url]) => url.endsWith("/reattach"));
  expect(JSON.parse(upload[1].body)).toEqual({ expected_revision: 3, before_id: "first", after_id: "second" });
  expect(document.activeElement).toBe(get("takes").querySelector("td[data-action='options']"));
});

it("gives same-label unattached takes distinct position names", async () => {
  const value = { ...edition, takes: [
    { ...take, label: "Speech", status: "unattached", after_id: "removed" },
    { ...take, id: "other", label: "Speech", status: "unattached", before_id: "removed" },
  ] };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [value] } : value);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  const labels = [...get("takes").querySelectorAll("button[aria-haspopup='menu']")].map((button) => button.getAttribute("aria-label"));
  expect(labels).toHaveLength(2);
  expect(new Set(labels).size).toBe(2);
  expect(labels[0]).toContain("item 1, A");
  expect(labels[1]).toContain("item 2, B");
});

it("sends one reattachment when its focused button is pressed twice", async () => {
  const detached = { ...edition, takes: [{ ...take, status: "unattached" }] };
  const replies = [];
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [detached] }
    : url.endsWith("/reattach") ? new Promise((resolve) => replies.push(resolve)) : detached);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("reattach").click();
  submitReattach(); submitReattach(); await settle();
  const count = replies.length;
  for (const reply of replies) reply({ ...detached, revision: 4, takes: [take] });
  await settle();
  expect(count).toBe(1);
  expect(document.activeElement).toBe(get("takes").querySelector("td[data-action='options']"));
});

it("uses revision-safe import and keeps existing audio after a refused request", async () => {
  const { controls, request } = harness();
  await controls.openShow(show); get("load").click(); await settle();
  const input = get("file");
  const file = new File(["interview"], "interview.mp3", { type: "audio/mpeg" });
  Object.defineProperty(input, "files", { value: [file] });
  request.mockRejectedValueOnce(new Error("Edition changed; reload before saving"));
  get("import").click(); await settle();
  expect(get("status").textContent).toContain("reload");
  expect(input.files[0]).toBe(file);
});

it("requires explicit reair confirmation and sends the chosen resume occurrence", async () => {
  const aired = { ...edition, previously_started: true };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [aired] } : aired);
  const confirm = vi.fn(async () => false);
  const { controls } = harness({ request, confirm });
  await controls.openShow(show); get("load").click(); await settle();
  get("resume-item").value = "second";
  get("resume").click(); await settle();
  expect(request.mock.calls.some(([url]) => url.endsWith("/start"))).toBe(false);
  confirm.mockResolvedValue(true);
  get("resume").click(); await settle();
  const start = request.mock.calls.find(([url]) => url.endsWith("/start"));
  expect(JSON.parse(start[1].body)).toEqual({ now: false, confirm_reair: true, from_occurrence: "second" });
});

it("schedules through the existing show action with explicit edition reair consent", async () => {
  const aired = { ...edition, previously_started: true };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [aired] } : aired);
  const { controls } = harness({ request, confirm: async () => true });
  await controls.openShow(show); get("load").click(); await settle();
  get("schedule-at").value = "2026-10-18T12:30";
  get("schedule").click(); await settle();
  const scheduled = request.mock.calls.find(([url]) => url === "/api/schedule");
  expect(JSON.parse(scheduled[1].body)).toMatchObject({
    action: "show", params: { show: "edition-confirmed:edition" }, repeat: "once", date: "2026-10-18", time: "12:30",
  });
});

it("keeps a full-target review valid across unrelated edition revisions", async () => {
  const withTake = { ...edition, takes: [{ ...take, dependency_hash: "unchanged" }] };
  const request = vi.fn(async (url, init) => {
    if (url.includes("?")) return { editions: [withTake] };
    return init?.method === "PUT" ? { ...withTake, revision: 4 } : withTake;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  takeAction("review").click();
  get("name").value = "Unrelated rename"; get("save-name").click(); await settle();
  document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", { detail: {
    edition_id: "edition", take_id: "take", review_id: "review", revision: 3,
  } }));
  expect(get("takes").querySelector("button[data-action='approve']").getAttribute("aria-disabled")).toBe("false");
});

it("drops a delayed edition response after changing the show", async () => {
  let resolve;
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [edition] }
    : new Promise((done) => { resolve = done; }));
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  await controls.openShow({ ...show, id: "other" });
  resolve(edition); await settle();
  expect(controls.active()).toBe(false);
  expect(get("editor").hidden).toBe(true);
  expect(get("audio").hasAttribute("src")).toBe(false);
});

it("focuses invalid input and announces an import failure once", async () => {
  const { controls, say } = harness();
  await controls.openShow(show); get("load").click(); await settle();
  say.mockClear(); get("import").click(); await settle();
  expect(document.activeElement).toBe(get("file"));
  expect(get("file").getAttribute("aria-invalid")).toBe("true");
  expect(say).toHaveBeenCalledExactlyOnceWith("Choose an audio file to import.");
});

it("shows and announces the committed-audio warning with a successful edit", async () => {
  const notice = "Previously approved audio already committed for the current item continues; edits affect future audio.";
  const request = vi.fn(async (url, init) => {
    if (url.includes("?")) return { editions: [edition] };
    return init?.method === "PUT" ? { ...edition, revision: 4, committed_audio_notice: notice } : edition;
  });
  const { controls, say } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  say.mockClear(); get("name").value = "Edited while airing"; get("save-name").click(); await settle();
  expect(get("run-status").textContent).toContain(notice);
  expect(say).toHaveBeenCalledExactlyOnceWith(`Edition saved. ${notice}`);
});


it("distinguishes repeated transition pairs and insertion points by item position", async () => {
  const repeated = { ...edition, occurrences: [
    { id: "a1", kind: "song", path: "/a" }, { id: "b1", kind: "song", path: "/b" },
    { id: "a2", kind: "song", path: "/a" }, { id: "b2", kind: "song", path: "/b" },
  ] };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [repeated] } : repeated);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  expect([...get("transition").options].map((item) => [item.value, item.textContent])).toEqual([
    ["0", "Item 1, A into item 2, B"], ["1", "Item 2, B into item 3, A"],
    ["2", "Item 3, A into item 4, B"],
  ]);
  expect([...get("insert").options].map((item) => [item.value, item.textContent])).toEqual([
    ["0", "Before item 1, A"], ["1", "Before item 2, B"],
    ["2", "Before item 3, A"], ["3", "Before item 4, B"],
    ["4", "At the end (after item 4)"],
  ]);
});

it("shows interrupted run state and waits for explicit resumption", async () => {
  const interrupted = { ...edition, previously_started: true,
    run: { id: edition.id, edition_id: edition.id, name: edition.name,
      interrupted: true, pending_resume: false, resume_occurrence: "second",
      occurrences: [{ id: "first", label: "A", kind: "song" }, { id: "second", label: "B", kind: "song" }] },
  };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [interrupted] } : interrupted);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  expect(get("run-status").textContent).toBe("Interrupted. Choose an item and explicitly resume.");
  expect([...get("resume-item").options].map((item) => item.value)).toEqual(["first", "second"]);
  expect(request.mock.calls.some(([url]) => url.endsWith("/start"))).toBe(false);
});

it("keeps an unsaved recording when listening and confirms before replacing it", async () => {
  class Recorder extends EventTarget {
    mimeType = "audio/webm";
    start() {}
    stop() {
      const event = new Event("dataavailable"); event.data = new Blob(["voice"]);
      this.dispatchEvent(event); this.dispatchEvent(new Event("stop"));
    }
  }
  const value = { ...edition, takes: [take], assets: { asset: { id: "asset", seconds: 8 } } };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [value] } : value);
  const confirm = vi.fn(async () => false), revokeUrl = vi.fn();
  const { controls } = harness({ request, confirm, revokeUrl, objectUrl: () => "blob:unsaved",
    media: () => ({ getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) }),
    Recorder: () => Recorder });
  await controls.openShow(show); get("load").click(); await settle();
  get("record").click(); await settle(); get("record").click(); await settle();
  expect(get("save-take").disabled).toBe(false);
  takeAction("listen").click(); await settle();
  expect(revokeUrl).not.toHaveBeenCalled();
  expect(get("save-take").disabled).toBe(false);
  takeAction("rerecord").click(); await settle();
  expect(confirm).toHaveBeenCalledOnce();
  expect(get("save-take").disabled).toBe(false);
  expect(revokeUrl).not.toHaveBeenCalled();
  confirm.mockResolvedValue(true);
  takeAction("rerecord").click(); await settle();
  expect(revokeUrl).toHaveBeenCalledWith("blob:unsaved");
  expect(get("save-take").disabled).toBe(true);
});

it("starts resume choices at the saved occurrence, preserves a choice and refuses a missing one", async () => {
  let value = { ...edition, run: { interrupted: true, resume_occurrence: "second" } };
  const request = vi.fn(async (url) => url.includes("?") ? { editions: [value] } : value);
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  expect(get("resume-item").value).toBe("second");
  get("resume-item").value = "first";
  get("save-name").click(); await settle();
  expect(get("resume-item").value).toBe("first");
  value = { ...value, occurrences: [edition.occurrences[1]] };
  get("load").click(); await settle();
  expect(get("resume-item").selectedIndex).toBe(-1);
  get("resume").click(); await settle();
  expect(request.mock.calls.some(([url]) => url.endsWith("/start"))).toBe(false);
});

it("focuses Open edition on a stale save without invalid-state markup on the button", async () => {
  const { controls, request } = harness();
  await controls.openShow(show); get("load").click(); await settle();
  request.mockRejectedValueOnce(Object.assign(new Error("Edition changed"), { status: 409 }));
  get("save-name").click(); await settle();
  expect(document.activeElement).toBe(get("load"));
  expect(get("load").hasAttribute("aria-invalid")).toBe(false);
});

it.each(["record", "load", "create", "import", "discard", "switch Show"])("cancelling %s keeps the unsaved recording and performs no replacement", async (action) => {
  class Recorder extends EventTarget {
    mimeType = "audio/webm";
    start() {}
    stop() {
      const event = new Event("dataavailable"); event.data = new Blob(["voice"]);
      this.dispatchEvent(event); this.dispatchEvent(new Event("stop"));
    }
  }
  const confirm = vi.fn(async () => false), revokeUrl = vi.fn();
  const media = vi.fn(async () => ({ getTracks: () => [{ stop() {} }] }));
  const { controls, request } = harness({ confirm, revokeUrl, objectUrl: () => "blob:unsaved",
    media: () => ({ getUserMedia: media }), Recorder: () => Recorder });
  await controls.openShow(show); get("load").click(); await settle();
  get("record").click(); await settle(); get("record").click(); await settle();
  Object.defineProperty(get("file"), "files", { value: [new File(["replacement"], "take.wav")] });
  request.mockClear();
  if (action === "switch Show") await controls.openShow({ ...show, id: "other-show" });
  else get(action).click();
  await settle();
  expect(confirm).toHaveBeenCalledOnce();
  expect(request).not.toHaveBeenCalled();
  expect(media).toHaveBeenCalledOnce();
  expect(get("save-take").disabled).toBe(false);
  expect(get("audio").getAttribute("src")).toBe("blob:unsaved");
  expect(revokeUrl).not.toHaveBeenCalled();
});


it("refreshes the started edition rather than a different dropdown selection", async () => {
  const other = { ...edition, id: "other", name: "Other edition" };
  const request = vi.fn(async (url) => {
    if (url.includes("?")) return { editions: [edition, other] };
    return url === "/api/editions/other" ? other : edition;
  });
  const { controls } = harness({ request });
  await controls.openShow(show); get("load").click(); await settle();
  get("list").value = other.id;
  get("start").click(); await settle();
  expect(get("name").value).toBe(edition.name);
  expect(request.mock.calls.some(([url]) => url === "/api/editions/other")).toBe(false);
});

it("reports a superseded Show open so the parent editor cannot apply stale context", async () => {
  const replies = new Map();
  const request = vi.fn((url) => new Promise((done) => replies.set(new URL(url, "http://test").searchParams.get("show_id"), done)));
  const { controls } = harness({ request });
  const first = controls.openShow({ ...show, id: "older" }); await settle();
  const second = controls.openShow({ ...show, id: "newer" }); await settle();
  replies.get("newer")({ editions: [] }); expect(await second).toBe(true);
  replies.get("older")({ editions: [] }); expect(await first).toBe(false);
});
