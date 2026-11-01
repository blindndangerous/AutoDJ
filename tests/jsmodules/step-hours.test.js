import { beforeEach, expect, it } from "vitest";
import { fillStepHours, installStepHours, readStepHours } from "../../src/autodj/static/modules/step-hours.js";

beforeEach(() => {
  document.body.innerHTML = '<input id="clock-step-hours-from" type="time" aria-describedby="time-help">'
    + '<input id="clock-step-hours-until" type="time" aria-describedby="time-help">'
    + '<p id="time-help">Station time</p><p id="clock-step-hours-error" hidden></p>';
});

it("preserves time-field help when marking and clearing a refused window", () => {
  const from = document.getElementById("clock-step-hours-from");
  const until = document.getElementById("clock-step-hours-until");
  installStepHours(document);
  fillStepHours(document);
  expect(from.getAttribute("aria-describedby")).toBe("time-help");
  from.value = "22:00";
  expect(() => readStepHours(document)).toThrow("Enter both Allowed from and Allowed until");
  expect(until.getAttribute("aria-describedby")).toBe("clock-step-hours-error time-help");
  expect(document.activeElement).toBe(until);
  until.value = "02:00";
  until.dispatchEvent(new Event("input"));
  expect(until.getAttribute("aria-describedby")).toBe("time-help");
  expect(until.hasAttribute("aria-invalid")).toBe(false);
  expect(readStepHours(document)).toEqual({ hours_from: "22:00", hours_until: "02:00" });
});
