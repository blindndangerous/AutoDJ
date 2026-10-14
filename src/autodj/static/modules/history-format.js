// History dates are headings; each play shows only its time.

function localDay(date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

export function updateHistoryDateHeadings(tbody, locale = undefined) {
  const headings = [...tbody.querySelectorAll(".history-date")];
  const used = new Set();
  let previousDay = null;
  for (const row of tbody.querySelectorAll("tr[data-played-at]")) {
    const played = new Date(row.dataset.playedAt);
    const valid = !Number.isNaN(played.getTime());
    const day = valid ? localDay(played) : "unknown";
    if (day === previousDay) continue;
    previousDay = day;
    let heading = headings.find((item) => item.dataset.day === day && !used.has(item));
    if (!heading) {
      heading = tbody.ownerDocument.createElement("tr");
      heading.className = "history-date";
      heading.dataset.day = day;
      const cell = tbody.ownerDocument.createElement("td");
      cell.colSpan = 5;
      const title = tbody.ownerDocument.createElement("h3");
      title.textContent = valid
        ? played.toLocaleDateString(locale, { day: "numeric", month: "short", year: "numeric" })
        : "Unknown date";
      cell.append(title);
      heading.append(cell);
    }
    used.add(heading);
    if (row.previousElementSibling !== heading) row.before(heading);
  }
  for (const heading of headings) if (!used.has(heading)) heading.remove();
}

export function formatPlayedAt(iso, withDate, locale = undefined) {
  const played = new Date(iso);
  if (Number.isNaN(played.getTime())) return String(iso);
  const time = { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return withDate
    ? played.toLocaleString(locale, { day: "numeric", month: "short", year: "numeric", ...time })
    : played.toLocaleTimeString(locale, time);
}
