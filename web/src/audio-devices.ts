/** One microphone and one speaker per Room: readable warnings and browser reports. */
export type AudioDevice={instance_id:string;host?:string|null;device_name?:string|null;
  kind?:string;state?:string|null;age_ms?:number;head_name?:string|null;
  head_instance?:string|null;volume?:number|null};
/** A robot head (board) serving the Room; the one started last is in use. */
export type RobotHead={instance_id:string;head_id?:string|null;head_name?:string|null;
  host?:string|null;address?:string|null;microphone?:string|null;speaker?:string|null;
  active_for_ms?:number;age_ms?:number};
export type HeadRef={instance_id?:string|null;head_name?:string|null;address?:string|null;host?:string|null};
export type HeadState={current:RobotHead|null;heads:RobotHead[];
  handover:{from:HeadRef;to:HeadRef;at_ms:number;age_ms:number}|null};
export type AudioConflict={role:'microphone'|'speaker';
  reason:'multiple_devices'|'frame_rate'|'multiple_playbacks';
  count?:number;frames_per_second?:number;sources?:string[]};
export type AudioDevices={status:'ok'|'conflict';conflicts:AudioConflict[];
  microphones:AudioDevice[];speakers:AudioDevice[];mic_frames_per_second?:number|null;
  head?:HeadState};

/** Read /inputs.audio_devices defensively; an older backend has none. */
export function audioDevices(value:unknown):AudioDevices|null{
  const devices=(value as {audio_devices?:unknown}|null)?.audio_devices as AudioDevices|undefined;
  if(!devices||typeof devices!=='object'||!Array.isArray(devices.conflicts))return null;
  return {...devices,microphones:Array.isArray(devices.microphones)?devices.microphones:[],
    speakers:Array.isArray(devices.speakers)?devices.speakers:[]};
}

export const deviceLabel=(device:AudioDevice)=>
  (device.device_name||'未知设备')+'（'+(device.kind==='browser'?'网页':device.head_name||device.host||'板端')+'）';

/** "大头机器人（192.168.1.188）" */
export const headLabel=(head:HeadRef|null|undefined)=>
  !head?'未知的头':(head.head_name||head.host||'未命名的头')+(head.address?`（${head.address}）`:'');

/** Shown for this long after a handover; the event list keeps it for good. */
export const HANDOVER_NOTICE_MS=10*60_000;

/** One sentence after a misstart: who took over and who stopped by itself. */
export function handoverMessage(head:HeadState|null|undefined):string|null{
  const handover=head?.handover;
  if(!handover||handover.age_ms>HANDOVER_NOTICE_MS)return null;
  const at=new Date(handover.at_ms).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  return `${at} ${headLabel(handover.to)}后启动，已接管麦克风和扬声器；${headLabel(handover.from)}已自动断开。`;
}

/** The SSE robot.head.handover event, before /inputs is refreshed. */
export const handoverEventMessage=(event:{from_name?:string;from_address?:string;
  to_name?:string;to_address?:string})=>
  `${headLabel({head_name:event.to_name,address:event.to_address})}后启动，已接管；`+
  `${headLabel({head_name:event.from_name,address:event.from_address})}已自动断开`;

const names=(devices:AudioDevice[])=>devices.map(deviceLabel).join('、');

/** One sentence per conflict, naming the devices so people know which to close. */
export function conflictMessages(devices:AudioDevices|null,roomId?:string):string[]{
  if(!devices||devices.status!=='conflict')return [];
  const room=roomId||'当前房间';
  return devices.conflicts.map(conflict=>{
    if(conflict.reason==='frame_rate')
      return `${room} 的麦克风音频约 ${conflict.frames_per_second??'—'} 帧/秒（一个麦克风是 50），可能有多个麦克风在同时发送，请只保留一个。`;
    if(conflict.reason==='multiple_playbacks')
      return `${room} 同一段回复有 ${conflict.count??2} 个扬声器在播放`+
        (conflict.sources?.length?`（${conflict.sources.map(s=>s==='browser_test'?'网页试听':'实体扬声器').join('、')}）`:'')+
        '，请只保留一个。';
    const list=conflict.role==='microphone'?devices.microphones:devices.speakers;
    return `${room} 同时有 ${conflict.count??list.length} 个${conflict.role==='microphone'?'麦克风':'扬声器'}在工作`+
      (list.length?`：${names(list)}`:'')+'，请只保留一个。';
  });
}

/** Per page session; tells Brain this tab is one device, not a person's identity. */
export function browserInstanceId(role:'microphone'|'speaker'){
  const random=Math.random().toString(36).slice(2,10);
  return `browser-${role}-${random}`;
}
