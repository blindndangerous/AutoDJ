import { afterEach, describe, expect, it, vi } from "vitest";
import { installLibraries, libraryScopedRequest } from "../../src/autodj/static/modules/libraries.js";

afterEach(() => { document.body.replaceChildren(); });

describe("library routing", () => {
  it("targets explicit libraries but preserves station-owned library requests", async () => {
    const request = vi.fn(async () => ({}));
    const scoped = libraryScopedRequest(request, () => "oldies");

    await scoped("/api/library/folders");
    await scoped("/api/library/points?path=song.mp3&station=default");
    await scoped("/api/library/points", { method: "POST", body: JSON.stringify({ path: "song.mp3", station: "default" }) });
    await scoped("/api/library/clock");
    await scoped("/api/library/stats?library=main");

    expect(request.mock.calls.map(([url]) => url)).toEqual([
      "/api/library/folders?library=oldies",
      "/api/library/points?path=song.mp3&station=default",
      "/api/library/points",
      "/api/library/clock",
      "/api/library/stats?library=main",
    ]);
  });
});

describe("library management", () => {
  function libraryPage() {
    document.body.innerHTML = `
      <div id="spanel-libraries"><div id="libraries-list"></div><p id="libraries-status"></p>
        <button id="libraries-add">Add library</button></div>
      <select id="station-library"><option value="main">Main</option></select>
      <select id="library-tools-library"><option value="main">Main</option></select>`;
  }

  async function openRowAction(id, action) {
    const row = document.querySelector(`[data-row-key="${id}"]`);
    row.querySelector('[aria-haspopup="menu"]').click();
    row.querySelector(`[data-action="${action}"]`).click();
  }

  it("reports a failed refresh and preserves the loaded libraries", async () => {
    document.body.innerHTML = `
      <div id="spanel-libraries"><div id="libraries-list"></div><p id="libraries-status"></p>
        <button id="libraries-add">Add library</button></div>
      <select id="library-tools-library"><option value="main">Main</option></select>`;
    const records = [{ id: "main", name: "Main" }];
    const request = vi.fn().mockResolvedValueOnce({ libraries: records })
      .mockRejectedValueOnce(new Error("Connection lost"));
    const controls = installLibraries({ document, request });
    await controls.refresh();
    await expect(controls.refresh()).resolves.toEqual(records);
    await vi.waitFor(() => expect(document.getElementById("libraries-status").textContent)
      .toContain("Connection lost"));
    expect(document.getElementById("library-tools-library").value).toBe("main");
    expect(document.getElementById("libraries-list").textContent).toContain("Main");
  });

  it("opens a named dialog heading without generic instructions", async () => {
    document.body.innerHTML = `
      <div id="spanel-libraries"><div id="libraries-list"></div><p id="libraries-status"></p>
        <button id="libraries-add">Add library</button></div>`;
    const controls = installLibraries({ document, request: vi.fn(async () => ({ libraries: [] })) });
    await controls.refresh();
    document.getElementById("libraries-add").click();

    const dialog = document.getElementById("library-editor-form-dialog");
    const heading = dialog.querySelector("legend h1");
    expect(heading.textContent).toBe("Add library");
    expect(dialog.getAttribute("aria-labelledby")).toBe(heading.id);
    expect(dialog.hasAttribute("aria-describedby")).toBe(false);
  });

  it("loads named libraries into the table and selectors", async () => {
    document.body.innerHTML = `
      <div id="spanel-libraries"><div id="libraries-list"></div><p id="libraries-status"></p>
        <button id="libraries-add">Add library</button></div>
      <select id="station-library"><option value="main">Main</option></select>
      <select id="library-tools-library"><option value="main">Main</option></select>`;
    const request = vi.fn(async () => ({ libraries: [
      { id: "main", name: "Main" }, { id: "oldies", name: "Oldies" },
    ] }));
    const changed = vi.fn();
    const controls = installLibraries({ document, request, onLibraryChange: changed });

    await controls.refresh();
    expect(document.querySelector("#libraries-list").textContent).toContain("Oldies");
    expect([...document.getElementById("station-library").options].map((option) => option.value))
      .toEqual(["main", "oldies"]);
    expect([...document.getElementById("library-tools-library").options].map((option) => option.value))
      .toEqual(["main", "oldies"]);
    expect(controls.getSelectedLibrary()).toBe("main");
    expect(changed).not.toHaveBeenCalled();

    const toolSelect = document.getElementById("library-tools-library");
    toolSelect.value = "oldies";
    toolSelect.dispatchEvent(new Event("change"));
    expect(controls.getSelectedLibrary()).toBe("oldies");
    expect(changed).toHaveBeenCalledWith("oldies");
  });

  it("duplicates a library through the copy endpoint and focuses the new row", async () => {
    libraryPage();
    const original = { id: "jazz", name: "Jazz" };
    const records = [{ id: "main", name: "Main" }, original];
    const request = vi.fn(async (url, options) => {
      if (url === "/api/libraries" && !options) return { libraries: records };
      if (url === "/api/libraries/jazz/duplicate" && options?.method === "POST") {
        records.push(JSON.parse(options.body));
        return records.at(-1);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const controls = installLibraries({ document, request });
    await controls.refresh();
    await openRowAction("jazz", "duplicate");
    const name = document.getElementById("library-editor-name");
    const id = document.getElementById("library-editor-id");
    expect(name.value).toBe("Copy of Jazz");
    expect(id.value).toBe("copy-of-jazz");
    expect(document.activeElement).toBe(name);
    name.value = "Jazz archive";
    name.dispatchEvent(new Event("input", { bubbles: true }));
    expect(id.value).toBe("jazz-archive");
    document.getElementById("library-editor-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));

    await vi.waitFor(() => expect(document.querySelector('[data-row-key="jazz-archive"]')).not.toBeNull());
    expect(request).toHaveBeenCalledWith("/api/libraries/jazz/duplicate", expect.objectContaining({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: "jazz-archive", name: "Jazz archive" }),
    }));
    expect(records.find((record) => record.id === "jazz")).toEqual(original);
    expect([...document.getElementById("station-library").options].map((option) => option.value))
      .toContain("jazz-archive");
    expect(document.activeElement.closest("tr").dataset.rowKey).toBe("jazz-archive");
  });

  it("prefills the next available name and ID when copy names collide", async () => {
    libraryPage();
    const records = [
      { id: "library", name: "Library" },
      { id: "copy-of-library", name: "Copy of Library" },
      { id: "copy-of-library-2", name: "Copy of Library 2" },
    ];
    const request = vi.fn(async () => ({ libraries: records }));
    const controls = installLibraries({ document, request });
    await controls.refresh();
    await openRowAction("library", "duplicate");
    expect(document.getElementById("library-editor-name").value).toBe("Copy of Library 3");
    expect(document.getElementById("library-editor-id").value).toBe("copy-of-library-3");
    expect(document.getElementById("library-editor-name").maxLength).toBe(80);
    expect(document.getElementById("library-editor-id").maxLength).toBe(32);
  });

  it("cancels a duplicate without a request and returns focus to its source row", async () => {
    libraryPage();
    const request = vi.fn(async () => ({ libraries: [{ id: "main", name: "Main" }, { id: "jazz", name: "Jazz" }] }));
    const controls = installLibraries({ document, request });
    await controls.refresh();
    await openRowAction("jazz", "duplicate");
    const callsBeforeCancel = request.mock.calls.length;
    document.querySelector("#library-editor-form button[type=button]").click();
    expect(document.getElementById("library-editor-form-dialog").open).toBe(false);
    expect(request).toHaveBeenCalledTimes(callsBeforeCancel);
    expect(document.activeElement.closest("tr").dataset.rowKey).toBe("jazz");
  });

  it("keeps create and rename requests on their existing routes", async () => {
    libraryPage();
    const records = [{ id: "main", name: "Main" }];
    const request = vi.fn(async (url, options) => {
      if (url === "/api/libraries" && !options) return { libraries: records };
      if (url === "/api/libraries" && options.method === "POST") {
        records.push(JSON.parse(options.body));
        return records.at(-1);
      }
      if (url === "/api/libraries/late-night" && options.method === "PATCH") {
        records.find((item) => item.id === "late-night").name = JSON.parse(options.body).name;
        return {};
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const controls = installLibraries({ document, request });
    await controls.refresh();
    document.getElementById("libraries-add").click();
    const name = document.getElementById("library-editor-name");
    name.value = "Late Night";
    name.dispatchEvent(new Event("input", { bubbles: true }));
    document.getElementById("library-editor-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(document.querySelector('[data-row-key="late-night"]')).not.toBeNull());
    await openRowAction("late-night", "rename");
    const renamed = document.getElementById("library-editor-name");
    renamed.value = "Night Jazz";
    document.getElementById("library-editor-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await vi.waitFor(() => expect(records.find((item) => item.id === "late-night").name).toBe("Night Jazz"));
    expect(request).toHaveBeenCalledWith("/api/libraries", expect.objectContaining({ method: "POST" }));
    expect(request).toHaveBeenCalledWith("/api/libraries/late-night", expect.objectContaining({
      method: "PATCH", body: JSON.stringify({ name: "Night Jazz" }),
    }));
  });
});
