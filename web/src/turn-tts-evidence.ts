import type {TurnTtsEvidence} from './api';

const labels:Record<string,Record<string,string>>={
  language:{zh:'中文',yue:'粤语',en:'英语',ja:'日语',ko:'韩语',fr:'法语',de:'德语',ru:'俄语',es:'西班牙语',pt:'葡萄牙语',it:'意大利语',ar:'阿拉伯语',hi:'印地语',id:'印尼语',th:'泰语',tr:'土耳其语',uk:'乌克兰语',vi:'越南语',ms:'马来语',fil:'菲律宾语'},
  dialect:{unknown:'未知',mandarin:'普通话',henan:'河南话',shaanxi:'陕西话',sichuan:'四川话',cantonese:'粤语'},
  emotion:{neutral:'中性（分类标签）',warm:'温柔关切',calm:'平静安抚',playful:'俏皮',happy:'开心',excited:'兴奋',sad:'低落',angry:'生气',serious:'认真',sarcastic:'轻微打趣',curious:'好奇'},
  delivery:{natural:'自然表达',contextual:'随语义表达（未指定固定情绪）',expressive:'丰富表达（随文本语义起伏）'},
  source:{runtime_context:'语境默认表达',llm_early:'Realtime 早期表达意图',llm_inferred:'Realtime 推断',asr_language:'ASR 语种观察',explicit_user:'用户显式要求',llm_intent:'Realtime 表达意图',chat_preference:'Chat 偏好',robot_preference:'机器人偏好',runtime_default:'默认策略',acoustic_first_follow:'首次跟随输入',acoustic_observation:'声学观察'},
  state:{locked:'表达已锁定，尚无提交证据',run_sent:'run-task 已提交',started:'云端任务已开始',completed:'云端完成（不等于扬声器已播完）',cancelled:'已取消',failed:'TTS 失败'},
};
const label=(kind:string,value:unknown)=>typeof value==='string'&&value?labels[kind]?.[value]??value:'未记录';
const present=(value:unknown)=>value===undefined||value===null?'未记录（未显式提交）':String(value);

/** Read only this turn's frozen wire evidence. Intent and heard emotion remain distinct. */
export function turnTtsEvidenceView(evidence:TurnTtsEvidence|null|undefined){
  const style=evidence?.speech_style;
  const wire=evidence?.parameters;
  // Never expose voice identifiers, credentials or future unreviewed wire keys in JSON.
  const parameters=Object.fromEntries(['text_type','format','sample_rate','enable_ssml','instruction','rate','pitch','volume','language_hints']
    .filter(key=>wire&&Object.hasOwn(wire,key)).map(key=>[key,wire![key as keyof typeof wire]]));
  return {
    recorded:!!evidence,
    status:evidence?label('state',evidence.state):'本轮 TTS 未记录（旧历史或证据已过期）',
    language:label('language',style?.language),dialect:label('dialect',style?.dialect),
    emotion:style?.emotion==='neutral'&&style.delivery==='contextual'?'未指定固定情绪':label('emotion',style?.emotion),delivery:label('delivery',style?.delivery),source:label('source',style?.source),
    instruction:present(wire?.instruction),rate:present(wire?.rate),pitch:present(wire?.pitch),volume:present(wire?.volume),
    languageHints:Array.isArray(wire?.language_hints)?wire.language_hints.join(' / ')||'空列表':present(undefined),
    audio:wire?.format&&wire?.sample_rate?`${wire.format.toUpperCase()} · ${wire.sample_rate} Hz`:'未记录',
    streaming:evidence?.streaming==='duplex'?'duplex（双向流式）':'未记录',
    textType:present(wire?.text_type),ssml:present(wire?.enable_ssml),
    taskId:evidence?.task_id??'未记录',segmentId:evidence?.segment_id??'未记录',
    submittedText:evidence?.submitted_text??'',submittedChars:evidence?.submitted_chars??0,
    segments:(evidence?.segments??[]).map(item=>({index:item.index,text:item.text,range:`${item.start_char}–${item.end_char}`,chars:item.chars,taskId:item.task_id,segmentId:item.segment_id,tsMs:item.ts_ms})),
    segmentCount:evidence?.segment_count??0,
    retention:`保留 ${(evidence?.segments??[]).length} 个文本片段；省略 ${evidence?.segments_omitted??0} 个${evidence?.text_truncated?'；文本已截断（上限 8000 字符）':''}`,
    error:evidence?.error??null,parameters,
  };
}
