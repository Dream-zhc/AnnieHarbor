import {useEffect,useState} from 'react';
import {api,robotPath,sessionKey,type Session,type SpeechExpression} from './api';
import {speechExpressionView,speechResponseIsCurrent} from './speech-expression-state';

function useExpression(robotId:string,session:Session|null,enabled:boolean,interval:number){
  const [data,setData]=useState<SpeechExpression|null>(null);
  const [status,setStatus]=useState('读取中');
  const valid=session?.robot_id===robotId?session:null;
  const key=valid?sessionKey(valid):'';
  useEffect(()=>{
    let disposed=false,controller:AbortController|null=null,timer:ReturnType<typeof setTimeout>|undefined;
    setData(null);
    if(!enabled||!valid){setStatus('等待当前会话');return;}
    const identity=valid;
    const load=async()=>{
      controller=new AbortController();
      const active=controller;
      const timeout=setTimeout(()=>active.abort(),6000);
      try{
        const result=await api<SpeechExpression>(robotPath(robotId)+'/speech-expression','GET',undefined,active.signal);
        if(!disposed&&speechResponseIsCurrent(result,identity,0,0)){
          setData(result);setStatus('已同步');
        }
      }catch{
        if(!disposed){setStatus('读取失败');setData(null);}
      }finally{
        clearTimeout(timeout);
        if(!disposed)timer=setTimeout(()=>void load(),interval);
      }
    };
    void load();
    return()=>{disposed=true;controller?.abort();if(timer)clearTimeout(timer);};
  },[robotId,key,enabled,interval]);
  return {data:valid&&data&&speechResponseIsCurrent(data,valid,0,0)?data:null,status};
}

/** Read-only preference location: settings are changed through normal conversation/FC. */
export function RoleLanguagePreference({robotId,session}:{robotId:string;session:Session|null}){
  const {data}=useExpression(robotId,session,true,8000);
  const view=data?speechExpressionView(data):null;
  const chat=view?.chatPreference;
  const robot=view?.robotPreference;
  const label=chat&&chat!=='未设置'?'会话：'+chat:
    robot&&robot!=='未设置'?'机器人：'+robot:'自动跟随';
  const detail='会话：'+(chat??'未读取')+'；机器人：'+(robot??'未读取')+
    '。直接说“这个会话用英语”或“以后用英语”即可调整。';
  return <span className="room-tag" title={detail}>语言偏好：{view?label:'读取中'}</span>;
}

/** Diagnostics only. Does not alter model/sidecar state or persist preferences. */
export function SpeechExpressionPanel({robotId,session}:{robotId:string;session:Session|null}){
  const [open,setOpen]=useState(false);
  const {data,status}=useExpression(robotId,session,open,2000);
  const view=data?speechExpressionView(data):null;
  return <details className="hud-section inspector-panel speech-expression-panel"
    onToggle={event=>setOpen(event.currentTarget.open)}>
    <summary className="speech-exp-summary">
      <span className="speech-summary-title">语音状态 · 语种 / TTS</span>
      <span className="speech-summary-badge">{view?view.ttsTask:open?status:'查看'}</span>
    </summary>
    <div className="speech-exp-body">
      {view?<>
        {view.ttsText&&<div className="speech-quote-card">
          <div className="speech-quote-head">
            <span className="speech-quote-tag">💬 最新播报</span>
            <span className="speech-task-status-pill">{view.ttsStage}</span>
          </div>
          <div className="speech-quote-text">“{view.ttsText}”</div>
        </div>}
        <section className="speech-subcard" aria-label="核心声学状态">
          <div className="speech-kv-list">
            <p className="speech-kv-line"><span className="speech-k">输入识别</span><span className="speech-v">{view.inputLanguage} · {view.inputSource}</span></p>
            <p className="speech-kv-line"><span className="speech-k">输出语种</span><span className="speech-v">{view.ttsLanguage}</span></p>
            <p className="speech-kv-line"><span className="speech-k">整轮情绪</span><span className="speech-v">{view.ttsEmotion}</span></p>
            <p className="speech-kv-line"><span className="speech-k">表达模式</span><span className="speech-v">{view.ttsDelivery}</span></p>
            <p className="speech-kv-line"><span className="speech-k">语速</span><span className="speech-v">{view.ttsRate}</span></p>
            <p className="speech-kv-line speech-instruction-row"><span className="speech-k">TTS 指令</span><span className="speech-v speech-instruction-val" title={view.ttsInstruction}>{view.ttsInstruction}</span></p>
            {view.ttsError&&<p role="status" className="speech-alert-msg">TTS 错误：{view.ttsError}</p>}
          </div>
        </section>
        <details className="speech-dev-details">
          <summary className="speech-dev-summary">
            <span>🔬 底层协议与排障详情</span>
            <span className="speech-dev-state-badge">{data?.llm_expression_enabled?'协议启用':'默认双向'}</span>
          </summary>
          <div className="speech-dev-body">
            <div className="speech-kv-list">
              <p className="speech-kv-line"><span className="speech-k">TTS 状态</span><span className="speech-v">{view.ttsTask} · {view.ttsStage}</span></p>
              <p className="speech-kv-line"><span className="speech-k">回复目标</span><span className="speech-v">{view.target} · {view.targetSource}</span></p>
              <p className="speech-kv-line"><span className="speech-k">回复文本推断</span><span className="speech-v">{view.llmActual==='未知'?'未识别':view.llmActual}</span></p>
              <p className="speech-kv-line"><span className="speech-k">LLM 情绪协议</span><span className="speech-v">{view.llmExpressionStatus} · {view.llmExpressionMood}</span></p>
              <p className="speech-kv-line speech-instruction-row"><span className="speech-k">LLM 原始指令</span><span className="speech-v speech-instruction-val">{view.llmRawInstruction}</span></p>
              <p className="speech-kv-line speech-instruction-row"><span className="speech-k">LLM 标签记录</span><span className="speech-v speech-instruction-val">{view.llmControlLog}</span></p>
              {view.llmExpressionWarnings&&<p className="speech-kv-line"><span className="speech-k">拒绝原因</span><span className="speech-v">{view.llmExpressionWarnings}</span></p>}
              <p className="speech-kv-line"><span className="speech-k">混合合成</span><span className="speech-v">{view.mixedStatus}</span></p>
              {data?.speech?.mixed_enabled&&<>
                <p className="speech-kv-line speech-instruction-row"><span className="speech-k">后续句实发</span><span className="speech-v speech-instruction-val">{view.mixedInstruction}</span></p>
                <p className="speech-kv-line"><span className="speech-k">句级标签</span><span className="speech-v">{view.mixedTag}</span></p>
              </>}
            </div>
            <small className="speech-note">{data?.speech?.mixed_enabled?
              '首句保持双向合成，后续句才使用有效的情绪指令；未生成标签时不伪造决策。云端实发不等于扬声器已播放。':
              '目前仍为原双向合成，每轮锁定一次TTS参数；LLM情绪标签只有启用协议后才会生成。'}</small>
          </div>
        </details>
      </>:<small role="status" className="speech-note">{status}</small>}
    </div>
  </details>;
}
