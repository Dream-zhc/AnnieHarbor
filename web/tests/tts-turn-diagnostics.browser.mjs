/** Local HTTP Runtime with deterministic providers; not a listening/device test. */
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdirSync,realpathSync,writeFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {dirname,resolve} from 'node:path';
const require=createRequire(import.meta.url);
let chromium;
try{({chromium}=require('playwright'));}
catch{({chromium}=require(dirname(realpathSync(execFileSync('which',['playwright'],{encoding:'utf8'}).trim()))));}
const base=process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:18791/';
const out=resolve(process.env.ANNIE_BROWSER_EVIDENCE_DIR??'var/tmp/tts-turn-evidence');mkdirSync(out,{recursive:true});
const browser=await chromium.launch({headless:true});
try{
 const page=await browser.newPage({viewport:{width:1440,height:900}}), errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(new URL('?view=workbench',base).href,{waitUntil:'domcontentloaded'});
 const submit=async(text)=>{
   const before=await page.request.get(new URL('/api/robots/annie-a/diagnostics',base).href).then(r=>r.json());
   const previous=new Set((before.turn_timelines??[]).map(t=>t.turn_id));
   await page.getByPlaceholder('输入消息...').fill(text);
   await page.getByRole('button',{name:'发送 ↗',exact:true}).click();
   for(let i=0;i<100;i++){
     const d=await page.request.get(new URL('/api/robots/annie-a/diagnostics',base).href).then(r=>r.json());
     const t=d.turn_timelines?.find(t=>!previous.has(t.turn_id)&&t.user_text===text&&t.tts?.state==='completed');
     if(t)return t;await new Promise(r=>setTimeout(r,50));
   }throw Error('fixture turn timeout '+text);
 };
 const old=await submit('讲个四川话');
 const latest=await submit('用兴奋的语气说你好');
 const rich=await submit('讲个情绪丰富的');
 assert.equal(old.tts.speech_style.dialect,'sichuan');
 assert.equal(latest.tts.speech_style.emotion,'excited');
 assert.equal(rich.tts.speech_style.delivery,'expressive');
 await page.getByRole('button',{name:/⌁ 诊断/}).click();
 const tts=page.locator('.turn-tts-evidence');
 await tts.getByText('实际 instruction',{exact:true}).waitFor();
 assert.match(await tts.innerText(),/丰富表达|增强表达|情绪起伏/);
 const select=page.locator('select[aria-label="选择诊断回合"]');
 await select.selectOption(old.turn_id);
 await tts.getByText('好嘛，我们慢慢摆一哈。',{exact:true}).first().waitFor();
 assert.match(await tts.innerText(),/四川话/);
 assert.doesNotMatch(await tts.innerText(),/你好呀！真高兴见到你！/);
 await tts.getByText(/continue-task 文本片段/).click();
 await tts.getByText(/片段 1 · 字符/).waitFor();
 assert.equal(await tts.locator('details').filter({hasText:'实际 run-task parameters（JSON）'}).getAttribute('open'),null);
 await tts.locator('summary').filter({hasText:'实际 run-task parameters（JSON）'}).click();
 assert.match(await tts.innerText(),/"sample_rate": 24000/);
 await page.screenshot({path:resolve(out,'桌面历史回合真实提交证据.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});
 const widths=await page.evaluate(()=>[document.documentElement.scrollWidth,innerWidth]);
 assert.ok(widths[0]<=widths[1]+1,JSON.stringify(widths));
 await page.getByRole('button',{name:'选择诊断回合',exact:true}).focus();await page.keyboard.press('Tab');
 assert.notEqual(await page.evaluate(()=>document.activeElement?.tagName),'BODY');
 await page.screenshot({path:resolve(out,'窄屏TTS历史证据.png'),fullPage:true});
 assert.deepEqual(errors,[]);
 const result={result:'PASS',provider:'deterministic_fixture',http:'real_local_runtime',physical_audio:'not_tested',
   checks:['actual_submit','dialect','explicit_emotion','expressive','historical_isolation','one_based_segments',
     'collapsed_json','wire_sample_rate','390px_no_overflow','keyboard_tab'],page_errors:errors};
 writeFileSync(resolve(out,'browser-result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result,null,2));
}finally{await browser.close();}
