import { defaultStorage, readStorage, writeStorage } from "./safe-storage.js";

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
