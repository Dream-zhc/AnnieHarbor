/** Presentation of the one canonical backend WakeAdmission state machine. */
export type WakeState = {
  enabled: boolean;
  backend?: string;
  state?: 'SLEEP' | 'WAKE_WAIT' | 'ACTIVE' | 'FOLLOWUP' | string;
  pcm_to_qwen?: boolean;
  audio_reply_allowed?: boolean;
  candidate_speech?: boolean;
  wakes?: number;
  input_dbfs?: number | null;
  last_keyword?: string | null;
  deadline_remaining_ms?: number | null;
  error?: string | null;
};

export type WakePresentation = {
  label: string;
  detail: string;
  tone: 'offline' | 'idle' | 'awake' | 'active' | 'error';
  countdown: number | null;
};

export function wakePresentation(ready: boolean, wake?: WakeState | null): WakePresentation {
  if (!ready) return {label: '未连接', detail: 'Realtime 尚未就绪', tone: 'offline', countdown: null};
  if (!wake?.enabled) return {label: '免唤醒', detail: '唤醒词未启用，按当前麦克风策略直接交谈', tone: 'idle', countdown: null};
  if (wake.error) return {label: '唤醒异常', detail: `本地唤醒检测异常：${wake.error}`, tone: 'error', countdown: null};
  const countdown = typeof wake.deadline_remaining_ms === 'number' &&
    Number.isFinite(wake.deadline_remaining_ms) && wake.deadline_remaining_ms >= 0
    ? Math.ceil(wake.deadline_remaining_ms / 1000) : null;
  switch (wake.state) {
    case 'SLEEP':
      return {label: '休眠 · 等待唤醒', detail: '本地检测唤醒词；普通语音未送往 Qwen', tone: 'idle', countdown: null};
    case 'WAKE_WAIT':
      return {label: wake.candidate_speech ? '已唤醒 · 正在听' : '已唤醒 · 待提问',
        detail: '等待有效的用户语音，尚未自动触发回复', tone: 'awake', countdown};
    case 'ACTIVE':
      return {label: wake.candidate_speech ? '待机中 · 正在听' : '待机中',
        detail: '唤醒链路保持就绪；当前处于待机状态，等待下次提问',
        tone: 'active', countdown};
    case 'FOLLOWUP':
      return {label: wake.candidate_speech ? '追问中 · 正在听' : '等待追问',
        detail: '一次回复播完后的追问窗口；超时会返回休眠', tone: 'awake', countdown};
    default:
      return {label: '状态未知', detail: '唤醒模块没有返回可识别的状态', tone: 'offline', countdown: null};
  }
}

export function wakeLabel(display: WakePresentation): string {
  return `${display.label}${display.countdown !== null ? ` · ${display.countdown}s` : ''}`;
}
