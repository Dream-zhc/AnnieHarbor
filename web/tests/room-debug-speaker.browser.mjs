// Optional real Chromium UI test. Reb_tts is mocked: no audio enters the real Room.
// Run against a local Runtime: node web/tests/room-debug-speaker.browser.mjs
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
  page.on('pageerror',error=>errors.push(String(error)));
  await page.addInitScript(()=>{
    window.__audioStarts=[];window.__audioStops=0;window.__audioCloses=0;
    window.__ttsSockets=[];window.__outbound=[];
    window.AudioContext=class{
      currentTime=0;destination={};
      async resume(){}
      async close(){window.__audioCloses++;}
      createBuffer(_channels,count,rate){
        if(rate!==24000)throw Error('wrong PCM rate');
        return {getChannelData:()=>new Float32Array(count)};
      }
      createBufferSource(){
        return {connect(){},disconnect(){},stop(){window.__audioStops++;},
          start(at){window.__audioStarts.push(at);}};
      }
    };
    window.WebSocket=class{
      static OPEN=1;
      readyState=1;
      constructor(url){
        this.url=url;this.closed=false;window.__ttsSockets.push(this);
        if(!url.endsWith('/reb_tts'))throw Error('unexpected Room socket: '+url);
        setTimeout(()=>this.emit({type:'WELCOME',ok:true,room:url.match(/\/robot\/([^/]+)\//)?.[1],robotId:'reb_tts'}),0);
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
    socket.emit(data);
    const afterFirst=window.__audioStarts.length;
    socket.emit(data); // repeated seq must not play twice
    const afterDuplicate=window.__audioStarts.length;
    socket.emit({type:'tts',data:{...data.data,seq:2,stop:true,audio:'',
      start_sample:480,end_sample:480}});
    return {url:socket.url,afterFirst,afterDuplicate,stops:window.__audioStops,
      outbound:window.__outbound};
  });
  assert.match(result.url,/roomv2\/reb_tts$/);
  assert.equal(result.afterFirst,1);
  assert.equal(result.afterDuplicate,1);
  assert.equal(result.stops,1);
  assert.deepEqual(result.outbound,[{type:'SUBSCRIBE',data:{types:['tts']}}]);
  await page.getByRole('button',{name:'关闭网页调试扬声器'}).click();
  assert.deepEqual(await page.evaluate(()=>({
    closed:window.__ttsSockets[0].closed,contextClosed:window.__audioCloses,
  })),{closed:true,contextClosed:1});
  await page.getByRole('button',{name:'开启网页调试扬声器'}).click();
  await page.getByText(/已订阅 roomv2 \/ reb_tts，等待新语音/).waitFor();
  await page.getByLabel('当前机器人',{exact:true}).selectOption('annie-b');
  await page.getByRole('button',{name:'开启网页调试扬声器'}).waitFor();
  assert.equal(await page.evaluate(()=>window.__ttsSockets.at(-1).closed),true);
  assert.deepEqual(errors,[]);
  // Also exercise native Chromium WebAudio (the first page has a deterministic
  // AudioContext spy). No real Room packets are sent or received here.
  const nativePage=await browser.newPage(),nativeErrors=[];
  nativePage.on('pageerror',error=>nativeErrors.push(String(error)));
  await nativePage.addInitScript(()=>{
    window.__nativeStarts=0;
    const nativeStart=AudioBufferSourceNode.prototype.start;
    AudioBufferSourceNode.prototype.start=function(...args){
      window.__nativeStarts++;return nativeStart.apply(this,args);
    };
    window.WebSocket=class{
      static OPEN=1;
      readyState=1;
      constructor(url){this.url=url;window.__nativeRoom=this;
        setTimeout(()=>this.emit({type:'WELCOME',ok:true,room:'roomv2',robotId:'reb_tts'}),0);
      }
      send(raw){if(JSON.parse(raw).type==='SUBSCRIBE')setTimeout(()=>this.emit({
        type:'ACK',data:{what:'subscribed',subscriptions:['TTS','*']},
      }),0);}
      emit(data){this.onmessage?.({data:JSON.stringify(data)});}
      close(){this.readyState=3;}
    };
  });
  await nativePage.goto(process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:8780/',{
    waitUntil:'domcontentloaded',
  });
  await nativePage.getByRole('button',{name:'开启网页调试扬声器'}).click();
  await nativePage.getByText(/已订阅 roomv2 \/ reb_tts，等待新语音/).waitFor();
  await nativePage.evaluate(()=>{
    const pcm=btoa(String.fromCharCode(...new Uint8Array(960)));
    const data={conversation_id:'native-turn',stream_epoch:2,seq:1,
      start_sample:0,end_sample:480,sample_rate:24000,channels:1,
      format:'pcm_s16le',audio:pcm,source:'robot_main_brain'};
    window.__nativeRoom.emit({type:'tts',data});
    window.__nativeRoom.emit({type:'tts',data:{...data,seq:2,
      start_sample:480,end_sample:480,audio:'',stream_final:true}});
  });
  await nativePage.getByText('本轮网页试听结束（非真机播放回执）').waitFor();
  assert.equal(await nativePage.evaluate(()=>window.__nativeStarts),1);
  assert.deepEqual(nativeErrors,[]);
  console.log('Room-only TTS browser test passed: protocol, native WebAudio, stop, off, robot switch');
}finally{await browser.close();}
