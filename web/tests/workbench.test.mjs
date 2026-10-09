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

test('production panels use compact labels rather than repeated explanatory copy',async()=>{
  const files=['workbench.tsx','memory-panel.tsx','developer-drawer.tsx',
    'action-panel.tsx','external-events.tsx','audio-panels.tsx','latency-hud.tsx'];
  const source=(await Promise.all(files.map(file=>readFile(path.join(web,'src',file),'utf8')))).join('\n');
  for(const verbose of [
    '公共偏好属于此机器人；角色关系和经历属于当前 Persona',
    '当前 Persona 的关系事实与共同经历，跨其 Chat 可用',
    '仅改变当前 Chat 的风格，保留原有历史和身份',
    '旧版抽取空结果，不参与记忆注入或召回；可点击',
    '单次任务持久化到本机 SQLite；到期提交模型',
    '通过当前机器人实时对话触发动作；此页不直接控制电机',
    '网页调试扬声器可逆 Hold/Resume',
  ])assert.ok(!source.includes(verbose),`unnecessary UI explanation: ${verbose}`);
  assert.match(source,/发至 Room 即完成/);
  assert.match(source,/删除后无法恢复/);
});

test('memory panel separates public, persona-only and current-chat records',async()=>{
  const source=await readFile(path.join(web,'src/memory-panel.tsx'),'utf8');
  assert.match(source,/长期通用/);
  assert.match(source,/当前角色/);
  assert.match(source,/本次会话/);
  assert.match(source,/visibleMemory\(item,scope,Date\.now\(\)\)/);
  assert.match(source,/scope==='chat'\?'\/chat-memories':'\/memories'/);
  assert.match(source,/creating&&<div className="memory-create-backdrop"/);
  assert.match(source,/scope==='persona'&&<div className="memory-form-row">/);
  assert.match(source,/activeCount=valid\.length/);
  assert.doesNotMatch(source,/memory-key-badge/);
});

test('memory management remains reachable in the active workbench',async()=>{
  const source=await readFile(path.join(web,'src/workbench.tsx'),'utf8');
  assert.match(source,/import \{MemoryPanel\} from '\.\/memory-panel'/);
  assert.match(source,/onClick=\{\(\)=>setMemoryOpen\(true\)\}/);
  assert.match(source,/memoryOpen&&session&&<MemoryPanel/);
  assert.match(source,/chatId=\{session\.chat_id\} generation=\{session\.generation\}/);
});

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

test('internal prompts do not render user bubbles, including live turns',async()=>{
  const workbench=await readFile(path.join(web,'src/workbench.tsx'),'utf8');
  const diagnostics=await readFile(path.join(web,'src/turn-diagnostics.tsx'),'utf8');
  assert.match(workbench,/turn\.turn_source!=='external'&&<div className="message-row me"/);
  assert.match(workbench,/liveTurn\.source!=='external'&&<div className="message-row me"/);
  assert.match(workbench,/source:item\.turn_source==='external'/);
  assert.match(diagnostics,/turn\.external_prompt/);
  assert.match(diagnostics,/内部触发（非用户发言）/);
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
  const workbench=await readFile(path.join(web,'src/workbench.tsx'),'utf8');
  const drawer=await readFile(path.join(web,'src/developer-drawer.tsx'),'utf8');
  const source=workbench+'\n'+drawer;
  const audioPanels=await readFile(path.join(web,'src/audio-panels.tsx'),'utf8');
  const latencyHud=await readFile(path.join(web,'src/latency-hud.tsx'),'utf8');
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
  assert.match(audioPanels,/<RoomDebugSpeaker/);
  assert.match(source,/route\?\.urls\?\.tts/);
  assert.match(source,/inspector-panel/);
  assert.match(source,/session\.error/);
  assert.match(source,/turn\.done/);
  assert.match(source,/speech_output/);
  assert.match(source,/session_frames_sent/);
  assert.match(source,/speech_started_count/);
  assert.match(source,/当前语音链路/);
  assert.match(source,/Qwen-Audio 3\.0 Realtime Flash/);
  assert.match(source,/ASR 暂定/);
  assert.match(source,/ASR 最终/);
  assert.match(source,/playback\.hold_requested/);
  assert.match(source,/playback\.resume_requested/);
  assert.match(source,/transcript\.ambient/);
  assert.match(source,/transcript\.ambient_delta/);
  assert.match(source,/transcript\.final_preview/);
  assert.match(source,/transcript\.first_delta/);
  assert.match(source,/setPreviewTranscript\(\(item\.text\?\?'\'\)\+\(item\.stash\?\?'\'\)\)/);
  assert.match(source,/云端起声 → 首次 ASR 增量/);
  assert.match(await readFile(path.join(web,'src/microphone.tsx'),'utf8'),
    /aria-label="麦克风增强"/);
  assert.match(source,/session\.ready/);
  assert.doesNotMatch(source,/cascade_llm|last_llm_input|llm_error/);
  assert.match(source,/last_stop_reason/);
  assert.match(source,/playback_evidence/);
  assert.doesNotMatch(source,/phase4c\.html|\/phase-tests\//);
  assert.match(source,/item\.persona_revision/);
  assert.match(source,/className="top-robot-switch"/);
  assert.match(source,/aria-label="切换机器人"/);
  assert.equal((source.match(/className="top-robot-switch"/g)??[]).length,1);
  assert.doesNotMatch(source,/type View=|'overview','chat','robots','developer'/);
  assert.match(source,/className="developer-drawer"/);
  assert.match(source,/historyOpen\?'history-open':'history-closed'/);
  assert.match(latencyHud,/aria-label="延迟瀑布图"/);
  assert.match(audioPanels,/onLevel=\{setLevel\}/);
  assert.match(audioPanels,/onBuffer=\{setBrowserBuffer\}/);
  assert.doesNotMatch(source,/setMicLevel|setBrowserBuffer|setMicDiagnostics/);
  assert.match(source,/api<PersonaProfile\[]>\('\/api\/personas'\)/);
  assert.match(source,/source:'user_setting',generation:session.generation/);
  assert.match(source,/aria-label="切换当前会话性格"/);
  assert.match(source,/fields:\{persona_style:styleId\}/);
  assert.match(source,/personaForSession.style_id===savedStyle/);
  assert.match(source,/if\(item.kind==='session.ready'\)\{/);
  assert.match(source,/personaForSession\?\.provider_session_id|persona.provider_session_id===realtime\?\.provider_session_id/);
  assert.match(source,/conversationChecks\[checkIndex\]\.turns\[checkStep\]/);
  assert.match(await readFile(path.join(web,'src/styles.css'),'utf8'),
    /grid-template-columns:minmax\(0,1fr\) minmax\(260px,290px\)/);
  assert.match(source,/↓ 回到最新/);
  assert.match(source,/onWheel=\{e=>\{if\(e.deltaY<0\)/);
  assert.match(source,/className="voice-mode"/);
  assert.doesNotMatch(source,/<Block title="能力状态"|<Block title="Room 六通道"/);
  assert.match(source,/cancelCurrent/);
  assert.match(source,/prefers-reduced-motion/);
  assert.match(source,/useGSAP/);
  assert.match(source,/chat-layout/);
  assert.match(source,/followLatest/);
  assert.match(source,/role="dialog"/);
  assert.doesNotMatch(source,/\.innerHTML\s*=/);
});

test('production drawer has no obsolete phase test links',async()=>{
  const drawer=await readFile(path.join(web,'src/developer-drawer.tsx'),'utf8');
  assert.doesNotMatch(drawer,/\/phase-tests\//);
});

test('real conversation has an explicitly authorized browser microphone and no test PCM control',async()=>{
  const workbench=await readFile(path.join(web,'src/workbench.tsx'),'utf8');
  const mic=await readFile(path.join(web,'src/microphone.tsx'),'utf8');
  assert.match(workbench,/<MicrophoneControls key=\{robotId/);
  assert.match(await readFile(path.join(web,'src/audio-panels.tsx'),'utf8'),
    /<RealtimeMicrophone route=\{route\}/);
  assert.match(workbench,/route\?\.room_id\?\?''/);
  assert.match(workbench,/route=\{realtime\?\.ready\?route:undefined\}/);
  assert.doesNotMatch(workbench,/RoomPlayback|AudioWorklet/);
  assert.match(mic,/getUserMedia/);
  assert.match(mic,/enumerateDevices\(\)/);
  assert.match(mic,/item\.kind==='audioinput'/);
  assert.match(mic,/aria-label="选择麦克风输入设备"/);
  assert.match(mic,/deviceId:\{exact:selectedInput\}/);
  assert.match(mic,/track\?\.getSettings\(\)/);
  assert.match(mic,/track\.onmute/);
  assert.match(mic,/frames>100&&level<0\.1/);
  assert.match(mic,/echoCancellation:!rawDiagnostic/);
  assert.match(mic,/noiseSuppression:!rawDiagnostic/);
  assert.match(mic,/原始收音排障/);
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
