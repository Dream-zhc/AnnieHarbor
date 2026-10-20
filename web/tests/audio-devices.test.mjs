import test from 'node:test';
import assert from 'node:assert/strict';
import {audioDevices,browserInstanceId,conflictMessages,handoverEventMessage,handoverMessage,headLabel,
  HANDOVER_NOTICE_MS} from '../src/audio-devices.ts';

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

test('a handover names who took over and who stopped, then fades from the banner',()=>{
  const at=new Date(2026,9,20,15,40).getTime();
  const head={current:{instance_id:'head-big',head_name:'大头机器人',address:'192.168.1.188'},heads:[],
    handover:{from:{head_name:'obj 娃娃小机器人',address:'192.168.1.143'},
      to:{head_name:'大头机器人',address:'192.168.1.188'},at_ms:at,age_ms:5_000}};
  assert.equal(headLabel(head.current),'大头机器人（192.168.1.188）');
  assert.equal(handoverMessage(head),
    '15:40 大头机器人（192.168.1.188）后启动，已接管麦克风和扬声器；obj 娃娃小机器人（192.168.1.143）已自动断开。');
  assert.equal(handoverMessage({...head,handover:{...head.handover,age_ms:HANDOVER_NOTICE_MS+1}}),null);
  assert.equal(handoverMessage(undefined),null);
  assert.equal(handoverEventMessage({from_name:'obj 娃娃小机器人',to_name:'大头机器人',to_address:'192.168.1.188'}),
    '大头机器人（192.168.1.188）后启动，已接管；obj 娃娃小机器人已自动断开');
});

test('a board device names its head instead of the host name',()=>{
  const devices=conflict([{role:'speaker',reason:'multiple_devices',count:2}],{speakers:[
    {instance_id:'speaker-a',host:'ubuntu',device_name:'CD002-AUDIO',kind:'device',head_name:'大头机器人'},
    {instance_id:'speaker-b',host:'ubuntu',device_name:'CD002-AUDIO',kind:'device'}]});
  assert.match(conflictMessages(devices,'roomv2')[0],/CD002-AUDIO（大头机器人）、CD002-AUDIO（ubuntu）/);
});

test('face registration by conversation shows one workbench notice per step',async()=>{
  const {faceNotice}=await import('../src/notices.ts');
  assert.deepEqual(faceNotice({kind:'face.registration.started',name:'小明'}),
    {text:'正在记住 小明 的脸，请 TA 正对摄像头',type:'info'});
  assert.deepEqual(faceNotice({kind:'face.registration.failed',name:'小明',reason:'timeout',saved:3,target_samples:18}),
    {text:'没能记住 小明 的脸（超时，没采到清楚的正脸），采到 3/18',type:'error'});
  assert.equal(faceNotice({kind:'face.registration.progress',name:'小明'}),null);
  assert.equal(faceNotice({kind:'text.delta'}),null);
});

test('a volume change by voice is one workbench notice',async()=>{
  const {volumeNotice}=await import('../src/notices.ts');
  assert.deepEqual(volumeNotice({kind:'speaker.volume.changed',previous:100,volume:80,status:'set'}),
    {text:'扬声器音量 100 → 80',type:'info'});
  assert.equal(volumeNotice({kind:'speaker.volume.changed',volume:80,status:'unconfirmed'})?.type,'error');
  assert.equal(volumeNotice({kind:'text.delta'}),null);
});
