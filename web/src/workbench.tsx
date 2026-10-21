import {useCallback,useEffect,useLayoutEffect,useMemo,useRef,useState} from 'react';
import {DeveloperDrawer,State} from './developer-drawer';
import {gsap} from 'gsap';
import {useGSAP} from '@gsap/react';
import {api,robotPath,eventBelongsToSession,type Health,type Session,type RealtimeEvent,type RealtimeEvents,type RealtimeDiagnostics,type PersonaProfile} from './api';
import {MicrophoneControls,RoomSpeakerPanel} from './audio-panels';
import {LatencyHud} from './latency-hud';
import {RoleLanguagePreference,SpeechExpressionPanel} from './speech-expression';
import {ActionPanel} from './action-panel';
import {ExternalEventsDialog} from './external-events';
import {MemoryPanel} from './memory-panel';
import {splitReplyBubbles} from './reply-bubbles';
import {RoomObservationsBar} from './room-observations';
import {decisionEntry,decisionSource,formatDecision,isProviderAmbient,parseDecision,type DecisionEntry} from './decision-log';
import {audioDevices,conflictMessages,handoverEventMessage,handoverMessage,headLabel} from './audio-devices';
import {faceNotice,volumeNotice} from './notices';
import {ParticleCanvas, type ColorPalette} from './particle-canvas';
import {CustomSelect} from './custom-select';
import {WakeIndicator} from './wake-indicator';
import {VoiceSettings} from './voice-settings';
import {AnnieStatePod} from './annie-state-pod';
import {isPodStatusEvent} from './annie-pod-refresh';

type RoomRoute={robot_id:string;room_id:string;mic_url:string;connected:boolean;accepted:number;frames_received:number;last_error:string|null;urls:Record<string,string>};
type Conversation={chat_id:string;persona_id:string;persona_revision?:number;turn_count?:number;updated_ms?:number};
type PersonaInstance={persona_id:string;display_name:string;created_ms:number;updated_ms:number};
type Turn={id:number;turn_id:string;user_text:string;turn_source?:'user'|'external';external_prompt?:string|null;assistant_generated_text?:string|null;assistant_published_text?:string|null;assistant_spoken_text?:string|null;status:string;cancelled?:boolean;playback_evidence_status?:string;created_ms?:number;updated_ms?:number};
type History={turns:Turn[];robot_id:string;chat_id:string;persona_id:string;has_more?:boolean;next_before_id?:number|null};
const HISTORY_PAGE_SIZE=80;
type SocialState=Record<string,{value:unknown;source:string;expires_at_ms:number|null}>;

const errorText=(value:unknown)=>value instanceof Error?value.message:String(value);

function formatTimestamp(ms?:number):string{
  if(!ms)return '';
  const d=new Date(ms);
  const now=new Date();
  const pad=(n:number)=>String(n).padStart(2,'0');
  const timeStr=`${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if(d.toDateString()===now.toDateString())return timeStr;
  const yesterday=new Date(now.getTime()-86400000);
  if(d.toDateString()===yesterday.toDateString())return `昨天 ${timeStr}`;
  return `${d.getMonth()+1}/${d.getDate()} ${timeStr}`;
}

const playbackLabel=(turn:Turn,ttsNotConfigured:boolean)=>{
  if(turn.cancelled||turn.status==='cancelled')return '已取消';
  if(turn.status==='failed'||turn.status==='discarded')return '未完成';
  if(turn.playback_evidence_status==='speaker_reported_drained')return '已播完';
  if(turn.assistant_spoken_text)return '播放中';
  if(turn.assistant_generated_text)return ttsNotConfigured?'文字回复':'已生成';
  return turn.status==='running'?'回复中':'';
};

type ActionRequest={
  robot_id:string;room_id?:string;turn_id:string;call_id?:string;
  kind:string;value:string;status:string;error?:string|null;updated_ms?:number;
};

const ACTION_SCRIPT_NAMES:Record<string,string>={
  agree_micro_nod:'附和小点头',listen_tilt:'倾听',listen_turn:'侧耳听',
  confused_tilt:'疑惑歪头',confused_up:'疑惑仰看',shy_retract:'害羞蜷缩',
  embarrass_left:'尴尬偏左',embarrass_right:'尴尬偏右',sniff_forward:'嗅闻寻源',
  sigh_droop:'叹气低头',yawn_stretch:'哈欠伸展',look_left:'看左边',
  look_right:'看右边',look_up:'往上看',micro_pitch:'轻抬下巴',
  micro_tilt:'轻歪右',micro_tilt_left:'轻歪左',micro_turn:'小转右',
  micro_turn_left:'小转左',eye_glance_left:'眼瞟左',eye_glance_right:'眼瞟右',
  eye_glance_up:'眼瞟上',eye_glance_down:'眼瞟下',eye_drift:'眼微漂移',
  glance_co_left:'眼颈协同左',glance_co_right:'眼颈协同右',shake_head:'摇头',
  tilt_head_left:'左歪头',cross_eyes:'斗鸡眼',think_tilt:'思考倾',
  think_curious:'思考复合',fast_double_blink:'连眨两下',wink_left:'左眼 Wink',
  wink_right:'右眼 Wink',surprise:'惊讶',smirk:'坏笑',proud:'得意',
  skeptical:'怀疑',shy:'害羞（脸部）',
  '喜':'喜悦','怒':'愤怒','哀':'悲伤','乐':'快乐','中':'中性',
};

const actionStatusLabel:Record<string,string>={
  awaiting_audio_anchor:'等待音频',awaiting_text_anchor:'等待目标词',
  call_observed:'工具调用待确认',reserved:'已预约',queued:'已排队',
  published_unknown:'云端已下发',send_unknown:'发送未确认',cancelled_before_send:'已取消',
  expired_no_pcm:'等待音频超时',expired_no_text_anchor:'目标词超时',
  rejected_late:'迟到丢弃',
};

function getTurnActions(
  turnId:string|undefined,
  actionRequests:ActionRequest[],
  realtimeEvents:RealtimeEvent[],
  diagnostics?:RealtimeDiagnostics|null
):ActionRequest[]{
  if(!turnId)return [];
  const map=new Map<string,ActionRequest>();
  for(const req of actionRequests){
    if(req.turn_id===turnId)map.set(req.call_id||req.value,req);
  }
  for(const event of realtimeEvents){
    if(event.turn_id===turnId){
      if(event.kind==='action.state'&&event.action_value){
        const key=event.call_id||event.action_value;
        const prev=map.get(key);
        map.set(key,{
          robot_id:event.robot_id,room_id:(event as unknown as {room_id?:string}).room_id??prev?.room_id??'',
          turn_id:event.turn_id,call_id:event.call_id??prev?.call_id??'',
          kind:event.action_kind??prev?.kind??'action',value:event.action_value,
          status:event.status??prev?.status??'call_observed',error:event.error??prev?.error??null,
        });
      }else if(event.kind==='tool.called'&&(event.name==='perform_action'||event.name==='set_expression')){
        try{
          const args=typeof event.arguments==='string'?JSON.parse(event.arguments):event.arguments;
          const isAct=event.name!=='set_expression';
          const val=isAct?args?.script_id:args?.expression;
          if(val&&!map.has(event.call_id||val)){
            map.set(event.call_id||val,{
              robot_id:event.robot_id,room_id:'',turn_id:event.turn_id,call_id:event.call_id??'',
              kind:isAct?'action':'expression',value:val,status:'call_observed',
            });
          }
        }catch{/* ignore json error */}
      }
    }
  }
  if(diagnostics?.tool_history){
    for(const tool of diagnostics.tool_history){
      if(tool.turn_id===turnId&&tool.kind==='tool.called'&&
        (tool.name==='perform_action'||tool.name==='set_expression')){
        try{
          const args=typeof tool.arguments==='string'?JSON.parse(tool.arguments):tool.arguments;
          const isAct=tool.name!=='set_expression';
          const val=isAct?args?.script_id:args?.expression;
          if(val&&!map.has(tool.call_id||val)){
            map.set(tool.call_id||val,{
              robot_id:tool.robot_id,room_id:'',turn_id:tool.turn_id,call_id:tool.call_id??'',
              kind:isAct?'action':'expression',value:val,status:'call_observed',
            });
          }
        }catch{/* ignore json error */}
      }
    }
  }
  return Array.from(map.values());
}

function DecisionItem({ entry, isExpanded }: { entry: DecisionEntry; isExpanded?: boolean }){
  const parsed = parseDecision(entry);
  return (
    <div className={`decision-item-container topic-${parsed.topicKind} outcome-${parsed.outcomeType} ${isExpanded ? 'is-expanded' : 'is-compact'}`}>
      {isExpanded && (
        <span className={`decision-pill topic-pill topic-${parsed.topicKind}`}>
          <span className="pill-emoji">{parsed.topicIcon}</span>
          <span className="pill-text">{parsed.topicLabel}</span>
        </span>
      )}

      <span className={`decision-pill outcome-pill outcome-${parsed.outcomeType}`}>
        {!isExpanded && <span className="pill-emoji">{parsed.topicIcon}</span>}
        <span className="outcome-title">{parsed.outcomeText}</span>
        {parsed.outcomeSub && isExpanded && <span className="outcome-detail">· {parsed.outcomeSub}</span>}
      </span>

      {entry.text && (
        <span className="decision-quote">
          <span className="quote-mark">“</span>
          <span className="quote-body">{entry.text}</span>
          <span className="quote-mark">”</span>
        </span>
      )}

      {parsed.metrics.length > 0 && (
        <div className="decision-metrics-wrap">
          {isExpanded ? (
            parsed.metrics.map((m, i) => (
              <span key={i} className="decision-metric-tag">{m}</span>
            ))
          ) : (
            <span className="decision-metric-tag">{parsed.metrics[parsed.metrics.length - 1]}</span>
          )}
        </div>
      )}
    </div>
  );
}

export function Workbench({ onSwitchMode }: { onSwitchMode?: (mode: 'cockpit' | 'workbench') => void } = {}){
  const root=useRef<HTMLDivElement>(null);
  useGSAP(()=>{
    const media=gsap.matchMedia();
    media.add('(prefers-reduced-motion: no-preference)',()=>{
      const tl=gsap.timeline({defaults:{ease:'power2.out'}});
      tl.from('.workspace',{autoAlpha:0,y:8,duration:.22,clearProps:'all'})
        .from('.cockpit-stage',{autoAlpha:0,x:-6,duration:.2,clearProps:'all'},'-=0.12')
        .from('.cockpit-hud',{autoAlpha:0,x:6,duration:.2,clearProps:'all'},'-=0.18')
        .from('.live-cockpit-card',{autoAlpha:0,y:4,stagger:0.03,duration:.18,clearProps:'all'},'-=0.1');
    });
    return ()=>media.revert();
  },{scope:root});

  const [theme,setTheme]=useState<'dark'|'light'>(()=>{
    return (typeof window!=='undefined'&&window.localStorage.getItem('annie_theme') as 'dark'|'light')||'dark';
  });
  useEffect(()=>{
    document.documentElement.setAttribute('data-theme',theme);
    try{window.localStorage.setItem('annie_theme',theme);}catch{}
  },[theme]);

  const [palette,setPalette]=useState<ColorPalette>(()=>{
    return (typeof window!=='undefined'&&window.localStorage.getItem('annie_palette') as ColorPalette)||'violet';
  });
  const [paletteOpen,setPaletteOpen]=useState(false);
  useEffect(()=>{
    document.documentElement.setAttribute('data-palette',palette);
    try{window.localStorage.setItem('annie_palette',palette);}catch{}
  },[palette]);

  const [developerOpen,setDeveloperOpen]=useState(false);
  const developerOpenRef=useRef(false);
  developerOpenRef.current=developerOpen;
  const [historyOpen,setHistoryOpen]=useState(false);
  const [memoryOpen,setMemoryOpen]=useState(false);
  const [actionOpen,setActionOpen]=useState(false);
  const [eventsOpen,setEventsOpen]=useState(false);
  const [preferenceOpen,setPreferenceOpen]=useState(false);
  const [micActive,setMicActive]=useState(false);
  const [auditionActive,setAuditionActive]=useState(false);
  const [health,setHealth]=useState<Health|null>(null);
  const [robotId,setRobotId]=useState('');
  const [session,setSession]=useState<Session|null>(null);
  const [conversations,setConversations]=useState<Conversation[]>([]);
  const [history,setHistory]=useState<History|null>(null);
  const [visibleHistoryCount,setVisibleHistoryCount]=useState(HISTORY_PAGE_SIZE);
  const [olderLoading,setOlderLoading]=useState(false);
  const [context,setContext]=useState<unknown>(null);
  const [profiles,setProfiles]=useState<PersonaProfile[]>([]);
  const [instances,setInstances]=useState<PersonaInstance[]>([]);
  const [pcm,setPcm]=useState<unknown>(null);
  const [routes,setRoutes]=useState<RoomRoute[]>([]);
  const [coordinator,setCoordinator]=useState<unknown>(null);
  const [social,setSocial]=useState<SocialState>({});
  const [settingPreference,setSettingPreference]=useState(false);
  const [realtimeEvents,setRealtimeEvents]=useState<RealtimeEvent[]>([]);
  const [podStatusRevision,setPodStatusRevision]=useState(0);
  const [actionRequests,setActionRequests]=useState<ActionRequest[]>([]);
  const [diagnostics,setDiagnostics]=useState<RealtimeDiagnostics|null>(null);
  const [liveTurn,setLiveTurn]=useState<{turnId:string;userText:string;source:'user'|'external'}|null>(null);
  const [liveText,setLiveText]=useState('');
  const [previewTranscript,setPreviewTranscript]=useState('');
  const [pendingAsr,setPendingAsr]=useState('');
  const [ambientPreview,setAmbientPreview]=useState('');
  const [asrStage,setAsrStage]=useState<'idle'|'speech'|'finalizing'>('idle');
  const [firstAsrDeltaMs,setFirstAsrDeltaMs]=useState<number|null>(null);
  const [finalTranscript,setFinalTranscript]=useState<{turnId:string;text:string}|null>(null);
  const [ambient,setAmbient]=useState<string[]>([]);
  const [decisions,setDecisions]=useState<DecisionEntry[]>([]);
  const [decisionPopoverOpen,setDecisionPopoverOpen]=useState(false);
  const decisionCardRef=useRef<HTMLDivElement>(null);

  useEffect(()=>{
    if(!decisionPopoverOpen)return;
    const handleOutsideClick=(e:MouseEvent)=>{
      if(decisionCardRef.current&&!decisionCardRef.current.contains(e.target as Node)){
        setDecisionPopoverOpen(false);
      }
    };
    window.addEventListener('mousedown',handleOutsideClick);
    return ()=>window.removeEventListener('mousedown',handleOutsideClick);
  },[decisionPopoverOpen]);
  const [lastFirstText,setLastFirstText]=useState<number|null>(null);
  const [systemError,setSystemError]=useState<string|null>(null);
  const [toast,setToast]=useState<{message:string;type:'error'|'info'}|null>(null);
  const toastTimer=useRef<number|null>(null);

  const notify=useCallback((message:string,type:'error'|'info'='error')=>{
    if(!message)return;
    if(type==='error'){
      setSystemError(message);
    }
    setToast({message,type});
    if(toastTimer.current)window.clearTimeout(toastTimer.current);
    toastTimer.current=window.setTimeout(()=>{
      setToast(null);
    },4000);
  },[]);

  const dismissError=useCallback(()=>{
    setSystemError(null);
    setToast(null);
    if(toastTimer.current)window.clearTimeout(toastTimer.current);
  },[]);

  const [text,setText]=useState('');
  const [pending,setPending]=useState(false);
  const [createOpen,setCreateOpen]=useState(false);
  const [draftChat,setDraftChat]=useState('');
  const [draftPersona,setDraftPersona]=useState('');
  const [draftName,setDraftName]=useState('');
  const [draftStyle,setDraftStyle]=useState('annie');
  const [draftRevision,setDraftRevision]=useState(1);
  const [deleteOpen,setDeleteOpen]=useState(false);

  const selection=useRef(0);
  const displayedSession=useRef('');
  const olderRequest=useRef(false);
  const eventCursor=useRef(0);
  const scrollPanel=useRef<HTMLDivElement>(null);
  const followLatest=useRef(true);
  const [showJump,setShowJump]=useState(false);

  const selectedRobot=health?.robots.find(item=>item.robot_id===robotId);
  // One microphone and one speaker per Room: Brain observes, the page warns.
  const roomDevices=audioDevices(pcm);
  const deviceWarnings=conflictMessages(roomDevices,selectedRobot?.room_id??session?.room_id);
  // Two boards may serve one Room (big head, obj doll); the later one takes over.
  const currentHead=roomDevices?.head?.current??null;
  const headVolume=roomDevices?.speakers.find(item=>item.head_instance===currentHead?.instance_id)?.volume;
  const handoverNotice=handoverMessage(roomDevices?.head);
  const route=routes.find(item=>item.robot_id===robotId);
  const realtime=selectedRobot?.realtime;
  const speech=realtime?.speech_output;
  const decisionModel=decisionSource(realtime?.decision);

  const playbackEvent=[...realtimeEvents].reverse().find(item=>
    (item.kind==='playback.hold_requested'||item.kind==='playback.resume_requested')&&item.turn_id);
  const playbackCommand=useMemo(()=>playbackEvent?.turn_id?{
    seq:playbackEvent.seq,turnId:playbackEvent.turn_id,
    kind:playbackEvent.kind==='playback.hold_requested'?'hold' as const:'resume' as const,
  }:undefined,[playbackEvent?.seq,playbackEvent?.turn_id,playbackEvent?.kind]);

  const restored=realtime?.restored_context;
  const restoredForSession=restored&&session&&restored.robot_id===session.robot_id&&restored.chat_id===session.chat_id&&restored.persona_id===session.persona_id&&restored.session_epoch===session.session_epoch&&restored.generation===session.generation?restored:null;
  const persona=realtime?.persona_snapshot;
  const personaForSession=persona&&session&&persona.robot_id===session.robot_id&&persona.chat_id===session.chat_id&&persona.persona_id===session.persona_id&&persona.session_epoch===session.session_epoch&&persona.generation===session.generation&&persona.provider_session_id===realtime?.provider_session_id?persona:null;
  const savedPreference=social.question_pressure?.source==='user_setting'&&['active','normal','low','none'].includes(String(social.question_pressure.value))?String(social.question_pressure.value):'normal';
  const lastVisibleTurn=history?.turns.at(-1);
  useEffect(()=>{
    if(!session||!robotId||!realtime?.ready||savedPreference!=='active'||
        !lastVisibleTurn||lastVisibleTurn.turn_source==='external'||
        lastVisibleTurn.status!=='completed'||!lastVisibleTurn.assistant_generated_text||
        !lastVisibleTurn.updated_ms)return;
    const {turn_id,updated_ms}=lastVisibleTurn;
    const robot=robotId,identity={
      chat_id:session.chat_id,persona_id:session.persona_id,
      session_epoch:session.session_epoch,generation:session.generation,
    };
    const heartbeat=()=>{
      if(document.visibilityState!=='visible'||!followLatest.current||
          Date.now()-updated_ms>60000)return;
      // A visible text bubble is NOT a speaker receipt; backend submits text only.
      void api(robotPath(robot)+'/turns/'+encodeURIComponent(turn_id)+'/viewed',
        'POST',identity).catch(()=>{});
    };
    heartbeat();
    const timer=window.setInterval(heartbeat,4000);
    return()=>window.clearInterval(timer);
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,
      session?.generation,realtime?.ready,savedPreference,
      lastVisibleTurn?.turn_id,lastVisibleTurn?.updated_ms,lastVisibleTurn?.status]);
  const savedStyle=social.persona_style?.source==='user_setting'&&profiles.some(item=>item.persona_id===social.persona_style.value)
    ?String(social.persona_style.value):profiles.some(item=>item.persona_id===session?.persona_id)
      ?session?.persona_id??'annie':'annie';
  const styleApplied=!!realtime?.ready&&!!personaForSession&&personaForSession.style_id===savedStyle;

  const refreshHealth=useCallback(async()=>{
    try{
      const result=await api<Health>('/health','GET',undefined,undefined,[503]);
      setHealth(result);
      setRobotId(old=>result.robots.some(item=>item.robot_id===old)?old:result.robots[0]?.robot_id??'');
    }catch(error){setHealth(null);notify('Runtime 无法连接：'+errorText(error),'error');}
  },[notify]);

  useEffect(()=>{
    window.scrollTo(0,0);
    void refreshHealth();
    const timer=window.setInterval(()=>void refreshHealth(),6000);
    return()=>window.clearInterval(timer);
  },[refreshHealth]);

  useEffect(()=>{
    void api<PersonaProfile[]>('/api/personas').then(setProfiles).catch(error=>notify('Persona 列表读取失败：'+errorText(error),'error'));
  },[notify]);

  const refreshSelected=useCallback(async(id:string)=>{
    const ticket=++selection.current;
    if(!id){displayedSession.current='';setSession(null);setHistory(null);setConversations([]);setActionRequests([]);return;}
    try{
      const base=robotPath(id);
      const selected=await api<Session>(base+'/session');
      if(ticket!==selection.current)return;
      const identity=[selected.robot_id,selected.chat_id,selected.persona_id,selected.session_epoch,selected.generation].join('\u0000');
      if(displayedSession.current!==identity){
        displayedSession.current=identity;
        setVisibleHistoryCount(HISTORY_PAGE_SIZE);
        setHistory(null);setContext(null);setCoordinator(null);setSocial({});setActionRequests([]);
      }
      setSession(selected);
      const [list,turns,rooms,socialState,roles,actionsData]=await Promise.all([
        api<Conversation[]>(base+'/conversations'),
        api<History>(base+'/conversations/'+encodeURIComponent(selected.chat_id)+'?persona_id='+encodeURIComponent(selected.persona_id)+'&limit='+HISTORY_PAGE_SIZE),
        api<RoomRoute[]>('/api/phase2/rooms'),
        api<SocialState>(base+'/social-state'),
        api<PersonaInstance[]>(base+'/persona-instances'),
        api<{requests?:ActionRequest[]}>(base+'/actions').catch(()=>null),
      ]);
      if(ticket!==selection.current)return;
      setConversations(list);setHistory(turns);setRoutes(rooms);setSocial(socialState);setInstances(roles);
      if(actionsData?.requests)setActionRequests(actionsData.requests);
    }catch(error){if(ticket===selection.current)notify('当前机器人数据读取失败：'+errorText(error),'error');}
  },[notify]);

  const refreshRecent=useCallback(async(id:string,selected:Session)=>{
    const ticket=selection.current;
    const identity=[selected.robot_id,selected.chat_id,selected.persona_id,selected.session_epoch,selected.generation].join('\u0000');
    try{
      const page=await api<History>(robotPath(id)+'/conversations/'+encodeURIComponent(selected.chat_id)+
        '?persona_id='+encodeURIComponent(selected.persona_id)+'&limit='+HISTORY_PAGE_SIZE);
      if(ticket!==selection.current||displayedSession.current!==identity)return;
      setHistory(prev=>{
        if(!prev||prev.chat_id!==page.chat_id||prev.persona_id!==page.persona_id)return page;
        if(!prev.turns.length)return page;
        const recentFirstId=page.turns[0]?.id??Number.POSITIVE_INFINITY;
        const earlier=prev.turns.filter(turn=>turn.id<recentFirstId);
        return {...page,turns:[...earlier,...page.turns],
          has_more:earlier.length?prev.has_more:page.has_more,
          next_before_id:earlier.length?prev.next_before_id:page.next_before_id};
      });
    }catch(error){if(ticket===selection.current)notify('最新对话读取失败：'+errorText(error),'error');}
  },[notify]);

  const loadOlder=useCallback(async()=>{
    if(olderRequest.current||!session||!history?.has_more||!history.next_before_id)return;
    const ticket=selection.current;
    const oldest=history.next_before_id;
    const identity=displayedSession.current;
    olderRequest.current=true;setOlderLoading(true);
    try{
      const page=await api<History>(robotPath(robotId)+'/conversations/'+encodeURIComponent(session.chat_id)+
        '?persona_id='+encodeURIComponent(session.persona_id)+'&limit='+HISTORY_PAGE_SIZE+'&before_id='+oldest);
      if(ticket!==selection.current||identity!==displayedSession.current)return;
      setHistory(prev=>prev&&prev.next_before_id===oldest?{
        ...prev,turns:[...page.turns,...prev.turns],has_more:page.has_more,
        next_before_id:page.next_before_id}:prev);
      setVisibleHistoryCount(count=>count+HISTORY_PAGE_SIZE);
    }catch(error){if(ticket===selection.current)notify('更早对话读取失败：'+errorText(error),'error');}
    finally{olderRequest.current=false;setOlderLoading(false);}
  },[session,history,robotId,notify]);

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
    }).catch(error=>{if(active)notify('诊断读取失败：'+errorText(error),'error');});
    return()=>{active=false;};
  },[developerOpen,robotId,session?.generation,notify]);

  useEffect(()=>{
    if(!developerOpen||!robotId||!session){setDiagnostics(null);return;}
    let active=true,inFlight=false;
    setDiagnostics(null);
    const selected=session;
    const refresh=async()=>{
      if(inFlight)return;
      inFlight=true;
      try{
        const result=await api<RealtimeDiagnostics>(robotPath(robotId)+'/diagnostics');
        if(active&&result.robot_id===selected.robot_id&&
          result.chat_id===selected.chat_id&&result.persona_id===selected.persona_id&&
          result.session_epoch===selected.session_epoch&&result.generation===selected.generation){
          setDiagnostics(result);
        }
      }catch{if(active)setDiagnostics(null);}
      finally{inFlight=false;}
    };
    void refresh();
    const timer=window.setInterval(()=>void refresh(),5000);
    return()=>{active=false;window.clearInterval(timer);};
  },[developerOpen,robotId,session?.chat_id,session?.persona_id,
    session?.session_epoch,session?.generation]);

  useEffect(()=>{
    setRealtimeEvents([]);setLiveTurn(null);setLiveText('');setPreviewTranscript('');
    setPendingAsr('');setAmbientPreview('');setAsrStage('idle');setFirstAsrDeltaMs(null);
    setFinalTranscript(null);setAmbient([]);setDecisions([]);setLastFirstText(null);eventCursor.current=0;
    followLatest.current=true;
    setShowJump(false);
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
        if(result.gap)notify('Realtime 事件缺口','info');
        const scoped=events.filter(event=>eventBelongsToSession(event,session));
        if(scoped.some(item=>isPodStatusEvent(item.kind)))
          setPodStatusRevision(previous=>previous+1);
        if(scoped.length){
          // Audio/diagnostic events should not diff the whole conversation
          // while the diagnostics drawer is closed. Keep only playback controls
          // and bounded event-policy updates; retain the full stream when inspected.
          const controls=scoped.filter(item=>item.kind==='playback.hold_requested'||
            item.kind==='playback.resume_requested'||item.kind==='event.policy'||
            item.kind==='task.status'||item.kind==='action.state'||item.kind==='tool.call');
          if(developerOpenRef.current||controls.length)
            setRealtimeEvents(prev=>[...prev,...(developerOpenRef.current?scoped:controls)].slice(-100));
          for(const item of scoped){
            if(item.kind==='action.state'&&item.turn_id&&item.action_value){
              const actEntry:ActionRequest={
                robot_id:item.robot_id,
                room_id:(item as unknown as {room_id?:string}).room_id??'',
                turn_id:item.turn_id,
                call_id:(item as unknown as {call_id?:string}).call_id??'',
                kind:item.action_kind??'action',
                value:item.action_value,
                status:item.status??'call_observed',
                error:item.error??null,
              };
              setActionRequests(prev=>{
                const idx=prev.findIndex(r=>r.turn_id===actEntry.turn_id&&(r.call_id===actEntry.call_id||r.value===actEntry.value));
                if(idx>=0){const next=[...prev];next[idx]=actEntry;return next;}
                return [actEntry,...prev];
              });
            }
            if(item.kind==='turn.started'||item.kind==='session.replaced'){
              setLiveText('');setPreviewTranscript('');setPendingAsr('');
              setAsrStage('idle');setFinalTranscript(null);
              setLiveTurn(item.kind==='turn.started'&&item.turn_id
                ?{turnId:item.turn_id,userText:item.user_text??'',source:item.turn_source==='external'?'external':'user'}:null);
              followLatest.current=true;
              setShowJump(false);
            }
            if(item.kind==='speech.started'){
              setAsrStage('speech');setPreviewTranscript('');setAmbientPreview('');
              setPendingAsr('');setFirstAsrDeltaMs(null);
            }
            if(item.kind==='speech.stopped')setAsrStage('finalizing');
            if(item.kind==='transcript.first_delta')setFirstAsrDeltaMs(item.after_speech_ms??null);
            if(item.kind==='transcript.delta'){
              setPreviewTranscript((item.text??'')+(item.stash??''));
              setAsrStage('speech');
            }
            if(item.kind==='transcript.ambient_delta')
              setAmbientPreview((item.text??'')+(item.stash??''));
            if(item.kind==='transcript.ambient'){
              setAmbientPreview('');setPreviewTranscript('');setAsrStage('idle');
              // Side talk the decision model declined is listed under decisions.
              if(isProviderAmbient(item)&&item.text?.trim())
                setAmbient(previous=>[...previous,item.text!.trim()].slice(-5));
            }
            if(item.kind==='audio.devices.conflict'||item.kind==='audio.devices.ok'||
               item.kind.startsWith('robot.head.')){
              // Refresh at once instead of waiting for the 6 s /inputs poll.
              void api<unknown>(robotPath(robotId)+'/inputs')
                .then(inputs=>{if(active)setPcm(inputs);}).catch(()=>{});
              if(item.kind==='audio.devices.conflict')
                notify('同一房间检测到多个麦克风或扬声器，请只保留一个','error');
              if(item.kind==='robot.head.handover')
                notify(handoverEventMessage(item),'info');
            }
            const notice=faceNotice(item)??volumeNotice(item);
            if(notice)notify(notice.text,notice.type);
            const decision=decisionEntry(item);
            if(decision)setDecisions(previous=>[decision,...previous].slice(0,6));
            if(item.kind==='persona.style.selected'||item.kind==='chat.preference.set'){
              // A change made by talking must show up exactly like one made here.
              void api<SocialState>(robotPath(robotId)+'/social-state')
                .then(state=>{if(active)setSocial(state);}).catch(()=>{});
              void refreshHealth();
              if(item.kind==='persona.style.selected')
                notify('已通过对话切换性格：'+(item.style_label??item.style_id??''),'info');
            }
            if(item.kind==='transcript.final_preview'){
              setPendingAsr(item.text??'');setPreviewTranscript('');
              setAsrStage('finalizing');
            }
            if(item.kind==='transcript.done'){
              setPreviewTranscript('');setPendingAsr('');setAsrStage('idle');
              if(item.text&&item.turn_id){
                setFinalTranscript({turnId:item.turn_id,text:item.text});
                if(session)void refreshRecent(robotId,session);
              }
            }
            if(item.kind==='text.delta'||item.kind==='text.done')setLiveText(item.text??'');
            if(item.kind==='session.error')notify('Realtime：'+(item.error??'发生错误'),'error');
            if(item.kind==='session.ready'){
              void refreshHealth();
              setSystemError(prev=>(prev?.startsWith('Realtime：')||prev?.startsWith('Realtime 实时推送中断')?null:prev));
              setToast(null);
            }
            if(item.kind==='turn.done'){
              if(typeof item.first_text_ms==='number')setLastFirstText(item.first_text_ms);
              setLiveText(item.text??'');if(session)void refreshRecent(robotId,session);
            }
          }
        }
      }catch(error){notify('Realtime 事件解析失败：'+errorText(error),'error');}
    };
    source.onerror=()=>{if(active)notify('Realtime 实时推送中断，正在自动重连…','error');};
    return()=>{active=false;source.close();};
  },[robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation,!!realtime,refreshRecent,notify,refreshHealth]);

  useEffect(()=>{
    if(!robotId)return;
    let active=true;
    const poll=async()=>{
      try{
        const [inputs,rooms]=await Promise.all([
          api<unknown>(robotPath(robotId)+'/inputs'),api<RoomRoute[]>('/api/phase2/rooms')
        ]);
        if(active){setPcm(inputs);setRoutes(rooms);}
      }catch(error){if(active)notify('Room 状态读取失败：'+errorText(error),'error');}
    };
    const timer=window.setInterval(()=>void poll(),6000);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId,notify]);

  useLayoutEffect(()=>{
    if(followLatest.current&&scrollPanel.current)
      scrollPanel.current.scrollTop=scrollPanel.current.scrollHeight;
  },[history,history?.turns.length,history?.turns.at(-1)?.assistant_generated_text,
     history?.turns.at(-1)?.assistant_spoken_text,history?.turns.at(-1)?.status,
     liveTurn?.turnId,liveTurn?.userText,liveText,previewTranscript,pendingAsr,
     finalTranscript?.text,robotId,session?.chat_id]);

  useEffect(()=>{
    const onKey=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){
        if(createOpen||deleteOpen||developerOpen||preferenceOpen||memoryOpen){
          setCreateOpen(false);setDeleteOpen(false);setDeveloperOpen(false);setPreferenceOpen(false);
          setMemoryOpen(false);
        }else if(historyOpen){
          setHistoryOpen(false);
        }
      }
      if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){
        event.preventDefault();
        setHistoryOpen(open=>!open);
      }
    };
    window.addEventListener('keydown',onKey);
    return()=>window.removeEventListener('keydown',onKey);
  },[createOpen,deleteOpen,developerOpen,preferenceOpen,memoryOpen,historyOpen]);

  function choose(id:string){
    if(id===robotId)return;
    selection.current++;eventCursor.current=0;followLatest.current=true;displayedSession.current='';
    setShowJump(false);
    setRobotId(id);setSession(null);setConversations([]);setHistory(null);setContext(null);setActionRequests([]);
    setPcm(null);setRoutes([]);setCoordinator(null);setSocial({});
    setLiveText('');setRealtimeEvents([]);dismissError();setCreateOpen(false);setDeleteOpen(false);setMemoryOpen(false);
  }

  async function switchSession(item:Conversation){
    if(!robotId||pending)return;
    const id=robotId,ticket=selection.current;
    setPending(true);dismissError();
    try{
      await api(robotPath(id)+'/activate','POST',{
        chat_id:item.chat_id,persona_id:item.persona_id,
        persona_revision:item.persona_revision??(session?.persona_id===item.persona_id?session.persona_revision:1)
      });
      if(ticket===selection.current){followLatest.current=true;await refreshSelected(id);
        notify('已激活到 '+id,'info');}
    }catch(error){if(ticket===selection.current)notify('切换会话失败：'+errorText(error),'error');}
    finally{setPending(false);}
  }

  async function createSession(){
    if(!robotId||pending||!draftChat.trim()||!draftPersona.trim()||!draftName.trim()||!Number.isSafeInteger(draftRevision)||draftRevision<1)return;
    const id=robotId,ticket=selection.current;
    setPending(true);dismissError();
    try{
      await api(robotPath(id)+'/persona-instances','POST',{
        persona_id:draftPersona.trim(),display_name:draftName.trim(),
      });
      await api(robotPath(id)+'/activate','POST',{
        chat_id:draftChat.trim(),persona_id:draftPersona.trim(),
        persona_revision:draftRevision,style_id:draftStyle,
      });
      if(ticket===selection.current){
        followLatest.current=true;setCreateOpen(false);await refreshSelected(id);
      }
    }catch(error){if(ticket===selection.current)notify('创建会话失败：'+errorText(error),'error');}
    finally{setPending(false);}
  }

  async function removeSession(){
    if(!session||pending)return;
    const id=robotId,ticket=selection.current;
    setPending(true);dismissError();
    try{
      await api(robotPath(id)+'/conversations/'+encodeURIComponent(session.chat_id)+'?persona_id='+encodeURIComponent(session.persona_id),'DELETE');
      if(ticket===selection.current){setDeleteOpen(false);await refreshSelected(id);}
    }catch(error){if(ticket===selection.current)notify('删除会话失败：'+errorText(error),'error');}
    finally{setPending(false);}
  }

  async function sendText(){
    if(!robotId||pending||!text.trim()||!realtime?.ready)return;
    const id=robotId,ticket=selection.current,content=text.trim();
    setPending(true);dismissError();
    followLatest.current=true;
    setShowJump(false);
    if(scrollPanel.current)scrollPanel.current.scrollTop=scrollPanel.current.scrollHeight;
    try{
      await api(robotPath(id)+'/realtime/text','POST',{text:content});
      if(ticket===selection.current){
        setText('');
        followLatest.current=true;
        setShowJump(false);
        if(scrollPanel.current)scrollPanel.current.scrollTop=scrollPanel.current.scrollHeight;
      }
    }catch(error){if(ticket===selection.current)notify('发送失败，文字已保留：'+errorText(error),'error');}
    finally{setPending(false);}
  }

  async function cancelCurrent(){
    if(!robotId)return;
    const id=robotId,ticket=selection.current;
    try{
      await api(robotPath(id)+'/cancel','POST');
      if(ticket===selection.current){notify('已发送取消请求','info');void refreshSelected(id);}
    }catch(error){if(ticket===selection.current)notify('取消请求失败：'+errorText(error),'error');}
  }

  async function setQuestionPressure(value:'active'|'normal'|'low'|'none'){
    if(!session||!robotId||settingPreference||pending||!realtime?.ready)return;
    const id=robotId,ticket=selection.current;
    setSettingPreference(true);dismissError();
    try{
      await api(robotPath(id)+'/social-state','PUT',{
        fields:{question_pressure:value},source:'user_setting',generation:session.generation
      });
      if(ticket===selection.current){
        await refreshSelected(id);
        await refreshHealth();
        notify('追问偏好已保存','info');
      }
    }catch(error){if(ticket===selection.current)notify('偏好设置失败：'+errorText(error),'error');}
    finally{setSettingPreference(false);}
  }

  async function setPersonaStyle(styleId:string){
    if(!session||!robotId||settingPreference||pending||!profiles.some(item=>item.persona_id===styleId)||styleId===savedStyle)return;
    const id=robotId,ticket=selection.current;
    setSettingPreference(true);dismissError();
    try{
      await api(robotPath(id)+'/social-state','PUT',{
        fields:{persona_style:styleId},source:'user_setting',generation:session.generation
      });
      if(ticket===selection.current){
        await refreshSelected(id);
        await refreshHealth();
        notify('性格已保存','info');
      }
    }catch(error){if(ticket===selection.current)notify('性格切换失败：'+errorText(error),'error');}
    finally{setSettingPreference(false);}
  }

  function openCreate(){
    setDraftChat('');setDraftPersona(session?.persona_id??selectedRobot?.persona_id??'annie');
    setDraftName(instances.find(item=>item.persona_id===session?.persona_id)?.display_name??session?.persona_id??'小安妮');
    setDraftStyle(savedStyle);setDraftRevision(session?.persona_revision??1);setCreateOpen(true);
  }

  const voiceMode=!realtime?.ready?'未连接':
    speech?.playback_control_state==='held_confirmed'||speech?.playback_control_state==='hold_requested'?'暂停中':
    ['speaking','streaming','running'].includes(speech?.state??'')?'说话':
    asrStage==='speech'||previewTranscript?'倾听':asrStage==='finalizing'?'识别结束':liveTurn||liveText?'思考':'语音空闲';

  const lastUserText=history?.turns?.at(-1)?.user_text;
  const lastAssistantReply=history?.turns?.at(-1)?.assistant_spoken_text||history?.turns?.at(-1)?.assistant_generated_text;

  return <div className="app-shell cockpit-shell" ref={root}>
    <ParticleCanvas theme={theme} palette={palette} micActive={micActive}
      speechActive={auditionActive||['speaking','streaming','running'].includes(speech?.state??'')} />
    <header className="topbar cockpit-topbar">
      <div className="cockpit-brand">
        <span className="brand-mark" aria-hidden="true">A<span>·</span></span>
        <div><strong>小安妮</strong></div>
      </div>
      <div className="top-robot-switch" role="group" aria-label="切换机器人">
        {health?.robots.map(robot=><button key={robot.robot_id} type="button"
          className={'robot-pill '+(robotId===robot.robot_id?'selected':'')}
          aria-pressed={robotId===robot.robot_id} onClick={()=>choose(robot.robot_id)}>
          {robot.robot_id}</button>)}
        {!health?.robots.length&&<span className="empty-copy">无可用机器人</span>}
      </div>
      <div className="topbar-right">
        {onSwitchMode && (
          <button className="action-pill" type="button" onClick={() => onSwitchMode('cockpit')} title="切回全息态势大屏" style={{ color: 'var(--neon-cyan, #00f0ff)', fontWeight: 600 }}>
            🌌 态势大屏
          </button>
        )}
        <button className="action-pill theme-toggle-btn" type="button"
          onClick={()=>setTheme(t=>t==='dark'?'light':'dark')}
          aria-label={theme==='dark'?'切换至日间模式':'切换至夜间模式'}
          title={theme==='dark'?'切换至日间模式':'切换至夜间模式'}>
          {theme==='dark'?'☀️ 日间':'🌙 夜间'}
        </button>
        <div className="palette-dropdown-wrap">
          <button className="action-pill palette-trigger" type="button"
            onClick={()=>setPaletteOpen(o=>!o)}
            aria-label="切换主题配色"
            title="切换主题配色">
            <span className={`palette-dot dot-${palette}`}/>
            <span>{palette==='violet'?'星云紫':palette==='emerald'?'极光绿':palette==='amber'?'暖阳橙':'深空蓝'}</span>
          </button>
          {paletteOpen&&(
            <div className="palette-popover" onMouseLeave={()=>setPaletteOpen(false)}>
              <div className={`palette-item ${palette==='violet'?'is-active':''}`} onClick={()=>{setPalette('violet');setPaletteOpen(false);}}>
                <span className="palette-dot dot-violet"/>
                <span>星云曜石 (紫)</span>
              </div>
              <div className={`palette-item ${palette==='emerald'?'is-active':''}`} onClick={()=>{setPalette('emerald');setPaletteOpen(false);}}>
                <span className="palette-dot dot-emerald"/>
                <span>赛博极光 (绿)</span>
              </div>
              <div className={`palette-item ${palette==='amber'?'is-active':''}`} onClick={()=>{setPalette('amber');setPaletteOpen(false);}}>
                <span className="palette-dot dot-amber"/>
                <span>暖阳琥珀 (橙)</span>
              </div>
              <div className={`palette-item ${palette==='blue'?'is-active':''}`} onClick={()=>{setPalette('blue');setPaletteOpen(false);}}>
                <span className="palette-dot dot-blue"/>
                <span>深空电蓝 (蓝)</span>
              </div>
            </div>
          )}
        </div>
        <button className={'action-pill '+(historyOpen?'active':'')} type="button"
          onClick={()=>setHistoryOpen(open=>!open)} aria-expanded={historyOpen}>
          💬 会话{conversations.length>0?' ('+conversations.length+')':''}
        </button>
        <button className={'action-pill '+(memoryOpen?'active':'')} type="button"
          onClick={()=>setMemoryOpen(true)} disabled={!session}
          aria-haspopup="dialog" aria-expanded={memoryOpen} title="会话与长期记忆管理">
          记忆
        </button>
        <button className="action-pill" type="button" onClick={()=>setPreferenceOpen(true)}>
          ⚙️ 设定
        </button>
        <button className="action-pill" type="button" onClick={()=>setActionOpen(true)} disabled={!robotId}>
          动作
        </button>
        <button className={'action-pill '+(eventsOpen?'active':'')} type="button"
          onClick={()=>setEventsOpen(true)} disabled={!robotId} title="定时提醒与事件">
          ⏰ 提醒
        </button>
        <button className={'action-pill '+(developerOpen?'active':'')+(systemError?' pill-has-error':'')} type="button"
          onClick={()=>setDeveloperOpen(true)}
          aria-haspopup="dialog" aria-expanded={developerOpen}
          title={systemError?`存在系统异常：${systemError}（点击查看详情）`:'系统诊断'}>
          ⌁ 诊断
          {systemError&&<span className="error-badge-dot" aria-label="存在系统错误"/>}
        </button>
        <span className={'top-health '+(health?.ready?'online':'offline')}>
          <i/>{health?.ready?'在线':'未就绪'}
        </span>
      </div>
    </header>

    <RoomObservationsBar robotId={robotId}/>

    {toast&&<aside className={'floating-toast toast-'+toast.type} role="status">
      <span className="toast-msg">{toast.message}</span>
      <button type="button" className="toast-close" onClick={dismissError} aria-label="忽略此通知">×</button>
    </aside>}

    <main className="workspace cockpit-workspace">
      <div className={'chat-layout cockpit-layout '+(historyOpen?'history-open':'history-closed')}>
        <AnnieStatePod key={robotId+'\u0000'+(session?.generation??'none')}
          robotId={robotId} generation={session?.generation}
          roomId={selectedRobot?.room_id??route?.room_id} micActive={micActive}
          statusRevision={podStatusRevision}/>
        <div className="cockpit-stage">
          <aside className="chat-sidebar">
            <div className="sidebar-top">
              <h2>历史会话</h2>
              <button type="button" className="history-toggle" onClick={()=>setHistoryOpen(false)} aria-label="收起历史会话">✕</button>
            </div>
            <button className="new-chat" onClick={openCreate} disabled={!robotId}>＋ 新建 Chat</button>
            <div className="chat-list">
              {conversations.map(item=>
                <button key={item.chat_id+'\u0000'+item.persona_id} disabled={pending}
                  className={'chat-list-item '+(session?.chat_id===item.chat_id&&session.persona_id===item.persona_id?'selected':'')}
                  onClick={()=>void switchSession(item)}>
                  <span className="chat-item-mark">◫</span>
                  <span className="chat-item-body">
                    <span className="chat-item-title-row">
                      <strong>{item.chat_id}</strong>
                      {item.updated_ms&&<time className="chat-item-time">{formatTimestamp(item.updated_ms)}</time>}
                    </span>
                    <small>角色 {instances.find(role=>role.persona_id===item.persona_id)?.display_name??item.persona_id} · {item.turn_count??0} 轮</small>
                    {session?.chat_id===item.chat_id&&session.persona_id===item.persona_id&&<small>已激活</small>}
                  </span>
                </button>)}
              {!conversations.length&&<p className="empty-copy">暂无会话</p>}
            </div>
          </aside>

          <div className="stage-header">
            <div className="stage-header-left">
              <span className="eyebrow" title="Qwen 全双工实时语音会话通道">实时对讲</span>
              <h1>{session?.chat_id??'实时对话'}</h1>
              {session&&<span className="room-tag" title="当前机器人唯一的实时会话">已激活 · {instances.find(role=>role.persona_id===session.persona_id)?.display_name??session.persona_id}</span>}
              <span className="room-tag" title="绑定的声学房间通道">{selectedRobot?.room_id??route?.room_id??session?.room_id??'—'}</span>
              <div className="stage-status-cluster" title="实时连接状态">
                <State value={realtime?.state} good={realtime?.ready}/>
                <span className="voice-mode" role="status">{voiceMode}</span>
                <WakeIndicator key={`${robotId}:${session?.generation??'none'}`}
                  robotId={robotId} generation={session?.generation}
                  ready={!!realtime?.ready} initial={realtime?.wakeword}/>
              </div>
            </div>
            <div className="stage-header-tools">
              <label className="persona-live-switch">当前性格
                <CustomSelect aria-label="切换当前会话性格" ariaLabel="切换当前会话性格" value={savedStyle}
                  disabled={!session||!realtime?.ready||pending||settingPreference||!profiles.length}
                  onChange={val=>void setPersonaStyle(val)}
                  options={[
                    ...(!profiles.some(item=>item.persona_id===savedStyle)?[{value:savedStyle,label:savedStyle||'未配置'}]:[]),
                    ...profiles.map(item=>({value:item.persona_id,label:item.label}))
                  ]}/>
              </label>
              <RoleLanguagePreference robotId={robotId} session={session}/>
              {!styleApplied&&session&&<span role="status" className="quality-note">性格待生效</span>}
              <button className="quiet-button" type="button" onClick={()=>setHistoryOpen(o=>!o)}>{historyOpen?'收起会话':'会话列表'}</button>
              <button className="quiet-button" type="button" onClick={openCreate} disabled={!robotId}>＋ 新建</button>
              <button className="quiet-button danger-text" type="button" onClick={()=>setDeleteOpen(true)} disabled={!session||pending}>删除</button>
            </div>
          </div>

          {currentHead&&<div className="head-strip" role="status">
            <strong>机器人头</strong>
            <span>{headLabel(currentHead)}</span>
            {currentHead.microphone&&<span>麦克风 {currentHead.microphone}</span>}
            {currentHead.speaker&&<span>扬声器 {currentHead.speaker}{headVolume!=null&&` · 音量 ${headVolume}`}</span>}
            {handoverNotice&&<em>{handoverNotice}</em>}
          </div>}
          {deviceWarnings.length>0&&<div className="device-conflict" role="alert">
            <strong>⚠ 音频设备冲突</strong>
            {deviceWarnings.map(text=><span key={text}>{text}</span>)}
          </div>}
          <div className="live-cockpit-cards">
            <div className="live-cockpit-card">
              <div className="card-label">
                <span>ASR 增量</span>
                <span className={'status-dot '+(previewTranscript?'dot-good':'')}/>
              </div>
              <div className={'card-body '+(previewTranscript?'partial':'')}>
                {previewTranscript||'点击麦克风直接说话'}
              </div>
            </div>
            <div className="live-cockpit-card">
              <div className="card-label"><span>你的最终文本</span></div>
              <div className="card-body">
                {finalTranscript?.text||lastUserText||'—'}
              </div>
            </div>
            <div className="live-cockpit-card decision-card" ref={decisionCardRef} aria-label="对话决策">
              <div className="card-label">
                <div className="decision-label-left">
                  <span>对话决策</span>
                  {decisions.length > 0 && (
                    <span className="decision-count-badge">{decisions.length}</span>
                  )}
                  {decisionModel && (
                    <span className={`decision-model-chip tone-${decisionModel.tone}`}
                      title={decisionModel.title} aria-label={`决策模型：${decisionModel.title}`}>
                      {decisionModel.label}
                    </span>
                  )}
                </div>
                {decisions.length > 1 && (
                  <button
                    type="button"
                    className={`decision-toggle-btn ${decisionPopoverOpen ? 'expanded' : ''}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      setDecisionPopoverOpen(prev => !prev);
                    }}
                    aria-label="查看决策流水"
                  >
                    {decisionPopoverOpen ? '收起 ▴' : `流水 (${decisions.length}) ▾`}
                  </button>
                )}
              </div>
              <div className="card-body decision is-single">
                {decisions.length === 0 ? (
                  <span className="decision-empty-text">待命 · 伴随对话触发</span>
                ) : (
                  <DecisionItem entry={decisions[0]} />
                )}
              </div>
              {decisionPopoverOpen && decisions.length > 0 && (
                <div className="decision-history-popover" role="dialog" aria-label="最近决策流水">
                  <div className="decision-popover-head">
                    <div className="decision-popover-title-row">
                      <span className="decision-popover-icon">🧠</span>
                      <span className="decision-popover-title">最近决策流水</span>
                      <span className="decision-popover-count">共 {decisions.length} 条</span>
                    </div>
                    <button
                      type="button"
                      className="decision-popover-close"
                      onClick={(e) => {
                        e.stopPropagation();
                        setDecisionPopoverOpen(false);
                      }}
                      aria-label="关闭"
                    >
                      ✕
                    </button>
                  </div>
                  <div className="decision-popover-stream">
                    {decisions.map((entry, idx) => (
                      <div
                        key={entry.seq ?? idx}
                        className={`decision-stream-row ${idx === 0 ? 'is-latest' : 'is-history'}`}
                      >
                        <span className="decision-seq-tag">
                          {idx === 0 ? '最新' : `#${decisions.length - idx}`}
                        </span>
                        <DecisionItem entry={entry} isExpanded={true} />
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
            <div className="live-cockpit-card">
              <div className="card-label">
                <span>AI 流式回答</span>
                <span className={'status-dot '+(liveText?'dot-good':'')}/>
              </div>
              <div className="card-body assistant">
                {liveText||lastAssistantReply||'—'}
              </div>
            </div>
            <div className="live-cockpit-card">
              <div className="card-label"><span>smart_turn 忽略的环境音</span></div>
              <div className="card-body ambient">
                {ambient.length>0?ambient.slice(-4).join(' / '):'—'}
              </div>
            </div>
          </div>

          <section className="conversation" aria-label="正式对话工作台">
            <div className="messages" ref={scrollPanel} aria-label="完整历史"
              onWheel={e=>{if(e.deltaY<0){followLatest.current=false;setShowJump(true);}}}
              onTouchMove={()=>{followLatest.current=false;setShowJump(true);}}
              onScroll={e=>{
                const node=e.currentTarget;
                const distance=node.scrollHeight-node.scrollTop-node.clientHeight;
                const atBottom=distance<=64;
                followLatest.current=atBottom;
                setShowJump(!atBottom);
              }}>
              {!history&&<div className="message-placeholder">正在读取会话历史…</div>}
              {history?.turns.length===0&&!liveText&&<div className="stage-hero">
                <div className="hero-orb-ring"><div className="hero-orb"/></div>
                <h2>小安妮实时对讲</h2>
                <p className="hero-sub">全双工声学通道就绪 · 随时说话或输入文字</p>
                <div className="hero-status-row">
                  <span className="hero-chip">🎙️ 拾音通道：{route?.room_id??selectedRobot?.room_id??'—'}</span>
                  <span className="hero-chip">🎭 角色：{instances.find(role=>role.persona_id===session?.persona_id)?.display_name??session?.persona_id??'小安妮'}</span>
                  <span className="hero-chip">⚡ 状态：{realtime?.ready?'在线双工':'连接中'}</span>
                </div>
                <div className="hero-prompt-suggestions">
                  <button type="button" className="hero-prompt-pill" onClick={()=>setText('你好呀小安妮，今天天气怎么样？')}>
                    💬 “你好呀小安妮，今天天气怎么样？”
                  </button>
                  <button type="button" className="hero-prompt-pill" onClick={()=>setText('你能听到我说话吗？')}>
                    💬 “你能听到我说话吗？”
                  </button>
                </div>
              </div>}
              {history&&(history.has_more||history.turns.length>visibleHistoryCount)&&
                <button type="button" disabled={olderLoading} onClick={()=>{
                  if(history.turns.length>visibleHistoryCount)setVisibleHistoryCount(count=>count+HISTORY_PAGE_SIZE);
                  else void loadOlder();
                }}>{olderLoading?'读取历史中…':'加载更早对话'}</button>}
              {history?.turns.slice(-visibleHistoryCount).map(turn=>{
                const streaming=liveTurn?.turnId===turn.turn_id&&turn.status==='running';
                const reply=streaming?(liveText||turn.assistant_generated_text):
                  (turn.assistant_spoken_text||turn.assistant_generated_text);
                const user=turn.user_text||(
                  finalTranscript?.turnId===turn.turn_id?finalTranscript.text:
                    liveTurn?.turnId===turn.turn_id?previewTranscript||liveTurn.userText:''
                );
                return <div className="exchange" key={turn.turn_id}>
                  {turn.turn_source!=='external'&&<div className="message-row me"><div className="message-content"><span className="message-author">你{turn.updated_ms&&<time className="message-timestamp"> · {formatTimestamp(turn.updated_ms)}</time>}</span><div className="bubble me-bubble">{user||'语音转写中…'}</div></div></div>}
                  {(reply||(turn.turn_source!=='external'&&turn.status!=='running'))&&<div className="message-row agent"><span className="agent-avatar">A</span><div className="message-content">
                    <span className="message-author">
                      小安妮
                      {getTurnActions(turn.turn_id,actionRequests,realtimeEvents,diagnostics).map(act=>{
                        const label=ACTION_SCRIPT_NAMES[act.value]??act.value;
                        const statusText=actionStatusLabel[act.status]??act.status;
                        const isFailed=['cancelled_before_send','expired_no_pcm','rejected_late'].includes(act.status);
                        return <button key={act.call_id||act.value} type="button"
                          className={'turn-action-badge '+(isFailed?'failed':'')}
                          onClick={()=>setActionOpen(true)}
                          title={`云端动作：${label} (${act.value}) · 状态：${statusText}（点击查看详情）`}>
                          <span className="turn-action-icon" aria-hidden="true">🎭</span>
                          <span className="turn-action-label">{label}</span>
                        </button>;
                      })}
                      {turn.updated_ms&&<time className="message-timestamp"> · {formatTimestamp(turn.updated_ms)}</time>}
                    </span>
                    {reply&&(streaming?[reply]:splitReplyBubbles(reply)).map((part,index)=>
                      <div className="bubble agent-bubble" style={index?{marginTop:8}:undefined}
                        key={index}>{part}</div>)}
                    <span className="message-foot">
                      {streaming&&<i className="streaming-pulse-dot" aria-hidden="true"/>}
                      {streaming?'正在生成':playbackLabel(turn,speech?.state==='not_configured')}
                    </span>
                  </div></div>}
                </div>;
              })}
              {liveTurn&&!history?.turns.some(turn=>turn.turn_id===liveTurn.turnId)&&<div className="exchange" aria-live="polite">
                {liveTurn.source!=='external'&&<div className="message-row me"><div className="message-content"><span className="message-author">你</span><div className="bubble me-bubble">{finalTranscript?.turnId===liveTurn.turnId?finalTranscript.text:previewTranscript||liveTurn.userText||'语音转写中…'}</div></div></div>}
                {liveText&&<div className="message-row agent"><span className="agent-avatar">A</span><div className="message-content">
                  <span className="message-author">
                    小安妮
                    {getTurnActions(liveTurn.turnId,actionRequests,realtimeEvents,diagnostics).map(act=>{
                      const label=ACTION_SCRIPT_NAMES[act.value]??act.value;
                      const statusText=actionStatusLabel[act.status]??act.status;
                      const isFailed=['cancelled_before_send','expired_no_pcm','rejected_late'].includes(act.status);
                      return <button key={act.call_id||act.value} type="button"
                        className={'turn-action-badge '+(isFailed?'failed':'')}
                        onClick={()=>setActionOpen(true)}
                        title={`云端动作：${label} (${act.value}) · 状态：${statusText}（点击查看详情）`}>
                        <span className="turn-action-icon" aria-hidden="true">🎭</span>
                        <span className="turn-action-label">{label}</span>
                      </button>;
                    })}
                  </span>
                  <div className="bubble agent-bubble">{liveText}</div><span className="message-foot"><i className="streaming-pulse-dot" aria-hidden="true"/>实时回复，等待入账</span>
                </div></div>}
              </div>}
              {(previewTranscript||pendingAsr)&&!liveTurn&&<div className="message-row me" aria-live="polite"><div className="message-content"><span className="message-author">你 · {pendingAsr?'最终转写':'语音转写中'}</span><div className="bubble me-bubble">{pendingAsr||previewTranscript}</div></div></div>}
              {finalTranscript&&!liveTurn&&!history?.turns.some(turn=>turn.turn_id===finalTranscript.turnId)&&<div className="message-row me" aria-live="polite"><div className="message-content"><span className="message-author">你 · 最终转写</span><div className="bubble me-bubble">{finalTranscript.text}</div></div></div>}
            </div>
            {showJump&&<button type="button" className="jump-latest" onClick={()=>{
              followLatest.current=true;setShowJump(false);
              if(scrollPanel.current)scrollPanel.current.scrollTo({top:scrollPanel.current.scrollHeight,behavior:'smooth'});
            }}>↓ 回到最新</button>}

            <div className="composer">
              {micActive&&auditionActive&&<div role="alert" className="audio-guidance audio-guidance-pill">
                <span>⚠️ 外放回声风险</span>
                <div className="guidance-popover">
                  建议使用耳机或关闭网页试听。
                </div>
              </div>}
              <form onSubmit={e=>{e.preventDefault();void sendText();}}>
                <label className="sr-only" htmlFor="message-input">输入消息</label>
                <textarea id="message-input" value={text} maxLength={2000} rows={2} onChange={e=>setText(e.target.value)}
                  onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();void sendText();}}}
                  placeholder="输入消息..." disabled={!robotId}/>
                <div className="composer-actions">
                  <button type="button" className="stop-button interrupt-button" onClick={()=>void cancelCurrent()} disabled={!robotId} title="停止模型说话并打断语音">■ 立即打断</button>
                  <button className="primary-button send-button" type="submit" disabled={!text.trim()||pending||!realtime?.ready}>发送 ↗</button>
                </div>
              </form>
            </div>
          </section>
        </div>

        <aside className="inspector cockpit-hud" aria-label="声学与延迟仪表盘">
          <LatencyHud realtime={realtime} speech={speech}/>
          <SpeechExpressionPanel key={JSON.stringify([robotId,session?.chat_id,session?.persona_id,session?.session_epoch,session?.generation])} robotId={robotId} session={session}/>
          <MicrophoneControls key={robotId+'\u0000'+(route?.room_id??'')}
            route={realtime?.ready?route:undefined} onActive={setMicActive}/>
          <RoomSpeakerPanel key={'speaker:'+robotId+'\u0000'+(route?.room_id??'')}
            url={route?.urls?.tts} voiceUrl={route?.urls?.voice} roomId={route?.room_id}
            command={playbackCommand} onActive={setAuditionActive}
            available={!!speech&&speech.state!=='not_configured'} voiceMode={voiceMode}/>
        </aside>
      </div>
    </main>

    {actionOpen&&session&&<ActionPanel key={session.robot_id}
      robotId={session.robot_id} onClose={()=>setActionOpen(false)}
      onFillText={setText}/>}

    {eventsOpen&&session&&<ExternalEventsDialog
      key={'events:'+session.robot_id}
      robotId={session.robot_id}
      session={session}
      events={realtimeEvents}
      onClose={()=>setEventsOpen(false)}/>}

    {memoryOpen&&session&&<MemoryPanel
      key={[session.robot_id,session.chat_id,session.persona_id,session.generation].join(':')}
      robotId={session.robot_id} personaId={session.persona_id}
      chatId={session.chat_id} generation={session.generation}
      onClose={()=>setMemoryOpen(false)}/>}

    {preferenceOpen&&<div className="modal-backdrop" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)setPreferenceOpen(false);}}>
      <div className="dialog voice-settings-dialog" role="dialog" aria-modal="true" aria-labelledby="preference-title">
        <button type="button" className="dialog-close-btn" onClick={()=>setPreferenceOpen(false)} aria-label="关闭">×</button>
        <h2 id="preference-title">机器人设定</h2>
        <div className="inspector-group" style={{marginBottom:'12px'}}>
          <div className="info-line"><span>当前 Persona</span><strong>{session?.persona_id??'—'}</strong></div>
          <div className="info-line"><span>当前性格</span><strong>{styleApplied?personaForSession?.label:'切换中 / 等待模型连接'}</strong></div>
          <div className="info-line"><span>Persona 版本</span><strong>{session?.persona_revision??'—'}</strong></div>
          <div className="info-line"><span>当前模型生效</span><strong>{personaForSession?.question_pressure==='active'?'积极追问':personaForSession?.question_pressure==='none'?'不主动追问':personaForSession?.question_pressure==='low'?'少追问':personaForSession?.question_pressure==='normal'?'自然追问':'等待 Session 就绪'}</strong></div>
        </div>
        <label className="quality-setting">当前会话性格
          <CustomSelect ariaLabel="设定当前会话性格" value={savedStyle}
            disabled={!session||!realtime?.ready||pending||settingPreference||!profiles.length}
            onChange={val=>void setPersonaStyle(val)}
            options={profiles.some(item=>item.persona_id===savedStyle)
              ? profiles.map(item=>({value:item.persona_id, label:item.label}))
              : [{value:savedStyle, label:savedStyle||'未配置'}, ...profiles.map(item=>({value:item.persona_id, label:item.label}))]}
          />
        </label>
        <label className="quality-setting">追问偏好
          <CustomSelect ariaLabel="追问偏好" value={savedPreference}
            disabled={!session||!realtime?.ready||!!realtime?.response_id||pending||settingPreference}
            onChange={val=>void setQuestionPressure(val as 'active'|'normal'|'low'|'none')}
            options={[
              {value:'active', label:'积极追问'},
              {value:'normal', label:'自然追问'},
              {value:'low', label:'少追问'},
              {value:'none', label:'不主动追问'},
            ]}
          />
        </label>
        <VoiceSettings key={[robotId,session?.chat_id,session?.persona_id].join(':')}
          robotId={robotId} session={session}/>
        <div className="dialog-actions">
          <button className="primary-button" type="button" onClick={()=>setPreferenceOpen(false)}>完成</button>
        </div>
      </div>
    </div>}

    {developerOpen&&<DeveloperDrawer key={session?.robot_id+'\u0000'+session?.chat_id+'\u0000'+session?.persona_id}
      onClose={()=>setDeveloperOpen(false)} onFillText={setText}
      onQuestionPressure={value=>void setQuestionPressure(value)}
      onClearError={dismissError}
      data={{robotId,roomId:selectedRobot?.room_id,roomConnected:!!route?.connected,
        session,realtime,speech,diagnostics,personaForSession,restoredForSession,savedPreference,
        history,social,firstAsrDeltaMs,previewTranscript,finalTranscript,ambient,
        pending,settingPreference,text,realtimeEvents,context,coordinator,pcm,routes,
        systemError}}/>}

    {createOpen&&<div className="modal-backdrop" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)setCreateOpen(false);}}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="create-title">
        <button type="button" className="dialog-close-btn" onClick={()=>setCreateOpen(false)} aria-label="关闭">×</button>
        <h2 id="create-title">新建 Chat · {robotId}</h2>
        <form onSubmit={e=>{e.preventDefault();void createSession();}}>
          <label>Chat 名称<input autoFocus value={draftChat} maxLength={128} onChange={e=>setDraftChat(e.target.value)} placeholder="例如：日常聊天"/></label>
          <label>Persona ID<input list="known-personas" value={draftPersona} maxLength={128} onChange={e=>{
            const id=e.target.value;setDraftPersona(id);
            setDraftName(instances.find(role=>role.persona_id===id)?.display_name??id);
          }} />
            <datalist id="known-personas">
              {instances.map(role=><option key={role.persona_id} value={role.persona_id} label={role.display_name}/>)}
            </datalist>
          </label>
          <label>角色显示名称<input aria-label="Persona 显示名称" value={draftName} maxLength={40} onChange={e=>setDraftName(e.target.value)} placeholder="例如：小安妮"/></label>
          <label>初始性格
            <CustomSelect ariaLabel="新会话初始性格" value={draftStyle} onChange={val=>setDraftStyle(val)}
              options={profiles.map(profile=>({value:profile.persona_id, label:profile.label}))}
            />
          </label>
          <label>Persona revision<input type="number" min="1" step="1" value={draftRevision} onChange={e=>setDraftRevision(Number(e.target.value))}/></label>
          <div className="dialog-actions">
            <button type="button" className="secondary-button" onClick={()=>setCreateOpen(false)}>取消</button>
            <button className="primary-button" disabled={pending||!draftChat.trim()||!draftPersona.trim()||!draftName.trim()||!profiles.some(profile=>profile.persona_id===draftStyle)||draftRevision<1||!Number.isSafeInteger(draftRevision)}>创建并激活</button>
          </div>
        </form>
      </div>
    </div>}

    {deleteOpen&&session&&<div className="modal-backdrop" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)setDeleteOpen(false);}}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="delete-title">
        <button type="button" className="dialog-close-btn" onClick={()=>setDeleteOpen(false)} aria-label="关闭">×</button>
        <h2 id="delete-title">删除当前会话历史？</h2>
        <p><strong>{session.chat_id}</strong> · 删除后无法恢复</p>
        <div className="dialog-actions">
          <button className="secondary-button" onClick={()=>setDeleteOpen(false)}>保留会话</button>
          <button className="danger-button" disabled={pending} onClick={()=>void removeSession()}>确认删除</button>
        </div>
      </div>
    </div>}
  </div>;
}
