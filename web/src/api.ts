export type SpeechStatus={state:string;voice?:string;turn_id?:string|null;queue_depth?:number;pcm_frames_published?:number;pcm_samples_published?:number;first_audio_ms?:number|null;first_publish_ms?:number|null;playback_evidence?:string;viseme?:string;error?:string|null};
export type Robot = {robot_id:string;room_id:string;persona_id:string;state:string;last_input?:string;last_reply?:string;error?:string;completed?:number;cancelled?:number;queue_depth?:number;realtime?:{ready:boolean;state:string;error?:string|null;speech_output?:SpeechStatus}};
export type Session = {robot_id:string;room_id:string;chat_id:string;persona_id:string;persona_revision:number;session_epoch:number;generation:number};
export type Health = {ready:boolean;status:string;pid:number;robots:Robot[];capabilities:Record<string,string>};
export type RealtimeEvent={
  seq:number;kind:string;robot_id:string;chat_id:string;persona_id:string;
  session_epoch:number;generation:number;text?:string;delta?:string;status?:string;
  error?:string;result?:unknown;name?:string;arguments?:string;first_text_ms?:number;
};
export type RealtimeEvents={events:RealtimeEvent[];gap:boolean;latest:number};
export const robotPath = (id:string) => `/api/robots/${encodeURIComponent(id)}`;
export async function api<T>(path:string, method='GET', body?:unknown, signal?:AbortSignal,acceptedStatuses:number[]=[]):Promise<T> {
  const response=await fetch(path,{method,headers:body===undefined?{}:{'content-type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal});
  let data:unknown;
  try {data=await response.json();}catch{throw new Error(`HTTP ${response.status}：响应不是 JSON`);}
  if(!response.ok&&!acceptedStatuses.includes(response.status)){const detail=(data as {detail?:unknown})?.detail;throw new Error(`HTTP ${response.status}：${typeof detail==='string'?detail:JSON.stringify(detail??'请求失败')}`);}
  return data as T;
}
export const sessionKey = (s:Session) => `${s.robot_id}\u0000${s.chat_id}\u0000${s.persona_id}\u0000${s.session_epoch}\u0000${s.generation}`;
