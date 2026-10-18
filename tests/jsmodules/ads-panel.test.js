
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup } from "./helpers.js";

import {
  breakText,
  hourLabel,
  installAdsPanel,
  outcomeText,
  parseMinutes,
  reportSummary,
  spotText,
} from "../../src/autodj/static/modules/ads-panel.js";

const { Blob, File, Headers } = globalThis;
const byId = (id) => document.getElementById(id);
const status = () => (byId("ads-break-form-dialog")?.open
  ? byId("ads-break-form-status")?.textContent
  : byId("ads-spot-form-dialog")?.open
    ? byId("ads-spot-form-status")?.textContent : byId("ads-status").textContent);
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function installIndex() {
  installIndexMarkup();
  globalThis.HTMLDialogElement.prototype.showModal = function showModal() { this.open = true; };
  globalThis.HTMLDialogElement.prototype.close = function close(value = "") {
    this.returnValue = value;
    this.open = false;
    this.dispatchEvent(new Event("close"));
  };
  byId("panel-ads").hidden = false;
  const dialog = byId("confirm-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value = "") => {
    dialog.returnValue = value;
    dialog.open = false;
    dialog.dispatchEvent(new Event("close"));
  };
}

class FakeError extends Error {
  constructor(message, { status = 422, field = null } = {}) {
    super(message);
    this.status = status;
    this.field = field;
  }
}

const SPOT = {
  id: "a".repeat(32), title: "Fall sale", advertiser: "Example Motors", campaign: "Autumn",
  category: "Car dealers", start_date: "2026-10-01", end_date: "2026-10-31",
  dates_text: "Flight from 1 October 2026 until 31 October 2026", in_flight: true, flight_text: "",
  max_per_day: 4, max_per_hour: 1, enabled: true, seconds: 30, plays_today: 1, booked: false, missing: false,
};
const BREAK = {
  id: "b".repeat(32), name: "Quarter past", minutes: [15, 45], days: [], from_hour: 0, until_hour: 24,
  start_date: null, end_date: null, max_spots: 3, max_seconds: 0, enabled: true,
  when_text: "At :15, :45 past the hour", next_at: "2026-10-13T12:15:00+00:00",
  next_text: "Tuesday 13 October 2026 at 12:15",
};
const REPORT = {
  from: "2026-10-01", to: "2026-10-13", count: 2, truncated: false, damaged: 1,
  totals: { aired: 1, late: 1, missed: 1, waiting: 0, missed_breaks: 0 },
  summary: [{ advertiser: "Example Motors", campaign: "Autumn", aired: 1, late: 1, missed: 1, interrupted: 0, waiting: 0, seconds: 30 }],
  rows: [
    {
      id: "c".repeat(32), scheduled_text: "13 October 2026 at 12:15:00", aired_text: "13 October 2026 at 12:31:00",
      delay_minutes: 16, status: "late", status_text: "Aired late", break_name: "Quarter past",
      source_text: "Minutes past the hour", spot_id: "a".repeat(32), title: "Fall sale",
      advertiser: "Example Motors", campaign: "Autumn", seconds: 30, reason: "",
    },
    {
      id: "d".repeat(32), scheduled_text: "13 October 2026 at 12:45:00", aired_text: "",
      delay_minutes: null, status: "missed", status_text: "Missed", break_name: "Quarter past",
      source_text: "Run now", spot_id: "a".repeat(32), title: "Fall sale",
      advertiser: "Example Motors", campaign: "Autumn", seconds: 30, reason: "skipped before it aired",
    },
  ],
};

function fakeServer() {
  const server = {
    view: {
      station: "default", enabled: true, reason: null, problem: null, timezone: "UTC",
      limits: { max_bytes: 1024, max_seconds: 120, max_spots: 200, max_breaks: 50, max_spots_per_break: 10 },
      settings: { separation_minutes: 15, late_minutes: 10, miss_after_minutes: 30 },
      spots: [], breaks: [], pending: [], recent: [],
    },
    calls: [],
    fail: null,
  };
  server.request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    server.calls.push({ method, url, body: options.body });
    if (server.fail) {
      const failure = server.fail;
      server.fail = null;
      throw failure;
    }
    if (method === "GET" && url === "/api/ads") return JSON.parse(JSON.stringify(server.view));
    if (method === "POST" && url === "/api/ads/spots") {
      const spot = { ...SPOT, id: "e".repeat(32), title: options.body.get("title") };
      server.view.spots.push(spot);
      return spot;
    }
    if (method === "PUT" && url.startsWith("/api/ads/spots/")) {
      const body = JSON.parse(options.body);
      const spot = server.view.spots.find((item) => url.endsWith(item.id));
      Object.assign(spot, body);
      return spot;
    }
    if (method === "DELETE" && url.startsWith("/api/ads/spots/")) {
      server.view.spots = server.view.spots.filter((item) => !url.endsWith(item.id));
      return {};
    }
    if (method === "POST" && url === "/api/ads/breaks") {
      const item = { ...BREAK, ...JSON.parse(options.body), id: "f".repeat(32) };
      server.view.breaks.push(item);
      return item;
    }
    if (method === "POST" && url.endsWith("/run")) {
      return { break_name: "Quarter past", message: "Ad break Quarter past airs at the next song change it can join." };
    }
    if (method === "PUT" && url === "/api/ads/settings") return JSON.parse(options.body);
    if (method === "GET" && url.startsWith("/api/ads/report?")) return REPORT;
    throw new Error(`unexpected ${method} ${url}`);
  });
  return server;
}

let server;
let controls;

async function start(view = {}) {
  installIndex();
  server = fakeServer();
  Object.assign(server.view, view);
  controls = installAdsPanel({ request: server.request });
  await controls.refresh();
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("words", () => {
  it("says each spot, break and outcome in a sentence", () => {
    expect(spotText(SPOT)).toBe(
      "Fall sale, Example Motors, campaign Autumn. 30 seconds. Competing category Car dealers. "
      + "Flight from 1 October 2026 until 31 October 2026. Airs at most 4 a day and at most 1 an hour. "
      + "Aired or booked 1 time today.",
    );
    expect(spotText({ ...SPOT, enabled: false, missing: true, campaign: "", category: "" })).toContain(
      "Turned off. Its audio file is missing.",
    );
    expect(breakText(BREAK)).toBe(
      "Quarter past: At :15, :45 past the hour. Up to 3 spots. Next Tuesday 13 October 2026 at 12:15.",
    );
    expect(breakText({ ...BREAK, enabled: false, max_seconds: 90 })).toBe(
      "Quarter past: At :15, :45 past the hour. Up to 3 spots, 90 seconds at most. Turned off.",
    );
    expect(outcomeText(REPORT.rows[1])).toBe(
      "Missed: Example Motors, Fall sale, in Quarter past due 13 October 2026 at 12:45:00. skipped before it aired.",
    );
    expect(reportSummary(REPORT)).toBe(
      "Ad report from 2026-10-01 to 2026-10-13: 1 spot aired, 1 of them late, 1 spot missed.",
    );
    expect(reportSummary({ ...REPORT, totals: { ...REPORT.totals, interrupted: 2 } })).toBe(
      "Ad report from 2026-10-01 to 2026-10-13: 1 spot aired, 1 of them late, 1 spot missed, "
      + "2 spots cut off on the air that may have aired.",
    );
    expect(parseMinutes("45, 15 15")).toEqual([15, 45]);
    expect(parseMinutes("")).toEqual([]);
    expect(parseMinutes("15, 60")).toBeNull();
    expect(parseMinutes("quarter")).toBeNull();
    expect(hourLabel(6)).toBe("06:00");
    expect(hourLabel(24)).toBe("24:00, midnight");
  });
});

describe("the Ads tab", () => {
  it("lists spots and breaks with named buttons, and says why ads do not air", async () => {
    await start({ spots: [SPOT], breaks: [BREAK], reason: "Ad breaks are off for this station." });
    expect(byId("ads-reason").hidden).toBe(false);
    expect(byId("ads-reason").textContent).toBe("Ad breaks are off for this station.");
    expect(byId("ads-timezone").textContent).toBe("UTC");
    const spotButtons = [...byId("ads-spots").querySelectorAll("button")];
    expect(spotButtons.map((button) => button.getAttribute("aria-label"))).toEqual([
      "Edit spot Fall sale, Example Motors", "Delete spot Fall sale, Example Motors",
    ]);
    const breakButtons = [...byId("ads-breaks").querySelectorAll("button")];
    expect(breakButtons.map((button) => button.textContent)).toEqual(["Edit", "Run now", "Delete"]);
    expect(breakButtons[1].hasAttribute("aria-describedby")).toBe(false);
    expect(byId("ads-rules-separation").value).toBe("15");
    expect(byId("ads-pending").textContent).toBe("Nothing is waiting.");
  });

  it("names a missing file, focuses it and links the error, then uploads", async () => {
    await start();
    byId("ads-spot-add").click();
    expect(document.activeElement).toBe(byId("ads-spot-file"));
    byId("ads-spot-form").requestSubmit();
    const error = byId("ads-spot-error");
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe("Choose the spot's audio file");
    expect(byId("ads-spot-form-dialog").open).toBe(true);
    expect(byId("ads-panel").contains(byId("ads-spot-form"))).toBe(false);
    const file = byId("ads-spot-file");
    expect(file.getAttribute("aria-invalid")).toBe("true");
    expect(file.getAttribute("aria-describedby").split(" ")).toEqual(["ads-spot-file-desc", "ads-spot-error"]);
    expect(document.activeElement).toBe(file);
    await vi.waitFor(() => expect(status()).toBe("Choose the spot's audio file"));
    await flush();
    // A file, but no advertiser.
    Object.defineProperty(file, "files", { value: [new File(["x"], "spot.wav")], configurable: true });
    file.dispatchEvent(new Event("input", { bubbles: true }));
    expect(file.hasAttribute("aria-invalid")).toBe(false);
    byId("ads-spot-title").value = "Fall sale";
    byId("ads-spot-form").requestSubmit();
    expect(document.activeElement).toBe(byId("ads-spot-advertiser"));
    await flush();
    byId("ads-spot-advertiser").value = "Example Motors";
    byId("ads-spot-form").requestSubmit();
    await vi.waitFor(() => expect(byId("ads-spot-form").hidden).toBe(true));
    const post = server.calls.find((call) => call.method === "POST");
    expect(post.body.get("advertiser")).toBe("Example Motors");
    expect(post.body.get("max_per_day")).toBe("0");
    expect(document.activeElement.getAttribute("aria-label")).toBe("Edit spot Fall sale, Example Motors");
    expect(status()).toBe("Spot Fall sale saved");
  });

  it("puts the server's refusal on the field it names", async () => {
    await start({ spots: [SPOT] });
    byId("ads-spots").querySelector('[data-role="edit"]').click();
    expect(byId("ads-spot-file-row").hidden).toBe(true);
    expect(document.activeElement).toBe(byId("ads-spot-title"));
    server.fail = new FakeError("Plays a day: too many", { field: "max_per_day" });
    byId("ads-spot-form").requestSubmit();
    await vi.waitFor(() => expect(byId("ads-spot-error").hidden).toBe(false));
    expect(document.activeElement).toBe(byId("ads-spot-day"));
    expect(byId("ads-spot-day").getAttribute("aria-invalid")).toBe("true");
    byId("ads-spot-cancel").click();
    expect(document.activeElement.getAttribute("aria-label")).toBe("Edit spot Fall sale, Example Motors");
  });

  it("keeps a pending spot save modal open, ignores duplicate submits, and drops a stale save on clear", async () => {
    await start();
    let resolveSave;
    let postCount = 0;
    const request = server.request.getMockImplementation();
    server.request.mockImplementation((url, options) => {
      if (url === "/api/ads/spots" && options?.method === "POST") {
        postCount += 1;
        return new Promise((resolve) => { resolveSave = resolve; });
      }
      return request(url, options);
    });
    byId("ads-spot-add").click();
    Object.defineProperty(byId("ads-spot-file"), "files", { value: [new File(["x"], "spot.wav")], configurable: true });
    byId("ads-spot-title").value = "Pending spot";
    byId("ads-spot-advertiser").value = "Example Motors";
    byId("ads-spot-form").requestSubmit();
    await vi.waitFor(() => expect(resolveSave).toBeTypeOf("function"));
    byId("ads-spot-form").requestSubmit();
    expect(postCount).toBe(1);
    expect(byId("ads-spot-form").getAttribute("aria-busy")).toBe("true");
    controls.clear();
    expect(byId("ads-spot-form-dialog").open).toBe(false);
    resolveSave({ ...SPOT, id: "pending", title: "Pending spot" });
    await flush();
    expect(byId("ads-spots").textContent).toBe("");
    expect(status()).toBe("");
  });

  it("uses Escape to confirm spot discard and returns focus to Add spot", async () => {
    await start();
    const opener = byId("ads-spot-add");
    opener.click();
    byId("ads-spot-title").value = "Unsaved spot";
    const dialog = byId("ads-spot-form-dialog");
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(dialog.open).toBe(true);
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(dialog.open).toBe(false));
    expect(document.activeElement).toBe(opener);
  });

  it("deletes a spot after asking, then focuses the next row", async () => {
    await start({ spots: [SPOT, { ...SPOT, id: "9".repeat(32), title: "Winter sale" }] });
    byId("ads-spots").querySelector('[data-role="delete"]').click();
    expect(byId("confirm-title").textContent).toBe("Delete spot Fall sale?");
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Spot Fall sale deleted"));
    expect(document.activeElement.getAttribute("aria-label")).toBe("Edit spot Winter sale, Example Motors");
  });

  it("checks a break's minutes and hours, saves it and runs it", async () => {
    await start();
    byId("ads-break-add").click();
    expect(document.activeElement).toBe(byId("ads-break-name"));
    expect(byId("ads-break-minutes").value).toBe("15, 45");
    byId("ads-break-name").value = "Quarter past";
    byId("ads-break-minutes").value = "15, 75";
    byId("ads-break-form").requestSubmit();
    expect(document.activeElement).toBe(byId("ads-break-minutes"));
    expect(byId("ads-break-error").textContent).toMatch(/0 to 59/);
    await flush();
    byId("ads-break-minutes").value = "15, 45";
    byId("ads-break-from").value = "20";
    byId("ads-break-until").value = "6";
    byId("ads-break-form").requestSubmit();
    expect(document.activeElement).toBe(byId("ads-break-until"));
    await flush();
    byId("ads-break-until").value = "24";
    byId("ads-break-day-0").checked = true;
    byId("ads-break-form").requestSubmit();
    await vi.waitFor(() => expect(status()).toBe("Ad break Quarter past saved"));
    const post = server.calls.find((call) => call.method === "POST" && call.url === "/api/ads/breaks");
    expect(JSON.parse(post.body)).toMatchObject({ minutes: [15, 45], days: [0], from_hour: 20, until_hour: 24 });
    expect(document.activeElement.getAttribute("aria-label")).toBe("Edit ad break Quarter past");
    byId("ads-breaks").querySelector('[data-role="run-now"]').click();
    await vi.waitFor(() => expect(status()).toMatch(/airs at the next song change/));
  });

  it("keeps a pending break save modal open, ignores duplicate submits, and drops a stale save on clear", async () => {
    await start();
    let resolveSave;
    let postCount = 0;
    const request = server.request.getMockImplementation();
    server.request.mockImplementation((url, options) => {
      if (url === "/api/ads/breaks" && options?.method === "POST") {
        postCount += 1;
        return new Promise((resolve) => { resolveSave = resolve; });
      }
      return request(url, options);
    });
    byId("ads-break-add").click();
    byId("ads-break-name").value = "Pending break";
    byId("ads-break-form").requestSubmit();
    await vi.waitFor(() => expect(resolveSave).toBeTypeOf("function"));
    byId("ads-break-form").requestSubmit();
    expect(postCount).toBe(1);
    expect(byId("ads-break-form").getAttribute("aria-busy")).toBe("true");
    controls.clear();
    expect(byId("ads-break-form-dialog").open).toBe(false);
    resolveSave({ ...BREAK, id: "pending", name: "Pending break" });
    await flush();
    expect(byId("ads-breaks").textContent).toBe("");
    expect(status()).toBe("");
  });

  it("uses Escape to confirm break discard and returns focus to Add ad break", async () => {
    await start();
    const opener = byId("ads-break-add");
    opener.click();
    byId("ads-break-name").value = "Unsaved break";
    const dialog = byId("ads-break-form-dialog");
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(false));
    expect(dialog.open).toBe(true);
    dialog.dispatchEvent(new Event("cancel", { cancelable: true }));
    await vi.waitFor(() => expect(byId("confirm-dialog").open).toBe(true));
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(dialog.open).toBe(false));
    expect(document.activeElement).toBe(opener);
  });

  it("saves the rules and refuses a missed time shorter than the late one", async () => {
    await start();
    // The rules card is closed by default; a refused field opens it, so
    // focus and the error are on screen.
    const card = byId("ads-rules-form").closest("details");
    expect(card.open).toBe(false);
    byId("ads-rules-late").value = "40";
    byId("ads-rules-form").requestSubmit();
    expect(card.open).toBe(true);
    expect(document.activeElement).toBe(byId("ads-rules-missed"));
    expect(byId("ads-rules-error").textContent).toBe("Missed after must not be shorter than Late after");
    byId("ads-rules-missed").value = "45";
    byId("ads-rules-form").requestSubmit();
    await vi.waitFor(() => expect(status()).toBe("Rules saved"));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(JSON.parse(put.body)).toEqual({ separation_minutes: 15, late_minutes: 40, miss_after_minutes: 45 });
  });

  it("shows the report as tables with headers and says its totals once", async () => {
    await start();
    expect(byId("ads-report-from").value).toMatch(/^\d{4}-\d{2}-01$/);
    byId("ads-report-show").focus();
    byId("ads-report-form").requestSubmit();
    await vi.waitFor(() => expect(byId("ads-rows-wrap").hidden).toBe(false));
    expect(document.activeElement).toBe(byId("ads-report-show"));
    expect(status()).toBe(reportSummary(REPORT));
    const totals = byId("ads-totals").querySelector("tr");
    expect(totals.firstElementChild.tagName).toBe("TH");
    expect(totals.firstElementChild.getAttribute("scope")).toBe("row");
    expect([...totals.children].map((cell) => cell.textContent)).toEqual(["Example Motors", "Autumn", "1", "1", "1", "0", "0", "30"]);
    const rows = [...byId("ads-rows").querySelectorAll("tr")];
    expect([...rows[0].children].map((cell) => cell.textContent)).toEqual([
      "13 October 2026 at 12:15:00", "13 October 2026 at 12:31:00", "Aired late, 16 minutes after its time",
      "Example Motors: Fall sale, campaign Autumn", "Quarter past, Minutes past the hour", "None",
    ]);
    expect(rows[1].children[1].textContent).toBe("Did not air");
    expect(byId("ads-report-note").textContent).toMatch(/1 line of the ad play log could not be read/);
    // Dates out of order are refused on the field.
    byId("ads-report-to").value = "2026-09-01";
    byId("ads-report-form").requestSubmit();
    expect(document.activeElement).toBe(byId("ads-report-to"));
    expect(byId("ads-report-to").getAttribute("aria-invalid")).toBe("true");
  });

  it("exports the report through the page and says how many rows", async () => {
    installIndex();
    server = fakeServer();
    const download = vi.fn(async () => ({
      blob: async () => new Blob(["a,b"]),
      headers: new Headers({ "Content-Disposition": 'attachment; filename="autodj-ads-x.csv"', "X-Row-Count": "2" }),
    }));
    const save = vi.fn();
    controls = installAdsPanel({ request: server.request, download, save });
    await controls.refresh();
    byId("ads-report-format").value = "summary";
    byId("ads-report-export").click();
    await vi.waitFor(() => expect(save).toHaveBeenCalled());
    expect(download.mock.calls[0][0]).toMatch(/^\/api\/ads\/report\/export\?from=.*&format=summary$/);
    expect(save.mock.calls[0][2]).toBe("autodj-ads-x.csv");
    expect(status()).toBe("Ad report saved: 2 rows.");
  });

  it("forgets everything on clear", async () => {
    await start({ spots: [SPOT], breaks: [BREAK] });
    controls.clear();
    await flush();
    expect(byId("ads-spots").children).toHaveLength(0);
    expect(byId("ads-breaks").children).toHaveLength(0);
    expect(byId("ads-spot-form").hidden).toBe(true);
  });
});
