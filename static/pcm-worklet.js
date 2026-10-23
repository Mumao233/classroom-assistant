/* Streaming mono resampler. The sample position and previous sample survive
   process() calls, so a 44.1 kHz input does not reset its phase every 128 frames. */
class ClassroomPCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.outputRate = 16000;
    this.step = sampleRate / this.outputRate;
    this.sourcePosition = 0;
    this.nextOutputPosition = 0;
    this.previous = 0;
    this.pending = new Float32Array(1600);
    this.pendingLength = 0;
    this.port.onmessage = ({ data }) => {
      if (data?.type === 'flush') {
        this.flush();
        this.port.postMessage({ type: 'flushed', token: data.token });
      }
    };
  }
  flush() {
    if (!this.pendingLength) return;
    const samples = this.pending.slice(0, this.pendingLength);
    this.port.postMessage({ type: 'pcm', samples }, [samples.buffer]);
    this.pendingLength = 0;
  }
  emit(value) {
    this.pending[this.pendingLength++] = value;
    if (this.pendingLength === this.pending.length) this.flush();
  }
  process(inputs, outputs) {
    const channels = inputs[0];
    if (channels?.length && channels[0]?.length) {
      const count = channels[0].length;
      for (let i = 0; i < count; i++) {
        let current = 0;
        for (let channel = 0; channel < channels.length; channel++) current += channels[channel][i] || 0;
        current /= channels.length;
        while (this.nextOutputPosition <= this.sourcePosition) {
          const fraction = this.sourcePosition === 0 ? 1 : this.nextOutputPosition - (this.sourcePosition - 1);
          this.emit(this.previous + (current - this.previous) * Math.max(0, Math.min(1, fraction)));
          this.nextOutputPosition += this.step;
        }
        this.previous = current;
        this.sourcePosition++;
      }
    }
    // Keep the graph alive without playing microphone audio through the speakers.
    for (const output of outputs) for (const channel of output) channel.fill(0);
    return true;
  }
}
registerProcessor('classroom-pcm', ClassroomPCMProcessor);
