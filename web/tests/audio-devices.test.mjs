import test from 'node:test';
import assert from 'node:assert/strict';
import {audioDevices,browserInstanceId,conflictMessages} from '../src/audio-devices.ts';

const conflict=(conflicts,extra={})=>({status:'conflict',conflicts,microphones:[],speakers:[],...extra});

test('names both microphones so people know which one to close',()=>{
  const devices=conflict([{role:'microphone',reason:'multiple_devices',count:2}],{microphones:[
    {instance_id:'microphone-a',host:'rdk-x5',device_name:'USB 阵列',kind:'device'},
    {instance_id:'browser-microphone-b',host:'browser',device_name:'MacBook 麦克风',kind:'browser'}]});
  assert.deepEqual(conflictMessages(devices,'roomv2'),
    ['roomv2 同时有 2 个麦克风在工作：USB 阵列（rdk-x5）、MacBook 麦克风（网页），请只保留一个。']);
});

test('old microphones are explained by their frame rate',()=>{
  assert.match(conflictMessages(conflict([{role:'microphone',reason:'frame_rate',frames_per_second:100}]),'roomv3')[0],
    /roomv3 的麦克风音频约 100 帧\/秒（一个麦克风是 50）/);
});

test('two players of one reply name the web audition',()=>{
  assert.equal(conflictMessages(conflict([{role:'speaker',reason:'multiple_playbacks',count:2,
    sources:['browser_test','speaker']}]),'roomv2')[0],
  'roomv2 同一段回复有 2 个扬声器在播放（网页试听、实体扬声器），请只保留一个。');
});

test('no warning when ok or when an older backend has no device state',()=>{
  assert.deepEqual(conflictMessages({status:'ok',conflicts:[],microphones:[],speakers:[]}),[]);
  assert.equal(audioDevices({accepted:3}),null);
  assert.deepEqual(conflictMessages(audioDevices(null)),[]);
  assert.equal(audioDevices({audio_devices:{status:'ok',conflicts:[]}}).microphones.length,0);
});

test('browser instance ids are per tab and marked as browser',()=>{
  const a=browserInstanceId('microphone'),b=browserInstanceId('microphone');
  assert.match(a,/^browser-microphone-[a-z0-9]+$/);
  assert.notEqual(a,b);
});
