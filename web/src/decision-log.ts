/** Runtime decision events shown on the workbench; no wire or model access here. */
export type DecisionEvent={
  seq:number;kind:string;text?:string;verdict?:string;choice?:string|null;
  confidence?:number|null;latency_ms?:number|null;error?:string|null;source?:string;
  script?:string|null;reason?:string;
};
export type DecisionEntry={seq:number;topic:string;choice:string;outcome:string;detail:string;text:string};

const ADDRESSEE:Record<string,string>={
  robot:'对机器人说',person:'对别人说',self_or_media:'自言自语/电视',unclear:'无法判断',
};
const MOTION:Record<string,string>={
  none:'不做',surprise:'惊讶',smirk:'坏笑',proud:'得意',skeptical:'怀疑',shy:'害羞',
  agree_micro_nod:'点头',shake_head:'摇头',look_left:'看左边',look_right:'看右边',
  look_up:'看上面',fast_double_blink:'眨眼',
};

const metrics=(event:DecisionEvent)=>[
  typeof event.confidence==='number'?'置信 '+event.confidence.toFixed(2):'',
  typeof event.latency_ms==='number'?Math.round(event.latency_ms)+' ms':'',
].filter(Boolean).join(' · ');

/** One readable row per decision; other events return null. */
export function decisionEntry(event:DecisionEvent):DecisionEntry|null{
  const text=(event.text??'').trim();
  if(event.kind==='addressee.decided'){
    const choice=event.error?'决策失败':ADDRESSEE[event.choice??'']??'无法判断';
    return {seq:event.seq,topic:'对谁说话',choice,text,
      outcome:event.verdict==='robot'?'回复':'不回复，继续听',
      detail:event.error?'按对机器人说处理':metrics(event)};
  }
  if(event.kind==='action.auto_decided'){
    const choice=MOTION[event.choice??'']??event.choice??'—';
    return {seq:event.seq,topic:'表情动作',choice,text,
      outcome:event.script?'执行'+(MOTION[event.script]??event.script):'不做',
      detail:event.source==='explicit'?'用户点名，未调用模型':metrics(event)};
  }
  if(event.kind==='turn.recovered'){
    return {seq:event.seq,topic:'判停补发',choice:'点名提问被判无效',text,
      outcome:'已按识别终稿补发',detail:''};
  }
  return null;
}

/** Side talk the decision model declined is shown as a decision, not ambient noise. */
export const isProviderAmbient=(event:{kind:string;reason?:string})=>
  event.kind==='transcript.ambient'&&event.reason!=='not_addressed';

export const formatDecision=(entry:DecisionEntry)=>
  `${entry.topic}：${entry.choice} → ${entry.outcome}`+(entry.detail?` · ${entry.detail}`:'')
  +(entry.text?` 「${entry.text}」`:'');
