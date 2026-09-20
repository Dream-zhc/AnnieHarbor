import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile,access} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import http from 'node:http';
import {api,robotPath,sessionKey,eventBelongsToSession} from '../src/api.ts';
import {pcmFrame} from '../src/pcm-format.ts';

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
  assert.match(source,/realtime\/events\?after=/);
  assert.match(source,/realtime\/text/);
  assert.match(source,/text\.delta/);
  assert.match(source,/session\.error/);
  assert.match(source,/turn\.done/);
  assert.match(source,/speech_output/);
  assert.match(source,/playback_evidence/);
  assert.match(source,/phase4b\.html/);
  assert.match(source,/phase4c\.html/);
  assert.match(source,/item\.persona_revision/);
  assert.match(source,/className="top-robot-switch"/);
  assert.match(source,/view!=='overview'/);
  assert.equal((source.match(/<select\b/g)??[]).length,1);
  assert.match(source,/className="stats-grid overview-stats"/);
  assert.doesNotMatch(source,/<Block title="能力状态"|<Block title="Room 六通道"/);
  assert.match(source,/cancelCurrent/);
  assert.match(source,/prefers-reduced-motion/);
  assert.match(source,/useGSAP/);
  assert.match(source,/chat-layout/);
  assert.match(source,/followLatest/);
  assert.match(source,/role="dialog"/);
  assert.doesNotMatch(source,/\.innerHTML\s*=/);
});

test('each earlier test remains a standalone HTML page with only explicit test controls',async()=>{
  const htmlRoot=path.resolve(web,'../tests/html');
  const [phase1,phase2,phase3,phase4b]=await Promise.all(
    ['phase1.html','phase2.html','phase3.html','phase4b.html'].map(file=>readFile(path.join(htmlRoot,file),'utf8'))
  );
  assert.match(phase1,/concurrent-all/);
  assert.match(phase2,/mic-start/);
  assert.match(phase3,/simulated_partial/);
  assert.match(phase4b,/browser_test/);
  assert.match(phase4b,/route\.urls\.tts/);
  assert.match(phase4b,/route\.urls\.main/);
  assert.match(phase4b,/route\.urls\.sam/);
  assert.match(phase4b,/played_sample_count/);
  assert.match(phase4b,/stream_final/);
  assert.match(phase4b,/new AudioContext/);
  assert.doesNotMatch(phase4b,/source:'speaker'/);
  const script=phase4b.match(/<script>([\s\S]*?)<\/script>/)?.[1];
  assert.ok(script);
  assert.doesNotThrow(()=>new Function(script));
});
