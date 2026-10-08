import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {anniePodPhase} from '../src/annie-pod-state.ts';

function fixture(state='SLEEP') {
  return {robot_id:'annie-a',generation:2,ready:true,
    state:'connected',wakeword:{enabled:true,state,wakes:2},
    speech_active:false,speech_output:{state:'idle',
      playback_state:'unknown',playback_source:null}};
}

test('workbench halo phases reflect actual wake, speech and verified playback state',()=>{
  const item=fixture();
  assert.equal(anniePodPhase(item).phase,'sleep');
  item.wakeword.state='WAKE_WAIT';
  assert.equal(anniePodPhase(item).phase,'awake');
  item.speech_active=true;
  assert.equal(anniePodPhase(item).phase,'listening');
  item.speech_active=false;
  item.wakeword.state='ACTIVE';
  item.speech_output.state='active';
  assert.equal(anniePodPhase(item).phase,'processing');
  item.speech_output.playback_state='started';
  assert.equal(anniePodPhase(item).phase,'processing', 'TTS is not proof of playback');
  item.speech_output.playback_source='browser_test';
  assert.equal(anniePodPhase(item).label,'网页试听中');
  item.speech_output.playback_source='speaker';
  assert.equal(anniePodPhase(item).label,'扬声器播放中');
  item.speech_output.playback_state='drained';
  item.wakeword.state='FOLLOWUP';
  assert.equal(anniePodPhase(item).phase,'followup');
  assert.equal(anniePodPhase(item,true).phase,'offline');
  item.wakeword.error='stream_backlog';
  assert.equal(anniePodPhase(item).phase,'error');
});

test('state pod is workbench-only, read-only and releases its poll on switch',async()=>{
  const workbench=await readFile(new URL('../src/workbench.tsx',import.meta.url),'utf8');
  const component=await readFile(new URL('../src/annie-state-pod.tsx',import.meta.url),'utf8');
  const css=await readFile(new URL('../src/annie-state-pod.css',import.meta.url),'utf8');
  assert.match(workbench,/<AnnieStatePod key=\{robotId/);
  assert.match(component,/robotPath\(robotId\) \+ '\/realtime'/);
  assert.match(component,/row\.robot_id !== robotId/);
  assert.match(component,/row\.generation !== generation/);
  assert.match(component,/controller\?\.abort\(\)/);
  assert.match(component,/window\.clearInterval\(timer\)/);
  assert.match(css,/prefers-reduced-motion:reduce/);
  assert.doesNotMatch(component,/WebGL|requestAnimationFrame|response\.create|\.send\(/);
});
