import {useCallback,useEffect,useRef,useState} from 'react';
import {api,robotPath,type Session} from './api';

type Voice={voice_id:string;name:string;model:string;region:string;created_ms:number};
type Preview={key:string;name:string;ready:boolean;error?:string|null;
  text?:string;instruction?:string;language_hint?:string;rate?:number;pitch?:number};
type Library={available:boolean;voices:Voice[];active_voice_id:string;default_voice_id:string;
  robot_voice_id:string;chat_voice_id:string;previews:Preview[]};
const DEFAULT='__default__';
const errorText=(error:unknown)=>error instanceof Error?error.message:String(error);

type SpeakerRow={speaker_id:string;name:string;duration_ms:number;created_ms:number;stale?:boolean};
type SpeakerCatalog={available:boolean;speakers:SpeakerRow[];threshold?:number};
type Heard={status?:string;name?:string|null};

function pcmLevel(chunks:Float32Array[]){
  let energy=0,count=0,peak=0;
  for(const chunk of chunks){
    for(let index=0;index<chunk.length;index+=1){
      const sample=chunk[index];
      energy+=sample*sample;
      count+=1;
      const abs=Math.abs(sample);
      if(abs>peak)peak=abs;
    }
  }
  return {rms:count?Math.sqrt(energy/count):0,peak};
}

function mixToMono(buffer:AudioBuffer){
  const out=new Float32Array(buffer.length);
  const channels=buffer.numberOfChannels;
  for(let channel=0;channel<channels;channel+=1){
    const data=buffer.getChannelData(channel);
    for(let index=0;index<data.length;index+=1)out[index]+=data[index]/channels;
  }
  return out;
}

function encodeWav(chunks:Float32Array[],sampleRate:number){
  const length=chunks.reduce((sum,chunk)=>sum+chunk.length,0);
  const pcm=new Int16Array(length);
  let offset=0;
  for(const chunk of chunks){
    for(let index=0;index<chunk.length;index+=1){
      const sample=Math.max(-1,Math.min(1,chunk[index]));
      pcm[offset++]=sample<0?sample*0x8000:sample*0x7fff;
    }
  }
  const buffer=new ArrayBuffer(44+pcm.length*2);
  const view=new DataView(buffer);
  const write=(at:number,text:string)=>{for(let index=0;index<text.length;index+=1)view.setUint8(at+index,text.charCodeAt(index));};
  write(0,'RIFF');
  view.setUint32(4,36+pcm.length*2,true);
  write(8,'WAVE');
  write(12,'fmt ');
  view.setUint32(16,16,true);
  view.setUint16(20,1,true);
  view.setUint16(22,1,true);
  view.setUint32(24,sampleRate,true);
  view.setUint32(28,sampleRate*2,true);
  view.setUint16(32,2,true);
  view.setUint16(34,16,true);
  write(36,'data');
  view.setUint32(40,pcm.length*2,true);
  new Int16Array(buffer,44).set(pcm);
  return new Blob([buffer],{type:'audio/wav'});
}

function SpeakerRegistry({robotId}:{robotId:string}){
  const [catalog,setCatalog]=useState<SpeakerCatalog|null>(null);
  const [heard,setHeard]=useState<Heard|null>(null);
  const [name,setName]=useState('');
  const [recording,setRecording]=useState(false);
  const [seconds,setSeconds]=useState(0);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [level,setLevel]=useState(0);
  const capture=useRef<{context:AudioContext;stream:MediaStream;processor:ScriptProcessorNode;chunks:Float32Array[];started:number;peak:number}|null>(null);
  const fileRef=useRef<HTMLInputElement|null>(null);
  const refresh=useCallback(async()=>{
    const result=await api<SpeakerCatalog>('/api/speakers');
    setCatalog(result);
    return result;
  },[]);
  useEffect(()=>{
    let active=true;
    void api<SpeakerCatalog>('/api/speakers').then(value=>{if(active)setCatalog(value);})
      .catch(reason=>{if(active)setError(errorText(reason));});
    return()=>{active=false;};
  },[]);
  useEffect(()=>{
    let active=true;
    const load=()=>void api<{speaker?:Heard}>(robotPath(robotId)+'/realtime')
      .then(value=>{if(active)setHeard(value.speaker??null);})
      .catch(()=>{if(active)setHeard(null);});
    load();
    const timer=window.setInterval(load,1000);
    return()=>{active=false;window.clearInterval(timer);};
  },[robotId]);
  useEffect(()=>()=>{
    capture.current?.stream.getTracks().forEach(track=>track.stop());
    void capture.current?.context.close();
    capture.current=null;
  },[]);
  const submitWav=async(wav:Blob)=>{
    if(!name.trim()){setError('请先填写名称');return;}
    setBusy(true);setError('');setNotice('');
    try{
      const response=await fetch('/api/speakers?name='+encodeURIComponent(name.trim()),{
        method:'POST',headers:{'Content-Type':'audio/wav'},body:wav,
      });
      const result=await response.json() as {status?:string;detail?:string;name?:string};
      if(!response.ok)throw Error(result.detail||'声纹登记失败');
      await refresh();
      setName('');
      setNotice(result.status==='updated'
        ?`已用新录音更新「${result.name??''}」。原始录音没有保存。`
        :`已登记「${result.name??''}」。所有机器人都会认识这个名字，原始录音没有保存。`);
    }catch(reason){setError(errorText(reason));}
    finally{setBusy(false);setSeconds(0);setLevel(0);}
  };
  const stopCapture=async(save:boolean)=>{
    const current=capture.current;
    capture.current=null;
    setRecording(false);
    if(!current)return;
    current.processor.onaudioprocess=null;
    current.processor.disconnect();
    current.stream.getTracks().forEach(track=>track.stop());
    const elapsed=(performance.now()-current.started)/1000;
    await current.context.close();
    if(!save)return;
    if(elapsed<3){setError('请至少录 3 秒');return;}
    if(!name.trim()){setError('请先填写名称');return;}
    const heard=pcmLevel(current.chunks);
    if(heard.rms<0.01){
      setError('麦克风几乎没有收到声音。工作台如果正在听，会占住麦克风，这次录音就是空白。请先停止页面麦克风再录，或直接上传一段 wav、mp3、m4a。');
      return;
    }
    await submitWav(encodeWav(current.chunks,current.context.sampleRate));
  };
  const uploadFile=async(file:File)=>{
    if(recording||busy)return;
    if(!name.trim()){setError('请先填写名称');return;}
    if(file.size>8*1024*1024){setError('音频不能超过 8MB');return;}
    setBusy(true);setError('');setNotice('');
    const context=new AudioContext();
    try{
      const decoded=await context.decodeAudioData(await file.arrayBuffer());
      const mono=mixToMono(decoded);
      const duration=mono.length/decoded.sampleRate;
      if(duration<3)throw Error('请至少提供 3 秒说话声');
      if(duration>12.25)throw Error('音频不能超过 12 秒');
      if(pcmLevel([mono]).rms<0.01)throw Error('这段音频几乎没有声音，请换一段正常音量的说话录音');
      setBusy(false);
      await submitWav(encodeWav([mono],decoded.sampleRate));
    }catch(reason){
      setBusy(false);
      setError(reason instanceof DOMException?'这个格式读不了，请换 wav、mp3 或 m4a':errorText(reason));
    }finally{
      await context.close();
    }
  };
  useEffect(()=>{
    if(!recording)return;
    const timer=window.setInterval(()=>{
      const started=capture.current?.started;
      if(!started)return;
      const elapsed=(performance.now()-started)/1000;
      setSeconds(elapsed);
      setLevel(capture.current?.peak??0);
      if(elapsed>=12)void stopCapture(true);
    },200);
    return()=>window.clearInterval(timer);
  },[recording,name]);
  const start=async()=>{
    if(recording||busy||!name.trim())return;
    setError('');setNotice('');setSeconds(0);
    try{
      const stream=await navigator.mediaDevices.getUserMedia({
        audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true},
      });
      const context=new AudioContext();
      if(context.state==='suspended')await context.resume();
      const source=context.createMediaStreamSource(stream);
      const processor=context.createScriptProcessor(4096,1,1);
      const gain=context.createGain();
      gain.gain.value=0;
      const chunks:Float32Array[]=[];
      processor.onaudioprocess=event=>{
        const data=event.inputBuffer.getChannelData(0);
        chunks.push(new Float32Array(data));
        let peak=0;
        for(let index=0;index<data.length;index+=1){
          const abs=Math.abs(data[index]);
          if(abs>peak)peak=abs;
        }
        const current=capture.current;
        if(current&&peak>current.peak)current.peak=peak;
      };
      source.connect(processor);
      processor.connect(gain);
      gain.connect(context.destination);
      capture.current={context,stream,processor,chunks,started:performance.now(),peak:0};
      setLevel(0);
      setRecording(true);
    }catch(reason){
      const nameOf=reason instanceof DOMException?reason.name:'';
      setError(nameOf==='NotAllowedError'?'麦克风权限被拒绝，无法录声纹':errorText(reason));
    }
  };
  const remove=async(row:SpeakerRow)=>{
    if(busy||recording)return;
    if(!window.confirm('删除「'+row.name+'」的声纹？已保存的向量会去掉，不会影响其他人。'))return;
    setBusy(true);setError('');
    try{
      await api('/api/speakers/'+encodeURIComponent(row.speaker_id),'DELETE');
      await refresh();
      setNotice('已删除「'+row.name+'」。');
    }catch(reason){setError(errorText(reason));}
    finally{setBusy(false);}
  };
  const heardText=heard?.status==='matched'&&heard.name?`当前这句话是「${heard.name}」说的`
    :heard?.status==='carried'&&heard.name?`这一句太短，沿用上一句的「${heard.name}」`
    :heard?.status==='ambiguous'?'这段声音和好几位已注册的人接近，先不点名'
    :heard?.status==='unknown'?'这一轮没有匹配到已注册声纹'
    :heard?.status==='short'?'这一句太短，还不能确认是谁'
    :'还没有识别结果';
  return <div className="voice-bento-card speaker-card">
    <div className="voice-card-title-row">
      <span className="voice-step-pill">声纹</span>
      <h4>声纹注册</h4>
      <span className="voice-badge-soft">全局 · 重启仍在</span>
    </div>
    <p className="voice-note">录 3–12 秒平时的说话声，或上传 wav、mp3、m4a。声纹存在本机数据库，所有机器人共用，重启仍在。原始录音不保存。认出是谁之后，模型知道这句话是这个人说的，只在语境合适时称呼。</p>
    {catalog&&!catalog.available&&<p className="voice-note">声纹模型还没装好，现在不能登记。在项目根运行 <code>uv run --locked python scripts/下载声纹模型.py</code> 后重启。</p>}
    <p className="speaker-heard" role="status">{heardText}</p>
    <div className="voice-inputs-row">
      <label className="voice-field-group">
        <span className="voice-label-text">名字</span>
        <input className="voice-input" value={name} maxLength={16} placeholder="例如：小明"
          disabled={recording||busy} onChange={event=>setName(event.target.value)}/>
      </label>
      <div className="speaker-record-actions">
        {!recording?<button type="button" className="primary-button" disabled={!name.trim()||busy||!catalog?.available}
          onClick={()=>void start()}>开始录音</button>:<>
          <button type="button" className="primary-button speaker-record-live" disabled={seconds<3||busy}
            onClick={()=>void stopCapture(true)}>{busy?'正在登记…':`停止并登记 ${seconds.toFixed(1)}s`}</button>
          <button type="button" className="action-pill" disabled={busy} onClick={()=>void stopCapture(false)}>取消</button>
          <span className="speaker-level" role="status">{level>=0.02?'音量：听得到':'音量：没有声音'}</span>
        </>}
        <input ref={fileRef} type="file" accept=".wav,.mp3,.m4a,.aac,.ogg,.webm,audio/*" style={{display:'none'}}
          onChange={event=>{
            const file=event.target.files?.[0];
            event.target.value='';
            if(file)void uploadFile(file);
          }}/>
        <button type="button" className="action-pill" disabled={!name.trim()||busy||recording||!catalog?.available}
          onClick={()=>fileRef.current?.click()}>上传音频</button>
      </div>
    </div>
    <div className="voice-library">
      {(catalog?.speakers??[]).map(row=><div key={row.speaker_id} className="voice-library-row">
        <div className="voice-row-meta">
          <span className="voice-type-icon custom" aria-hidden="true">声</span>
          <div>
            <strong>{row.name}</strong>
            <small className="voice-subinfo">{Math.round(row.duration_ms/100)/10} 秒
              {row.stale?' · 模型已更换，请重新录':''}</small>
          </div>
        </div>
        <button type="button" className="action-pill voice-tool-pill danger" disabled={busy||recording}
          onClick={()=>void remove(row)}>删除</button>
      </div>)}
      {catalog&&!catalog.speakers.length&&<div className="voice-empty-box">还没有登记声纹</div>}
    </div>
    {notice&&<p role="status" className="quality-note voice-status-alert success">{notice}</p>}
    {error&&<p role="alert" className="quality-note voice-status-alert error">{error}</p>}
  </div>;
}

let activeAudioElement: HTMLAudioElement | null = null;

function formatDuration(seconds: number): string {
  if (isNaN(seconds) || seconds < 0 || !isFinite(seconds)) return '0:00';
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs < 10 ? '0' : ''}${secs}`;
}

function CustomAudioPlayer({src, previewKey}:{src:string;previewKey:string}){
  const audioRef = useRef<HTMLAudioElement|null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isMuted, setIsMuted] = useState(false);

  useEffect(()=>{
    setIsPlaying(false);
    setCurrentTime(0);
    setDuration(0);
    return () => {
      const audio = audioRef.current;
      if (audio) {
        audio.pause();
      }
      if (activeAudioElement === audio) {
        activeAudioElement = null;
      }
    };
  },[src]);

  const togglePlay = async () => {
    const audio = audioRef.current;
    if(!audio) return;
    if(audio.paused){
      if(activeAudioElement && activeAudioElement !== audio){
        activeAudioElement.pause();
      }
      activeAudioElement = audio;
      try {
        await audio.play();
        setIsPlaying(true);
      } catch(e) {
        console.warn('音频播放异常:', e);
      }
    } else {
      audio.pause();
      setIsPlaying(false);
      if(activeAudioElement === audio){
        activeAudioElement = null;
      }
    }
  };

  const toggleMute = () => {
    const audio = audioRef.current;
    if(!audio) return;
    audio.muted = !audio.muted;
    setIsMuted(audio.muted);
  };

  const handleSeek = (e: React.MouseEvent<HTMLDivElement>) => {
    const audio = audioRef.current;
    if(!audio || !audio.duration || isNaN(audio.duration) || !isFinite(audio.duration)) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const ratio = Math.max(0, Math.min(1, clickX / rect.width));
    audio.currentTime = ratio * audio.duration;
    setCurrentTime(audio.currentTime);
  };

  const progressPercent = duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0;

  return <div className={`cyber-audio-player ${isPlaying ? 'is-playing' : ''}`} data-key={previewKey}>
    <audio controls preload="none"
      ref={audioRef}
      className="voice-audio-native-hidden"
      src={src}
      onPlay={() => {
        setIsPlaying(true);
        if(activeAudioElement && activeAudioElement !== audioRef.current){
          activeAudioElement.pause();
        }
        activeAudioElement = audioRef.current;
      }}
      onPause={() => {
        setIsPlaying(false);
        if(activeAudioElement === audioRef.current){
          activeAudioElement = null;
        }
      }}
      onTimeUpdate={e => {
        const el = e.currentTarget;
        setCurrentTime(el.currentTime);
        if(!isNaN(el.duration) && el.duration > 0 && isFinite(el.duration)){
          setDuration(el.duration);
        }
      }}
      onLoadedMetadata={e => {
        const el = e.currentTarget;
        if(!isNaN(el.duration) && el.duration > 0 && isFinite(el.duration)){
          setDuration(el.duration);
        }
      }}
      onDurationChange={e => {
        const el = e.currentTarget;
        if(!isNaN(el.duration) && el.duration > 0 && isFinite(el.duration)){
          setDuration(el.duration);
        }
      }}
      onEnded={() => {
        setIsPlaying(false);
        setCurrentTime(0);
        if(activeAudioElement === audioRef.current){
          activeAudioElement = null;
        }
      }}
    />

    <button
      type="button"
      className={`cyber-play-btn ${isPlaying ? 'playing' : ''}`}
      onClick={() => void togglePlay()}
      aria-label={isPlaying ? '暂停' : '播放'}
      title={isPlaying ? '暂停' : '播放'}
    >
      {isPlaying ? (
        <svg viewBox="0 0 16 16" fill="currentColor" width="12" height="12">
          <rect x="2.5" y="2" width="3.5" height="12" rx="1" />
          <rect x="10" y="2" width="3.5" height="12" rx="1" />
        </svg>
      ) : (
        <svg viewBox="0 0 16 16" fill="currentColor" width="12" height="12">
          <path d="M4.5 2.8a.6.6 0 0 1 .91-.52l8 5.2a.6.6 0 0 1 0 1.04l-8 5.2a.6.6 0 0 1-.91-.52V2.8z" />
        </svg>
      )}
    </button>

    <div className="cyber-track-area">
      <div
        className="cyber-track"
        onClick={handleSeek}
        role="slider"
        aria-valuenow={Math.round(progressPercent)}
        aria-valuemin={0}
        aria-valuemax={100}
        tabIndex={0}
      >
        <div className="cyber-progress-fill" style={{ width: `${progressPercent}%` }} />
        <div className="cyber-progress-glow" style={{ width: `${progressPercent}%` }} />
        <div className="cyber-scrub-handle" style={{ left: `${progressPercent}%` }} />
      </div>
      <div className="cyber-time-display">
        <span className="cyber-current-time">{formatDuration(currentTime)}</span>
        <span className="cyber-time-divider">/</span>
        <span className="cyber-total-time">{duration > 0 ? formatDuration(duration) : '--:--'}</span>
      </div>
    </div>

    <button
      type="button"
      className={`cyber-vol-btn ${isMuted ? 'muted' : ''}`}
      onClick={toggleMute}
      title={isMuted ? '取消静音' : '静音'}
      aria-label={isMuted ? '取消静音' : '静音'}
    >
      {isMuted ? (
        <svg viewBox="0 0 16 16" fill="currentColor" width="13" height="13">
          <path d="M3.5 5.5A.5.5 0 0 1 4 6v4a.5.5 0 0 1-.5.5H2a1 1 0 0 1-1-1V6.5a1 1 0 0 1 1-1h1.5zm3.146-2.854a.5.5 0 0 1 .854.354v10a.5.5 0 0 1-.854.354L4 10.707V5.293l2.646-2.647zm4.208 4.208a.5.5 0 0 1 .708 0l1.414 1.414 1.414-1.414a.5.5 0 1 1 .708.708l-1.414 1.414 1.414 1.414a.5.5 0 0 1-.708.708l-1.414-1.414-1.414 1.414a.5.5 0 0 1-.708-.708l1.414-1.414-1.414-1.414a.5.5 0 0 1 0-.708z"/>
        </svg>
      ) : (
        <svg viewBox="0 0 16 16" fill="currentColor" width="13" height="13">
          <path d="M11.536 14.01A8.473 8.473 0 0 0 14.026 8a8.47 8.47 0 0 0-2.49-6.01l-.708.707A7.476 7.476 0 0 1 13.025 8c0 2.071-.84 3.946-2.197 5.303l.708.707z"/>
          <path d="M10.121 12.596A6.48 6.48 0 0 0 12.025 8a6.48 6.48 0 0 0-1.904-4.596l-.707.707A5.483 5.483 0 0 1 11.025 8a5.483 5.483 0 0 1-1.611 3.889l.707.707z"/>
          <path d="M8.707 11.182A4.486 4.486 0 0 0 10.025 8a4.486 4.486 0 0 0-1.318-3.182L8 5.525A3.489 3.489 0 0 1 9.025 8 3.49 3.49 0 0 1 8 10.475l.707.707zM6.717 3.55A.5.5 0 0 1 7 4v8a.5.5 0 0 1-.812.39L3.825 10H1.5A1.5 1.5 0 0 1 0 8.5v-1A1.5 1.5 0 0 1 1.5 6h2.325l2.363-2.39a.5.5 0 0 1 .529-.06z"/>
        </svg>
      )}
    </button>
  </div>;
}

export function VoiceSettings({robotId,session}:{robotId:string;session:Session|null}){
  const [data,setData]=useState<Library|null>(null);
  const [activeTab,setActiveTab]=useState<'roster'|'preview'|'speaker'>('roster');
  const [name,setName]=useState('');
  const [source,setSource]=useState<File|null>(null);
  const [language,setLanguage]=useState('zh');
  const [busy,setBusy]=useState('');
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [voiceForPreview,setVoiceForPreview]=useState('');
  const [previews,setPreviews]=useState<Preview[]>([]);
  const fileInputRef=useRef<HTMLInputElement|null>(null);
  const base=robotPath(robotId);
  const refresh=useCallback(async()=>{
    const result=await api<Library>(base+'/voices');
    setData(result);
    return result;
  },[base]);

  useEffect(()=>{
    let active=true;
    void api<Library>(base+'/voices').then(value=>{if(active)setData(value);})
      .catch(e=>{if(active)setError(errorText(e));});
    return()=>{active=false;};
  },[base,session?.chat_id,session?.persona_id,session?.generation]);

  const previewId=voiceForPreview||(data?.voices[0]?.voice_id??'');
  useEffect(()=>{
    if(!previewId){setPreviews([]);return;}
    let active=true;
    const load=()=>void api<{previews:Preview[]}>(base+'/voices/'+encodeURIComponent(previewId)+'/previews')
      .then(result=>{if(active)setPreviews(result.previews);})
      .catch(e=>{if(active)setError(errorText(e));});
    load();
    const timer=window.setInterval(load,4500);
    return()=>{active=false;window.clearInterval(timer);};
  },[base,previewId]);

  const clone=async()=>{
    if(!source||!name.trim()||busy)return;
    if(source.size>10*1024*1024){setError('参考音频不得超过10MB');return;}
    setBusy('clone');setError('');setNotice('');
    try{
      const qs=new URLSearchParams({name:name.trim(),filename:source.name,language});
      const response=await fetch(base+'/voices/clone?'+qs.toString(),
        {method:'POST',headers:{'Content-Type':'application/octet-stream'},body:source});
      const result=await response.json() as {voice_id?:string;name?:string;detail?:string};
      if(!response.ok||!result.voice_id)throw Error(result.detail||'音色克隆失败');
      await refresh();
      setVoiceForPreview(result.voice_id);
      setActiveTab('preview');
      setName('');setSource(null);
      if(fileInputRef.current)fileInputRef.current.value='';
      setNotice('音色已创建，15组试听正在后台依次合成，不阻塞机器人。');
    }catch(e){setError(errorText(e));}
    finally{setBusy('');}
  };
  const select=async(voiceId:string,scope:'chat'|'robot')=>{
    if(!session||busy)return;
    setBusy('select');setError('');
    try{
      await api(base+'/voices/selection','PUT',{
        chat_id:session.chat_id,persona_id:session.persona_id,
        session_epoch:session.session_epoch,generation:session.generation,
        voice_id:voiceId,scope,
      });
      await refresh();
      setNotice('已保存音色，下一个 TTS 任务生效；正在播放的内容不会被改音。');
    }catch(e){setError(errorText(e));}
    finally{setBusy('');}
  };
  const rename=async(voice:Voice)=>{
    const value=window.prompt('新的音色备注',voice.name);
    if(value===null||value.trim()===voice.name)return;
    setBusy('rename');setError('');
    try{
      await api(base+'/voices/'+encodeURIComponent(voice.voice_id),'PATCH',{name:value.trim()});
      await refresh();
    }catch(e){setError(errorText(e));}
    finally{setBusy('');}
  };
  const remove=async(voice:Voice)=>{
    if(!window.confirm('仅从本系统删除“'+voice.name+'”及相关选择，云端克隆不会自动删除。继续？'))return;
    setBusy('delete');setError('');
    try{
      await api(base+'/voices/'+encodeURIComponent(voice.voice_id),'DELETE');
      if(previewId===voice.voice_id)setVoiceForPreview('');
      await refresh();
    }catch(e){setError(errorText(e));}
    finally{setBusy('');}
  };
  const generate=async(key:string)=>{
    if(!previewId||busy)return;
    setBusy(key);setError('');
    try{
      await api(base+'/voices/'+encodeURIComponent(previewId)+'/previews/'+key,'POST');
      const res=await api<{previews:Preview[]}>(base+'/voices/'+encodeURIComponent(previewId)+'/previews');
      setPreviews(res.previews);
    }catch(e){setError(errorText(e));}
    finally{setBusy('');}
  };

  const activeVoice=data?.voices.find(v=>v.voice_id===data.active_voice_id);
  const activeVoiceName=activeVoice?.name??(data?.active_voice_id===DEFAULT?'系统默认':'系统默认');
  const previewVoice=data?.voices.find(v=>v.voice_id===previewId);

  return <section className="voice-settings">
    <div className="voice-header-box">
      <div className="voice-header-title-row">
        <span className="voice-header-icon" aria-hidden="true">🎙️</span>
        <div>
          <h3>音色克隆与管理</h3>
          <p className="voice-header-sub">高保真定制人声发音 · 毫秒级声学风格复刻与多场景试听</p>
        </div>
      </div>
      {data?.available&&(
        <span className="voice-ready-tag">
          <i className="status-dot-good"/> Qwen TTS 就绪
        </span>
      )}
    </div>

    {!data?.available?<div className="voice-unavail-box"><p>未配置 Qwen TTS 服务，无法克隆音色。</p></div>:<>
      <div className="voice-tab-nav" role="tablist" aria-label="音色管理二级标签">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab==='roster'}
          className={`voice-tab-nav-btn ${activeTab==='roster'?'active':''}`}
          onClick={()=>setActiveTab('roster')}
        >
          <span className="voice-tab-nav-icon" aria-hidden="true">🎙️</span>
          <span className="voice-tab-nav-title">音色库与克隆</span>
          {data?.voices && data.voices.length>0 && (
            <span className="voice-tab-count-badge">{data.voices.length}</span>
          )}
        </button>

        <button
          type="button"
          role="tab"
          aria-selected={activeTab==='preview'}
          className={`voice-tab-nav-btn ${activeTab==='preview'?'active':''}`}
          onClick={()=>setActiveTab('preview')}
        >
          <span className="voice-tab-nav-icon" aria-hidden="true">🎧</span>
          <span className="voice-tab-nav-title">场景试听 (15组试听)</span>
          {previewVoice && (
            <span className="voice-tab-target-badge">{previewVoice.name}</span>
          )}
        </button>

        <button
          type="button"
          role="tab"
          aria-selected={activeTab==='speaker'}
          className={`voice-tab-nav-btn ${activeTab==='speaker'?'active':''}`}
          onClick={()=>setActiveTab('speaker')}
        >
          <span className="voice-tab-nav-icon" aria-hidden="true">👤</span>
          <span className="voice-tab-nav-title">声纹注册</span>
        </button>
      </div>

      {activeTab==='roster' && <>
        <div className="voice-bento-card voice-clone-card">
          <div className="voice-card-title-row">
            <span className="voice-step-pill">01</span>
            <h4>创建克隆音色</h4>
            <span className="voice-badge-soft">定制声音</span>
          </div>
          <p className="voice-note">上传本人声音或已获授权的声音。建议10–20秒清晰独白；WAV 16bit/16kHz以上，或 MP3/M4A，最多10MB。参考音频不在本机长期保存。</p>
          
          <div className="voice-clone-form">
            <div className="voice-inputs-row">
              <label className="voice-field-group">
                <span className="voice-label-text">音色备注</span>
                <input className="voice-input" value={name} onChange={e=>setName(e.target.value)}
                  maxLength={40} placeholder="例如：温柔知性、沉稳男声"/>
              </label>
              <label className="voice-field-group">
                <span className="voice-label-text">参考音频语种</span>
                <select className="voice-select" value={language} onChange={e=>setLanguage(e.target.value)}>
                  <option value="zh">中文 (普通话)</option>
                  <option value="en">英语 (English)</option>
                  <option value="ru">俄语 (Русский)</option>
                </select>
              </label>
            </div>

            <div className={`voice-dropzone ${source?'has-file':''}`}
              onClick={()=>fileInputRef.current?.click()}
              onDragOver={e=>e.preventDefault()}
              onDrop={e=>{
                e.preventDefault();
                const f=e.dataTransfer.files[0];
                if(f)setSource(f);
              }}>
              <input ref={fileInputRef} type="file" accept=".wav,.mp3,.m4a,audio/*"
                style={{display:'none'}}
                onChange={e=>setSource(e.target.files?.[0]??null)}/>
              {source?(
                <div className="voice-dropzone-active">
                  <span className="voice-file-icon" aria-hidden="true">🎵</span>
                  <div className="voice-file-text">
                    <strong>{source.name}</strong>
                    <small>{(source.size/(1024*1024)).toFixed(2)} MB · 点击可更换音频</small>
                  </div>
                  <button type="button" className="voice-clear-btn"
                    onClick={e=>{
                      e.stopPropagation();
                      setSource(null);
                      if(fileInputRef.current)fileInputRef.current.value='';
                    }} title="移除文件">✕</button>
                </div>
              ):(
                <div className="voice-dropzone-prompt">
                  <span className="voice-upload-icon" aria-hidden="true">📂</span>
                  <div>
                    <strong>点击上传或拖拽音频文件到此处</strong>
                    <small>支持 WAV / MP3 / M4A，最多10MB</small>
                  </div>
                </div>
              )}
            </div>

            <button className="primary-button voice-submit-btn" type="button"
              disabled={!source||!name.trim()||!!busy} onClick={()=>void clone()}>
              {busy==='clone'?'复刻中，请等待…':'⚡ 创建克隆音色'}
            </button>
          </div>
        </div>

        <div className="voice-bento-card voice-roster-card">
          <div className="voice-roster-head">
            <div className="voice-card-title-row">
              <span className="voice-step-pill">02</span>
              <h4>我的音色库</h4>
            </div>
            <div className="voice-active-indicator">
              <span className="voice-indicator-label">当前生效：</span>
              <strong className="voice-indicator-name">我的音色 · 当前生效：{activeVoiceName}</strong>
            </div>
          </div>

          <div className="voice-library">
            <div className={`voice-library-row voice-default-row ${data.active_voice_id===DEFAULT?'row-active':''}`}>
              <div className="voice-row-meta">
                <span className="voice-type-icon" aria-hidden="true">⚙️</span>
                <div>
                  <div className="voice-row-name-wrap">
                    <strong>系统默认</strong>
                    <span className="voice-pill-tag">官方基础</span>
                    {data.active_voice_id===DEFAULT&&<span className="voice-pill-active">生效中</span>}
                  </div>
                  <small className="voice-subinfo">系统内置默认标准发音人</small>
                </div>
              </div>
              <div className="voice-row-actions">
                <button type="button" className="action-pill voice-act-pill" disabled={!session||!!busy}
                  onClick={()=>void select(DEFAULT,'chat')}>恢复本 Chat 默认</button>
                <button type="button" className="action-pill voice-act-pill" disabled={!session||!!busy}
                  onClick={()=>void select(DEFAULT,'robot')}>恢复机器人默认</button>
              </div>
            </div>

            {data.voices.map(voice=>{
              const isActive=data.active_voice_id===voice.voice_id;
              const isChat=data.chat_voice_id===voice.voice_id;
              const isRobot=data.robot_voice_id===voice.voice_id;
              const isPreviewing=previewId===voice.voice_id;
              return <div key={voice.voice_id} className={`voice-library-row ${isActive?'row-active':''} ${isPreviewing?'row-previewing':''}`}>
                <div className="voice-row-meta">
                  <span className="voice-type-icon custom" aria-hidden="true">🎙️</span>
                  <div>
                    <div className="voice-row-name-wrap">
                      <strong className="voice-custom-name">{voice.name}</strong>
                      {isActive&&<span className="voice-pill-active">生效中</span>}
                      {isChat&&<span className="voice-pill-chat">Chat 专属</span>}
                      {isRobot&&<span className="voice-pill-robot">机器人默认</span>}
                    </div>
                    <small className="voice-subinfo" title={voice.voice_id}>ID: {voice.voice_id.slice(0,22)}…</small>
                  </div>
                </div>

                <div className="voice-row-actions">
                  <div className="voice-btn-group primary-group">
                    <button type="button" className={`action-pill voice-act-pill ${isChat?'is-current':''}`}
                      disabled={!session||!!busy}
                      onClick={()=>void select(voice.voice_id,'chat')} title="在当前 Chat 中生效">当前 Chat 使用</button>
                    <button type="button" className={`action-pill voice-act-pill ${isRobot?'is-current':''}`}
                      disabled={!session||!!busy}
                      onClick={()=>void select(voice.voice_id,'robot')} title="设为此机器人全局默认">设为机器人默认</button>
                  </div>
                  <div className="voice-btn-group tool-group">
                    <button type="button" className={`action-pill voice-tool-pill ${isPreviewing?'preview-active':''}`}
                      onClick={()=>{
                        setVoiceForPreview(voice.voice_id);
                        setActiveTab('preview');
                      }}>🎧 试听</button>
                    <button type="button" className="action-pill voice-tool-pill"
                      disabled={!!busy} onClick={()=>void rename(voice)}>改名</button>
                    <button type="button" className="action-pill voice-tool-pill danger"
                      disabled={!!busy} onClick={()=>void remove(voice)}>本地删除</button>
                  </div>
                </div>
              </div>;
            })}

            {!data.voices.length&&(
              <div className="voice-empty-box">暂无克隆音色，请在上方上传音频创建</div>
            )}
          </div>
        </div>
      </>}

      {activeTab==='preview' && (
        !data?.voices.length ? (
          <div className="voice-bento-card voice-empty-audition">
            <span className="voice-empty-icon" aria-hidden="true">🎧</span>
            <h4>暂无克隆音色</h4>
            <p className="voice-note">场景试听需要基于已上传的克隆音色合成。请先在「音色库与克隆」中创建音色。</p>
            <button type="button" className="primary-button" onClick={()=>setActiveTab('roster')}>
              ＋ 前往创建克隆音色
            </button>
          </div>
        ) : (
          <section className="voice-preview-cases voice-bento-card">
            <div className="voice-preview-head">
              <div className="voice-card-title-row">
                <span className="voice-step-pill">03</span>
                <h4>试听效果 · {previewVoice?.name ?? '已选择音色'}</h4>
              </div>
              <div className="voice-preview-head-right">
                <span className="voice-preview-stat">
                  已就绪 {previews.filter(p=>p.ready).length} / {previews.length||15} 项
                </span>
                <button type="button" className="action-pill voice-back-pill" onClick={()=>setActiveTab('roster')}>
                  ← 返回音色库
                </button>
              </div>
            </div>

            {data.voices.length > 1 && (
              <div className="voice-preview-switcher-row">
                <span className="voice-switcher-label">切换试听：</span>
                <div className="voice-switcher-pills">
                  {data.voices.map(v => (
                    <button
                      key={v.voice_id}
                      type="button"
                      className={`action-pill voice-switcher-btn ${v.voice_id === previewId ? 'active' : ''}`}
                      onClick={() => setVoiceForPreview(v.voice_id)}
                    >
                      {v.name}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <p className="voice-note">普通话、五地方言、英语、俄语、情绪/拟声标签、自然语言 instruction、语速音调与耳语；创建后自动合成15项（产生云端费用），独立试听，不发送到机器人房间。失败项可以按需重试。</p>
            
            <div className="voice-preview-grid">{previews.map(item=><div key={item.key} className={`voice-preview-row ${item.ready?'is-ready':''}`}>
              <div className="voice-preview-title-row">
                <span className="voice-preview-name">{item.name}</span>
                <span className={`voice-preview-status-dot ${item.ready?'ready':item.error?'error':'pending'}`}>
                  {item.ready?'● 就绪':item.error?'● 失败':'○ 待生成'}
                </span>
              </div>
              <details className="voice-preview-details">
                <summary>试听文本与 instruction</summary>
                <div className="voice-preview-meta">
                  <small>文本：{item.text}</small>
                  <small>指令：{item.instruction}</small>
                  <small>语种：{item.language_hint} · 语速：{item.rate} · 音调：{item.pitch}</small>
                </div>
              </details>
              <div className="voice-preview-player-wrap">
                {item.ready?(
                  <CustomAudioPlayer
                    src={base+'/voices/'+encodeURIComponent(previewId)+'/previews/'+item.key}
                    previewKey={item.key}
                  />
                ):(
                  <button type="button" className="action-pill voice-gen-btn" disabled={!!busy}
                    onClick={()=>void generate(item.key)}>
                    {busy===item.key?'正在合成…':item.error?'重试试听':'生成试听'}
                  </button>
                )}
              </div>
            </div>)}</div>
          </section>
        )
      )}

      {activeTab==='speaker' && <SpeakerRegistry robotId={robotId}/>}
    </>}
    {notice&&<p role="status" className="quality-note voice-status-alert success">{notice}</p>}
    {error&&<p role="alert" className="quality-note voice-status-alert error">{error}</p>}
  </section>;
}
