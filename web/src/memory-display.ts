/** UI-only phrasing. Canonical SQLite facts keep their original, neutral wording. */
export type MemoryFact={id:string;kind:string;persona_id:string;chat_id?:string;fact_key:string;text:string;
  source_turn_id:string|null;source_created_ms?:number;version:number;status:string;index_state:string;
  expires_at_ms?:number|null;importance?:number;hits?:number;hit_days?:number;last_hit_ms?:number|null;
  superseded_by?:string|null;origin?:string;retention?:number;faded?:boolean};
export type MemoryScope='shared'|'persona'|'chat';

export type RecallHit={id:string;text:string;score:number;why:string[]};
export type MemoryRecall={at_ms:number;source:'turn'|'tool';persona_id:string;query:string;hits:RecallHit[]};
export type ConsolidationResult={op:string;kind:string;text:string;importance:number;result:string;id?:string|null;target?:string};
export type MemoryConsolidation={at_ms:number;persona_id?:string;chat_id?:string;turns?:number;related?:number;
  results?:ConsolidationResult[];ms?:number;error?:string};
export type MemoryInsight={status:string;recalls?:MemoryRecall[];recall_turns?:number;
  consolidations?:MemoryConsolidation[];pending?:{turns:number;since_ms:number;last_ms:number}|null;
  recap?:{text:string;updated_ms:number;folded:number}|null;error?:string|null;idle_seconds?:number;max_turns?:number};

// Mirrors is_meta_memory in annie_brain/memory.py: rows memory never uses are not shown either.
export const invalidMemory=(item:MemoryFact)=>
  /^no[-_]?(extractable(?:[-_]?content)?|extraction|memory|fact|durable[-_]?fact)$|^(none|null|placeholder|empty[-_]?memory)$/i.test(item.fact_key)||
  /^(?:placeholder|无效抽取|用户(?:曾)?询问|用户问过|用户只是询问|用户提及地点为|用户称呼助手|本轮(?:没有|未))/.test(item.text)||
  /(?:未提取到|未发现|无(?:法|明确)?|没有|暂无|未能).{0,45}(?:长期记忆|持久偏好|有意义经历|事实|提取|可记忆内容)/.test(item.text)||
  /用户(?:的)?(?:名字|姓名|名)(?:是|叫|为)|用户(?:自称|名叫|叫做)|用户(?:说|称|表示|介绍)自己(?:叫|名叫)/.test(item.text)||
  /用户(?:称呼|叫|管)(?:当前|这个)?(?:的)?(?:AI|ai|助手|机器人|角色|小安妮|你)/.test(item.text)||
  /用户.{0,6}(?:要求|让|请求|叫).{0,8}(?:忘记|忘掉|退下|闭嘴|休眠)/.test(item.text)||
  /^用户不?喜欢(?:带|吃|穿|用|看|买|去)?[。.]?$/.test(item.text.trim())||
  /测试(?:其|她|小安妮|机器人)?.{0,6}能力/.test(item.text);

export const visibleMemory=(item:MemoryFact,scope:MemoryScope,now:number,withOutdated=false)=>
  (item.status==='active'||withOutdated&&item.status==='outdated')&&
  !(item.expires_at_ms!=null&&item.expires_at_ms<=now)&&!invalidMemory(item)&&
  (scope==='chat'?item.kind==='chat':scope==='shared'?item.kind==='semantic':
    item.kind==='persona'||item.kind==='episodes');

export const memoryType=(item:MemoryFact)=>item.kind==='episodes'?'经历':
  item.kind==='persona'?'关系':item.kind==='chat'?'会话约定':'偏好与事实';

export const memoryText=(value:string)=>{
  if(/用户曾(?:对当前AI助手|向当前 Persona|向当前AI助手).{0,15}(?:表示|表达)喜欢/.test(value)&&
     !/(后来|更正|不再|不喜欢|已不|并非)/.test(value))
    return '你曾向当前角色表达喜欢。';
  return value.replace(/^用户(?=曾|最近|此前|明确|现在|提到|说过|喜欢|不喜欢|希望|要求|偏好|习惯|计划|有|会|要|对|在|是|说)/,'你')
    .replace(/当前(?:AI助手| Persona|Persona|机器人)\s*/g,'当前角色')
    .replace(/(来找|联系|见到)他([。！!，,]|$)/g,'$1你$2')
    .replace(/不要骂他/g,'不要骂你');
};

export const memoryDate=(ms?:number|null)=>ms&&ms>=946684800000?
  new Date(ms).toLocaleString('zh-CN',{year:'numeric',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';

export const memoryClock=(ms?:number|null)=>ms?
  new Date(ms).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'';

export const importanceLabel=(value=0.5)=>value>=0.8?'很重要':value>=0.6?'重要':value>=0.4?'一般':'随口一提';

/** How strongly she still remembers it, 0-100: the forgetting curve the backend computed. */
export const memoryStrength=(item:MemoryFact)=>Math.round(Math.max(0,Math.min(1,item.retention??1))*100);

export const memoryOrigin=(item:MemoryFact)=>item.origin==='manual'?'手动添加或更正':
  item.origin==='auto'||item.source_turn_id?'对话后整理':'手动添加';

export const consolidationLabel:Record<string,string>={
  added:'新记下',merged:'合并进旧记忆',known:'早就记得',updated:'补充了旧记忆',outdated:'旧记忆标记过时',
  blocked:'和已遗忘的事太像，不再记',rejected:'不该记（名字、一次性要求等）',skipped:'没写入',
};
