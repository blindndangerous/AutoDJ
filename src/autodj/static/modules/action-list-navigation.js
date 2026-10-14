// Keep each list of row actions to one Tab stop. Arrow keys move between
// actions and rows without changing the list's native semantics.
const controllers = new WeakMap();

const rowButtons = (row) => [...row.querySelectorAll(":scope > button:not(:disabled)")];
const allButtons = (list) => [...list.querySelectorAll(":scope > li > button")];
function matchingButton(row, source, column) {
  const actions = rowButtons(row);
  const named = actions.find((item) => item.dataset.action
    && item.dataset.action === source.dataset.action);
  const positional = [...row.querySelectorAll(":scope > button")][column];
  return named || (positional && !positional.disabled ? positional : null) || actions[0];
}

export function installActionListNavigation(list) {
  if (!list) return { refresh() {} };
  if (controllers.has(list)) return controllers.get(list);

  function refresh() {
    const buttons = allButtons(list);
    const active = list.ownerDocument.activeElement;
    const chosen = buttons.includes(active) && !active.disabled
      ? active : buttons.find((button) => !button.disabled);
    for (const button of buttons) button.tabIndex = button === chosen ? 0 : -1;
  }

  list.addEventListener("focusin", (event) => {
    if (allButtons(list).includes(event.target)) refresh();
  });
  list.addEventListener("keydown", (event) => {
    const button = event.target.closest?.("button");
    const row = button?.parentElement;
    if (row?.parentElement !== list || row.tagName !== "LI") return;
    const rowEdge = event.ctrlKey && !event.altKey && !event.metaKey && !event.shiftKey
      && (event.key === "Home" || event.key === "End");
    if ((event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) && !rowEdge) return;
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;

    const rows = [...list.children].filter((item) => rowButtons(item).length);
    const rowIndex = rows.indexOf(row);
    if (rowIndex < 0) return;
    const buttons = rowButtons(row);
    const actionIndex = buttons.indexOf(button);
    const column = [...row.querySelectorAll(":scope > button")].indexOf(button);
    let target = null;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      target = buttons[actionIndex + (event.key === "ArrowLeft" ? -1 : 1)];
    } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      const next = rows[rowIndex + (event.key === "ArrowUp" ? -1 : 1)];
      target = next && matchingButton(next, button, column);
    } else {
      const destination = rowEdge ? rows[event.key === "Home" ? 0 : rows.length - 1] : row;
      const edgeButtons = rowButtons(destination);
      target = rowEdge
        ? matchingButton(destination, button, column)
        : edgeButtons[event.key === "Home" ? 0 : edgeButtons.length - 1];
    }
    event.preventDefault();
    if (target && !target.disabled) target.focus();
  });

  const controller = { refresh };
  controllers.set(list, controller);
  refresh();
  return controller;
}
