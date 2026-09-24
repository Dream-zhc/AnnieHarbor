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
const reminderReason:Record<string,string>={
  waiting_playback:'等待上一轮播放结束',waiting_turn:'等待当前对话结束',
  waiting_realtime:'等待模型重新连接',restart_recovered:'重启后待恢复',
  late_reminder:'补发迟到提醒',missed_confirmation:'错过较久，询问是否还需要',
};
const reminderStatus:Record<string,string>={
  scheduled:'已排期',paused:'已暂停',submitting:'正在提交',
  submitted_unknown:'已提交处理',played:'已播报',spoken:'已播报',
  cancelled:'已取消',expired:'已过期',send_unknown:'提交异常',
};

export function ExternalEvents({robotId,session,events}:{robotId:string;session:Session|null;events:RealtimeEvent[]}){
  const [snapshot,setSnapshot]=useState<EventSnapshot|null>(null);
  const [error,setError]=useState('');
  const [refreshIndex,setRefreshIndex]=useState(0);
  const [tasks,setTasks]=useState<Reminder[]>([]);
  const [showCreate,setShowCreate]=useState(false);
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
      keyRef.current=null;setText('');setShowCreate(false);refresh();setError('');
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
  return <section className="events-panel-content" aria-label="定时提醒与事件">
    <div className="surface-heading events-heading">
      <div className="events-heading-title">
        <h3>定时提醒任务</h3>
        <span className="events-count-pill">{tasks.length}</span>
      </div>
      <div className="surface-heading-actions">
        <button type="button" className={'events-add-btn '+(showCreate?'active':'')}
          onClick={()=>setShowCreate(v=>!v)}
          aria-expanded={showCreate}
          title={showCreate?'收起新建表单':'新建定时提醒'}>
          {showCreate?'✕ 取消':'＋ 新建'}
        </button>
        <button type="button" className="quiet-button" onClick={refresh} title="刷新列表">刷新</button>
      </div>
    </div>
    {error&&<p role="alert" className="error-text">{error}</p>}
    {!current?<p className="empty-copy">等待事件数据就绪…</p>:<>
      {showCreate&&<form className="events-reminder-form" onSubmit={event=>{event.preventDefault();void createReminder();}}>
        <div className="events-create-grid">
          <input className="events-input events-input-text" autoFocus value={text} maxLength={200}
            placeholder="输入提醒内容，例如：提醒我喝水"
            onChange={event=>{setText(event.target.value);keyRef.current=null;}}/>
          <div className="events-delay-wrap">
            <input className="events-input events-input-delay" type="number" min={1} max={86400} value={seconds}
              onChange={event=>{setSeconds(Number(event.target.value));keyRef.current=null;}}/>
            <span className="events-delay-unit">秒后</span>
          </div>
          <button className="primary-button events-create-submit" type="submit" disabled={busy||!text.trim()||seconds<1||seconds>86400}>创建 ↗</button>
        </div>
      </form>}
      {tasks.length>0?<div className="events-tasks-list">
        {tasks.map(task=><div key={task.task_id} className="events-task-card">
          <div className="info-line">
            <span className="task-text">{task.text}</span>
            <strong className={'task-status status-'+task.status}>{reminderStatus[task.status]??task.status}</strong>
          </div>
          <small>到期：{new Date(task.due_ms).toLocaleTimeString()} · {task.reason?(reminderReason[task.reason]??task.reason):'状态正常'} · {task.turn_id?'已提交处理':task.status==='scheduled'&&Date.now()>=task.due_ms?'已到期，等待提交':'等待触发'}</small>
          <div className="quality-actions">
            {task.status==='scheduled'&&<button className="secondary-button" disabled={busy} type="button" onClick={()=>void control(task,'pause')}>暂停</button>}
            {task.status==='paused'&&<button className="secondary-button" disabled={busy} type="button" onClick={()=>void control(task,'resume')}>恢复</button>}
            {['scheduled','paused'].includes(task.status)&&<button className="danger-button" disabled={busy} type="button" onClick={()=>void control(task,'cancel')}>取消</button>}
          </div>
        </div>)}
      </div>:<p className="empty-copy">暂无定时提醒任务</p>}
      <div className="events-facts-section">
        <div className="info-line"><span>待处理发声</span><strong>{snapshot.pending_count}</strong></div>
        <details><summary>有效环境事实 ({snapshot.facts.length})</summary><pre>{JSON.stringify(snapshot.facts,null,2)}</pre></details>
        <details open><summary>最近事件流 ({snapshot.events.length})</summary>
          {snapshot.events.slice(-10).reverse().map((item,index)=><div className="info-line" key={item.event_id??item.observed_ms+'-'+index}>
            <span>{item.kind} · {item.transport}</span>
            <strong>{item.decision} / {item.status}{item.reason?' · '+item.reason:''}</strong>
          </div>)}
        </details>
      </div>
    </>}
  </section>;
}

export function ExternalEventsDialog({robotId,session,events,onClose}:{
  robotId:string;session:Session|null;events:RealtimeEvent[];onClose:()=>void;
}){
  return <div className="modal-backdrop" role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)onClose();}}>
    <div className="dialog events-dialog" role="dialog" aria-modal="true" aria-labelledby="events-dialog-title">
      <button type="button" className="dialog-close-btn" onClick={onClose} aria-label="关闭">×</button>
      <h2 id="events-dialog-title">定时提醒与事件 · {robotId}</h2>
      <ExternalEvents robotId={robotId} session={session} events={events}/>
      <div className="dialog-actions">
        <button className="primary-button" type="button" onClick={onClose}>完成</button>
      </div>
    </div>
  </div>;
}
