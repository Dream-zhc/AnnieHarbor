import type {AudioDevices} from '../audio-devices';
import type {WakeState} from '../wake-state';

export type ActionPreview = {
  call_id: string | null;
  turn_id: string | null;
  generation: number | null;
  kind: string | null;
  value: string | null;
  status: string;
  real_device_executed: boolean;
  animation_kind: "command_preview";
};

export type ExhibitEvent = {
  kind: string;
  ts_ms: number | null;
  turn_id: string | null;
  seq: number | null;
  source?: string | null;
};

export type ExhibitSnapshot = {
  schema: "annie.exhibit.v1";
  generated_at_ms: number;
  access_mode: "intranet";
  robot: { robot_id: string; room_id: string; state: string; generation: number; ready: boolean };
  realtime: {
    state: string; wakeword: WakeState; speech_active: boolean | null; speech_output_state: string | null;
    playback_state?: string | null; playback_source?: string | null;
    viseme: string | null; playback_evidence: string | null; turn_id: string | null;
    user_text: string | null; assistant_text: string | null;
    last_turn_timeline: Record<string, unknown> | null;
  };
  audio: {
    frames_received: number | null; frames_accepted: number | null;
    frames_dropped: number | null; mic_connected: boolean | null;
    aec?: {enabled?: boolean; status?: string; processed?: number;
      bypassed?: number; missing_reference?: number; errors?: number;
      speaker_receipt_age_ms?: number | null; speaker_played_samples?: number | null};
    devices?: AudioDevices;
  };
  vision: { description: string | null; updated_at_ms: number | null; availability: string };
  actions: ActionPreview[];
  events: ExhibitEvent[];
  performance: {
    source: string; process_cpu_pct: number | null; rss_peak_mb: number | null;
    event_loop_lag_ms: number | null; samples: number;
  };
  memory: { recalled: number | null; status: string };
  device_pose: { yaw: number | null; pitch: number | null; roll: number | null; status: string };
};

export const showMetric = (value: number | null | undefined, unit = "", digits = 0) =>
  value == null || !Number.isFinite(value) ? "--" : `${value.toFixed(digits)}${unit}`;

/** UI illumination is evidence-driven: TTS synthesis is not speaker playback. */
export function interactionPhase(snapshot: ExhibitSnapshot | null, stale = false) {
  type Phase = 'offline'|'sleep'|'awake'|'listening'|'processing'|'playing'|'followup'|'error';
  const state = (phase: Phase, label: string, detail: string) => ({phase, label, detail});
  if (stale || !snapshot?.robot.ready || snapshot.realtime.state !== 'connected')
    return state('offline', '未连接', '实时状态不可用');
  const rt = snapshot.realtime;
  if (rt.wakeword?.error || rt.speech_output_state === 'error')
    return state('error', '交互异常', rt.wakeword.error || '语音输出异常');
  const playing = ['started', 'progress', 'resumed'].includes(rt.playback_state ?? '') &&
    ['speaker', 'browser_test'].includes(rt.playback_source ?? '');
  if (playing) return state('playing', rt.playback_source === 'speaker' ? '扬声器播放中' : '网页试听中',
    rt.playback_source === 'speaker' ? '收到物理扬声器播放回执' : '仅收到网页播放回执，不代表机器人扬声器已播');
  const wake = rt.wakeword;
  if (wake?.enabled && wake.state === 'SLEEP') return state('sleep', '休眠待唤醒', '仅本地 KWS 检测唤醒词');
  if (rt.speech_active === true || wake?.candidate_speech === true)
    return state('listening', '正在收听', '收到实时语音开始事件');
  if (wake?.state === 'WAKE_WAIT') return state('awake', '已唤醒 · 待提问', '唤醒词命中，等待有效提问');
  if (wake?.state === 'FOLLOWUP') return state('followup', '等待追问', '播放结束后的追问窗口');
  if (rt.speech_output_state === 'active') return state('processing', '语音生成中', '正在合成或发布，不代表设备已播放');
  if (wake?.state === 'ACTIVE') return state('processing', '对话处理中', '已准入本轮对话，尚无播放回执');
  return state('awake', '在线待命', '未检测到当前讲话或播放');
}

export function latestPreview(actions: ActionPreview[], generation: number): ActionPreview | null {
  return [...actions].reverse().find(a =>
    a.generation === generation && (
      a.status === "published_unknown" || a.status === "send_unknown" ||
      a.status === "device_started" || a.status === "device_finished" ||
      a.status === "queued" || a.status === "awaiting_audio_anchor" ||
      a.status === "awaiting_text_anchor")) ?? null;
}

export function eventStages(events: ExhibitEvent[], nowMs: number): Array<{ label: string; active: boolean }> {
  // Old buffered events are not evidence that this moment's pipeline is active.
  const recent = events.filter(e => e.ts_ms !== null && e.ts_ms <= nowMs + 2000 &&
    nowMs - e.ts_ms <= 30_000);
  const types = new Set(recent.map(e => e.kind));
  return [
    { label: "环境感知", active: types.has("speech.started") || types.has("transcript.done") },
    { label: "语义理解", active: types.has("turn.started") },
    { label: "记忆关联", active: false }, // no recall evidence feed yet
    { label: "行为决策", active: types.has("action.state") || types.has("tool.result") },
    { label: "语音与动作", active: types.has("tts.first_published") || types.has("tts.first_audio") },
    { label: "设备反馈", active: recent.some(e => e.kind === "tts.playback.started" &&
      e.source === "speaker") },
  ];
}
