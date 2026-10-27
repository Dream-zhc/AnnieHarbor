/** Her mood, energy, day, open threads, what is on her mind and her motions, as the
 * workbench shows them. Read-only: GET /api/robots/{id}/inner-life. Nothing here is
 * estimated in the browser; every line is a field the runtime reported. */

export type InnerLifeAppraisal={choice:string|null;confidence:number|null;applied:boolean;model:string|null;ts_ms:number};
export type InnerLifeMotion={kind:string;script:string|null;name?:string|null;status?:string;ts_ms:number;
  sentence?:string;reason?:string;choice?:string|null};
export type InnerLife={
  robot_id:string;enabled:boolean;started?:boolean;
  mood?:{label:string;text:string;valence:number;energy:number;energy_label:string;tone:string;reason:string;reason_ms:number|null};
  today?:Array<{kind:string;text:string;count:number;person:string|null;created_ms:number}>;
  partner?:string|null;
  threads?:Array<{person:string|null;text:string;follow_up:string;asks:number;created_ms:number;visible:boolean}>;
  reflection?:{day:string;self:string;people:number;partner_impression:string|null}|null;
  thought?:{status:string;intent?:string;weight?:number;ts_ms?:number};
  proactive_last_hour?:number;
  conversation?:{open:boolean;turns?:number;who?:string|null;started_ms?:number};
  summarizer?:string|null;
  hint_in_session?:boolean;
  has_hint?:boolean;
  stats?:{appraisals:number;appraisal_failures:number;summaries:number;summary_failures:number;
    reflections?:number;reflection_failures?:number;thoughts_spoken?:number;write_failures:number;
    last_appraisal:InnerLifeAppraisal|null};
  motions?:{actions_enabled?:boolean;listening:boolean;speaking:boolean;reactions:boolean;per_reply:number;
    recent:InnerLifeMotion[];stats:{listen:number;speech:number;inner:number;skipped:number;decision_failures:number}}|null;
};

const KIND_ICON:Record<string,string>={touch:'🤲',smell:'👃',close:'👀',arrival:'👋',met:'🤝',chat:'💬'};
const CHOICE_LABEL:Record<string,string>={happy:'开心',moved:'感动',hurt:'委屈',worried:'担心',bored:'无聊',neutral:'平常'};
const THOUGHT_LABEL:Record<string,string>={
  idle:'还没开始',queued:'刚说出口',nothing_on_mind:'没什么想说的',below_threshold:'有点想说，但还不到开口的程度',
  no_face_in_view:'画面里没有人脸',conversation_busy:'正在对话',conversation_recent:'刚聊过，先不插话',
  asked_to_be_quiet:'刚被要求安静',dialogue_declined:'对方不想被追问',external_event_pending:'有事件在排队',
  proactive_hourly_cap:'这一小时主动说够了（4 次）',proactive_gap:'离上次主动开口不到 5 分钟',
  realtime_or_tts_unavailable:'实时会话或语音未就绪',
};
const MOTION_KIND:Record<string,string>={listen:'倾听',speech:'说话',inner:'内心'};

export const beijingTime=(ms:number)=>new Date(ms).toLocaleTimeString('zh-CN',
  {hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'});

export type InnerLifeView={
  badge:string;ready:boolean;mood:string;energy:string;valence:string;
  day:Array<{time:string;icon:string;text:string}>;
  conversation:string;appraisal:string;summarizer:string;session:string;failures:string;
  partner:string;threads:string[];impression:string;lately:string;thought:string;
  motions:string;motionLines:string[];
};

function thoughtLabel(status:string){
  if(status.startsWith('not_queued'))return '没能排进说话队列';
  return THOUGHT_LABEL[status]??status;
}

export function innerLifeView(data:InnerLife|null,status:string):InnerLifeView{
  const empty={mood:'—',energy:'—',valence:'—',day:[],conversation:'—',appraisal:'—',
    summarizer:'—',session:'—',failures:'',partner:'—',threads:[],impression:'—',lately:'—',
    thought:'—',motions:'—',motionLines:[]};
  if(!data)return {...empty,badge:status,ready:false};
  const motionsOff=!data.motions;
  const motions=motionsOff?'未启用：config/runtime_defaults.toml [motions] 或 ANNIE_INTERACTION_MOTIONS_ENABLED':
    data.motions!.actions_enabled===false?'已打开，但真动作没开（.env 的 ANNIE_ACTIONS_ENABLED=1 才会发到机器人）':
    `倾听 ${data.motions!.stats.listen} · 说话 ${data.motions!.stats.speech} · 内心 ${data.motions!.stats.inner} · 跳过 ${data.motions!.stats.skipped}`;
  const motionLines=(data.motions?.recent??[]).slice(-6).reverse().map(row=>
    `${beijingTime(row.ts_ms)} ${MOTION_KIND[row.kind]??row.kind}：${row.name??row.script??'不配动作'}`+
    (row.sentence?`（“${row.sentence}”）`:row.reason?`（${row.reason}）`:'')+
    (row.status&&row.status!=='published_unknown'?` · ${row.status}`:''));
  if(!data.enabled)return {...empty,badge:'未启用',ready:false,motions,motionLines,
    mood:'未启用：config/runtime_defaults.toml [inner_life] 或 ANNIE_INNER_LIFE_ENABLED'};
  const mood=data.mood;
  const stats=data.stats;
  const last=stats?.last_appraisal;
  const talk=data.conversation;
  const failures=[
    stats?.appraisal_failures?`心情判断失败 ${stats.appraisal_failures} 次`:'',
    stats?.summary_failures?`摘要失败 ${stats.summary_failures} 次`:'',
    stats?.reflection_failures?`反思失败 ${stats.reflection_failures} 次`:'',
    stats?.write_failures?`写库失败 ${stats.write_failures} 次`:'',
  ].filter(Boolean).join('；');
  const thought=data.thought;
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
    summarizer:data.summarizer?`${data.summarizer}（对话结束后写摘要、记待续话题）`:'未配置：只记“聊了N轮”',
    session:data.hint_in_session?'已写进当前会话提示词':
      data.has_hint===false?'没有要写的（心情平静，也没认出有待续话题的人）':'还没写进去（实时会话未连接，或正在等下一次更新）',
    failures,
    partner:data.partner?`${data.partner}（声纹或画面认出）`:'没认出',
    threads:(data.threads??[]).map(row=>`${row.person??'没认出的人'}：${row.text}`+
      (row.asks?` · 问过 ${row.asks} 次`:'')+(row.visible?'':' · 暂不提')),
    impression:data.reflection?.partner_impression??(data.partner?'还没有印象':'—'),
    lately:data.reflection?.self?`${data.reflection.self}（${data.reflection.day} 的回顾）`:'还没做过每晚反思',
    thought:thought?thoughtLabel(thought.status)+(thought.intent?`：${thought.intent}`:'')+
      (typeof data.proactive_last_hour==='number'?` · 这一小时已主动说 ${data.proactive_last_hour} 次`:''):'—',
    motions,motionLines,
  };
}
