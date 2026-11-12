import {memo} from 'react';
import type {Robot,SpeechStatus} from './api';
import {latencyMetrics} from './latency-metrics';

type Realtime=NonNullable<Robot['realtime']>;
const ms=(value:number|null|undefined)=>value==null?'—':`${Math.round(value)} ms`;

/** Diagnostics are scoped to one robot; no microphone/audio-clock state lives here. */
export const LatencyTag=memo(function LatencyTag({realtime,speech}:{
  realtime?:Realtime;speech?:SpeechStatus;
}){
  const percentiles=realtime?.latency_percentiles?.endpoint_to_speaker_start_ms;
  const {endToEnd,speakerStart,firstAudio,firstRoom}=latencyMetrics(realtime,speech);
  const evidence=speakerStart==null?'等待设备回执':endToEnd==null?'缺少停声事件':'设备自报 · 非声学测量';
  const detail=[
    `模型停声回调 → 扬声器起播回执：${ms(endToEnd)}（${evidence}）`,
    `TTS 首音频：${ms(firstAudio)}`,
    `首音频 → Room 首帧：${ms(firstRoom)}`,
    ...(percentiles?.samples?[`设备回执 P50 / P95（${percentiles.samples}轮）：${ms(percentiles.p50_ms)} / ${ms(percentiles.p95_ms)}`]:[]),
  ].join('\n');
  return <span className="room-tag latency-tag" title={detail}>延迟 {ms(endToEnd)}</span>;
});
