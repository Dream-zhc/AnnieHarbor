import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {latencyMetrics} from '../src/latency-metrics.ts';

const realtime=(times,turn='turn-1')=>({
  last_turn_timeline:{turn_id:turn,times,durations:{
    endpoint_to_asr_final_ms:2,llm_request_to_first_text_ms:2,
  }},
});

test('a 2ms provider event gap cannot masquerade as end-to-end latency',()=>{
  const result=latencyMetrics(realtime({speech_stopped_ms:1000,
    asr_final_ms:1002,llm_first_text_ms:1002}),
  {turn_id:'turn-1',first_audio_ms:479.78});
  assert.equal(result.endToEnd,null);
  assert.equal(result.speakerStart,null);
  assert.equal(result.firstAudio,479.78);
  assert.equal(result.firstRoom,null);
});

test('latency values require ordered same-turn events and speaker receipt',()=>{
  const result=latencyMetrics(realtime({speech_stopped_ms:1000,
    tts_first_pcm_ms:1500,room_first_publish_ms:1540,speaker_started_ms:1700}),
  {turn_id:'turn-1',first_audio_ms:479.78});
  assert.deepEqual(result,{speakerStart:1700,endToEnd:700,
    firstAudio:479.78,firstRoom:40});
  assert.equal(latencyMetrics(realtime({speaker_started_ms:1700})).endToEnd,null);
  assert.equal(latencyMetrics(realtime({speech_stopped_ms:2000,
    speaker_started_ms:1700,tts_first_pcm_ms:1600,room_first_publish_ms:1550})).endToEnd,null);
  assert.equal(latencyMetrics(realtime({tts_first_pcm_ms:1600,
    room_first_publish_ms:1550})).firstRoom,null);
  assert.equal(latencyMetrics(realtime({},'older'),
    {turn_id:'newer',first_audio_ms:479.78}).firstAudio,null);
});

test('dashboard and diagnostics show provenance instead of fabricated ASR/LLM latency',()=>{
  const hud=readFileSync(new URL('../src/latency-hud.tsx',import.meta.url),'utf8');
  const drawer=readFileSync(new URL('../src/developer-drawer.tsx',import.meta.url),'utf8');
  const round=readFileSync(new URL('../src/turn-diagnostics.tsx',import.meta.url),'utf8');
  const workbench=readFileSync(new URL('../src/workbench.tsx',import.meta.url),'utf8');
  assert.doesNotMatch(hud,/停声 → ASR Final|停声 → 首字|全链路延迟/);
  assert.match(hud,/设备自报 · 非声学测量/);
  assert.match(drawer,/<TurnDiagnostics/);
  assert.match(round,/Session 实际发送的完整 instructions/);
  assert.match(round,/bootstrap_count/);
  assert.match(round,/recall_memory/);
  assert.match(round,/tool\.called/);
  assert.match(round,/本轮 Session 提示词 \/ 注入上下文未留存/);
  assert.match(round,/云端后续滚动上下文不可读取/);
  assert.match(workbench,/if\(!developerOpen\|\|!robotId\|\|!session\)/);
  assert.match(workbench,/robotPath\(robotId\)\+'\/diagnostics'/);
});

test('browser receipt is separately visible without claiming a physical speaker',()=>{
  const round=readFileSync(new URL('../src/turn-diagnostics.tsx',import.meta.url),'utf8');
  const speaker=readFileSync(new URL('../src/room-debug-speaker.tsx',import.meta.url),'utf8');
  assert.match(round,/browser_started_ms/);
  assert.match(round,/网页试听回执/);
  assert.match(round,/真机回执/);
  assert.match(speaker,/description:descriptions\[type\],ts:Date\.now\(\)/);
  assert.doesNotMatch(speaker,/report\(current,'progress'/);
  assert.match(speaker,/report\(current,'drained'/);
});
