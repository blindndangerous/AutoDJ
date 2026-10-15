import { NUMERIC_OPERATORS, TEXT_OPERATORS } from "./clockwheel-step.js";

const NUMERIC_FIELDS = ["year", "bpm", "duration"];
const BEETS_NUMERIC_OPERATORS = new Set(["eq", "not_eq", "lt", "lte", "gt", "gte", "between", "outside"]);

function optionsWithMissing(options, selected, label = selected) {
  const result = [...options];
  if (selected && !result.some(([value]) => value === selected)) result.unshift([selected, `${label} (unavailable)`]);
  return result;
}

function optionList(doc, select, choices) {
  select.replaceChildren(...choices.map(([value, text]) => {
    const option = doc.createElement("option");
    option.value = value;
    option.textContent = text;
    return option;
  }));
}

/** Build the common metadata-condition row; callers provide their available fields and choices. */
export function createConditionRow({
  document: doc,
  nextId,
  index,
  value = {},
  fields,
  sourceChoices = [],
  genreChoices = [],
  beetsChoices = [],
  categoryChoices = null,
  onFieldChange = () => {},
  onRemove = () => {},
}) {
  const row = doc.createElement("fieldset");
  row.className = "clock-condition";
  const legend = doc.createElement("legend");
  legend.textContent = `Condition ${index}`;
  row.append(legend);

  const control = (labelText, tag = "input", choices = []) => {
    const id = nextId();
    const label = doc.createElement("label");
    const node = doc.createElement(tag);
    label.htmlFor = id;
    label.textContent = labelText;
    node.id = id;
    if (tag === "select") optionList(doc, node, choices);
    row.append(label, node);
    return node;
  };

  const field = control("Condition type", "select", fields.map((item) => [item, item === "bpm" ? "BPM" : item[0].toUpperCase() + item.slice(1)]));
  const source = control("Library folder", "select", optionsWithMissing(sourceChoices, value.source));
  const folder = control("Subfolder");
  folder.type = "text";
  const recursive = control("Include subfolders");
  recursive.type = "checkbox";
  const operator = control("Operator", "select");
  const input = control("Value");
  input.type = "text";
  const upper = control("Upper value");
  upper.type = "text";
  const genre = control("Genre", "select", optionsWithMissing(genreChoices, value.field === "genre" ? value.value : ""));
  const name = control("Beets field", "select", optionsWithMissing(beetsChoices, value.name));
  let category = null;
  if (categoryChoices !== null) category = control("Music category", "select", optionsWithMissing(categoryChoices, value.category));

  field.value = value.field || (source.options.length ? "folder" : "title");
  source.value = value.source || source.options[0]?.value || "";
  folder.value = value.folder || "";
  recursive.checked = Boolean(value.recursive);
  input.value = value.value ?? "";
  upper.value = value.upper ?? "";
  genre.value = value.field === "genre" ? value.value || "" : "";
  name.value = value.name || name.options[0]?.value || "";
  if (category) category.value = value.category || "";
  let selectedOperator = value.operator || "";

  const sync = () => {
    const current = field.value;
    const isFolder = current === "folder";
    const isGenre = current === "genre";
    const isCategory = current === "category";
    const isBeets = current === "beets";
    const numeric = NUMERIC_FIELDS.includes(current);
    for (const node of [source, folder, recursive]) node.hidden = node.previousElementSibling.hidden = !isFolder;
    genre.hidden = genre.previousElementSibling.hidden = !isGenre;
    name.hidden = name.previousElementSibling.hidden = !isBeets;
    if (category) category.hidden = category.previousElementSibling.hidden = !isCategory;

    let choices = TEXT_OPERATORS;
    if (isFolder || isGenre || isCategory) choices = [["is", "is"], ["is_not", "is not"]];
    else if (numeric) choices = NUMERIC_OPERATORS;
    else if (isBeets) choices = [["is", "Is (text)"], ["is_not", "Is not (text)"], ...TEXT_OPERATORS.slice(2), ["eq", "Equals (number)"], ...NUMERIC_OPERATORS.slice(1)];
    const previous = operator.value || selectedOperator;
    optionList(doc, operator, choices);
    operator.value = choices.some(([item]) => item === previous) ? previous : choices[0][0];
    selectedOperator = operator.value;
    input.hidden = input.previousElementSibling.hidden = isFolder || isGenre || isCategory;
    upper.hidden = upper.previousElementSibling.hidden = !["between", "outside"].includes(operator.value);
    input.previousElementSibling.textContent = current === "duration" ? "Value (seconds or MM:SS)" : "Value";
    upper.previousElementSibling.textContent = current === "duration" ? "Upper value (seconds or MM:SS)" : "Upper value";
  };
  field.addEventListener("change", () => { sync(); onFieldChange(); });
  operator.addEventListener("change", sync);
  const remove = doc.createElement("button");
  remove.type = "button";
  remove.dataset.removeCondition = "";
  remove.textContent = `Remove condition ${index}`;
  row.append(remove);
  remove.addEventListener("click", () => onRemove(row));
  sync();
  return { row, field, source, folder, recursive, operator, input, upper, genre, name, category };
}

/**
 * Number the condition rows in *host* from 1, before focus moves into one of
 * them.  A row whose number changes gets a new legend element, not new text
 * in the old one: Chrome did not always pass a legend's new text on to its
 * group's name before the focus that followed, so NVDA read the group by its
 * old number ("Condition 3 grouping" after Remove on condition 2, #326).  A
 * new child of the group makes its name be worked out again.
 */
export function renumberConditionRows(host) {
  [...host.children].forEach((row, at) => {
    const text = `Condition ${at + 1}`;
    const legend = row.querySelector("legend");
    if (legend && legend.textContent !== text) {
      const fresh = legend.cloneNode(false);
      fresh.textContent = text;
      legend.replaceWith(fresh);
    }
    const remove = row.querySelector("button[data-remove-condition]");
    if (remove) remove.textContent = `Remove condition ${at + 1}`;
  });
}

/** Read and validate the common row format into the condition objects sent to the API. */
export function readConditionRows(rows, {
  interactive = true,
  parsePointTime,
  folderRequired = "Choose a library folder.",
  durationError = "Enter a duration in seconds or minutes:seconds.",
  numberError = (field) => `Enter a number for ${field}.`,
  negativeError = (field) => numberError(field),
  upperRequired = "Enter the upper value.",
  beetsError = () => "Choose an available Beets field.",
  beetsAvailable = () => true,
  errorField,
} = {}) {
  const invalid = (control, message) => {
    if (interactive) {
      control?.setAttribute("aria-invalid", "true");
      control?.focus();
    }
    const error = new Error(message);
    if (errorField) error.field = errorField;
    throw error;
  };
  for (const row of rows) row.querySelectorAll("[aria-invalid]").forEach((control) => control.removeAttribute("aria-invalid"));
  return rows.map((row) => {
    const selects = [...row.querySelectorAll("select")];
    const [fieldSelect, sourceSelect, operatorSelect, genreSelect, beetsSelect, categorySelect] = selects;
    const field = fieldSelect.value;
    const operator = operatorSelect.value;
    if (field === "folder") {
      const folderInput = row.querySelector('input[type="text"]');
      const folder = folderInput.value.trim().replaceAll("\\", "/").replace(/^[/\\]+|[/\\]+$/g, "");
      if (!sourceSelect.value) invalid(sourceSelect, folderRequired);
      if (folder && (folder.split("/").some((part) => !part || part === "." || part === "..") || folder.includes(":"))) {
        invalid(folderInput, 'The subfolder must stay inside its library folder, with no "..".');
      }
      return { field, source: sourceSelect.value, folder, recursive: row.querySelector('input[type="checkbox"]').checked, ...(operator === "is_not" ? { operator } : {}) };
    }
    if (field === "genre") {
      if (!genreSelect.value) invalid(genreSelect, "Choose a genre.");
      return { field, operator, value: genreSelect.value };
    }
    if (field === "category") {
      if (!categorySelect?.value) invalid(categorySelect, "Choose a saved music category.");
      return { field, category: categorySelect.value, ...(operator === "is_not" ? { operator } : {}) };
    }

    const inputs = [...row.querySelectorAll('input[type="text"]')];
    const raw = inputs[1].value.trim();
    if (!raw) invalid(inputs[1], `Enter a value for ${field}.`);
    const numeric = NUMERIC_FIELDS.includes(field) || (field === "beets" && BEETS_NUMERIC_OPERATORS.has(operator));
    const parse = (text, control) => {
      let value;
      try { value = field === "duration" ? parsePointTime(text) : Number(text); }
      catch { invalid(control, durationError); }
      if (!Number.isFinite(value)) invalid(control, numberError(field));
      if (field !== "beets" && value < 0) invalid(control, negativeError(field));
      return value;
    };
    const result = { field, ...(field === "beets" ? { name: beetsSelect.value } : {}), operator, value: numeric ? parse(raw, inputs[1]) : raw };
    if (field === "beets" && !beetsAvailable(result.name)) invalid(beetsSelect, beetsError(result.name));
    if (["between", "outside"].includes(operator)) {
      const high = inputs[2].value.trim();
      if (!high) invalid(inputs[2], upperRequired);
      result.upper = numeric ? parse(high, inputs[2]) : high;
    }
    return result;
  });
}
