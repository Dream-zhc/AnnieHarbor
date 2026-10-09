/** UI-only phrasing. Canonical SQLite facts keep their original, neutral wording. */
export type MemoryFact={id:string;kind:string;persona_id:string;chat_id?:string;fact_key:string;text:string;
  source_turn_id:string|null;source_created_ms?:number;source_valid?:boolean;version:number;status:string;index_state:string;expires_at_ms?:number|null};
export type MemoryScope='shared'|'persona'|'chat';

export const invalidMemory=(item:MemoryFact)=>
  /^no[-_]?(extractable(?:[-_]?content)?|extraction|memory|fact|durable[-_]?fact)$|^(none|null|placeholder|empty[-_]?memory)$/i.test(item.fact_key)||
  /^(?:placeholder|无效抽取|用户(?:曾)?询问|用户问过|用户只是询问|本轮(?:没有|未))/.test(item.text)||
  /(?:未提取到|未发现|无(?:法|明确)?|没有|暂无|未能).{0,45}(?:长期记忆|持久偏好|有意义经历|事实|提取|可记忆内容)/.test(item.text)||
  /测试其(?:物理动作|安装软件)能力/.test(item.text);

export const visibleMemory=(item:MemoryFact,scope:MemoryScope,now:number)=>
  item.status==='active'&&!(item.expires_at_ms!=null&&item.expires_at_ms<=now)&&
  item.source_valid!==false&&!invalidMemory(item)&&
  (scope==='chat'?item.kind==='chat':scope==='shared'?item.kind==='semantic':
    item.kind==='persona'||item.kind==='episodes');

export const memoryType=(item:MemoryFact)=>item.fact_key.startsWith('ongoing_plan_')?'近期计划':
  item.kind==='episodes'?'经历':
  item.kind==='persona'?'关系':item.kind==='chat'?'会话约定':'偏好与事实';

export const memoryText=(value:string)=>{
  if(/用户曾(?:对当前AI助手|向当前 Persona|向当前AI助手).{0,15}(?:表示|表达)喜欢/.test(value)&&
     !/(后来|更正|不再|不喜欢|已不|并非)/.test(value))
    return '你曾向当前角色表达喜欢。';
  return value.replace(/^用户(?=曾|最近|此前|明确|现在|提到|说过|喜欢|不喜欢|希望|要求|偏好|习惯|计划|有|会|要|对)/,'你')
    .replace(/当前(?:AI助手| Persona|Persona|机器人)\s*/g,'当前角色')
    .replace(/(来找|联系|见到)他([。！!，,]|$)/g,'$1你$2')
    .replace(/不要骂他/g,'不要骂你');
};

export const memoryDate=(ms?:number)=>ms&&ms>=946684800000?
  new Date(ms).toLocaleString('zh-CN',{year:'numeric',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
