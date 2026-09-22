import {useEffect,useState} from 'react';
import {api,robotPath} from './api';
import './action-panel.css';

type Action={id:string;name:string;duration_ms:number;enabled:boolean;category:string};
type Request={robot_id:string;room_id:string;turn_id:string;call_id:string;kind:string;
  value:string;status:string;error:string|null;updated_ms:number};
type State={status:string;enabled:boolean;social_actions_enabled:boolean;
  allowlist:string[];actions:Action[];requests:Request[]};
const sample:Record<string,string>={
  agree_micro_nod:'请你点下头',look_left:'你看左边',
  look_right:'你看右边',fast_double_blink:'请你眨两下眼',
};
const status:Record<string,string>={
  awaiting_audio_anchor:'等待当前轮首帧音频',reserved:'已预约',
  published_unknown:'已发至Room，设备执行未知',
  send_unknown:'发送结果不确定，禁止盲重试',
  cancelled_before_send:'取消（未发布）',
  expired_no_pcm:'未见首帧音频，已过期',
  rejected_late:'音频已开始，表情未发送',
};
export function ActionPanel({robotId,onClose,onFillText}:{robotId:string;onClose:()=>void;
  onFillText:(text:string)=>void}){
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
  return <div className="modal-backdrop" role="presentation"
    onMouseDown={e=>{if(e.target===e.currentTarget)onClose();}}>
    <section className="dialog action-dialog" role="dialog" aria-modal="true"
      aria-labelledby="action-title">
      <h2 id="action-title">动作与表情 · {robotId}</h2>
      <p>仅通过当前机器人实时对话提出动作；此页不直接控制电机。发送成功不等于物理设备执行或完成。</p>
      {error&&<p role="alert" className="danger-text">{error}</p>}
      <p role="status">真机发送：{data?.enabled?'已显式启用':data?'未启用':'读取中'} ·
        自主轻动作：{data?.social_actions_enabled?'已单独授权':'关闭'} ·
        设备完成回执：未确认</p>
      {!data?.enabled&&<p>启用需在确认真实设备接收端后配置 ANNIE_ACTIONS_ENABLED=1，
        再停止麦克风、安全重启 Runtime。默认仅放行四个轻动作，其他目录项不会自动开放。</p>}
      <div className="action-samples">
        {data?.allowlist.map(id=>sample[id]&&<button type="button" key={id}
          onClick={()=>{onFillText(sample[id]);onClose();}}>{sample[id]}（填入，不发送）</button>)}
      </div>
      <h3>最近执行记录（SQLite）</h3>
      <div className="action-records">
        {!data?.requests.length&&<p>尚无真实请求；预览调用不会伪装成真机记录。</p>}
        {data?.requests.map(item=><article key={item.robot_id+':'+item.call_id}
          className="inspector-group">
          <p><strong>{item.kind==='action'?item.value:'表情 '+item.value}</strong>
            {' · '}{status[item.status]??item.status}</p>
          <small>Room：{item.room_id} · Turn：{item.turn_id} · Call：{item.call_id}</small>
          <small>时间：{new Date(item.updated_ms).toLocaleString()}</small>
          {item.error&&<p role="alert">发送故障：{item.error}；设备最终状态未知。</p>}
        </article>)}
      </div>
      <details><summary>动作目录与本机放行列表</summary>
        <p>目录存在不证明设备具备动作。以下ID与已有config/action_scripts.json一致。</p>
        {data?.actions.map(item=><p key={item.id}>
          {item.name} · {item.id} · {item.duration_ms} ms ·
          {data.allowlist.includes(item.id)?'本机允许':'未放行'}
        </p>)}
      </details>
      <button type="button" className="quiet-button" onClick={onClose}>关闭</button>
    </section>
  </div>;
}
