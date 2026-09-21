// Optional Chromium AudioWorklet + fake Room; never touches the real Room.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {realpathSync} from 'node:fs';
import {createRequire} from 'node:module';
import {dirname} from 'node:path';
const require=createRequire(import.meta.url);
let chromium;
try{({chromium}=require('playwright'));}
catch{({chromium}=require(dirname(realpathSync(
  execFileSync('which',['playwright'],{encoding:'utf8'}).trim()))));}
const browser=await chromium.launch({headless:true});
try{
  const page=await browser.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(String(error)));
  await page.addInitScript(()=>{
    window.__ttsSockets=[];window.__outbound=[];
    window.WebSocket=class{
      static OPEN=1;readyState=1;
      constructor(url){
        this.url=url;this.closed=false;window.__ttsSockets.push(this);
        if(!url.endsWith('/reb_tts'))throw Error('unexpected Room socket: '+url);
        setTimeout(()=>this.emit({type:'WELCOME',ok:true,
          room:url.match(/\/robot\/([^/]+)\//)?.[1],robotId:'reb_tts'}),0);
      }
      send(raw){
        const message=JSON.parse(raw);window.__outbound.push(message);
        if(message.type==='SUBSCRIBE')setTimeout(()=>this.emit({type:'ACK',data:{
          what:'subscribed',subscriptions:['TTS','*'],
        }}),0);
      }
      emit(data){if(!this.closed)this.onmessage?.({data:JSON.stringify(data)});}
      close(){this.closed=true;this.readyState=3;}
    };
  });
  await page.goto(process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:8780/',{
    waitUntil:'domcontentloaded',
  });
  await page.getByRole('button',{name:'开启网页调试扬声器'}).click();
  await page.getByText(/已订阅 roomv2 \/ reb_tts，等待新语音/).waitFor();
  const result=await page.evaluate(()=>{
    const socket=window.__ttsSockets.at(-1);
    const pcm=String.fromCharCode(...new Uint8Array(960));
    const data={type:'tts',data:{conversation_id:'test-turn',stream_epoch:1,seq:1,
      start_sample:0,end_sample:480,sample_rate:24000,channels:1,
      format:'pcm_s16le',audio:btoa(pcm),source:'robot_main_brain'}};
    socket.emit(data);socket.emit(data);
    for(let seq=2;seq<=25;seq++)socket.emit({type:'tts',data:{...data.data,
      seq,start_sample:(seq-1)*480,end_sample:seq*480}});
    return {url:socket.url,outbound:window.__outbound};
  });
  assert.match(result.url,/roomv2\/reb_tts$/);
  assert.deepEqual(result.outbound,[{type:'SUBSCRIBE',data:{types:['tts']}}]);
  await page.getByText(/已接收 25 帧/).waitFor();
  await page.getByText(/缓冲 \d+ms · 断粮 \d+ 次/).waitFor();
  await page.evaluate(()=>window.__ttsSockets.at(-1).emit({type:'tts',data:{
    conversation_id:'test-turn',stream_epoch:1,seq:26,
    start_sample:12000,end_sample:12000,stop:true,audio:''}}));
  await page.getByText(/网页音频已停止/).waitFor();
  await page.getByRole('button',{name:'关闭网页调试扬声器'}).click();
  assert.equal(await page.evaluate(()=>window.__ttsSockets[0].closed),true);
  await page.getByRole('button',{name:'开启网页调试扬声器'}).click();
  await page.getByText(/已订阅 roomv2 \/ reb_tts，等待新语音/).waitFor();
  await page.getByRole('button',{name:'annie-b'}).click();
  await page.getByRole('button',{name:'开启网页调试扬声器'}).waitFor();
  assert.equal(await page.evaluate(()=>window.__ttsSockets.at(-1).closed),true);
  assert.deepEqual(errors,[]);
  console.log('PASS Chromium AudioWorklet Room player: PCM, dedup, stop, restart, robot switch');
}finally{await browser.close();}
