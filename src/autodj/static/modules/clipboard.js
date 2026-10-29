// Plain http is not a secure context, so it has no clipboard API; the
// older copy command still works there through a one-off copy listener.
export function copyByCommand(text, doc = globalThis.document) {
  let wrote = false;
  const onCopy = (event) => {
    if (!event.clipboardData) return;
    event.clipboardData.setData("text/plain", text);
    event.preventDefault();
    wrote = true;
  };
  doc.addEventListener("copy", onCopy);
  try {
    return typeof doc.execCommand === "function" && doc.execCommand("copy") === true && wrote;
  } catch (_) {
    return false;
  } finally {
    doc.removeEventListener("copy", onCopy);
  }
}
