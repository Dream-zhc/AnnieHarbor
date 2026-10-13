import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const source=readFileSync(new URL('../src/voice-settings.tsx',import.meta.url),'utf8');
const workbench=readFileSync(new URL('../src/workbench.tsx',import.meta.url),'utf8');

test('settings integrates voice cloning, persistent Chat selection and preview playback',()=>{
  assert.match(workbench,/<VoiceSettings/);
  assert.match(workbench,/机器人设定/);
  assert.match(source,/\/voices\/clone\?/);
  assert.match(source,/voice_id:voiceId,scope/);
  assert.match(source,/session_epoch:session\.session_epoch/);
  assert.match(source,/generation:session\.generation/);
  assert.match(source,/\.wav,\.mp3,\.m4a/);
  assert.match(source,/15组试听/);
  assert.match(source,/<audio controls preload="none"/);
  assert.match(source,/恢复本 Chat 默认/);
  assert.match(source,/设为机器人默认/);
  assert.match(source,/声纹注册/);
  assert.match(source,/\/api\/speakers/);
  assert.match(source,/getUserMedia/);
  assert.match(source,/上传音频/);
  assert.match(source,/decodeAudioData/);
  assert.match(source,/所有机器人都会认识这个名字/);
  assert.doesNotMatch(source,/embedding|voiceprint_audio_urls/);
});

test('voice preview is separate from Room playback',()=>{
  assert.doesNotMatch(source,/RoomSpeakerPanel|reb_tts|new WebSocket/);
  assert.match(source,/试听独立播放|独立试听/);
});
