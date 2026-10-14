const MIN = -10;
const MAX = 10;

function displaySpeed(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "0%";
  const rounded = Math.round(numeric * 100) / 100;
  return `${rounded > 0 ? "+" : ""}${rounded}%`;
}

// Whether the slider is in use: focused, or held by the pointer (WebKit does
// not focus a range input it drags), so a state push must not move it.
export function musicSpeedSliderBusy(slider) {
  if (!slider) return false;
  if (slider.ownerDocument?.activeElement === slider) return true;
  try { return slider.matches(":active"); } catch (_) { return false; }
}

export function syncMusicSpeedSlider(number, slider, output) {
  if (!number || !slider) return;
  slider.value = number.value || "0";
  if (output) output.value = displaySpeed(slider.value);
}

export function installMusicSpeedSlider({ number, slider, output }) {
  if (!number || !slider) return;
  const syncFromSlider = () => {
    number.value = slider.value;
    if (output) output.value = displaySpeed(slider.value);
  };
  const syncFromNumber = () => {
    slider.value = number.value || "0";
    if (output) output.value = displaySpeed(slider.value);
  };

  slider.addEventListener("input", syncFromSlider);
  slider.addEventListener("change", () => {
    syncFromSlider();
    number.dispatchEvent(new Event("change", { bubbles: true }));
  });
  slider.addEventListener("keydown", (event) => {
    const direction = ["ArrowRight", "ArrowUp"].includes(event.key) ? 1
      : ["ArrowLeft", "ArrowDown"].includes(event.key) ? -1 : 0;
    if (!direction) return;
    event.preventDefault();
    const current = Number(slider.value) || 0;
    const next = Math.max(MIN, Math.min(MAX, Math.round((current + direction) * 100) / 100));
    slider.value = String(next);
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    slider.dispatchEvent(new Event("change", { bubbles: true }));
  });
  number.addEventListener("input", syncFromNumber);
  number.addEventListener("change", syncFromNumber);
}
