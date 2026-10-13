import { stepHoursText } from "./step-hours.js";

const FOLDER_TYPES = new Set(["music", "liner"]);
export const NUMERIC_OPERATORS = [["eq", "is"], ["not_eq", "is not"], ["lt", "less than"], ["lte", "at most"], ["gt", "greater than"], ["gte", "at least"], ["between", "between"], ["outside", "outside range"]];
export const TEXT_OPERATORS = [["is", "is"], ["is_not", "is not"], ["contains", "contains"], ["not_contains", "does not contain"], ["matches", "matches"], ["not_matches", "does not match"]];

function lastPart(path) {
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || String(path);
}

function selectionExtras(step) {
  const extras = [];
  extras.push(step.selection === "random" ? "at random" : "least recently played");
  if (step.enforce_rules === false) extras.push("ignoring repeat rules");
  return extras;
}

function beetsFieldName(name) {
  return name === "original_year" ? "first release year" : String(name ?? "").replaceAll("_", " ");
}

function describeCondition(condition, sources = [], categories = []) {
  if (condition.field === "folder") {
    const source = sources.find((item) => item.id === condition.source);
    const folder = source ? condition.folder || lastPart(source.path)
      : `${condition.source || "A folder"} (no longer registered)`;
    const described = `${folder}${condition.recursive ? " (with subfolders)" : ""}`;
    return condition.operator === "is_not" ? `folder is not ${described}` : described;
  }
  if (condition.field === "category") {
    const category = categories.find((item) => item.id === condition.category);
    const described = category?.name || "that no longer exists";
    return condition.operator === "is_not" ? `category is not ${described}` : `category ${described}`;
  }
  const field = condition.field === "beets" ? `Beets ${beetsFieldName(condition.name)}`
    : condition.field === "duration" ? "Duration (seconds)"
      : condition.field === "bpm" ? "BPM"
        : condition.field[0].toUpperCase() + condition.field.slice(1);
  const operator = [...NUMERIC_OPERATORS, ...TEXT_OPERATORS].find(([value]) => value === condition.operator)?.[1] || condition.operator;
  return `${field} ${operator} ${condition.value}${["between", "outside"].includes(condition.operator) ? ` and ${condition.upper}` : ""}`;
}

export function describeConditions(conditions = [], match = "all", sources = [], categories = []) {
  return conditions.map((condition) => describeCondition(condition, sources, categories))
    .join(match === "any" ? " or " : " and ");
}

// Shared by the saved-step list and the Why this track explanation.
export function describeStep(step, sources = [], categories = []) {
  return [describeSelection(step, sources, categories), stepHoursText(step)].filter(Boolean).join(", ");
}

function describeSelection(step, sources, categories) {
  if (step.type === "category") {
    const category = categories.find((item) => item.id === step.category);
    const where = category ? `category ${category.name}` : "a category that no longer exists";
    return [`music from ${where}`, ...selectionExtras(step)].join(", ");
  }
  if (FOLDER_TYPES.has(step.type)) {
    if (!step.conditions) {
      const source = sources.find((item) => item.id === step.source);
      const where = step.folder || (source ? lastPart(source.path) : "a folder that is no longer registered");
      const extras = [];
      if (step.recursive) extras.push("with subfolders");
      extras.push(step.selection === "random" ? "at random" : "least recently played");
      if (step.enforce_rules === false) extras.push("ignoring repeat rules");
      if (step.type === "liner" && step.require_following) extras.push("only if the following track fits before the next reset");
      return [`${step.type} from ${where}`, ...extras].join(", ");
    }
    const where = describeConditions(step.conditions, step.match, sources, categories);
    const extras = [];
    extras.push(step.selection === "random" ? "at random" : "least recently played");
    if (step.enforce_rules === false) extras.push("ignoring repeat rules");
    if (step.type === "liner" && step.require_following) extras.push("only if the following track fits before the next reset");
    return [`${step.type} from ${where}`, ...extras].join(", ");
  }
  if (step.type === "track") return `track ${step.path}`;
  if (step.type === "settings") return `switch to preset ${step.preset}`;
  return `${String(step.type).replaceAll("_", " ")} step`;
}
