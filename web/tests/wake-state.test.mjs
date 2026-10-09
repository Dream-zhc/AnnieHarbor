import assert from 'node:assert/strict';
import test from 'node:test';
import {wakeLabel, wakePresentation} from '../src/wake-state.ts';

test('actual sleep is not mislabelled as audio idle or realtime offline', () => {
  const status = wakePresentation(true, {enabled:true, state:'SLEEP', pcm_to_qwen:false});
  assert.equal(status.label, '休眠 · 等待唤醒');
  assert.equal(status.countdown, null);
  assert.match(status.detail, /普通语音未送往 Qwen/);
  assert.equal(wakePresentation(false, {enabled:true, state:'SLEEP'}).label, '未连接');
});

test('wake wait and followup distinguish candidate speech, and countdown is measured', () => {
  const wait = wakePresentation(true, {enabled:true, state:'WAKE_WAIT',
    deadline_remaining_ms:4100, candidate_speech:false});
  assert.equal(wakeLabel(wait), '已唤醒 · 待提问 · 5s');
  assert.equal(wakePresentation(true, {enabled:true, state:'WAKE_WAIT',
    candidate_speech:true, deadline_remaining_ms:null}).label, '已唤醒 · 正在听');
  const followup = wakePresentation(true, {enabled:true, state:'FOLLOWUP',
    deadline_remaining_ms:7800, candidate_speech:false});
  assert.equal(wakeLabel(followup), '等待追问 · 8s');
  assert.equal(wakePresentation(true, {enabled:true, state:'ACTIVE',
    candidate_speech:false}).label, '待机中');
});

test('disabled, invalid and failed wake modules show truthful states', () => {
  assert.equal(wakePresentation(true, {enabled:false}).label, '免唤醒');
  assert.equal(wakePresentation(true, {enabled:true, state:'SLEEP', error:'stream_backlog'}).label,
    '唤醒异常');
  assert.equal(wakePresentation(true, {enabled:true, state:'unknown'}).label, '状态未知');
  assert.equal(wakePresentation(true, {enabled:true, state:'FOLLOWUP',
    deadline_remaining_ms:NaN}).countdown, null);
});
