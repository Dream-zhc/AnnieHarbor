import {useCallback,useEffect,useMemo,useRef,useState,type ReactNode} from 'react';
import {gsap} from 'gsap';
import {useGSAP} from '@gsap/react';
import {api,robotPath,eventBelongsToSession,type Health,type Session,type RealtimeEvent,type RealtimeEvents,type PersonaProfile} from './api';
import {RealtimeMicrophone} from './microphone';
import {RoomDebugSpeaker} from './room-debug-speaker';
import {ActionPanel} from './action-panel';
import {ExternalEvents} from './external-events';
type RoomRoute={robot_id:string;room_id:string;mic_url:string;connected:boolean;accepted:number;frames_received:number;last_error:string|null;urls:Record<string,string>};

type Conversation={chat_id:string;persona_id:string;persona_revision?:number;turn_count?:number;updated_ms?:number};
type Turn={turn_id:string;user_text:string;assistant_generated_text?:string|null;assistant_published_text?:string|null;assistant_spoken_text?:string|null;status:string;cancelled?:boolean;playback_evidence_status?:string};
type History={turns:Turn[];robot_id:string;chat_id:string;persona_id:string};
type SocialState=Record<string,{value:unknown;source:string;expires_at_ms:number|null}>;
const conversationChecks=[
  {label:'改口 · 茶与咖啡',turns:['我喜欢喝茶。','不是茶，是咖啡。','我刚才更正喜欢喝什么？']},
  {label:'指代与承诺',turns:['我明天准备去看电影。','继续刚才的话题。','你刚才说过你答应了我什么吗？没有就直说。']},
  {label:'停止追问',turns:['今天有点累，不想讲原因。','别老问我，我不想继续聊了。','知道了。']},
];
const errorText=(value:unknown)=>value instanceof Error?value.message:String(value);
const pretty=(value:unknown)=>JSON.stringify(value,null,2);
const playbackLabel=(turn:Turn,ttsNotConfigured:boolean)=>{
  if(turn.cancelled||turn.status==='cancelled')return '已取消';
  if(turn.status==='failed'||turn.status==='discarded')return '回复未完成';
  if(turn.playback_evidence_status==='speaker_reported_drained')return '扬声器回执：已播完';
  if(turn.assistant_spoken_text)return '有播放片段，未确认播完';
  if(turn.assistant_generated_text)return ttsNotConfigured?'文字回复（当前未启用 TTS）':'文本已生成，播放无回执';
  return turn.status==='running'?'回复中':'暂无回复';
};
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
  const [developerOpen,setDeveloperOpen]=useState(false);
  const [historyOpen,setHistoryOpen]=useState(false);
  const [actionOpen,setActionOpen]=useState(false);
  const [micLevel,setMicLevel]=useState<number|null>(null);
  const [browserBuffer,setBrowserBuffer]=useState<number|null>(null);
  const [health,setHealth]=useState<Health|null>(null);
  const [robotId,setRobotId]=useState('');
  const [session,setSession]=useState<Session|null>(null);
  const [conversations,setConversations]=useState<Conversation[]>([]);
  const [history,setHistory]=useState<History|null>(null);
  const [context,setContext]=useState<unknown>(null);
  const [profiles,setProfiles]=useState<PersonaProfile[]>([]);
  const [pcm,setPcm]=useState<unknown>(null);
  const [routes,setRoutes]=useState<RoomRoute[]>([]);
  const [coordinator,setCoordinator]=useState<unknown>(null);
  const [social,setSocial]=useState<SocialState>({});
  const [settingPreference,setSettingPreference]=useState(false);
  const [checkIndex,setCheckIndex]=useState(0);
  const [checkStep,setCheckStep]=useState(0);
  const [realtimeEvents,setRealtimeEvents]=useState<RealtimeEvent[]>([]);
  const [liveTurn,setLiveTurn]=useState<{turnId:string;userText:string}|null>(null);
  const [liveText,setLiveText]=useState('');
  const [previewTranscript,setPreviewTranscript]=useState('');
  const [finalTranscript,setFinalTranscript]=useState<{turnId:string;text:string}|null>(null);
  const [ambient,setAmbient]=useState<string[]>([]);
  const [lastFirstText,setLastFirstText]=useState<number|null>(null);
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
  const timeline=realtime?.last_turn_timeline;
  const durations=timeline?.durations;
  const percentiles=realtime?.latency_percentiles?.endpoint_to_speaker_start_ms;
  const milliseconds=(value:number|null|undefined)=>value==null?'未测':value+' ms';
  const playbackEvent=[...realtimeEvents].reverse().find(item=>
    (item.kind==='playback.hold_requested'||item.kind==='playback.resume_requested')&&item.turn_id);
  const playbackCommand=playbackEvent?.turn_id?{
    seq:playbackEvent.seq,turnId:playbackEvent.turn_id,
    kind:playbackEvent.kind==='playback.hold_requested'?'hold' as const:'resume' as const,
  }:undefined;
  const restored=realtime?.restored_context;
  const restoredForSession=restored&&session&&restored.robot_id===session.robot_id&&restored.chat_id===session.chat_id&&restored.persona_id===session.persona_id&&restored.session_epoch===session.session_epoch&&restored.generation===session.generation?restored:null;
  const persona=realtime?.persona_snapshot;
  const personaForSession=persona&&session&&persona.robot_id===session.robot_id&&persona.chat_id===session.chat_id&&persona.persona_id===session.persona_id&&persona.session_epoch===session.session_epoch&&persona.generation===session.generation&&persona.provider_session_id===realtime?.provider_session_id?persona:null;
  const savedPreference=social.question_pressure?.source==='user_setting'&&['normal','low','none'].includes(String(social.question_pressure.value))?String(social.question_pressure.value):'normal';
  const scenarioResults=useMemo(()=>{
    const prompts=conversationChecks[checkIndex].turns;
    const matching=new Map<string,Turn>();
    for(const turn of history?.turns??[]){
      if(prompts.includes(turn.user_text.trim()))matching.set(turn.user_text.trim(),turn);
    }
    return prompts.map(prompt=>matching.get(prompt));
  },[history,checkIndex]);

  const refreshHealth=useCallback(async()=>{
    try{
      const result=await api<Health>('/health','GET',undefined,undefined,[503]);
      setHealth(result);
      setRobotId(old=>result.robots.some(item=>item.robot_id===old)?old:result.robots[0]?.robot_id??'');
    }catch(error){setHealth(null);setNotice('Runtime 无法连接：'+errorText(error));}
  },[]);
  useEffect(()=>{
    void refreshHealth();
    const timer=window.setInterval(()=>void refreshHealth(),6000);
    return()=>window.clearInterval(timer);
  },[refreshHealth]);
  useEffect(()=>{void api<PersonaProfile[]>('/api/personas').then(setProfiles).catch(error=>setNotice('Persona 列表读取失败：'+errorText(error)));},[]);

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
        setHistory(null);setContext(null);setCoordinator(null);setSocial({});
      }
      setSession(selected);
      const [list,turns,rooms,socialState]=await Promise.all([
        api<Conversation[]>(base+'/conversations'),
        api<History>(base+'/conversations/'+encodeURIComponent(selected.chat_id)+'?persona_id='+encodeURIComponent(selected.persona_id)),
        api<RoomRoute[]>('/api/phase2/rooms'),
        api<SocialState>(base+'/social-state')
      ]);
      if(ticket!==selection.current)return;
      setConversations(list);setHistory(turns);setRoutes(rooms);setSocial(socialState);
    }catch(error){if(ticket===selection.current)setNotice('当前机器人数据读取失败：'+errorText(error));}
  },[]);
  useEffect(()=>{void refreshSelected(robotId);return()=>{selection.current++;};},[robotId,refreshSelected]);
  useEffect(()=>{
    if(!developerOpen||!robotId)return;
    let active=true;
    const base=robotPath(robotId);
    void Promise.all([
      api<unknown>(base+'/context'),api<unknown>(base+'/inputs'),
      api<unknown>(base+'/coordinator'),api<SocialState>(base+'/social-state')
    ]).then(([recent,inputs,coord,socialState])=>{
      if(active){setContext(recent);setPcm(inputs);setCoordinator(coord);setSocial(socialState);}
    }).catch(error=>{if(active)setNotice('诊断读取失败：'+errorText(error));});
    return()=>{active=false;};
  },[developerOpen,robotId,session?.generation]);
  useEffect(()=>{
    setRealtimeEvents([]);setLiveTurn(null);setLiveText('');setPreviewTranscript('');setFinalTranscript(null);setAmbient([]);setLastFirstText(null);eventCursor.current=0;
    followLatest.current=true;
    setCheckStep(0);
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation]);
  useEffect(()=>{
    if(!robotId||!session||!realtime)return;
    let active=true;
    const source=new EventSource(robotPath(robotId)+'/realtime/stream?after='+eventCursor.current);
    source.onmessage=message=>{
      if(!active)return;
      try{
        const result=JSON.parse(message.data) as RealtimeEvents;
        const events=result.events.filter(event=>event.seq>eventCursor.current);
        eventCursor.current=Math.max(eventCursor.current,result.latest);
        if(result.gap)setNotice('Realtime 事件缓冲出现缺口；完整会话以 Ledger 为准。');
        const scoped=events.filter(event=>eventBelongsToSession(event,session));
        if(scoped.length){
          setRealtimeEvents(prev=>[...prev,...scoped].slice(-100));
          for(const item of scoped){
            if(item.kind==='turn.started'||item.kind==='session.replaced'){
              setLiveText('');setPreviewTranscript('');setFinalTranscript(null);
              setLiveTurn(item.kind==='turn.started'&&item.turn_id
                ?{turnId:item.turn_id,userText:item.user_text??''}:null);
            }
            if(item.kind==='transcript.delta')setPreviewTranscript((item.stash??'')+(item.text??''));
            if(item.kind==='transcript.ambient'&&item.text?.trim()){
              setAmbient(previous=>[...previous,item.text!.trim()].slice(-5));
            }
            if(item.kind==='transcript.done'){
              setPreviewTranscript('');
              if(item.text&&item.turn_id){
                setFinalTranscript({turnId:item.turn_id,text:item.text});
                void refreshSelected(robotId);
              }
            }
            if(item.kind==='text.delta'||item.kind==='text.done')setLiveText(item.text??'');
            if(item.kind==='session.error')setNotice('Realtime：'+(item.error??'发生错误'));
            if(item.kind==='session.ready')setNotice(previous=>
              previous.startsWith('Realtime：')||previous.startsWith('Realtime 实时推送中断')
                ?'Realtime 已恢复连接（历史错误已解除）':previous);
            if(item.kind==='turn.done'){
              // Retain the final event until the Ledger read contains this turn.
              // Clearing immediately creates a disappearing/duplicate reply.
              if(typeof item.first_text_ms==='number')setLastFirstText(item.first_text_ms);
              setLiveText(item.text??'');void refreshSelected(robotId);
            }
          }
        }
      }catch(error){setNotice('Realtime 事件解析失败：'+errorText(error));}
    };
    source.onerror=()=>{if(active)setNotice('Realtime 实时推送中断；浏览器正在自动重连。');};
    return()=>{active=false;source.close();};
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation,!!realtime,refreshSelected]);
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
    const timer=window.setInterval(()=>void poll(),6000);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId]);
  useEffect(()=>{
    if(followLatest.current&&scrollPanel.current)
      scrollPanel.current.scrollTop=scrollPanel.current.scrollHeight;
  },[history?.turns.length,liveText,robotId,session?.chat_id]);
  useEffect(()=>{
    if(!createOpen&&!deleteOpen&&!developerOpen)return;
    const onEscape=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){setCreateOpen(false);setDeleteOpen(false);setDeveloperOpen(false);}
    };
    window.addEventListener('keydown',onEscape);
    return()=>window.removeEventListener('keydown',onEscape);
  },[createOpen,deleteOpen,developerOpen]);

  function choose(id:string){
    if(id===robotId)return;
    selection.current++;eventCursor.current=0;followLatest.current=true;displayedSession.current='';
    setRobotId(id);setSession(null);setConversations([]);setHistory(null);setContext(null);
    setPcm(null);setRoutes([]);setCoordinator(null);setSocial({});
    setMicLevel(null);setBrowserBuffer(null);
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
  async function setQuestionPressure(value:'normal'|'low'|'none'){
    if(!session||!robotId||settingPreference||pending||!realtime?.ready)return;
    const id=robotId,ticket=selection.current;
    setSettingPreference(true);setNotice('');
    try{
      await api(robotPath(id)+'/social-state','PUT',{
        fields:{question_pressure:value},source:'user_setting',generation:session.generation
      });
      if(ticket===selection.current){
        await refreshSelected(id);
        await refreshHealth();
        setNotice('追问偏好已保存，Realtime 正在重建 Session；以当前 Persona 快照为准。');
      }
    }catch(error){if(ticket===selection.current)setNotice('偏好设置失败：'+errorText(error));}
    finally{setSettingPreference(false);}
  }
  function openCreate(){
    setDraftChat('');setDraftPersona(session?.persona_id??selectedRobot?.persona_id??'annie');
    setDraftRevision(session?.persona_revision??1);setCreateOpen(true);
  }
  const voiceMode=!realtime?.ready?'未连接':
    ['speaking','streaming','running'].includes(speech?.state??'')?'说话':
    previewTranscript?'倾听':liveTurn||liveText?'思考':'空闲';
  const micDbfs=micLevel===null?'未开启':micLevel<=0?'-∞ dBFS':
    (20*Math.log10(micLevel/100)).toFixed(1)+' dBFS';

  return <div className="app-shell cockpit-shell" ref={root}>
    <header className="topbar cockpit-topbar">
      <div className="cockpit-brand"><span className="brand-mark" aria-hidden="true">A<span>·</span></span><div><strong>小安妮</strong><small>实时对讲</small></div></div>
      <div className="top-robot-switch" role="group" aria-label="切换机器人">
        {health?.robots.map(robot=><button key={robot.robot_id} type="button"
          className={'robot-pill '+(robotId===robot.robot_id?'selected':'')}
          aria-pressed={robotId===robot.robot_id} onClick={()=>choose(robot.robot_id)}>
          {robot.robot_id}</button>)}
        {!health?.robots.length&&<span className="empty-copy">无可用机器人</span>}
      </div>
      <div className="topbar-right">
        <span className={'top-health '+(health?.ready?'online':'offline')}><i/>{health?.ready?'在线':'未就绪'}</span>
        <button className="quiet-button" type="button" onClick={()=>setActionOpen(true)} disabled={!robotId}>动作</button>
        <button className="quiet-button developer-trigger" type="button" onClick={()=>setDeveloperOpen(true)}
          aria-haspopup="dialog" aria-expanded={developerOpen}>开发者</button>
      </div>
    </header>
    {notice&&<div className="notice-bar" role="status"><span>{notice}</span><button onClick={()=>setNotice('')} aria-label="关闭提示">×</button></div>}
    <main className="workspace cockpit-workspace">
      <div className={'chat-layout cockpit-layout '+(historyOpen?'history-open':'history-closed')}>
        <div className="cockpit-stage">
          <aside className="chat-sidebar">
            <div className="sidebar-top">
              {historyOpen&&<h2>历史会话</h2>}
              <button type="button" className="history-toggle" onClick={()=>setHistoryOpen(open=>!open)}
                aria-label={historyOpen?'收起历史会话':'展开历史会话'} aria-expanded={historyOpen}
                title={historyOpen?'收起历史':'展开历史'}>{historyOpen?'‹':'☰'}</button>
            </div>
            {historyOpen&&<><button className="new-chat" onClick={openCreate} disabled={!robotId}>＋ 新建会话</button>
            <div className="chat-list">{conversations.map(item=>
              <button key={item.chat_id+'\u0000'+item.persona_id} disabled={pending} className={'chat-list-item '+(session?.chat_id===item.chat_id&&session.persona_id===item.persona_id?'selected':'')} onClick={()=>void switchSession(item)}>
                <span className="chat-item-mark">◫</span><span className="chat-item-body"><strong>{item.chat_id}</strong><small>{item.persona_id} · {item.turn_count??0} 轮</small></span>
              </button>)}
              {!conversations.length&&<p className="empty-copy">还没有保存的对话，点击「＋」创建。</p>}
            </div></>}
          </aside>
          <section className="conversation" aria-label="正式对话工作台">
            <div className="conversation-header">
              <div><span className="eyebrow">对话</span><h1>{session?.chat_id??'选择会话'}</h1><p>{session?session.persona_id+' · '+(route?.room_id??selectedRobot?.room_id??session.room_id):'等待会话连接'}</p></div>
              <div className="conversation-tools">
                {!historyOpen&&<button className="quiet-button" onClick={openCreate} disabled={!robotId}>＋ 新会话</button>}
                <button className="quiet-button" onClick={()=>setHistoryOpen(true)} disabled={!robotId}>历史</button>
                <button className="quiet-button danger-text" onClick={()=>setDeleteOpen(true)} disabled={!session||pending}>删除</button>
              </div>
            </div>
            <div className="messages" ref={scrollPanel} aria-label="完整历史" onScroll={e=>{
              const node=e.currentTarget;followLatest.current=node.scrollHeight-node.scrollTop-node.clientHeight<90;
            }}>
              {!history&&<div className="message-placeholder">正在读取会话历史…</div>}
              {history?.turns.length===0&&!liveText&&<div className="message-placeholder"><strong>新对话，从这里开始。</strong><span>发送文字，或使用机器人 Room 接入的音频与小安妮交互。</span></div>}
              {history?.turns.map(turn=>{
                const streaming=liveTurn?.turnId===turn.turn_id&&turn.status==='running';
                const reply=streaming?(liveText||turn.assistant_generated_text):
                  (turn.assistant_spoken_text||turn.assistant_generated_text);
                const user=turn.user_text||(
                  finalTranscript?.turnId===turn.turn_id?finalTranscript.text:
                    liveTurn?.turnId===turn.turn_id?previewTranscript||liveTurn.userText:''
                );
                return <div className="exchange" key={turn.turn_id}>
                  <div className="message-row me"><div className="message-content"><span className="message-author">你</span><div className="bubble me-bubble">{user||'语音转写中…'}</div></div></div>
                  {(reply||turn.status!=='running')&&<div className="message-row agent"><span className="agent-avatar">A</span><div className="message-content">
                    <span className="message-author">小安妮</span>
                    {reply&&<div className="bubble agent-bubble">{reply}</div>}
                    <span className="message-foot">{streaming?'正在生成':playbackLabel(turn,speech?.state==='not_configured')}</span>
                  </div></div>}
                </div>;
              })}
              {liveTurn&&!history?.turns.some(turn=>turn.turn_id===liveTurn.turnId)&&<div className="exchange" aria-live="polite">
                <div className="message-row me"><div className="message-content"><span className="message-author">你</span><div className="bubble me-bubble">{finalTranscript?.turnId===liveTurn.turnId?finalTranscript.text:previewTranscript||liveTurn.userText||'语音转写中…'}</div></div></div>
                {liveText&&<div className="message-row agent"><span className="agent-avatar">A</span><div className="message-content"><span className="message-author">小安妮</span><div className="bubble agent-bubble">{liveText}</div><span className="message-foot">实时回复，等待入账</span></div></div>}
              </div>}
              {previewTranscript&&!liveTurn&&<div className="message-row me" aria-live="polite"><div className="message-content"><span className="message-author">你 · 语音转写中</span><div className="bubble me-bubble">{previewTranscript}</div></div></div>}
              {finalTranscript&&!liveTurn&&!history?.turns.some(turn=>turn.turn_id===finalTranscript.turnId)&&<div className="message-row me" aria-live="polite"><div className="message-content"><span className="message-author">你 · 最终转写</span><div className="bubble me-bubble">{finalTranscript.text}</div></div></div>}
            </div>
            <div className="composer">
              <div className="composer-status"><span className={'status-dot '+(realtime?.ready?'dot-good':'dot-warn')}/>{realtime?.ready?'Realtime 已连接':'Realtime 未就绪'}<span>·</span>语音输入来自所属 Room</div>
              <RealtimeMicrophone key={robotId+'\u0000'+(route?.room_id??'')} route={realtime?.ready?route:undefined} onLevel={setMicLevel}/>
              <form onSubmit={e=>{e.preventDefault();void sendText();}}>
                <label className="sr-only" htmlFor="message-input">输入消息</label>
                <textarea id="message-input" value={text} maxLength={2000} rows={2} onChange={e=>setText(e.target.value)}
                  onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void sendText();}}}
                  placeholder="给小安妮发送消息…（Enter 发送，Shift + Enter 换行）" disabled={!robotId}/>
                <div className="composer-actions"><span>Enter 发送 · Shift + Enter 换行</span><div>
                  <button type="button" className="stop-button interrupt-button" onClick={()=>void cancelCurrent()} disabled={!robotId}>■ 立即打断</button>
                  <button className="primary-button" type="submit" disabled={!text.trim()||pending||!realtime?.ready}>发送 ↗</button>
                </div></div>
              </form>
            </div>
          </section>
          </div>
          <aside className="inspector cockpit-hud" aria-label="声学与延迟仪表盘">
            <section className="hud-group hud-duplex" aria-label="全双工状态">
              <span className="eyebrow">全双工状态</span>
              <div className="duplex-mode" role="status"><span className={'status-dot '+(realtime?.ready?'dot-good':'dot-warn')}/>{voiceMode}</div>
              <p>{realtime?.ready?'Room '+(selectedRobot?.room_id??'—')+' · '+stateLabel(realtime?.state):'等待 Realtime 连接'}</p>
              {realtime?.error&&<p className="inline-error" role="alert">{realtime.error}</p>}
              {realtime?.llm_error&&<p className="inline-error" role="alert">{realtime.llm_error}</p>}
            </section>
            <section className="hud-group" aria-label="声学电平与播放缓冲">
              <h2>声学与缓冲</h2>
              <div className="hud-reading"><span>浏览器 Mic</span><strong>{micDbfs}</strong></div>
              <div className="mic-meter" role="meter" aria-label="浏览器麦克风电平"
                aria-valuemin={0} aria-valuemax={100} aria-valuenow={micLevel??0}>
                <span style={{width:(micLevel??0)+'%'}}/></div>
              <div className="hud-reading"><span>网页试听缓冲</span><strong>{browserBuffer===null?'未开启':browserBuffer+' ms'}</strong></div>
              <small>电平仅来自已授权的浏览器麦克风；未提供机器人端电平时不代填。</small>
            </section>
            <section className="hud-group" aria-label="延迟瀑布图">
              <h2>时延阶段</h2>
              <div className="latency-waterfall">
                <div><span>起声 → ASR 最终</span><strong>{milliseconds(durations?.speech_to_asr_final_ms)}</strong></div>
                <div><span>停声 → ASR 最终</span><strong>{milliseconds(durations?.endpoint_to_asr_final_ms)}</strong></div>
                <div><span>LLM 请求 → 首字</span><strong>{milliseconds(durations?.llm_request_to_first_text_ms)}</strong></div>
                <div><span>TTS 文本提交 → 首 PCM</span><strong>{milliseconds(durations?.tts_submit_to_pcm_ms)}</strong></div>
                <div><span>Room 发布 → 真机起播回执</span><strong>{milliseconds(durations?.room_to_speaker_start_ms)}</strong></div>
                <div><span>停声 → 真机起播回执</span><strong>{milliseconds(durations?.endpoint_to_speaker_start_ms)}</strong></div>
              </div>
              <small>同一 turn_id 的服务端观测时间；起播回执含设备上报往返延迟，不等于 DAC 实际起播时间。</small>
              <div className="hud-reading"><span>停声→真机回执 P50 / P95</span>
                <strong>{percentiles?.samples?milliseconds(percentiles.p50_ms)+' / '+milliseconds(percentiles.p95_ms):'未测'}</strong></div>
              <small>本会话有真机起播回执的样本：{percentiles?.samples??0} 轮；样本不足时不代表稳定分位数。</small>
            </section>
            <section className="hud-group" aria-label="设备回执与耳机监听">
              <h2>设备回执与试听</h2>
              <div className="hud-reading"><span>合成</span><State value={speech?.state}/></div>
              <div className="hud-reading"><span>真机播放</span><strong>{speech?.playback_evidence==='unknown'?'未确认':speech?.playback_evidence??'未确认'}</strong></div>
              <div className="hud-reading"><span>真机打断控制</span><strong>{stateLabel(speech?.playback_control_state)}</strong></div>
              <div className="hud-reading"><span>嘴型</span><State value={speech?.viseme}/></div>
              {speech?.last_stop_reason&&<div className="hud-reading"><span>最近停播</span><strong>{speech.last_stop_reason}</strong></div>}
              <RoomDebugSpeaker key={robotId+'\u0000'+(route?.room_id??'')}
                url={route?.urls?.tts} roomId={route?.room_id}
                command={playbackCommand} onBuffer={setBrowserBuffer}
                available={!!speech&&speech.state!=='not_configured'}/>
              <small>网页监听不是实体扬声器回执；请使用耳机避免回声。</small>
              {speech?.error&&<p className="inline-error" role="alert">{speech.error}</p>}
            </section>
          {session&&<details className="inspector-group inspector-panel">
            <summary>Phase 8 · 外部事件</summary>
            <ExternalEvents robotId={robotId} session={session} events={realtimeEvents}/>
          </details>}
          </aside>
      </div>
    </main>
    {actionOpen&&session&&<ActionPanel key={session.robot_id}
      robotId={session.robot_id} onClose={()=>setActionOpen(false)}
      onFillText={setText}/>}
    {developerOpen&&<div className="developer-backdrop" role="presentation"
      onMouseDown={event=>{if(event.target===event.currentTarget)setDeveloperOpen(false);}}>
      <aside className="developer-drawer" role="dialog" aria-modal="true" aria-labelledby="developer-title">
        <div className="drawer-head"><div><span className="eyebrow">仅供开发调试</span><h2 id="developer-title">开发者诊断</h2></div>
          <button className="quiet-button" type="button" onClick={()=>setDeveloperOpen(false)} aria-label="关闭开发者诊断">关闭 ✕</button></div>
        <div className="drawer-content">
          <section className="inspector-group"><h3>当前语音链路</h3>
            <div className="info-line"><span>听觉 / ASR</span><strong>Qwen-Audio smart_turn</strong></div>
            <div className="info-line"><span>最终回答模型</span><strong>{realtime?.cascade_llm??'Realtime'}</strong></div>
            <div className="info-line"><span>LLM 状态</span><State value={realtime?.llm_state??'not_configured'}/></div>
            {previewTranscript&&<p>ASR 暂定：{previewTranscript}</p>}
            {finalTranscript?.text&&<p>ASR 最终：{finalTranscript.text}</p>}
            {ambient.length>0&&<p>smart_turn 忽略的输入：{ambient.join(' / ')}</p>}
            <small>网页调试扬声器可逆 Hold/Resume；真实设备需单独报告播放状态。</small>
          </section>
          <details className="inspector-group inspector-panel"><summary>Persona 与追问偏好 <span>{personaForSession?.label??session?.persona_id??'—'}</span></summary><div className="info-line"><span>Persona</span><strong>{session?.persona_id??'—'}</strong></div>
              <div className="info-line"><span>Revision</span><strong>{session?.persona_revision??'—'}</strong></div>
              <div className="info-line"><span>历史回合</span><strong>{history?.turns.length??'—'}</strong></div>
              <div className="info-line"><span>表达风格</span><strong>{personaForSession?.configured?personaForSession.label:'未配置预设或待就绪'}</strong></div>
              <label className="quality-setting">追问偏好
                <select aria-label="追问偏好" value={savedPreference} disabled={!session||!realtime?.ready||!!realtime?.response_id||pending||settingPreference} onChange={event=>void setQuestionPressure(event.target.value as 'normal'|'low'|'none')}>
                  <option value="normal">自然追问</option><option value="low">少追问</option><option value="none">不主动追问</option>
                </select>
              </label>
              <p className="quality-note">仅作用于当前机器人 / Chat / Persona。切换偏好将重建 Realtime Session，不改变其他会话。</p>
              <small>当前模型生效：{personaForSession?.question_pressure==='none'?'不主动追问':personaForSession?.question_pressure==='low'?'少追问':personaForSession?.question_pressure==='normal'?'自然追问':'等待 Session 就绪'}</small>
            </details>
            <details className="inspector-group inspector-panel"><summary>多轮对话复跑 <span>3 组</span></summary>
              <label className="quality-setting">场景
                <select aria-label="多轮复跑场景" value={checkIndex} onChange={event=>{setCheckIndex(Number(event.target.value));setCheckStep(0);}}>
                  {conversationChecks.map((item,index)=><option key={item.label} value={index}>{item.label}</option>)}
                </select>
              </label>
              <p className="quality-note">{checkStep+1} / {conversationChecks[checkIndex].turns.length} · 逐句手动发送，真实回复留在当前 Chat 历史；切 Chat / Persona 后从第一句复跑。</p>
              <p className="quality-prompt">{conversationChecks[checkIndex].turns[checkStep]}</p>
              <div className="quality-actions"><button className="quiet-button" disabled={!!text.trim()||pending||!session} onClick={()=>setText(conversationChecks[checkIndex].turns[checkStep])}>填入输入框</button>
                <button className="quiet-button" disabled={checkStep===0} onClick={()=>setCheckStep(step=>Math.max(0,step-1))}>上一句</button>
                <button className="quiet-button" disabled={checkStep===conversationChecks[checkIndex].turns.length-1} onClick={()=>setCheckStep(step=>Math.min(conversationChecks[checkIndex].turns.length-1,step+1))}>下一句</button></div>
              <details><summary>本 Chat 的实际回复（不自动评分）</summary>
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
            <details className="inspector-group inspector-panel"><summary>连接详情</summary><div className="info-line"><span>Room 麦克风</span><State value={route?.connected?'connected':'not_connected'} good={route?.connected}/></div>
              <div className="info-line"><span>本次 Realtime 已发送</span><strong>{realtime?.session_frames_sent??0} 帧</strong></div>
              <div className="info-line"><span>模型检测语音开始</span><strong>{realtime?.speech_started_count??0} 次</strong></div>
              <div className="info-line"><span>模型完成转写</span><strong>{realtime?.transcripts_completed??0} 次</strong></div>
              <div className="info-line"><span>判为无效轮次</span><strong>{realtime?.invalid_turns??0} 次</strong></div>
              {(realtime?.session_frames_sent??0)>100&&!(realtime?.speech_started_count)&&<p className="audio-guidance">PCM 已送给模型，但本次 Session 尚无语音开始事件。先看麦克风本地电平；有效电平仍不触发时，再查 Room 内容/Provider 的 smart_turn。帧数不代表语音已被理解。</p>}
              <div className="info-line"><span>Session</span><strong>{session?.generation??'—'}</strong></div>
            </details>
          <div className="developer-grid">
            <Block title="当前目标" sub="所有数据均属于左上角选中的机器人。"><div className="info-line"><span>Robot / Room</span><strong>{robotId} / {selectedRobot?.room_id??'—'}</strong></div><div className="info-line"><span>Chat / Persona</span><strong>{session?.chat_id??'—'} / {session?.persona_id??'—'}</strong></div><div className="info-line"><span>Session epoch / generation</span><strong>{session?.session_epoch??'—'} / {session?.generation??'—'}</strong></div></Block>
            <Block title="Persona 与社交偏好" sub="当前 Provider Session 初始化时采用的表达预设和追问偏好。其他 SocialState 不自动变成模型指令。">
              <div className="info-line"><span>当前表达预设</span><strong>{personaForSession?.configured?personaForSession.label:'未配置或待连接'}</strong></div>
              <div className="info-line"><span>实际生效偏好</span><strong>{personaForSession?.question_pressure??'待连接'}</strong></div>
              <details><summary>查看带来源的 SocialState</summary><pre>{pretty(social)}</pre></details>
            </Block>
            <Block title="实时与音频" sub="先看状态；必要时展开原始事件。"><div className="info-line"><span>Realtime</span><State value={realtime?.state} good={realtime?.ready}/></div><div className="info-line"><span>TTS</span><State value={speech?.state}/></div><div className="info-line"><span>播放证据</span><strong>{speech?.playback_evidence??'unknown'}</strong></div><div className="info-line"><span>嘴型</span><State value={speech?.viseme}/></div><details><summary>Realtime / Function Calling / TTS 事件</summary><pre>{pretty(realtimeEvents)}</pre></details></Block>
            <Block title="重连注入上下文" sub="仅显示当前 Provider Session 建立时实际注入的历史；不声称涵盖随后发生的实时对话。字符预算不是精确 token 数。">
              {restoredForSession?<><div className="info-line"><span>注入轮数 / 较旧省略</span><strong>{restoredForSession.turns.length} / {restoredForSession.omitted_older_turns}</strong></div><div className="info-line"><span>字符预算</span><strong>{restoredForSession.chars_used} / {restoredForSession.char_budget}</strong></div><details><summary>逐轮查看输入与播放证据</summary><pre>{pretty(restoredForSession.turns)}</pre></details></>:<p>当前 Session 尚无可确认的历史注入快照（未连接或正在重连）。</p>}
              <details><summary>查看最近 Ledger 上下文（不等同于模型输入）</summary><pre>{pretty(context)}</pre></details><details><summary>查看完整 Ledger</summary><pre>{pretty(history)}</pre></details>
            </Block>
            <Block title="调度与社交状态"><details><summary>TurnCoordinator</summary><pre>{pretty(coordinator)}</pre></details><details><summary>SocialState</summary><pre>{pretty(social)}</pre></details></Block>
            <Block title="Room / PCM"><details><summary>输入与通道诊断</summary><pre>{pretty({inputs:pcm,routes})}</pre></details></Block>
            <Block title="独立阶段测试" sub="以下链接打开单独 HTML，不属于正式业务操作。">
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
        </div>
      </aside>
    </div>}
    {createOpen&&<div className="modal-backdrop" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)setCreateOpen(false);}}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="create-title">
        <span className="eyebrow">NEW CONVERSATION</span><h2 id="create-title">创建 / 切换会话</h2><p>绑定到 {robotId} / {selectedRobot?.room_id}。切换不会删除已有历史。</p>
        <form onSubmit={e=>{e.preventDefault();void createSession();}}>
          <label>Chat 名称<input autoFocus value={draftChat} maxLength={128} onChange={e=>setDraftChat(e.target.value)} placeholder="例如：日常聊天"/></label>
          <label>Persona<input list="known-personas" value={draftPersona} maxLength={128} onChange={e=>setDraftPersona(e.target.value)} />
            <datalist id="known-personas">{profiles.map(profile=><option key={'profile:'+profile.persona_id} value={profile.persona_id} label={profile.label}/>)}{[...new Set([session?.persona_id,selectedRobot?.persona_id,...conversations.map(item=>item.persona_id)].filter((value):value is string=>!!value))].map(name=><option key={'existing:'+name} value={name}/>)}</datalist>
          </label>
          <div className="quality-actions" aria-label="Persona 起始预设">{profiles.map(profile=><button type="button" className="quiet-button" key={profile.persona_id} onClick={()=>setDraftPersona(profile.persona_id)}>{profile.label}</button>)}</div>
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
