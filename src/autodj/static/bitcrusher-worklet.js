// AudioWorklet processor implementing a real bitcrusher — both
// amplitude quantisation AND sample-rate reduction.  WaveShaperNode
// only does the former; this gives the authentic Atari/PC-speaker
// sound.  Registered in app.js when the AudioContext boots.

class BitcrusherProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [
      { name: "bits",         defaultValue: 12, minValue: 1, maxValue: 16,
        automationRate: "k-rate" },
      { name: "rateReduce",   defaultValue: 1,  minValue: 1, maxValue: 64,
        automationRate: "k-rate" },
    ];
  }

  constructor() {
    super();
    this._heldL = 0;
    this._heldR = 0;
    this._ctr = 0;
  }

  process(inputs, outputs, params) {
    const input = inputs[0];
    const output = outputs[0];
    if (!input || input.length === 0) return true;

    const bits = Math.max(1, Math.min(16, params.bits[0] | 0));
    const rateReduce = Math.max(1, Math.min(64, params.rateReduce[0] | 0));
    const levels = Math.pow(2, bits - 1);

    const channelCount = output.length;
    const frameCount = output[0].length;

    for (let i = 0; i < frameCount; i++) {
      // Sample-and-hold every `rateReduce` samples → fake low sample rate
      if (this._ctr === 0) {
        this._heldL = input[0] ? input[0][i] : 0;
        this._heldR = input[1] ? input[1][i] : this._heldL;
      }
      this._ctr = (this._ctr + 1) % rateReduce;

      // Amplitude quantise to N bits
      const qL = Math.round(this._heldL * levels) / levels;
      const qR = Math.round(this._heldR * levels) / levels;

      output[0][i] = qL;
      if (channelCount > 1) output[1][i] = qR;
    }
    return true;
  }
}

registerProcessor("bitcrusher", BitcrusherProcessor);
