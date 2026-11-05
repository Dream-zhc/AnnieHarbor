import {useCallback,useEffect,useRef,useState} from 'react';
import {gsap} from 'gsap';
import {useGSAP} from '@gsap/react';
import {api,robotPath} from './api';
import {consolidationLabel,importanceLabel,memoryClock,memoryDate,memoryOrigin,memoryStrength,memoryText,memoryType,
  visibleMemory} from './memory-display';
import type {MemoryFact,MemoryInsight,MemoryScope} from './memory-display';
import './memory-panel.css';

type List={status:string;memories:MemoryFact[]};
type Tab=MemoryScope|'insight';
const TABS:[Tab,string][]=[['shared','长期通用'],['persona','当前角色'],['chat','本次会话'],['insight','想起与整理']];
const LIST_POLL_MS=15_000;
const INSIGHT_POLL_MS=5_000;
const message=(error:unknown)=>error instanceof Error?error.message:String(error);
const scopeName=(scope:MemoryScope)=>scope==='chat'?'本次会话':scope==='shared'?'长期通用':'当前角色';
const stamp=(ms:number)=>new Date(ms).toDateString()===new Date().toDateString()?memoryClock(ms):memoryDate(ms);

function InsightView({insight}:{insight:MemoryInsight|null}){
  if(!insight)return <div className="memory-empty-state">读取中</div>;
  const recalls=insight.recalls??[];
  const consolidations=insight.consolidations??[];
  return <div className="memory-insight">
    <section className="memory-insight-card">
      <h3>整理进度</h3>
      <p className="memory-insight-muted">安静 {insight.idle_seconds??120} 秒或满 {insight.max_turns??24} 句后整理进长期记忆</p>
      {insight.pending?<p>有 <strong>{insight.pending.turns}</strong> 句话等着整理（{stamp(insight.pending.since_ms)} 起，最后一句 {stamp(insight.pending.last_ms)}）</p>
        :<p>没有待整理的对话</p>}
      {insight.error&&<p className="danger-text">上次整理出错：{insight.error}，会自动重试</p>}
    </section>

    <section className="memory-insight-card">
      <h3>本次对话前情</h3>
      {insight.recap?<>
        <p className="memory-recap-text">{insight.recap.text}</p>
        <p className="memory-insight-muted">已折叠 {insight.recap.folded} 轮较早的对话 · {stamp(insight.recap.updated_ms)} 更新</p>
      </>:<p className="memory-insight-muted">对话还短，最近几轮她都看得到</p>}
    </section>

    <section className="memory-insight-card">
      <h3>最近想起<span className="memory-insight-count">已为 {insight.recall_turns??0} 句话自动检索</span></h3>
      {recalls.length===0&&<p className="memory-insight-muted">还没想起过什么</p>}
      {recalls.map((entry,index)=><div key={entry.at_ms+':'+index} className="memory-insight-entry">
        <div className="memory-insight-head">
          <span className="memory-insight-time">{stamp(entry.at_ms)}</span>
          <span className={'memory-tag '+(entry.source==='tool'?'memory-source-tool':'memory-source-turn')}>
            {entry.source==='tool'?'她主动查':'每句话自动'}</span>
          <span className="memory-insight-query">“{entry.query}”</span>
        </div>
        {entry.hits.length===0?<p className="memory-insight-muted">没查到</p>:<ul className="memory-insight-list">
          {entry.hits.map(hit=><li key={hit.id}>
            <span className="memory-insight-text">{memoryText(hit.text)}</span>
            <span className="memory-insight-why" title="对上的词和匹配分">
              {hit.why.map((word,position)=><span key={word+position} className="memory-why-word">{word}</span>)}
              <span className="memory-insight-score">{hit.score.toFixed(2)}</span>
            </span>
          </li>)}
        </ul>}
      </div>)}
    </section>

    <section className="memory-insight-card">
      <h3>最近整理</h3>
      {consolidations.length===0&&<p className="memory-insight-muted">还没整理过</p>}
      {consolidations.map((entry,index)=>{
        const results=entry.results??[];
        return <div key={entry.at_ms+':'+index} className="memory-insight-entry">
          <div className="memory-insight-head">
            <span className="memory-insight-time">{stamp(entry.at_ms)}</span>
            {entry.error?<span className="danger-text">整理失败（{entry.error}），已跳过这批对话</span>
              :<span className="memory-insight-muted">{entry.turns??0} 句话 · 参考 {entry.related??0} 条旧记忆 · {((entry.ms??0)/1000).toFixed(1)} 秒</span>}
          </div>
          {!entry.error&&(results.length?<ul className="memory-insight-list">
            {results.map((result,position)=><li key={position}>
              <span className={'memory-tag memory-result-tag result-'+result.result}>{consolidationLabel[result.result]??result.result}</span>
              <span className="memory-insight-text">{memoryText(result.text)}</span>
            </li>)}
          </ul>:<p className="memory-insight-muted">这段对话没有值得记的事</p>)}
        </div>;
      })}
    </section>
  </div>;
}

export function MemoryPanel({robotId,personaId,chatId,generation,onClose}:{
  robotId:string;personaId:string;chatId:string;generation:number;onClose:()=>void;
}){
  const panelRef=useRef<HTMLDivElement>(null);
  useGSAP(()=>{
    const media=gsap.matchMedia();
    media.add('(prefers-reduced-motion: no-preference)',()=>{
      gsap.fromTo('.memory-dialog',
        {scale:0.95,y:12,opacity:0},
        {scale:1,y:0,opacity:1,duration:0.22,ease:'power2.out',clearProps:'all'}
      );
    });
    return ()=>media.revert();
  },{scope:panelRef});

  const [tab,setTab]=useState<Tab>('shared');
  const [items,setItems]=useState<MemoryFact[]>([]);
  const [insight,setInsight]=useState<MemoryInsight|null>(null);
  const [state,setState]=useState('读取中');
  const [failure,setFailure]=useState('');
  const [busy,setBusy]=useState(false);
  const [kind,setKind]=useState('semantic');
  const [key,setKey]=useState('');
  const [text,setText]=useState('');
  const [ttlHours,setTtlHours]=useState(0);
  const [editing,setEditing]=useState<string|null>(null);
  const [draft,setDraft]=useState('');
  const [creating,setCreating]=useState(false);
  const [search,setSearch]=useState('');
  const [filterType,setFilterType]=useState('all');
  const [withOutdated,setWithOutdated]=useState(false);

  const listing=tab!=='insight';
  const scope:MemoryScope=tab==='insight'?'shared':tab;
  const base=robotPath(robotId)+(scope==='chat'?'/chat-memories':'/memories');
  const identity={chat_id:chatId,persona_id:personaId,generation};

  const refresh=useCallback(async()=>{
    const result=await api<List>(base+'?include_deleted=true');
    setItems(result.memories);setState(result.status);
  },[base]);

  useEffect(()=>{
    if(!listing)return;
    let active=true;
    const load=()=>api<List>(base+'?include_deleted=true').then(result=>{
      if(active){setItems(result.memories);setState(result.status);}
    });
    setState('读取中');setItems([]);setFailure('');
    load().catch(error=>{if(active)setFailure(message(error));});
    const timer=window.setInterval(()=>{load().catch(()=>undefined);},LIST_POLL_MS);
    return()=>{active=false;window.clearInterval(timer);};
  },[base,listing,personaId,chatId,generation]);

  useEffect(()=>{
    if(listing)return;
    let active=true;
    const load=()=>api<MemoryInsight>(robotPath(robotId)+'/memory-insight').then(result=>{
      if(active){setInsight(result);setFailure('');}
    }).catch(error=>{if(active)setFailure(message(error));});
    void load();
    const timer=window.setInterval(()=>{void load();},INSIGHT_POLL_MS);
    return()=>{active=false;window.clearInterval(timer);};
  },[listing,robotId]);

  async function mutate(action:()=>Promise<unknown>){
    setBusy(true);setFailure('');
    try{await action();await refresh();setEditing(null);}
    catch(error){setFailure(message(error));}
    finally{setBusy(false);}
  }

  const now=Date.now();
  const valid=items.filter(item=>visibleMemory(item,scope,now,withOutdated&&scope!=='chat'));
  const outdatedCount=scope==='chat'?0:items.filter(item=>item.status==='outdated'&&visibleMemory(item,scope,now,true)).length;
  const needle=search.trim();
  const visible=valid.filter(item=>(filterType==='all'||item.kind===filterType)&&
    (!needle||memoryText(item.text).includes(needle)||item.text.includes(needle)||item.fact_key.includes(needle)))
    .sort((a,b)=>Number(a.status==='outdated')-Number(b.status==='outdated'));
  const activeCount=valid.filter(item=>item.status==='active').length;
  const changeTab=(next:Tab)=>{
    setTab(next);if(next!=='insight')setKind(next==='persona'?'persona':'semantic');setEditing(null);
    setCreating(false);setSearch('');setFilterType('all');setFailure('');
  };

  return <div className="modal-backdrop" ref={panelRef} role="presentation" onMouseDown={e=>{if(e.target===e.currentTarget)onClose();}}>
    <section className="dialog memory-dialog" role="dialog" aria-modal="true" aria-labelledby="memory-title">
      <div className="memory-dialog-header">
        <h2 id="memory-title">
          <span>记忆管理</span>
          <span className="memory-robot-badge">{robotId}</span>
        </h2>
        <p>{personaId} / {chatId}</p>
        {listing&&<button type="button" className="memory-add-button" onClick={()=>{setCreating(true);setFailure('');}}
          aria-label="新增记忆" title="新增记忆">＋</button>}
        <button type="button" className="dialog-close-btn" onClick={onClose} aria-label="关闭弹窗">×</button>
      </div>

      <div className="memory-scope-bar">
        <div className="memory-scope-tabs" role="tablist" aria-label="记忆作用范围">
          {TABS.map(([value,label])=><button key={value} type="button" role="tab" aria-selected={tab===value}
            className={'memory-scope-tab '+(tab===value?'active':'')}
            onClick={()=>changeTab(value)}>{label}</button>)}
        </div>
        <div className="memory-status-line" role="status">
          <span className={'memory-status-dot '+(failure||(listing&&state!=='ready')?'memory-status-idle':'')}/>
          {listing?<>
            <span>{state==='ready'?'已同步':state}</span>
            <span>·</span>
            <span><strong>{activeCount}</strong> 条记忆</span>
          </>:<span>每 {INSIGHT_POLL_MS/1000} 秒刷新</span>}
        </div>
      </div>

      {failure&&<div style={{padding:'8px 24px 0'}}><p role="alert" className="danger-text" style={{margin:0}}>{failure}</p></div>}

      <div className="memory-body">
        {listing?<>
          <div className="memory-toolbar">
            <input aria-label="搜索记忆" value={search} onChange={e=>setSearch(e.target.value)} placeholder="搜索记忆"/>
            {scope==='persona'&&<select aria-label="筛选记忆类型" value={filterType} onChange={e=>setFilterType(e.target.value)}>
              <option value="all">全部类型</option><option value="persona">关系</option><option value="episodes">经历</option>
            </select>}
            {scope!=='chat'&&<label className="memory-toggle" title="被后来的说法取代的旧记忆；她不会再用，但可以恢复">
              <input type="checkbox" checked={withOutdated} onChange={e=>setWithOutdated(e.target.checked)}/>
              <span>含已过时{outdatedCount?`（${outdatedCount}）`:''}</span>
            </label>}
          </div>
          <div className="memory-list">
            {visible.length===0&&<div className="memory-empty-state">{valid.length?'没有匹配的记忆':'暂无记忆'}</div>}
            {visible.map(item=>{
              const outdated=item.status==='outdated';
              const newer=outdated&&item.superseded_by?items.find(other=>other.id===item.superseded_by):undefined;
              const strength=memoryStrength(item);
              return <article key={item.id} className={'memory-item-card'+(outdated?' memory-item-outdated':item.faded?' memory-item-faded':'')}>
                <div className="memory-item-top">
                  <div className="memory-badges">
                    <span className="memory-tag">{scopeName(scope)}</span>
                    <span className="memory-tag memory-type-tag">{memoryType(item)}</span>
                    {scope!=='chat'&&<span className={'memory-tag memory-importance-tag'+((item.importance??0.5)>=0.6?' important':'')}>
                      {importanceLabel(item.importance)}</span>}
                    {outdated&&<span className="memory-tag memory-outdated-tag">已过时</span>}
                    {!outdated&&item.faded&&<span className="memory-tag memory-faded-tag"
                      title="很久没想起又不太重要：不再自动想起，用户问起时仍能查到">淡出</span>}
                  </div>
                </div>

                {editing===item.id?(
                  <textarea className="memory-edit-textarea" aria-label="修改记忆" value={draft} maxLength={300} onChange={e=>setDraft(e.target.value)}/>
                ):(
                  <p className="memory-content-text">{memoryText(item.text)}</p>
                )}
                {outdated&&<p className="memory-superseded">{newer&&newer.status!=='deleted'&&newer.text
                  ?<>被新记忆取代：{memoryText(newer.text)}</>:'被后来的说法取代'}</p>}

                {scope!=='chat'&&<div className="memory-stats" title="遗忘曲线：越重要、被想起的天数越多，忘得越慢">
                  <span className="memory-strength" aria-hidden="true"><span style={{width:strength+'%'}}/></span>
                  <span>记得 {strength}%</span>
                  <span>· {item.hits?`想起 ${item.hits} 次（${item.hit_days??0} 天）`:'还没被想起过'}</span>
                  {memoryDate(item.last_hit_ms)&&<span>· 上次 {memoryDate(item.last_hit_ms)}</span>}
                </div>}

                <div className="memory-meta-info">
                  <span>{memoryOrigin(item)}</span>
                  {memoryDate(item.source_created_ms)&&<span>· {memoryDate(item.source_created_ms)}</span>}
                  {item.expires_at_ms&&<span>· 到期 {memoryDate(item.expires_at_ms)}</span>}
                </div>

                <div className="memory-actions">
                  <details className="memory-technical"><summary>详情</summary>
                    <div>事实键：{item.fact_key}</div>
                    <div>来源 ID：{item.source_turn_id||'手动添加'}</div>
                    <div>版本：{item.version}{item.importance!=null&&<> · 重要度：{item.importance.toFixed(2)}</>}</div>
                  </details>
                  {outdated?(
                    <button type="button" className="secondary-button" disabled={busy}
                      onClick={()=>void mutate(()=>api(base+'/'+encodeURIComponent(item.id)+'/restore','POST',{version:item.version}))}>恢复</button>
                  ):editing===item.id?(
                    <>
                      <button type="button" className="quiet-button" onClick={()=>setEditing(null)}>取消</button>
                      <button type="button" className="primary-button" disabled={busy||!draft.trim()} onClick={()=>void mutate(()=>api(base+'/'+encodeURIComponent(item.id),'PATCH',{version:item.version,text:draft.trim(),...(scope==='chat'?identity:{})}))}>保存更正</button>
                    </>
                  ):(
                    <button type="button" className="secondary-button" disabled={busy} onClick={()=>{setEditing(item.id);setDraft(item.text);}}>更正</button>
                  )}
                  <button type="button" disabled={busy} className="danger-button" onClick={()=>{
                    if(window.confirm(scope==='chat'?'仅移除本 Chat 记忆；聊天原文保留。':'仅遗忘这条长期记忆；聊天原文保留。'))
                      void mutate(()=>api(base+'/'+encodeURIComponent(item.id),'DELETE',{version:item.version,...(scope==='chat'?identity:{})}));
                  }}>遗忘</button>
                </div>
              </article>;
            })}
          </div>
        </>:<InsightView insight={insight}/>}
      </div>

      {creating&&listing&&<div className="memory-create-backdrop" role="presentation"
        onMouseDown={e=>{if(e.target===e.currentTarget&&!busy)setCreating(false);}}>
        <form className="memory-form-card" role="dialog" aria-modal="true" aria-labelledby="memory-create-title"
          onKeyDown={e=>{if(e.key==='Escape'&&!busy){e.stopPropagation();setCreating(false);}}}
          onSubmit={e=>{e.preventDefault();void mutate(async()=>{
          await api(base,'POST',scope!=='chat'?{kind,key:key.trim(),text:text.trim()}:{
            key:key.trim(),text:text.trim(),...identity,
            expires_at_ms:ttlHours>0?Date.now()+ttlHours*3_600_000:null,
          });setKey('');setText('');setCreating(false);
        });}}>
          <div className="memory-form-heading">
            <h3 id="memory-create-title">新增记忆 · {scopeName(scope)}</h3>
            <button type="button" className="memory-create-close" aria-label="关闭新增记忆" disabled={busy} onClick={()=>setCreating(false)}>×</button>
          </div>
          {scope==='persona'&&<div className="memory-form-row">
            <label>记忆类型</label>
            <select aria-label="记忆类型" value={kind} onChange={e=>setKind(e.target.value)}>
              <option value="persona">关系</option>
              <option value="episodes">经历</option>
            </select>
          </div>}
          <div className="memory-form-row">
            <label>事实键</label>
            <input autoFocus aria-label="事实键" placeholder="如 friend_visit" maxLength={64} value={key} onChange={e=>setKey(e.target.value)}/>
          </div>
          <div className="memory-form-row">
            <label>记忆内容</label>
            <textarea rows={2} aria-label="记忆内容" placeholder="记忆内容" maxLength={300} value={text} onChange={e=>setText(e.target.value)}/>
          </div>
          {scope==='chat'&&<div className="memory-form-row">
            <label>有效期（小时，0=不限）</label>
            <input aria-label="会话记忆有效小时" type="number" min="0" max="8760" value={ttlHours} onChange={e=>setTtlHours(Number(e.target.value))}/>
          </div>}
          <div className="memory-form-actions">
            <button type="button" className="secondary-button" disabled={busy} onClick={()=>setCreating(false)}>取消</button>
            <button className="primary-button" disabled={busy||state!=='ready'||!key.trim()||!text.trim()||(scope==='chat'&&(!Number.isFinite(ttlHours)||ttlHours<0||ttlHours>8760))} type="submit">保存记忆</button>
          </div>
        </form>
      </div>}

      <div className="memory-dialog-footer">
        <button type="button" className="secondary-button" onClick={onClose}>关闭</button>
      </div>
    </section>
  </div>;
}
