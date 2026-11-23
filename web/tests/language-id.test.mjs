import test from 'node:test';
import assert from 'node:assert/strict';
import {languagePill} from '../src/language-id.ts';

const base={state:'online',heartbeat_age_ms:800,reason:null,device:'cuda',queue:0,count:3,latest_age_ms:5_000,
  latest:{label:'zh yue',lang:'zh',dialect:'yue',name:'粤语',confidence:0.999,duration_s:3.12,latency_ms:420,seq:3,robot_id:'annie-a'}};

test('the latest heard language shows with its confidence; details on hover',()=>{
  const pill=languagePill(base);
  assert.equal(pill.text,'粤语 100%');
  assert.equal(pill.tone,'good');
  assert.match(pill.title,/最近一句：粤语（zh yue）· 把握 99\.9% · 3\.12 秒的话 · 0\.4 秒出结果 · 5 秒前/);
  assert.match(pill.title,/设备：cuda · 排队 0 · 已识别 3 句/);
});

test('service states are shown instead of a stale or missing result',()=>{
  assert.equal(languagePill({...base,latest_age_ms:11*60_000}).text,'在线');
  assert.deepEqual([languagePill({...base,state:'waiting',latest:null}).text,languagePill({...base,state:'offline'}).tone],['未接入','bad']);
  assert.equal(languagePill({...base,state:'no_mic'}).text,'麦克风未连');
  assert.equal(languagePill(null,true).text,'离线');
});

test('online but no sound from the robot microphone says so instead of just "在线"',()=>{
  const idle={...base,latest:null,latest_age_ms:null,mic_fps:0,echo_skipped:0};
  const pill=languagePill(idle);
  assert.deepEqual([pill.text,pill.tone],['没收到麦克风声音','warn']);
  assert.match(pill.title,/麦克风：0 帧\/秒（正常约 50） · 机器人的麦克风没有把声音送进 Room/);
  assert.equal(languagePill({...idle,mic_fps:49.8}).text,'在线');
  assert.equal(languagePill({...base,mic_fps:0}).text,'粤语 100%');  // a recent result still shows
});

test('her own voice that was skipped as echo is counted in the details',()=>{
  const pill=languagePill({...base,mic_fps:50,echo_skipped:3});
  assert.match(pill.title,/已跳过 3 句她自己的声音（回声）/);
  assert.doesNotMatch(languagePill({...base,echo_skipped:0}).title,/回声/);
});
