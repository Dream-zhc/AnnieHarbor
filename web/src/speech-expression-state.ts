import type {LanguageSelection, Session, SpeechExpression} from './api';

type Identity=Pick<Session,'robot_id'|'chat_id'|'persona_id'|'session_epoch'|'generation'>;
export type PreferenceScope='chat'|'robot';
const identityFields=['robot_id','chat_id','persona_id','session_epoch','generation'] as const;

export function speechResponseIsCurrent(response:Identity,current:Identity|null,request:number,latest:number){
  return !!current&&request===latest&&identityFields.every(key=>response[key]===current[key]);
}

const languages:Record<string,string>={zh:'中文',yue:'粤语',en:'英语',ja:'日语',de:'德语',ko:'韩语',ru:'俄语',fr:'法语',pt:'葡萄牙语',ar:'阿拉伯语',it:'意大利语',es:'西班牙语',hi:'印地语',id:'印尼语',th:'泰语',tr:'土耳其语',uk:'乌克兰语',vi:'越南语',ms:'马来语',fil:'菲律宾语'};
const dialects:Record<string,string>={mandarin:'普通话',sichuan:'四川话',henan:'河南话',shaanxi:'陕西话',cantonese:'粤语'};
const emotions:Record<string,string>={neutral:'中性',warm:'温柔关切',playful:'轻松俏皮',sarcastic:'轻微打趣',curious:'好奇',serious:'认真稳重',sad:'低落',excited:'适度兴奋',calm:'平稳安抚',angry:'愤怒',fearful:'害怕',happy:'开心'};
const sources:Record<string,string>={typed_text:'键盘文本语种（启发式）',runtime_context:'语境默认表达',asr_language:'ASR 语种观察',lexical_inference:'词汇推断（非声学确认）',llm_inferred:'LLM 推断',llm_early:'LLM 早期选择',explicit_user:'用户明确要求',chat_preference:'Chat 显式偏好',robot_preference:'机器人显式偏好',runtime_default:'运行默认'};
const language=(value?:string)=>languages[value??'']??'未知';
const dialect=(value?:string)=>dialects[value??'']??'未知';
const selection=(value?:LanguageSelection|null)=>value?`${language(value.language)} · ${dialect(value.dialect)}`:'未设置';
const milliseconds=(value?:number|null)=>value!=null&&Number.isFinite(value)?`${Math.round(value)} ms`:'未知';

export function speechExpressionView(data:SpeechExpression){
  const input={...(data.language?.asr_text_input??{}),...(data.language?.input??{}),
    ...(data.language?.typed_input??{})};
  const current=data.speech?.state==='active'||data.speech?.state==='streaming';
  const latest=data.latest_tts;
  const matching=latest&&['robot_id','room_id','chat_id','persona_id','persona_revision','session_epoch','generation']
    .every(key=>latest[key as keyof typeof latest]!=null&&latest[key as keyof typeof latest]===data[key as keyof typeof data]);
  // A current owner may not borrow the last finished task's submitted parameters.
  const recorded=matching&&latest.tts&&(!current||latest.turn_id===data.speech?.turn_id)?latest:null;
  const style=recorded?.tts?.speech_style??data.speech?.speech_style;
  const wire=recorded?.tts?.parameters;
  const parameter=(number?:number)=>number!=null&&Number.isFinite(number)?String(number):'未记录';
  const rawInstruction=wire?.instruction;
  const ttsInstruction=typeof rawInstruction==='string'?
    rawInstruction?rawInstruction.replace(/https?:\/\/\S+/gi,'[地址已隐藏]').slice(0,512):'未提交指令（空字符串）':
    recorded?'未记录（未显式提交）':'未记录（请查看逐轮诊断）';
  const state=data.sidecar?.state??data.sidecar?.status;
  const ownerMatches=!recorded||recorded.turn_id===data.speech?.turn_id;
  const evidence=ownerMatches?data.speech?.playback_evidence:null;
  const sidecar=state==='not_configured'?'未接入':
    !data.enabled?'已关闭（保留偏好）':
    ['unavailable','failed','error'].includes(state??'')?'旁路失败':
    state==='streaming'&&data.sidecar?.connected?'旁路已连接':
    ['reconnecting','disconnected','stopped'].includes(state??'')?'旁路断线 / 等待重连':
    state==='idle'?'等待输入':state==='connecting'?'旁路连接中':'未知';
  return {
    llmExpressionStatus:!data.llm_expression_enabled?'未启用（不影响现有首音）':
      data.llm_expression?.tts_applied?'后续句 TTS 已提交（不等于设备已播）':
      data.llm_expression?'已解析（未送入TTS）':'等待本轮表达控制',
    mixedStatus:data.speech?.mixed_enabled?`句级合成：已提交 ${data.speech?.mixed_sentences_submitted??0} 句`:'原双向合成',
    mixedInstruction:data.speech?.mixed_last_run?.instruction??'尚无后续句实发指令',
    mixedTag:data.speech?.mixed_last_run?.tag??'无',
    llmExpressionMood:data.llm_expression?.fields?.overall??'未给出',
    llmRawInstruction:(data.llm_expression?.raw_fields?.instruction??data.llm_expression?.fields?.instruction)?.slice(0,120)??'未给出',
    llmControlLog:(data.llm_expression?.controls??[]).slice(-12)
      .map(item=>`[${item.kind==='tag'?item.value:`${item.kind}:${item.value}`}] #${item.position}`)
      .join(' · ')||'LLM 本轮未生成情绪标签',
    llmExpressionWarnings:data.llm_expression?.warnings?.join('、')??'',
    sidecar,canToggleSidecar:state!=='not_configured',inputLanguage:language(input?.language),inputDialect:dialect(input?.dialect),
    dialectCandidate:input?.dialect_candidate?`${dialect(input.dialect_candidate)}（候选，非声学确认）`:'无',
    inputEmotion:emotions[input?.emotion??'']??'未知',
    inputEvidence:sources[input?.evidence_level??'']??'未知',
    inputSource:input?.source==='typed_text'?'键盘文本推断':
      input?.source==='asr_text_heuristic'?'主 ASR 文本推断':
      input?.source==='qwen3_asr_sidecar'?'Qwen3 ASR 旁路':'未知',
    inputAge:input?.age_ms!=null&&Number.isFinite(input.age_ms)&&input.age_ms>=0?`${milliseconds(input.age_ms)}${input.age_ms>3000?'（历史观察，超过 3 秒有效窗）':'（服务端观测年龄）'}`:'未知',
    inputTime:input?.observed_monotonic_ms!=null&&Number.isFinite(input.observed_monotonic_ms)?`${milliseconds(input.observed_monotonic_ms)}（单调时钟，非日期时间）`:'未知',
    robotPreference:selection(data.language?.preferences?.robot),chatPreference:selection(data.language?.preferences?.chat),
    target:data.language?.target?selection(data.language.target):'未知',targetSource:sources[data.language?.target?.source??'']??'未知',
    llmDecision:data.language?.llm_decision?selection(data.language.llm_decision):'尚无本轮决策',
    llmApplication:({current:'本轮生效',next_turn:'下轮生效',unknown:'未确认'})[data.language?.application??'unknown']??'未确认',
    llmActual:language(data.language?.llm_actual_text_language),
    ttsTask:current?recorded?'当前任务':'当前任务 · 待实发证据':recorded?'当前无任务 · 最近任务':'当前无任务',
    ttsTurn:recorded?.turn_id??(current?data.speech?.turn_id:null)??'未记录',
    ttsStage:recorded?.tts?({locked:'表达已锁定，尚无提交证据',run_sent:'run-task 已提交',started:'云端任务已开始',completed:'云端完成（不等于已播完）',cancelled:'已取消',failed:'TTS 失败'})[recorded.tts.state]??'未记录':'未记录',
    ttsError:recorded?.tts?.error??null,ttsText:recorded?.tts?.submitted_text?.replace(/https?:\/\/\S+/gi,'[地址已隐藏]').slice(0,240)??'',
    ttsInstruction,ttsRate:parameter(wire?.rate),ttsPitch:parameter(wire?.pitch),ttsVolume:parameter(wire?.volume),
    ttsLanguage:style?selection(style):'未知',ttsEmotion:style?.emotion==='neutral'&&style.delivery==='contextual'?'未指定固定情绪':emotions[style?.emotion??'']??'未知',
    ttsDelivery:({natural:'自然表达',contextual:'随语义表达（未指定固定情绪）',expressive:'丰富表达（随文本语义起伏）'})[style?.delivery as 'natural'|'contextual'|'expressive']??'未记录',
    ttsSource:sources[style?.source??'']??'未知',
    audibleLanguage:languages[data.language?.tts_audibly_verified_language??'']??'待听测',
    firstPcm:milliseconds(ownerMatches?data.speech?.run_to_first_pcm_ms:null),firstPublish:milliseconds(ownerMatches?data.speech?.first_publish_ms:null),
    speakerEvidence:evidence==='speaker_reported_started'?'设备自报起播（非声学测量）':
      evidence==='speaker_reported_drained'?'设备自报播完（非声学测量）':'待设备回执 / 未核验',
  };
}

/** Reply language choices: every language/dialect the TTS speaks. auto = no saved preference. */
export const REPLY_LANGUAGE_CHOICES=[
  {value:'auto',label:'自动跟随'},
  ...[['zh:mandarin','普通话'],['zh:sichuan','四川话'],['zh:henan','河南话'],['zh:shaanxi','陕西话'],
    ['yue:cantonese','粤语'],['en:unknown','英语'],['ja:unknown','日语'],['ko:unknown','韩语'],
    ['ru:unknown','俄语'],['fr:unknown','法语'],['de:unknown','德语'],['es:unknown','西班牙语'],
    ['pt:unknown','葡萄牙语'],['it:unknown','意大利语'],['ar:unknown','阿拉伯语'],['th:unknown','泰语'],
    ['vi:unknown','越南语'],['id:unknown','印尼语'],['ms:unknown','马来语'],['fil:unknown','菲律宾语'],
    ['hi:unknown','印地语'],['tr:unknown','土耳其语'],['uk:unknown','乌克兰语']]
    .map(([value,label])=>({value,label})),
];

export const replyLanguageValue=(value?:LanguageSelection|null)=>
  value?`${value.language}:${value.dialect}`:'auto';

export function replyLanguageSelection(value:string):LanguageSelection|null{
  if(value==='auto')return null;
  const [language,dialect]=value.split(':');
  return {language,dialect};
}

export function replyLanguageLabel(value?:LanguageSelection|null){
  const key=replyLanguageValue(value);
  return REPLY_LANGUAGE_CHOICES.find(item=>item.value===key)?.label??
    (value?(value.dialect==='unknown'?language(value.language):dialect(value.dialect)):'自动跟随');
}

export function languagePreferenceBody(identity:Identity,scope:PreferenceScope,value:LanguageSelection|null,requestId:string){
  return {scope,...(value?{language:value.language,dialect:value.dialect}:{}),request_id:requestId,
    chat_id:identity.chat_id,persona_id:identity.persona_id,session_epoch:identity.session_epoch,generation:identity.generation};
}

export function preferenceResultMessage(scope:PreferenceScope,cleared:boolean){
  const label=scope==='chat'?'当前 Chat 的':'机器人级';
  return cleared?`已清除${label}语言偏好；其它范围保留。`:`已保存${label}语言偏好。`;
}
