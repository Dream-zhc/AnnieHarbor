// Optional Chromium integration: actual React + AudioWorklet and mock Room WS.
// No packets are sent to a physical robot. Run against a local Runtime server.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {realpathSync} from 'node:fs';
import {createRequire} from 'node:module';
import {dirname} from 'node:path';

const require=createRequire(import.meta.url);
let chromium;
try{({chromium}=require('playwright'));}
catch{
  const binary=execFileSync('which',['playwright'],{encoding:'utf8'}).trim();
  ({chromium}=require(dirname(realpathSync(binary))));
}

const browser=await chromium.launch({headless:true});
try{
  const page=await browser.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.addInitScript(()=>{
    window.__receipts=[];
    window.__sockets=[];
    window.WebSocket=class{
      static OPEN=1;
      constructor(url){
        this.url=url;this.readyState=0;window.__sockets.push(this);
        const parts=/\/robot\/([^/]+)\/(reb_[^/]+)$/.exec(url);
        this.room=parts?.[1];this.channel=parts?.[2];
        setTimeout(()=>{
          if(this.readyState!==0)return;
          this.readyState=1;
          this.onmessage?.({data:JSON.stringify({
            type:'WELCOME',ok:true,room:this.room,robotId:this.channel,
          })});
        },0);
      }
      send(payload){
        const p=JSON.parse(payload);
        if(p.type==='SUBSCRIBE'){
          setTimeout(()=>this.onmessage?.({data:JSON.stringify({
            type:'ACK',data:{what:'subscribed',subscriptions:['*']},
          })}),0);
        }
        if(p.type?.startsWith('tts.playback.'))window.__receipts.push(p);
      }
      close(){this.readyState=3;}
    };
  });
  await page.goto(process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:8780/');
  const controls=page.locator('.room-playback');
  await controls.getByRole('checkbox').check();
  await controls.getByRole('button',{name:'开启本机播放'}).click();
  await page.waitForFunction(()=>document.querySelector('.room-playback .playback-state')
    ?.textContent.includes('已订阅'),null,{timeout:10000});
  const result=await page.evaluate(async()=>{
    const socket=window.__sockets.find(ws=>ws.channel==='reb_tts');
    if(!socket)throw new Error('missing reb_tts');
    const bytes=new Uint8Array(960);
    const view=new DataView(bytes.buffer);
    for(let j=0;j<480;j++)view.setInt16(j*2,Math.round(8000*Math.sin(j*2*Math.PI*440/24000)),true);
    const base64=btoa(String.fromCharCode(...bytes));
    let start=0;
    const emit=()=>socket.onmessage({data:JSON.stringify({
      type:'tts',data:{
        protocol_version:1,format:'pcm_s16le',sample_rate:24000,channels:1,
        conversation_id:'mock-turn',stream_epoch:1,
        start_sample:start,end_sample:start+480,audio:base64,
      },
    })});
    for(let i=0;i<6;i++){emit();start+=480;}
    const interval=setInterval(()=>{
      if(start>=24000){
        clearInterval(interval);
        socket.onmessage({data:JSON.stringify({type:'tts',data:{
          protocol_version:1,format:'pcm_s16le',sample_rate:24000,channels:1,
          conversation_id:'mock-turn',stream_epoch:1,
          start_sample:24000,end_sample:24000,stream_final:true,
        }})});
        return;
      }
      emit();start+=480;
    },20);
    setTimeout(()=>{
      const sam=window.__sockets.find(ws=>ws.channel==='reb_sam');
      sam?.onmessage?.({data:JSON.stringify({
        type:'mouth_script',conversation_id:'mock-turn',stream_epoch:1,
        start_sample:0,end_sample:12000,seq:1,viseme:'a',
        timing_source:'qwen3_forced_aligner_online',
      })});
    },180);
    return 'injected fake PCM into mocked Room only';
  });
  assert.match(result,/mocked Room/);
  await page.waitForFunction(()=>window.__receipts.some(p=>p.type==='tts.playback.drained'),
    null,{timeout:10000});
  const status=await controls.innerText();
  const receipts=await page.evaluate(()=>window.__receipts);
  assert.ok(receipts.some(p=>p.type==='tts.playback.started'));
  assert.ok(receipts.some(p=>p.type==='tts.playback.progress'));
  assert.ok(receipts.some(p=>p.type==='tts.playback.drained'&&p.data.played_sample_count===24000));
  assert.ok(receipts.every(p=>p.data.source==='browser_test'));
  assert.match(status,/已播放：24000 样本/);
  assert.match(status,/嘴型：.*事件 1/);
  assert.match(status,/源峰值：/);
  await controls.getByRole('button',{name:'关闭本机播放'}).click();
  assert.deepEqual(errors,[]);
  console.log('PASS Chromium + React + AudioWorklet; 24000 PCM samples, mouth_script, started/progress/drained; no physical Room writes.');
}finally{await browser.close();}
