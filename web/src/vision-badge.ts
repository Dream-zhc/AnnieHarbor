import type {RealtimeDiagnostics,RealtimeEvent} from './api';

/** What a turn sent to a vision model, rebuilt from its vision.intent.* events.
 * looking/seen/failed: the vision service was asked to look; judged: only the
 * look decision got the camera frame and she did not go and look. */
export type TurnVision={
  state:'looking'|'seen'|'failed'|'judged';
  route:string|null;model:string|null;chars:number|null;totalMs:number|null;
  status:string|null;error:string|null;reason:string|null;
  judgeImage:boolean;judgeModel:string|null;frameAgeMs:number|null;
};

const VISION_KINDS=new Set(['vision.intent.decided','vision.intent.started',
  'vision.intent.result','vision.intent.abandoned']);
export const isVisionEvent=(kind:string)=>VISION_KINDS.has(kind);

export function turnVision(turnId:string|undefined,events:RealtimeEvent[],
  diagnostics?:RealtimeDiagnostics|null):TurnVision|null{
  if(!turnId)return null;
  const bySeq=new Map<number,RealtimeEvent>();
  for(const event of [...(diagnostics?.turn_events??[]),...events])
    if(event.turn_id===turnId&&isVisionEvent(event.kind))bySeq.set(event.seq,event);
  const latest:Partial<Record<string,RealtimeEvent>>={};
  for(const event of [...bySeq.values()].sort((a,b)=>a.seq-b.seq))latest[event.kind]=event;
  const decided=latest['vision.intent.decided'];
  const started=latest['vision.intent.started'];
  const after=(event?:RealtimeEvent)=>event&&started&&event.seq>started.seq?event:undefined;
  const result=after(latest['vision.intent.result']);
  const abandoned=after(latest['vision.intent.abandoned']);
  const judgeImage=decided?.image===true;
  if(!started&&!judgeImage)return null;
  const state=!started?'judged':result?(result.status==='ok'?'seen':'failed'):abandoned?'failed':'looking';
  return {
    state,route:result?.route??null,model:result?.model??null,chars:result?.chars??null,
    totalMs:result?.total_ms??null,status:result?.status??null,error:result?.error??null,
    reason:result?null:abandoned?.reason??null,judgeImage,
    judgeModel:judgeImage?decided?.model??decided?.judge??null:null,
    frameAgeMs:judgeImage?decided?.frame_age_ms??null:null,
  };
}

const ROUTES:Record<string,string>={cloud:'云端',local:'本地'};
const FAILURES:Record<string,string>={
  timeout:'视觉端没按时回',send_failed:'请求没发出去',expired:'视觉端超时',superseded:'被下一问替换',
  no_frame:'摄像头没画面',cloud_unavailable:'没配云端视觉',cloud_error:'云端视觉出错',
  cloud_timeout:'云端视觉超时',local_error:'本地视觉出错',invalid_request:'请求字段不对',
};
const REASONS:Record<string,string>={turn_changed:'用户又说了话',realtime_connection:'实时连接断开'};
const LABELS:Record<TurnVision['state'],string>={
  looking:'看画面中…',seen:'看了画面',failed:'没看成',judged:'带图判断',
};

export function visionBadge(vision:TurnVision){
  const parts:string[]=[];
  if(vision.state!=='judged'){
    const where=[vision.route?ROUTES[vision.route]??vision.route:null,vision.model].filter(Boolean);
    parts.push('请视觉模型看画面'+(where.length?'：'+where.join(' · '):''));
    if(vision.state==='looking')parts.push('等描述中');
    if(vision.state==='seen'&&vision.chars!=null)parts.push(`描述 ${vision.chars} 字`);
    if(vision.state==='failed'){
      const why=vision.reason?REASONS[vision.reason]??vision.reason:
        FAILURES[vision.error??'']??FAILURES[vision.status??'']??vision.error??vision.status;
      parts.push('没看成'+(why?'：'+why:''));
    }
    if(vision.totalMs!=null)parts.push(`共 ${(vision.totalMs/1000).toFixed(1)} 秒`);
  }
  if(vision.judgeImage){
    const detail=[vision.judgeModel,vision.frameAgeMs!=null?`画面 ${vision.frameAgeMs} ms 前`:null].filter(Boolean);
    parts.push('判断要不要看时带了摄像头画面'+(detail.length?`（${detail.join('，')}）`:''));
    if(vision.state==='judged')parts.push('这次没去看');
  }
  return {icon:vision.state==='judged'?'🖼':'👁',label:LABELS[vision.state],title:parts.join(' · '),tone:vision.state};
}
