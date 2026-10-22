import {Fragment,useEffect,useId,useState} from 'react';
import {api,robotPath} from './api';
import {type MouthRound,mouthRoundRows,mouthRoundTitle} from './mouth-round';
import './room-observations.css';

export type RoomObservations={
  room_id:string;
  viseme:{state:string;heartbeat_age_ms:number|null;reason:string|null;
    mouth_count:number;last_mouth_age_ms:number|null;
    latest_mouth:{char:string;viseme:string;seq:number;conversation_id:string}|null;
    last_round:MouthRound|null};
  visual:{state:string;description:string|null;age_ms:number|null;
    llm_context:{state:string;sent_seq?:number;confirmed_seq?:number}};
};

const visemeLabel:Record<string,string>={
  online:'在线',waiting:'等待心跳',offline:'离线',disconnected:'房间断开',
  degraded:'服务异常',not_configured:'未接入',
};
const visualLabel:Record<string,string>={
  online:'更新中',waiting:'等待画面',stale:'画面未更新',
  disconnected:'房间断开',not_configured:'未接入',
};
const llmLabel:Record<string,string>={
  confirmed:'LLM 已更新',idle:'LLM 无画面',pending_send:'LLM 待发送',
  pending_confirmation:'LLM 待确认',disconnected:'LLM 未连接',
  not_configured:'LLM 未接入',
};

export function RoomObservationsBar({robotId}:{robotId:string}){
  const [data,setData]=useState<RoomObservations|null>(null);
  const [error,setError]=useState(false);
  const popoverId=useId();
  useEffect(()=>{
    setData(null);setError(false);
    if(!robotId)return;
    let active=true,inFlight=false;
    const poll=async()=>{
      if(inFlight)return;
      inFlight=true;
      try{
        const result=await api<RoomObservations>(robotPath(robotId)+'/room-observations');
        if(active){setData(result);setError(false);}
      }catch{if(active){setData(null);setError(true);}}
      finally{inFlight=false;}
    };
    void poll();
    const timer=window.setInterval(()=>void poll(),2000);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId]);
  const viseme=data?.viseme;
  const visual=data?.visual;
  const mouth=viseme?.latest_mouth&&viseme.last_mouth_age_ms!==null&&
    viseme.last_mouth_age_ms<10_000?viseme.latest_mouth:null;
  const visemeStatus = error ? '离线' : visemeLabel[viseme?.state ?? ''] ?? '读取中';
  const visualStatus = error ? '离线' : visualLabel[visual?.state ?? ''] ?? '读取中';

  const round=viseme?.last_round??null;
  const heartbeat=viseme?.heartbeat_age_ms!=null?` · 心跳 ${(viseme.heartbeat_age_ms/1000).toFixed(1)} 秒前`:'';
  const visualTooltip = `视觉描述 · ${visualStatus}${visual?.description ? `\n画面描述：${visual.description}` : ''}${visual?.llm_context?.state ? `\nLLM：${llmLabel[visual.llm_context.state] ?? visual.llm_context.state}` : ''}`;

  return <div className="room-observations" aria-label="房间实时观测">
    <div className="room-observation-wrap">
      <div className="room-observation-pill" tabIndex={0} aria-describedby={popoverId}>
        <span className={'status-dot '+(viseme?.state==='online'?'dot-good':viseme?.state==='offline'||viseme?.state==='disconnected'||error?'dot-bad':'dot-warn')}/>
        <span>嘴型 · {visemeStatus}</span>
      </div>
      <div className="mouth-popover" role="tooltip" id={popoverId}>
        <div className="mouth-popover-head"><strong>嘴型服务</strong><span>{visemeStatus}{heartbeat}</span></div>
        {viseme?.reason&&<div className="mouth-popover-note">原因：{viseme.reason}</div>}
        {round?<>
          <div className="mouth-popover-section">{mouthRoundTitle(round)}</div>
          <dl className="mouth-popover-rows">
            {mouthRoundRows(round).map(row=><Fragment key={row.label}>
              <dt>{row.label}</dt><dd className={row.warn?'is-warn':undefined}>{row.value}</dd>
            </Fragment>)}
          </dl>
          {round.first_delay_ms!==null&&<div className="mouth-popover-note">延迟 = 收到嘴型 − 发出对应 TTS 帧（本机时钟）；TTS 会提前约 240 ms 发出</div>}
        </>:<div className="mouth-popover-empty">还没有收到嘴型</div>}
        {mouth&&<div className="mouth-popover-foot">最新口型：{mouth.char} / {mouth.viseme} · #{mouth.seq}</div>}
      </div>
    </div>
    <div className="room-observation-pill" title={visualTooltip}>
      <span className={'status-dot '+(visual?.state==='online'?'dot-good':visual?.state==='disconnected'||error?'dot-bad':'dot-warn')}/>
      <span>视觉 · {visualStatus}</span>
    </div>
  </div>;
}
