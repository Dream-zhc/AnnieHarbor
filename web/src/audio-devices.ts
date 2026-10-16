/** One microphone and one speaker per Room: readable warnings and browser reports. */
export type AudioDevice={instance_id:string;host?:string|null;device_name?:string|null;
  kind?:string;state?:string|null;age_ms?:number};
export type AudioConflict={role:'microphone'|'speaker';
  reason:'multiple_devices'|'frame_rate'|'multiple_playbacks';
  count?:number;frames_per_second?:number;sources?:string[]};
export type AudioDevices={status:'ok'|'conflict';conflicts:AudioConflict[];
  microphones:AudioDevice[];speakers:AudioDevice[];mic_frames_per_second?:number|null};

/** Read /inputs.audio_devices defensively; an older backend has none. */
export function audioDevices(value:unknown):AudioDevices|null{
  const devices=(value as {audio_devices?:unknown}|null)?.audio_devices as AudioDevices|undefined;
  if(!devices||typeof devices!=='object'||!Array.isArray(devices.conflicts))return null;
  return {...devices,microphones:Array.isArray(devices.microphones)?devices.microphones:[],
    speakers:Array.isArray(devices.speakers)?devices.speakers:[]};
}

export const deviceLabel=(device:AudioDevice)=>
  (device.device_name||'未知设备')+'（'+(device.kind==='browser'?'网页':device.host||'板端')+'）';

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
