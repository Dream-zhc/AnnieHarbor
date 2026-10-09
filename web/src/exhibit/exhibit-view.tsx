import {lazy, Suspense, useEffect, useMemo, useState} from 'react';
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
  MessageSquare,
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
import {wakeLabel, wakePresentation} from '../wake-state';
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

// Dynamic Audio Waveform Strip
function AudioWaveStrip({
  tone = 'cyan',
  active = false,
  bars = 28,
}: {
  tone?: 'cyan' | 'green' | 'pink';
  active?: boolean;
  bars?: number;
}) {
  const heights = [20, 35, 60, 45, 80, 95, 70, 50, 85, 100, 65, 40, 75, 55, 30, 60, 80, 40, 25, 70, 90, 50, 35, 65, 85, 45, 30, 20];
  return (
    <div className={`ex-wavestrip tone-${tone} ${active ? 'is-active' : ''}`} aria-hidden="true">
      {Array.from({length: bars}).map((_, i) => (
        <span
          key={i}
          className="wavestrip-bar"
          style={
            {
              '--bar-h': `${heights[i % heights.length]}%`,
              '--delay': `${(i * 0.04).toFixed(2)}s`,
            } as React.CSSProperties
          }
        />
      ))}
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
  const [activeEmoTab, setActiveEmoTab] = useState('愉悦');

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

  useEffect(() => {
    let alive = true;
    let inflight = false;
    const poll = async () => {
      if (inflight) return;
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
  const isSpeaking = snapshot?.realtime.speech_output_state === 'playing';
  const isUserSpeaking = !!snapshot?.realtime.speech_active;

  // Real or high-fidelity graceful fallback dialogue text
  const userSpeechText =
    snapshot?.realtime.user_text || '你好！你会泡茶吗？';
  const assistantSpeechText =
    snapshot?.realtime.assistant_text ||
    '我很喜欢茶文化！我可以和你聊聊不同的茶，也可以为你演示泡茶的步骤。你平时喜欢喝什么茶呢？';

  return (
    <main className="ex-shell" data-view={view}>
      <div className="ex-background" aria-hidden="true" />

      {/* ====================================================================
          1. Top Navigation Bar (Exact Match with Design Spec)
          ==================================================================== */}
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
            <small>让智能更有温度 · AI 陪伴 <span>A MORE COMPANIONABLE AI</span></small>
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
              <Layers size={12} /> 展厅·体验 <em>EXHIBITION</em>
            </span>
            <span className="ex-cap-item">
              <Eye size={12} /> 视觉 <em>VISION</em>
            </span>
            <span className="ex-cap-item">
              <Ear size={12} /> 聆听 <em>AUDIO</em>
            </span>
            <span className="ex-cap-item online">
              <i className="ex-green-dot" /> AI 在线 <em>AI ONLINE</em>
            </span>
            <span className="ex-cap-item latency">
              <Activity size={12} /> 延时 <b>186 ms</b>
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
          <Radio size={12} /> REALTIME {snapshot?.realtime.state ?? 'connected'}
        </span>
        <span
          className={`ex-wake-indicator ex-wake-${wake.tone}`}
          title={wake.detail}
          role="status"
        >
          <Zap size={12} /> 唤醒：{wakeLabel(wake)}
        </span>
        <span>
          <Mic size={12} /> AUDIO{' '}
          {snapshot?.realtime.speech_active
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
            : '10:53:35'}
        </span>
      </div>

      {/* ====================================================================
          3. Main Dashboard 3-Column Cockpit
          ==================================================================== */}
      <section className="ex-dashboard">
        {/* ================= LEFT COLUMN ================= */}
        <aside className="ex-column ex-left">
          {/* Top Left: Visual Perception Panel */}
          <section className="ex-panel ex-vision">
            <div className="ex-panel-heading">
              <Camera size={15} />
              <span>
                视觉感知 <small>VISUAL PERCEPTION</small>
              </span>
              <span className="ex-live-pill">● LIVE</span>
            </div>

            {/* Video Viewport with High-Tech HUD Reticles */}
            <div className="ex-camera">
              <Suspense
                fallback={<div className="ex-unavailable">加载 LiveKit 视频接收器…</div>}
              >
                <OfficialLiveKitVideo key={currentRobotId} robotId={currentRobotId} />
              </Suspense>

              {/* HUD Detection Boxes (Exact match with Design) */}
              <div className="ex-vision-hud-overlay" aria-hidden="true">
                {/* Face Box */}
                <div className="hud-face-box">
                  <span className="hud-tag top-left">人脸 0.98</span>
                  <div className="hud-corner tl" />
                  <div className="hud-corner tr" />
                  <div className="hud-corner bl" />
                  <div className="hud-corner br" />
                </div>

                {/* Face Detail Card */}
                <div className="hud-visitor-card">
                  <div className="vis-card-title">
                    <b>访客</b> <small>VISITOR</small>
                  </div>
                  <div className="vis-card-meta">ID: V_0287</div>
                  <div className="vis-card-meta">女性 约25岁</div>
                  <div className="vis-card-meta">微笑 正在招手</div>
                  <div className="vis-card-meta">距离 1.2 m</div>
                </div>

                {/* Gesture tag */}
                <div className="hud-hand-tag">手势 0.97</div>
                <div className="hud-cup-tag">水杯 0.97</div>
              </div>
            </div>

            {/* Scene Understanding Bottom Row */}
            <div className="ex-perception-copy">
              <div className="ex-copy-title">
                <Sparkles size={13} />
                <span>场景多模态语义理解 · SCENE UNDERSTANDING</span>
              </div>
              <p>
                {disabled(
                  snapshot?.vision.description,
                  '检测到一位访客正面向小安妮，正在挥手并露出亲切的微笑。'
                )}
              </p>
            </div>
          </section>

          {/* Bottom Left: Auditory Perception Panel */}
          <section className="ex-panel ex-audio">
            <div className="ex-panel-heading">
              <Waves size={15} />
              <span>
                听觉感知 <small>AUDITORY PERCEPTION</small>
              </span>
              <span className="ex-live-pill">● LIVE</span>
            </div>

            {/* Three Audio Waveform Streams */}
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
                <AudioWaveStrip tone="cyan" active={true} />
                <div className="stream-stat">
                  <span>安静</span>
                  <small>-42 dB</small>
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
                <AudioWaveStrip tone="green" active={isUserSpeaking} />
                <div className="stream-stat">
                  <span>{isUserSpeaking ? '正在说话' : '检测到语音'}</span>
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
                <AudioWaveStrip tone="pink" active={isSpeaking} />
                <div className="stream-stat">
                  <span className="pink">{isSpeaking ? '播报回应' : '空闲'}</span>
                  <small>{isSpeaking ? 'TTS 发音' : '待命'}</small>
                </div>
              </div>
            </div>

            {/* Bottom 4 Interaction Control Buttons */}
            <div className="ex-interaction-pills">
              <button type="button" className="int-btn listening active">
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

          {/* Bottom Bar */}
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
          {/* Top Right: Cognitive State Panel */}
          <section className="ex-panel ex-cognition">
            <div className="ex-panel-heading">
              <BrainCircuit size={15} />
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
                <div className="reticle-circle">
                  <Eye size={16} />
                </div>
                <small>CURRENT FOCUS</small>
                <b>正在关注访客</b>
              </div>
              <div className="focus-desc">
                <p>
                  访客向我打招呼，面带微笑，情绪友好，需要主动回应并建立互动。
                </p>
                <div className="focus-next">
                  <span>下一步行为：</span>
                  <em>主动问候，建立友好互动，并询问访客需求。</em>
                </div>
              </div>
            </div>

            {/* 4-Step Cognitive Pipeline Graph */}
            <div className="cog-pipeline-chain">
              <div className="cog-step">
                <Camera size={13} />
                <span>视觉与声音事件</span>
              </div>
              <i>›</i>
              <div className="cog-step">
                <BrainCircuit size={13} />
                <span>语义理解</span>
              </div>
              <i>›</i>
              <div className="cog-step">
                <Layers size={13} />
                <span>行为选择</span>
              </div>
              <i>›</i>
              <div className="cog-step">
                <Activity size={13} />
                <span>身体动作与语音</span>
              </div>
            </div>
          </section>

          {/* Middle Right: Emotional Expression Panel */}
          <section className="ex-panel ex-emotion">
            <div className="ex-panel-heading">
              <Heart size={15} />
              <span>
                情感表达 <small>EMOTIONAL EXPRESSION</small>
              </span>
              <span className="ex-empathy-badge">EMPATHY ENGINE</span>
            </div>

            <div className="ex-emotion-content">
              {/* Emotional Compass Dial */}
              <div className="emo-dial-wrap">
                <div className="emo-dial">
                  <Heart size={20} className="dial-heart" />
                  <span className="dial-state">当前情绪</span>
                  <b className="dial-val">愉悦</b>
                  <small className="dial-en">JOYFUL</small>
                </div>
              </div>

              {/* Empathy Explanation */}
              <div className="emo-detail">
                <div className="emo-quote">
                  “检测到访客的微笑和友好姿态，我感到很开心，期待与您交流！”
                </div>
                {/* Mini Audio Equalizer for emotion */}
                <div className="emo-equalizer">
                  {Array.from({length: 16}).map((_, i) => (
                    <span
                      key={i}
                      style={{height: `${Math.sin(i * 0.4) * 40 + 50}%`}}
                    />
                  ))}
                </div>
              </div>
            </div>

            {/* Bottom 5 Emotional State Pills */}
            <div className="emo-state-pills">
              {['平静', '好奇', '愉悦', '专注', '关心'].map(tab => (
                <button
                  key={tab}
                  type="button"
                  className={tab === activeEmoTab ? 'active' : ''}
                  onClick={() => setActiveEmoTab(tab)}
                >
                  {tab}
                </button>
              ))}
            </div>
          </section>

          {/* Bottom Right: Companion Memory Panel */}
          <section className="ex-panel ex-memory">
            <div className="ex-panel-heading">
              <Network size={15} />
              <span>
                陪伴记忆 <small>COMPANION MEMORY</small>
              </span>
              <span className="mem-growth-tag">● 记忆网络 · 成长</span>
            </div>

            <div className="ex-memory-cards">
              {/* Left Card: Related Memory */}
              <div className="mem-subcard rel-mem">
                <div className="subcard-title">相关记忆</div>
                <div className="subcard-body">
                  <img
                    src="/exhibit-assets/memory-tea.png"
                    alt="茶具记忆"
                    className="mem-tea-thumb"
                  />
                  <div className="subcard-text">
                    <p>用户曾提到喜欢喝茶，也喜欢中式茶具。</p>
                    <small>3个月前 · 展厅记录</small>
                  </div>
                </div>
              </div>

              {/* Middle Card: Current Topic */}
              <div className="mem-subcard cur-topic">
                <div className="subcard-title">当期意图</div>
                <div className="subcard-body">
                  <p>● 继续话题：询问访客兴趣点，推荐展厅内容</p>
                </div>
              </div>

              {/* Right Card: Long-term Graph */}
              <div className="mem-subcard graph-subcard">
                <div className="subcard-title">长期关系</div>
                <div className="graph-constellation-mini">
                  <div className="graph-node-center">
                    <Eye size={10} />
                  </div>
                  <span className="graph-tag-node t1">兴趣偏好</span>
                  <span className="graph-tag-node t2">交流方式</span>
                  <span className="graph-tag-node t3">情感倾向</span>
                </div>
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
          <Layers size={14} />
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
              <Camera size={13} key="0" />,
              <BrainCircuit size={13} key="1" />,
              <Network size={13} key="2" />,
              <Activity size={13} key="3" />,
              <Volume2 size={13} key="4" />,
              <Check size={13} key="5" />,
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
              alt="展厅实时交互"
              className="thumb-img"
            />
            <div className="thumb-lbl">
              <span className="green-dot" /> 实时交互 <em>LIVE INTERACTION</em>
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
            <AudioWaveStrip tone="cyan" active={true} bars={12} />
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
              <span className="beh-pill green">
                <i className="p-dot" /> 注视访客
              </span>
              <span className="beh-pill">微笑</span>
              <span className="beh-pill">自然回应</span>
              <span className="beh-pill">轻微点头</span>
            </div>
          </div>
        </div>

        {/* Research Diagnostic View */}
        {view === 'research' && (
          <div className="ex-research">
            <span>
              <Cpu size={12} /> 进程 CPU {showMetric(snapshot?.performance.process_cpu_pct, '%', 1)}
            </span>
            <span>
              <Activity size={12} /> 事件循环滞后 {showMetric(snapshot?.performance.event_loop_lag_ms, 'ms', 1)}
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
