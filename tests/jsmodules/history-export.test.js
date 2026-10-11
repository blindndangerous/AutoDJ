import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  exportFilename,
  exportMessage,
  installHistoryExport,
  localDate,
} from "../../src/autodj/static/modules/history-export.js";
import { requestFile } from "../../src/autodj/static/modules/api-client.js";

function mount() {
  document.body.innerHTML = `
    <form id="hist-export" novalidate>
      <label for="hist-export-from">From</label><input type="date" id="hist-export-from">
      <label for="hist-export-to">To</label><input type="date" id="hist-export-to">
      <label for="hist-export-format">Format</label>
      <select id="hist-export-format">
        <option value="csv">Play log</option><option value="soundexchange">SoundExchange</option>
      </select>
      <fieldset id="hist-export-soundexchange" hidden>
        <label for="hist-export-service">Name of service</label><input id="hist-export-service">
        <label for="hist-export-category">Category</label>
        <select id="hist-export-category"><option value="A">A</option><option value="B">B</option></select>
      </fieldset>
      <button type="submit" id="hist-export-button">Export</button>
    </form>`;
}

function csvResponse(body, headers = {}) {
  return new globalThis.Response(body, {
    headers: { "Content-Type": "text/csv; charset=utf-8", ...headers },
  });
}

const submit = () => document.getElementById("hist-export-button").click();
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("history export control", () => {
  let announce;
  let save;
  let request;

  beforeEach(() => {
    mount();
    announce = vi.fn();
    save = vi.fn();
    request = vi.fn();
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  let storage;
  const install = () => installHistoryExport({
    document, request, save, announce, storage, now: () => new Date(2026, 8, 17, 15, 30),
  });
  beforeEach(() => {
    const values = new Map();
    storage = {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
    };
  });

  it("defaults to this month so far, as local dates", () => {
    install();
    expect(document.getElementById("hist-export-from").value).toBe("2026-09-01");
    expect(document.getElementById("hist-export-to").value).toBe("2026-09-17");
    expect(localDate(new Date(2026, 0, 5))).toBe("2026-01-05");
  });

  it("keeps dates already chosen and does nothing without the form", () => {
    document.getElementById("hist-export-from").value = "2026-01-02";
    install();
    expect(document.getElementById("hist-export-from").value).toBe("2026-01-02");
    document.body.innerHTML = "";
    expect(() => install()).not.toThrow();
  });

  it("downloads the CSV and announces the count once", async () => {
    request.mockResolvedValue(csvResponse("start\r\n", {
      "Content-Disposition": 'attachment; filename="autodj-plays-2026-09-01-to-2026-09-17.csv"',
      "X-Play-Count": "12",
      "X-Songs-Missing-Fields": "3",
    }));
    install();
    submit();
    await settle();
    expect(request).toHaveBeenCalledWith(
      "/api/history/export?from=2026-09-01&to=2026-09-17", ["text/csv", "text/plain"],
    );
    expect(save).toHaveBeenCalledTimes(1);
    const [, blob, name] = save.mock.calls[0];
    expect(await blob.text()).toBe("start\r\n");
    expect(name).toBe("autodj-plays-2026-09-01-to-2026-09-17.csv");
    expect(announce.mock.calls).toEqual([
      ["Exporting play log", "info"],
      ["Play log saved: 12 plays.  3 songs lack ISRC, label, listener count or other reporting fields.", "info"],
    ]);
  });

  it("is busy while exporting and ready again afterwards", async () => {
    let finish;
    request.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    install();
    const form = document.getElementById("hist-export");
    const button = document.getElementById("hist-export-button");
    button.focus();
    submit();
    expect(button.disabled).toBe(true);
    expect(form.getAttribute("aria-busy")).toBe("true");
    expect(announce.mock.calls).toEqual([["Exporting play log", "info"]]);
    button.blur(); // what a browser does to a focused button it disables
    finish(csvResponse("start\r\n", { "X-Play-Count": "1" }));
    await settle();
    expect(button.disabled).toBe(false);
    expect(form.hasAttribute("aria-busy")).toBe(false);
    expect(document.activeElement).toBe(button);
    expect(announce).toHaveBeenCalledTimes(2);
  });

  it("leaves focus alone when it was not on the button", async () => {
    request.mockRejectedValue(new Error("offline"));
    install();
    const to = document.getElementById("hist-export-to");
    to.focus();
    document.getElementById("hist-export").requestSubmit();
    await settle();
    expect(document.activeElement).toBe(to);
    expect(document.getElementById("hist-export-button").disabled).toBe(false);
  });

  it("ignores a second press while the first export runs", async () => {
    let finish;
    request.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    install();
    submit();
    submit();
    finish(csvResponse("start\r\n", { "X-Play-Count": "1" }));
    await settle();
    expect(request).toHaveBeenCalledTimes(1);
    expect(announce).toHaveBeenCalledWith("Play log saved: 1 play.", "info");
  });

  it("announces a failed request once", async () => {
    request.mockRejectedValue(new Error("The AutoDJ server is not reachable."));
    install();
    submit();
    await settle();
    expect(save).not.toHaveBeenCalled();
    expect(announce.mock.calls).toEqual([
      ["Exporting play log", "info"],
      ["Could not export the play log: The AutoDJ server is not reachable.", "error"],
    ]);
    expect(document.getElementById("hist-export-button").disabled).toBe(false);
    expect(document.getElementById("hist-export").hasAttribute("aria-busy")).toBe(false);
  });

  it("checks the dates before asking the server", () => {
    install();
    const from = document.getElementById("hist-export-from");
    const to = document.getElementById("hist-export-to");
    to.value = "";
    submit();
    expect(announce).toHaveBeenLastCalledWith("Choose both a From date and a To date.", "error");
    expect(document.activeElement).toBe(to);
    to.value = "2026-08-01";
    submit();
    expect(announce).toHaveBeenLastCalledWith("The From date is after the To date.", "error");
    expect(document.activeElement).toBe(from);
    from.value = "";
    submit();
    expect(document.activeElement).toBe(from);
    expect(request).not.toHaveBeenCalled();
  });

  it("asks for a SoundExchange report with the service name and category", async () => {
    request.mockResolvedValue(new globalThis.Response("^A^|^A^", {
      headers: {
        "Content-Type": "text/plain; charset=us-ascii",
        "Content-Disposition": 'attachment; filename="MyStation20260901-20260917.txt"',
        "X-Play-Count": "2",
      },
    }));
    install();
    const format = document.getElementById("hist-export-format");
    const fields = document.getElementById("hist-export-soundexchange");
    const service = document.getElementById("hist-export-service");
    expect(fields.hidden).toBe(true);
    format.value = "soundexchange";
    format.dispatchEvent(new Event("change"));
    expect(fields.hidden).toBe(false);
    expect(announce.mock.calls).toEqual([["SoundExchange fields shown", "info"]]);
    format.dispatchEvent(new Event("change")); // already shown: nothing new to say
    expect(announce).toHaveBeenCalledTimes(1);
    submit();
    expect(announce).toHaveBeenLastCalledWith(
      "Enter the name of the service for the SoundExchange report.", "error",
    );
    expect(document.activeElement).toBe(service);
    expect(request).not.toHaveBeenCalled();
    service.value = " My Station ";
    document.getElementById("hist-export-category").value = "B";
    submit();
    await settle();
    expect(request).toHaveBeenCalledWith(
      "/api/history/export?from=2026-09-01&to=2026-09-17&format=soundexchange&service=My+Station&category=B",
      ["text/csv", "text/plain"],
    );
    expect(save.mock.calls[0][2]).toBe("MyStation20260901-20260917.txt");
    expect(announce).toHaveBeenLastCalledWith("SoundExchange report saved: 2 plays.", "info");
    mount();
    install();
    expect(document.getElementById("hist-export-service").value).toBe("My Station");
  });

  it("works when the browser refuses storage", () => {
    const refusing = {
      getItem: () => { throw new Error("denied"); },
      setItem: () => { throw new Error("denied"); },
    };
    installHistoryExport({ document, request, save, announce, storage: refusing });
    const format = document.getElementById("hist-export-format");
    format.value = "soundexchange";
    document.getElementById("hist-export-service").value = "Station";
    expect(document.getElementById("hist-export-service").value).toBe("Station");
    expect(() => submit()).not.toThrow();
  });

  it("saves through a temporary link by default", async () => {
    const click = vi.spyOn(globalThis.HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    vi.useFakeTimers();
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:play-log");
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    installHistoryExport({
      document, request: vi.fn().mockResolvedValue(csvResponse("x")), announce, storage: null,
    });
    submit();
    await vi.waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(click.mock.contexts[0].download).toBe("autodj-plays.csv");
    expect(click.mock.contexts[0].href).toBe("blob:play-log");
    expect(document.querySelector("a")).toBeNull();
    expect(revoke).not.toHaveBeenCalled();
    vi.advanceTimersByTime(10000);
    expect(revoke).toHaveBeenCalledWith("blob:play-log");
    vi.useRealTimers();
    for (const spy of [click, create, revoke]) spy.mockRestore();
  });
});

describe("export wording", () => {
  it("reads the file name only from a quoted, plain filename", () => {
    expect(exportFilename('attachment; filename="a.csv"')).toBe("a.csv");
    expect(exportFilename('attachment; filename="../a.csv"')).toBe("autodj-plays.csv");
    expect(exportFilename(null)).toBe("autodj-plays.csv");
  });

  it("says when the range had no plays", () => {
    expect(exportMessage("0", "0")).toBe(
      "Play log saved.  No plays in those dates, so the file is empty apart from any column names.",
    );
    expect(exportMessage("2", "1", "SoundExchange report")).toBe(
      "SoundExchange report saved: 2 plays.  1 song lacks ISRC, label, listener count or other reporting fields.",
    );
  });

  it("warns about songs SoundExchange cannot match", () => {
    expect(exportMessage("3", "2", "Play log", "1")).toBe(
      "Play log saved: 3 plays.  2 songs lack ISRC, label, listener count or other reporting fields."
      + "  Warning: 1 song has neither an ISRC nor both an album and a label,"
      + " so SoundExchange may not be able to match it.",
    );
    expect(exportMessage("3", "2", "Play log", "2")).toMatch(/2 songs have .* match them\.$/);
  });

  it("announces the SoundExchange fields each time they appear", () => {
    mount();
    const announce = vi.fn();
    installHistoryExport({ document, request: vi.fn(), save: vi.fn(), announce, storage: null });
    const format = document.getElementById("hist-export-format");
    for (const value of ["soundexchange", "csv", "soundexchange"]) {
      format.value = value;
      format.dispatchEvent(new Event("change"));
    }
    expect(announce.mock.calls).toEqual([
      ["SoundExchange fields shown", "info"],
      ["SoundExchange fields shown", "info"],
    ]);
  });
});

describe("requestFile", () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it("returns the response for an accepted type and rejects others", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(csvResponse("a,b\r\n"))
      .mockResolvedValueOnce(new globalThis.Response("<html>", {
        headers: { "Content-Type": "text/html" },
      }))
      .mockResolvedValueOnce(new globalThis.Response(JSON.stringify({ detail: "Bad date" }), {
        status: 422, headers: { "Content-Type": "application/json" },
      })));
    const response = await requestFile("/a.csv", ["text/csv"]);
    expect(await response.text()).toBe("a,b\r\n");
    await expect(requestFile("/b.csv", ["text/csv"])).rejects.toThrow("cannot read");
    await expect(requestFile("/c.csv", ["text/csv"])).rejects.toThrow("Bad date");
  });
});
