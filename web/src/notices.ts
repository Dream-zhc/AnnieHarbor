const REASONS:Record<string,string>={timeout:'超时，没采到清楚的正脸',runtime_restarted:'视觉服务重启了',
  unreachable:'视觉服务连不上',registration_not_found:'视觉端找不到这次注册'};

/** Workbench notices for things changed by talking to Annie. */

/** Face registration: one notice per step. */
export function faceNotice(event:{kind:string;name?:string;reason?:string;
  saved?:number;target_samples?:number}):{text:string;type:'info'|'error'}|null{
  const who=event.name||'对方';
  switch(event.kind){
    case 'face.registration.started':return {text:`正在记住 ${who} 的脸，请 TA 正对摄像头`,type:'info'};
    case 'face.registration.completed':return {text:`已记住 ${who} 的脸`,type:'info'};
    case 'face.registration.failed':
      return {text:`没能记住 ${who} 的脸`+(event.reason?`（${REASONS[event.reason]??event.reason}）`:'')+
        (event.target_samples?`，采到 ${event.saved??0}/${event.target_samples}`:''),type:'error'};
    case 'face.registration.cancelled':return {text:`${who} 的人脸注册已取消`,type:'info'};
    case 'face.deleted':return {text:`已从人脸库删除 ${who}`,type:'info'};
    case 'face.delete.unconfirmed':return {text:`删除 ${who} 未确认，请检查视觉端人脸库`,type:'error'};
    default:return null;
  }
}

/** "大声点" turned the board speaker; via=voice_style when no board speaker takes a volume. */
export function volumeNotice(event:{kind:string;previous?:number;volume?:number;status?:string}):
  {text:string;type:'info'|'error'}|null{
  if(event.kind!=='speaker.volume.changed')return null;
  if(event.status==='unconfirmed')
    return {text:`扬声器没有确认音量调整（目标 ${event.volume}）`,type:'error'};
  return {text:`扬声器音量 ${event.previous} → ${event.volume}`,type:'info'};
}
