type PodWakeState = {
  enabled: boolean;
  state?: string;
  wakes?: number;
  input_dbfs?: number | null;
  deadline_remaining_ms?: number | null;
  candidate_speech?: boolean;
  error?: string | null;
};

export type AnniePodReadout = {
  robot_id: string;
  generation: number;
  ready: boolean;
  state: string;
  wakeword: PodWakeState;
  speech_active: boolean | null;
  speech_output: {
    state: string;
    playback_state?: string | null;
    playback_source?: string | null;
  };
};

export type AnniePodPhase = 'offline'|'sleep'|'awake'|'listening'|'processing'|'playing'|'followup'|'error';

/** KWS admission, provider speech, and verified playback are separate evidence. */
export function anniePodPhase(snapshot: AnniePodReadout | null, stale = false): {
  phase: AnniePodPhase; label: string; detail: string;
} {
  const state = (phase: AnniePodPhase, label: string, detail: string) => ({phase, label, detail});
  if (stale || !snapshot?.ready || snapshot.state !== 'connected')
    return state('offline', '未连接', 'Realtime 状态不可用');
  const wake = snapshot.wakeword;
  const output = snapshot.speech_output;
  if (wake?.error || output?.state === 'error')
    return state('error', '交互异常', wake?.error || '语音输出异常');
  const played = ['started', 'progress', 'resumed'].includes(output?.playback_state ?? '');
  if (played && output?.playback_source === 'speaker')
    return state('playing', '扬声器播放中', '收到实体扬声器播放回执');
  if (played && output?.playback_source === 'browser_test')
    return state('playing', '网页试听中', '仅网页播放，不代表真机播放');
  if (wake?.enabled && wake.state === 'SLEEP')
    return state('sleep', '休眠待唤醒', '仅在本地检测唤醒词');
  if (snapshot.speech_active || wake?.candidate_speech)
    return state('listening', '正在收听', '收到用户语音开始事件');
  if (wake?.state === 'WAKE_WAIT')
    return state('awake', '已唤醒', '等待有效提问');
  if (wake?.state === 'FOLLOWUP')
    return state('followup', '等待追问', '追问窗口保持开放');
  if (output?.state === 'active')
    return state('processing', '语音生成中', '正在合成，不代表已播放');
  if (wake?.state === 'ACTIVE')
    return state('processing', '对话处理中', '本轮已准入，等待回复');
  return state('awake', '在线待命', '当前没有讲话或播放回执');
}
