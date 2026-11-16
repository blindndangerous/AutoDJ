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
});
