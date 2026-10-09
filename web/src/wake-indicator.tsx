import {memo, useEffect, useState} from 'react';
import {robotPath} from './api';
import {wakeLabel, wakePresentation} from './wake-state';
import type {WakeState} from './wake-state';

type Readout = {robot_id: string; generation: number; ready: boolean; wakeword: WakeState};

/** Only the selected workbench robot, one tiny in-memory snapshot per ~1 s. */
function WakeIndicatorImpl({robotId, generation, ready, initial}: {
  robotId: string; generation?: number; ready: boolean; initial?: WakeState;
}) {
  const [received, setReceived] = useState<Readout | 'offline' | null>(null);

  useEffect(() => {
    setReceived(null);
    if (!robotId || !initial?.enabled) return;
    let active = true;
    let timer = 0;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const response = await fetch(robotPath(robotId) + '/wakeword', {
          signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const row = await response.json() as Readout;
        if (active && row.robot_id === robotId &&
            (generation === undefined || row.generation === generation)) setReceived(row);
      } catch {
        if (active) setReceived('offline');
      } finally {
        if (active) timer = window.setTimeout(() => {void poll();}, 900);
      }
    };
    void poll();
    return () => {active = false; controller.abort(); window.clearTimeout(timer);};
  }, [robotId, generation, initial?.enabled]);

  const valid = received && received !== 'offline' &&
    (generation === undefined || received.generation === generation) ? received : null;
  const display = received === 'offline' ? {
    label: '状态更新中断', detail: '无法确认最新的唤醒状态', tone: 'error' as const,
    countdown: null,
  } : wakePresentation(valid?.ready ?? ready, valid?.wakeword ?? initial);
  return <span className={`wake-status wake-status-${display.tone}`} role="status"
    aria-label={`唤醒状态：${wakeLabel(display)}`} title={display.detail}>
    唤醒：{wakeLabel(display)}
  </span>;
}

export const WakeIndicator = memo(WakeIndicatorImpl);
