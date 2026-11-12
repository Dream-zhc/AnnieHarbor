import {useEffect,useState} from 'react';
import {api,robotPath} from './api';
import {innerLifeView,type InnerLife} from './inner-life-state';

/** “内心”: her mood, day, open threads, what is on her mind and her motions, read only. */
export function InnerLifePanel({robotId}:{robotId:string}){
  const [data,setData]=useState<InnerLife|null>(null);
  const [status,setStatus]=useState('读取中');
  useEffect(()=>{
    let disposed=false,controller:AbortController|null=null,timer:ReturnType<typeof setTimeout>|undefined;
    setData(null);
    if(!robotId){setStatus('未选机器人');return;}
    setStatus('读取中');
    const load=async()=>{
      controller=new AbortController();
      const active=controller;
      const timeout=setTimeout(()=>active.abort(),6000);
      try{
        const result=await api<InnerLife>(robotPath(robotId)+'/inner-life','GET',undefined,active.signal);
        if(!disposed&&result.robot_id===robotId){setData(result);setStatus('已同步');}
      }catch{
        if(!disposed){setData(null);setStatus('读取失败');}
      }finally{
        clearTimeout(timeout);
        if(!disposed)timer=setTimeout(()=>void load(),4000);
      }
    };
    void load();
    return()=>{disposed=true;controller?.abort();if(timer)clearTimeout(timer);};
  },[robotId]);
  const view=innerLifeView(data,status);
  const tone=`tone-${view.tone||'none'}`;
  return <details className="hud-section inspector-panel speech-expression-panel inner-life-panel" open>
    <summary className="speech-exp-summary">
      <span className="speech-summary-title">内心</span>
      <span className={`speech-summary-badge inner-badge ${tone}`}>{view.badge}</span>
    </summary>
    <div className="speech-exp-body inner-life-body">
      <section className={`inner-mood ${tone}`} aria-label="此刻的心情">
        <div className="inner-mood-head">
          <span className="inner-mood-face" aria-hidden="true">{view.face}</span>
          <div className="inner-mood-title">
            <strong>{view.moodLabel}</strong>
            {view.moodNote&&<small>{view.moodNote}</small>}
          </div>
        </div>
        <div className="inner-meter" title={view.energy}>
          <span>精力</span>
          <span className="inner-meter-track"><i style={{width:`${view.energyPct??0}%`}}/></span>
          <span className="inner-meter-value">{view.energyPct==null?'—':`${view.energyPct}%`}</span>
        </div>
        <div className="inner-meter inner-valence" title={view.valence}>
          <span>心情</span>
          <span className="inner-meter-track">{view.valencePct!=null&&<b style={{left:`${view.valencePct}%`}}/>}</span>
          <span className="inner-meter-value">{view.valenceText}</span>
        </div>
        {view.failures&&<p role="status" className="speech-alert-msg">{view.failures}</p>}
      </section>
      <div className="inner-chips">
        <span className="inner-chip" title={view.partner}><em>眼前</em>{view.partnerName||'没认出'}</span>
        <span className="inner-chip" title={view.conversation}><em>对话</em>{view.talk}</span>
      </div>
      <section className="inner-card" aria-label="今天的真实经历">
        <header className="inner-card-head">
          <span>今天的经历</span><small>北京时间{view.day.length?` · ${view.day.length} 件`:''}</small>
        </header>
        {view.day.length?<ol className="inner-timeline">
          {view.day.map((row,index)=><li key={index}>
            <time>{row.time}</time>
            <span className="inner-timeline-icon" aria-hidden="true">{row.icon}</span>
            <span>{row.text}</span>
          </li>)}
        </ol>:<p className="inner-life-empty">{view.ready?'今天还没有记下的事':'—'}</p>}
      </section>
      <section className="inner-card" aria-label="记得的人和事">
        <header className="inner-card-head">
          <span>她最近</span>{view.latelyDay&&<small>{view.latelyDay} 的回顾</small>}
        </header>
        {view.latelyDay?<p className="inner-quote">{view.latelyText}</p>:<p className="inner-life-empty">{view.latelyText}</p>}
        {view.partnerName&&<p className="inner-line"><em>对TA的印象</em><span>{view.impression}</span></p>}
        <header className="inner-card-head">
          <span>待续话题</span>{view.threads.length>0&&<small>{view.threads.length} 件</small>}
        </header>
        {view.threads.length?<ul className="inner-threads">
          {view.threads.map((line,index)=><li key={index}>{line}</li>)}
        </ul>:<p className="inner-life-empty">没有还没结果的事</p>}
      </section>
      <details className="inner-more">
        <summary>更多状态 · 心里有话 / 动作 / 同步</summary>
        <dl className="inner-dl">
          <dt>心里有话</dt><dd>{view.thought}</dd>
          <dt>交互动作</dt><dd>{view.motions}</dd>
          <dt>上一轮判断</dt><dd>{view.appraisal}</dd>
          <dt>会话同步</dt><dd>{view.session}</dd>
          <dt>对话摘要</dt><dd>{view.summarizer}</dd>
        </dl>
        {view.motionLines.length>0&&<ol className="inner-timeline inner-motions">
          {view.motionLines.map((line,index)=><li key={index}>
            <span className="inner-timeline-icon" aria-hidden="true">🤖</span><span>{line}</span>
          </li>)}
        </ol>}
      </details>
    </div>
  </details>;
}
