import {useEffect,useMemo,useState,type ReactNode} from 'react';
import {api,type Health,type PersonaSnapshot,type RealtimeDiagnostics,type RealtimeEvent,type RestoredContext,type Robot,type Session,type SpeechStatus} from './api';
import {TurnDiagnostics} from './turn-diagnostics';

type Turn={turn_id:string;user_text:string;assistant_spoken_text?:string|null;assistant_generated_text?:string|null;
  status:string;playback_evidence_status?:string};
type SocialState=Record<string,{value:unknown;source:string;expires_at_ms:number|null}>;
type DrawerData={
  robotId:string;roomId:string|undefined;roomConnected:boolean;
  session:Session|null;realtime?:Robot['realtime'];speech?:SpeechStatus;
  diagnostics:RealtimeDiagnostics|null;
  personaForSession:PersonaSnapshot|null;restoredForSession:RestoredContext|null;
  savedPreference:string;history:{turns:Turn[]}|null;social:SocialState;
  firstAsrDeltaMs:number|null;previewTranscript:string;
  finalTranscript:{turnId:string;text:string}|null;ambient:string[];
  pending:boolean;settingPreference:boolean;text:string;realtimeEvents:RealtimeEvent[];
  context:unknown;coordinator:unknown;pcm:unknown;routes:unknown[];
  systemError?:string|null;
};
type Preference='normal'|'low'|'none';

const conversationChecks=[
  {label:'改口 · 茶与咖啡',turns:['我喜欢喝茶。','不是茶，是咖啡。','我刚才更正喜欢喝什么？']},
  {label:'指代与承诺',turns:['我明天准备去看电影。','继续刚才的话题。','你刚才说过你答应了我什么吗？没有就直说。']},
  {label:'停止追问',turns:['今天有点累，不想讲原因。','别老问我，我不想继续聊了。','知道了。']},
];

const pretty=(value:unknown)=>JSON.stringify(value,null,2);

const stateLabel=(value:string|undefined)=>{
  if(!value)return '未接入';
  const names:Record<string,string>={
    connected:'已连接',ready:'已就绪',room_gateway_ready:'已就绪',ledger_ready:'已就绪',
    not_connected:'未接入',not_started:'未接入',not_configured:'未配置',
    configured:'已配置',unknown:'未确认（unknown）',idle:'空闲',
    disconnected:'连接断开',error:'异常',failed:'失败',running:'运行中',
    hold_requested:'已请求暂停，等待设备回执',held_confirmed:'设备已报告暂停',
    resume_requested:'已请求恢复，等待设备回执',resumed_confirmed:'设备已报告恢复',
    control_error:'控制命令发送失败',
    cancelled:'已取消',completed:'已完成',
    fake:'Fake（调试）',subscribed:'已订阅',loading:'加载中'
  };
  return names[value]??value;
};

export function State({value,good}:{value:string|undefined;good?:boolean}){
  return <span className={'state-pill '+(good?'state-good':!value||value==='unknown'||value.startsWith('not_')?'state-muted':'')}>{stateLabel(value)}</span>;
}

function Block({children,title}:{children:ReactNode;title:string}){
  return <section className="surface"><div className="surface-heading"><h2>{title}</h2></div>{children}</section>;
}


export function DeveloperDrawer({data,onClose,onFillText,onQuestionPressure,onClearError}:{
  data:DrawerData;onClose:()=>void;onFillText:(text:string)=>void;
  onQuestionPressure:(value:Preference)=>void;onClearError?:()=>void;
}){
  const {robotId,roomId,roomConnected,session,realtime,speech,personaForSession,
    diagnostics,restoredForSession,savedPreference,history,social,firstAsrDeltaMs,
    previewTranscript,finalTranscript,ambient,pending,settingPreference,text,
    realtimeEvents,context,coordinator,pcm,routes,systemError}=data;
  const [checkIndex,setCheckIndex]=useState(0);
  const [checkStep,setCheckStep]=useState(0);
  const [processHealth,setProcessHealth]=useState<Health|null>(null);
  useEffect(()=>{
    let closed=false;
    const load=()=>{void api<Health>('/health').then(result=>{if(!closed)setProcessHealth(result);}).catch(()=>{
      if(!closed)setProcessHealth(null);
    });};
    load();
    const interval=setInterval(load,5000); // developer drawer only; no hot-path polling
    return ()=>{closed=true;clearInterval(interval);};
  },[]);
  const turns=history?.turns??[];
  const scenarioResults=useMemo(()=>{
    const prompts=conversationChecks[checkIndex].turns;
    const matching=new Map<string,Turn>();
    for(const turn of history?.turns??[]){
      if(prompts.includes(turn.user_text.trim()))matching.set(turn.user_text.trim(),turn);
    }
    return prompts.map(prompt=>matching.get(prompt));
  },[history,checkIndex]);

  return <div className="developer-backdrop" role="presentation"
      onMouseDown={event=>{if(event.target===event.currentTarget)onClose();}}>
      <aside className="developer-drawer" role="dialog" aria-modal="true" aria-labelledby="developer-title">
        <div className="drawer-head">
          <h2 id="developer-title">开发者诊断</h2>
          <button className="quiet-button" type="button" onClick={()=>onClose()} aria-label="关闭开发者诊断">关闭 ✕</button>
        </div>
        <div className="drawer-content">
          <details className="inspector-group inspector-panel">
            <summary>Phase 9 · 进程与队列诊断</summary>
            <div className="info-line"><span>CPU · 进程</span><strong>{processHealth?.performance?.cpu_percent==null?'未采样':`${processHealth.performance.cpu_percent}%`}</strong></div>
            <div className="info-line"><span>峰值 RSS（非当前内存）</span><strong>{processHealth?.performance?.rss_peak_mb==null?'平台未提供':`${processHealth.performance.rss_peak_mb} MB`}</strong></div>
            <div className="info-line"><span>事件循环延迟 / 最大</span><strong>{processHealth?.performance?.loop_lag_ms==null?'未采样':`${processHealth.performance.loop_lag_ms} / ${processHealth.performance.loop_lag_max_ms} ms`}</strong></div>
            <div className="info-line"><span>Trace队列 / 丢弃</span><strong>{processHealth?.storage?`${processHealth.storage.trace_queue_depth}/${processHealth.storage.trace_queue_capacity} · ${processHealth.storage.trace_dropped}`:'未获取'}</strong></div>
            <div className="info-line"><span>Ledger 队列</span><strong>{processHealth?.storage?`${processHealth.storage.ledger_queue_depth}/${processHealth.storage.ledger_queue_capacity}`:'未获取'}</strong></div>
            <small>仅抽屉开启时每5秒读取健康快照；CPU为进程累计CPU时间差值，可能超过100%。Windows缺少峰值RSS数据时留空。</small>
          </details>
          {systemError&&<div className="drawer-error-alert" role="alert">
            <div>
              <strong>⚠️ 系统异常 / 最近错误</strong>
              <p>{systemError}</p>
            </div>
            {onClearError&&<button type="button" className="quiet-button" onClick={onClearError}>忽略错误</button>}
          </div>}
          <TurnDiagnostics key={`${session?.robot_id}/${session?.chat_id}/${session?.persona_id}/${session?.generation}`}
            turns={turns} diagnostics={diagnostics}
            latestTurnId={realtime?.last_turn_timeline?.turn_id}
            liveUser={finalTranscript?{turnId:finalTranscript.turnId,text:finalTranscript.text}:null}/>
          <details className="inspector-group inspector-panel">
            <summary>其他系统信息与调试</summary>
          <section className="inspector-group">
            <h3>当前语音链路</h3>
            <div className="info-line"><span>听觉 / ASR</span><strong>Qwen-Audio smart_turn</strong></div>
            <div className="info-line"><span>最终回答模型</span><strong>Qwen-Audio 3.0 Realtime Flash</strong></div>
            <div className="info-line"><span>LLM 状态</span><State value={realtime?.state??'not_configured'}/></div>
            <div className="info-line"><span>云端起声 → 首次 ASR 增量</span><strong>{firstAsrDeltaMs!=null?firstAsrDeltaMs+' ms':'—'}</strong></div>
            {previewTranscript&&<p>ASR 暂定：{previewTranscript}</p>}
            {finalTranscript?.text&&<p>ASR 最终：{finalTranscript.text}</p>}
            {ambient.length>0&&<p>smart_turn 忽略的输入：{ambient.join(' / ')}</p>}
          </section>

          <details className="inspector-group inspector-panel" open>
            <summary>Persona 与追问偏好 <span>{personaForSession?.label??session?.persona_id??'—'}</span></summary>
            <div className="info-line"><span>Persona</span><strong>{session?.persona_id??'—'}</strong></div>
            <div className="info-line"><span>Revision</span><strong>{session?.persona_revision??'—'}</strong></div>
            <div className="info-line"><span>历史回合</span><strong>{history?.turns.length??'—'}</strong></div>
            <div className="info-line"><span>表达风格</span><strong>{personaForSession?.configured?personaForSession.label:'未配置预设或待就绪'}</strong></div>
            <label className="quality-setting">追问偏好
              <select aria-label="追问偏好" value={savedPreference} disabled={!session||!realtime?.ready||!!realtime?.response_id||pending||settingPreference} onChange={event=>void onQuestionPressure(event.target.value as 'normal'|'low'|'none')}>
                <option value="normal">自然追问</option><option value="low">少追问</option><option value="none">不主动追问</option>
              </select>
            </label>
            <small>当前模型生效：{personaForSession?.question_pressure==='none'?'不主动追问':personaForSession?.question_pressure==='low'?'少追问':personaForSession?.question_pressure==='normal'?'自然追问':'等待 Session 就绪'}</small>
            {diagnostics?.dialogue_policy&&<>
              <div className="info-line"><span>近期应答 / 已播放追问</span><strong>{diagnostics.dialogue_policy.short_reply_streak} / {diagnostics.dialogue_policy.delivered_question_streak}</strong></div>
              <div className="info-line"><span>追问策略</span><strong>{diagnostics.dialogue_policy.followup==='avoid'?'暂缓主动追问':'视当前语境决定'}</strong></div>
              <div className="info-line"><span>动态提示确认</span><strong>{diagnostics.dialogue_policy.provider_hint_confirmed?'已收到会话确认':'待确认（可能未赶上当前轮）'}</strong></div>
              <small>长度档位是本地推断，不代表模型已按该档位生成；实际回复以对话记录为准。</small>
            </>}
          </details>

          <details className="inspector-group inspector-panel">
            <summary>多轮对话复跑 <span>3 组</span></summary>
            <label className="quality-setting">场景
              <select aria-label="多轮复跑场景" value={checkIndex} onChange={event=>{setCheckIndex(Number(event.target.value));setCheckStep(0);}}>
                {conversationChecks.map((item,index)=><option key={item.label} value={index}>{item.label}</option>)}
              </select>
            </label>
            <p className="quality-note">{checkStep+1} / {conversationChecks[checkIndex].turns.length}</p>
            <p className="quality-prompt">{conversationChecks[checkIndex].turns[checkStep]}</p>
            <div className="quality-actions">
              <button className="quiet-button" disabled={!!text.trim()||pending||!session} onClick={()=>onFillText(conversationChecks[checkIndex].turns[checkStep])}>填入输入框</button>
              <button className="quiet-button" disabled={checkStep===0} onClick={()=>setCheckStep(step=>Math.max(0,step-1))}>上一句</button>
              <button className="quiet-button" disabled={checkStep===conversationChecks[checkIndex].turns.length-1} onClick={()=>setCheckStep(step=>Math.min(conversationChecks[checkIndex].turns.length-1,step+1))}>下一句</button>
            </div>
            <details style={{marginTop:'8px'}}>
              <summary>本 Chat 回复</summary>
              {conversationChecks[checkIndex].turns.map((prompt,index)=>{
                const turn=scenarioResults[index];
                return <div className="quality-result" key={index}>
                  <strong>{index+1}. {prompt}</strong>
                  <p>{turn?(turn.assistant_spoken_text||turn.assistant_generated_text||'尚无完整回复'):'尚未发送本句'}</p>
                  {turn&&<small>实际状态：{turn.status} · 播放：{turn.playback_evidence_status??'unknown'}</small>}
                </div>;
              })}
            </details>
          </details>

          <details className="inspector-group inspector-panel">
            <summary>连接详情</summary>
            <div className="info-line"><span>Room 麦克风</span><State value={roomConnected?'connected':'not_connected'} good={roomConnected}/></div>
            <div className="info-line"><span>本次 Realtime 已发送</span><strong>{realtime?.session_frames_sent??0} 帧</strong></div>
            <div className="info-line"><span>模型检测语音开始</span><strong>{realtime?.speech_started_count??0} 次</strong></div>
            <div className="info-line"><span>模型完成转写</span><strong>{realtime?.transcripts_completed??0} 次</strong></div>
            <div className="info-line"><span>判为无效轮次</span><strong>{realtime?.invalid_turns??0} 次</strong></div>
            {(realtime?.session_frames_sent??0)>100&&!(realtime?.speech_started_count)&&<p className="audio-guidance">未检测到语音开始 · 检查麦克风电平 / Room PCM</p>}
            <div className="info-line"><span>Session</span><strong>{session?.generation??'—'}</strong></div>
          </details>

          <div className="developer-grid">
            <Block title="当前目标">
              <div className="info-line"><span>Robot / Room</span><strong>{robotId} / {roomId??'—'}</strong></div>
              <div className="info-line"><span>Chat / Persona</span><strong>{session?.chat_id??'—'} / {session?.persona_id??'—'}</strong></div>
              <div className="info-line"><span>Session epoch / generation</span><strong>{session?.session_epoch??'—'} / {session?.generation??'—'}</strong></div>
            </Block>
            <Block title="Persona 与社交偏好">
              <div className="info-line"><span>当前表达预设</span><strong>{personaForSession?.configured?personaForSession.label:'未配置或待连接'}</strong></div>
              <div className="info-line"><span>实际生效偏好</span><strong>{personaForSession?.question_pressure??'待连接'}</strong></div>
              <details><summary>查看带来源的 SocialState</summary><pre>{pretty(social)}</pre></details>
            </Block>
            <Block title="实时与音频">
              <div className="info-line"><span>Realtime</span><State value={realtime?.state} good={realtime?.ready}/></div>
              <div className="info-line"><span>TTS</span><State value={speech?.state}/></div>
              <div className="info-line"><span>播放证据</span><strong>{speech?.playback_evidence??'unknown'}</strong></div>
              <div className="info-line"><span>嘴型</span><State value={speech?.viseme}/></div>
              {speech?.last_stop_reason&&<div className="info-line"><span>最近停播</span><strong>{speech.last_stop_reason}</strong></div>}
              <details><summary>Realtime / Function Calling / TTS 事件</summary><pre>{pretty(realtimeEvents)}</pre></details>
            </Block>
            <Block title="调度与社交状态">
              <details><summary>TurnCoordinator</summary><pre>{pretty(coordinator)}</pre></details>
              <details><summary>SocialState</summary><pre>{pretty(social)}</pre></details>
            </Block>
            <Block title="Room / PCM">
              <details><summary>输入与通道诊断</summary><pre>{pretty({inputs:pcm,routes})}</pre></details>
            </Block>
            <Block title="独立阶段测试">
              <div className="test-links">
                <a href="/phase-tests/phase1.html">Phase 1 · Fake / 多头测试 ↗</a>
                <a href="/phase-tests/phase2.html">Phase 2 · 测试 PCM / 麦克风 ↗</a>
                <a href="/phase-tests/phase3.html">Phase 3 · Ledger / 模拟回执 ↗</a>
                <a href="/phase-tests/phase4.html">Phase 4 · Realtime 验收 ↗</a>
                <a href="/phase-tests/phase4c.html">Phase 4C · 工作台验收 ↗</a>
                <a href="/phase-tests/phase5-context.html">Phase 5 · 短期上下文验收 ↗</a>
              </div>
            </Block>
          </div>
          </details>
        </div>
      </aside>
    </div>;
}
