import { describe, expect, it } from "vitest";
import { updateHistoryDateHeadings } from "../../src/autodj/static/modules/history-format.js";

function row(playedAt) {
  const element = document.createElement("tr");
  element.dataset.playedAt = playedAt;
  return element;
}

describe("history date headings", () => {
  it("groups by local day across months and years", () => {
    const body = document.createElement("tbody");
    body.append(
      row(new Date(2027, 0, 1, 0, 2).toISOString()),
      row(new Date(2027, 0, 1, 0, 1).toISOString()),
      row(new Date(2026, 11, 31, 23, 59).toISOString()),
    );
    updateHistoryDateHeadings(body, "en-GB");
    expect([...body.querySelectorAll("h3")].map((heading) => heading.textContent))
      .toEqual(["1 Jan 2027", "31 Dec 2026"]);
    expect(body.querySelectorAll(".history-date td[colspan='4']")).toHaveLength(2);
  });

  it("reuses a day's heading on prepend and removes it when its last play is trimmed", () => {
    const body = document.createElement("tbody");
    const older = row(new Date(2026, 9, 13, 23, 59).toISOString());
    const newer = row(new Date(2026, 9, 14, 0, 1).toISOString());
    body.append(newer, older);
    updateHistoryDateHeadings(body);
    const heading = body.firstElementChild;
    const freshest = row(new Date(2026, 9, 14, 0, 2).toISOString());
    body.prepend(freshest);
    older.remove();
    updateHistoryDateHeadings(body);
    expect([...body.children]).toEqual([heading, freshest, newer]);
    freshest.remove();
    newer.remove();
    updateHistoryDateHeadings(body);
    expect(body.children).toHaveLength(0);
  });

  it("does not invent a date for an invalid timestamp", () => {
    const body = document.createElement("tbody");
    body.append(row("invalid"));
    updateHistoryDateHeadings(body);
    expect(body.querySelector("h3").textContent).toBe("Unknown date");
  });
});
