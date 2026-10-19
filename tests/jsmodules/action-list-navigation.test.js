import { describe, expect, it } from "vitest";
import { installActionListNavigation } from "../../src/autodj/static/modules/action-list-navigation.js";

function key(target, name, ctrlKey = false) {
  const event = new KeyboardEvent("keydown", { key: name, ctrlKey, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

describe("editable action list navigation", () => {
  it("keeps one tab stop, moves by row/action and jumps to list ends", () => {
    document.body.innerHTML = `<ul><li><span>One</span><button data-action="edit">Edit</button><button data-action="delete">Delete</button></li>
      <li><span>Two</span><button data-action="edit">Edit</button><button data-action="delete">Delete</button></li></ul>`;
    const list = document.querySelector("ul");
    installActionListNavigation(list);
    const [firstEdit, firstDelete, lastEdit, lastDelete] = list.querySelectorAll("button");
    firstEdit.focus();
    key(firstEdit, "ArrowRight");
    expect(document.activeElement).toBe(firstDelete);
    key(firstDelete, "ArrowDown");
    expect(document.activeElement).toBe(lastDelete);
    key(lastDelete, "Home", true);
    expect(document.activeElement).toBe(firstDelete);
    key(firstDelete, "End", true);
    expect(document.activeElement).toBe(lastDelete);
    key(lastDelete, "Home");
    expect(document.activeElement).toBe(lastEdit);
    expect([...list.querySelectorAll("button")].filter((button) => button.tabIndex === 0)).toEqual([lastEdit]);
  });

  it("moves focus off an action that becomes disabled and leaves editing keys alone", () => {
    document.body.innerHTML = `<ul><li><button data-action="edit">Edit</button><button data-action="run">Run</button></li></ul>`;
    const list = document.querySelector("ul");
    const navigation = installActionListNavigation(list);
    const [edit, run] = list.querySelectorAll("button");
    run.focus();
    run.disabled = true;
    navigation.refresh();
    expect(document.activeElement).toBe(edit);
    const typing = key(edit, "ArrowDown", true);
    expect(typing.defaultPrevented).toBe(false);
  });
});
