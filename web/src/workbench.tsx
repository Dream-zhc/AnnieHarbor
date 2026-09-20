import {useCallback,useEffect,useRef,useState} from 'react';
import {gsap} from 'gsap';
import {useGSAP} from '@gsap/react';
import {api,robotPath,eventBelongsToSession,type Health,type Session,type RealtimeEvent,type RealtimeEvents} from './api';
import {RoomInput,type RoomRoute} from './pcm';
import {RoomPlaybackTester} from './playback';

type Conversation={chat_id:string;persona_id:string;persona_revision?:number;turn_count?:number;[key:string]:unknown};
type Turn={turn_id:string;user_text:string;assistant_generated_text?:string|null;assistant_published_text?:string|null;assistant_spoken_text?:string|null;status:string;cancelled?:boolean;playback_evidence_status?:string};
type History={turns:Turn[];robot_id:string;chat_id:string;persona_id:string};
const pretty=(value:unknown)=>JSON.stringify(value,null,2);
const errorText=(error:unknown)=>error instanceof Error?error.message:String(error);

export function Workbench(){
  const workbench=useRef<HTMLDivElement>(null);
  const [developerOpen,setDeveloperOpen]=useState(false);
  const [fakeTurnId,setFakeTurnId]=useState('');
  useGSAP(()=>{
    const media=gsap.matchMedia();
    media.add('(prefers-reduced-motion: no-preference)',()=>{
      gsap.from('.panel',{autoAlpha:0,y:12,duration:0.24,stagger:0.035,ease:'power1.out',clearProps:'all'});
    });
    return ()=>media.revert();
  },{scope:workbench});
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
  const selectedRoute=routes.find(route=>route.robot_id===robotId);

  const refreshHealth=useCallback(async()=>{
    try {
      const result=await api<Health>('/health','GET',undefined,undefined,[503]);
      setHealth(result);
      setRobotId(old=>result.robots.some(r=>r.robot_id===old)?old:result.robots[0]?.robot_id??'');
    }catch(error){setHealth(null);setMessage('Runtime 健康检查失败：'+errorText(error));}
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
    eventCursor.current=0;
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation]);
  useEffect(()=>{
    setFakeTurnId('');
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation]);
  useEffect(()=>{
    if(!robotId||!session)return;
    let active=true,reading=false;
    const poll=async()=>{
      if(reading)return;
      reading=true;
      try{
        const result=await api<RealtimeEvents>(robotPath(robotId)+'/realtime/events?after='+eventCursor.current);
        if(!active)return;
        eventCursor.current=result.latest;
        const items=result.events.filter(item=>eventBelongsToSession(item,session));
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
      if(ticket===selection.current){
        if(name==='turns'&&result&&typeof result==='object'&&'request_id' in result&&typeof result.request_id==='string'){
          setFakeTurnId(result.request_id);
        }
        setMessage('操作结果：'+pretty(result));await refreshSelected(id);await refreshHealth();
      }
    }catch(error){if(ticket===selection.current)setMessage(errorText(error));}
    finally{setBusy(false);}
  }
  async function deleteChat(){
    if(!session||busy)return;
    if(!window.confirm(`仅删除 ${session.robot_id} / ${session.persona_id} / ${session.chat_id} 的历史？不可恢复。`))return;
    await operation('conversations/'+encodeURIComponent(session.chat_id)+'?persona_id='+encodeURIComponent(session.persona_id),undefined,'DELETE');
  }
  async function cancelCurrent(){
    if(!robotId)return;
    const id=robotId,ticket=selection.current;
    try{
      const result=await api<unknown>(robotPath(id)+'/cancel','POST');
      if(ticket===selection.current){setMessage('取消结果：'+pretty(result));void refreshSelected(id);}
    }catch(error){if(ticket===selection.current)setMessage('取消失败：'+errorText(error));}
  }
  async function fakePlayback(partial:boolean){
    if(!robotId||!fakeTurnId||busy)return;
    const id=robotId,ticket=selection.current;
    setBusy(true);
    try{
      const turn=await api<Turn>(robotPath(id)+'/turns/'+encodeURIComponent(fakeTurnId));
      const generated=turn.assistant_generated_text??'';
      const result=await api<unknown>(robotPath(id)+'/turns/'+encodeURIComponent(fakeTurnId)+'/playback','POST',
        partial?{
          published_text:generated,
          spoken_text:generated.slice(0,Math.max(1,Math.floor(generated.length/2))),
          evidence_status:'simulated_partial',playback_state:'playing',
        }:{
          published_text:null,spoken_text:null,evidence_status:'unknown',playback_state:'unknown',
        });
      if(ticket===selection.current){
        setMessage('Fake 播放证据（不代表真机）：'+pretty(result));await refreshSelected(id);
      }
    }catch(error){if(ticket===selection.current)setMessage('Fake 播放测试失败：'+errorText(error));}
    finally{setBusy(false);}
  }
  const choose=(id:string)=>{
    if(id===robotId)return;
    selection.current++;eventCursor.current=0;setRealtimeEvents([]);setLiveText('');
    setRobotId(id);setSession(null);setConversations([]);setHistory(null);setContext(null);
    setPcm(null);setCoordinator(null);setSocial(null);setRoutes([]);setFakeTurnId('');setMessage('');
  };
  return <div className="shell" ref={workbench}>
    <header><strong>ANNIE HARBOR <span>· Phase 4C</span></strong><span className="sub">多机器人 · 会话 · Realtime · 音频 · 诊断</span></header>
    <section className="status" aria-live="polite">
      <span className={health?.ready?'ok':'bad'}>{health?.ready?'Runtime 运行中':'Runtime 未连接'}</span>
      <span>PID {health?.pid??'—'}</span><span>{health?.robots.length??0} 个机器人</span>
      <span>目标：{robotId||'未选机器人'} / {currentRobot?.room_id??'未连接'} / {session?.chat_id??'未选会话'}</span>
      <button onClick={()=>void refreshHealth()}>刷新</button>
      <button className="danger" disabled={!robotId} onClick={()=>void cancelCurrent()}>取消当前机器人回复</button>
    </section>
    <nav className="navigation" aria-label="工作台区域">
      <a href="#conversation">会话与对话</a><a href="#audio">音频与嘴型</a>
      <a href="#pcm">Room / PCM</a>
      <a href="#developer" onClick={()=>setDeveloperOpen(true)}>开发者诊断</a>
      <a href="/phase-tests/phase4c.html">4C 验收说明</a>
    </nav>
    <div className="layout">
      <aside>
        <h2>机器人 / Room</h2>
        {health?.robots.map(robot=><button className={robotId===robot.robot_id?'selected':''} key={robot.robot_id} onClick={()=>choose(robot.robot_id)}>
          <b>{robot.robot_id}</b><small>{robot.room_id} · {robot.state} · {robot.realtime?.state??'未接入 Realtime'}</small>
        </button>)}
        <h2>会话</h2>
        {conversations.map(item=><button key={item.chat_id+'\u0000'+item.persona_id} className={session?.chat_id===item.chat_id&&session.persona_id===item.persona_id?'selected':''}
          disabled={busy} onClick={()=>void operation('session',{chat_id:item.chat_id,persona_id:item.persona_id,persona_revision:item.persona_revision??(session?.persona_id===item.persona_id?session.persona_revision:1)})}>
          <b>{item.chat_id}</b><small>{item.persona_id} · revision {item.persona_revision??'未记录'} · {item.turn_count??0} 轮</small>
        </button>)}
        {!conversations.length&&<p>没有已保存的历史会话。可在右侧创建 Chat。</p>}
      </aside>
      <main>
        <section className="panel" id="conversation">
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
          <div className="actions"><button disabled={!robotId} onClick={()=>void cancelCurrent()}>取消当前回合</button>
            <button disabled={busy} onClick={()=>void refreshSelected(robotId)}>刷新当前会话</button></div>
          {message&&<pre className="notice" role="status">{message}</pre>}
        </section>
        <section className="panel"><details><summary>当前有效上下文（最多 20 轮，完整历史见上方）</summary><pre>{pretty(context)}</pre></details></section>
        <section className="panel" id="audio"><h2>独立 TTS / PCM / 嘴型</h2>
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
          <RoomPlaybackTester key={robotId} route={selectedRoute} onCancel={()=>void cancelCurrent()}/>
          {realtimeState?.speech_output?.error&&<p role="alert" className="bad">TTS 错误：{realtimeState.speech_output.error}</p>}
          <p>未安装对齐模型或无设备回执时，嘴型与已播证据保持未就绪/unknown；不以浏览器试听模拟设备播放，也不重复合成。</p>
          <details><summary>音频、停止及嘴型事件</summary><pre>{pretty(realtimeEvents.filter(e=>e.kind.startsWith('tts.')||e.kind.startsWith('viseme.')))}</pre></details>
          <a href="/phase-tests/phase4b.html">Phase 4B 验收步骤</a>
        </section>
        <section className="panel" id="pcm"><h2>PCM / Room 输入诊断</h2>
          <p>六通道配置：{['main','tts','mic','visualdata','vision','sam'].map(channel=>
            <span className="channel" key={channel}>{channel}：{selectedRoute?.urls[channel]?'已配置':'未配置'}</span>)}</p>
          <p>mic 实际连接：{selectedRoute?.connected?'已连接':'未连接 / 未获证据'}，其余通道“已配置”不代表已连接。</p>
          <p>采用 Phase 2 MIC_AUDIO BinaryFrame 封包；页面退出或切机器人时释放本页麦克风。无法自动检测另一音源，请先人工确认。</p>
          <RoomInput key={robotId} robotId={robotId} route={selectedRoute}/>
          <details><summary>PCM / Room 原始诊断</summary><pre>{pretty({inputs:pcm,routes})}</pre></details>
          <a href="/phase-tests/phase2.html">进入 Phase 2 真实 Room / PCM 验收页</a>
        </section>
        <section className="panel" id="developer"><details open={developerOpen} onToggle={event=>setDeveloperOpen(event.currentTarget.open)}>
          <summary>开发者诊断 · Fake 操作 / Coordinator / SocialState</summary>
          <p>以下按钮仅操作当前机器人 Fake Runtime，不代表实际动作或真实硬件停机。</p>
          <div className="actions"><button disabled={busy} onClick={()=>void operation('start')}>启动 Fake 机器人</button>
            <button disabled={busy} onClick={()=>void operation('stop')}>停止 Fake 机器人</button>
            <button disabled={busy} className="danger" onClick={()=>void operation('fail')}>注入 Fake 故障</button></div>
          <button disabled={busy||!robotId} onClick={()=>void operation('turns',{text:'Fake debug probe'})}>发送独立 Fake 调试回合</button>
          <p>仅针对刚刚由本页创建的 Fake 回合（{fakeTurnId||'无'}）模拟播放证据；不修改真实 Realtime 回合。</p>
          <div className="actions">
            <button disabled={busy||!fakeTurnId} onClick={()=>void fakePlayback(true)}>Fake 部分播放</button>
            <button disabled={busy||!fakeTurnId} onClick={()=>void fakePlayback(false)}>Fake 恢复 unknown</button>
            <button disabled={busy||!robotId} onClick={()=>void operation('response-requests',{
              source:'phase4c-dev',dedupe_key:'phase4c-'+Date.now(),ttl_ms:1500,
            })}>Fake 事件响应请求（TTL 1.5s）</button>
          </div>
          <h3>TurnCoordinator</h3><pre>{pretty(coordinator)}</pre><h3>SocialState</h3><pre>{pretty(social)}</pre>
        </details></section>
      </main>
    </div>
  </div>;
}
