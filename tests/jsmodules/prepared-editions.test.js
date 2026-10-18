import { setImmediate } from "node:timers/promises";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { installIndexMarkup } from "./helpers.js";
import { installPreparedEditions } from "../../src/autodj/static/modules/prepared-editions.js";
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
async function settle() { for (let i = 0; i < 30; i += 1) await Promise.resolve(); }
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
  const buttons = get("occurrences").querySelectorAll("button[data-action='down']");
  buttons[0].click(); await settle();
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
    .toBe("Review pending speech Introduction; transition from item 1, A to item 2, B");
  const events = [];
  document.addEventListener("autodj:review-edition", (event) => events.push(event.detail), { once: true });
  get("takes").querySelector("button[data-action='review']").click();
  expect(events[0]).toMatchObject({ edition_id: "edition", take_id: "take", revision: 3, include_pending: true });
  document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", {
    detail: { edition_id: "edition", take_id: "take", review_id: "review", revision: 3 },
  }));
  expect(request.mock.calls.some(([url]) => url.includes("/approve"))).toBe(false);
  get("takes").querySelector("button[data-action='approve']").click(); await settle();
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
  expect(get("takes").querySelector("button[data-action='approve']").disabled).toBe(true);
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

it("records without a near-air slot, caps the microphone, and uploads the chosen position", async () => {
  const stop = vi.fn(), timers = [];
  class Recorder extends EventTarget {
    mimeType = "audio/webm";
    start() {}
    stop() {
      const event = new Event("dataavailable");
      event.data = new Blob(["recorded"], { type: "audio/webm" });
      this.dispatchEvent(event); this.dispatchEvent(new Event("stop"));
    }
  }
  const { controls, request } = harness({
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
  get("save-take").click(); await settle();
  const upload = request.mock.calls.find(([url, init]) => url.includes("/takes?") && init?.method === "POST");
  expect(upload[0]).toContain("position=opening");
  expect(upload[0]).toContain("expected_revision=3");
  expect(upload[1].body).toBeInstanceOf(Blob);
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
  get("takes").querySelector("button[data-action='review']").click();
  get("name").value = "Unrelated rename"; get("save-name").click(); await settle();
  document.dispatchEvent(new CustomEvent("autodj:edition-review-ready", { detail: {
    edition_id: "edition", take_id: "take", review_id: "review", revision: 3,
  } }));
  expect(get("takes").querySelector("button[data-action='approve']").disabled).toBe(false);
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
  get("takes").querySelector("[data-action='listen']").click(); await settle();
  expect(revokeUrl).not.toHaveBeenCalled();
  expect(get("save-take").disabled).toBe(false);
  get("takes").querySelector("[data-action='rerecord']").click(); await settle();
  expect(confirm).toHaveBeenCalledOnce();
  expect(get("save-take").disabled).toBe(false);
  expect(revokeUrl).not.toHaveBeenCalled();
  confirm.mockResolvedValue(true);
  get("takes").querySelector("[data-action='rerecord']").click(); await settle();
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
