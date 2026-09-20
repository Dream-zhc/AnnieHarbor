import {useCallback,useEffect,useRef,useState} from 'react';
import {api,robotPath,type Health,type Session,type RealtimeEvent,type RealtimeEvents} from './api';
import {RoomInput,type RoomRoute} from './pcm';
import {RoomPlaybackTester} from './playback';

type Conversation={chat_id:string;persona_id:string;[key:string]:unknown};
type Turn={turn_id:string;user_text:string;assistant_generated_text?:string|null;assistant_published_text?:string|null;assistant_spoken_text?:string|null;status:string;cancelled?:boolean;playback_evidence_status?:string};
type History={turns:Turn[];robot_id:string;chat_id:string;persona_id:string};
const pretty=(value:unknown)=>JSON.stringify(value,null,2);
const errorText=(error:unknown)=>error instanceof Error?error.message:String(error);

export function Workbench(){
  const [health,setHealth]=useState<Health|null>(null);
  const [robotId,setRobotId]=useState('');
  const [session,setSession]=useState<Session|null>(null);
  const [conversations,setConversations]=useState<Conversation[]>([]);
  const [history,setHistory]=useState<History|null>(null);
  const [context,setContext]=useState<unknown>(null);
  const [pcm,setPcm]=useState<unknown>(null);
  const [routes,setRoutes]=useState<RoomRoute[]>([]);
  const [coordinator,setCoordinator]=useState<unknown>(null);
  const [social,setSocial]=useState<unknown>(null);
  const [message,setMessage]=useState('');
  const [chat,setChat]=useState('');
  const [persona,setPersona]=useState('');
  const [revision,setRevision]=useState(1);
  const [text,setText]=useState('');
  const [busy,setBusy]=useState(false);
  const [realtimeEvents,setRealtimeEvents]=useState<RealtimeEvent[]>([]);
  const [liveText,setLiveText]=useState('');
  const selection=useRef(0);
  const eventCursor=useRef(0);
  const currentRobot=health?.robots.find(robot=>robot.robot_id===robotId);
  const realtimeState=currentRobot?.realtime;
  const realtimeConfigured=health?.capabilities.llm!=='not_connected';

  const refreshHealth=useCallback(async()=>{
    try {
      const result=await api<Health>('/health','GET',undefined,undefined,[503]);
      setHealth(result);
      setRobotId(old=>result.robots.some(r=>r.robot_id===old)?old:result.robots[0]?.robot_id??'');
    }catch(error){setMessage('Runtime 健康检查失败：'+errorText(error));}
  },[]);
  useEffect(()=>{
    void refreshHealth();
    const timer=window.setInterval(()=>{void refreshHealth();},2000);
    return()=>window.clearInterval(timer);
  },[refreshHealth]);

  const refreshSelected=useCallback(async(id:string)=>{
    const ticket=++selection.current;
    setHistory(null);setContext(null);
    if(!id){setSession(null);return;}
    try{
      const base=robotPath(id);
      const s=await api<Session>(base+'/session');
      if(ticket!==selection.current)return;
      setSession(s);setChat(s.chat_id);setPersona(s.persona_id);setRevision(s.persona_revision);
      const results=await Promise.all([
        api<Conversation[]>(base+'/conversations'),
        api<History>(base+'/conversations/'+encodeURIComponent(s.chat_id)+'?persona_id='+encodeURIComponent(s.persona_id)),
        api<unknown>(base+'/context'),api<unknown>(base+'/inputs'),
        api<RoomRoute[]>('/api/phase2/rooms'),api<unknown>(base+'/coordinator'),
        api<unknown>(base+'/social-state')
      ]);
      if(ticket!==selection.current)return;
      setConversations(results[0]);setHistory(results[1]);setContext(results[2]);
      setPcm(results[3]);setRoutes(results[4]);setCoordinator(results[5]);setSocial(results[6]);
    }catch(error){if(ticket===selection.current)setMessage('读取会话失败：'+errorText(error));}
  },[]);
  useEffect(()=>{void refreshSelected(robotId);return()=>{selection.current++;};},[robotId,refreshSelected]);
  useEffect(()=>{
    setRealtimeEvents([]);
    setLiveText('');
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation]);
  useEffect(()=>{
    if(!robotId||!session)return;
    let active=true,reading=false;
    const key=[robotId,session.chat_id,session.persona_id,session.session_epoch,session.generation].join(':');
    const poll=async()=>{
      if(reading)return;
      reading=true;
      try{
        const result=await api<RealtimeEvents>(robotPath(robotId)+'/realtime/events?after='+eventCursor.current);
        if(!active)return;
        eventCursor.current=result.latest;
        const items=result.events.filter(item=>
          [item.robot_id,item.chat_id,item.persona_id,item.session_epoch,item.generation].join(':')===key);
        if(result.gap)setMessage('实时事件缓冲区发生缺口；请查看完整 Ledger。');
        if(items.length){
          setRealtimeEvents(old=>[...old,...items].slice(-70));
          for(const item of items){
            if(item.kind==='text.delta'||item.kind==='text.done')setLiveText(item.text??'');
            if(item.kind==='turn.started'||item.kind==='session.replaced')setLiveText('');
            if(item.kind==='session.error')setMessage('Realtime: '+item.error);
            if(item.kind==='turn.done'){setLiveText('');void refreshSelected(robotId);}
          }
        }
      }catch(error){if(active)setMessage('实时事件读取失败：'+errorText(error));}
      finally{reading=false;}
    };
    void poll();
    const timer=window.setInterval(()=>{void poll();},250);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation,refreshSelected]);
  useEffect(()=>{
    if(!robotId)return;
    let active=true;
    const poll=async()=>{
      try{
        const [inputs,rooms]=await Promise.all([api<unknown>(robotPath(robotId)+'/inputs'),api<RoomRoute[]>('/api/phase2/rooms')]);
        if(active){setPcm(inputs);setRoutes(rooms);}
      }catch{/* Detailed error remains in the session refresh path. */}
    };
    const timer=window.setInterval(()=>{void poll();},2000);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId]);

  async function operation(name:string,body?:unknown,method='POST'){
    if(!robotId||busy)return;
    const id=robotId;
    const ticket=selection.current;
    setBusy(true);setMessage('');
    try{
      const result=await api<unknown>(robotPath(id)+'/'+name,method,body);
      if(ticket===selection.current){setMessage('操作结果：'+pretty(result));await refreshSelected(id);await refreshHealth();}
    }catch(error){if(ticket===selection.current)setMessage(errorText(error));}
    finally{setBusy(false);}
  }
  async function deleteChat(){
    if(!session||busy)return;
    if(!window.confirm(`仅删除 ${session.robot_id} / ${session.persona_id} / ${session.chat_id} 的历史？不可恢复。`))return;
    await operation('conversations/'+encodeURIComponent(session.chat_id)+'?persona_id='+encodeURIComponent(session.persona_id),undefined,'DELETE');
  }
  const choose=(id:string)=>{
    selection.current++;eventCursor.current=0;setRealtimeEvents([]);setLiveText('');
    setRobotId(id);setSession(null);setHistory(null);setContext(null);setPcm(null);setMessage('');
  };
  return <div className="shell">
    <header><strong>ANNIE HARBOR <span>· Phase 4B</span></strong><span className="sub">Realtime 文本 / 独立 TTS 输出诊断</span></header>
    <section className="status" aria-live="polite">
      <span className={health?.ready?'ok':'bad'}>{health?.ready?'Runtime 运行中':'Runtime 未连接'}</span>
      <span>PID {health?.pid??'—'}</span><span>{health?.robots.length??0} 个机器人</span>
      <button onClick={()=>void refreshHealth()}>刷新</button>
    </section>
    <div className="layout">
      <aside>
        <h2>机器人 / Room</h2>
        {health?.robots.map(robot=><button className={robotId===robot.robot_id?'selected':''} key={robot.robot_id} onClick={()=>choose(robot.robot_id)}>
          <b>{robot.robot_id}</b><small>{robot.room_id} · {robot.state}</small>
        </button>)}
        <h2>会话</h2>
        {conversations.map((item,index)=><button key={index} className={session?.chat_id===item.chat_id&&session.persona_id===item.persona_id?'selected':''}
          onClick={()=>void operation('session',{chat_id:item.chat_id,persona_id:item.persona_id,persona_revision:1})}>
          <b>{item.chat_id}</b><small>{item.persona_id}</small>
        </button>)}
      </aside>
      <main>
        <section className="panel">
          <h1>{currentRobot?.robot_id??'选择机器人'} <small>{currentRobot?.room_id??''}</small></h1>
          <p>Realtime: {realtimeState?.state??'未配置'} · 文本与独立 TTS 分路；设备报告、浏览器测试和未确认状态分开展示。</p>
          {realtimeState?.error&&<p role="alert" className="bad">连接错误：{realtimeState.error}</p>}
          <div className="badges">{Object.entries(health?.capabilities??{}).map(([name,state])=><span key={name}>{name}: {state}</span>)}</div>
          <div className="fields">
            <label>Chat<input aria-label="Chat" value={chat} onChange={e=>setChat(e.target.value)}/></label>
            <label>Persona<input aria-label="Persona" value={persona} onChange={e=>setPersona(e.target.value)}/></label>
            <label>Revision<input aria-label="Revision" type="number" min="1" value={revision} onChange={e=>setRevision(Number(e.target.value))}/></label>
          </div>
          <div className="actions"><button disabled={busy||!chat.trim()||!persona.trim()||revision<1} onClick={()=>void operation('session',{chat_id:chat.trim(),persona_id:persona.trim(),persona_revision:revision})}>创建 / 切换会话</button>
            <button className="danger" disabled={busy||!session} onClick={()=>void deleteChat()}>删除当前会话</button></div>
          {session&&<p className="metadata">Session epoch {session.session_epoch} / generation {session.generation} / persona revision {session.persona_revision}</p>}
        </section>
        <section className="panel">
          <h2>对话 · 完整历史</h2>
          <div className="transcript" aria-label="完整历史">
            {history?.turns.length===0&&<p>当前会话还没有历史。</p>}
            {history?.turns.map(turn=><article className="turn" key={turn.turn_id}>
              <div className="bubble user"><b>用户</b><p>{turn.user_text}</p></div>
              <div className="bubble assistant"><b>助手 · {turn.status}{turn.cancelled?' / 已取消':''}</b>
                <p>{turn.assistant_spoken_text||turn.assistant_generated_text||'未生成回复'}</p>
                <small>生成：{turn.assistant_generated_text?'有':'无'} · 已发布文本：{turn.assistant_published_text??'无'} · 已确认播放：{turn.assistant_spoken_text??'未知'} · 证据：{turn.playback_evidence_status??'unknown'}</small>
              </div>
            </article>)}
            {!history&&<p>正在读取历史…</p>}
          </div>
          {liveText&&<div className="bubble assistant" aria-live="polite"><b>Realtime · 正在生成</b><p>{liveText}</p></div>}
          <details><summary>实时转写、判轮与工具事件</summary><pre>{pretty(realtimeEvents)}</pre></details>
          <details><summary>完整 Ledger JSON</summary><pre>{pretty(history)}</pre></details>
          <form onSubmit={event=>{event.preventDefault();if(text.trim()){void operation('realtime/text',{text:text.trim()});setText('');}}}>
            <label>网页文本 → Realtime<input value={text} maxLength={2000} onChange={e=>setText(e.target.value)} placeholder="输入测试消息"/></label>
            <button disabled={busy||!text.trim()||!realtimeState?.ready}>发送真实对话</button>
          </form>
          {!realtimeConfigured&&<p>Realtime 未配置；仅保留下方 Fake 诊断，不自动回退为伪装的真实回复。</p>}
          <div className="actions"><button disabled={busy} onClick={()=>void operation('cancel')}>取消当前回合</button>
            <button disabled={busy} onClick={()=>void refreshSelected(robotId)}>刷新当前会话</button></div>
          {message&&<pre className="notice" role="status">{message}</pre>}
        </section>
        <section className="panel"><h2>当前有效上下文（最多 20 轮）</h2><pre>{pretty(context)}</pre></section>
        <section className="panel"><h2>独立 TTS / PCM / 嘴型</h2>
          <p>输出到 {currentRobot?.room_id??'未选择'} / reb_tts。模型回复与合成、Room 发布、设备已播放是不同状态；PCM 发布不代表设备已播放。</p>
          <div className="badges">
            <span>TTS：{realtimeState?.speech_output?.state??'未配置'}</span>
            <span>首音频：{realtimeState?.speech_output?.first_audio_ms??'未测'} ms</span>
            <span>首发布：{realtimeState?.speech_output?.first_publish_ms??'未测'} ms</span>
            <span>已发布 PCM 帧：{realtimeState?.speech_output?.pcm_frames_published??0}</span>
            <span>设备播放：{realtimeState?.speech_output?.playback_evidence??'unknown'}</span>
            <span>嘴型：{realtimeState?.speech_output?.viseme??'not_connected'}</span>
          </div>
          <p>起播回执：{realtimeState?.speech_output?.started_reported_ms??'无'}；已报告播放进度：{realtimeState?.speech_output?.played_sample_count??0} 样本；停止请求：{realtimeState?.speech_output?.stop_requested_ms??'无'}；停止帧发布：{realtimeState?.speech_output?.stop_published_ms??'无'}；停止回执：{realtimeState?.speech_output?.stop_confirmed_ms??'无'}（{realtimeState?.speech_output?.last_stop_evidence??'unknown'}，CID {realtimeState?.speech_output?.stopped_conversation_id??'无'}）。</p>
          <RoomPlaybackTester key={robotId} route={routes.find(route=>route.robot_id===robotId)} onCancel={()=>void operation('cancel')}/>
          {realtimeState?.speech_output?.error&&<p role="alert" className="bad">TTS 错误：{realtimeState.speech_output.error}</p>}
          <p>未安装对齐模型或无设备回执时，嘴型与已播证据保持未就绪/unknown；不以浏览器试听模拟设备播放，也不重复合成。</p>
          <details><summary>音频、停止及嘴型事件</summary><pre>{pretty(realtimeEvents.filter(e=>e.kind.startsWith('tts.')||e.kind.startsWith('viseme.')))}</pre></details>
          <a href="/phase-tests/phase4b.html">Phase 4B 验收步骤</a>
        </section>
        <section className="panel"><h2>PCM / Room 输入诊断</h2>
          <p>采用 Phase 2 MIC_AUDIO BinaryFrame 封包；页面退出或切机器人时释放本页麦克风。无法自动检测另一音源，请先人工确认。</p>
          <RoomInput key={robotId} robotId={robotId} route={routes.find(route=>route.robot_id===robotId)}/>
          <pre>{pretty({inputs:pcm,routes})}</pre>
          <a href="/phase-tests/phase2.html">进入 Phase 2 真实 Room / PCM 验收页</a>
        </section>
        <section className="panel"><h2>开发者诊断</h2>
          <p>以下按钮仅操作当前机器人 Fake Runtime，不代表实际动作或真实硬件停机。</p>
          <div className="actions"><button disabled={busy} onClick={()=>void operation('start')}>启动 Fake 机器人</button>
            <button disabled={busy} onClick={()=>void operation('stop')}>停止 Fake 机器人</button>
            <button disabled={busy} className="danger" onClick={()=>void operation('fail')}>注入 Fake 故障</button></div>
          <button disabled={busy} onClick={()=>void operation('turns',{text:'Fake debug probe'})}>发送独立 Fake 调试回合</button>
          <h3>TurnCoordinator</h3><pre>{pretty(coordinator)}</pre><h3>SocialState</h3><pre>{pretty(social)}</pre></section>
      </main>
    </div>
  </div>;
}
