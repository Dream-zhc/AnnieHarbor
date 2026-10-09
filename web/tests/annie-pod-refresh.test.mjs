import test from 'node:test';
import assert from 'node:assert/strict';

const load = () => import('../src/annie-pod-refresh.ts');
const settle = () => new Promise(resolve => setImmediate(resolve));

test('status events request a snapshot immediately, without a polling timer', async () => {
  const {createPodRefresh, isPodStatusEvent} = await load();
  let calls = 0;
  const refresh = createPodRefresh(async () => { calls++; }, () => true);
  assert.equal(isPodStatusEvent('wakeword.detected'), true);
  await refresh.request();
  assert.equal(calls, 1);
  for (const kind of ['speech.started', 'speech.stopped', 'transcript.done',
    'turn.started', 'turn.done', 'session.ready', 'session.error',
    'tts.segment_queued', 'tts.stream_finished', 'tts.playback.started',
    'tts.playback.drained', 'tts.playback.resumed']) {
    assert.equal(isPodStatusEvent(kind), true, kind);
  }
  for (const kind of ['text.delta', 'transcript.delta', 'tts.pcm', 'viseme.frame']) {
    assert.equal(isPodStatusEvent(kind), false, kind);
  }
  refresh.stop();
});

test('an event during a snapshot queues one immediate trailing read, not concurrent reads', async () => {
  const {createPodRefresh} = await load();
  let calls = 0;
  const releases = [];
  const refresh = createPodRefresh(() => {
    calls++;
    return new Promise(resolve => releases.push(resolve));
  }, () => true);
  const first = refresh.request();
  await refresh.request();
  await refresh.request();
  assert.equal(calls, 1);
  releases.shift()();
  await first;
  await settle();
  assert.equal(calls, 2);
  releases.shift()();
  await settle();
  assert.equal(calls, 2);
  refresh.stop();
});

test('hidden pages and stopped robot subscriptions do not schedule more reads', async () => {
  const {createPodRefresh} = await load();
  let visible = false;
  let calls = 0;
  let release;
  const refresh = createPodRefresh(() => {
    calls++;
    return new Promise(resolve => { release = resolve; });
  }, () => visible);
  await refresh.request();
  assert.equal(calls, 0);
  visible = true;
  const first = refresh.request();
  await refresh.request();
  refresh.stop();
  release();
  await first;
  await refresh.request();
  assert.equal(calls, 1);
});

test('workbench reuses the scoped SSE connection and pod retains identity and cleanup guards', async () => {
  const {readFile} = await import('node:fs/promises');
  const workbench = await readFile(new URL('../src/workbench.tsx', import.meta.url), 'utf8');
  const component = await readFile(new URL('../src/annie-state-pod.tsx', import.meta.url), 'utf8');
  assert.match(workbench, /scoped\.some\(item=>isPodStatusEvent\(item\.kind\)\)/);
  assert.match(workbench, /statusRevision=\{podStatusRevision\}/);
  assert.match(component, /refreshNow\.current\?\.\(\)/);
  assert.match(component, /\[statusRevision\]/);
  assert.match(component, /refresh\.stop\(\)/);
  assert.doesNotMatch(component, /new EventSource|new WebSocket/);
});
