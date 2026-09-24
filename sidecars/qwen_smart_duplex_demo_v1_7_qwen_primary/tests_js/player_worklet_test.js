const assert = require('assert');
global.sampleRate = 48000;
let Registered = null;
class FakePort {
  constructor() { this.messages = []; this.onmessage = null; }
  postMessage(msg) { this.messages.push(msg); }
  send(msg) { this.onmessage({ data: msg }); }
}
global.AudioWorkletProcessor = class { constructor() { this.port = new FakePort(); } };
global.registerProcessor = (_name, cls) => { Registered = cls; };
require('../static/player-worklet.js');
assert(Registered, 'processor not registered');

const output = [[new Float32Array(128)]];
const processN = (p, n) => { for (let i = 0; i < n; i++) p.process([], output); };
const audioMs = (ms, value = 0.5) => new Float32Array(Math.round(48000 * ms / 1000)).fill(value);

// 1) 首播预缓冲：100ms 不应立即消费，累计到 180ms 后才开始。
const p = new Registered();
p.port.send({
  type: 'generation', generation: 1,
  maxQueueMs: 30000, prebufferMs: 180, rebufferMs: 100, holdQueueMs: 240,
});
p.port.send({ type: 'push', buffer: audioMs(100).buffer });
processN(p, 10);
assert.strictEqual(p.samplesQueued, 4800, 'prebuffering must not consume early');
assert.strictEqual(p.streamState, 'prebuffering');
p.port.send({ type: 'push', buffer: audioMs(100).buffer });
processN(p, 1);
assert.strictEqual(p.streamState, 'playing');
assert(p.samplesQueued < 9600, 'playback should start after prebuffer threshold');
assert(p.port.messages.some(m => m.type === 'buffer_ready'));

// 2) 正常 TTS 即使一次快速返回数秒，也不能用 1.2 秒队列截断。
const burst = new Registered();
burst.port.send({
  type: 'generation', generation: 2,
  maxQueueMs: 30000, prebufferMs: 180, rebufferMs: 100, holdQueueMs: 240,
});
for (const ms of [320, 704, 309, 1800]) {
  burst.port.send({ type: 'push', buffer: audioMs(ms, 0.25).buffer });
}
const expectedBurstSamples = Math.round(48000 * (320 + 704 + 309 + 1800) / 1000);
assert.strictEqual(burst.samplesQueued, expectedBurstSamples, 'normal burst audio must be kept intact');
assert(!burst.port.messages.some(m => m.type === 'hard_overflow'));

// 3) 中途断粮时进入一次 rebuffering，不反复播放几毫秒又停几毫秒。
const jitter = new Registered();
jitter.port.send({
  type: 'generation', generation: 3,
  maxQueueMs: 30000, prebufferMs: 80, rebufferMs: 100, holdQueueMs: 240,
});
jitter.port.send({ type: 'push', buffer: audioMs(100).buffer });
processN(jitter, 50); // 足够耗尽 100ms
assert.strictEqual(jitter.streamState, 'rebuffering');
assert.strictEqual(jitter.underrunCount, 1);
jitter.port.send({ type: 'push', buffer: audioMs(50).buffer });
processN(jitter, 5);
assert.strictEqual(jitter.streamState, 'rebuffering', '50ms is below rebuffer target');
jitter.port.send({ type: 'push', buffer: audioMs(60).buffer });
processN(jitter, 1);
assert.strictEqual(jitter.streamState, 'playing');
assert(jitter.port.messages.some(m => m.type === 'rebuffered'));

// 4) source_finished 后不足预缓冲的尾音也必须播完，然后进入 ended。
const tail = new Registered();
tail.port.send({
  type: 'generation', generation: 4,
  maxQueueMs: 30000, prebufferMs: 180, rebufferMs: 100, holdQueueMs: 240,
});
tail.port.send({ type: 'push', buffer: audioMs(70).buffer });
assert.strictEqual(tail.streamState, 'prebuffering');
tail.port.send({ type: 'source_finished' });
assert.strictEqual(tail.streamState, 'playing');
processN(tail, 40);
assert.strictEqual(tail.samplesQueued, 0);
assert.strictEqual(tail.streamState, 'ended');

// 5) Hold 状态只保留最近窗口；恢复与清空仍可靠。
const hold = new Registered();
hold.port.send({
  type: 'generation', generation: 5,
  maxQueueMs: 30000, prebufferMs: 80, rebufferMs: 60, holdQueueMs: 240,
});
hold.port.send({ type: 'push', buffer: audioMs(300).buffer });
processN(hold, 5);
hold.port.send({ type: 'hold', requestId: 'h1', fadeMs: 6, preserveAudio: false });
processN(hold, 4);
assert.strictEqual(hold.mode, 'held');
assert(hold.port.messages.some(m => m.type === 'held' && m.requestId === 'h1'));
const heldQueued = hold.samplesQueued;
processN(hold, 20);
assert.strictEqual(hold.samplesQueued, heldQueued, 'held playback head must not advance');
for (let i = 0; i < 8; i++) hold.port.send({ type: 'push', buffer: audioMs(100, 0.2).buffer });
assert(hold.samplesQueued <= Math.round(48000 * 0.24));
assert(hold.port.messages.some(m => m.type === 'hold_trim'));
hold.port.send({ type: 'resume', requestId: 'r1', fadeMs: 12 });
processN(hold, 8);
assert.notStrictEqual(hold.mode, 'held');
hold.port.send({ type: 'clear', requestId: 'c1' });
assert.strictEqual(hold.samplesQueued, 0);
assert.strictEqual(hold.streamState, 'ended');
assert(hold.port.messages.some(m => m.type === 'cleared' && m.requestId === 'c1'));


// 6) Qwen smart_turn 暂定暂停必须完整保留队列，turn_invalid 后原位续播。
const qwenHold = new Registered();
qwenHold.port.send({
  type: 'generation', generation: 6,
  maxQueueMs: 30000, prebufferMs: 80, rebufferMs: 60, holdQueueMs: 240,
});
qwenHold.port.send({ type: 'push', buffer: audioMs(500, 0.3).buffer });
processN(qwenHold, 5);
qwenHold.port.send({
  type: 'hold', requestId: 'qh1', fadeMs: 0, preserveAudio: true,
});
processN(qwenHold, 2);
const qwenHeldBefore = qwenHold.samplesQueued;
for (let i = 0; i < 10; i++) {
  qwenHold.port.send({ type: 'push', buffer: audioMs(100, 0.2).buffer });
}
assert(qwenHold.samplesQueued >= qwenHeldBefore + Math.round(48000 * 1.0),
  'qwen hold must preserve incoming audio instead of trimming to 240ms');
assert(!qwenHold.port.messages.some(m => m.type === 'hold_trim'),
  'qwen hold must not emit hold_trim');
qwenHold.port.send({ type: 'resume', requestId: 'qr1', fadeMs: 0 });
processN(qwenHold, 3);
assert.notStrictEqual(qwenHold.mode, 'held');

console.log('player-worklet reliable queue/jitter/qwen-hold tests: OK');
