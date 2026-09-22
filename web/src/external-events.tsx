import {useCallback,useEffect,useState} from 'react';
import {api,robotPath,type RealtimeEvent,type Session} from './api';

type EventRow={kind:string;event_id:string|null;decision:string;status:string;
  reason:string|null;transport:string;observed_ms:number;expires_at_ms:number;
  chat_id:string;persona_id:string;generation:number;summary:unknown};
type EventSnapshot={facts:unknown[];events:EventRow[];pending_count:number;
  complex_task:string;chat_id:string;persona_id:string;generation:number};

export function ExternalEvents({robotId,session,events}:{robotId:string;session:Session|null;events:RealtimeEvent[]}){
  const [snapshot,setSnapshot]=useState<EventSnapshot|null>(null);
  const [error,setError]=useState('');
  const [refreshIndex,setRefreshIndex]=useState(0);
  const policySeq=[...events].reverse().find(e=>e.kind==='event.policy')?.seq??0;
  const refresh=useCallback(()=>setRefreshIndex(x=>x+1),[]);
  useEffect(()=>{
    let active=true;
    setSnapshot(null);
    void api<EventSnapshot>(robotPath(robotId)+'/external-events').then(result=>{
      if(active){setSnapshot(result);setError('');}
    }).catch(reason=>{if(active)setError(String(reason));});
    return()=>{active=false;};
  },[robotId,session?.session_epoch,session?.generation,policySeq,refreshIndex]);
  const current=snapshot&&session&&snapshot.chat_id===session.chat_id&&
    snapshot.persona_id===session.persona_id&&snapshot.generation===session.generation;
  return <section className="inspector-group" aria-label="Phase 8 外部事件">
    <div className="surface-heading"><h3>外部事件 · Phase 8A</h3>
      <button type="button" className="quiet-button" onClick={refresh}>刷新事实</button></div>
    <p>事件来源、策略、等待或丢弃原因；提交模型不等于已经说出或设备已执行。</p>
    {error&&<p role="alert">{error}</p>}
    {!current?<p>等待当前机器人 / 会话事实</p>:<>
      <div className="info-line"><span>待发声</span><strong>{snapshot.pending_count}</strong></div>
      <div className="info-line"><span>复杂任务 8B</span><strong>{snapshot.complex_task==='not_configured'?'待选定实际任务':'已配置'}</strong></div>
      <details><summary>有效世界事实（{snapshot.facts.length}）</summary><pre>{JSON.stringify(snapshot.facts,null,2)}</pre></details>
      <details open><summary>最近事件（{snapshot.events.length}）</summary>
        {snapshot.events.slice(-12).reverse().map((item,index)=><div className="info-line" key={item.event_id??item.observed_ms+'-'+index}>
          <span>{item.kind} · {item.transport}</span>
          <strong>{item.decision} / {item.status}{item.reason?' · '+item.reason:''}</strong>
        </div>)}
      </details>
    </>}
  </section>;
}
