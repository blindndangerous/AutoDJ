// Freeze (granular looper) AudioWorkletProcessor.
//
// Captures `grainMs` of input audio across however many process blocks
// it takes (one block is only ~3 ms at 44.1 kHz, so a 150 ms grain
// needs ~50 blocks of capture before the loop begins).  Once full, it
// loops the captured grain forever (or until fadeOutSec elapses).
//
// Worklet-only — no native Web Audio node provides this behaviour.

class FreezeProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [
      { name: "grainMs",    defaultValue: 150,  minValue: 10,  maxValue: 500,
        automationRate: "k-rate" },
      { name: "fadeOutSec", defaultValue: 4.0,  minValue: 0.0, maxValue: 30.0,
        automationRate: "k-rate" },
    ];
  }

  constructor() {
    super();
    this._grain = null;        // Float32Array, allocated on first process call
    this._grainLen = 0;
    this._capturePos = 0;      // # samples captured so far
    this._readIdx = 0;
    this._elapsedSamples = 0;
  }

  _seamCrossfade(buf) {
    // Smooth the loop seam with a short raised-cosine crossfade so the
    // join doesn't click.
    const n = buf.length;
    const seam = Math.min(96, Math.floor(n / 8));
    if (seam <= 0) return;
    for (let i = 0; i < seam; i++) {
      const fade = 0.5 - 0.5 * Math.cos((Math.PI * i) / seam);
      const a = buf[i];
      const b = buf[n - seam + i];
      buf[i] = a * fade + b * (1 - fade);
    }
  }

  process(inputs, outputs, params) {
    const input = inputs[0];
    const output = outputs[0];
    if (!output || output.length === 0) return true;

    const grainMs = params.grainMs[0];
    const fadeOutSec = params.fadeOutSec[0];

    // Lazy-allocate the grain buffer on the first block where we know
    // sampleRate (a worklet global).
    if (this._grain === null) {
      this._grainLen = Math.max(1, Math.floor(grainMs * 0.001 * sampleRate));
      this._grain = new Float32Array(this._grainLen);
    }

    const inCh = (input && input.length > 0) ? input[0] : null;
    const frames = output[0].length;
    const channels = output.length;
    const fadeSamples = Math.max(1, Math.floor(fadeOutSec * sampleRate));

    for (let i = 0; i < frames; i++) {
      // Capture phase — fill the grain buffer over multiple process blocks
      if (this._capturePos < this._grainLen) {
        this._grain[this._capturePos++] = inCh ? inCh[i] : 0;
        if (this._capturePos === this._grainLen) {
          this._seamCrossfade(this._grain);
        }
        // While capturing, pass input through so the start of the
        // freeze isn't a sudden silence — the loop kicks in once the
        // grain is full.
        const passVal = inCh ? inCh[i] : 0;
        for (let c = 0; c < channels; c++) output[c][i] = passVal;
        continue;
      }

      // Loop phase — read the captured grain
      const sample = this._grain[this._readIdx];
      this._readIdx = (this._readIdx + 1) % this._grainLen;
      let env = 1.0;
      if (fadeOutSec > 0) {
        env = Math.max(0, 1 - this._elapsedSamples / fadeSamples);
      }
      const v = sample * env;
      for (let c = 0; c < channels; c++) output[c][i] = v;
      this._elapsedSamples++;
    }
    if (fadeOutSec > 0 && this._elapsedSamples >= fadeSamples) {
      return false;
    }
    return true;
  }
}

registerProcessor("freeze", FreezeProcessor);
