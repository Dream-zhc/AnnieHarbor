// 语种 / 方言（Windows FireRedLID 旁路，2026-11-20）：工作台“语种”胶囊的文字、颜色和悬停说明。
export type LanguageId={
  state:string;heartbeat_age_ms:number|null;reason:string|null;device:string|null;
  queue:number|null;count:number;latest_age_ms:number|null;
  mic_fps?:number|null;echo_skipped?:number|null;
  latest:{label:string;lang:string|null;dialect:string|null;name:string;confidence:number;
    duration_s:number;latency_ms:number;seq:number;robot_id:string|null}|null;
};

const stateLabel:Record<string,string>={
  online:'在线',waiting:'未接入',offline:'离线',disconnected:'房间断开',
  error:'模型出错',loading:'加载模型',no_mic:'麦克风未连',
};
const RECENT_MS=10*60_000;
const MIC_FPS_NORMAL=50;  // one 20 ms frame per message
const MIC_FPS_IDLE=5;

export function languagePill(language:LanguageId|null|undefined,error=false){
  if(error||!language)return {text:error?'离线':'读取中',tone:error?'bad':'warn',title:'语种识别'};
  const latest=language.latest&&language.latest_age_ms!==null&&language.latest_age_ms<RECENT_MS?language.latest:null;
  const fps=typeof language.mic_fps==='number'?language.mic_fps:null;
  const silent=language.state==='online'&&!latest&&fps!==null&&fps<MIC_FPS_IDLE;
  const status=stateLabel[language.state]??language.state;
  const text=language.state==='online'&&latest?`${latest.name} ${Math.round(latest.confidence*100)}%`:
    silent?'没收到麦克风声音':status;
  const tone=silent?'warn':language.state==='online'?'good':
    ['offline','disconnected','error'].includes(language.state)?'bad':'warn';
  const lines=[`语种识别（Windows FireRedLID）· ${status}`];
  if(latest)lines.push(`最近一句：${latest.name}（${latest.label}）· 把握 ${(latest.confidence*100).toFixed(1)}% · `+
    `${latest.duration_s} 秒的话 · ${(latest.latency_ms/1000).toFixed(1)} 秒出结果 · ${Math.round((language.latest_age_ms??0)/1000)} 秒前`);
  if(language.device)lines.push(`设备：${language.device} · 排队 ${language.queue??0} · 已识别 ${language.count} 句`);
  if(fps!==null)lines.push(`麦克风：${fps} 帧/秒（正常约 ${MIC_FPS_NORMAL}）${fps<MIC_FPS_IDLE?' · 机器人的麦克风没有把声音送进 Room':''}`);
  if(language.echo_skipped)lines.push(`已跳过 ${language.echo_skipped} 句她自己的声音（回声）`);
  if(language.reason)lines.push(`原因：${language.reason}`);
  return {text,tone,title:lines.join('\n')};
}
