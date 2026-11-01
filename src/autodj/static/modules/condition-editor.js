import { addBrowseButton, libraryFolderSource } from "./path-picker.js";

export const NUMERIC_OPERATORS = [["eq", "is"], ["not_eq", "is not"], ["lt", "less than"], ["lte", "at most"], ["gt", "greater than"], ["gte", "at least"], ["between", "between"], ["outside", "outside range"]];
export const TEXT_OPERATORS = [["is", "is"], ["is_not", "is not"], ["contains", "contains"], ["not_contains", "does not contain"], ["matches", "matches"], ["not_matches", "does not match"]];
/** The relative date operator: a Beets date, such as added or mtime, in the last N days. */
export const WITHIN_DAYS = "within_days";
const MAX_WITHIN_DAYS = 3650;
const DATE_OPERATORS = [[WITHIN_DAYS, "within the last N days (date)"]];
const WITHIN_DAYS_HELP = "A Beets date, such as added or mtime, in the last N days; checked every hour.";
const BEETS_NUMERIC_OPERATORS = new Set(NUMERIC_OPERATORS.map(([value]) => value));
const CONDITION_FIELD_DEFINITIONS = {
  folder: { label: "Folder", valueType: "folder", operators: "folder", help: "Choose a library folder; optionally limit it to a subfolder." },
  year: { label: "Year", valueType: "number", operators: "numeric", help: "Match the track year." },
  bpm: { label: "BPM", valueType: "number", operators: "numeric", help: "Match beats per minute." },
  duration: { label: "Duration", valueType: "duration", operators: "numeric", help: "Enter seconds or minutes:seconds." },
  artist: { label: "Artist", valueType: "text", operators: "text", help: "Match the artist name." },
  title: { label: "Title", valueType: "text", operators: "text", help: "Match the track title." },
  album: { label: "Album", valueType: "text", operators: "text", help: "Match the album name." },
  genre: { label: "Genre", valueType: "genre", operators: "folder", help: "Choose a saved genre." },
  beets: { label: "Beets", valueType: "beets", operators: "beets", help: "Choose a Beets field, then compare its value." },
  category: { label: "Category", valueType: "category", operators: "folder", help: "Choose a saved music category." },
};
export const CONDITION_FIELDS = Object.keys(CONDITION_FIELD_DEFINITIONS);
const ROW_CONTROLS = new WeakMap();

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
  allowCategory = false,
  sourceChoices = [],
  genreChoices = [],
  beetsChoices = [],
  categoryChoices = null,
  onAddCategory = null,
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
    node.dataset.conditionControl = labelText.toLowerCase().replaceAll(" ", "-");
    label.htmlFor = id;
    label.textContent = labelText;
    node.id = id;
    if (tag === "select") optionList(doc, node, choices);
    row.append(label, node);
    return node;
  };

  const fields = allowCategory ? CONDITION_FIELDS : CONDITION_FIELDS.filter((item) => item !== "category");
  const field = control("Condition type", "select", fields.map((item) => [item, CONDITION_FIELD_DEFINITIONS[item].label]));
  const help = doc.createElement("p");
  help.className = "setting-desc condition-help";
  row.append(help);
  const source = control("Library folder", "select", optionsWithMissing(sourceChoices, value.source));
  const folder = control("Subfolder");
  folder.type = "text";
  const browse = addBrowseButton(folder, { source: libraryFolderSource(() => source.value), label: "subfolder" });
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
  let addCategory = null;
  if (category && onAddCategory) {
    addCategory = doc.createElement("button");
    addCategory.type = "button";
    addCategory.textContent = "Add category";
    addCategory.dataset.addCategory = "";
    row.append(addCategory);
    addCategory.addEventListener("click", () => onAddCategory({ row, category, button: addCategory }));
  }

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
    help.textContent = CONDITION_FIELD_DEFINITIONS[current]?.help || "";
    const isFolder = current === "folder";
    const isGenre = current === "genre";
    const isCategory = current === "category";
    const isBeets = current === "beets";
    for (const node of [source, folder, recursive]) node.hidden = node.previousElementSibling.hidden = !isFolder;
    browse.hidden = !isFolder;
    genre.hidden = genre.previousElementSibling.hidden = !isGenre;
    name.hidden = name.previousElementSibling.hidden = !isBeets;
    if (category) category.hidden = category.previousElementSibling.hidden = !isCategory;
    if (addCategory) addCategory.hidden = !isCategory;

    let choices = TEXT_OPERATORS;
    const family = CONDITION_FIELD_DEFINITIONS[current]?.operators;
    if (family === "folder") choices = [["is", "is"], ["is_not", "is not"]];
    else if (family === "numeric") choices = NUMERIC_OPERATORS;
    else if (family === "beets") choices = [["is", "Is (text)"], ["is_not", "Is not (text)"], ...TEXT_OPERATORS.slice(2), ["eq", "Equals (number)"], ...NUMERIC_OPERATORS.slice(1), ...DATE_OPERATORS];
    const previous = operator.value || selectedOperator;
    optionList(doc, operator, choices);
    operator.value = choices.some(([item]) => item === previous) ? previous : choices[0][0];
    selectedOperator = operator.value;
    input.hidden = input.previousElementSibling.hidden = isFolder || isGenre || isCategory;
    upper.hidden = upper.previousElementSibling.hidden = !["between", "outside"].includes(operator.value);
    const withinDays = operator.value === WITHIN_DAYS;
    if (withinDays) help.textContent = WITHIN_DAYS_HELP;
    if (withinDays) input.setAttribute("inputmode", "numeric");
    else input.removeAttribute("inputmode");
    input.previousElementSibling.textContent = current === "duration" ? "Value (seconds or MM:SS)"
      : withinDays ? `Days (1 to ${MAX_WITHIN_DAYS})` : "Value";
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
  ROW_CONTROLS.set(row, { field, source, folder, recursive, operator, input, upper, genre, name, category });
  sync();
  return { row, field, source, folder, recursive, operator, input, upper, genre, name, category, help };
}

export function updateConditionCategories(host, choices, selectedControl, selectedId) {
  for (const row of host.children) {
    const category = ROW_CONTROLS.get(row)?.category;
    if (!category) continue;
    const value = category === selectedControl ? selectedId : category.value;
    optionList(host.ownerDocument, category, optionsWithMissing(choices, value));
    category.value = value;
  }
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
    const error = new Error(message);
    error.control = control;
    if (errorField) error.field = errorField;
    throw error;
  };
  return rows.map((row) => {
    const controls = ROW_CONTROLS.get(row);
    const { field: fieldSelect, source: sourceSelect, operator: operatorSelect, genre: genreSelect, name: beetsSelect, category: categorySelect } = controls;
    const field = fieldSelect.value;
    const operator = operatorSelect.value;
    if (field === "folder") {
      const folderInput = controls.folder;
      const folder = folderInput.value.trim().replaceAll("\\", "/").replace(/^[/\\]+|[/\\]+$/g, "");
      if (!sourceSelect.value) invalid(sourceSelect, folderRequired);
      if (folder && (folder.split("/").some((part) => !part || part === "." || part === "..") || folder.includes(":"))) {
        invalid(folderInput, 'The subfolder must stay inside its library folder, with no "..".');
      }
      return { field, source: sourceSelect.value, folder, recursive: controls.recursive.checked, ...(operator === "is_not" ? { operator } : {}) };
    }
    if (field === "genre") {
      if (!genreSelect.value) invalid(genreSelect, "Choose a genre.");
      return { field, operator, value: genreSelect.value };
    }
    if (field === "category") {
      if (!categorySelect?.value) invalid(categorySelect, "Choose a saved music category.");
      return { field, category: categorySelect.value, ...(operator === "is_not" ? { operator } : {}) };
    }

    const raw = controls.input.value.trim();
    if (!raw) invalid(controls.input, `Enter a value for ${field}.`);
    const definition = CONDITION_FIELD_DEFINITIONS[field];
    const numeric = definition?.valueType === "number" || definition?.valueType === "duration"
      || (definition?.valueType === "beets" && BEETS_NUMERIC_OPERATORS.has(operator));
    const parse = (text, control) => {
      let value;
      try { value = definition?.valueType === "duration" ? parsePointTime(text) : Number(text); }
      catch { invalid(control, durationError); }
      if (!Number.isFinite(value)) invalid(control, numberError(field));
      if (field !== "beets" && value < 0) invalid(control, negativeError(field));
      return value;
    };
    if (operator === WITHIN_DAYS) {
      const days = Number(raw);
      if (!/^\d+$/.test(raw) || days < 1 || days > MAX_WITHIN_DAYS) {
        invalid(controls.input, `Enter a whole number of days from 1 to ${MAX_WITHIN_DAYS}.`);
      }
      const result = { field, name: beetsSelect.value, operator, value: days };
      if (!beetsAvailable(result.name)) invalid(beetsSelect, beetsError(result.name));
      return result;
    }
    const result = { field, ...(field === "beets" ? { name: beetsSelect.value } : {}), operator, value: numeric ? parse(raw, controls.input) : raw };
    if (field === "beets" && !beetsAvailable(result.name)) invalid(beetsSelect, beetsError(result.name));
    if (["between", "outside"].includes(operator)) {
      const high = controls.upper.value.trim();
      if (!high) invalid(controls.upper, upperRequired);
      result.upper = numeric ? parse(high, controls.upper) : high;
    }
    return result;
  });
}
