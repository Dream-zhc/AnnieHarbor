import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {selectTurnEvidence} from '../src/turn-evidence.ts';

const identity={robot_id:'A',chat_id:'chat-a',persona_id:'annie',session_epoch:3,generation:3};
const tts=(extra={})=>({task_id:'tts-old',segment_id:'speech-old',streaming:'duplex',state:'completed',
  speech_style:{language:'zh',dialect:'sichuan',emotion:'neutral',delivery:'expressive',source:'explicit_user',instruction:'用四川话按故事情节自然起伏。',rate:1,pitch:1,volume:50},
  parameters:{text_type:'PlainText',format:'pcm',sample_rate:24000,enable_ssml:false,instruction:'用四川话按故事情节自然起伏。',rate:1,pitch:1,volume:50,language_hints:['zh']},
  submitted_text:'第一句。第二句。',submitted_chars:8,segments:[{index:1,text:'第一句。',start_char:0,end_char:4,chars:4,task_id:'tts-old',segment_id:'speech-old',ts_ms:100},{index:2,text:'第二句。',start_char:4,end_char:8,chars:4,task_id:'tts-old',segment_id:'speech-old',ts_ms:120}],segment_count:2,segments_omitted:0,text_truncated:false,...extra});
const diagnostics=(timelines)=>({...identity,turn_timelines:timelines,tool_history:[],turn_events:[],turn_prompt_refs:[],prompt_history:[],speech_output:{speech_style:{dialect:'henan'}}});
async function view(value){
  const module=await import('../src/turn-tts-evidence.ts').catch(()=>null);
  assert.equal(typeof module?.turnTtsEvidenceView,'function','必须提供本轮 TTS 证据视图，而非当前 speech 状态');
  return module.turnTtsEvidenceView(value);
}

test('selected historical TTS never borrows latest speech output or a different turn',()=>{
  const old=tts();
  const data=diagnostics([{turn_id:'old',times:{},durations:{},tts:old},{turn_id:'new',times:{},durations:{},tts:tts({task_id:'new-task',submitted_text:'河南话'})}]);
  assert.equal(selectTurnEvidence(data,'old').tts,old);
  assert.equal(selectTurnEvidence(data,'missing').tts,null);
  assert.equal(selectTurnEvidence(diagnostics([{turn_id:'old',times:{},durations:{}}]),'old').tts,null);
});

test('mismatched robot, Chat, Persona, epoch or generation cannot supply TTS or events',()=>{
  for(const field of ['robot_id','chat_id','persona_id','session_epoch','generation']){
    const wrong={...identity,[field]:'other'};
    const data=diagnostics([{...wrong,turn_id:'old',times:{},durations:{},tts:tts()}]);
    data.turn_events=[{...wrong,turn_id:'old',kind:'tts.run_task',seq:1}];
    assert.equal(selectTurnEvidence(data,'old').tts,null,field);
    assert.equal(selectTurnEvidence(data,'old').events.length,0,field);
  }
});

test('old unrecorded evidence remains unknown and does not invent provider defaults',async()=>{
  const result=await view(null);
  assert.equal(result.recorded,false);
  assert.match(result.status,/本轮 TTS 未记录/);
  const partial=await view(tts({parameters:{},speech_style:null,state:'locked',segments:[]}));
  assert.equal(partial.rate,'未记录（未显式提交）');
  assert.equal(partial.emotion,'未记录');
  assert.match(partial.status,/锁定/);
});

test('expressive delivery is distinct from neutral emotion and displays actual raw parameters',async()=>{
  const result=await view(tts());
  assert.equal(result.recorded,true);
  assert.equal(result.dialect,'四川话');
  assert.equal(result.emotion,'中性（分类标签）');
  assert.equal(result.delivery,'丰富表达（随文本语义起伏）');
  assert.equal(result.source,'用户显式要求');
  assert.equal(result.instruction,'用四川话按故事情节自然起伏。');
  assert.equal(result.rate,'1');
  assert.equal(result.audio,'PCM · 24000 Hz');
  assert.equal(result.streaming,'duplex（双向流式）');
  assert.equal(result.submittedText,'第一句。第二句。');
  assert.equal(result.segments.length,2);
  assert.equal(result.segments[1].range,'4–8');
});

test('actual submitted parameters are not reconstructed from style and voice/key are excluded',async()=>{
  const result=await view(tts({parameters:{format:'pcm',sample_rate:24000,voice:'PRIVATE_VOICE',api_key:'SECRET',instruction:'实际提交指令',rate:1.02},speech_style:{...tts().speech_style,instruction:'意图指令',rate:.9}}));
  assert.equal(result.instruction,'实际提交指令');
  assert.equal(result.rate,'1.02');
  assert.equal(result.pitch,'未记录（未显式提交）');
  assert.doesNotMatch(JSON.stringify(result),/PRIVATE_VOICE|SECRET|api_key|"voice"/);
});

test('cancelled, failed and retained-text truncation are explicit, never playback proof',async()=>{
  const failed=await view(tts({state:'failed',error:'Provider rejected parameter',text_truncated:true,segments_omitted:3,segment_count:5}));
  assert.equal(failed.status,'TTS 失败');
  assert.equal(failed.error,'Provider rejected parameter');
  assert.match(failed.retention,/3/);
  assert.match(failed.retention,/截断/);
  assert.equal((await view(tts({state:'cancelled'}))).status,'已取消');
  assert.match((await view(tts({state:'completed'}))).status,/云端完成/);
});

test('diagnostics renders readable per-turn TTS and names continue-task pieces honestly',()=>{
  const source=readFileSync(new URL('../src/turn-diagnostics.tsx',import.meta.url),'utf8');
  assert.match(source,/本轮 TTS 文本与表达参数/);
  assert.match(source,/continue-task 文本片段/);
  assert.match(source,/不是独立声学分段/);
  assert.match(source,/同一任务共享冻结参数/);
  assert.match(source,/实际 run-task parameters/);
  assert.doesNotMatch(source,/speech_output|speech\.snapshot/);
});

test('default contextual delivery communicates semantic variation, not a fixed neutral reading',async()=>{
  const contextual=tts({speech_style:{...tts().speech_style,delivery:'contextual',source:'runtime_context'}});
  const result=await view(contextual);
  assert.equal(result.delivery,'随语义表达（未指定固定情绪）');
  assert.equal(result.emotion,'未指定固定情绪');
  assert.equal(result.source,'语境默认表达');
  const {speechExpressionView}=await import('../src/speech-expression-state.ts');
  const current=speechExpressionView({enabled:false,sidecar:{status:'disabled'},language:{preferences:{}},speech:{speech_style:contextual.speech_style}});
  assert.equal(current.ttsDelivery,'随语义表达（未指定固定情绪）');
  assert.equal(current.ttsEmotion,'未指定固定情绪');
  assert.equal(current.ttsSource,'语境默认表达');
  assert.equal(current.audibleLanguage,'待听测');
  const source=readFileSync(new URL('../src/speech-expression.tsx',import.meta.url),'utf8');
  assert.match(source,/view\.ttsDelivery/);
});

test('continue-task fragment labels use backend one-based indices without renumbering',()=>{
  const source=readFileSync(new URL('../src/turn-diagnostics.tsx',import.meta.url),'utf8');
  assert.match(source,/片段 \{piece\.index\}/);
  assert.doesNotMatch(source,/piece\.index\s*\+\s*1/);
});
