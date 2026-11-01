import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

// Subtab panels dispatch toggle too. Ignore nested cards' events so opening
// a child cannot start a second refresh of its owning panel.
export function onCardOpen(card, refresh, { onClose = () => {} } = {}) {
  if (!card) return () => {};
  const toggle = (event) => {
    if (event.target !== card) return;
    if (card.open) void refresh();
    else onClose();
  };
  card.addEventListener("toggle", toggle);
  return () => card.removeEventListener("toggle", toggle);
}

// Card IDs keep browser preferences stable when cards move or headings change.
export function installCardState(doc = document) {
  const storage = defaultStorage();
  for (const card of doc.querySelectorAll("details.card[id]")) {
    const key = `autodj.card.${card.id}`;
    const saved = readStorage(storage, key);
    if (saved === "open" || saved === "closed") card.open = saved === "open";
    card.addEventListener("toggle", () => {
      writeStorage(storage, key, card.open ? "open" : "closed");
    });
  }
}
