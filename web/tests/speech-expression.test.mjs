import assert from 'node:assert/strict';
import test from 'node:test';
import {speechResponseIsCurrent, speechExpressionView, languagePreferenceBody, preferenceResultMessage} from '../src/speech-expression-state.ts';

const identity={robot_id:'A',room_id:'room-a',chat_id:'chat-a',persona_id:'annie',persona_revision:1,session_epoch:1,generation:2};
const snapshot=(extra={})=>({...identity,enabled:true,sidecar:{status:'streaming',connected:true},language:{input:null,preferences:{chat:null,robot:null},target:{language:'zh',dialect:'unknown',source:'runtime_default'},llm_decision:null,application:'unknown',llm_actual_text_language:'unknown',tts_audibly_verified_language:'unknown'},speech:{state:'idle'},...extra});

test('late robot, Chat, Persona, epoch, generation and request results are rejected',()=>{
  assert.equal(speechResponseIsCurrent(snapshot(),identity,3,3),true);
  for(const key of ['robot_id','chat_id','persona_id','session_epoch','generation']){
    assert.equal(speechResponseIsCurrent(snapshot({[key]:'other'}),identity,3,3),false,key);
  }
  assert.equal(speechResponseIsCurrent(snapshot(),identity,2,3),false);
  assert.equal(speechResponseIsCurrent(snapshot(),null,3,3),false);
});

test('input ASR dialect candidates never become LLM decisions or audible TTS proof',()=>{
  const data=snapshot();
  data.language.input={language:'zh',emotion:'sad',dialect:'unknown',dialect_candidate:'sichuan',evidence_level:'lexical_inference'};
  const view=speechExpressionView(data);
  assert.equal(view.inputLanguage,'中文');
  assert.equal(view.inputDialect,'未知');
  assert.equal(view.dialectCandidate,'四川话（候选，非声学确认）');
  assert.equal(view.llmDecision,'尚无本轮决策');
  assert.equal(view.llmApplication,'未确认');
  assert.equal(view.ttsLanguage,'未知');
  assert.equal(view.audibleLanguage,'待听测');
});

test('LLM raw instructions and TTS applied instructions are independent evidence',()=>{
  const data=snapshot({
    llm_expression_enabled:true,
    llm_expression:{fields:{overall:'playful'},raw_fields:{instruction:'语气活泼，尾音轻快。'},
      warnings:['invalid_instruction'],tts_applied:false,controls_observed:2,
      controls:[{position:5,kind:'overall',value:'playful'},{position:9,kind:'tag',value:'serious'}]},
    speech:{state:'idle'},
  });
  const view=speechExpressionView(data);
  assert.match(view.llmExpressionStatus,/未送入TTS/);
  assert.equal(view.llmExpressionMood,'playful');
  assert.equal(view.llmRawInstruction,'语气活泼，尾音轻快。');
  assert.match(view.llmControlLog,/\[overall:playful\] #5/);
  assert.match(view.llmControlLog,/\[serious\] #9/);
  assert.equal(view.ttsInstruction,'未记录（请查看逐轮诊断）');
  assert.equal(view.llmExpressionWarnings,'invalid_instruction');
  assert.match(speechExpressionView(snapshot()).llmExpressionStatus,/未启用/);
});

test('mixed TTS separately displays actual later-task instructions and submission evidence',()=>{
  const data=snapshot({
    llm_expression_enabled:true,
    llm_expression:{fields:{overall:'serious',instruction:'请自然发声'},warnings:[],
      tts_applied:true,controls_observed:2},
    speech:{state:'active',mixed_enabled:true,mixed_sentences_submitted:2,
      mixed_last_run:{segment_id:'t-3',task_id:'third',instruction:'语气认真，提醒清晰',
        tag:'serious',rate:.97,source:'llm_sentence'}},
  });
  const view=speechExpressionView(data);
  assert.match(view.llmExpressionStatus,/已提交/);
  assert.match(view.mixedStatus,/已提交 2 句/);
  assert.equal(view.mixedInstruction,'语气认真，提醒清晰');
  assert.equal(view.mixedTag,'serious');
  assert.equal(speechExpressionView(snapshot()).mixedStatus,'原双向合成');
});

test('TTS wire parameters and independent first PCM, Room publication and speaker evidence stay distinct',()=>{
  const data=snapshot({speech:{state:'streaming',run_to_first_pcm_ms:26,first_publish_ms:31,playback_evidence:'speaker_reported_started',speech_style:{language:'yue',dialect:'cantonese',emotion:'warm',rate:.97,pitch:1,volume:50,instruction:'请使用自然粤语。',source:'chat_preference'}}});
  data.language.application='next_turn';
  data.language.llm_decision={language:'en',dialect:'unknown'};
  const view=speechExpressionView(data);
  assert.equal(view.llmDecision,'英语 · 未知');
  assert.equal(view.llmApplication,'下轮生效');
  assert.equal(view.ttsLanguage,'粤语 · 粤语');
  assert.equal(view.ttsEmotion,'温柔关切');
  assert.equal(view.firstPcm,'26 ms');
  assert.equal(view.firstPublish,'31 ms');
  assert.equal(view.speakerEvidence,'设备自报起播（非声学测量）');
  assert.equal(view.audibleLanguage,'待听测');
});

test('disabled and failed sidecars preserve preferences and accurately show states',()=>{
  const data=snapshot({enabled:false,sidecar:{status:'disabled'}});
  data.language.preferences.robot={language:'en',dialect:'unknown'};
  assert.equal(speechExpressionView(data).sidecar,'已关闭（保留偏好）');
  assert.equal(speechExpressionView(data).robotPreference,'英语 · 未知');
  assert.equal(speechExpressionView(snapshot({sidecar:{status:'unavailable',connected:false}})).sidecar,'旁路失败');
  assert.equal(speechExpressionView(snapshot({sidecar:{status:'reconnecting',connected:false}})).sidecar,'旁路断线 / 等待重连');
  assert.equal(speechExpressionView(snapshot({sidecar:{state:'not_configured'}})).sidecar,'未接入');
});

test('explicit selection writes only chosen scope and active identity; clear does not claim all preferences',()=>{
  assert.deepEqual(languagePreferenceBody(identity,'robot',{language:'zh',dialect:'henan'},'req-1'),{scope:'robot',language:'zh',dialect:'henan',request_id:'req-1',chat_id:'chat-a',persona_id:'annie',session_epoch:1,generation:2});
  assert.deepEqual(languagePreferenceBody(identity,'chat',null,'req-2'),{scope:'chat',request_id:'req-2',chat_id:'chat-a',persona_id:'annie',session_epoch:1,generation:2});
  assert.equal(preferenceResultMessage('chat',true),'已清除当前 Chat 的语言偏好；其它范围保留。');
  assert.equal(preferenceResultMessage('robot',false),'已保存机器人级语言偏好。');
});

test('compact read-only workbench panel and role preference are identity scoped',async()=>{
  const {readFileSync}=await import('node:fs');
  const panel=readFileSync(new URL('../src/speech-expression.tsx',import.meta.url),'utf8');
  assert.match(panel,/<details/);
  assert.match(panel,/new AbortController/);
  assert.match(panel,/controller\?\.abort\(\)/);
  assert.match(panel,/speechResponseIsCurrent/);
  assert.match(panel,/RoleLanguagePreference/);
  assert.match(panel,/整轮情绪/);
  assert.match(panel,/TTS 指令/);
  assert.doesNotMatch(panel,/语言方言偏好|保存所选范围|清除所选范围|启用声学旁路/);
  assert.doesNotMatch(panel,/\.voice\b|innerHTML|localStorage/);
});

test('older backend partial language and empty input display unknown without throwing',()=>{
  for(const input of [null,{}, {emotion:'sad'}]){
    const data=snapshot({language:{input,preferences:{}}});
    const view=speechExpressionView(data);
    assert.equal(view.target,'未知');
    assert.equal(view.targetSource,'未知');
    assert.equal(view.inputLanguage,'未知');
    assert.equal(view.inputDialect,'未知');
    assert.equal(view.robotPreference,'未设置');
    assert.equal(view.chatPreference,'未设置');
    assert.equal(view.llmApplication,'未确认');
    assert.equal(view.audibleLanguage,'待听测');
  }
});

test('observation source is allowlisted and monotonic stamp is not displayed as wall time',()=>{
  const data=snapshot();
  data.language.input={language:'zh',observed_monotonic_ms:12345.5,evidence_level:'asr_language',source:'qwen3_asr_sidecar'};
  const view=speechExpressionView(data);
  assert.equal(view.inputSource,'Qwen3 ASR 旁路');
  assert.equal(view.inputTime,'12346 ms（单调时钟，非日期时间）');
  data.language.input.source='https://private.invalid/token';
  assert.equal(speechExpressionView(data).inputSource,'未知');
  data.language.input.observed_monotonic_ms=NaN;
  assert.equal(speechExpressionView(data).inputTime,'未知');
});

test('disabled unconfigured sidecar remains not connected; preferences can be read in role',async()=>{
  const data=snapshot({enabled:false,sidecar:{state:'not_configured'}});
  data.language.preferences.chat={language:'ja',dialect:'unknown'};
  const view=speechExpressionView(data);
  assert.equal(view.sidecar,'未接入');
  assert.equal(view.canToggleSidecar,false);
  assert.equal(view.chatPreference,'日语 · 未知');
  assert.equal(speechExpressionView(snapshot({enabled:false,sidecar:{status:'disabled'}})).canToggleSidecar,true);
  const {readFileSync}=await import('node:fs');
  const panel=readFileSync(new URL('../src/speech-expression.tsx',import.meta.url),'utf8');
  assert.match(panel,/会话：/);
  assert.doesNotMatch(panel,/onClick=\{\(\)=>void mutate/);
});

test('typed language evidence shows separately from voice ASR and does not become a preference',()=>{
  const data=snapshot();
  data.language.typed_input={language:'en',dialect:'unknown',source:'typed_text',evidence_level:'typed_text_heuristic'};
  const view=speechExpressionView(data);
  assert.equal(view.inputLanguage,'英语');
  assert.equal(view.inputSource,'键盘文本推断');
  assert.equal(view.chatPreference,'未设置');
  assert.equal(view.robotPreference,'未设置');
});

test('main ASR language heuristic stays distinguishable from acoustic classification',()=>{
  const data=snapshot();
  data.language.asr_text_input={language:'en',dialect:'unknown',source:'asr_text_heuristic',evidence_level:'text_heuristic'};
  assert.equal(speechExpressionView(data).inputSource,'主 ASR 文本推断');
  assert.equal(speechExpressionView(data).inputLanguage,'英语');
  data.language.input={language:'zh',source:'qwen3_asr_sidecar',evidence_level:'asr_language'};
  assert.equal(speechExpressionView(data).inputLanguage,'中文');
  assert.equal(speechExpressionView(data).inputSource,'Qwen3 ASR 旁路');
});

test('input age uses only server supplied duration and marks observation staleness without browser clock',()=>{
  const data=snapshot();
  data.language.input={language:'zh',observed_monotonic_ms:1,age_ms:4000};
  assert.equal(speechExpressionView(data).inputAge,'4000 ms（历史观察，超过 3 秒有效窗）');
  data.language.input.age_ms=100;
  assert.equal(speechExpressionView(data).inputAge,'100 ms（服务端观测年龄）');
  delete data.language.input.age_ms;
  assert.equal(speechExpressionView(data).inputAge,'未知');
  data.language.input.age_ms=-1;
  assert.equal(speechExpressionView(data).inputAge,'未知');
});

const latestTts=(extra={})=>({...identity,turn_id:'turn-last',times:{},durations:{},tts:{state:'completed',task_id:'task-last',segment_id:'seg-last',speech_style:{language:'en',dialect:'unknown',emotion:'curious',delivery:'contextual',source:'llm_early',instruction:'INTENT ONLY',rate:.9,pitch:1,volume:40},parameters:{instruction:'Actually sent instruction',rate:1.03,pitch:1.02,volume:55},submitted_text:'Hello?',submitted_chars:6,segments:[],segment_count:1,segments_omitted:0,text_truncated:false},...extra});

test('finished owner does not erase latest same-session actual TTS instruction and parameters',()=>{
  const data=snapshot({speech:{state:'idle',speech_style:null},latest_tts:latestTts()});
  const view=speechExpressionView(data);
  assert.equal(view.ttsTask,'当前无任务 · 最近任务');
  assert.equal(view.ttsTurn,'turn-last');
  assert.equal(view.ttsInstruction,'Actually sent instruction');
  assert.equal(view.ttsRate,'1.03');assert.equal(view.ttsPitch,'1.02');assert.equal(view.ttsVolume,'55');
  assert.equal(view.ttsLanguage,'英语 · 未知');
});

test('latest TTS never crosses robot, room, chat, persona, revision, epoch or generation',()=>{
  for(const key of ['robot_id','room_id','chat_id','persona_id','persona_revision','session_epoch','generation']){
    const view=speechExpressionView(snapshot({latest_tts:latestTts({[key]:'other'})}));
    assert.equal(view.ttsTask,'当前无任务',key);
    assert.equal(view.ttsInstruction,'未记录（请查看逐轮诊断）',key);
    assert.equal(view.ttsLanguage,'未知',key);
  }
});

test('current pending TTS never borrows previous task wire or claims style intent was sent',()=>{
  const view=speechExpressionView(snapshot({speech:{state:'active',turn_id:'new-turn',speech_style:{language:'zh',dialect:'mandarin',emotion:'warm',rate:.9,pitch:1,volume:40,instruction:'NOT SENT',source:'runtime_context'}},latest_tts:latestTts()}));
  assert.equal(view.ttsTask,'当前任务 · 待实发证据');
  assert.equal(view.ttsInstruction,'未记录（请查看逐轮诊断）');
  assert.equal(view.ttsRate,'未记录');
  assert.equal(view.ttsLanguage,'中文 · 普通话');
});

test('wire instruction absent, explicitly empty, and safe text preview remain distinct',()=>{
  const last=latestTts();delete last.tts.parameters.instruction;
  assert.equal(speechExpressionView(snapshot({latest_tts:last})).ttsInstruction,'未记录（未显式提交）');
  last.tts.parameters.instruction='';
  assert.equal(speechExpressionView(snapshot({latest_tts:last})).ttsInstruction,'未提交指令（空字符串）');
  last.tts.parameters.instruction='See https://private.invalid/token '+ 'a'.repeat(800);
  const view=speechExpressionView(snapshot({latest_tts:last}));
  assert.ok(view.ttsInstruction.length<=512);assert.doesNotMatch(view.ttsInstruction,/private\.invalid/);
});

test('partial identities are not sufficient proof and latest failed task stays visibly failed',()=>{
  for(const key of ['robot_id','room_id','chat_id','persona_id','persona_revision','session_epoch','generation']){
    const last=latestTts();delete last[key];
    assert.equal(speechExpressionView(snapshot({latest_tts:last})).ttsTask,'当前无任务',key);
  }
  const last=latestTts();last.tts.state='failed';last.tts.error='synthesis_failed';
  const view=speechExpressionView(snapshot({latest_tts:last}));
  assert.equal(view.ttsStage,'TTS 失败');assert.equal(view.ttsError,'synthesis_failed');
  assert.equal(view.ttsText,'Hello?');
  assert.equal(view.audibleLanguage,'待听测');
});

test('recent wire evidence never borrows latency or device proof from another owner',()=>{
  const view=speechExpressionView(snapshot({latest_tts:latestTts(),speech:{state:'idle',turn_id:'different',run_to_first_pcm_ms:99,first_publish_ms:101,playback_evidence:'speaker_reported_drained'}}));
  assert.equal(view.firstPcm,'未知');assert.equal(view.firstPublish,'未知');
  assert.equal(view.speakerEvidence,'待设备回执 / 未核验');
});
