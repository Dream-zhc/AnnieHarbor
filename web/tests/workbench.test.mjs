import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile,access} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import http from 'node:http';
import {api,robotPath,sessionKey,eventBelongsToSession} from '../src/api.ts';
import {pcmFrame} from '../src/pcm-format.ts';
import {createMicPcmPump} from '../src/mic-pump.ts';

const web=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');

test('robot and session routing preserves all isolation dimensions',()=>{
  assert.equal(robotPath('annie/b'),'/api/robots/annie%2Fb');
  const a={robot_id:'a',chat_id:'same',persona_id:'same',session_epoch:4,generation:5};
  assert.notEqual(sessionKey(a),sessionKey({...a,robot_id:'b'}));
  assert.notEqual(sessionKey(a),sessionKey({...a,generation:6}));
  assert.notEqual(sessionKey(a),sessionKey({...a,chat_id:'other'}));
});

test('Realtime stream is visible only within matching robot, chat, persona, epoch and generation',()=>{
  const event={robot_id:'annie-a',chat_id:'same',persona_id:'calm',session_epoch:7,generation:9};
  assert.equal(eventBelongsToSession(event,event),true);
  for(const [key,value] of Object.entries({
    robot_id:'annie-b',chat_id:'other',persona_id:'lively',session_epoch:8,generation:10,
  })){
    assert.equal(eventBelongsToSession({...event,[key]:value},event),false,key);
  }
});

test('same-origin API sends exact scoped payload and shows server errors',async()=>{
  const requests=[];
  const server=http.createServer(async(req,res)=>{
    let body='';for await(const part of req)body+=part;
    requests.push([req.method,req.url,body]);
    res.setHeader('content-type','application/json');
    if(req.url==='/health'){res.statusCode=503;res.end(JSON.stringify({ready:false,robots:[]}));}
    else if(body.includes('invalid')){res.statusCode=422;res.end(JSON.stringify({detail:'Bad text'}));}
    else res.end(JSON.stringify({reply:'<script>escaped by React</script>'}));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const root=`http://127.0.0.1:${server.address().port}`;
  try{
    assert.deepEqual(await api(root+'/health','GET',undefined,undefined,[503]),{ready:false,robots:[]});
    assert.equal((await api(root+'/api/robots/annie-b/turns','POST',{text:'hello'})).reply,'<script>escaped by React</script>');
    assert.deepEqual(requests[1],['POST','/api/robots/annie-b/turns','{"text":"hello"}']);
    await assert.rejects(api(root+'/api/robots/annie-a/turns','POST',{text:'invalid'}),/422.*Bad text/);
  }finally{await new Promise(resolve=>server.close(resolve));}
});

test('Room PCM format retains Phase 2 MIC_AUDIO wire contract',()=>{
  const data=new Uint8Array(640);data[0]=42;data[639]=99;
  const frame=pcmFrame(data);
  assert.equal(frame.length,2+9+640);
  assert.deepEqual(Array.from(frame.slice(0,2)),[0,9]);
  assert.equal(new TextDecoder().decode(frame.slice(2,11)),'MIC_AUDIO');
  assert.equal(frame[11],42);assert.equal(frame.at(-1),99);
  assert.throws(()=>pcmFrame(new Uint8Array(639)),/640B/);
});

test('live microphone pump emits actual 20ms Room frames across audio callbacks',()=>{
  for(const rate of [16000,44100,48000]){
    const pump=createMicPcmPump(),packets=[];
    let count=0;
    // Exercise the browser callback with silence too: no VAD gate drops packets.
    for(let i=0;i<100;i++){
      count=pump(new Float32Array(1024),rate,frame=>packets.push(frame));
    }
    const expected=Math.floor((1024*100/rate)*16000/320);
    assert.ok(Math.abs(count-expected)<=1,`rate=${rate} count=${count} expected≈${expected}`);
    assert.equal(packets.length,count);
    for(const frame of packets){
      assert.equal(frame.length,651);
      assert.equal(new TextDecoder().decode(frame.slice(2,11)),'MIC_AUDIO');
      assert.ok(frame.slice(11).every(sample=>sample===0));
    }
  }
});

test('microphone PCM pump retains nonzero signal at 16k and 48k',()=>{
  for(const rate of [16000,48000]){
    const pump=createMicPcmPump(),packets=[];
    for(let i=0;i<12;i++){
      const wave=Float32Array.from({length:1024},(_,j)=>
        0.3*Math.sin(2*Math.PI*440*(i*1024+j)/rate));
      pump(wave,rate,frame=>packets.push(frame));
    }
    assert.ok(packets.length>0);
    const view=new DataView(packets[0].buffer,packets[0].byteOffset+11,640);
    assert.ok(Array.from({length:320},(_,i)=>Math.abs(view.getInt16(i*2,true)))
      .some(value=>value>1000),`rate=${rate}: microphone waveform unexpectedly silent`);
  }
});

test('React build is the only production page and serves local assets',async()=>{
  const html=await readFile(path.join(web,'dist/index.html'),'utf8');
  assert.match(html,/<div id="root"><\/div>/);
  assert.match(html,/assets\/index-.*\.js/);
  assert.doesNotMatch(html,/https?:\/\//);
  for(const match of html.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)){
    await access(path.join(web,'dist',match[1]));
  }
  const source=await readFile(path.join(web,'src/workbench.tsx'),'utf8');
  assert.match(source,/session_epoch/);
  assert.match(source,/generation/);
  assert.match(source,/conversation/);
  assert.doesNotMatch(source,/RoomInput|RoomPlaybackTester|Fake debug probe|Fake 部分播放/);
  assert.match(source,/new EventSource/);
  assert.match(source,/realtime\/stream\?after=/);
  assert.doesNotMatch(source,/setInterval\(\(\)=>void poll\(\),250\)/);
  assert.doesNotMatch(source,/RoomPlayback/);
  assert.match(source,/realtime\/text/);
  assert.match(source,/text\.delta/);
  assert.match(source,/transcript\.delta/);
  assert.match(source,/transcript\.done/);
  assert.match(source,/finalTranscript\?\.turnId===turn\.turn_id/);
  assert.match(source,/liveTurn\?\.turnId===turn\.turn_id\?previewTranscript\|\|liveTurn\.userText/);
  assert.match(source,/语音转写中/);
  assert.match(source,/liveTurn\?\.turnId===turn\.turn_id/);
  assert.match(source,/playbackLabel\(turn,speech\?\.state==='not_configured'\)/);
  assert.match(source,/<RoomDebugSpeaker/);
  assert.match(source,/route\?\.urls\?\.tts/);
  assert.match(source,/inspector-panel/);
  assert.match(source,/session\.error/);
  assert.match(source,/turn\.done/);
  assert.match(source,/speech_output/);
  assert.match(source,/session_frames_sent/);
  assert.match(source,/speech_started_count/);
  assert.match(source,/当前语音链路/);
  assert.match(source,/realtime\?\.cascade_llm/);
  assert.match(source,/ASR 暂定/);
  assert.match(source,/ASR 最终/);
  assert.match(source,/网页调试扬声器可逆 Hold\/Resume/);
  assert.match(source,/playback\.resume_requested/);
  assert.match(source,/transcript\.ambient/);
  assert.match(source,/session\.ready/);
  assert.match(source,/realtime\?\.llm_error/);
  assert.match(source,/last_stop_reason/);
  assert.match(source,/playback_evidence/);
  assert.match(source,/phase4c\.html/);
  assert.match(source,/item\.persona_revision/);
  assert.match(source,/className="top-robot-switch"/);
  assert.match(source,/aria-label="切换机器人"/);
  assert.equal((source.match(/className="top-robot-switch"/g)??[]).length,1);
  assert.doesNotMatch(source,/type View=|'overview','chat','robots','developer'/);
  assert.match(source,/className="developer-drawer"/);
  assert.match(source,/historyOpen\?'history-open':'history-closed'/);
  assert.match(source,/aria-label="延迟瀑布图"/);
  assert.match(source,/onLevel=\{setMicLevel\}/);
  assert.match(source,/onBuffer=\{setBrowserBuffer\}/);
  assert.match(source,/api<PersonaProfile\[]>\('\/api\/personas'\)/);
  assert.match(source,/source:'user_setting',generation:session.generation/);
  assert.match(source,/personaForSession\?\.provider_session_id|persona.provider_session_id===realtime\?\.provider_session_id/);
  assert.match(source,/conversationChecks\[checkIndex\]\.turns\[checkStep\]/);
  assert.match(await readFile(path.join(web,'src/styles.css'),'utf8'),
    /grid-template-columns:minmax\(0,65fr\) minmax\(300px,35fr\)/);
  assert.doesNotMatch(source,/<Block title="能力状态"|<Block title="Room 六通道"/);
  assert.match(source,/cancelCurrent/);
  assert.match(source,/prefers-reduced-motion/);
  assert.match(source,/useGSAP/);
  assert.match(source,/chat-layout/);
  assert.match(source,/followLatest/);
  assert.match(source,/role="dialog"/);
  assert.doesNotMatch(source,/\.innerHTML\s*=/);
});

test('remaining protocol fixtures stay separate from the production conversation',async()=>{
  const htmlRoot=path.resolve(web,'../tests/html');
  const [phase1,phase2,phase3]=await Promise.all(
    ['phase1.html','phase2.html','phase3.html'].map(file=>readFile(path.join(htmlRoot,file),'utf8'))
  );
  assert.match(phase1,/concurrent-all/);
  assert.match(phase2,/mic-start/);
  assert.match(phase3,/simulated_partial/);
});

test('real conversation has an explicitly authorized browser microphone and no test PCM control',async()=>{
  const workbench=await readFile(path.join(web,'src/workbench.tsx'),'utf8');
  const mic=await readFile(path.join(web,'src/microphone.tsx'),'utf8');
  assert.match(workbench,/<RealtimeMicrophone key=\{robotId/);
  assert.match(workbench,/route\?\.room_id\?\?''/);
  assert.match(workbench,/route=\{realtime\?\.ready\?route:undefined\}/);
  assert.doesNotMatch(workbench,/RoomPlayback|AudioWorklet/);
  assert.match(mic,/getUserMedia/);
  assert.match(mic,/room-capture-worklet\.js/);
  assert.match(mic,/new AudioWorkletNode\(context,'room-capture'/);
  assert.doesNotMatch(mic,/createScriptProcessor|onaudioprocess/);
  assert.match(mic,/message\.robotId!=='reb_mic'/);
  assert.match(mic,/window\.addEventListener\('pagehide'/);
  assert.match(mic,/Room 发送积压超过 800ms/);
  assert.match(mic,/const captureWs=ws,captureContext=context,captureStream=stream/);
  assert.match(mic,/captureWs\.send\(pcmFrame\(new Uint8Array\(event\.data\.buffer\)\)\)/);
  assert.match(mic,/handedOff=true/);
  assert.doesNotMatch(mic,/ws=undefined;stream=undefined;context=undefined/);
  assert.match(mic,/3 秒未收到麦克风音频回调/);
  assert.match(mic,/取消开启/);
  assert.match(mic,/此 Room 没有另一个麦克风/);
  assert.doesNotMatch(mic,/测试 PCM|sendFrame|new Uint8Array\(640\)\)\)/);
});
