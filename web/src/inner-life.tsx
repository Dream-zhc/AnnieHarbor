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
  return <details className="hud-section inspector-panel speech-expression-panel inner-life-panel">
    <summary className="speech-exp-summary">
      <span className="speech-summary-title">内心 · 心情 / 今天 / 动作</span>
      <span className="speech-summary-badge">{view.badge}</span>
    </summary>
    <div className="speech-exp-body">
      <section className="speech-subcard" aria-label="此刻的心情">
        <div className="speech-kv-list">
          <p className="speech-kv-line"><span className="speech-k">心情</span><span className="speech-v">{view.mood}</span></p>
          <p className="speech-kv-line"><span className="speech-k">精力</span><span className="speech-v">{view.energy}</span></p>
          <p className="speech-kv-line"><span className="speech-k">心情值</span><span className="speech-v">{view.valence}</span></p>
          <p className="speech-kv-line"><span className="speech-k">上一轮判断</span><span className="speech-v">{view.appraisal}</span></p>
          <p className="speech-kv-line"><span className="speech-k">当前对话</span><span className="speech-v">{view.conversation}</span></p>
          {view.failures&&<p role="status" className="speech-alert-msg">{view.failures}</p>}
        </div>
      </section>
      <section className="speech-subcard" aria-label="今天的真实经历">
        <div className="inner-life-day-title">今天的真实经历（北京时间）</div>
        {view.day.length?<ol className="inner-life-day">
          {view.day.map((row,index)=><li key={index}>
            <span className="inner-life-time">{row.time}</span>
            <span aria-hidden="true">{row.icon}</span>
            <span>{row.text}</span>
          </li>)}
        </ol>:<p className="inner-life-empty">{view.ready?'今天还没有记下的事':'—'}</p>}
      </section>
      <section className="speech-subcard" aria-label="记得的人和事">
        <div className="speech-kv-list">
          <p className="speech-kv-line"><span className="speech-k">眼前的人</span><span className="speech-v">{view.partner}</span></p>
          <p className="speech-kv-line"><span className="speech-k">对TA的印象</span><span className="speech-v">{view.impression}</span></p>
          <p className="speech-kv-line"><span className="speech-k">她最近</span><span className="speech-v">{view.lately}</span></p>
        </div>
        <div className="inner-life-day-title">待续话题</div>
        {view.threads.length?<ol className="inner-life-day">
          {view.threads.map((line,index)=><li key={index}><span aria-hidden="true">📌</span><span>{line}</span></li>)}
        </ol>:<p className="inner-life-empty">没有还没结果的事</p>}
      </section>
      <section className="speech-subcard" aria-label="心里有话与动作">
        <div className="speech-kv-list">
          <p className="speech-kv-line"><span className="speech-k">心里有话</span><span className="speech-v">{view.thought}</span></p>
          <p className="speech-kv-line"><span className="speech-k">交互动作</span><span className="speech-v">{view.motions}</span></p>
        </div>
        {view.motionLines.length>0&&<ol className="inner-life-day">
          {view.motionLines.map((line,index)=><li key={index}><span aria-hidden="true">🤖</span><span>{line}</span></li>)}
        </ol>}
      </section>
      <div className="speech-kv-list">
        <p className="speech-kv-line"><span className="speech-k">会话同步</span><span className="speech-v">{view.session}</span></p>
        <p className="speech-kv-line"><span className="speech-k">对话摘要</span><span className="speech-v">{view.summarizer}</span></p>
      </div>
    </div>
  </details>;
}
