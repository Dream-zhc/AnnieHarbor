import {useCallback,useEffect,useRef,useState,type ReactNode} from 'react';
import {gsap} from 'gsap';
import {useGSAP} from '@gsap/react';
import {api,robotPath,eventBelongsToSession,type Health,type Session,type RealtimeEvent,type RealtimeEvents} from './api';
type RoomRoute={robot_id:string;room_id:string;mic_url:string;connected:boolean;accepted:number;frames_received:number;last_error:string|null;urls:Record<string,string>};

type View='overview'|'chat'|'robots'|'developer';
type Conversation={chat_id:string;persona_id:string;persona_revision?:number;turn_count?:number;updated_ms?:number};
type Turn={turn_id:string;user_text:string;assistant_generated_text?:string|null;assistant_published_text?:string|null;assistant_spoken_text?:string|null;status:string;cancelled?:boolean;playback_evidence_status?:string};
type History={turns:Turn[];robot_id:string;chat_id:string;persona_id:string};
const errorText=(value:unknown)=>value instanceof Error?value.message:String(value);
const pretty=(value:unknown)=>JSON.stringify(value,null,2);
const stateLabel=(value:string|undefined)=>{
  if(!value)return '未接入';
  const names:Record<string,string>={
    connected:'已连接',ready:'已就绪',room_gateway_ready:'已就绪',ledger_ready:'已就绪',
    not_connected:'未接入',not_started:'未接入',not_configured:'未配置',
    configured:'已配置',unknown:'未确认（unknown）',idle:'空闲',
    disconnected:'连接断开',error:'异常',failed:'失败',running:'运行中',
    fake:'Fake（调试）',subscribed:'已订阅',loading:'加载中'
  };
  return names[value]??value;
};

function Stat({name,value,detail,kind}:{name:string;value:string|number;detail?:string;kind?:'good'|'warn'|'neutral'}){
  return <div className="stat"><span className="stat-name">{name}</span><strong className={kind==='good'?'ink-good':kind==='warn'?'ink-warn':''}>{value}</strong>{detail&&<small>{detail}</small>}</div>;
}
function State({value,good}:{value:string|undefined;good?:boolean}){
  return <span className={'state-pill '+(good?'state-good':!value||value==='unknown'||value.startsWith('not_')?'state-muted':'')}>{stateLabel(value)}</span>;
}
function Block({children,title,sub}:{children:ReactNode;title:string;sub?:string}){
  return <section className="surface"><div className="surface-heading"><div><h2>{title}</h2>{sub&&<p>{sub}</p>}</div></div>{children}</section>;
}

export function Workbench(){
  const root=useRef<HTMLDivElement>(null);
  useGSAP(()=>{
    const media=gsap.matchMedia();
    media.add('(prefers-reduced-motion: no-preference)',()=>{
      gsap.from('.workspace',{autoAlpha:0,y:8,duration:.22,ease:'power1.out',clearProps:'all'});
    });
    return ()=>media.revert();
  },{scope:root});
  const [view,setView]=useState<View>('chat');
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
  const [realtimeEvents,setRealtimeEvents]=useState<RealtimeEvent[]>([]);
  const [liveText,setLiveText]=useState('');
  const [notice,setNotice]=useState('');
  const [text,setText]=useState('');
  const [pending,setPending]=useState(false);
  const [createOpen,setCreateOpen]=useState(false);
  const [draftChat,setDraftChat]=useState('');
  const [draftPersona,setDraftPersona]=useState('');
  const [draftRevision,setDraftRevision]=useState(1);
  const [deleteOpen,setDeleteOpen]=useState(false);
  const selection=useRef(0);
  const displayedSession=useRef('');
  const eventCursor=useRef(0);
  const scrollPanel=useRef<HTMLDivElement>(null);
  const followLatest=useRef(true);
  const selectedRobot=health?.robots.find(item=>item.robot_id===robotId);
  const route=routes.find(item=>item.robot_id===robotId);
  const realtime=selectedRobot?.realtime;
  const speech=realtime?.speech_output;

  const refreshHealth=useCallback(async()=>{
    try{
      const result=await api<Health>('/health','GET',undefined,undefined,[503]);
      setHealth(result);
      setRobotId(old=>result.robots.some(item=>item.robot_id===old)?old:result.robots[0]?.robot_id??'');
    }catch(error){setHealth(null);setNotice('Runtime 无法连接：'+errorText(error));}
  },[]);
  useEffect(()=>{
    void refreshHealth();
    const timer=window.setInterval(()=>void refreshHealth(),3000);
    return()=>window.clearInterval(timer);
  },[refreshHealth]);

  const refreshSelected=useCallback(async(id:string)=>{
    const ticket=++selection.current;
    if(!id){displayedSession.current='';setSession(null);setHistory(null);setConversations([]);return;}
    try{
      const base=robotPath(id);
      const selected=await api<Session>(base+'/session');
      if(ticket!==selection.current)return;
      const identity=[selected.robot_id,selected.chat_id,selected.persona_id,selected.session_epoch,selected.generation].join('\u0000');
      if(displayedSession.current!==identity){
        displayedSession.current=identity;
        setHistory(null);setContext(null);setCoordinator(null);setSocial(null);
      }
      setSession(selected);
      const [list,turns,recent,inputs,rooms,coord,socialState]=await Promise.all([
        api<Conversation[]>(base+'/conversations'),
        api<History>(base+'/conversations/'+encodeURIComponent(selected.chat_id)+'?persona_id='+encodeURIComponent(selected.persona_id)),
        api<unknown>(base+'/context'),api<unknown>(base+'/inputs'),api<RoomRoute[]>('/api/phase2/rooms'),
        api<unknown>(base+'/coordinator'),api<unknown>(base+'/social-state')
      ]);
      if(ticket!==selection.current)return;
      setConversations(list);setHistory(turns);setContext(recent);
      setPcm(inputs);setRoutes(rooms);setCoordinator(coord);setSocial(socialState);
    }catch(error){if(ticket===selection.current)setNotice('当前机器人数据读取失败：'+errorText(error));}
  },[]);
  useEffect(()=>{void refreshSelected(robotId);return()=>{selection.current++;};},[robotId,refreshSelected]);
  useEffect(()=>{
    setRealtimeEvents([]);setLiveText('');eventCursor.current=0;
    followLatest.current=true;
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
        if(result.gap)setNotice('Realtime 事件缓冲出现缺口；完整会话以 Ledger 为准。');
        const events=result.events.filter(event=>eventBelongsToSession(event,session));
        if(events.length){
          setRealtimeEvents(prev=>[...prev,...events].slice(-100));
          for(const item of events){
            if(item.kind==='turn.started'||item.kind==='session.replaced')setLiveText('');
            if(item.kind==='text.delta'||item.kind==='text.done')setLiveText(item.text??'');
            if(item.kind==='session.error')setNotice('Realtime：'+(item.error??'发生错误'));
            if(item.kind==='turn.done'){setLiveText('');void refreshSelected(robotId);}
          }
        }
      }catch(error){if(active)setNotice('Realtime 事件读取失败：'+errorText(error));}
      finally{reading=false;}
    };
    void poll();
    const timer=window.setInterval(()=>void poll(),250);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation,refreshSelected]);
  useEffect(()=>{
    if(!robotId)return;
    let active=true;
    const poll=async()=>{
      try{
        const [inputs,rooms]=await Promise.all([
          api<unknown>(robotPath(robotId)+'/inputs'),api<RoomRoute[]>('/api/phase2/rooms')
        ]);
        if(active){setPcm(inputs);setRoutes(rooms);}
      }catch(error){if(active)setNotice('Room 状态读取失败：'+errorText(error));}
    };
    const timer=window.setInterval(()=>void poll(),2000);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId]);
  useEffect(()=>{
    if(view==='chat'&&followLatest.current&&scrollPanel.current)
      scrollPanel.current.scrollTop=scrollPanel.current.scrollHeight;
  },[history?.turns.length,liveText,view,robotId,session?.chat_id]);
  useEffect(()=>{
    if(!createOpen&&!deleteOpen)return;
    const onEscape=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){setCreateOpen(false);setDeleteOpen(false);}
    };
    window.addEventListener('keydown',onEscape);
    return()=>window.removeEventListener('keydown',onEscape);
  },[createOpen,deleteOpen]);

  function choose(id:string){
    if(id===robotId)return;
    selection.current++;eventCursor.current=0;followLatest.current=true;displayedSession.current='';
    setRobotId(id);setSession(null);setConversations([]);setHistory(null);setContext(null);
    setPcm(null);setRoutes([]);setCoordinator(null);setSocial(null);
    setLiveText('');setRealtimeEvents([]);setNotice('');setCreateOpen(false);setDeleteOpen(false);
  }
  async function switchSession(item:Conversation){
    if(!robotId||pending)return;
    const id=robotId,ticket=selection.current;
    setPending(true);setNotice('');
    try{
      await api(robotPath(id)+'/session','POST',{
        chat_id:item.chat_id,persona_id:item.persona_id,
        persona_revision:item.persona_revision??(session?.persona_id===item.persona_id?session.persona_revision:1)
      });
      if(ticket===selection.current){followLatest.current=true;await refreshSelected(id);}
    }catch(error){if(ticket===selection.current)setNotice('切换会话失败：'+errorText(error));}
    finally{setPending(false);}
  }
  async function createSession(){
    if(!robotId||pending||!draftChat.trim()||!draftPersona.trim()||!Number.isSafeInteger(draftRevision)||draftRevision<1)return;
    const id=robotId,ticket=selection.current;
    setPending(true);setNotice('');
    try{
      await api(robotPath(id)+'/session','POST',{
        chat_id:draftChat.trim(),persona_id:draftPersona.trim(),persona_revision:draftRevision
      });
      if(ticket===selection.current){
        followLatest.current=true;setCreateOpen(false);await refreshSelected(id);
      }
    }catch(error){if(ticket===selection.current)setNotice('创建会话失败：'+errorText(error));}
    finally{setPending(false);}
  }
  async function removeSession(){
    if(!session||pending)return;
    const id=robotId,ticket=selection.current;
    setPending(true);setNotice('');
    try{
      await api(robotPath(id)+'/conversations/'+encodeURIComponent(session.chat_id)+'?persona_id='+encodeURIComponent(session.persona_id),'DELETE');
      if(ticket===selection.current){setDeleteOpen(false);await refreshSelected(id);}
    }catch(error){if(ticket===selection.current)setNotice('删除会话失败：'+errorText(error));}
    finally{setPending(false);}
  }
  async function sendText(){
    if(!robotId||pending||!text.trim()||!realtime?.ready)return;
    const id=robotId,ticket=selection.current,content=text.trim();
    setPending(true);setNotice('');
    try{
      await api(robotPath(id)+'/realtime/text','POST',{text:content});
      if(ticket===selection.current){setText('');followLatest.current=true;}
    }catch(error){if(ticket===selection.current)setNotice('发送失败，文字已保留：'+errorText(error));}
    finally{setPending(false);}
  }
  async function cancelCurrent(){
    if(!robotId)return;
    const id=robotId,ticket=selection.current;
    try{
      await api(robotPath(id)+'/cancel','POST');
      if(ticket===selection.current){setNotice('已向当前机器人发送取消请求；设备实际停播以回执为准。');void refreshSelected(id);}
    }catch(error){if(ticket===selection.current)setNotice('取消请求失败：'+errorText(error));}
  }
  function openCreate(){
    setDraftChat('');setDraftPersona(session?.persona_id??selectedRobot?.persona_id??'annie');
    setDraftRevision(session?.persona_revision??1);setCreateOpen(true);
  }

  return <div className="app-shell" ref={root}>
    <aside className="rail" aria-label="全局导航">
      <div className="brand-mark" aria-label="Annie Harbor">A<span>·</span></div>
      <div className="rail-label">ANNIE / V2</div>
      <nav className="rail-nav">
        {([
          ['overview','▦','总览'],['chat','◫','对话'],['robots','◇','机器人'],['developer','⌁','开发者']
        ] as const).map(([id,mark,label])=>
          <button key={id} className={'rail-link '+(view===id?'is-active':'')} aria-current={view===id?'page':undefined}
            onClick={()=>setView(id)}><span aria-hidden="true" className="rail-glyph">{mark}</span><span>{label}</span></button>)}
      </nav>
      <div className="rail-foot"><span className={'status-dot '+(health?.ready?'dot-good':'dot-bad')}/><span>{health?.ready?'运行中':'未就绪'}</span></div>
    </aside>
    <div className="app-body">
      <header className="topbar">
        <div className="breadcrumb"><span>安妮港</span><span className="crumb-sep">/</span><strong>{view==='overview'?'运行总览':view==='chat'?'实时对话':view==='robots'?'机器人控制中心':'开发者观测'}</strong></div>
        <div className="topbar-right">{view!=='overview'&&<label className="top-robot-switch">
          <span>机器人</span>
          <select aria-label="当前机器人" value={robotId} onChange={event=>choose(event.target.value)} disabled={!health?.robots.length}>
            {health?.robots.map(robot=><option key={robot.robot_id} value={robot.robot_id}>{robot.robot_id} · {robot.room_id}</option>)}
          </select>
        </label>}
          <span className={'top-health '+(health?.ready?'online':'offline')}><i/>{health?.ready?'Runtime 在线':'Runtime 未就绪'}</span>
          <button className="quiet-button" title="刷新状态" onClick={()=>void refreshHealth()}>刷新</button>
        </div>
      </header>
      {notice&&<div className="notice-bar" role="status"><span>{notice}</span><button onClick={()=>setNotice('')} aria-label="关闭提示">×</button></div>}
      <div className="workspace" key={view}>
        {view==='overview'&&<>
          <div className="page-heading"><div><span className="eyebrow">SYSTEM OVERVIEW</span><h1>运行总览</h1><p>来自当前 Runtime 的真实状态；未接入能力不会显示为在线。</p></div><button className="soft-button" onClick={()=>void refreshHealth()}>更新状态</button></div>
          <div className="stats-grid overview-stats">
            <Stat name="Runtime" value={health?.ready?'运行中':'未就绪'} detail={health?.pid?'PID '+health.pid:'等待连接'} kind={health?.ready?'good':'warn'}/>
            <Stat name="机器人实例" value={health?.robots.length??'—'} detail="各自独占 Room"/>
          </div>
          <Block title="机器人运行状态" sub="选择一个机器人，进入对应会话和状态查看。">
            <div className="robot-grid">{health?.robots.map(robot=>
              <button className="robot-tile" key={robot.robot_id} onClick={()=>{choose(robot.robot_id);setView('chat');}}>
                <span className="robot-avatar">{robot.robot_id.slice(0,1).toUpperCase()}</span>
                <span><strong>{robot.robot_id}</strong><small>{robot.room_id} · {robot.state}</small></span>
                <State value={robot.realtime?.state} good={robot.realtime?.ready}/>
                <span className="tile-arrow">↗</span>
              </button>)}
              {!health?.robots.length&&<p className="empty-copy">没有可用机器人；检查 Runtime 和机器人配置。</p>}
            </div>
          </Block>
        </>}
        {view==='chat'&&<div className="chat-layout">
          <aside className="chat-sidebar">
            <div className="sidebar-top"><div><span className="eyebrow">WORKSPACE</span><h2>会话</h2></div><button className="new-chat" onClick={openCreate} disabled={!robotId} title="新建会话">＋</button></div>
            <div className="sidebar-divider"/><div className="sidebar-caption">历史会话 <span>{conversations.length}</span></div>
            <div className="chat-list">{conversations.map(item=>
              <button key={item.chat_id+'\u0000'+item.persona_id} disabled={pending} className={'chat-list-item '+(session?.chat_id===item.chat_id&&session.persona_id===item.persona_id?'selected':'')} onClick={()=>void switchSession(item)}>
                <span className="chat-item-mark">◫</span><span className="chat-item-body"><strong>{item.chat_id}</strong><small>{item.persona_id} · {item.turn_count??0} 轮</small></span>
              </button>)}
              {!conversations.length&&<p className="empty-copy">还没有保存的对话，点击「＋」创建。</p>}
            </div>
            <div className="sidebar-bottom"><div className="mini-status"><span className={'status-dot '+(realtime?.ready?'dot-good':'dot-warn')}/><div><strong>{stateLabel(realtime?.state)}</strong><small>Realtime · {selectedRobot?.room_id??'—'}</small></div></div></div>
          </aside>
          <section className="conversation" aria-label="正式对话工作台">
            <div className="conversation-header">
              <div><span className="eyebrow">LIVE CONVERSATION</span><h1>{session?.chat_id??'选择会话'}</h1><p>{session?session.persona_id+' · '+selectedRobot?.room_id:'等待会话连接'}</p></div>
              <div className="conversation-tools"><button className="quiet-button" onClick={()=>void refreshSelected(robotId)} disabled={!robotId||pending}>刷新</button>
                <button className="quiet-button danger-text" onClick={()=>setDeleteOpen(true)} disabled={!session||pending}>删除会话</button></div>
            </div>
            <div className="messages" ref={scrollPanel} aria-label="完整历史" onScroll={e=>{
              const node=e.currentTarget;followLatest.current=node.scrollHeight-node.scrollTop-node.clientHeight<90;
            }}>
              {!history&&<div className="message-placeholder">正在读取会话历史…</div>}
              {history?.turns.length===0&&!liveText&&<div className="message-placeholder"><strong>新对话，从这里开始。</strong><span>发送文字，或使用机器人 Room 接入的音频与小安妮交互。</span></div>}
              {history?.turns.map(turn=><div className="exchange" key={turn.turn_id}>
                <div className="message-row me"><div className="message-content"><span className="message-author">你</span><div className="bubble me-bubble">{turn.user_text}</div></div></div>
                <div className="message-row agent"><span className="agent-avatar">A</span><div className="message-content">
                  <span className="message-author">小安妮 <span>· {turn.cancelled?'已取消':turn.status}</span></span>
                  <div className="bubble agent-bubble">{turn.assistant_spoken_text||turn.assistant_generated_text||'尚无回复'}</div>
                  <span className="message-foot">播放证据：{turn.playback_evidence_status??'unknown'}</span>
                </div></div>
              </div>)}
              {liveText&&<div className="message-row agent" aria-live="polite"><span className="agent-avatar">A</span><div className="message-content"><span className="message-author">小安妮 · 生成中</span><div className="bubble agent-bubble">{liveText}</div></div></div>}
            </div>
            <div className="composer">
              <div className="composer-status"><span className={'status-dot '+(realtime?.ready?'dot-good':'dot-warn')}/>{realtime?.ready?'Realtime 已连接':'Realtime 未就绪'}<span>·</span>语音输入来自所属 Room</div>
              <form onSubmit={e=>{e.preventDefault();void sendText();}}>
                <label className="sr-only" htmlFor="message-input">输入消息</label>
                <textarea id="message-input" value={text} maxLength={2000} rows={2} onChange={e=>setText(e.target.value)}
                  onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void sendText();}}}
                  placeholder="给小安妮发送消息…（Enter 发送，Shift + Enter 换行）" disabled={!robotId}/>
                <div className="composer-actions"><span>文字 · 同一 Realtime 会话</span><div>
                  <button type="button" className="stop-button" onClick={()=>void cancelCurrent()} disabled={!robotId}>停止回复</button>
                  <button className="primary-button" type="submit" disabled={!text.trim()||pending||!realtime?.ready}>发送 ↗</button>
                </div></div>
              </form>
            </div>
          </section>
          <aside className="inspector" aria-label="当前机器人状态">
            <div className="inspector-title"><span className="eyebrow">CURRENT ROBOT</span><h2>实时状态</h2></div>
            <div className="inspector-robot"><span className="robot-avatar large">{robotId.slice(0,1).toUpperCase()||'A'}</span><div><strong>{robotId||'未选择'}</strong><small>{selectedRobot?.room_id??'—'}</small></div><State value={realtime?.state} good={realtime?.ready}/></div>
            <div className="inspector-group"><h3>会话状态</h3><div className="info-line"><span>Persona</span><strong>{session?.persona_id??'—'}</strong></div>
              <div className="info-line"><span>Revision</span><strong>{session?.persona_revision??'—'}</strong></div>
              <div className="info-line"><span>历史回合</span><strong>{history?.turns.length??'—'}</strong></div>
            </div>
            <div className="inspector-group"><h3>语音输出</h3><div className="info-line"><span>TTS</span><State value={speech?.state}/></div>
              <div className="info-line"><span>嘴型</span><State value={speech?.viseme}/></div>
              <div className="info-line"><span>设备播放</span><strong>{speech?.playback_evidence??'unknown'}</strong></div>
              <div className="info-line"><span>首音频</span><strong>{speech?.first_audio_ms==null?'未测':speech.first_audio_ms+' ms'}</strong></div>
              <div className="info-line"><span>首发布</span><strong>{speech?.first_publish_ms==null?'未测':speech.first_publish_ms+' ms'}</strong></div>
            </div>
            <div className="inspector-group"><h3>连接</h3><div className="info-line"><span>Room 麦克风</span><State value={route?.connected?'connected':'not_connected'} good={route?.connected}/></div>
              <div className="info-line"><span>Session</span><strong>{session?.generation??'—'}</strong></div>
            </div>
            {realtime?.error&&<p className="inline-error" role="alert">{realtime.error}</p>}
            {speech?.error&&<p className="inline-error" role="alert">{speech.error}</p>}
            <button className="inspector-link" onClick={()=>setView('developer')}>打开完整诊断 ↗</button>
          </aside>
        </div>}
        {view==='robots'&&<>
          <div className="page-heading"><div><span className="eyebrow">ROBOT OPERATIONS</span><h1>机器人控制中心</h1><p>查看真实 Room、语音输出与运行状态；测试音源和故障注入位于独立阶段页。</p></div></div>
          <div className="stats-grid">
            <Stat name="Realtime" value={stateLabel(realtime?.state)} kind={realtime?.ready?'good':'warn'}/>
            <Stat name="TTS" value={stateLabel(speech?.state)}/>
            <Stat name="嘴型" value={stateLabel(speech?.viseme)}/>
            <Stat name="设备播放" value={speech?.playback_evidence??'unknown'}/>
          </div>
          <Block title="当前 Room" sub="只展示现阶段真实接入的音频状态；其它通道留在开发者诊断。">
            <div className="info-line"><span>Room</span><strong>{selectedRobot?.room_id??'—'}</strong></div>
            <div className="info-line"><span>麦克风输入</span><State value={route?.connected?'connected':'not_connected'} good={route?.connected}/></div>
            <div className="info-line"><span>首音频 / 首发布</span><strong>{speech?.first_audio_ms==null?'未测':speech.first_audio_ms+' ms'} / {speech?.first_publish_ms==null?'未测':speech.first_publish_ms+' ms'}</strong></div>
            {speech?.error&&<p className="inline-error" role="alert">{speech.error}</p>}
          </Block>
        </>}
        {view==='developer'&&<>
          <div className="page-heading"><div><span className="eyebrow">OBSERVABILITY</span><h1>开发者观测</h1><p>同一 Runtime 的只读诊断。测试请求、Fake 控制和模拟回执放在阶段 HTML。</p></div><button className="soft-button" disabled={!robotId} onClick={()=>void refreshSelected(robotId)}>刷新诊断</button></div>
          <div className="developer-grid">
            <Block title="当前目标" sub="所有数据均属于左上角选中的机器人。"><div className="info-line"><span>Robot / Room</span><strong>{robotId} / {selectedRobot?.room_id??'—'}</strong></div><div className="info-line"><span>Chat / Persona</span><strong>{session?.chat_id??'—'} / {session?.persona_id??'—'}</strong></div><div className="info-line"><span>Session epoch / generation</span><strong>{session?.session_epoch??'—'} / {session?.generation??'—'}</strong></div></Block>
            <Block title="实时与音频" sub="先看状态；必要时展开原始事件。"><div className="info-line"><span>Realtime</span><State value={realtime?.state} good={realtime?.ready}/></div><div className="info-line"><span>TTS</span><State value={speech?.state}/></div><div className="info-line"><span>播放证据</span><strong>{speech?.playback_evidence??'unknown'}</strong></div><div className="info-line"><span>嘴型</span><State value={speech?.viseme}/></div><details><summary>Realtime / Function Calling / TTS 事件</summary><pre>{pretty(realtimeEvents)}</pre></details></Block>
            <Block title="当前有效上下文" sub="最近 20 轮与完整历史分开。"><details><summary>查看最近上下文</summary><pre>{pretty(context)}</pre></details><details><summary>查看完整 Ledger</summary><pre>{pretty(history)}</pre></details></Block>
            <Block title="调度与社交状态"><details><summary>TurnCoordinator</summary><pre>{pretty(coordinator)}</pre></details><details><summary>SocialState</summary><pre>{pretty(social)}</pre></details></Block>
            <Block title="Room / PCM"><details><summary>输入与通道诊断</summary><pre>{pretty({inputs:pcm,routes})}</pre></details></Block>
            <Block title="独立阶段测试" sub="以下链接打开单独 HTML，不属于正式业务操作。">
              <div className="test-links">
                <a href="/phase-tests/phase1.html">Phase 1 · Fake / 多头测试 ↗</a>
                <a href="/phase-tests/phase2.html">Phase 2 · 测试 PCM / 麦克风 ↗</a>
                <a href="/phase-tests/phase3.html">Phase 3 · Ledger / 模拟回执 ↗</a>
                <a href="/phase-tests/phase4.html">Phase 4 · Realtime 验收 ↗</a>
                <a href="/phase-tests/phase4b.html">Phase 4B · 浏览器扬声器 / 嘴型 ↗</a>
                <a href="/phase-tests/phase4c.html">Phase 4C · 工作台验收 ↗</a>
              </div>
            </Block>
          </div>
        </>}
      </div>
    </div>
    {createOpen&&<div className="modal-backdrop" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)setCreateOpen(false);}}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="create-title">
        <span className="eyebrow">NEW CONVERSATION</span><h2 id="create-title">创建 / 切换会话</h2><p>绑定到 {robotId} / {selectedRobot?.room_id}。切换不会删除已有历史。</p>
        <form onSubmit={e=>{e.preventDefault();void createSession();}}>
          <label>Chat 名称<input autoFocus value={draftChat} maxLength={128} onChange={e=>setDraftChat(e.target.value)} placeholder="例如：日常聊天"/></label>
          <label>Persona<input list="known-personas" value={draftPersona} maxLength={128} onChange={e=>setDraftPersona(e.target.value)} />
            <datalist id="known-personas">{[...new Set([session?.persona_id,selectedRobot?.persona_id,...conversations.map(item=>item.persona_id)].filter((value):value is string=>!!value))].map(name=><option key={name} value={name}/>)}</datalist>
          </label>
          <label>Persona revision<input type="number" min="1" step="1" value={draftRevision} onChange={e=>setDraftRevision(Number(e.target.value))}/></label>
          <div className="dialog-actions"><button type="button" className="quiet-button" onClick={()=>setCreateOpen(false)}>取消</button><button className="primary-button" disabled={pending||!draftChat.trim()||!draftPersona.trim()||draftRevision<1||!Number.isSafeInteger(draftRevision)}>确认</button></div>
        </form>
      </div>
    </div>}
    {deleteOpen&&session&&<div className="modal-backdrop" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)setDeleteOpen(false);}}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title">
        <span className="eyebrow danger-text">PERMANENT DELETE</span><h2 id="delete-title">删除当前会话历史？</h2>
        <p>仅删除 <strong>{session.robot_id} / {session.persona_id} / {session.chat_id}</strong> 的已保存历史，无法撤销；其它机器人不受影响。</p>
        <div className="dialog-actions"><button className="quiet-button" onClick={()=>setDeleteOpen(false)}>保留会话</button><button className="stop-button" disabled={pending} onClick={()=>void removeSession()}>确认删除</button></div>
      </div>
    </div>}
  </div>;
}
