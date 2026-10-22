/** Built workbench + local backend; no physical audio or cloud language claims. */
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdirSync,realpathSync} from 'node:fs';
import {createRequire} from 'node:module';
import {dirname,resolve} from 'node:path';

const require=createRequire(import.meta.url);
let chromium;
try{({chromium}=require('playwright'));}
catch{({chromium}=require(dirname(realpathSync(execFileSync('which',['playwright'],{encoding:'utf8'}).trim()))));}
const base=process.env.ANNIE_BROWSER_TEST_URL??'http://127.0.0.1:18790/';
const output=resolve(process.env.ANNIE_BROWSER_EVIDENCE_DIR??'var/tmp/qsr-browser-evidence');
mkdirSync(output,{recursive:true});
const browser=await chromium.launch({headless:true});
try{
  const page=await browser.newPage({viewport:{width:1440,height:900}}),errors=[];
  page.on('pageerror',error=>errors.push(String(error)));
  await page.goto(new URL('?view=workbench',base).href,{waitUntil:'domcontentloaded'});
  const panel=()=>page.locator('details.speech-expression-panel');
  const preference=()=>page.locator('.stage-header-tools [role="group"][aria-label="回复语言"]');
  await preference().waitFor();
  assert.equal(await panel().getAttribute('open'),null);
  await panel().locator('summary').click();
  await panel().getByText('输入识别',{exact:true}).waitFor();
  const original=await page.request.get(new URL('/api/robots/annie-a/speech-expression',base).href).then(r=>r.json());
  const last={...original,turn_id:'fixture-last',times:{},durations:{},tts:{
    state:'completed',task_id:'fixture-task',segment_id:'fixture-segment',
    speech_style:{language:'en',dialect:'unknown',emotion:'curious',delivery:'contextual',
      source:'llm_early',instruction:'INTENT MUST NOT DISPLAY',rate:.9,pitch:1,volume:40},
    parameters:{instruction:'实际提交：轻快自然，问句略上扬。',rate:1.03,pitch:1.02,volume:55},
    submitted_text:'Have an iced drink at a small shop.',submitted_chars:36,segments:[],
    segment_count:1,segments_omitted:0,text_truncated:false
  }};
  let injected={...original,speech:{state:'idle'},latest_tts:last,
    language:{...original.language,typed_input:{language:'en',dialect:'unknown',
      source:'typed_text',evidence_level:'typed_text_heuristic'},
      preferences:{chat:{language:'en',dialect:'unknown'},robot:null},
      llm_actual_text_language:'en'}};
  await page.route('**/api/robots/annie-a/speech-expression',route=>route.fulfill({json:injected}));
  await panel().getByText('实际提交：轻快自然，问句略上扬。',{exact:true}).waitFor({timeout:10000});
  assert.match(await panel().innerText(),/英语.*键盘文本推断/);
  assert.match(await panel().innerText(),/整轮情绪.*好奇/);
  assert.doesNotMatch(await panel().innerText(),/INTENT MUST NOT DISPLAY|保存所选范围|启用声学旁路/);
  await preference().locator('.custom-select-label',{hasText:'英语'}).waitFor({timeout:13000});
  await page.screenshot({path:resolve(output,'精简语音状态.png'),fullPage:true});
  injected={...injected,speech:{state:'active',turn_id:'new-turn',speech_style:null}};
  await panel().getByText('当前任务 · 待实发证据',{exact:true}).waitFor({timeout:10000});
  assert.doesNotMatch(await panel().innerText(),/实际提交：轻快自然/);
  injected={...original,speech:{state:'idle'},latest_tts:{...last,chat_id:'different'}};
  await panel().getByText('当前无任务',{exact:true}).waitFor({timeout:10000});
  assert.doesNotMatch(await panel().innerText(),/实际提交：轻快自然/);
  await page.setViewportSize({width:390,height:844});
  await panel().scrollIntoViewIfNeeded();
  const widths=await page.evaluate(()=>({document:document.documentElement.scrollWidth,viewport:innerWidth}));
  assert.ok(widths.document<=widths.viewport+1,JSON.stringify(widths));
  await page.screenshot({path:resolve(output,'语音状态窄屏.png'),fullPage:true});
  await page.route('**/api/robots/annie-a/speech-expression',route=>route.fulfill({
    status:503,contentType:'application/json',body:JSON.stringify({detail:'fixture_error'}),
  }));
  await panel().getByText('读取失败',{exact:true}).waitFor({timeout:10000});
  await page.unroute('**/api/robots/annie-a/speech-expression');
  await panel().getByText('输入识别',{exact:true}).waitFor({timeout:10000});
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({result:'passed',browser:'Chromium headless',
    physical_audio:'not_tested',checks:['role_preference','read_only','typed_input',
    'latest_tts_wire','no_stale_tts','narrow_width','injected_failure','recovery'],
    errors,screenshots:output},null,2));
}finally{await browser.close();}
