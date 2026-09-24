import {useEffect,useRef,useState} from 'react';
import {gsap} from 'gsap';
import {useGSAP} from '@gsap/react';
import {api,robotPath} from './api';
import './action-panel.css';

type Action={id:string;name:string;duration_ms:number;enabled:boolean;category:string};
type Request={robot_id:string;room_id:string;turn_id:string;call_id:string;kind:string;
  value:string;status:string;error:string|null;updated_ms:number};
type State={status:string;enabled:boolean;
  allowlist:string[];actions:Action[];requests:Request[]};
const sample:Record<string,string>={
  agree_micro_nod:'请你点下头',look_left:'你看左边',
  look_right:'你看右边',fast_double_blink:'请你眨两下眼',
};
const status:Record<string,string>={
  awaiting_audio_anchor:'等待当前轮首帧音频',reserved:'已预约',
  queued:'表情已排队',
  published_unknown:'已执行（已发至 Room）',
  send_unknown:'发送结果不确定，禁止盲重试',
  cancelled_before_send:'取消（未发布）',
  expired_no_pcm:'未见首帧音频，已过期',
  rejected_late:'音频已开始，表情未发送',
};

export function ActionPanel({robotId,onClose,onFillText}:{robotId:string;onClose:()=>void;
  onFillText:(text:string)=>void}){
  const panelRef=useRef<HTMLDivElement>(null);
  useGSAP(()=>{
    const media=gsap.matchMedia();
    media.add('(prefers-reduced-motion: no-preference)',()=>{
      gsap.fromTo('.action-dialog',
        {scale:0.95,y:12,opacity:0},
        {scale:1,y:0,opacity:1,duration:0.22,ease:'power2.out',clearProps:'all'}
      );
    });
    return ()=>media.revert();
  },{scope:panelRef});

  const [data,setData]=useState<State|null>(null);
  const [error,setError]=useState('');

  useEffect(()=>{
    let active=true;
    const refresh=()=>void api<State>(robotPath(robotId)+'/actions')
      .then(result=>{if(active){setData(result);setError('');}})
      .catch(reason=>{if(active)setError(reason instanceof Error?reason.message:String(reason));});
    refresh();
    const timer=window.setInterval(refresh,2000);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId]);

  return <div className="modal-backdrop" ref={panelRef} role="presentation"
    onMouseDown={e=>{if(e.target===e.currentTarget)onClose();}}>
    <section className="dialog action-dialog" role="dialog" aria-modal="true"
      aria-labelledby="action-title">
      <div className="action-dialog-header">
        <h2 id="action-title">
          <span>🎭 动作与表情</span>
          <span className="memory-robot-badge">{robotId}</span>
        </h2>
        <button type="button" className="dialog-close-btn" onClick={onClose} aria-label="关闭弹窗">×</button>
      </div>

      <div className="action-body">
        {error&&<p role="alert" className="danger-text">{error}</p>}
        <div className="action-status-card">
          <div>动作发送：<strong>{data?.enabled?'已启用':data?'未启用':'读取中'}</strong> · 执行口径：<strong>发至 Room 即完成</strong></div>
          {!data?.enabled&&<small style={{color:'#94a3b8',lineHeight:1.4}}>启用：ANNIE_ACTIONS_ENABLED=1 · 重启 Runtime</small>}
        </div>

        <div className="action-samples-section">
          <h3>⚡ 快捷动作指令样例</h3>
          <div className="action-samples">
            {data?.allowlist.map(id=>sample[id]&&<button type="button" className="action-sample-chip" key={id}
              onClick={()=>{onFillText(sample[id]);onClose();}}>
              <span>▶</span> {sample[id]} <small style={{opacity:0.7}}>（填入）</small>
            </button>)}
          </div>
        </div>

        <div className="action-records-section">
          <h3>📋 最近动作记录（SQLite）</h3>
          <div className="action-records">
            {!data?.requests.length&&<p style={{color:'#64748b',fontSize:'12px'}}>暂无请求</p>}
            {data?.requests.map(item=><article key={item.robot_id+':'+item.call_id}
              className="action-record-card">
              <p style={{margin:0}}><strong>{item.kind==='action'?item.value:'表情 '+item.value}</strong>
                {' · '}<span style={{color:'#60a5fa'}}>{status[item.status]??item.status}</span></p>
              <small>Room：{item.room_id} · Turn：{item.turn_id} · Call：{item.call_id}</small>
              <small>时间：{new Date(item.updated_ms).toLocaleString()}</small>
              {item.error&&<p role="alert" className="danger-text" style={{margin:'4px 0 0'}}>发送故障：{item.error}</p>}
            </article>)}
          </div>
        </div>

        <details className="inspector-group"><summary style={{cursor:'pointer',fontWeight:600,color:'#cbd5e1'}}>动作目录与本机放行列表</summary>
          <div style={{display:'flex',flexDirection:'column',gap:'4px',marginTop:'6px'}}>
            {data?.actions.map(item=><div key={item.id} style={{fontSize:'11.5px',display:'flex',justifyContent:'space-between',padding:'3px 0',borderBottom:'1px solid rgba(255,255,255,0.04)'}}>
              <span>{item.name} <small style={{color:'#64748b'}}>({item.id})</small></span>
              <span style={{color:data.allowlist.includes(item.id)?'#34d399':'#94a3b8'}}>
                {item.duration_ms}ms · {data.allowlist.includes(item.id)?'本机允许':'未放行'}
              </span>
            </div>)}
          </div>
        </details>
      </div>

      <div className="action-dialog-footer">
        <button type="button" className="secondary-button" onClick={onClose}>关闭</button>
      </div>
    </section>
  </div>;
}
