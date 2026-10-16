import {lazy, Suspense, useEffect, useMemo, useRef, useState} from 'react';
import {
  Activity,
  BrainCircuit,
  Camera,
  Check,
  Cpu,
  Ear,
  Eye,
  Heart,
  Layers,
  Mic,
  Network,
  Radio,
  Sparkles,
  Volume2,
  Waves,
  Zap,
} from 'lucide-react';
import {eventStages, latestPreview, showMetric} from './telemetry';
import type {ExhibitSnapshot} from './telemetry';
import {eventBelongsToSession, type Session, type RealtimeEvent} from '../api';
import {wakeLabel, wakePresentation} from '../wake-state';
import {conflictMessages} from '../audio-devices';
import './exhibit.css';

const RobotTwin = lazy(() => import('./robot-twin').then(m => ({default: m.RobotTwin})));
const OfficialLiveKitVideo = lazy(() =>
  import('./official-livekit').then(m => ({default: m.OfficialLiveKitVideo}))
);

type Props = {
  currentRobotId: string;
  onSelectRobot: (id: string) => void;
  onSwitchMode: (mode: 'workbench' | 'cockpit') => void;
};

async function jsonApi<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...init,
    headers: {'Content-Type': 'application/json', ...init?.headers},
  });
  if (!response.ok)
    throw new Error(`${response.status}: ${await response.text().catch(() => '请求失败')}`);
  return response.json() as Promise<T>;
}

const disabled = (value: string | null | undefined, fallback = '未收到数据') =>
  value || fallback;

// Status-only indicator: no PCM sample data is available to draw a waveform.
function HiFiAudioWave({
  tone = 'cyan',
  active = false,
  speaking = false,
  bars: _bars = 32,
}: {
  tone?: 'cyan' | 'green' | 'pink';
  active?: boolean;
  speaking?: boolean;
  bars?: number;
}) {
  return (
    <div
      className={`ex-hifi-wave tone-${tone} ${active || speaking ? 'is-active' : ''}`}
      aria-label={active || speaking ? '活动状态' : '未观测音频幅度'}
    >
      <span>{active || speaking ? '信号活动中' : '幅度未观测'}</span>
    </div>
  );
}

function ExhibitView({currentRobotId, onSelectRobot, onSwitchMode}: Props) {
  const [error, setError] = useState('');
  const [snapshot, setSnapshot] = useState<ExhibitSnapshot | null>(null);
  const [robotIds, setRobotIds] = useState<string[]>([]);
  const [view, setView] = useState<'exhibition' | 'research'>('exhibition');
  const [reducedMotion, setReducedMotion] = useState(false);
  const [stale, setStale] = useState(false);

  // Ultra-Low-Latency Realtime Stream Overrides
  const [liveSpeechActive, setLiveSpeechActive] = useState<boolean | null>(null);
  const [liveSpeechOutput, setLiveSpeechOutput] = useState<string | null>(null);
  const [liveUserText, setLiveUserText] = useState<string | null>(null);
  const [liveAssistantText, setLiveAssistantText] = useState<string | null>(null);

  // Track latest seq for SSE
  const sseSeqRef = useRef(0);

  useEffect(() => {
    let alive = true;
    jsonApi<Array<{robot_id: string}>>('/api/exhibit/robots')
      .then(rows => {
        if (alive) setRobotIds(rows.map(row => row.robot_id));
      })
      .catch(() => {
        if (alive) setRobotIds([]);
      });
    return () => {
      alive = false;
    };
  }, []);

  // SSE is a fast overlay on evidence snapshots, never a second state machine.
  useEffect(() => {
    if (typeof window === 'undefined' || !window.EventSource) return;
    let es: EventSource | null = null;
    let disposed = false;
    let activeSession: Session | null = null;

    setLiveSpeechActive(null);
    setLiveSpeechOutput(null);
    setLiveUserText(null);
    setLiveAssistantText(null);
    sseSeqRef.current = 0;

    const connectSSE = async () => {
      try {
        const [session, cursor] = await Promise.all([
          jsonApi<Session>(`/api/robots/${encodeURIComponent(currentRobotId)}/session`),
          jsonApi<{latest:number}>(`/api/robots/${encodeURIComponent(currentRobotId)}/realtime/events?after=0`),
        ]);
        if (disposed) return;
        activeSession = session;
        sseSeqRef.current = cursor.latest;
        es = new EventSource(
          `/api/robots/${encodeURIComponent(currentRobotId)}/realtime/stream?after=${cursor.latest}`
        );
        es.onmessage = e => {
          if (disposed || !e.data) return;
          try {
            const data = JSON.parse(e.data);
            if(data.gap){setLiveSpeechActive(null);setLiveSpeechOutput(null);}
            const events = Array.isArray(data.events) ? data.events : [];
            for (const ev of events as RealtimeEvent[]) {
              if(ev.seq<=sseSeqRef.current)continue;
              sseSeqRef.current=ev.seq;
              if(!activeSession||!eventBelongsToSession(ev,activeSession)){
                if(ev.kind==='session.ready'&&ev.robot_id===currentRobotId){
                  void jsonApi<Session>(`/api/robots/${encodeURIComponent(currentRobotId)}/session`)
                    .then(next=>{if(!disposed){activeSession=next;setLiveSpeechActive(null);
                      setLiveSpeechOutput(null);setLiveUserText(null);setLiveAssistantText(null);}})
                    .catch(()=>{});
                }
                continue;
              }
              if (ev.kind === 'speech.started') {
                setLiveSpeechActive(true);
              } else if (ev.kind === 'speech.stopped') {
                setLiveSpeechActive(false);
              } else if (ev.kind === 'transcript.done') {
                if (ev.text) setLiveUserText(ev.text);
              } else if (ev.kind === 'transcript.delta') {
                if (ev.text) setLiveUserText(ev.text);
              } else if (ev.kind === 'tts.playback.started' || ev.kind === 'tts.playback.resumed') {
                if(ev.source === 'speaker')setLiveSpeechOutput('speaker');
                else if(ev.source === 'browser_test')setLiveSpeechOutput(previous=>
                  previous==='speaker'?previous:'browser_test');
              } else if (['tts.playback.drained','tts.playback.stopped','tts.playback.held'].includes(ev.kind)) {
                // A browser-test receipt cannot stop or replace a physical speaker.
                setLiveSpeechOutput(previous=>previous===ev.source?'idle':previous);
              } else if (ev.kind === 'text.delta' || ev.kind === 'text.done' || ev.kind === 'turn.done') {
                if (ev.text) setLiveAssistantText(ev.text);
              }
            }
          } catch {}
        };
        es.onerror = () => {
          setLiveSpeechActive(null);
          setLiveSpeechOutput(null);
        };
      } catch {}
    };

    void connectSSE();

    return () => {
      disposed = true;
      if (es) {
        es.close();
        es = null;
      }
    };
  }, [currentRobotId]);

  // Low-frequency snapshot fallback for SSE; avoid repeated full health reads.
  useEffect(() => {
    let alive = true;
    let inflight = false;

    const poll = async () => {
      if (inflight || document.hidden) return;
      inflight = true;
      try {
        const data = await jsonApi<ExhibitSnapshot>(
          `/api/exhibit/robots/${encodeURIComponent(currentRobotId)}/snapshot`
        );
        if (!alive) return;
        if (data.robot.robot_id !== currentRobotId) return;
        setSnapshot(data);
        setStale(false);
        setError('');
      } catch (reason) {
        if (!alive) return;
        setStale(true);
        const msg = String(reason);
        setError(msg.includes('<') ? '实时遥测同步中…' : msg);
        setSnapshot(null);
      } finally {
        inflight = false;
      }
    };

    setSnapshot(null);
    void poll();
    const interval = window.setInterval(() => {
      void poll();
    }, 1000);

    return () => {
      alive = false;
      window.clearInterval(interval);
    };
  }, [currentRobotId]);

  const preview = useMemo(
    () => latestPreview(snapshot?.actions ?? [], snapshot?.robot.generation ?? -1),
    [snapshot?.actions, snapshot?.robot.generation]
  );
  const stages = useMemo(
    () => eventStages(snapshot?.events ?? [], snapshot?.generated_at_ms ?? 0),
    [snapshot?.events, snapshot?.generated_at_ms]
  );
  const lastEvents = snapshot?.events.slice(-6).reverse() ?? [];
  const wake = wakePresentation(!!snapshot?.robot.ready, snapshot?.realtime.wakeword);

  const online = !stale&&snapshot?.robot.ready===true&&snapshot.realtime.state==='connected';
  const playbackSource = liveSpeechOutput==='speaker'||liveSpeechOutput==='browser_test'
    ? liveSpeechOutput : snapshot?.realtime.playback_source;
  const isSpeaking = liveSpeechOutput != null
    ? liveSpeechOutput==='speaker'||liveSpeechOutput==='browser_test'
    : ['started','progress','resumed'].includes(snapshot?.realtime.playback_state??'')&&
      ['speaker','browser_test'].includes(playbackSource??'');

  const isUserSpeaking =
    liveSpeechActive != null
      ? liveSpeechActive
      : !!snapshot?.realtime.speech_active;

  const userSpeechText =
    liveUserText ||
    snapshot?.realtime.user_text ||
    '暂无用户对话';

  const deviceWarnings = conflictMessages(snapshot?.audio.devices ?? null, snapshot?.robot.room_id);
  const assistantSpeechText =
    liveAssistantText ||
    snapshot?.realtime.assistant_text ||
    '暂无机器人回复';

  return (
    <main className="ex-shell" data-view={view}>
      <div className="ex-background" aria-hidden="true" />

      {/* ====================================================================
          1. Top Navigation Bar (Header)
          ==================================================================== */}
      {deviceWarnings.length > 0 && (
        <div className="ex-device-alert" role="alert">
          <strong>音频设备冲突 · ONE MIC &amp; ONE SPEAKER PER ROOM</strong>
          {deviceWarnings.map((text) => <span key={text}>{text}</span>)}
        </div>
      )}

      <header className="ex-topbar">
        {/* Brand */}
        <div className="ex-brand">
          <span className="ex-logo-mark">A</span>
          <div className="ex-brand-titles">
            <div className="ex-brand-row">
              <b>小安妮</b>
              <span className="ex-brand-en">ANNIE</span>
              <span className="ex-badge-v2">V2</span>
            </div>
            <small>
              让智能更有温度 · AI 陪伴 <span>A MORE COMPANIONABLE AI</span>
            </small>
          </div>
        </div>

        {/* Center Main Heading */}
        <div className="ex-heading">
          <strong>小安妮 V2 · 具身智能与情感陪伴大屏</strong>
          <small>ANNIE V2 · EMBODIED INTELLIGENCE & COMPANION DISPLAY</small>
        </div>

        {/* Right Status Capsules & Controls */}
        <div className="ex-toolbar">
          <div className="ex-status-capsule-group">
            <span className="ex-cap-item">
              <Layers size={11} /> 展厅·体验 <em>EXHIBITION</em>
            </span>
            <span className="ex-cap-item">
              <Eye size={11} /> 视觉 <em>VISION</em>
            </span>
            <span className="ex-cap-item">
              <Ear size={11} /> 聆听 <em>AUDIO</em>
            </span>
            <span className={online?'ex-cap-item online':'ex-cap-item'}>
              {online&&<i className="ex-green-dot pulse" />} {online?'AI 在线':'AI 未连接'} <em>{online?'AI ONLINE':'OFFLINE'}</em>
            </span>
            <span className="ex-cap-item latency">
              <Activity size={11} /> 主循环延迟 <b>{showMetric(snapshot?.performance.event_loop_lag_ms,' ms',1)}</b>
            </span>
          </div>

          {/* Robot Selector & Actions */}
          <div className="ex-ctrl-pills">
            {robotIds.map(id => (
              <button
                type="button"
                className={id === currentRobotId ? 'selected' : ''}
                key={id}
                onClick={() => onSelectRobot(id)}
              >
                {id}
              </button>
            ))}
            <button
              type="button"
              className="ex-workbench-btn"
              onClick={() => onSwitchMode('workbench')}
            >
              工作台
            </button>
          </div>
        </div>
      </header>

      {/* ====================================================================
          2. Sub-top Ambient HUD Status Banner
          ==================================================================== */}
      <div className="ex-statusbar">
        <span className="ex-room-pill">ROOM {snapshot?.robot.room_id ?? 'roomv2'}</span>
        <span>
          <Radio size={11} /> REALTIME {snapshot?.realtime.state ?? 'connected'}
        </span>
        <span
          className={`ex-wake-indicator ex-wake-${wake.tone}`}
          title={wake.detail}
          role="status"
        >
          <Zap size={11} /> 唤醒：{wakeLabel(wake)}
        </span>
        <span>
          <Mic size={11} /> AUDIO{' '}
          {isUserSpeaking
            ? '检测到讲话'
            : snapshot?.realtime.wakeword?.enabled &&
              snapshot?.realtime.wakeword.state === 'SLEEP'
            ? '休眠门控'
            : '环境监听'}
        </span>
        <span className="ex-intranet-tag">内网直连 · 无需登录</span>
        <span className="ex-time-tag">
          更新{' '}
          {snapshot?.generated_at_ms
            ? new Date(snapshot.generated_at_ms).toLocaleTimeString('zh-CN', {
                hour12: false,
              })
            : '--:--:--'}
        </span>
      </div>

      {/* ====================================================================
          3. Main Dashboard 3-Column Cockpit
          ==================================================================== */}
      <section className="ex-dashboard">
        {/* ================= LEFT COLUMN ================= */}
        <aside className="ex-column ex-left">
          {/* Visual Perception Panel */}
          <section className="ex-panel ex-vision">
            <div className="ex-panel-heading">
              <Camera size={14} />
              <span>
                视觉感知 <small>VISUAL PERCEPTION</small>
              </span>
              <span className="ex-live-pill">{snapshot?.vision.description?'已观测':'未观测'}</span>
            </div>

            {/* Video Viewport with High-Tech HUD Reticles */}
            <div className="ex-camera">
              {/* Dynamic Laser Scanning Line */}
              <div className="ex-vision-scanline" aria-hidden="true" />

              <Suspense
                fallback={<div className="ex-unavailable">加载 LiveKit 视频接收器…</div>}
              >
                <OfficialLiveKitVideo key={currentRobotId} robotId={currentRobotId} />
              </Suspense>

              {/* Coordinates/confidence are unknown; do not draw fabricated detections. */}
              <div className="ex-vision-hud-overlay" aria-hidden="true">
              </div>
            </div>

            {/* Scene Understanding Bottom Row */}
            <div className="ex-perception-copy">
              <div className="ex-copy-title">
                <Sparkles size={12} />
                <span>场景多模态语义理解 · SCENE UNDERSTANDING</span>
              </div>
              <p>
                {disabled(
                  snapshot?.vision.description,
                  '视觉信息未观测'
                )}
              </p>
            </div>
          </section>

          {/* Auditory Perception Panel */}
          <section className="ex-panel ex-audio">
            <div className="ex-panel-heading">
              <Waves size={14} />
              <span>
                听觉感知 <small>AUDITORY PERCEPTION</small>
              </span>
              <span className="ex-live-pill">{snapshot?.audio.mic_connected?'麦克风已连接':'音频状态未确认'}</span>
            </div>

            {/* Three Real Hi-Fi Continuous Waveform Streams */}
            <div className="ex-audio-streams">
              {/* Stream 1: Ambient */}
              <div className="audio-stream-row">
                <div className="stream-lbl">
                  <Volume2 size={13} className="stream-icon cyan" />
                  <div>
                    <b>环境声音</b>
                    <small>AMBIENT</small>
                  </div>
                </div>
                <HiFiAudioWave tone="cyan" bars={30} />
                <div className="stream-stat">
                  <span>幅度未知</span>
                  <small>无 dB 观测</small>
                </div>
              </div>

              {/* Stream 2: Human Speech */}
              <div className="audio-stream-row">
                <div className="stream-lbl">
                  <Mic size={13} className="stream-icon green" />
                  <div>
                    <b>人类语音</b>
                    <small>HUMAN SPEECH</small>
                  </div>
                </div>
                <HiFiAudioWave tone="green" active={isUserSpeaking} bars={30} />
                <div className="stream-stat">
                  <span>{isUserSpeaking ? '正在说话' : '未检测到当前说话'}</span>
                  <small>{isUserSpeaking ? '输入中…' : '待命'}</small>
                </div>
              </div>

              {/* Stream 3: Annie Speech */}
              <div className="audio-stream-row">
                <div className="stream-lbl">
                  <Heart size={13} className="stream-icon pink" />
                  <div>
                    <b>小安妮语音</b>
                    <small>ANNIE SPEECH</small>
                  </div>
                </div>
                <HiFiAudioWave tone="pink" speaking={isSpeaking} bars={30} />
                <div className="stream-stat">
                  <span className="pink">{isSpeaking?(playbackSource==='speaker'?'设备播放':'网页试听'):'未报告播放'}</span>
                  <small>{isSpeaking?'收到播放回执':'待命'}</small>
                </div>
              </div>
            </div>

            {/* Bottom 4 Interaction Control Buttons */}
            <div className="ex-interaction-pills">
              <button
                type="button"
                className={`int-btn listening ${isUserSpeaking ? 'active' : ''}`}
              >
                <Ear size={12} /> 倾听 <em>LISTENING</em>
              </button>
              <button type="button" className="int-btn understanding">
                <BrainCircuit size={12} /> 理解 <em>UNDERSTANDING</em>
              </button>
              <button
                type="button"
                className={`int-btn responding ${isSpeaking ? 'active pulse' : ''}`}
              >
                <Heart size={12} /> 回应 <em>RESPONDING</em>
              </button>
              <button type="button" className="int-btn interrupt">
                <Radio size={12} /> 打断 <em>INTERRUPT</em>
              </button>
            </div>
          </section>
        </aside>

        {/* ================= CENTER HERO: DIGITAL TWIN ================= */}
        <section className="ex-hero">
          {/* Header */}
          <div className="ex-hero-heading">
            <span>EMBODIED DIGITAL TWIN / ANNIE V2</span>
            <strong>具身智能全息核心</strong>
            <small>多模态感知驱动的 3D 数字孪生体</small>
          </div>

          <div className="ex-hero-orbit" aria-hidden="true" />

          {/* 3D WebGL Canvas & Cybernetic Avatar */}
          <Suspense fallback={<div className="ex-unavailable">加载全息具身核心…</div>}>
            <RobotTwin
              key={currentRobotId}
              preview={preview}
              generation={snapshot?.robot.generation ?? -1}
              speaking={isSpeaking}
              reducedMotion={reducedMotion}
              phase={wake.tone}
              pose={snapshot?.device_pose}
              viseme={snapshot?.realtime.viseme}
              expression={snapshot?.actions.filter(a => a.kind === 'expression').at(-1)?.value}
            />
          </Suspense>

          {/* Bottom Telemetry Bar */}
          <div className="ex-hero-base">
            <span>
              <i className="ex-pulse-dot" />{' '}
              {snapshot?.robot.state === 'running' ? '具身核心运行中' : 'NORMAL'}
            </span>
            <span>THREE.JS ENGINE · HOLOGRAPHIC TWIN CORE</span>
          </div>
        </section>

        {/* ================= RIGHT COLUMN ================= */}
        <aside className="ex-column ex-right">
          {/* Cognitive State Panel */}
          <section className="ex-panel ex-cognition">
            <div className="ex-panel-heading">
              <BrainCircuit size={14} />
              <span>
                当前认知态 <small>COGNITIVE STATE</small>
              </span>
              <div className="cog-tabs">
                <span className="active">全局</span>
                <span>认知轨迹</span>
                <span>记忆</span>
                <span>意图与规划</span>
              </div>
            </div>

            {/* Current Focus Card */}
            <div className="ex-focus-card">
              <div className="focus-reticle">
                <div className="reticle-circle pulse">
                  <Eye size={15} />
                </div>
                <small>CURRENT FOCUS</small>
                <b>{snapshot?.vision.description?'收到场景描述':'未观测关注目标'}</b>
              </div>
              <div className="focus-desc">
                <p>
                  {snapshot?.vision.description || '暂无有效视觉观察；无法推断访客身份、姿态或意图。'}
                </p>
                <div className="focus-next">
                  <span>动作指令：</span>
                  <em>{preview ? `${preview.value}（${preview.status}，不代表设备执行）` : '暂无已报告的动作指令'}</em>
                </div>
              </div>
            </div>

            {/* 4-Step Cognitive Pipeline Graph */}
            <div className="cog-pipeline-chain">
              <div className="cog-step">
                <Camera size={12} />
                <span>视觉与声音事件</span>
              </div>
              <i>›</i>
              <div className="cog-step">
                <BrainCircuit size={12} />
                <span>语义理解</span>
              </div>
              <i>›</i>
              <div className="cog-step">
                <Layers size={12} />
                <span>行为选择</span>
              </div>
              <i>›</i>
              <div className="cog-step">
                <Activity size={12} />
                <span>身体动作与语音</span>
              </div>
            </div>
          </section>

          {/* Emotional Expression Panel */}
          <section className="ex-panel ex-emotion">
            <div className="ex-panel-heading">
              <Heart size={14} />
              <span>
                情感表达 <small>EMOTIONAL EXPRESSION</small>
              </span>
              <span className="ex-empathy-badge">EMPATHY ENGINE</span>
            </div>

            <div className="ex-emotion-content">
              {/* Emotional Compass Dial */}
              <div className="emo-dial-wrap">
                <div className="emo-dial">
                  <Heart size={18} className="dial-heart" />
                  <span className="dial-state">当前情绪</span>
                  <b className="dial-val">未观测</b>
                  <small className="dial-en">UNKNOWN</small>
                </div>
              </div>

              {/* Empathy Explanation */}
              <div className="emo-detail">
                <div className="emo-quote">
                  当前快照不包含可信情绪标签。
                </div>
              </div>
            </div>

            <div className="emo-state-pills">
              情绪推断待接入可验证数据
            </div>
          </section>

          {/* Companion Memory Panel */}
          <section className="ex-panel ex-memory">
            <div className="ex-panel-heading">
              <Network size={14} />
              <span>
                陪伴记忆 <small>COMPANION MEMORY</small>
              </span>
              <span className="mem-growth-tag">记忆状态：{snapshot?.memory.status??'未观测'}</span>
            </div>

            <div className="ex-memory-cards">
              {/* Left Card: Related Memory */}
              <div className="mem-subcard rel-mem">
                <div className="subcard-title">相关记忆</div>
                <div className="subcard-body">
                  <div className="subcard-text">
                    <p>暂无可验证的记忆内容</p>
                    <small>大屏未接入长期记忆明细</small>
                  </div>
                </div>
              </div>

              {/* Middle Card: Current Topic */}
              <div className="mem-subcard cur-topic">
                <div className="subcard-title">当期意图</div>
                <div className="subcard-body">
                  <p>当前意图：未观测</p>
                </div>
              </div>

              {/* Right Card: Long-term Graph */}
              <div className="mem-subcard graph-subcard">
                <div className="subcard-title">长期关系</div>
                <div className="graph-constellation-mini">尚无关系图谱证据</div>
              </div>
            </div>
          </section>
        </aside>
      </section>

      {/* ====================================================================
          4. Bottom Closed-Loop Pipeline & Live Interaction Deck
          ==================================================================== */}
      <footer className="ex-deck">
        <div className="ex-deck-title">
          <Layers size={13} />
          <b>感知 — 决策 — 执行闭环流水线</b>
          <span>PERCEPTION TO ACTION PIPELINE</span>
          <div className="ex-deck-controls">
            <button
              type="button"
              onClick={() => setReducedMotion(!reducedMotion)}
            >
              {reducedMotion ? '恢复动效' : '减少动效'}
            </button>
          </div>
        </div>

        {/* 6 Stages High-Tech Pipeline (Exact match with Design) */}
        <div className="ex-pipeline">
          {stages.map((step, index) => {
            const icons = [
              <Camera size={12} key="0" />,
              <BrainCircuit size={12} key="1" />,
              <Network size={12} key="2" />,
              <Activity size={12} key="3" />,
              <Volume2 size={12} key="4" />,
              <Check size={12} key="5" />,
            ];
            const enLabels = [
              'SENSE',
              'UNDERSTAND',
              'MEMORY',
              'PLAN',
              'GENERATE',
              'EXECUTE',
            ];
            return (
              <div key={step.label} className={step.active ? 'on' : ''}>
                <span className="pipe-num">{String(index + 1).padStart(2, '0')}</span>
                <div className="pipe-info">
                  <div className="pipe-row">
                    {icons[index]}
                    <b>{step.label}</b>
                  </div>
                  <small>{enLabels[index]}</small>
                </div>
              </div>
            );
          })}
        </div>

        {/* Bottom Dialogue & Behavior Summary */}
        <div className="ex-bottom-row">
          {/* Live Interaction Camera Thumbnail */}
          <div className="ex-interaction-thumb">
            <img
              src="/exhibit-assets/live-interaction.png"
              alt="展厅交互示意图"
              className="thumb-img"
            />
            <div className="thumb-lbl">
              交互示意 <em>ILLUSTRATION</em>
            </div>
          </div>

          {/* User Bubble */}
          <div className="ex-dialog-card user-card">
            <img
              src="/exhibit-assets/user-avatar.png"
              alt="访客"
              className="chat-avatar"
            />
            <div className="chat-text">
              <small>访客 USER</small>
              <p>{userSpeechText}</p>
            </div>
          </div>

          {/* Wave Transfer Arrow */}
          <div className="ex-chat-wave-arrow" aria-hidden="true">
            <HiFiAudioWave tone="cyan" active={isUserSpeaking || isSpeaking} bars={14} />
            <span className="arrow-sym">→</span>
          </div>

          {/* Annie Bubble */}
          <div className="ex-dialog-card annie-card">
            <img
              src="/exhibit-assets/annie-avatar.png"
              alt="小安妮"
              className="chat-avatar"
            />
            <div className="chat-text">
              <small>小安妮 ANNIE</small>
              <p>{assistantSpeechText}</p>
            </div>
          </div>

          {/* Behavior Feedback Badges */}
          <div className="ex-behavior-badges">
            <div className="behavior-title">
              行为表现 <span>BEHAVIOR</span>
            </div>
            <div className="behavior-pills">
              <span className="beh-pill">{preview?`动作指令：${preview.value}`:'尚无动作指令'}</span>
              <span className="beh-pill">设备动作完成：未观测</span>
            </div>
          </div>
        </div>

        {/* Research Diagnostic View */}
        {view === 'research' && (
          <div className="ex-research">
            <span>
              <Cpu size={11} /> 进程 CPU {showMetric(snapshot?.performance.process_cpu_pct, '%', 1)}
            </span>
            <span>
              <Activity size={11} /> 事件循环滞后 {showMetric(snapshot?.performance.event_loop_lag_ms, 'ms', 1)}
            </span>
            <span>RSS 内存峰值 {showMetric(snapshot?.performance.rss_peak_mb, 'MB', 1)}</span>
            <span>最近流水线事件：{lastEvents.map(e => e.kind).join(' · ') || '无事件'}</span>
          </div>
        )}

        {error && (
          <div role="alert" className="ex-error ex-footer-error">
            {error}
          </div>
        )}
      </footer>
    </main>
  );
}

export {ExhibitView};
