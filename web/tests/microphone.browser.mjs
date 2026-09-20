// Optional real Chromium + fake microphone test. Never sends PCM to real Room:
// only the WebSocket publisher is replaced, and all HTTP calls are read-only.
// Run with an existing local Runtime: node web/tests/microphone.browser.mjs
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

const browser=await chromium.launch({headless:true,args:[
  '--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream',
]});
try{
  const context=await browser.newContext({permissions:['microphone']});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',error=>errors.push(String(error)));
  await page.addInitScript(()=>{
    window.__micFrames=[];
    window.__micClosed=0;
    window.WebSocket=class{
      static OPEN=1;
      constructor(url){
        this.url=url;this.readyState=0;
        setTimeout(()=>{
          if(this.readyState!==0)return;
          this.readyState=1;
          this.onmessage?.({data:JSON.stringify({
            type:'WELCOME',ok:true,
            room:url.match(/\/robot\/([^/]+)\/reb_mic/)?.[1]??'unknown',
            robotId:'reb_mic',
          })});
        },0);
      }
      send(bytes){
        const frame=Array.from(new Uint8Array(bytes));
        window.__micFrames.push({
          room:this.url,bytes:frame.length,header:String.fromCharCode(...frame.slice(2,11)),
        });
      }
      close(){this.readyState=3;window.__micClosed++;}
    };
  });
  const url=process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:8780/';
  await page.goto(url,{waitUntil:'domcontentloaded'});
  await page.getByRole('checkbox',{name:/此 Room 没有另一个麦克风/}).check();
  await page.getByRole('button',{name:'开启麦克风'}).click();
  await page.waitForFunction(()=>window.__micFrames.length>=10,null,{timeout:10000});
  const first=await page.evaluate(()=>window.__micFrames[0]);
  assert.equal(first.bytes,651);
  assert.equal(first.header,'MIC_AUDIO');
  assert.match(first.room,/\/reb_mic$/);
  assert.match(await page.locator('.voice-actions [role="status"]').innerText(),/已发 [1-9]\d* 帧/);
  await page.getByRole('button',{name:'停止麦克风'}).click();
  const stopped=await page.evaluate(()=>window.__micFrames.length);
  await page.waitForTimeout(450);
  const after=await page.evaluate(()=>({
    frames:window.__micFrames.length,closed:window.__micClosed,
  }));
  assert.equal(after.frames,stopped,'stopped browser microphone still sends PCM');
  assert.ok(after.closed>0,'microphone WebSocket not released');
  await page.getByRole('button',{name:'开启麦克风'}).click();
  await page.waitForFunction(before=>window.__micFrames.length>=before+5,stopped,{timeout:10000});
  await page.getByRole('combobox',{name:'当前机器人'}).selectOption('annie-b');
  await page.waitForTimeout(200);
  const switched=await page.evaluate(()=>({
    frames:window.__micFrames.length,closed:window.__micClosed,
  }));
  await page.waitForTimeout(450);
  assert.equal(await page.evaluate(()=>window.__micFrames.length),switched.frames,
    'robot switch leaked old-room mic frames');
  assert.ok(switched.closed>after.closed,'robot switch did not close the old mic WS');
  assert.deepEqual(errors,[],'browser JS errors');
  console.log('PASS Chromium fake microphone → real audio callbacks →',
    stopped,'651-byte MIC_AUDIO packets; stop and robot switch release WS; no leaked frames.');
}finally{await browser.close();}
