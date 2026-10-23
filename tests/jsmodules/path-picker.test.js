import { beforeEach, expect, it, vi } from "vitest";
import { addBrowseButton, libraryFolderSource, serverFolderSource } from "../../src/autodj/static/modules/path-picker.js";
import { installIndexMarkup } from "./helpers.js";
import { createConditionRow } from "../../src/autodj/static/modules/condition-editor.js";

const $ = (id) => document.getElementById(id);
async function settle() {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}
const entryButtons = () => [...$("path-picker-list").querySelectorAll("button")];

const SERVER = {
  "": { path: "", parent: null, folders: [{ name: "C:\\", path: "C:\\" }, { name: "D:\\", path: "D:\\" }], files: [], truncated: false },
  "D:\\": { path: "D:\\", parent: "", folders: [{ name: "Music", path: "D:\\Music" }], files: [], truncated: false },
  "D:\\Music": { path: "D:\\Music", parent: "D:\\", folders: [{ name: "Rock", path: "D:\\Music\\Rock" }], files: [], truncated: false },
};

function serverField() {
  document.body.innerHTML = '<label for="folder">Folder path</label><input id="folder" type="text"><p id="after">after</p>';
  const input = $("folder");
  const request = vi.fn(async (url) => {
    const path = decodeURIComponent(url.split("path=")[1]);
    if (!(path in SERVER)) throw Object.assign(new Error("That folder does not exist on the AutoDJ machine"), { status: 404 });
    return SERVER[path];
  });
  const button = addBrowseButton(input, { source: serverFolderSource({ request }), label: "folder path" });
  return { input, button, request };
}

beforeEach(() => {
  document.body.innerHTML = "";
  document.getElementById("path-picker-dialog")?.remove();
});

it("puts a labelled Browse button after the field", () => {
  const { input, button } = serverField();
  expect(input.nextElementSibling).toBe(button);
  expect(button.type).toBe("button");
  expect(button.textContent).toBe("Browse…");
  expect(button.getAttribute("aria-label")).toBe("Browse for folder path");
});

it("browses server folders with the keyboard and fills the field", async () => {
  const { input, button } = serverField();
  const changed = vi.fn();
  input.addEventListener("change", changed);
  button.focus(); button.click(); await settle();
  const dialog = $("path-picker-dialog");
  expect(dialog.open).toBe(true);
  expect(dialog.getAttribute("aria-labelledby")).toBe("path-picker-title");
  expect($("path-picker-title").textContent).toBe("Choose folder path");
  expect($("path-picker-location").textContent).toBe("Folder: Drives");
  expect($("path-picker-select").getAttribute("aria-disabled")).toBe("true");
  expect($("path-picker-up").getAttribute("aria-disabled")).toBe("true");
  // One Tab stop: the first entry has focus and the rest are out of the tab order.
  expect(document.activeElement).toBe(entryButtons()[0]);
  expect(entryButtons().map((item) => item.tabIndex)).toEqual([0, -1]);
  entryButtons()[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  expect(document.activeElement).toBe(entryButtons()[1]);
  expect(entryButtons()[1].textContent).toBe("D:\\, folder");

  entryButtons()[1].click(); await settle();
  expect($("path-picker-location").textContent).toBe("Folder: D:\\");
  entryButtons()[0].click(); await settle();
  expect($("path-picker-location").textContent).toBe("Folder: D:\\Music");
  expect($("path-picker-status").textContent).toContain("D:\\Music: 1 folder.");
  expect($("path-picker-select").hasAttribute("aria-disabled")).toBe(false);

  $("path-picker-up").click(); await settle();
  expect($("path-picker-location").textContent).toBe("Folder: D:\\");
  // Up lands on the folder just left.
  expect(document.activeElement.dataset.path).toBe("D:\\Music");
  document.activeElement.click(); await settle();
  $("path-picker-select").click(); await settle();
  expect(dialog.open).toBe(false);
  expect(input.value).toBe("D:\\Music");
  expect(changed).toHaveBeenCalledOnce();
  expect(document.activeElement).toBe(button);
});

it("starts at a typed folder and falls back to the top for one that is missing", async () => {
  const { input, button, request } = serverField();
  input.value = "D:\\Music";
  button.click(); await settle();
  expect($("path-picker-location").textContent).toBe("Folder: D:\\Music");
  $("path-picker-cancel").click(); await settle();
  input.value = "Z:\\Gone";
  button.click(); await settle();
  expect(request).toHaveBeenLastCalledWith("/api/library/browse?path=");
  expect($("path-picker-location").textContent).toBe("Folder: Drives");
  expect($("path-picker-error").hidden).toBe(true);
});

it("leaves the field alone on Cancel and Escape and returns focus to Browse", async () => {
  const { input, button } = serverField();
  input.value = "typed";
  for (const close of [() => $("path-picker-cancel").click(),
    () => $("path-picker-dialog").dispatchEvent(new Event("cancel", { cancelable: true }))]) {
    button.click(); await settle();
    close(); await settle();
    expect($("path-picker-dialog").open).toBe(false);
    expect(input.value).toBe("typed");
    expect(document.activeElement).toBe(button);
  }
});

it("shows and announces a folder that cannot be read", async () => {
  document.body.innerHTML = '<input id="folder">';
  const request = vi.fn(async (url) => {
    if (url.endsWith("path=")) return SERVER[""];
    throw new Error("AutoDJ is not allowed to read that folder");
  });
  const button = addBrowseButton($("folder"), { source: serverFolderSource({ request }), label: "folder path" });
  button.click(); await settle();
  entryButtons()[0].click(); await settle();
  expect($("path-picker-error").hidden).toBe(false);
  expect($("path-picker-error").textContent).toBe("AutoDJ is not allowed to read that folder");
  expect($("path-picker-status").textContent).toContain("not allowed");
  expect($("path-picker-dialog").open).toBe(true);
});

it("picks a track file inside the chosen library folder", async () => {
  document.body.innerHTML = '<select id="source"><option value="lib1">/music</option></select><input id="track">';
  const request = vi.fn(async (url) => {
    expect(url.startsWith("/api/library/folders/lib1/browse?path=")).toBe(true);
    expect(url).toContain("&files=true");
    const path = decodeURIComponent(url.split("path=")[1].split("&")[0]);
    return path === "Album"
      ? { path: "Album", parent: "", folders: [], files: [{ name: "One.flac", path: "Album/One.flac" }, { name: "Two.flac", path: "Album/Two.flac" }], truncated: false }
      : { path: "", parent: null, folders: [{ name: "Album", path: "Album" }], files: [], truncated: false };
  });
  const input = $("track");
  input.value = "Album/Two.flac";
  const button = addBrowseButton(input, {
    source: libraryFolderSource(() => $("source").value, { files: true, request }), label: "track",
  });
  button.click(); await settle();
  expect($("path-picker-select").hidden).toBe(true);
  // Opens beside the current track, on it.
  expect($("path-picker-location").textContent).toBe("Folder: Album");
  expect(document.activeElement.dataset.path).toBe("Album/Two.flac");
  expect($("path-picker-status").textContent).toContain("0 folders, 2 audio files.");
  $("path-picker-up").click(); await settle();
  expect($("path-picker-location").textContent).toBe("Folder: Library folder");
  document.activeElement.click(); await settle();
  entryButtons()[0].click(); await settle();
  expect(input.value).toBe("Album/One.flac");
  expect(document.activeElement).toBe(button);
});

it("says to choose a library folder when none is chosen", async () => {
  document.body.innerHTML = '<input id="sub">';
  const button = addBrowseButton($("sub"), { source: libraryFolderSource(() => ""), label: "subfolder" });
  button.click(); await settle();
  expect($("path-picker-error").textContent).toBe("Choose a library folder first.");
  expect(document.activeElement).toBe($("path-picker-cancel"));
});

it("gives folder conditions a Browse button that hides with the subfolder", () => {
  installIndexMarkup();
  const { row, folder, field } = createConditionRow({ document, nextId: () => `c${Math.random()}`, index: 1, sourceChoices: [["lib1", "/music"]], value: { field: "folder" } });
  const browse = folder.nextElementSibling;
  expect(browse.getAttribute("aria-label")).toBe("Browse for subfolder");
  expect(browse.hidden).toBe(false);
  field.value = "title";
  field.dispatchEvent(new Event("change"));
  expect(browse.hidden).toBe(true);
  expect(row.contains(browse)).toBe(true);
});

it("adds Browse to the folder path and track step fields of the page", async () => {
  installIndexMarkup();
  const { installLibraryFolders } = await import("../../src/autodj/static/modules/library-folders.js");
  const { installClockwheelPanel } = await import("../../src/autodj/static/modules/clockwheel-panel.js");
  const request = vi.fn(async () => ({}));
  try { installLibraryFolders({ document, request }); } catch { /* wiring only */ }
  try { installClockwheelPanel({ document, request }); } catch { /* wiring only */ }
  expect($("source-folder-path").nextElementSibling.getAttribute("aria-label")).toBe("Browse for folder path");
  expect($("clock-step-path").nextElementSibling.getAttribute("aria-label")).toBe("Browse for track");
});
