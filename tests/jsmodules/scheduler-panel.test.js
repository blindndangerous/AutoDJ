
import { beforeEach, describe, expect, it, vi } from "vitest";

import { installIndexMarkup } from "./helpers.js";

import { installSchedulerPanel } from "../../src/autodj/static/modules/scheduler-panel.js";
import { initSubtabs } from "../../src/autodj/static/modules/subtabs.js";

const byId = (id) => document.getElementById(id);
const status = () => byId("schedule-status").textContent;
const NOW = Date.parse("2026-10-11T12:00:00Z");

const ACTIONS = [
  {
    name: "profile", description: "Switch profile",
    fields: [{ name: "name", label: "Profile", type: "select", options: [{ value: "Morning", label: "Morning" }, { value: "Night", label: "Night" }] }],
  },
  { name: "note", description: "Future action", fields: [{ name: "text", label: "Message", type: "text" }] },
];

function installIndex() {
  installIndexMarkup();
  // The Settings view, as the section router shows it.
  byId("settings-tabs").closest("section").hidden = false;
  const dialog = byId("confirm-dialog");
  dialog.showModal = () => { dialog.open = true; };
  dialog.close = (value = "") => {
    dialog.returnValue = value;
    dialog.open = false;
    dialog.dispatchEvent(new Event("close"));
  };
}

class FakeError extends Error {
  constructor(message, { status = 400, field = null } = {}) {
    super(message);
    this.status = status;
    this.field = field;
  }
}

// A stand-in for the schedule routes, holding events the way the server does.
function fakeServer() {
  const server = { events: [], runs: [], calls: [], fail: null, runResult: { result: "ok", message: "" } };
  const view = (event) => ({
    ...event,
    repeat_text: event.repeat === "interval" ? `Every ${event.interval_minutes} minutes` : `Every day at ${event.time}`,
    action_text: `${ACTIONS.find((action) => action.name === event.action)?.description}: ${Object.values(event.params).join(", ")}`,
  });
  server.request = vi.fn(async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : undefined;
    server.calls.push({ method, url, body });
    if (server.fail) throw server.fail;
    if (method === "GET" && url === "/api/schedule") {
      return {
        timezone: "UTC",
        actions: ACTIONS,
        events: server.events.map(view),
        upcoming: server.events.filter((event) => event.enabled).map((event) => ({
          id: event.id, name: event.name, time: "2026-10-11T14:00:00+00:00",
          repeat_text: view(event).repeat_text, action_text: view(event).action_text,
        })),
        runs: server.runs,
      };
    }
    const id = decodeURIComponent(url.split("/")[3] || "");
    const index = server.events.findIndex((event) => event.id === id);
    if (method === "POST" && url === "/api/schedule") {
      const event = { ...body, id: `id-${server.events.length + 1}` };
      server.events.push(event);
      return view(event);
    }
    if (method === "PUT") {
      server.events[index] = { ...body, id };
      return view(server.events[index]);
    }
    if (method === "DELETE") return { deleted: server.events.splice(index, 1)[0].id };
    if (url.endsWith("/enable")) {
      server.events[index].enabled = body.enabled;
      return view(server.events[index]);
    }
    if (url.endsWith("/run")) {
      const run = { time: "2026-10-11T12:00:00+00:00", event: server.events[index].name, summary: "Switch profile: Morning", ...server.runResult };
      server.runs.unshift(run);
      return run;
    }
    throw new Error(`Unexpected ${method} ${url}`);
  });
  return server;
}

function event(name, changes = {}) {
  return {
    id: `id-${name}`, name, action: "profile", params: { name: "Morning" }, repeat: "daily",
    date: null, time: "07:00:00", days: [], interval_minutes: null, window_start: null, window_end: null,
    timing: "soft", enabled: true, ...changes,
  };
}

async function openPanel(server) {
  const controls = installSchedulerPanel({ doc: document, request: server.request, now: () => NOW });
  const subtabs = initSubtabs(byId("settings-tabs"), { storage: null });
  byId("stab-schedule").click();
  await vi.waitFor(() => expect(server.request).toHaveBeenCalled());
  await vi.waitFor(() => expect(byId("schedule-timezone").textContent).toBe("UTC"));
  return { controls, subtabs };
}

const rowButton = (name, role) => [...byId("schedule-events").children]
  .find((row) => row.querySelector("span").textContent.startsWith(`${name}:`))
  ?.querySelector(`[data-role="${role}"]`);

function submit() {
  byId("schedule-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}

beforeEach(() => {
  installIndex();
});

describe("Schedule panel", () => {
  it("is a Settings subtab whose panel is labelled by its tab", () => {
    const tab = byId("stab-schedule");
    expect(tab.getAttribute("role")).toBe("tab");
    expect(tab.hasAttribute("aria-controls")).toBe(false);
    expect(byId("spanel-schedule").getAttribute("aria-labelledby")).toBe("stab-schedule");
    expect(byId("spanel-schedule").querySelector(":scope > h3").textContent).toBe("Schedule");
  });

  it("labels every control of the form", () => {
    const form = byId("schedule-form");
    for (const control of form.querySelectorAll("input, select")) {
      expect(form.querySelector(`label[for="${control.id}"]`), control.id).not.toBeNull();
    }
    expect(form.querySelector("fieldset > legend").textContent).toBe("Add event");
    expect(byId("schedule-days").querySelector("legend").textContent).toBe("Days");
  });

  it("lists events, next events and recent runs as plain text with named buttons", async () => {
    const server = fakeServer();
    server.events.push(event("Morning show"), event("Overnight", { enabled: false, timing: "hard" }));
    server.runs.push({ time: "2026-10-11T07:00:00+00:00", event: "Morning show", summary: "Switch profile: Morning", result: "error", message: "Profile Morning was not found" });
    await openPanel(server);
    const rows = [...byId("schedule-events").children];
    expect(rows.map((row) => row.querySelector("span").textContent)).toEqual([
      "Morning show: Every day at 07:00:00. Switch profile: Morning. Soft timing.",
      "Overnight: Every day at 07:00:00. Switch profile: Morning. Hard timing. Disabled.",
    ]);
    expect([...rows[0].querySelectorAll("button")].map((button) => [button.textContent, button.getAttribute("aria-label")])).toEqual([
      ["Edit", "Edit Morning show"],
      ["Disable", "Disable Morning show"],
      ["Run now", "Run now, Morning show"],
      ["Delete", "Delete Morning show"],
    ]);
    expect(rows[1].querySelector('[data-role="toggle"]').getAttribute("aria-label")).toBe("Enable Overnight");
    const next = byId("schedule-upcoming").textContent;
    expect(next).toContain("Morning show. Switch profile: Morning.");
    expect(next).toContain("in 2 hours");
    const clock = new Intl.DateTimeFormat(undefined, { hourCycle: "h23", hour: "numeric", minute: "2-digit", timeZone: "UTC" });
    expect(next).toContain(clock.format(new Date("2026-10-11T14:00:00Z")));
    expect(byId("schedule-runs").textContent).toContain("Morning show. Switch profile: Morning. Failed: Profile Morning was not found.");
    expect(byId("schedule-upcoming").getAttribute("aria-live")).toBeNull();
  });

  it("says when there is nothing to list", async () => {
    await openPanel(fakeServer());
    expect(byId("schedule-events").textContent).toBe("No events yet.");
    expect(byId("schedule-upcoming").textContent).toBe("Nothing is scheduled.");
    expect(byId("schedule-runs").textContent).toBe("No events have run yet.");
  });

  it("adds an event from the server's action list and focuses its Edit button", async () => {
    const server = fakeServer();
    await openPanel(server);
    byId("schedule-add").click();
    expect(byId("schedule-form").hidden).toBe(false);
    expect(document.activeElement).toBe(byId("schedule-name"));
    expect([...byId("schedule-action").options].map((item) => item.textContent)).toEqual(["Switch profile", "Future action"]);
    const profile = byId("schedule-param-name");
    expect(document.querySelector('label[for="schedule-param-name"]').textContent).toBe("Profile");
    expect([...profile.options].map((item) => item.value)).toEqual(["Morning", "Night"]);

    byId("schedule-name").value = "Morning show";
    profile.value = "Night";
    byId("schedule-time").value = "07:00";
    byId("schedule-timing").value = "hard";
    submit();
    await vi.waitFor(() => expect(status()).toBe("Event Morning show saved"));
    expect(server.calls.find((call) => call.method === "POST").body).toEqual({
      name: "Morning show", action: "profile", params: { name: "Night" }, repeat: "daily",
      time: "07:00", timing: "hard", wait_seconds: 0, enabled: true,
    });
    expect(byId("schedule-form").hidden).toBe(true);
    expect(document.activeElement).toBe(rowButton("Morning show", "edit"));
  });

  it("shows only the fields the repeat choice uses, keeping focus on Repeat", async () => {
    await openPanel(fakeServer());
    byId("schedule-add").click();
    const repeat = byId("schedule-repeat");
    const visible = () => ["date", "time", "days", "interval"].filter((id) => !byId(`schedule-${id}`).closest("[data-repeat]").hidden);
    repeat.focus();
    for (const [value, fields] of [["once", ["date", "time"]], ["daily", ["time"]], ["weekdays", ["time"]], ["days", ["time", "days"]], ["interval", ["days", "interval"]]]) {
      repeat.value = value;
      repeat.dispatchEvent(new Event("change", { bubbles: true }));
      expect(visible()).toEqual(fields);
      expect(byId("schedule-range").hidden).toBe(value === "once");
      expect(document.activeElement).toBe(repeat);
    }
  });

  it("keeps a one-time event's saved date range in view, to keep or clear", async () => {
    await openPanel(fakeServer());
    byId("schedule-add").click();
    const repeat = byId("schedule-repeat");
    byId("schedule-start-date").value = "2026-11-27";
    repeat.value = "once";
    repeat.dispatchEvent(new Event("change", { bubbles: true }));
    expect(byId("schedule-range").hidden).toBe(false);
    byId("schedule-start-date").value = "";
    repeat.value = "daily";
    repeat.dispatchEvent(new Event("change", { bubbles: true }));
    repeat.value = "once";
    repeat.dispatchEvent(new Event("change", { bubbles: true }));
    expect(byId("schedule-range").hidden).toBe(true);
  });

  it("builds an action's fields again when the action changes", async () => {
    await openPanel(fakeServer());
    byId("schedule-add").click();
    byId("schedule-action").value = "note";
    byId("schedule-action").dispatchEvent(new Event("change", { bubbles: true }));
    expect(byId("schedule-param-name")).toBeNull();
    const text = byId("schedule-param-text");
    expect(text.type).toBe("text");
    expect(document.querySelector('label[for="schedule-param-text"]').textContent).toBe("Message");
  });

  it("names a missing field, marks it invalid and moves focus to it", async () => {
    const server = fakeServer();
    await openPanel(server);
    byId("schedule-add").click();
    byId("schedule-save").focus();
    submit();
    await vi.waitFor(() => expect(status()).toBe("Enter a name"));
    const name = byId("schedule-name");
    expect(document.activeElement).toBe(name);
    expect(name.getAttribute("aria-invalid")).toBe("true");
    expect(byId("schedule-form-error").textContent).toBe("Enter a name");
    expect(byId("schedule-form-error").hidden).toBe(false);
    name.value = "Weekend";
    name.dispatchEvent(new Event("input", { bubbles: true }));
    expect(name.hasAttribute("aria-invalid")).toBe(false);

    byId("schedule-repeat").value = "days";
    byId("schedule-repeat").dispatchEvent(new Event("change", { bubbles: true }));
    byId("schedule-time").value = "09:00";
    submit();
    await vi.waitFor(() => expect(status()).toBe("Choose at least one day"));
    expect(document.activeElement).toBe(byId("schedule-day-0"));
    expect([...byId("schedule-days").querySelectorAll("input")].every((box) => box.getAttribute("aria-invalid") === "true")).toBe(true);

    byId("schedule-repeat").value = "interval";
    byId("schedule-repeat").dispatchEvent(new Event("change", { bubbles: true }));
    byId("schedule-window-start").value = "22:00";
    submit();
    await vi.waitFor(() => expect(status()).toBe("Enter both Active from and Active until, or leave both empty"));
    expect(document.activeElement).toBe(byId("schedule-window-end"));
    expect(byId("schedule-day-0").hasAttribute("aria-invalid")).toBe(false);
    expect(server.calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("saves a date range and chosen days, and fills them in again on Edit", async () => {
    const server = fakeServer();
    await openPanel(server);
    byId("schedule-add").click();
    byId("schedule-name").value = "Friday nights";
    const repeat = byId("schedule-repeat");
    repeat.value = "interval";
    repeat.dispatchEvent(new Event("change", { bubbles: true }));
    expect(byId("schedule-days").getAttribute("aria-describedby")).toBe("schedule-days-desc");
    byId("schedule-interval").value = "60";
    byId("schedule-window-start").value = "18:00";
    byId("schedule-window-end").value = "02:00";
    byId("schedule-day-4").checked = true;
    byId("schedule-start-date").value = "2026-11-27";
    byId("schedule-end-date").value = "2026-12-26";
    submit();
    await vi.waitFor(() => expect(status()).toBe("Event Friday nights saved"));
    expect(server.calls.find((call) => call.method === "POST").body).toMatchObject({
      repeat: "interval", interval_minutes: 60, window_start: "18:00", window_end: "02:00",
      days: [4], start_date: "2026-11-27", end_date: "2026-12-26",
    });

    rowButton("Friday nights", "edit").click();
    expect(byId("schedule-start-date").value).toBe("2026-11-27");
    expect(byId("schedule-end-date").value).toBe("2026-12-26");
    expect(byId("schedule-day-4").checked).toBe(true);
    repeat.value = "days";
    repeat.dispatchEvent(new Event("change", { bubbles: true }));
    expect(byId("schedule-days").hasAttribute("aria-describedby")).toBe(false);
    expect(byId("schedule-days-desc").hidden).toBe(true);
  });

  it("saves a hard event's wait, checks it in words and lists it", async () => {
    const server = fakeServer();
    await openPanel(server);
    byId("schedule-add").click();
    byId("schedule-name").value = "News";
    byId("schedule-time").value = "13:00";
    const wait = byId("schedule-wait");
    expect(wait.value).toBe("0");
    expect(wait.getAttribute("aria-describedby")).toBe("schedule-wait-desc");
    expect(wait.closest(".settings-row").dataset.showWhen).toBe("schedule-timing=hard");
    byId("schedule-timing").value = "hard";
    wait.value = "301";
    submit();
    const message = "Wait up to seconds must be a whole number from 0 to 300";
    await vi.waitFor(() => expect(status()).toBe(message));
    expect(document.activeElement).toBe(wait);
    expect(wait.getAttribute("aria-invalid")).toBe("true");
    expect(wait.getAttribute("aria-describedby")).toBe("schedule-wait-desc schedule-form-error");
    expect(byId("schedule-form-error").textContent).toBe(message);
    expect(server.calls.some((call) => call.method === "POST")).toBe(false);
    wait.value = "45";
    wait.dispatchEvent(new Event("input", { bubbles: true }));
    expect(wait.getAttribute("aria-describedby")).toBe("schedule-wait-desc");
    submit();
    await vi.waitFor(() => expect(status()).toBe("Event News saved"));
    expect(server.calls.find((call) => call.method === "POST").body)
      .toMatchObject({ timing: "hard", wait_seconds: 45 });
    expect(rowButton("News", "edit").closest("li").textContent).toContain("Hard timing, waits up to 45 seconds.");

    // Soft timing never waits, whatever the hidden field still holds.
    rowButton("News", "edit").click();
    expect(byId("schedule-wait").value).toBe("45");
    byId("schedule-timing").value = "soft";
    submit();
    await vi.waitFor(() => expect(server.calls.some((call) => call.method === "PUT")).toBe(true));
    expect(server.calls.find((call) => call.method === "PUT").body.wait_seconds).toBe(0);
  });

  it("names a backwards date range and links the error to the field", async () => {
    const server = fakeServer();
    await openPanel(server);
    byId("schedule-add").click();
    byId("schedule-name").value = "Holiday music";
    byId("schedule-time").value = "00:00";
    byId("schedule-start-date").value = "2026-12-26";
    byId("schedule-end-date").value = "2026-11-27";
    submit();
    await vi.waitFor(() => expect(status()).toBe("End date must be on or after the start date"));
    const end = byId("schedule-end-date");
    expect(document.activeElement).toBe(end);
    expect(end.getAttribute("aria-invalid")).toBe("true");
    expect(end.getAttribute("aria-describedby")).toBe("schedule-form-error");
    expect(byId("schedule-form-error").textContent).toBe("End date must be on or after the start date");
    end.value = "2026-12-31";
    end.dispatchEvent(new Event("input", { bubbles: true }));
    expect(end.hasAttribute("aria-invalid")).toBe(false);
    expect(end.hasAttribute("aria-describedby")).toBe(false);

    byId("schedule-repeat").value = "once";
    byId("schedule-repeat").dispatchEvent(new Event("change", { bubbles: true }));
    byId("schedule-date").value = "2027-01-02";
    submit();
    await vi.waitFor(() => expect(status()).toBe("Date must be within the start and end dates"));
    expect(document.activeElement).toBe(byId("schedule-date"));
    expect(byId("schedule-date").getAttribute("aria-describedby")).toBe("schedule-form-error");
    expect(server.calls.some((call) => call.method === "POST")).toBe(false);

    byId("schedule-date").value = "2026-12-30";
    server.fail = new FakeError("The event has no run left before its end date", { field: "end_date" });
    submit();
    await vi.waitFor(() => expect(status()).toBe("The event has no run left before its end date"));
    expect(document.activeElement).toBe(end);
    expect(byId("schedule-date").hasAttribute("aria-describedby")).toBe(false);
  });

  it("says when an event has ended, and dates the next events", async () => {
    const server = fakeServer();
    server.events.push(event("Season", { end_date: "2026-01-01", enabled: false, ended: true }), event("Morning show"));
    const reply = server.request.getMockImplementation();
    server.request.mockImplementation(async (url, options) => {
      const data = await reply(url, options);
      if (url !== "/api/schedule" || options?.method) return data;
      const later = { ...data.upcoming[0], time: "2027-01-04T07:00:00+00:00" };
      return { ...data, upcoming: [...data.upcoming, later] };
    });
    await openPanel(server);
    expect(rowButton("Season", "edit").closest("li").querySelector("span").textContent)
      .toMatch(/Disabled\. Ended, no runs left\.$/);
    const [today, later] = [...byId("schedule-upcoming").children].map((item) => item.textContent);
    expect(today).toContain("Sunday");
    expect(today).toContain("11");
    expect(today).toContain("October");
    expect(today).not.toContain("2026");
    expect(later).toContain("Monday");
    expect(later).toContain("January");
    expect(later).toContain("2027");
  });

  it("reads this year in the station time zone", async () => {
    const server = fakeServer();
    const reply = server.request.getMockImplementation();
    // 08:00 UTC on 31 December is 22:00 that day at UTC+14, and 12:00 UTC
    // is already 1 January there.
    server.request.mockImplementation(async (url, options) => {
      const data = await reply(url, options);
      if (url !== "/api/schedule" || options?.method) return data;
      return {
        ...data, timezone: "Pacific/Kiritimati",
        upcoming: [{ ...data.upcoming[0], time: "2026-12-31T12:00:00+00:00" }],
      };
    });
    installSchedulerPanel({ doc: document, request: server.request, now: () => Date.parse("2026-12-31T08:00:00Z") });
    initSubtabs(byId("settings-tabs"), { storage: null });
    byId("stab-schedule").click();
    await vi.waitFor(() => expect(byId("schedule-timezone").textContent).toBe("Pacific/Kiritimati"));
    const [first] = [...byId("schedule-upcoming").children].map((item) => item.textContent);
    expect(first).toContain("January");
    expect(first).toContain("2027");
  });

  it("says when an action has nothing to choose yet", async () => {
    const server = fakeServer();
    const empty = { ...ACTIONS[0], fields: [{ ...ACTIONS[0].fields[0], options: [] }] };
    const reply = server.request.getMockImplementation();
    server.request.mockImplementation(async (url, options) => {
      const data = await reply(url, options);
      return url === "/api/schedule" && !options?.method ? { ...data, actions: [empty] } : data;
    });
    await openPanel(server);
    byId("schedule-add").click();
    byId("schedule-name").value = "Morning";
    submit();
    await vi.waitFor(() => expect(status()).toBe("No profile to choose yet"));
    expect(document.activeElement).toBe(byId("schedule-param-name"));
  });

  it("focuses the field the server names in its refusal", async () => {
    const server = fakeServer();
    await openPanel(server);
    byId("schedule-add").click();
    byId("schedule-name").value = "Night";
    byId("schedule-time").value = "23:00";
    server.fail = new FakeError("Profile Night was not found", { field: "params.name" });
    submit();
    await vi.waitFor(() => expect(status()).toBe("Profile Night was not found"));
    expect(document.activeElement).toBe(byId("schedule-param-name"));
    expect(byId("schedule-param-name").getAttribute("aria-invalid")).toBe("true");

    server.fail = new FakeError("The AutoDJ server is not reachable.", { status: null });
    submit();
    await vi.waitFor(() => expect(status()).toBe("Could not save: The AutoDJ server is not reachable."));
    expect(byId("schedule-form").hidden).toBe(false);
  });

  it("edits an event in place and Cancel returns focus to its Edit button", async () => {
    const server = fakeServer();
    server.events.push(event("Morning show", { params: { name: "Gone" } }));
    await openPanel(server);
    const edit = rowButton("Morning show", "edit");
    edit.click();
    expect(byId("schedule-form-legend").textContent).toBe("Edit Morning show");
    expect(byId("schedule-name").value).toBe("Morning show");
    expect(byId("schedule-time").value).toBe("07:00");
    expect(byId("schedule-param-name").selectedOptions[0].textContent).toBe("Gone (not found)");
    byId("schedule-cancel").click();
    expect(byId("schedule-form").hidden).toBe(true);
    expect(document.activeElement).toBe(edit);

    edit.click();
    byId("schedule-param-name").value = "Night";
    byId("schedule-name").value = "Breakfast";
    submit();
    await vi.waitFor(() => expect(status()).toBe("Event Breakfast saved"));
    const put = server.calls.find((call) => call.method === "PUT");
    expect(put.url).toBe("/api/schedule/id-Morning%20show");
    expect(document.activeElement).toBe(edit);
    expect(edit.getAttribute("aria-label")).toBe("Edit Breakfast");
  });

  it("disables and enables an event without moving focus", async () => {
    const server = fakeServer();
    server.events.push(event("Morning show"));
    await openPanel(server);
    const toggle = rowButton("Morning show", "toggle");
    toggle.focus();
    toggle.click();
    await vi.waitFor(() => expect(status()).toBe("Event Morning show disabled"));
    expect(server.calls.at(-2).body).toEqual({ enabled: false });
    expect(document.activeElement).toBe(toggle);
    expect(toggle.textContent).toBe("Enable");
    expect(toggle.getAttribute("aria-label")).toBe("Enable Morning show");
  });

  it("runs an event now and says how it went", async () => {
    const server = fakeServer();
    server.events.push(event("Morning show"));
    await openPanel(server);
    rowButton("Morning show", "run").click();
    await vi.waitFor(() => expect(status()).toBe("Event Morning show ran"));
    server.runResult = { result: "error", message: "Stream control needs stream mode" };
    rowButton("Morning show", "run").click();
    await vi.waitFor(() => expect(status()).toBe("Event Morning show failed: Stream control needs stream mode"));
    expect(byId("schedule-runs").children).toHaveLength(2);
  });

  it("asks before deleting, then focuses the next event", async () => {
    const server = fakeServer();
    server.events.push(event("First"), event("Second"));
    await openPanel(server);
    const remove = rowButton("First", "delete");
    remove.click();
    expect(byId("confirm-dialog").open).toBe(true);
    expect(byId("confirm-title").textContent).toBe("Delete event First?");
    byId("confirm-cancel").click();
    await vi.waitFor(() => expect(document.activeElement).toBe(remove));
    expect(server.events).toHaveLength(2);

    remove.click();
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Event First deleted"));
    expect(document.activeElement).toBe(rowButton("Second", "edit"));

    rowButton("Second", "delete").click();
    byId("confirm-accept").click();
    await vi.waitFor(() => expect(status()).toBe("Event Second deleted"));
    expect(document.activeElement).toBe(byId("schedule-add"));
  });

  it("keeps focus on a row's button when another page changes the schedule", async () => {
    const server = fakeServer();
    server.events.push(event("First"), event("Second"));
    const { controls } = await openPanel(server);
    const run = rowButton("Second", "run");
    run.focus();
    server.events.unshift(event("Zero"));
    server.events.find((item) => item.name === "Second").enabled = false;
    await controls.refresh();
    expect(document.activeElement).toBe(run);
    expect(run.closest("li").querySelector("span").textContent).toContain("Disabled.");
    expect([...byId("schedule-events").children].map((row) => row.dataset.eventId)).toEqual(["id-Zero", "id-First", "id-Second"]);
  });

  it("does not load while hidden, and forgets everything on sign-out", async () => {
    const server = fakeServer();
    server.events.push(event("Private show"));
    const { controls, subtabs } = await openPanel(server);
    expect(byId("schedule-events").textContent).toContain("Private show");
    byId("stab-sound").click();
    server.request.mockClear();
    await controls.refresh();
    expect(server.request).not.toHaveBeenCalled();
    expect(subtabs.selected).toBe("sound");

    byId("stab-schedule").click();
    let release;
    server.request.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    const pending = controls.refresh();
    controls.clear();
    release({ timezone: "UTC", actions: ACTIONS, events: [event("Private show")], upcoming: [], runs: [] });
    await pending;
    expect(byId("schedule-events").textContent).toBe("");
    expect(byId("schedule-timezone").textContent).toBe("");
  });

  it("says when the schedule cannot load", async () => {
    const server = fakeServer();
    server.fail = new FakeError("The AutoDJ server is not reachable.", { status: null });
    installSchedulerPanel({ doc: document, request: server.request, now: () => NOW });
    initSubtabs(byId("settings-tabs"), { storage: null });
    byId("stab-schedule").click();
    await vi.waitFor(() => expect(status()).toBe("Could not load the schedule: The AutoDJ server is not reachable."));
  });
});
