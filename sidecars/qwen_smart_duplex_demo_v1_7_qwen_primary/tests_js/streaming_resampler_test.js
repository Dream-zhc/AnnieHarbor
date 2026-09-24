const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
const start = src.indexOf('class StreamingPCM16Resampler');
const end = src.indexOf('\nfunction pushAudio', start);
assert(start >= 0 && end > start, 'resampler source not found');
const sandbox = { Float32Array, Int16Array, Math };
vm.runInNewContext(`${src.slice(start, end)}\nthis.Resampler = StreamingPCM16Resampler;`, sandbox);
const Resampler = sandbox.Resampler;

function pcmBuffer(values) {
  const a = new Int16Array(values);
  return a.buffer;
}
function concatFloat(parts) {
  const n = parts.reduce((sum, p) => sum + p.length, 0);
  const out = new Float32Array(n);
  let off = 0;
  for (const p of parts) { out.set(p, off); off += p.length; }
  return out;
}

const values = [];
for (let i = 0; i < 2000; i++) values.push(Math.round(Math.sin(i / 17) * 20000));

const whole = new Resampler(24000, 44100);
const expected = concatFloat([whole.push(pcmBuffer(values)), whole.flush()]);

const chunked = new Resampler(24000, 44100);
const actualParts = [];
for (const [a, b] of [[0, 157], [157, 619], [619, 1003], [1003, 1731], [1731, 2000]]) {
  actualParts.push(chunked.push(pcmBuffer(values.slice(a, b))));
}
actualParts.push(chunked.flush());
const actual = concatFloat(actualParts);

assert.strictEqual(actual.length, expected.length, 'chunked and whole resampling length differs');
let maxError = 0;
for (let i = 0; i < actual.length; i++) maxError = Math.max(maxError, Math.abs(actual[i] - expected[i]));
assert(maxError < 1e-7, `resampling phase discontinuity: ${maxError}`);
console.log('streaming resampler cross-frame continuity test: OK');
