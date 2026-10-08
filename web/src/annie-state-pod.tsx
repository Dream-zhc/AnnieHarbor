import {memo, useEffect, useState} from 'react';
import {robotPath} from './api';
import {anniePodPhase} from './annie-pod-state';
import type {AnniePodReadout} from './annie-pod-state';
import './annie-state-pod.css';

const phases = [
  {key: 'sleep', label: '休眠'},
  {key: 'awake', label: '唤醒'},
  {key: 'listening', label: '收听'},
  {key: 'processing', label: '处理'},
  {key: 'playing', label: '播放'},
  {key: 'followup', label: '追问'},
];

const displayNumber = (value: number | null | undefined, unit = '') =>
  typeof value === 'number' && Number.isFinite(value) ? `${value}${unit}` : '—';

/** A read-only, 1 Hz status view. Does not interact with the mic, player, or model. */
function AnnieStatePodImpl({robotId, generation, roomId}: {
  robotId: string;
  generation?: number;
  roomId?: string;
}) {
  const [snapshot, setSnapshot] = useState<AnniePodReadout | null>(null);
  const [stale, setStale] = useState(true);

  useEffect(() => {
    setSnapshot(null);
    setStale(true);
    if (!robotId) return;
    let alive = true;
    let busy = false;
    let controller: AbortController | null = null;

    const poll = async () => {
      if (busy || document.hidden) return;
      busy = true;
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
      } finally {
        busy = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => {void poll();}, 950);
    document.addEventListener('visibilitychange', poll);
    return () => {
      alive = false;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', poll);
      controller?.abort();
    };
  }, [robotId, generation]);

  const interaction = anniePodPhase(snapshot, stale);
  const wake = snapshot?.wakeword;
  const listeningWhilePlaying = interaction.phase === 'playing' && (
    snapshot?.speech_active === true || wake?.candidate_speech === true
  );

  return <aside className="annie-pod" data-phase={interaction.phase}
    aria-label="小安妮实时状态动效舱">
    <header className="annie-pod-heading">
      <span>ANNIE / LIVE STATE</span>
      <span className="annie-pod-link"><i aria-hidden="true"/>{stale ? '未知' : '已连接'}</span>
    </header>
    <div className="annie-pod-core" aria-hidden="true">
      <div className="annie-pod-scan"/>
      <div className="annie-pod-ring outer"/>
      <div className="annie-pod-ring middle"/>
      <div className="annie-pod-ring inner"/>
      <div className="annie-pod-aura"><span>A</span></div>
      <span className="annie-pod-core-caption">ANNIE V2</span>
    </div>
    <div className="annie-pod-reading" role="status" aria-live="polite">
      <span className="annie-pod-reading-label"><i aria-hidden="true"/> 实时交互</span>
      <strong>{interaction.label}</strong>
      <p>{interaction.detail}</p>
      {listeningWhilePlaying && <small>全双工 · 同时检测到用户讲话</small>}
    </div>
    <div className="annie-pod-phases" aria-label="交互状态">
      {phases.map(({key, label}) => <span key={key}
        className={interaction.phase === key ? 'active' : ''}
        aria-current={interaction.phase === key ? 'step' : undefined}>{label}</span>)}
    </div>
    <dl className="annie-pod-facts">
      <div><dt>机器人</dt><dd>{robotId || '—'}</dd></div>
      <div><dt>房间</dt><dd>{roomId || '—'}</dd></div>
      <div><dt>唤醒命中</dt><dd>{displayNumber(wake?.wakes, ' 次')}</dd></div>
      <div><dt>检测电平</dt><dd>{displayNumber(wake?.input_dbfs, ' dBFS')}</dd></div>
      <div><dt>窗口剩余</dt><dd>{wake?.deadline_remaining_ms == null
        ? '—' : `${Math.ceil(wake.deadline_remaining_ms / 1000)} 秒`}</dd></div>
    </dl>
    <footer className="annie-pod-footer">仅反映后端状态与播放回执 · 光效不代表音量</footer>
  </aside>;
}

export const AnnieStatePod = memo(AnnieStatePodImpl);
