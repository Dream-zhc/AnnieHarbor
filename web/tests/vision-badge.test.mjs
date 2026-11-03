import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {isVisionEvent,turnVision,visionBadge} from '../src/vision-badge.ts';

const web=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
let seq=0;
const event=(kind,fields={})=>({seq:++seq,kind,robot_id:'a',chat_id:'c',persona_id:'p',
  session_epoch:1,generation:1,turn_id:'t1',...fields});

test('a look by the vision model shows its route, model and outcome',()=>{
  const vision=turnVision('t1',[
    event('vision.intent.started',{script:'look_left',text:'左边有什么'}),
    event('vision.intent.result',{status:'ok',route:'cloud',model:'qwen-vl-max',chars:56,total_ms:1800}),
  ]);
  assert.equal(vision.state,'seen');
  const badge=visionBadge(vision);
  assert.equal(badge.label,'看了画面');
  assert.equal(badge.icon,'👁');
  assert.match(badge.title,/请视觉模型看画面：云端 · qwen-vl-max/);
  assert.match(badge.title,/描述 56 字/);
  assert.match(badge.title,/共 1\.8 秒/);
});

test('until the vision model answers the badge says she is looking',()=>{
  const vision=turnVision('t1',[event('vision.intent.started')]);
  assert.equal(vision.state,'looking');
  assert.equal(visionBadge(vision).label,'看画面中…');
});

test('a failed or abandoned look says why',()=>{
  const timedOut=turnVision('t1',[event('vision.intent.started'),
    event('vision.intent.result',{status:'timeout',route:'cloud'})]);
  assert.equal(timedOut.state,'failed');
  assert.equal(visionBadge(timedOut).label,'没看成');
  assert.match(visionBadge(timedOut).title,/没看成：视觉端没按时回/);
  const cloudSlow=turnVision('t1',[event('vision.intent.started'),
    event('vision.intent.result',{status:'failed',error:'cloud_timeout',route:'cloud'})]);
  assert.match(visionBadge(cloudSlow).title,/没看成：云端视觉超时/);
  const dropped=turnVision('t1',[event('vision.intent.started'),
    event('vision.intent.abandoned',{reason:'turn_changed'})]);
  assert.equal(dropped.state,'failed');
  assert.match(visionBadge(dropped).title,/没看成：用户又说了话/);
});

test('a frame sent only to the look decision is marked too',()=>{
  const judged=turnVision('t1',[event('vision.intent.decided',
    {image:true,judge:'d3',model:'d3-vl',frame_age_ms:300,choice:'none'})]);
  assert.equal(judged.state,'judged');
  const badge=visionBadge(judged);
  assert.equal(badge.label,'带图判断');
  assert.equal(badge.icon,'🖼');
  assert.match(badge.title,/判断要不要看时带了摄像头画面（d3-vl，画面 300 ms 前） · 这次没去看/);
  const looked=turnVision('t1',[event('vision.intent.decided',{image:true,model:'d3-vl'}),
    event('vision.intent.started')]);
  assert.equal(looked.state,'looking');
  assert.match(visionBadge(looked).title,/带了摄像头画面（d3-vl）/);
});

test('no picture sent, another turn or no turn means no badge',()=>{
  assert.equal(turnVision('t1',[event('vision.intent.decided',{image:false})]),null);
  assert.equal(turnVision('t2',[event('vision.intent.started')]),null);
  assert.equal(turnVision(undefined,[event('vision.intent.started')]),null);
  assert.equal(isVisionEvent('vision.context.sent'),false);
});

test('the same event from the stream and from diagnostics counts once',()=>{
  const started=event('vision.intent.started');
  const result=event('vision.intent.result',{status:'ok',chars:12});
  const vision=turnVision('t1',[started,result],{turn_events:[started,result]});
  assert.equal(vision.state,'seen');
  assert.equal(vision.chars,12);
});

test('the workbench keeps vision events and badges history and live turns',async()=>{
  const source=await readFile(path.join(web,'src','workbench.tsx'),'utf8');
  assert.match(source,/isVisionEvent\(item\.kind\)/);
  assert.match(source,/turnVision\(turn\.turn_id,realtimeEvents,diagnostics\)/);
  assert.match(source,/turnVision\(liveTurn\.turnId,realtimeEvents,diagnostics\)/);
  assert.equal(source.match(/<VisionBadge vision=/g)?.length,2);
});
