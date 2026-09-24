import {memo,useState} from 'react';
import {RealtimeMicrophone,type MicDiagnostics} from './microphone';
import {RoomDebugSpeaker} from './room-debug-speaker';

type Route={room_id:string;mic_url:string;urls:Record<string,string>};
type Command={seq:number;kind:'hold'|'resume';turnId:string};

export const MicrophoneControls=memo(function MicrophoneControls({route,onActive}:{
  route:Route|undefined;onActive:(active:boolean)=>void;
}){
  const [level,setLevel]=useState<number|null>(null);
  const [metrics,setMetrics]=useState<MicDiagnostics|null>(null);
  return <section className="hud-section" aria-label="麦克风与声学输入">
    <div className="hud-heading"><h2>录音麦克风</h2></div>
    <RealtimeMicrophone route={route} onLevel={setLevel}
      onDiagnostics={setMetrics} onActive={onActive}/>
    <div className="mic-compact-diagnostics">
      <div className="mic-meter" role="meter" aria-label="浏览器麦克风电平"
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={level??0}>
        <span style={{width:(level??0)+'%'}}/>
      </div>
      <small>Mic {metrics?metrics.dbfs.toFixed(1)+' dBFS':'—'} · 底噪 {metrics?metrics.noiseFloor.toFixed(1)+' dBFS':'—'} · SNR {metrics?metrics.snr.toFixed(1)+' dB':'—'}</small>
    </div>
  </section>;
});

export const RoomSpeakerPanel=memo(function RoomSpeakerPanel({url,voiceUrl,roomId,command,
  available,onActive,voiceMode}:{url?:string;voiceUrl?:string;roomId?:string;command?:Command;
    available:boolean;onActive:(active:boolean)=>void;voiceMode:string;
  }){
  const [browserBuffer,setBrowserBuffer]=useState<number|null>(null);
  const [auditionActive,setAuditionActive]=useState(false);
  return <section className="hud-section" aria-label="声学电平与播放缓冲">
    <div className="hud-heading"><h2>播放器</h2></div>
    <div className="live-grid">
      <div className="live-item"><small>播放缓存</small><b>{browserBuffer!==null?browserBuffer+' ms':'0 ms'}</b></div>
      <div className="live-item"><small>网页试听</small><b>{auditionActive?'已开启':'已关闭'}</b></div>
      <div className="live-item"><small>轮次状态</small><b>{voiceMode}</b></div>
    </div>
    <RoomDebugSpeaker url={url} voiceUrl={voiceUrl} roomId={roomId} command={command}
      onBuffer={setBrowserBuffer} onActive={active=>{
        setAuditionActive(active);onActive(active);
      }} available={available}/>
  </section>;
});
