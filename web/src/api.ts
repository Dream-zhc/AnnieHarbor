export type SpeechStatus={state:string;voice?:string;turn_id?:string|null;queue_depth?:number;pcm_frames_published?:number;pcm_samples_published?:number;first_audio_ms?:number|null;first_publish_ms?:number|null;playback_evidence?:string;playback_control_state?:string;playback_control_pending?:number;viseme?:string;error?:string|null;last_stop_reason?:string|null;played_sample_count?:number;started_reported_ms?:number|null;stop_requested_ms?:number|null;stop_published_ms?:number|null;stop_confirmed_ms?:number|null;last_stop_evidence?:string;stopped_conversation_id?:string|null;speed_ratio?:number;underruns?:number;silence_ms?:number;interrupt_ms?:number};
export type TurnTimeline={turn_id:string;user_text?:string|null;provider_session_id?:string|null;viseme_count?:number;times:Record<string,number|null>;durations:Record<string,number|null>;duration_sources?:Record<string,string>;input_mode?:string;status?:string};
export type LatencyPercentiles=Record<string,{samples:number;p50_ms:number|null;p95_ms:number|null;missing?:number}>;
export type LatencyWindow={turns:number;window_capacity:number;by_input:Record<string,number>;statuses:Record<string,number>;speaker_receipt_missing:number};
export type RestoredContext={robot_id:string;chat_id:string;persona_id:string;session_epoch:number;generation:number;provider_session_id:string|null;chars_used:number;char_budget:number;unit:string;omitted_older_turns:number;turns:Array<{turn_id:string;user_text:string;assistant_text:string;assistant_source:string;assistant_excerpt:boolean}>};
export type PersonaSnapshot={robot_id:string;chat_id:string;persona_id:string;style_id:string;persona_revision:number;session_epoch:number;generation:number;provider_session_id:string|null;configured:boolean;label:string|null;question_pressure:'normal'|'low'|'none'};
export type DialoguePolicy={mode:string;length:string;short_reply_streak:number;delivered_question_streak:number;recent_user_interrupt:boolean;followup:'avoid'|'contextual';provider_hint_confirmed:boolean};
export type PersonaProfile={persona_id:string;label:string};
export type PromptSnapshot={robot_id:string;chat_id:string;persona_id:string;session_epoch:number;generation:number;provider_session_id:string|null;instructions:string;tool_names:string[];bootstrap_text:string;bootstrap_count:number;history_turns_requested:number;history_turns_confirmed:number|null;restored_turns?:RestoredContext['turns'];restored_chars_used?:number;restored_char_budget?:number;restored_older_omitted?:number};
export type ToolDiagnostic={seq:number;kind:string;robot_id:string;chat_id:string;persona_id:string;session_epoch:number;generation:number;provider_session_id?:string|null;turn_id?:string|null;call_id?:string;name?:string;arguments?:string;result?:unknown;valid?:boolean;duration_ms?:number;evidence_count?:number|null;ts_ms?:number};
export type RealtimeDiagnostics={robot_id:string;chat_id:string;persona_id:string;session_epoch:number;generation:number;ready:boolean;provider_session_id:string|null;prompt_snapshot:PromptSnapshot|null;prompt_history:PromptSnapshot[];turn_prompt_refs:Array<{turn_id:string;provider_session_id:string|null}>;turn_prompt_snapshots?:Array<{turn_id:string;prompt:PromptSnapshot}>;tool_history:ToolDiagnostic[];turn_timelines?:TurnTimeline[];turn_events?:RealtimeEvent[];dialogue_policy?:DialoguePolicy;latency_percentiles?:LatencyPercentiles;latency_window?:LatencyWindow};
export type Robot = {robot_id:string;room_id:string;persona_id:string;state:string;last_input?:string;last_reply?:string;error?:string;completed?:number;cancelled?:number;queue_depth?:number;realtime?:{ready:boolean;state:string;error?:string|null;response_id?:string|null;provider_session_id?:string|null;provider_history_turns?:number;provider_history_turns_confirmed?:number|null;restore_char_budget?:number;session_frames_sent?:number;speech_started_count?:number;transcripts_completed?:number;invalid_turns?:number;last_provider_speech_ms?:number|null;last_provider_transcript_ms?:number|null;last_asr_first_delta_ms?:number|null;asr_delta_count?:number;last_turn_timeline?:TurnTimeline|null;latency_percentiles?:LatencyPercentiles;speech_output?:SpeechStatus;restored_context?:RestoredContext|null;persona_snapshot?:PersonaSnapshot|null}};
export type Session = {robot_id:string;room_id:string;chat_id:string;persona_id:string;persona_revision:number;session_epoch:number;generation:number};
export type Health = {ready:boolean;status:string;pid:number;robots:Robot[];capabilities:Record<string,string>;performance?:{cpu_percent:number|null;rss_peak_mb:number|null;loop_lag_ms:number|null;loop_lag_max_ms:number|null;samples:number;memory_source:string};storage?:{trace_dropped:number;trace_queue_depth:number;trace_queue_capacity:number;ledger_queue_depth:number;ledger_queue_capacity:number;ledger_error:string|null;error:string|null}};
export type RealtimeEvent={
  seq:number;kind:string;robot_id:string;chat_id:string;persona_id:string;
  session_epoch:number;generation:number;turn_id?:string|null;text?:string;stash?:string;delta?:string;status?:string;
  user_text?:string;item_id?:string;ts_ms?:number;after_speech_ms?:number|null;
  turn_source?:'user'|'external';
  error?:string;result?:unknown;name?:string;arguments?:string;first_text_ms?:number;
  call_id?:string;valid?:boolean;duration_ms?:number;evidence_count?:number|null;
  provider_session_id?:string|null;
  action_kind?:string;action_value?:string;real_device_executed?:boolean;device_completion?:string;
  source?:string;evidence?:string;timing_source?:string;viseme?:string;
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
