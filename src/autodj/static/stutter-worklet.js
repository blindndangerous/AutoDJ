// Sample-accurate stutter / gate effect.
//
// Why a worklet: scheduling on a GainNode via setValueAtTime can click
// at hard 1→0 transitions because Web Audio defers parameter changes to
// block boundaries (typically 128 samples).  Doing it in a worklet lets
// us apply a short raised-cosine fade on every gate edge (zero clicks)
// AND ramp the rate / duty cycle smoothly in real time without
// re-scheduling.
//
// Parameters (k-rate):
//   rate       — gate cycles per second (1–32 Hz)
//   duty       — fraction of each cycle that's "open" (0.05–0.95)
//   edgeMs     — fade time at each on/off edge, in milliseconds (0.5–20)
//
// Both stereo channels are processed identically (mono gate).

class StutterProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [
      { name: "rate",   defaultValue: 8.0,  minValue: 1.0,  maxValue: 32.0,
        automationRate: "k-rate" },
      { name: "duty",   defaultValue: 0.25, minValue: 0.05, maxValue: 0.95,
        automationRate: "k-rate" },
      { name: "edgeMs", defaultValue: 3.0,  minValue: 0.5,  maxValue: 20.0,
        automationRate: "k-rate" },
    ];
  }

  constructor() {
    super();
    // Phase accumulator in [0, 1) over one gate cycle
    this._phase = 0;
  }

  process(inputs, outputs, params) {
    const input = inputs[0];
    const output = outputs[0];
    if (!input || input.length === 0) return true;

    const rate = params.rate[0];
    const duty = params.duty[0];
    const edge = (params.edgeMs[0] * 0.001) * rate; // edge in cycle units

    const channelCount = output.length;
    const frames = output[0].length;
    const phaseInc = rate / sampleRate;

    for (let i = 0; i < frames; i++) {
      // Compute gate envelope at this phase
      let env;
      if (this._phase < edge) {
        // Rising edge — raised-cosine in [0, 1]
        const t = this._phase / edge;
        env = 0.5 - 0.5 * Math.cos(Math.PI * t);
      } else if (this._phase < duty - edge) {
        env = 1.0;
      } else if (this._phase < duty) {
        // Falling edge
        const t = (this._phase - (duty - edge)) / edge;
        env = 0.5 + 0.5 * Math.cos(Math.PI * t);
      } else {
        env = 0.0;
      }

      for (let c = 0; c < channelCount; c++) {
        const inCh = input[c] || input[0];
        output[c][i] = (inCh ? inCh[i] : 0) * env;
      }

      this._phase += phaseInc;
      if (this._phase >= 1) this._phase -= 1;
    }
    return true;
  }
}

registerProcessor("stutter", StutterProcessor);
