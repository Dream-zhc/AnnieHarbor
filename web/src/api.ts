export type SpeechStatus={state:string;voice?:string;turn_id?:string|null;queue_depth?:number;pcm_frames_published?:number;pcm_samples_published?:number;first_audio_ms?:number|null;first_publish_ms?:number|null;playback_evidence?:string;viseme?:string;error?:string|null;last_stop_reason?:string|null;played_sample_count?:number;started_reported_ms?:number|null;stop_requested_ms?:number|null;stop_published_ms?:number|null;stop_confirmed_ms?:number|null;last_stop_evidence?:string;stopped_conversation_id?:string|null};
export type RestoredContext={robot_id:string;chat_id:string;persona_id:string;session_epoch:number;generation:number;provider_session_id:string|null;chars_used:number;char_budget:number;unit:string;omitted_older_turns:number;turns:Array<{turn_id:string;user_text:string;assistant_text:string;assistant_source:string;assistant_excerpt:boolean}>};
export type PersonaSnapshot={robot_id:string;chat_id:string;persona_id:string;persona_revision:number;session_epoch:number;generation:number;provider_session_id:string|null;configured:boolean;label:string|null;question_pressure:'normal'|'low'|'none'};
export type PersonaProfile={persona_id:string;label:string};
export type Robot = {robot_id:string;room_id:string;persona_id:string;state:string;last_input?:string;last_reply?:string;error?:string;completed?:number;cancelled?:number;queue_depth?:number;realtime?:{ready:boolean;state:string;error?:string|null;response_id?:string|null;provider_session_id?:string|null;session_frames_sent?:number;speech_started_count?:number;transcripts_completed?:number;invalid_turns?:number;last_provider_speech_ms?:number|null;last_provider_transcript_ms?:number|null;speech_output?:SpeechStatus;restored_context?:RestoredContext|null;persona_snapshot?:PersonaSnapshot|null}};
export type Session = {robot_id:string;room_id:string;chat_id:string;persona_id:string;persona_revision:number;session_epoch:number;generation:number};
export type Health = {ready:boolean;status:string;pid:number;robots:Robot[];capabilities:Record<string,string>};
export type RealtimeEvent={
  seq:number;kind:string;robot_id:string;chat_id:string;persona_id:string;
  session_epoch:number;generation:number;turn_id?:string|null;text?:string;stash?:string;delta?:string;status?:string;
  user_text?:string;
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
export const eventBelongsToSession = (
  event:RealtimeEvent,
  session:Pick<Session,'robot_id'|'chat_id'|'persona_id'|'session_epoch'|'generation'>
) => event.robot_id===session.robot_id&&event.chat_id===session.chat_id&&
  event.persona_id===session.persona_id&&event.session_epoch===session.session_epoch&&
  event.generation===session.generation;
