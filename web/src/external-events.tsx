import {useCallback,useEffect,useRef,useState} from 'react';
import {api,robotPath,type RealtimeEvent,type Session} from './api';

type EventRow={kind:string;event_id:string|null;decision:string;status:string;
  reason:string|null;transport:string;observed_ms:number;expires_at_ms:number;
  chat_id:string;persona_id:string;generation:number;summary:unknown};
type EventSnapshot={facts:unknown[];events:EventRow[];pending_count:number;
  complex_task:string;chat_id:string;persona_id:string;generation:number};
type Reminder={task_id:string;text:string;status:string;reason:string|null;
  due_ms:number;version:number;turn_id:string|null;result:unknown;checkpoint:unknown;
  robot_id:string;chat_id:string;persona_id:string;session_epoch:number;generation:number};

export function ExternalEvents({robotId,session,events}:{robotId:string;session:Session|null;events:RealtimeEvent[]}){
  const [snapshot,setSnapshot]=useState<EventSnapshot|null>(null);
  const [error,setError]=useState('');
  const [refreshIndex,setRefreshIndex]=useState(0);
  const [tasks,setTasks]=useState<Reminder[]>([]);
  const [text,setText]=useState('');
  const [seconds,setSeconds]=useState(30);
  const [busy,setBusy]=useState(false);
  const keyRef=useRef<string|null>(null);
  const policySeq=[...events].reverse().find(e=>e.kind==='event.policy'||e.kind==='task.status')?.seq??0;
  const refresh=useCallback(()=>setRefreshIndex(x=>x+1),[]);
  useEffect(()=>{
    let active=true;
    void Promise.all([
      api<EventSnapshot>(robotPath(robotId)+'/external-events'),
      api<{tasks:Reminder[]}>(robotPath(robotId)+'/reminders')
    ]).then(([result,reminders])=>{
      if(active){setSnapshot(result);setTasks(reminders.tasks);setError('');}
    }).catch(reason=>{if(active)setError(String(reason));});
    return()=>{active=false;};
  },[robotId,session?.session_epoch,session?.generation,policySeq,refreshIndex]);
  async function createReminder(){
    if(!text.trim()||!session||busy)return;
    setBusy(true);
    // crypto.randomUUID may be unavailable on LAN HTTP; getRandomValues works
    // on non-secure contexts too. Preserve the key across network retries.
    const key=keyRef.current??(crypto.randomUUID?.()??
      Array.from(crypto.getRandomValues(new Uint8Array(16)),value=>
        value.toString(16).padStart(2,'0')).join(''));
    keyRef.current=key;
    try{
      await api(robotPath(robotId)+'/reminders','POST',{
        text:text.trim(),delay_ms:seconds*1000,request_key:key});
      keyRef.current=null;setText('');refresh();setError('');
    }catch(reason){setError(String(reason));}finally{setBusy(false);}
  }
  async function control(task:Reminder,action:'pause'|'resume'|'cancel'){
    if(busy)return;
    setBusy(true);
    try{await api(robotPath(robotId)+'/reminders/'+encodeURIComponent(task.task_id)+'/control',
      'POST',{action});refresh();setError('');}
    catch(reason){setError(String(reason));}finally{setBusy(false);}
  }
  const current=snapshot&&session&&snapshot.chat_id===session.chat_id&&
    snapshot.persona_id===session.persona_id&&snapshot.generation===session.generation;
  return <section className="inspector-group" aria-label="Phase 8 外部事件">
    <div className="surface-heading"><h3>外部事件 · Phase 8A</h3>
      <button type="button" className="quiet-button" onClick={refresh}>刷新事实</button></div>
    <p>事件来源、策略、等待或丢弃原因；提交模型不等于已经说出或设备已执行。</p>
    {error&&<p role="alert">{error}</p>}
    {!current?<p>等待当前机器人 / 会话事实</p>:<>
      <div className="info-line"><span>待发声</span><strong>{snapshot.pending_count}</strong></div>
      <section aria-label="单次提醒任务">
        <h3>8B · 单次提醒</h3>
        <p>单次任务持久化到本机 SQLite；到期提交模型≠实际扬声器播出。切换 Chat 后不会向新会话插话。</p>
        <form onSubmit={event=>{event.preventDefault();void createReminder();}}>
          <label>提醒内容<input value={text} maxLength={200} placeholder="例如：提醒我喝水"
            onChange={event=>{setText(event.target.value);keyRef.current=null;}}/></label>
          <label>几秒后提醒<input type="number" min={1} max={86400} value={seconds}
            onChange={event=>{setSeconds(Number(event.target.value));keyRef.current=null;}}/></label>
          <button type="submit" disabled={busy||!text.trim()||seconds<1||seconds>86400}>创建真实提醒</button>
        </form>
        {tasks.map(task=><div key={task.task_id} className="inspector-group">
          <div className="info-line"><span>{task.text}</span><strong>{task.status}</strong></div>
          <small>到期：{new Date(task.due_ms).toLocaleString()} · {task.reason??'无异常'} · {task.turn_id?'已提交模型（播放未知）':'尚无语音提交'}</small>
          <div className="quality-actions">
            {task.status==='scheduled'&&<button disabled={busy} type="button" onClick={()=>void control(task,'pause')}>暂停</button>}
            {task.status==='paused'&&<button disabled={busy} type="button" onClick={()=>void control(task,'resume')}>恢复</button>}
            {['scheduled','paused'].includes(task.status)&&<button disabled={busy} type="button" onClick={()=>void control(task,'cancel')}>取消</button>}
          </div>
          <details><summary>工作流事实／checkpoint</summary><pre>{JSON.stringify({result:task.result,checkpoint:task.checkpoint},null,2)}</pre></details>
        </div>)}
      </section>
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
