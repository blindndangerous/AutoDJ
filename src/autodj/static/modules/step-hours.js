// A clockwheel step's days and hours ("When this step plays"), for the
// clockwheel editor (clockwheel-panel.js).  Outside them the server skips
// the step, as it skips an empty one, and the clockwheel moves on.
//
// The fields are native checkboxes and time inputs with visible labels.  A
// field the page refuses is marked invalid, linked to the error text under
// the fields and focused; the editor says the error through its status.

const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const DAY_IDS = DAY_NAMES.map((_, day) => `clock-step-day-${day}`);
const ERROR_ID = "clock-step-hours-error";

function controls(doc) {
  return {
    days: DAY_IDS.map((id) => doc.getElementById(id)).filter(Boolean),
    from: doc.getElementById("clock-step-hours-from"),
    until: doc.getElementById("clock-step-hours-until"),
    error: doc.getElementById(ERROR_ID),
  };
}

// A step's days and hours in words, as the server says a category's, or ""
// for a step that plays at any time: "only on Friday from 22:00 until 02:00".
export function stepHoursText(step) {
  const days = [...new Set(step?.days || [])].sort();
  const hours = step?.hours_from && step?.hours_until;
  if (!days.length && !hours) return "";
  let when;
  if (days.join() === "0,1,2,3,4") when = "on weekdays";
  else if (days.join() === "5,6") when = "at weekends";
  else if (days.length && days.length < 7) {
    const names = days.map((day) => DAY_NAMES[day]);
    when = `on ${names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`}`;
  } else when = "every day";
  if (!hours) return when === "every day" ? "" : `only ${when}`;
  return `only ${when} from ${step.hours_from.slice(0, 5)} until ${step.hours_until.slice(0, 5)}`;
}

function unmark(field) {
  field?.removeAttribute("aria-invalid");
  field?.removeAttribute("aria-describedby");
}

function clearStepHoursError(doc) {
  const { days, from, until, error } = controls(doc);
  for (const field of [...days, from, until]) unmark(field);
  if (error) { error.textContent = ""; error.hidden = true; }
}

// Fills the fields from *step*, or clears them for a new step.
export function fillStepHours(doc, step = null) {
  const { days, from, until } = controls(doc);
  for (const box of days) box.checked = Boolean(step?.days?.includes(Number(box.value)));
  if (from) from.value = step?.hours_from?.slice(0, 5) || "";
  if (until) until.value = step?.hours_until?.slice(0, 5) || "";
  clearStepHoursError(doc);
}

function refuse(doc, fields, message) {
  const { error } = controls(doc);
  for (const field of fields) {
    field.setAttribute("aria-invalid", "true");
    if (error) field.setAttribute("aria-describedby", ERROR_ID);
  }
  if (error) { error.textContent = message; error.hidden = false; }
  fields[0]?.focus();
  throw new Error(message);
}

// The days and hours to save with the step, only those set.  Throws an
// Error that names the field, after marking it, when they cannot be saved.
export function readStepHours(doc) {
  const { days, from, until } = controls(doc);
  clearStepHoursError(doc);
  const value = {};
  const chosen = days.filter((box) => box.checked).map((box) => Number(box.value));
  if (chosen.length) value.days = chosen;
  // A half-typed time reads as empty; say so rather than save "all day".
  for (const [field, label] of [[from, "Allowed from"], [until, "Allowed until"]]) {
    if (field?.validity?.badInput) {
      refuse(doc, [field], `${label} is not a whole time. Enter hours and minutes, such as 22:00, or clear it.`);
    }
  }
  const start = from?.value || "";
  const end = until?.value || "";
  if (Boolean(start) !== Boolean(end)) {
    refuse(doc, [start ? until : from], "Enter both Allowed from and Allowed until, or leave both empty.");
  }
  if (start && start === end) refuse(doc, [until], "Allowed until must differ from Allowed from.");
  if (start) Object.assign(value, { hours_from: start, hours_until: end });
  return value;
}

// Clears a field's error mark once it is changed; the days as one group.
export function installStepHours(doc) {
  const { days, from, until } = controls(doc);
  for (const field of [from, until]) field?.addEventListener("input", () => unmark(field));
  for (const box of days) box.addEventListener("input", () => days.forEach(unmark));
}
