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
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(String(error)));
  await page.addInitScript(()=>{
    window.__ttsSockets=[];window.__voiceSockets=[];
    window.__outbound=[];window.__voiceOutbound=[];
    window.WebSocket=class{
      static OPEN=1;readyState=1;
      constructor(url){
        this.url=url;this.closed=false;
        const channel=url.match(/\/robot\/([^/]+)\/(reb_tts|reb_voice)$/);
        if(!channel)throw Error('unexpected Room socket: '+url);
        this.channel=channel[2];
        if(this.channel==='reb_tts')window.__ttsSockets.push(this);
        else window.__voiceSockets.push(this);
        setTimeout(()=>this.emit({type:'WELCOME',ok:true,
          room:channel[1],robotId:this.channel}),0);
      }
      send(raw){
        const message=JSON.parse(raw);
        if(this.channel==='reb_tts'){
          window.__outbound.push(message);
          if(message.type==='SUBSCRIBE')setTimeout(()=>this.emit({type:'ACK',data:{
            what:'subscribed',subscriptions:['TTS','*'],
          }}),0);
        }else{
          window.__voiceOutbound.push(message);
          if(message.type==='SUBSCRIBE')setTimeout(()=>this.emit({type:'ACK',data:{
            what:'subscribed',subscriptions:['*'],
          }}),0);
        }
      }
      emit(data){if(!this.closed)this.onmessage?.({data:JSON.stringify(data)});}
      close(){this.closed=true;this.readyState=3;}
    };
  });
  await page.goto(process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:8780/?view=workbench',{
    waitUntil:'domcontentloaded',
  });
  await page.getByRole('button',{name:'开启网页调试扬声器'}).click();
  await page.getByText(/已订阅 roomv2 \/ reb_tts，等待新语音/).waitFor();
  const result=await page.evaluate(()=>{
    const socket=window.__ttsSockets.at(-1);
    const pcm='\0'.repeat(960);
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

  // Coalesced reb_tts packets up to 800 ms (38400 B / 19200 samples) must play and drain cleanly.
  await page.evaluate(()=>{
    const socket=window.__ttsSockets.at(-1);
    const chunk800=btoa('\0'.repeat(38400));
    socket.emit({type:'tts',data:{conversation_id:'coalesced-turn',stream_epoch:2,seq:1,
      start_sample:0,end_sample:19200,sample_rate:24000,channels:1,
      format:'pcm_s16le',audio:chunk800,source:'robot_main_brain'}});
    socket.emit({type:'tts',data:{conversation_id:'coalesced-turn',stream_epoch:2,seq:2,
      start_sample:19200,end_sample:19200,stream_final:true,audio:''}});
  });
  await page.getByText(/已接收 26 帧/).waitFor();
  await page.getByText(/本轮网页试听结束（非真机播放回执）/).waitFor({timeout:5000});
  const drained=await page.evaluate(()=>window.__voiceOutbound.find(
    m=>m.type==='tts.playback.drained'&&m.data?.conversation_id==='coalesced-turn'));
  assert.equal(drained?.data?.source,'browser_test');
  assert.equal(drained?.data?.played_sample_count,19200);

  await page.getByRole('button',{name:'关闭网页调试扬声器'}).click();
  assert.equal(await page.evaluate(()=>window.__ttsSockets[0].closed&&window.__voiceSockets[0].closed),true);
  await page.getByRole('button',{name:'开启网页调试扬声器'}).click();
  await page.getByText(/已订阅 roomv2 \/ reb_tts，等待新语音/).waitFor();
  await page.evaluate(()=>window.__ttsSockets.at(-1).emit({type:'tts',data:{
    conversation_id:'oversize-turn',stream_epoch:3,seq:1,start_sample:0,end_sample:25000,
    sample_rate:24000,channels:1,format:'pcm_s16le',audio:'A'.repeat(65540),source:'robot_main_brain'}}));
  await page.getByText(/网页调试扬声器：TTS 包超出 1s 上限/).waitFor();

  await page.getByRole('button',{name:'开启网页调试扬声器'}).focus();
  assert.equal(await page.evaluate(()=>document.activeElement?.textContent?.trim()),'开启网页调试扬声器');
  await page.keyboard.press('Enter');
  await page.getByText(/已订阅 roomv2 \/ reb_tts，等待新语音/).waitFor();
  const desktopOverflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
  assert.equal(desktopOverflow,false);

  await page.setViewportSize({width:390,height:844});
  const narrowOverflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
  assert.equal(narrowOverflow,false);
  await page.setViewportSize({width:1440,height:900});

  await page.getByRole('button',{name:'annie-b'}).click();
  await page.getByRole('button',{name:'开启网页调试扬声器'}).waitFor();
  assert.equal(await page.evaluate(()=>window.__ttsSockets.at(-1).closed&&window.__voiceSockets.at(-1).closed),true);
  await page.reload({waitUntil:'domcontentloaded'});
  await page.getByRole('button',{name:'开启网页调试扬声器'}).waitFor();
  assert.deepEqual(errors,[]);
  console.log('PASS Chromium AudioWorklet Room player: 20ms & 800ms coalesced PCM, drained cursor, oversize guard, 1440x900/390x844, dedup, stop, restart, robot switch');
}finally{await browser.close();}
