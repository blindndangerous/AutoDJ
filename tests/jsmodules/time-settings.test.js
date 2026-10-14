import { beforeEach, describe, expect, it, vi } from "vitest";
import { clockText, setTimeFormat } from "../../src/autodj/static/modules/time-format.js";
import { installTimeSettings } from "../../src/autodj/static/modules/time-settings.js";

const when = new Date(2026, 9, 14, 15, 4, 30);

describe("time format", () => {
  beforeEach(() => setTimeFormat("24"));

  it("writes 24-hour times unless set to 12 hour", () => {
    expect(clockText(when, { locale: "en-US" })).toBe("15:04");
    setTimeFormat("12");
    expect(clockText(when, { locale: "en-US", seconds: true })).toBe("3:04:30 PM");
  });

  it("saves the time settings and says when the zone waits for a restart", async () => {
    document.body.innerHTML = `
      <div id="spanel-stations"><div id="stations-status"></div>
      <select id="time-zone"></select>
      <select id="time-format"><option value="24">24</option><option value="12">12</option></select>
      <button id="time-settings-save"></button></div>`;
    const view = (timezone, format) => ({
      timezone, running_timezone: "UTC", time_format: format, timezones: ["Pacific/Auckland", "UTC"],
    });
    const request = vi.fn(async (_url, options) => (options
      ? view(JSON.parse(options.body).timezone, JSON.parse(options.body).time_format)
      : view("UTC", "24")));
    const settings = installTimeSettings({ request });
    await settings.load();
    expect(document.getElementById("time-zone").options[0].textContent).toBe("Pacific/Auckland");
    document.getElementById("time-zone").value = "Pacific/Auckland";
    document.getElementById("time-format").value = "12";
    document.getElementById("time-settings-save").click();
    await vi.waitFor(() => expect(document.getElementById("stations-status").textContent)
      .toBe("Time settings saved. The new time zone applies when AutoDJ restarts."));
    expect(clockText(when, { locale: "en-US" })).toBe("3:04 PM");
  });
});
