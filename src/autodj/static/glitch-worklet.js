// Glitch (random buffer slicer + reorder) AudioWorkletProcessor.
//
// Maintains a rolling 0.5 s buffer of recent input.  On each "slice"
// boundary, picks a random offset within the buffer and reads the next
// slice from there instead of the live input.  Slice edges crossfade
// to avoid clicks.
//
// Until the ring buffer has accumulated at least one full slice of
// audio, every slice is forced straight-through — otherwise the first
// 0.5 s would output silence (zeroed ring contents).

const _RING_SECONDS = 0.5;
const _SEAM_SAMPLES = 64;

class GlitchProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [
      { name: "sliceMs", defaultValue: 80,  minValue: 5,   maxValue: 500,
        automationRate: "k-rate" },
      { name: "density", defaultValue: 0.85, minValue: 0.0, maxValue: 1.0,
        automationRate: "k-rate" },
    ];
  }

  constructor() {
    super();
    this._ringSize = Math.max(1024, Math.floor(_RING_SECONDS * sampleRate));
    this._ring = new Float32Array(this._ringSize);
    this._writePos = 0;
    this._samplesWritten = 0;        // monotonic — caps at ringSize
    this._sliceRemaining = 0;
    this._currentReadOffset = 0;
    this._straightPass = true;
    this._fadePos = 0;
  }

  _pickSlice(sliceLen, density) {
    // Force straight-pass while ring isn't filled yet.
    if (this._samplesWritten < sliceLen + _SEAM_SAMPLES) {
      this._straightPass = true;
      this._currentReadOffset = 0;
    } else {
      this._straightPass = Math.random() >= density;
      if (this._straightPass) {
        this._currentReadOffset = 0;
      } else {
        const minOffset = sliceLen + 1;
        const maxOffset = Math.min(this._samplesWritten, this._ringSize) - sliceLen - 1;
        if (maxOffset > minOffset) {
          this._currentReadOffset = minOffset
            + Math.floor(Math.random() * (maxOffset - minOffset));
        } else {
          this._straightPass = true;
          this._currentReadOffset = 0;
        }
      }
    }
    this._sliceRemaining = sliceLen;
    this._fadePos = 0;
  }

  process(inputs, outputs, params) {
    const input = inputs[0];
    const output = outputs[0];
    if (!output || output.length === 0) return true;

    const sliceLen = Math.max(1, Math.floor(params.sliceMs[0] * 0.001 * sampleRate));
    const density = params.density[0];
    const seam = Math.min(_SEAM_SAMPLES, Math.floor(sliceLen / 8));

    const inCh = (input && input.length > 0) ? input[0] : null;
    const frames = output[0].length;
    const channels = output.length;

    for (let i = 0; i < frames; i++) {
      const sample = inCh ? inCh[i] : 0;
      this._ring[this._writePos] = sample;
      this._writePos = (this._writePos + 1) % this._ringSize;
      if (this._samplesWritten < this._ringSize) this._samplesWritten++;

      if (this._sliceRemaining <= 0) {
        this._pickSlice(sliceLen, density);
      }

      let outVal;
      if (this._straightPass || this._currentReadOffset === 0) {
        outVal = sample;
      } else {
        const readPos = (this._writePos - this._currentReadOffset + this._ringSize)
                        % this._ringSize;
        outVal = this._ring[readPos];
      }

      if (this._fadePos < seam) {
        outVal *= this._fadePos / seam;
      } else if (this._sliceRemaining < seam) {
        outVal *= this._sliceRemaining / seam;
      }
      this._fadePos++;
      this._sliceRemaining--;

      for (let c = 0; c < channels; c++) output[c][i] = outVal;
    }
    return true;
  }
}

registerProcessor("glitch", GlitchProcessor);
