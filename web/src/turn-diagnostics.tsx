import {useState} from 'react';
import type {RealtimeDiagnostics,TurnTimeline} from './api';
import {selectTurnEvidence} from './turn-evidence';
import {turnTtsEvidenceView} from './turn-tts-evidence';
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
  if(status==='published_unknown')return '云端下发完成（等待设备回执）';
  if(status==='device_started')return '设备已开始执行';
  if(status==='device_finished')return '设备执行完成';
  if(status==='device_dropped')return '设备未执行（已丢弃）';
  if(status==='awaiting_audio_anchor')return '等待首帧音频';
  if(status==='awaiting_text_anchor')return '等待回复包含目标词';
  if(status==='awaiting_word_alignment')return '旧协议：等待声学对齐';
  if(status==='call_observed')return '仅观察到工具调用';
  if(status==='queued')return '已排队';
  if(status==='send_unknown')return '发送结果不确定';
  if(status==='rejected_late')return '已错过触发点，动作未发布';
  if(status==='expired_no_text_anchor')return '回复未出现目标词，未发布动作';
  if(status==='expired_no_alignment')return '旧协议：字词对齐超时';
  if(status.startsWith('rejected')||status.startsWith('cancelled')||status.startsWith('expired'))return `未发布：${status}`;
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
  const {timeline,tts:ttsEvidence,prompt,tools,events}=selectTurnEvidence(diagnostics,turnId);
  const ttsView=turnTtsEvidenceView(ttsEvidence);
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
  const weatherCalls=toolCalls.filter(item=>item.name==='get_weather');
  const weatherResults=tools.filter(item=>item.kind==='tool.result'&&item.name==='get_weather');
  const planning=diagnostics?.idle_planning;
  const plans=[...(planning?.continuation.recent??[]),...(planning?.recent??[])]
    .filter(item=>item.source_turn_id===turnId||item.result_turn_id===turnId)
    .sort((a,b)=>a.ts_ms-b.ts_ms);
  const waitingPlan=planning&&turnId&&planning.continuation.turn_id===turnId?
    planning.continuation:null;

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

          <div className="turn-diagnostic-section turn-tts-evidence">
            <div className="section-head"><span className="section-icon">🔊</span><h4>本轮 TTS 文本与表达参数</h4></div>
            <p className="turn-unrecorded">{ttsView.status}</p>
            {ttsView.recorded&&<>
              {([
                ['实际 TTS 音色指纹',ttsEvidence?.voice_fingerprint??'旧记录未采集'],
                ['目标语种 / 方言',`${ttsView.language} / ${ttsView.dialect}`],
                ['情绪分类 / 表达方式',`${ttsView.emotion} / ${ttsView.delivery}`],
                ['风格来源',ttsView.source],
                ['实际 rate / pitch / volume',`${ttsView.rate} / ${ttsView.pitch} / ${ttsView.volume}`],
                ['实际 language_hints',ttsView.languageHints],
                ['音频 / 流式方式',`${ttsView.audio} / ${ttsView.streaming}`],
                ['text_type / enable_ssml',`${ttsView.textType} / ${ttsView.ssml}`],
              ] as const).map(([name,value])=><div className="turn-diagnostic-line" key={name}><span>{name}</span><strong>{value}</strong></div>)}
              <div className="turn-tts-instruction"><strong>实际 instruction</strong><p>{ttsView.instruction}</p></div>
              <div className="turn-tts-text"><strong>实际提交文本 · {ttsView.submittedChars} 字符</strong>
                <p>{ttsView.submittedText?ttsView.submittedText.slice(0,240):'尚无 continue-task 文本提交记录'}</p>
                {ttsView.submittedText.length>240&&<Detail label="展开完整 TTS 提交文本" value={ttsView.submittedText}/>}
              </div>
              <details className="turn-detail">
                <summary><span className="turn-detail-chevron">▶</span>continue-task 文本片段（{ttsView.segmentCount} 个）</summary>
                <small>同一任务共享冻结参数；以下是发送文本的片段，不是独立声学分段，也不代表逐句识别了情绪。</small>
                {ttsView.segments.map((piece,index)=><div className="turn-tts-piece" key={`${piece.index}-${index}`}>
                  <strong>片段 {piece.index} · 字符 {piece.range} · {piece.chars} 字符</strong><p>{piece.text}</p>
                </div>)}
                <small>{ttsView.retention}</small>
              </details>
              {ttsView.error&&<p className="turn-unrecorded">失败信息：{ttsView.error}</p>}
              <Detail label="实际 run-task parameters（JSON）" value={ttsView.parameters}/>
              <Detail label="TTS 任务标识" value={{task_id:ttsView.taskId,segment_id:ttsView.segmentId}}/>
              <small>参数已提交不等于听感已验证；情绪分类为中性也不等于无表情。TTS 云端完成、浏览器试听回执和真机播完证据分开查看。{ttsView.retention}</small>
            </>}
            {!ttsView.recorded&&<small>不借用当前任务或其它回合的 TTS 文本、方言、情绪与参数。</small>}
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
              <strong>{weatherCalls.length?`${weatherCalls.length} 次`:'未见调用证据'}</strong>
            </div>
            {weatherCalls.map(call=>{
              const result=weatherResults.find(item=>item.call_id===call.call_id);
              const output=result?.result as {status?:string;reason?:string}|undefined;
              return <Detail key={call.call_id??call.seq}
                label={`get_weather · ${output?.status??'未见结果'}${output?.reason?' / '+output.reason:''} · ${result?.duration_ms??'—'} ms`}
                value={{arguments:call.arguments,result:result?.result??'无结果记录'}}/>;
            })}
            <small>没有工具记录不等于没有调用：诊断是有界 Session 缓存，重连后旧轮工具明细可能丢失。天气失败须看具体 status/reason。</small>
          </div>

          <div className="turn-diagnostic-section">
            <div className="section-head"><h4>主动追问 / 独立规划</h4></div>
            <div className="turn-diagnostic-line">
              <span className="line-label">本轮来源：</span>
              <strong>{turn?.turn_source==='external'?'外部触发回合':turn?.turn_source==='user'?'用户输入 → Realtime 主模型':'未知'}</strong>
            </div>
            <div className="turn-diagnostic-line">
              <span className="line-label">独立规划：</span>
              <strong>{plans.length?`${plans.length} 次决策记录`:waitingPlan?.status==='planning'?'正在评估':
                '本轮无独立规划调用记录'}</strong>
            </div>
            {waitingPlan&&!plans.length&&<div className="turn-diagnostic-line">
              <span className="line-label">短时续聊准入：</span>
              <strong>{waitingPlan.status} · {waitingPlan.reason??'未记录'}</strong>
            </div>}
            {plans.map((record,index)=><Detail key={`${record.mode}-${record.ts_ms}-${index}`}
              label={`${record.mode==='short_continuation'?'15秒短时续聊':'8C空闲主动交谈'} · ${record.decision??record.status} · ${record.model_ms??'—'} ms`}
              value={{model:planning?.model,thinking_enabled:planning?.thinking_enabled,
                source_turn_id:record.source_turn_id,result_turn_id:record.result_turn_id,
                decision:record.decision,topic:record.topic,status:record.status,
                reason:record.reason,model_ms:record.model_ms,ts_ms:record.ts_ms,
                channel:record.channel}}/>)}
            <small>普通回合内模型自己生成的问句不代表 LangGraph 审核。仅标记实际发生的独立规划；历史记录最多保留32次/类型，Runtime重启后不保留。</small>
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
            <small>云端动作以成功发布到 Room 为下发完成，不等待设备回执；字级动作由设备按流 epoch 与实际播放采样点执行，物理动作仍待核验。</small>
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
