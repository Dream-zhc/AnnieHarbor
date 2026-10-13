import {memo, useEffect, useRef, useState} from 'react';
import {robotPath} from './api';
import {anniePodPhase} from './annie-pod-state';
import {createPodRefresh} from './annie-pod-refresh';
import type {AnniePodPhase, AnniePodReadout} from './annie-pod-state';
import {AnnieParticleOrb} from './annie-particle-orb';
import './annie-state-pod.css';

const phases: Array<{
  key: AnniePodPhase;
  label: string;
  code: string;
  color: string;
  detail: string;
}> = [
  {key: 'sleep', label: '休眠', code: 'SLEEP', color: '#6484ac', detail: '低频暗夜待机'},
  {key: 'awake', label: '唤醒', code: 'AWAKE', color: '#00f0ff', detail: '赛博极光就绪'},
  {key: 'listening', label: '收听', code: 'LISTEN', color: '#00f5ff', detail: '天青倾听中'},
  {key: 'processing', label: '处理', code: 'THINK', color: '#c084fc', detail: '鸢尾深度联想'},
  {key: 'playing', label: '播放', code: 'SPEAK', color: '#ff2a85', detail: '全息珊瑚绽放'},
  {key: 'followup', label: '等追问', code: 'FLOW', color: '#00ffc4', detail: '等待用户继续提问，不代表主动续聊'},
];

const displayNumber = (value: number | null | undefined, unit = '') =>
  typeof value === 'number' && Number.isFinite(value) ? `${value}${unit}` : '—';

/** Scoped SSE transitions refresh immediately; polling only reconciles snapshots. */
function AnnieStatePodImpl({robotId, generation, roomId, micActive = false, statusRevision = 0}: {
  robotId: string;
  generation?: number;
  roomId?: string;
  micActive?: boolean;
  statusRevision?: number;
}) {
  const [snapshot, setSnapshot] = useState<AnniePodReadout | null>(null);
  const [stale, setStale] = useState(true);
  const [previewPhase, setPreviewPhase] = useState<AnniePodPhase | null>(null);
  const previewTimer = useRef<number | null>(null);
  const refreshNow = useRef<(() => Promise<void>) | null>(null);
  const lastStatusRevision = useRef(statusRevision);

  const triggerPreview = (phaseKey: AnniePodPhase) => {
    if (previewTimer.current) window.clearTimeout(previewTimer.current);
    setPreviewPhase(phaseKey);
    previewTimer.current = window.setTimeout(() => {
      setPreviewPhase(null);
    }, 4500);
  };

  useEffect(() => {
    return () => {
      if (previewTimer.current) window.clearTimeout(previewTimer.current);
    };
  }, []);

  useEffect(() => {
    setSnapshot(null);
    setStale(true);
    if (!robotId) return;
    let alive = true;
    let controller: AbortController | null = null;

    const poll = async () => {
      controller = new AbortController();
      try {
        const response = await fetch(robotPath(robotId) + '/realtime', {
          signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const row = await response.json() as AnniePodReadout;
        if (!alive) return;
        // A robot change or restart cannot leak stale state into this panel.
        if (row.robot_id !== robotId || (generation !== undefined && row.generation !== generation)) {
          setSnapshot(null);
          setStale(true);
          return;
        }
        setSnapshot(row);
        setStale(false);
      } catch {
        if (alive) {setSnapshot(null); setStale(true);}
      }
    };
    const refresh = createPodRefresh(poll, () => !document.hidden);
    refreshNow.current = refresh.request;
    void refresh.request();
    const timer = window.setInterval(() => {void refresh.request();}, 950);
    const onVisible = () => {void refresh.request();};
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      alive = false;
      refresh.stop();
      if (refreshNow.current === refresh.request) refreshNow.current = null;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
      controller?.abort();
    };
  }, [robotId, generation]);

  useEffect(() => {
    if (lastStatusRevision.current === statusRevision) return;
    lastStatusRevision.current = statusRevision;
    void refreshNow.current?.();
  }, [statusRevision]);

  const interaction = anniePodPhase(snapshot, stale);
  const wake = snapshot?.wakeword;
  const listeningWhilePlaying = interaction.phase === 'playing' && (
    snapshot?.speech_active === true || wake?.candidate_speech === true
  );
  const currentPhase = previewPhase ?? interaction.phase;

  const latencyDisplay = (() => {
    if (typeof snapshot?.first_text_ms === 'number' && snapshot.first_text_ms > 0 && snapshot.first_text_ms < 60000) {
      return `${Math.round(snapshot.first_text_ms)} ms`;
    }
    const asr = snapshot?.last_asr_first_delta_ms;
    const speech = (snapshot as Record<string, unknown>)?.last_provider_speech_ms as number | undefined;
    if (typeof asr === 'number' && typeof speech === 'number' && asr >= speech && (asr - speech) < 60000) {
      return `${Math.round(asr - speech)} ms`;
    }
    return '—';
  })();

  return <aside className="annie-pod" data-phase={currentPhase}
    aria-label="小安妮实时状态动效舱">
    <header className="annie-pod-heading">
      <span>ANNIE / LIVE STATE</span>
      <span className="annie-pod-link"><i aria-hidden="true"/>{stale ? '未知' : '已连接'}</span>
    </header>
    <div className="annie-pod-core" aria-hidden="true">
      {micActive ?
        <div className="annie-particle-orb-fallback annie-pod-low-power">
          <div className="annie-pod-ring outer"/>
          <div className="annie-pod-ring middle"/>
          <div className="annie-pod-ring inner"/>
          <div className="annie-pod-aura"><span>A</span></div>
        </div>
        : <AnnieParticleOrb phase={currentPhase} listeningWhilePlaying={listeningWhilePlaying}/>}
      <span className="annie-pod-core-caption">ANNIE V2</span>
    </div>
    <div className="annie-pod-reading" role="status" aria-live="polite">
      <span className="annie-pod-reading-label">
        <i aria-hidden="true"/> {previewPhase ? '动效预览中 (4秒后自动复位)' : '实时交互'}
      </span>
      <strong>{previewPhase ? `${phases.find(p=>p.key===previewPhase)?.label} 预览` : interaction.label}</strong>
      <p>{previewPhase ? phases.find(p=>p.key===previewPhase)?.detail : interaction.detail}</p>
      {listeningWhilePlaying && <small>全双工 · 同时检测到用户讲话</small>}
    </div>
    <div className="annie-pod-phases" role="group" aria-label="交互状态频谱标尺">
      {phases.map(({key, label, code, color, detail}) => {
        const isActive = currentPhase === key;
        return (
          <button
            key={key}
            type="button"
            className={`annie-phase-item ${isActive ? 'active' : ''}`}
            onClick={() => triggerPreview(key)}
            title={`点击预览: ${label} (${code}) · ${detail}`}
            aria-current={isActive ? 'step' : undefined}
            style={{'--phase-color': color} as React.CSSProperties}
          >
            <span className="annie-phase-pip" style={{backgroundColor: color}} />
            <span className="annie-phase-label">{label}</span>
            <span className="annie-phase-code">{code}</span>
          </button>
        );
      })}
    </div>
    <div className="annie-pod-telemetry-header">
      <span>TELEMETRY METRICS</span>
      <span className="annie-telemetry-badge">{stale ? 'DISCONNECTED' : 'NOMINAL'}</span>
    </div>
    <dl className="annie-pod-facts">
      <div><dt>机器人</dt><dd>{robotId || '—'}</dd></div>
      <div><dt>房间</dt><dd>{roomId || '—'}</dd></div>
      <div><dt>对话轮次</dt><dd>{displayNumber(snapshot?.transcripts_completed, ' 轮')}</dd></div>
      <div><dt>上下文记忆</dt><dd>{displayNumber(snapshot?.provider_history_turns, ' 轮')}</dd></div>
      <div><dt>唤醒命中</dt><dd>{displayNumber(wake?.wakes, ' 次')}</dd></div>
      <div><dt>检测电平</dt><dd>{displayNumber(wake?.input_dbfs, ' dBFS')}</dd></div>
      <div><dt>首字延迟</dt><dd>{latencyDisplay}</dd></div>
      <div><dt>窗口剩余</dt><dd>{wake?.deadline_remaining_ms == null
        ? '—' : `${Math.ceil(wake.deadline_remaining_ms / 1000)} 秒`}</dd></div>
      <div><dt>音频流吞吐</dt><dd>{snapshot?.session_frames_sent ? `${(snapshot.session_frames_sent * 0.02).toFixed(1)}s` : '—'}</dd></div>
      <div><dt>会话链路</dt><dd title={snapshot?.provider_session_id || ''}>{snapshot?.provider_session_id ? snapshot.provider_session_id.slice(-8) : (stale ? '未连接' : '待建立')}</dd></div>
    </dl>
    <footer className="annie-pod-footer">仅反映后端状态与播放回执 · 光效不代表音量</footer>
  </aside>;
}

export const AnnieStatePod = memo(AnnieStatePodImpl);
