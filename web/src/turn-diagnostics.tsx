import {useState} from 'react';
import type {RealtimeDiagnostics,TurnTimeline} from './api';
import {selectTurnEvidence} from './turn-evidence';
import {CustomSelect} from './custom-select';
import './turn-diagnostics.css';

export type DiagnosticTurn={turn_id:string;user_text:string;turn_source?:'user'|'external';external_prompt?:string|null;status:string;created_ms?:number;
  assistant_generated_text?:string|null;assistant_published_text?:string|null;
  assistant_spoken_text?:string|null;playback_evidence_status?:string};

const text=(value:unknown)=>typeof value==='string'&&value.trim()?value:'未记录';
const pretty=(value:unknown)=>JSON.stringify(value,null,2);
const since=(times:TurnTimeline['times']|undefined,from:string,to:string)=>{
  const a=times?.[from],b=times?.[to];
  return a!=null&&b!=null&&b>=a?`${b-a} ms`:'未记录';
};
const endState=(status?:string)=>{
  if(!status)return '未记录';
  if(status==='published_unknown')return '已执行（已发至 Room）';
  if(status==='awaiting_audio_anchor')return '等待首帧音频';
  if(status==='queued')return '已排队';
  if(status==='send_unknown')return '发送结果不确定';
  if(status.startsWith('rejected')||status.startsWith('cancelled')||status.startsWith('expired'))return `未执行：${status}`;
  return status;
};

function Detail({label,value}:{label:string;value:unknown}){
  return (
    <details className="turn-detail">
      <summary><span className="turn-detail-chevron">▶</span>{label}</summary>
      <pre>{typeof value==='string'?text(value):pretty(value)}</pre>
    </details>
  );
}

export function TurnDiagnostics({turns,diagnostics,latestTurnId,liveUser}:{
  turns:DiagnosticTurn[];diagnostics:RealtimeDiagnostics|null;latestTurnId?:string|null;
  liveUser?:{turnId:string;text:string}|null;
}){
  const [choice,setChoice]=useState('latest');
  const turnId=choice==='latest'?(latestTurnId??turns.at(-1)?.turn_id):choice;
  const turn=turns.find(item=>item.turn_id===turnId);
  const {timeline,prompt,tools,events}=selectTurnEvidence(diagnostics,turnId);
  const user=(liveUser?.turnId===turnId?liveUser?.text:null)||turn?.user_text||timeline?.user_text;
  const toolCalls=tools.filter(item=>item.kind==='tool.called');
  const recall=tools.filter(item=>item.kind==='tool.result'&&item.name==='recall_memory');
  const actions=events.filter(item=>item.kind==='action.state');
  const mouth=events.filter(item=>item.kind==='viseme.aligned');
  const tts=events.filter(item=>item.kind.startsWith('tts.'));
  const browserReceipts=tts.filter(item=>item.kind.startsWith('tts.playback.')&&
    item.source==='browser_test');
  const latestBrowser=browserReceipts.at(-1);
  const duration=timeline?.times;
  const latencyWindow=diagnostics?.latency_window;
  const percentiles=diagnostics?.latency_percentiles;
  const measured=(name:string,start:string,end:string)=>{
    const value=timeline?.durations?.[name];
    return value!=null?`${value} ms`:since(duration,start,end);
  };
  const memoryEvidence=recall.flatMap(item=>{
    const result=item.result as {evidence?:unknown[]}|undefined;
    return Array.isArray(result?.evidence)?result.evidence:[];
  });
  const actionCalls=toolCalls.filter(item=>item.name==='perform_action'||item.name==='set_expression');
  const weatherResults=tools.filter(item=>item.kind==='tool.result'&&item.name==='get_weather');

  return (
    <section className="turn-diagnostic">
      <header className="turn-diagnostic-head">
        <div className="turn-head-title-row">
          <h3>回合诊断</h3>
          <span className="turn-head-badge">{turns.length} 轮记录</span>
        </div>
        <label className="turn-select-label">
          <span>选择回合</span>
          <CustomSelect
            aria-label="选择诊断回合"
            ariaLabel="选择诊断回合"
            value={choice}
            onChange={val=>setChoice(val)}
            options={[
              {value:'latest',label:'最近一轮 (Latest)'},
              ...turns.slice(-30).reverse().map(item=>({
                value:item.turn_id,
                label:`${item.turn_source==='external'?'内部事件':item.user_text.slice(0,36)||item.turn_id} · ${item.status}`
              }))
            ]}
          />
        </label>
      </header>

      {latencyWindow&&<details className="turn-diagnostic-section">
        <summary>Phase 9 · 最近 {latencyWindow.turns}/{latencyWindow.window_capacity} 轮性能统计</summary>
        <div className="turn-diagnostic-line"><span>语音 / 文字 / 外部</span>
          <strong>{latencyWindow.by_input.audio??0} / {latencyWindow.by_input.text??0} / {latencyWindow.by_input.external??0}</strong></div>
        <div className="turn-diagnostic-line"><span>完成 / 取消 / 失败 / 未结束</span>
          <strong>{latencyWindow.statuses.completed??0} / {latencyWindow.statuses.cancelled??0} / {latencyWindow.statuses.failed??0} / {latencyWindow.statuses.running??0}</strong></div>
        <div className="turn-diagnostic-line"><span>已生成但无真机播完回执</span>
          <strong>{latencyWindow.speaker_receipt_missing} 轮（不等于设备未播放）</strong></div>
        {Object.entries(percentiles??{}).map(([name,stats])=><div className="turn-diagnostic-line" key={name}>
          <span>{name}</span><strong>n={stats.samples} · P50 {stats.p50_ms??'—'} / P95 {stats.p95_ms??'—'} ms · 缺失 {stats.missing??'—'}</strong>
        </div>)}
        <small>有界近128轮，未观测不按0ms计算；分位数只取有证据的样本，缺失和失败单列。</small>
      </details>}

      {!turnId ? (
        <div className="turn-empty-state"><p>暂无回合数据</p></div>
      ) : (
        <>
          <div className="turn-diagnostic-id">
            <span className={`turn-id-dot dot-${turn?.status??'running'}`} />
            <span className="turn-status-text">{turn?.status??'进行中'}</span>
            <span className="turn-id-sep">/</span>
            <code className="turn-id-hash">{turnId}</code>
          </div>

          <div className="turn-qa">
            <div className="turn-card turn-card-user">
              <div className="turn-card-head">
                <span className="turn-badge badge-user">{turn?.turn_source==='external'?'内部触发（非用户发言）':'👤 用户输入'}</span>
              </div>
              <p className="turn-card-body">{turn?.turn_source==='external'?'事件触发的模型回合':text(user)}</p>
              {turn?.turn_source==='external'&&<Detail label="Runtime 提交的内部提示（仅诊断）" value={turn.external_prompt}/>}
            </div>

            <div className="turn-card turn-card-assistant">
              <div className="turn-card-head">
                <span className="turn-badge badge-assistant">✨ LLM 生成回复</span>
                {turn?.status==='running'&&!turn.assistant_generated_text&&<span className="turn-streaming-tag">生成中…</span>}
              </div>
              <p className="turn-card-body assistant-body">
                {turn?.status==='running'&&!turn.assistant_generated_text?'回复中…':text(turn?.assistant_generated_text)}
              </p>
            </div>

            <div className="turn-card turn-card-playback">
              <div className="turn-card-head">
                <span className="turn-badge badge-playback">🔊 实际播报</span>
                <span className={`evidence-badge ${turn?.playback_evidence_status==='speaker_reported_drained'?'badge-good':'badge-muted'}`}>
                  真机回执: {turn?.playback_evidence_status??'未记录'}
                </span>
              </div>
              <p className="turn-card-body">{text(turn?.assistant_spoken_text)}</p>
              <small>播放证据：{turn?.playback_evidence_status??'未记录'}</small>
              <small>网页试听回执：{latestBrowser
                ?`${latestBrowser.kind.replace('tts.playback.','')}（仅 browser_test，非真机）`
                :'未记录'}</small>
            </div>
          </div>

          <div className="turn-diagnostic-section">
            <div className="section-head">
              <span className="section-icon">🧠</span>
              <h4>LLM 输入与上下文</h4>
            </div>
            {prompt ? (
              <>
                <Detail label="Session 实际发送的完整 instructions" value={prompt.instructions}/>
                <Detail label={`Session 建立时注入的历史（${prompt.restored_turns?.length??0} 轮）`} value={prompt.restored_turns??[]}/>
                <div className="turn-diagnostic-line">
                  <span className="line-label">注入历史字符：</span>
                  <strong>{prompt.restored_chars_used??'未记录'} / {prompt.restored_char_budget??'未记录'}</strong>
                  <span className="line-sub">；较旧省略：{prompt.restored_older_omitted??'未记录'} 轮</span>
                </div>
                <div className="turn-diagnostic-line">
                  <span className="line-label">Provider 历史窗口：</span>
                  <strong>请求 {prompt.history_turns_requested} / 回执 {prompt.history_turns_confirmed??'未返回'} 轮</strong>
                </div>
                <div className="turn-diagnostic-line">
                  <span className="line-label">Session ID：</span>
                  <code>{prompt.provider_session_id}</code>
                </div>
                <small>这是本轮所属 Session 实际发送的指令与重连历史；云端后续滚动上下文不可读取，不能声称是本轮完整云端输入。</small>
              </>
            ) : (
              <p className="turn-unrecorded">本轮 Session 提示词 / 注入上下文未留存，不展示其他回合的内容。</p>
            )}
          </div>

          <div className="turn-diagnostic-section">
            <div className="section-head">
              <span className="section-icon">💾</span>
              <h4>记忆</h4>
            </div>
            {prompt ? (
              <Detail label={`Session 启动记忆（${prompt.bootstrap_count} 条）`} value={prompt.bootstrap_text||'无'}/>
            ) : (
              <p className="turn-unrecorded">Session 启动记忆未记录</p>
            )}
            <div className="turn-diagnostic-line">
              <span className="line-label">本轮显式召回：</span>
              <strong>
                {recall.length
                  ? `${recall.length} 次，${recall.some(item=>!item.valid)?'含失败 / 拒绝请求，':''}返回 ${memoryEvidence.length} 条证据`
                  : '无调用记录（不等于检索失败）'}
              </strong>
            </div>
            {recall.map(item=>(
              <Detail
                key={item.call_id??item.seq}
                label={`recall_memory · ${item.valid?'已返回':'异常 / 未确认'} · ${item.duration_ms??'—'} ms`}
                value={{arguments:item.arguments,result:item.result}}
              />
            ))}
          </div>

          <div className="turn-diagnostic-section">
            <div className="section-head"><h4>实时天气工具</h4></div>
            <div className="turn-diagnostic-line">
              <span className="line-label">Session 可用性：</span>
              <strong>{!prompt?'未记录':prompt.tool_names.includes('get_weather')?'已配置':'未提供 get_weather'}</strong>
            </div>
            <div className="turn-diagnostic-line">
              <span className="line-label">本轮调用：</span>
              <strong>{weatherResults.length?`${weatherResults.length} 次，有结果`:'无结果记录'}</strong>
            </div>
            {weatherResults.map(item=><Detail key={item.call_id??item.seq}
              label={`get_weather · ${item.valid?'已返回':'失败'} · ${item.duration_ms??'—'} ms`}
              value={{arguments:item.arguments,result:item.result}}/>)}
          </div>

          <div className="turn-diagnostic-section">
            <div className="section-head">
              <span className="section-icon">🎭</span>
              <h4>动作与嘴型</h4>
            </div>
            <div className="turn-diagnostic-line">
              <span className="line-label">动作 / 表情：</span>
              <strong>{actionCalls.length?`${actionCalls.length} 次 Function Calling`:'无调用记录'}</strong>
            </div>
            {actionCalls.map(call=>{
              const result=tools.find(item=>item.kind==='tool.result'&&item.call_id===call.call_id);
              const related=actions.filter(item=>item.call_id===call.call_id);
              const last=related.at(-1);
              return (
                <Detail
                  key={call.call_id??call.seq}
                  label={`${call.name} · ${last?endState(last.status):endState((result?.result as {status?:string}|undefined)?.status)}`}
                  value={{arguments:call.arguments,tool_result:result?.result??'未记录',room_events:related,
                    device_completion:last?.device_completion??'unknown'}}
                />
              );
            })}
            {actions.length>0&&<Detail label={`Room 动作状态（${actions.length} 条）`} value={actions}/>}
            <div className="turn-diagnostic-line">
              <span className="line-label">声学嘴型：</span>
              <strong>{timeline?.viseme_count?`收到 ${timeline.viseme_count} 个 Aligner 音素`:'无音素回执记录'}</strong>
            </div>
            {mouth.length>0&&<Detail label="嘴型对齐证据" value={mouth}/>}
            <small>动作以成功发至 Room 为完成；音素仅证明对齐消息已到达，不代表嘴部物理执行。</small>
          </div>

          <div className="turn-diagnostic-section">
            <div className="section-head">
              <span className="section-icon">⏱️</span>
              <h4>本轮耗时</h4>
            </div>
            {([
              ['起声 → ASR 最终','speech_started_ms','asr_final_ms','speech_to_asr_final_ms'],
              ['语音结束 → ASR 最终','speech_stopped_ms','asr_final_ms','endpoint_to_asr_final_ms'],
              ['LLM 请求 → 首文本','llm_request_ms','llm_first_text_ms','llm_request_to_first_text_ms'],
              ['TTS 提交 → 首 PCM','tts_submit_ms','tts_first_pcm_ms','tts_submit_to_pcm_ms'],
              ['首 PCM → Room 首帧','tts_first_pcm_ms','room_first_publish_ms','first_pcm_to_room_ms'],
              ['Room 首帧 → 扬声器回执','room_first_publish_ms','speaker_started_ms','room_to_speaker_start_ms'],
              ['Room 首帧 → 扬声器播完','room_first_publish_ms','speaker_drained_ms','room_to_speaker_drained_ms'],
              ['Room 首帧 → 网页试听开始','room_first_publish_ms','browser_started_ms',''],
            ] as const).map(([label,start,end,name])=>(
              <div className="turn-diagnostic-metric" key={label}>
                <span>{label}</span>
                <strong>{measured(name,start,end)}</strong>
              </div>
            ))}
            {timeline&&<Detail label="原始时间戳 / 耗时" value={timeline}/>}
            {tts.length>0&&<Detail label="音频 / 播放回执" value={tts}/>}
            <small>仅显示当前回合实际观测时间戳；未记录不填推测数值。</small>
          </div>

          {toolCalls.length>0&&(
            <div className="turn-diagnostic-section">
              <Detail label={`其他工具调用（${toolCalls.length} 次）`} value={tools}/>
            </div>
          )}
        </>
      )}
    </section>
  );
}
