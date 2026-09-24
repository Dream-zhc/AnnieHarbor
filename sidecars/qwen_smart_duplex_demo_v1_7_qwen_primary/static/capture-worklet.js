class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.buffer = [];
    this.phase = 0;
    this.frameSamples = 320; // 20 ms @ 16 kHz
    this.levelCounter = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const src = input[0];
    const ratio = sampleRate / this.targetRate;

    let sum = 0;
    for (let i = 0; i < src.length; i++) sum += src[i] * src[i];
    const rms = Math.sqrt(sum / Math.max(1, src.length));
    const dbfs = 20 * Math.log10(Math.max(rms, 1e-7));
    if (++this.levelCounter % 4 === 0) {
      this.port.postMessage({ type: 'level', dbfs });
    }

    while (this.phase < src.length) {
      const i0 = Math.floor(this.phase);
      const i1 = Math.min(i0 + 1, src.length - 1);
      const frac = this.phase - i0;
      const sample = src[i0] * (1 - frac) + src[i1] * frac;
      this.buffer.push(Math.max(-1, Math.min(1, sample)));
      this.phase += ratio;
    }
    this.phase -= src.length;

    while (this.buffer.length >= this.frameSamples) {
      const frame = this.buffer.splice(0, this.frameSamples);
      const pcm = new Int16Array(frame.length);
      for (let i = 0; i < frame.length; i++) {
        pcm[i] = frame[i] < 0 ? frame[i] * 32768 : frame[i] * 32767;
      }
      this.port.postMessage({ type: 'audio', buffer: pcm.buffer }, [pcm.buffer]);
    }
    return true;
  }
}

registerProcessor('capture-processor', CaptureProcessor);
