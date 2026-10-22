import {useEffect,useState} from 'react';
import {api,robotPath,sessionKey,type Session,type SpeechExpression} from './api';
import {CustomSelect} from './custom-select';
import {
  REPLY_LANGUAGE_CHOICES,languagePreferenceBody,replyLanguageLabel,replyLanguageSelection,
  replyLanguageValue,speechExpressionView,speechResponseIsCurrent,type PreferenceScope,
} from './speech-expression-state';

function useExpression(robotId:string,session:Session|null,enabled:boolean,interval:number,reload=0){
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
  },[robotId,key,enabled,interval,reload]);
  return {data:valid&&data&&speechResponseIsCurrent(data,valid,0,0)?data:null,status};
}

const requestId=()=>crypto.randomUUID?.()??
  // LAN HTTP is not a secure context: randomUUID is missing there.
  Array.from(crypto.getRandomValues(new Uint8Array(16)),value=>value.toString(16).padStart(2,'0')).join('');

/** Reply language for this Chat or the whole robot. 自动跟随 = no saved preference,
 * so replies follow the user's language; a choice holds whatever the user speaks. */
export function RoleLanguagePreference({robotId,session}:{robotId:string;session:Session|null}){
  const [reload,setReload]=useState(0);
  const {data}=useExpression(robotId,session,true,8000,reload);
  const [scope,setScope]=useState<PreferenceScope|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const key=session?sessionKey(session):'';
  useEffect(()=>{setScope(null);setError('');},[key]);
  const saved=data?.language?.preferences;
  const active:PreferenceScope=scope??(saved?.chat?'chat':saved?.robot?'robot':'chat');
  const value=replyLanguageValue(saved?.[active]);
  // In Chat scope, “auto” means the robot-wide choice (if any) still applies.
  const inherited=active==='chat'&&!saved?.chat&&saved?.robot?saved.robot:null;
  const options=REPLY_LANGUAGE_CHOICES.map(item=>item.value==='auto'&&inherited?
    {...item,label:'沿用机器人（'+replyLanguageLabel(inherited)+'）'}:item);
  if(!options.some(item=>item.value===value))
    options.push({value,label:replyLanguageLabel(saved?.[active])});
  const apply=async(next:string)=>{
    if(!session||!data||busy||next===value)return;
    const choice=replyLanguageSelection(next);
    setScope(active);  // clearing Chat must not jump the view to the robot scope
    setBusy(true);setError('');
    try{
      await api(robotPath(robotId)+'/language-preference',choice?'PUT':'DELETE',
        languagePreferenceBody(session,active,choice,requestId()));
      setReload(n=>n+1);
    }catch(e){setError(e instanceof Error?e.message:String(e));}
    finally{setBusy(false);}
  };
  const detail='本会话：'+replyLanguageLabel(saved?.chat)+'；本机器人：'+replyLanguageLabel(saved?.robot)+
    '。自动跟随：按用户说的语言回复；选定后不管用户说什么都用它回复，本轮明确要求换语言除外。'+
    '本会话设置优先于本机器人设置。'+(error?' 保存失败：'+error:'');
  return <div className="persona-live-switch" role="group" aria-label="回复语言" title={detail}>
    <span>回复语言</span>
    <CustomSelect ariaLabel="回复语言范围" value={active} disabled={!data||busy}
      onChange={next=>setScope(next as PreferenceScope)}
      options={[{value:'chat',label:'本会话'},{value:'robot',label:'本机器人'}]}/>
    <CustomSelect ariaLabel="回复语言" value={value} disabled={!data||!session||busy}
      onChange={next=>void apply(next)} options={options}/>
    {error&&<span role="status" className="quality-note">语言未保存</span>}
  </div>;
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
