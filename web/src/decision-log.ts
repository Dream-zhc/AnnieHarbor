/** Runtime decision events shown on the workbench; no wire or model access here. */
export type DecisionEvent={
  seq:number;kind:string;text?:string;verdict?:string;choice?:string|null;
  confidence?:number|null;latency_ms?:number|null;error?:string|null;source?:string;
  script?:string|null;reason?:string;turn_id?:string|null;item_id?:string|null;model?:string|null;
};
/** realtime.decision in the robot snapshot: which model answers right now. */
export type DecisionStatus={
  addressee:boolean;addressee_judge?:string|null;addressee_model?:string|null;
  auto_expression:boolean;expression_model?:string|null;
  d3?:{model:string;status:'probing'|'ready'|'down';ready:boolean;error:string|null;
    checked_ms:number|null;failures:number}|null;
};
export type DecisionEntry={
  seq:number;topic:string;choice:string;outcome:string;detail:string;text:string;
  turn_id?:string;item_id?:string;
};

const ADDRESSEE:Record<string,string>={
  robot:'对机器人说',person:'对别人说',self_or_media:'自言自语/电视',unclear:'无法判断',
};
const MOTION:Record<string,string>={
  none:'不做',surprise:'惊讶',smirk:'坏笑',proud:'得意',skeptical:'怀疑',shy:'害羞',
  agree_micro_nod:'点头',shake_head:'摇头',look_left:'看左边',look_right:'看右边',
  look_up:'看上面',fast_double_blink:'眨眼',
};

const MODEL_NAME:Record<string,string>={
  'd3-NVFP4':'d3','qwen-flash':'阿里 qwen-flash',
  'decision-model-preview':'阿里决策模型',decision:'阿里决策模型',
};
export const modelName=(model?:string|null)=>model?MODEL_NAME[model]??model:'';

/** The model in use for the decision card: d3 while it answers, Aliyun otherwise. */
export function decisionSource(status?:DecisionStatus|null)
  :{label:string;title:string;tone:'good'|'warn'|'off'}|null{
  if(!status||!(status.addressee||status.auto_expression))return null;
  const d3=status.d3;
  if(d3?.ready)return {label:'d3 在线',tone:'good',
    title:`自建 ${d3.model}：对话对象和表情都由它判断，出错自动回退阿里`};
  const using=[
    status.addressee&&status.addressee_model?'对话对象 '+modelName(status.addressee_model):'',
    status.auto_expression&&status.expression_model?'表情 '+modelName(status.expression_model):'',
  ].filter(Boolean).join('，');
  if(!d3)return {label:'阿里',tone:'off',title:`未配置 d3；${using||'阿里'}`};
  const why=d3.status==='probing'?'d3 探测中':'d3 不可用'+(d3.error?`（${d3.error}）`:'');
  return {label:d3.status==='probing'?'阿里 · d3 探测中':'阿里保底 · d3 离线',tone:'warn',
    title:`${why}，正用阿里保底：${using||'阿里'}`};
}

const metrics=(event:DecisionEvent)=>[
  modelName(event.model),
  typeof event.confidence==='number'?'置信 '+event.confidence.toFixed(2):'',
  typeof event.latency_ms==='number'?Math.round(event.latency_ms)+' ms':'',
].filter(Boolean).join(' · ');

/** One readable row per decision; other events return null. */
export function decisionEntry(event:DecisionEvent):DecisionEntry|null{
  const text=(event.text??'').trim();
  const turn_id=(event.turn_id??event.item_id)??undefined;
  const item_id=event.item_id??undefined;
  if(event.kind==='addressee.decided'){
    const choice=event.error?'决策失败':ADDRESSEE[event.choice??'']??'无法判断';
    return {seq:event.seq,topic:'对谁说话',choice,text,
      outcome:event.verdict==='robot'?'回复':'不回复，继续听',
      detail:event.error?'按对机器人说处理':metrics(event),
      turn_id,item_id};
  }
  if(event.kind==='action.auto_decided'){
    const choice=MOTION[event.choice??'']??event.choice??'—';
    return {seq:event.seq,topic:'表情动作',choice,text,
      outcome:event.script?'执行'+(MOTION[event.script]??event.script):'不做',
      detail:event.source==='explicit'?'用户点名，未调用模型':metrics(event),
      turn_id,item_id};
  }
  if(event.kind==='turn.recovered'){
    return {seq:event.seq,topic:'判停补发',choice:'点名提问被判无效',text,
      outcome:'已按识别终稿补发',detail:'',turn_id,item_id};
  }
  return null;
}

/** Side talk the decision model declined is shown as a decision, not ambient noise. */
export const isProviderAmbient=(event:{kind:string;reason?:string})=>
  event.kind==='transcript.ambient'&&event.reason!=='not_addressed';

export const formatDecision=(entry:DecisionEntry)=>
  `${entry.topic}：${entry.choice} → ${entry.outcome}`+(entry.detail?` · ${entry.detail}`:'')
  +(entry.text?` 「${entry.text}」`:'');

export type ParsedDecision={
  topicLabel:string;
  topicIcon:string;
  topicKind:'motion'|'addressee'|'recovery'|'other';
  outcomeText:string;
  outcomeSub:string;
  outcomeType:'quiet'|'action'|'accepted'|'declined'|'recovery'|'other';
  metrics:string[];
};

export function parseDecision(entry:DecisionEntry):ParsedDecision{
  let topicLabel=entry.topic;
  let topicIcon='💡';
  let topicKind:ParsedDecision['topicKind']='other';

  if(entry.topic==='表情动作'){
    topicLabel='表情动作';
    topicIcon='🎭';
    topicKind='motion';
  }else if(entry.topic==='对谁说话'){
    topicLabel='目标判定';
    topicIcon='🎯';
    topicKind='addressee';
  }else if(entry.topic==='判停补发'){
    topicLabel='判停补发';
    topicIcon='⚡';
    topicKind='recovery';
  }

  let outcomeText='';
  let outcomeSub='';
  let outcomeType:ParsedDecision['outcomeType']='other';

  if(entry.topic==='表情动作'){
    if(entry.outcome==='不做'){
      outcomeText='保持自然';
      outcomeSub='无需动作';
      outcomeType='quiet';
    }else if(entry.outcome.startsWith('执行')){
      const act=entry.outcome.slice(2);
      outcomeText=act?`执行动作：${act}`:'执行动作';
      outcomeSub='';
      outcomeType='action';
    }else{
      outcomeText=entry.outcome;
      outcomeSub=entry.choice;
      outcomeType='other';
    }
  }else if(entry.topic==='对谁说话'){
    if(entry.outcome==='回复'){
      if(entry.choice==='决策失败'){
        outcomeText='容错回复';
        outcomeSub='决策异常';
      }else{
        outcomeText='命中回复';
        outcomeSub='对机器人说';
      }
      outcomeType='accepted';
    }else if(entry.outcome.startsWith('不回复')){
      outcomeText='旁话忽略';
      outcomeSub=entry.choice?`判定：${entry.choice}`:'不回复，继续听';
      outcomeType='declined';
    }else{
      outcomeText=entry.outcome;
      outcomeSub=entry.choice;
      outcomeType='other';
    }
  }else if(entry.topic==='判停补发'){
    outcomeText='已按终稿补发';
    outcomeSub='点名提问挽回';
    outcomeType='recovery';
  }else{
    outcomeText=`${entry.choice} → ${entry.outcome}`;
    outcomeSub='';
    outcomeType='other';
  }

  const metrics=entry.detail
    ?entry.detail.split(' · ').map(s=>s.trim()).filter(Boolean)
    :[];

  return {
    topicLabel,
    topicIcon,
    topicKind,
    outcomeText,
    outcomeSub,
    outcomeType,
    metrics,
  };
}
