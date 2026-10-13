// Production React build, explicit read-only HTTP/SSE fixtures. Never opens a mic.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {dirname} from 'node:path';
import {realpathSync} from 'node:fs';
const require=createRequire(import.meta.url);
let chromium;
try{({chromium}=require('playwright'));}
catch{({chromium}=require(dirname(realpathSync(execFileSync('which',['playwright'],{encoding:'utf8'}).trim()))));}
const base=process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:8780/';
const get=path=>fetch(new URL(path,base)).then(r=>{assert.ok(r.ok);return r.json();});
const health=await get('/health');
const robot=health.robots[0].robot_id;
const session=await get('/api/robots/'+robot+'/session');
const initial=await get('/api/robots/'+robot+'/realtime');
let wake='SLEEP',talking=false,responding=false,output={state:'idle',playback_state:'unknown',playback_source:null};
const readout=()=>({...initial,robot_id:robot,generation:session.generation,ready:true,state:'connected',
  response_id:responding?'fixture-response':null,speech_active:talking,wakeword:{...initial.wakeword,enabled:true,state:wake,error:null,candidate_speech:false},speech_output:output});
const browser=await chromium.launch({headless:true,args:['--use-gl=angle','--use-angle=swiftshader']});
try{
 const page=await browser.newPage({viewport:{width:1440,height:900},reducedMotion:'reduce'});
 const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 await page.addInitScript(()=>{
  window.__podStreams=[];window.__podReads=0;window.__podSeq=0;
  const fetchOriginal=window.fetch.bind(window);
  window.fetch=(...args)=>{if(/\/realtime$/.test(String(args[0])))window.__podReads++;return fetchOriginal(...args);};
  const interval=window.setInterval.bind(window);
  // Suppress only the pod's fallback timer: a passing test must use SSE.
  window.setInterval=(fn,ms,...args)=>ms===950?-950:interval(fn,ms,...args);
  window.EventSource=class{
   constructor(url){this.url=url;this.closed=false;window.__podStreams.push(this);}
   close(){this.closed=true;}
  };
 });
 await page.route('**/*',async route=>{
  const request=route.request(),path=new URL(request.url()).pathname;
  if(request.method()!=='GET')return route.abort();
  if(path==='/api/robots/'+robot+'/realtime')return route.fulfill({json:readout()});
  if(path.endsWith('/social-state'))return route.fulfill({json:{}});
  if(/\/conversations\//.test(path))return route.fulfill({json:{...session,turns:[]}});
  if(path.endsWith('/conversations'))return route.fulfill({json:[]});
  return route.continue();
 });
 await page.goto(new URL('?view=workbench',base).href,{waitUntil:'domcontentloaded'});
 const pod=page.locator('.annie-pod');
 await pod.locator('strong').filter({hasText:'休眠待唤醒'}).waitFor();
 await page.waitForFunction(()=>window.__podStreams.some(s=>!s.closed&&s.url.includes('/realtime/stream')));
 await page.waitForTimeout(100);
 const emit=async(kind,identity=session)=>page.evaluate(({kind,identity})=>{
  const source=window.__podStreams.find(s=>!s.closed&&s.url.includes('/realtime/stream'));
  const seq=++window.__podSeq;
  source.onmessage?.({data:JSON.stringify({events:[{...identity,kind,seq}],latest:seq,gap:false})});
 },{kind,identity});
 const before=await page.evaluate(()=>window.__podReads);
 wake='WAKE_WAIT';await emit('wakeword.detected',{...session,generation:session.generation-1});
 await page.waitForTimeout(100);
 assert.equal(await page.evaluate(()=>window.__podReads),before,'stale generation must not refresh');
 await emit('text.delta');await page.waitForTimeout(100);
 assert.equal(await page.evaluate(()=>window.__podReads),before,'token events must not poll');
 const timings=[];
 for(const [state,kind,label] of [
  ['WAKE_WAIT','wakeword.detected','已唤醒'],
  ['LISTEN','speech.started','正在收听'],
  ['THINK','tts.segment_queued','语音生成中'],
  ['TEXT_RESPONSE','response.created','正在回复'],
  ['TEXT_TTS','tts.segment_queued','语音生成中'],
  ['FOLLOWUP_REPLY','response.created','正在回复'],
  ['SPEAK','tts.playback.started','扬声器播放中'],
  ['FOLLOWUP','tts.playback.drained','等待追问'],
 ]){
  talking=state==='LISTEN';responding=['TEXT_RESPONSE','FOLLOWUP_REPLY'].includes(state);
  wake=['LISTEN','WAKE_WAIT'].includes(state)?'WAKE_WAIT':['FOLLOWUP','FOLLOWUP_REPLY'].includes(state)?'FOLLOWUP':state.startsWith('TEXT_')?'SLEEP':'ACTIVE';
  output={state:['THINK','TEXT_TTS'].includes(state)?'active':'idle',playback_state:state==='SPEAK'?'started':'unknown',playback_source:state==='SPEAK'?'speaker':null};
  const start=performance.now();await emit(kind);
  await pod.locator('strong').filter({hasText:label}).waitFor({timeout:800});
  timings.push({state,fixture_event_to_observation_ms:Math.round(performance.now()-start)});
 }
 if(process.env.ANNIE_POD_SCREENSHOT)await pod.screenshot({path:process.env.ANNIE_POD_SCREENSHOT});
 await page.goto('about:blank');
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({result:'PASS',scope:'Chromium production build with HTTP/SSE fixtures; 950ms timer disabled; no physical microphone',timings},null,2));
}finally{await browser.close();}
