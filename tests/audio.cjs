const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path');
const root = path.resolve(__dirname, '../static');
const source = fs.readFileSync(path.join(root, 'pcm-worklet.js'), 'utf8');

for (const rate of [48000, 44100]) {
  const messages = [];
  let Processor;
  const context = vm.createContext({
    sampleRate: rate, Float32Array,
    AudioWorkletProcessor: class { constructor() { this.port = { postMessage: (message) => messages.push(message) }; } },
    registerProcessor: (name, implementation) => { Processor = implementation; },
  });
  vm.runInContext(source, context);
  const processor = new Processor();
  const totalFrames = rate * 13 + 17;
  let cursor = 0, frames = 0;
  while (cursor < totalFrames) {
    const count = Math.min(128, totalFrames - cursor);
    const left = Float32Array.from({ length: count }, (_, i) => Math.sin(2 * Math.PI * 440 * (cursor + i) / rate) * .2);
    const right = Float32Array.from(left, (value) => value * .5);
    const output = new Float32Array(count).fill(1);
    assert.equal(processor.process([[left, right]], [[output]]), true);
    assert.ok(output.every((value) => value === 0), 'microphone must not play through speakers');
    cursor += count;
    if (++frames % 37 === 0) processor.port.onmessage({ data: { type: 'flush', token: frames } });
  }
  processor.port.onmessage({ data: { type: 'flush', token: 'end' } });
  const samples = messages.filter((message) => message.type === 'pcm').flatMap((message) => [...message.samples]);
  const expected = Math.floor((totalFrames - 1) * 16000 / rate) + 1;
  assert.equal(samples.length, expected, 'streaming resampling output length');
  let maxError = 0;
  for (let i = 0; i < samples.length; i++) {
    const position = i * rate / 16000;
    const lower = Math.floor(position), fraction = position - lower;
    const a = Math.sin(2 * Math.PI * 440 * lower / rate) * .15;
    const b = Math.sin(2 * Math.PI * 440 * (lower + 1) / rate) * .15;
    maxError = Math.max(maxError, Math.abs(samples[i] - (a + (b - a) * fraction)));
  }
  assert.ok(maxError < .00001, `cross-block interpolation must retain phase: ${maxError}`);
  console.log(JSON.stringify({ test: 'AudioWorklet continuous resampling', inputRate: rate, inputFrames: totalFrames, outputFrames: samples.length, expectedFrames: expected, maxInterpolationError: maxError, partialFlushes: messages.filter((m) => m.type === 'flushed').length }));
}

const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const chunkCode = app.slice(app.indexOf('  class AudioSegmenter {'), app.indexOf('  async function startRecording()'));
const context = vm.createContext({ Float32Array, ArrayBuffer, DataView, SAMPLE_RATE: 16000, setVolume: () => {} });
vm.runInContext(chunkCode + '\nglobalThis.Segmenter = AudioSegmenter; globalThis.encode = wavBytes;', context);
function feed(segmenter, seconds, amplitude) {
  let remaining = Math.round(seconds * 16000);
  while (remaining > 0) { const count = Math.min(1600, remaining); segmenter.accept(new Float32Array(count).fill(amplitude)); remaining -= count; }
}
let chunks = [];
let segmenter = new context.Segmenter(10, (chunk) => chunks.push(chunk));
feed(segmenter, 5, 0); segmenter.flush();
assert.equal(chunks.length, 0, 'silence is not uploaded');
segmenter = new context.Segmenter(10, (chunk) => chunks.push(chunk));
feed(segmenter, .2, .01); segmenter.flush();
assert.equal(chunks.length, 1); assert.equal(chunks[0].duration, .2, 'stop preserves 0.2 s tail');
chunks = [];
segmenter = new context.Segmenter(0, (chunk) => chunks.push(chunk));
feed(segmenter, 1, 0); feed(segmenter, 3.2, .01); feed(segmenter, .8, 0);
assert.equal(chunks.length, 1, '>=4 s plus >=0.7 s silence creates chunk');
assert.equal(chunks[0].start, .75, '250 ms preroll is correctly placed');
assert.ok(chunks[0].duration >= 4 && chunks[0].duration < 4.2);
chunks = [];
segmenter = new context.Segmenter(50, (chunk) => chunks.push(chunk));
feed(segmenter, .3, 0); feed(segmenter, 30, .01); segmenter.flush();
assert.equal(chunks.length, 3);
assert.ok(chunks.every((chunk) => chunk.duration <= 12), 'no chunk exceeds 12 seconds');
for (let i = 1; i < chunks.length; i++) assert.ok(Math.abs(chunks[i].start - (chunks[i - 1].start + chunks[i - 1].duration)) < 1e-8, 'chunks remain contiguous');
const buffer = context.encode(new Float32Array([-1, 0, 1]));
const view = new DataView(buffer);
assert.equal(buffer.byteLength, 50); assert.equal(view.getUint32(24, true), 16000);
assert.equal(view.getUint16(22, true), 1); assert.equal(view.getUint16(34, true), 16);
assert.equal(view.getInt16(44, true), -32768); assert.equal(view.getInt16(48, true), 32767);
console.log(JSON.stringify({ test: 'Audio segmenter and WAV encoder', silenceSkipped: true, shortTailPreserved: true, silenceBoundary: true, maxChunkSeconds: 12, chunkTimelineContinuous: true, wav: '16 kHz mono PCM16' }));
