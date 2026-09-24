import {useCallback,useEffect,useRef,useState} from 'react';
import {gsap} from 'gsap';
import {useGSAP} from '@gsap/react';
import {api,robotPath} from './api';
import {memoryDate,memoryText,memoryType,visibleMemory} from './memory-display';
import type {MemoryFact,MemoryScope} from './memory-display';
import './memory-panel.css';

type List={status:string;memories:MemoryFact[]};
const message=(error:unknown)=>error instanceof Error?error.message:String(error);

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

  const [scope,setScope]=useState<MemoryScope>('shared');
  const [items,setItems]=useState<MemoryFact[]>([]);
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

  const base=robotPath(robotId)+(scope==='chat'?'/chat-memories':'/memories');
  const identity={chat_id:chatId,persona_id:personaId,generation};

  const refresh=useCallback(async()=>{
    const result=await api<List>(base+'?include_deleted=true');
    setItems(result.memories);setState(result.status);
  },[base]);

  useEffect(()=>{
    let active=true;
    setState('读取中');setItems([]);setFailure('');
    void api<List>(base+'?include_deleted=true').then(result=>{
      if(active){setItems(result.memories);setState(result.status);}
    }).catch(error=>{if(active)setFailure(message(error));});
    return()=>{active=false;};
  },[base,personaId,chatId,generation]);

  async function mutate(action:()=>Promise<unknown>){
    setBusy(true);setFailure('');
    try{await action();await refresh();setEditing(null);}
    catch(error){setFailure(message(error));}
    finally{setBusy(false);}
  }

  const valid=items.filter(item=>visibleMemory(item,scope,Date.now()));
  const visible=valid.filter(item=>(filterType==='all'||item.kind===filterType)&&
    (!search.trim()||memoryText(item.text).includes(search.trim())||item.fact_key.includes(search.trim())));
  const activeCount=valid.length;
  const changeScope=(next:MemoryScope)=>{
    setScope(next);setKind(next==='persona'?'persona':'semantic');setEditing(null);
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
        <button type="button" className="memory-add-button" onClick={()=>{setCreating(true);setFailure('');}}
          aria-label="新增记忆" title="新增记忆">＋</button>
        <button type="button" className="dialog-close-btn" onClick={onClose} aria-label="关闭弹窗">×</button>
      </div>

      <div className="memory-scope-bar">
        <div className="memory-scope-tabs" role="tablist" aria-label="记忆作用范围">
          <button type="button" role="tab" aria-selected={scope==='shared'}
            className={'memory-scope-tab '+(scope==='shared'?'active':'')}
            onClick={()=>changeScope('shared')}>长期通用</button>
          <button type="button" role="tab" aria-selected={scope==='persona'}
            className={'memory-scope-tab '+(scope==='persona'?'active':'')}
            onClick={()=>changeScope('persona')}>当前角色</button>
          <button type="button" role="tab" aria-selected={scope==='chat'}
            className={'memory-scope-tab '+(scope==='chat'?'active':'')}
            onClick={()=>changeScope('chat')}>本次会话</button>
        </div>
        <div className="memory-status-line" role="status">
          <span className={'memory-status-dot '+(failure||state!=='ready'?'memory-status-idle':'')}/>
          <span>{state==='ready'?'已同步':state}</span>
          <span>·</span>
          <span><strong>{activeCount}</strong> 条记忆</span>
        </div>
      </div>

      {failure&&<div style={{padding:'8px 24px 0'}}><p role="alert" className="danger-text" style={{margin:0}}>{failure}</p></div>}

      <div className="memory-body">
        <div className="memory-toolbar">
          <input aria-label="搜索记忆" value={search} onChange={e=>setSearch(e.target.value)} placeholder="搜索记忆"/>
          {scope==='persona'&&<select aria-label="筛选记忆类型" value={filterType} onChange={e=>setFilterType(e.target.value)}>
            <option value="all">全部类型</option><option value="persona">关系</option><option value="episodes">经历</option>
          </select>}
        </div>
        <div className="memory-list">
          {visible.length===0&&<div className="memory-empty-state">{activeCount?'没有匹配的记忆':'暂无记忆'}</div>}
          {visible.map(item=>{
            return <article key={item.id} className="memory-item-card">
              <div className="memory-item-top">
                <div className="memory-badges">
                  <span className="memory-tag">{scope==='chat'?'本次会话':scope==='shared'?'长期通用':'当前角色'}</span>
                  <span className="memory-tag memory-type-tag">{memoryType(item)}</span>
                </div>
              </div>

              {editing===item.id?(
                <textarea className="memory-edit-textarea" aria-label="修改记忆" value={draft} maxLength={300} onChange={e=>setDraft(e.target.value)}/>
              ):(
                <p className="memory-content-text">{memoryText(item.text)}</p>
              )}

              <div className="memory-meta-info">
                <span>{item.source_turn_id?'来自对话':'手动添加'}</span>
                {memoryDate(item.source_created_ms)&&<span>· {memoryDate(item.source_created_ms)}</span>}
                {item.expires_at_ms&&<span>· 到期 {memoryDate(item.expires_at_ms)}</span>}
              </div>

              <div className="memory-actions">
                <details className="memory-technical"><summary>详情</summary>
                  <div>事实键：{item.fact_key}</div>
                  <div>来源 ID：{item.source_turn_id||'手动添加'}</div>
                  <div>版本：{item.version} · 索引：{item.index_state}</div>
                </details>
                {editing===item.id?(
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

      </div>

      {creating&&<div className="memory-create-backdrop" role="presentation"
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
            <h3 id="memory-create-title">新增记忆 · {scope==='chat'?'本次会话':scope==='shared'?'长期通用':'当前角色'}</h3>
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
