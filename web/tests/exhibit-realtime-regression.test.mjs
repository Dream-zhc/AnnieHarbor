import assert from 'node:assert/strict';
import test from 'node:test';
import {eventStages, interactionPhase, latestPreview} from '../src/exhibit/telemetry.ts';

const row = (generation, status='published_unknown') => ({
  call_id: `call-${generation}`, turn_id: `turn-${generation}`,
  generation, kind:'action', value:'agree_micro_nod', status,
  real_device_executed:false, animation_kind:'command_preview',
});

test('new robot generation never replays an action from a prior generation', () => {
  assert.equal(latestPreview([row(1)], 2), null);
  assert.equal(latestPreview([row(1), row(2)], 2)?.call_id, 'call-2');
  assert.equal(latestPreview([row(1), row(2, 'cancelled_before_send')], 2), null);
});

test('stale events do not pretend that a realtime pipeline is currently active', () => {
  const now = 180000;
  const stages = eventStages([
    {kind:'speech.started',ts_ms:now - 90000,turn_id:'old',seq:1},
    {kind:'tool.called',ts_ms:now,turn_id:'new',seq:2},
    {kind:'tts.playback.started',ts_ms:now,turn_id:'new',seq:3,source:'browser_test'},
  ], now);
  assert.equal(stages[0].active, false);
  assert.equal(stages[2].active, false, 'generic tools are not proof of memory recall');
  assert.equal(stages[5].active, false, 'browser test playback is not a device receipt');
  assert.equal(eventStages([{
    kind:'tts.playback.started',ts_ms:now,turn_id:'new',seq:4,source:'speaker',
  }], now)[5].active, true);
});

test('holographic state follows realtime receipts rather than TTS estimates', () => {
  const rt = {state:'connected', wakeword:{enabled:true,state:'SLEEP',wakes:2},
    speech_active:false, speech_output_state:'idle', playback_state:'unknown',playback_source:null};
  const snapshot = {robot:{ready:true},realtime:rt};
  const get = () => interactionPhase(snapshot).phase;
  assert.equal(get(),'sleep');
  rt.wakeword.state='WAKE_WAIT';
  assert.equal(get(),'awake');
  rt.speech_active=true;
  assert.equal(get(),'listening');
  rt.speech_active=false;
  rt.wakeword.state='ACTIVE';
  assert.equal(get(),'processing');
  rt.speech_output_state='active';
  assert.equal(get(),'processing', 'TTS generating does not prove playback');
  rt.playback_source='browser_test';rt.playback_state='started';
  assert.equal(interactionPhase(snapshot).label,'网页试听中');
  rt.playback_source='speaker';
  assert.equal(interactionPhase(snapshot).label,'扬声器播放中');
  rt.playback_source='unverified_source';
  assert.equal(get(),'processing', 'untrusted source cannot assert physical playback');
  rt.playback_source='speaker';
  rt.speech_active=true;
  assert.equal(get(),'playing', 'full duplex keeps the verified speaker phase');
  rt.playback_state='drained'; rt.speech_active=false;
  rt.wakeword.state='FOLLOWUP';
  assert.equal(get(),'followup');
  rt.wakeword.state='SLEEP';
  assert.equal(get(),'sleep');
  assert.equal(interactionPhase(snapshot,true).phase,'offline');
  rt.wakeword.error='stream_backlog';
  assert.equal(get(),'error');
});
