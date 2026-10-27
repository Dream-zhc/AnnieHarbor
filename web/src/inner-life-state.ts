/** Her mood, energy and today's real experiences, as the workbench shows them.
 * Read-only: GET /api/robots/{id}/inner-life. Nothing here is estimated in the
 * browser; every line is a field the runtime reported. */

export type InnerLifeAppraisal={choice:string|null;confidence:number|null;applied:boolean;model:string|null;ts_ms:number};
export type InnerLife={
  robot_id:string;enabled:boolean;started?:boolean;
  mood?:{label:string;text:string;valence:number;energy:number;energy_label:string;tone:string;reason:string;reason_ms:number|null};
  today?:Array<{kind:string;text:string;count:number;person:string|null;created_ms:number}>;
  conversation?:{open:boolean;turns?:number;who?:string|null;started_ms?:number};
  summarizer?:string|null;
  hint_in_session?:boolean;
  stats?:{appraisals:number;appraisal_failures:number;summaries:number;summary_failures:number;write_failures:number;last_appraisal:InnerLifeAppraisal|null};
};

const KIND_ICON:Record<string,string>={touch:'🤲',smell:'👃',close:'👀',arrival:'👋',met:'🤝',chat:'💬'};
const CHOICE_LABEL:Record<string,string>={happy:'开心',moved:'感动',hurt:'委屈',worried:'担心',bored:'无聊',neutral:'平常'};

export const beijingTime=(ms:number)=>new Date(ms).toLocaleTimeString('zh-CN',
  {hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});

export type InnerLifeView={
  badge:string;ready:boolean;mood:string;energy:string;valence:string;
  day:Array<{time:string;icon:string;text:string}>;
  conversation:string;appraisal:string;summarizer:string;session:string;failures:string;
};

export function innerLifeView(data:InnerLife|null,status:string):InnerLifeView{
  const empty={mood:'—',energy:'—',valence:'—',day:[],conversation:'—',appraisal:'—',
    summarizer:'—',session:'—',failures:''};
  if(!data)return {...empty,badge:status,ready:false};
  if(!data.enabled)return {...empty,badge:'未启用',ready:false,
    mood:'未启用：config/runtime_defaults.toml [inner_life] 或 ANNIE_INNER_LIFE_ENABLED'};
  const mood=data.mood;
  const stats=data.stats;
  const last=stats?.last_appraisal;
  const talk=data.conversation;
  const failures=[
    stats?.appraisal_failures?`心情判断失败 ${stats.appraisal_failures} 次`:'',
    stats?.summary_failures?`摘要失败 ${stats.summary_failures} 次`:'',
    stats?.write_failures?`写库失败 ${stats.write_failures} 次`:'',
  ].filter(Boolean).join('；');
  return {
    badge:mood?.label??'—',ready:!!data.started,
    mood:mood?.text??'—',
    energy:mood?`${mood.energy_label}（${Math.round(mood.energy*100)}%）`:'—',
    valence:mood?`${mood.valence>0?'+':''}${mood.valence.toFixed(2)}（-1 难过 … +1 开心）`:'—',
    day:(data.today??[]).map(row=>({time:beijingTime(row.created_ms),icon:KIND_ICON[row.kind]??'•',
      text:row.text+(row.count>1?`（共${row.count}次）`:'')})),
    conversation:talk?.open?`进行中 · ${talk.turns??0} 轮${talk.who?' · 和'+talk.who:' · 没认出是谁'}`:'没有进行中的对话',
    appraisal:last?`${CHOICE_LABEL[last.choice??'']??last.choice??'未知'}`+
      (typeof last.confidence==='number'?` · 置信度 ${last.confidence.toFixed(2)}`:'')+
      (last.applied?' · 已影响心情':' · 未影响（平常或把握不够）'):'还没有判断过',
    summarizer:data.summarizer?`${data.summarizer}（对话结束后写一句摘要）`:'未配置：只记“聊了N轮”',
    session:data.hint_in_session?'已写进当前会话提示词':'未写入（实时会话未连接，或正在等下一次更新）',
    failures,
  };
}
