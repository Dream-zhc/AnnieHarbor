import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {cameOnline,isTalking,latestOnline,robotName,robotPresence,TALKING_WINDOW_MS} from '../src/robot-presence.ts';

const NOW=1_800_000_000_000;
const head=(instance,name,activeForMs)=>({instance_id:instance,head_id:instance,head_name:name,
  host:'board',address:null,active_for_ms:activeForMs,age_ms:500});
const robot=(id,fields={})=>({robot_id:id,room_id:'room-'+id,persona_id:'annie',state:'ready',head:null,...fields});
const big=(fields={})=>robot('annie-a',{head:head('big-1','大头机器人',3_600_000),...fields});
const small=(fields={})=>robot('annie-small',{head:head('small-1','小头机器人',5_000),...fields});
const ready=(fields={})=>({ready:true,state:'connected',response_id:null,speech_active:false,
  last_provider_speech_ms:null,last_provider_transcript_ms:null,speech_output:{state:'idle'},...fields});

test('she is talking while answering, hearing speech, playing TTS or shortly after someone spoke',()=>{
  assert.equal(isTalking(robot('a',{realtime:ready({response_id:'resp-1'})}),NOW),true);
  assert.equal(isTalking(robot('a',{realtime:ready({speech_active:true})}),NOW),true);
  assert.equal(isTalking(robot('a',{realtime:ready({speech_output:{state:'active'}})}),NOW),true);
  assert.equal(isTalking(robot('a',{realtime:ready({last_provider_transcript_ms:NOW-10_000})}),NOW),true);
  assert.equal(isTalking(robot('a',{realtime:ready({last_provider_speech_ms:NOW-TALKING_WINDOW_MS-1})}),NOW),false);
  assert.equal(isTalking(robot('a',{realtime:ready()}),NOW),false);
  assert.equal(isTalking(robot('a',{realtime:ready({ready:false,response_id:'resp-1'})}),NOW),false);
  assert.equal(isTalking(robot('a'),NOW),false);
});

test('the board that came online last is marked only while two or more boards are online',()=>{
  const robots=[big(),robot('annie-b'),small()];
  assert.equal(latestOnline(robots)?.robot_id,'annie-small');
  const presence=robotPresence(robots,NOW);
  assert.deepEqual([...presence.values()].map(item=>[item.online,item.latest]),
    [[true,false],[false,false],[true,true]]);
  assert.match(presence.get('annie-small').title,/板子在线：小头机器人/);
  assert.match(presence.get('annie-small').title,/后上线/);
  assert.match(presence.get('annie-b').title,/^板子离线/);
  assert.equal(robotPresence([big(),robot('annie-small')],NOW).get('annie-a').latest,false);
  assert.equal(latestOnline([robot('annie-a'),robot('annie-small')]),null);
});

test('the button title lists the switches as they took effect',()=>{
  const switches={wake:false,aec:true,mouth:false,camera:true,
    actions:['shake_head','look_left','look_right','fast_double_blink','wink_left','wink_right']};
  const title=robotPresence([small({switches})],NOW).get('annie-small').title;
  assert.match(title,/唤醒词关，回声消除开，嘴型关，摄像头开，动作 6 个/);
});

test('the workbench follows a board only when it comes online after the others',()=>{
  let state=cameOnline(null,[big(),robot('annie-small')]);
  assert.equal(state.robot,null,'the page just opened: nothing counts as new');

  state=cameOnline(state.seen,[big(),small()]);
  assert.equal(state.robot?.robot_id,'annie-small','the small head powered on later');
  assert.equal(robotName(state.robot),'小头机器人');

  state=cameOnline(state.seen,[big(),small({head:head('small-1','小头机器人',11_000)})]);
  assert.equal(state.robot,null,'later polls do not pull a view switched back by hand');

  state=cameOnline(state.seen,[big(),robot('annie-small')]);
  assert.equal(state.robot,null,'the small head powered off; the big head did not come online');

  state=cameOnline(state.seen,[big(),small({head:head('small-2','小头机器人',2_000)})]);
  assert.equal(state.robot?.robot_id,'annie-small','a rebooted board comes online again');

  state=cameOnline(state.seen,[big({head:head('big-2','大头机器人',60_000)}),
    small({head:head('small-2','小头机器人',8_000)})]);
  assert.equal(state.robot,null,'a new board online longer than another one did not come online later');
});

test('robot buttons show the board state and the workbench says where it switched',async()=>{
  const source=await readFile(new URL('../src/workbench.tsx',import.meta.url),'utf8');
  assert.match(source,/cameOnline\(seenHeads\.current,health\.robots\)/);
  assert.match(source,/后上线，已切到/);
  assert.match(source,/robot-pill-tag talking/);
  assert.match(source,/robot-pill-tag latest/);
  assert.match(source,/latestOnline\(result\.robots\)\?\?result\.robots\[0\]/);
});
