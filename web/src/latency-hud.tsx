import {memo} from 'react';
import type {Robot,SpeechStatus} from './api';
import {latencyMetrics} from './latency-metrics';

type Realtime=NonNullable<Robot['realtime']>;
const ms=(value:number|null|undefined)=>value==null?'—':`${Math.round(value)} ms`;

/** Diagnostics are scoped to one robot; no microphone/audio-clock state lives here. */
export const LatencyHud=memo(function LatencyHud({realtime,speech}:{
  realtime?:Realtime;speech?:SpeechStatus;
}){
  const percentiles=realtime?.latency_percentiles?.endpoint_to_speaker_start_ms;
  const {endToEnd,speakerStart,firstAudio,firstRoom}=latencyMetrics(realtime,speech);
  return <section className="hud-section" aria-label="延迟瀑布图">
    <div className="hud-heading"><h2>语音响应延迟</h2></div>
    <div className="metrics-grid">
      <div className="metric-card hero-metric">
        <span>模型停声回调 → 扬声器起播回执</span>
        <strong>{ms(endToEnd)}</strong>
        <small>{speakerStart==null?'等待设备回执':endToEnd==null?'缺少停声事件':'设备自报 · 非声学测量'}</small>
      </div>
      <div className="metric-card"><span>TTS 首音频</span><strong>{ms(firstAudio)}</strong></div>
      <div className="metric-card"><span>首音频 → Room 首帧</span><strong>{ms(firstRoom)}</strong></div>
    </div>
    {percentiles?.samples?<small>设备回执 P50 / P95（{percentiles.samples}轮）：{ms(percentiles.p50_ms)} / {ms(percentiles.p95_ms)}</small>:null}
  </section>;
});
