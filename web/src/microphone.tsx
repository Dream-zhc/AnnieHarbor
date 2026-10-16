import {useEffect,useRef,useState} from 'react';
import {pcmFrame} from './pcm-format';
import {waitForCaptureReady} from './capture-ready';
import {acquireAudioContext,loadAudioWorklet,releaseAudioContext} from './shared-audio-context';
import {browserInstanceId} from './audio-devices';

type Route={room_id:string;mic_url:string;urls?:Record<string,string>};
type Microphone={ws:WebSocket;stream:MediaStream;context:AudioContext;
  source:MediaStreamAudioSourceNode;processor:AudioWorkletNode;silent:GainNode;watchdog:number;
  voiceWs:WebSocket|null;instanceId:string;heartbeat:number|null};
export type MicDiagnostics={dbfs:number;noiseFloor:number;snr:number;clipped:boolean};
type InputDevice={deviceId:string;label:string};

function connect(url:string,roomId:string):Promise<WebSocket>{
  return new Promise((resolve,reject)=>{
    const ws=new WebSocket(url);
    ws.binaryType='arraybuffer';
    let settled=false;
    const fail=(reason:string)=>{
      if(settled)return;
      settled=true;window.clearTimeout(timer);ws.close();reject(new Error(reason));
    };
    const timer=window.setTimeout(()=>fail('Room WELCOME 超时'),4000);
    ws.onerror=()=>fail('Room 麦克风 WebSocket 连接失败');
    ws.onclose=()=>fail('Room 在欢迎握手前断开');
    ws.onmessage=event=>{
      if(typeof event.data!=='string')return;
      try{
        const message=JSON.parse(event.data) as {type?:string;ok?:boolean;room?:string;robotId?:string};
        if(message.type!=='WELCOME')return;
        if(message.ok!==true||message.room!==roomId||message.robotId!=='reb_mic'){
          fail('Room 麦克风身份不匹配');return;
        }
        settled=true;window.clearTimeout(timer);resolve(ws);
      }catch{/* ignore malformed control messages */}
    };
  });
}

/** Actual Room speech input. No test-frame injection or fake ASR path. */
export function RealtimeMicrophone({route,onLevel,onDiagnostics,onActive}:{
  route:Route|undefined;onLevel?:(level:number|null)=>void;
  onDiagnostics?:(metrics:MicDiagnostics|null)=>void;onActive?:(active:boolean)=>void;
}){
  const active=useRef<Microphone|null>(null);
  const token=useRef(0);
  const startupAbort=useRef<AbortController|null>(null);
  const [confirmed,setConfirmed]=useState(false);
  const [working,setWorking]=useState(false);
  const [listening,setListening]=useState(false);
  const [status,setStatus]=useState('未开启');
  const [frames,setFrames]=useState(0);
  const [level,setLevel]=useState(0);
  const gain=1;
  const [clipped,setClipped]=useState(false);
  const [inputs,setInputs]=useState<InputDevice[]>([]);
  const [selectedInput,setSelectedInput]=useState('');
  const [trackInfo,setTrackInfo]=useState('');
  const [deviceError,setDeviceError]=useState('');

  async function refreshDevices(){
    try{
      if(!navigator.mediaDevices?.enumerateDevices){
        setDeviceError('浏览器不支持列出录音设备');return;
      }
      const listed=(await navigator.mediaDevices.enumerateDevices())
        .filter(item=>item.kind==='audioinput')
        .map((item,index)=>({
          deviceId:item.deviceId,label:item.label||'麦克风 '+(index+1)+'（允许权限后可显示名称）',
        }));
      setInputs(listed);
      setDeviceError(listed.length?'':'Windows / 浏览器没有枚举到任何录音设备');
      setSelectedInput(previous=>previous&&listed.some(item=>item.deviceId===previous)
        ?previous:'');
    }catch(error){
      setDeviceError('无法列出录音设备：'+String(error));
    }
  }

  useEffect(()=>{
    void refreshDevices();
    navigator.mediaDevices?.addEventListener?.('devicechange',refreshDevices);
    return()=>navigator.mediaDevices?.removeEventListener?.('devicechange',refreshDevices);
  },[]);

  function stop(update=true){
    token.current++;
    startupAbort.current?.abort();startupAbort.current=null;
    const old=active.current;active.current=null;
    if(old){
      if(old.heartbeat!==null)window.clearInterval(old.heartbeat);
      if(old.voiceWs?.readyState===WebSocket.OPEN){
        old.voiceWs.send(JSON.stringify({type:'mic.capture.stopped',data:{sender:'microphone',
          source:'microphone',instance_id:old.instanceId,host:'browser',reason:'stopped'}}));
      }
      old.voiceWs?.close();
      window.clearTimeout(old.watchdog);
      old.processor.port.postMessage({type:'stop'});
      old.processor.port.onmessage=null;old.processor.onprocessorerror=null;
      old.processor.disconnect();old.source.disconnect();old.silent.disconnect();
      old.stream.getTracks().forEach(track=>track.stop());
      old.ws.onclose=null;old.ws.close();releaseAudioContext(old.context);
    }
    onLevel?.(null);onDiagnostics?.(null);onActive?.(false);setTrackInfo('');
    if(update){setWorking(false);setListening(false);setStatus('已停止浏览器麦克风');}
  }
  useEffect(()=>{
    const onHide=()=>stop(false);
    window.addEventListener('pagehide',onHide);
    return()=>{window.removeEventListener('pagehide',onHide);stop(false);};
  },[]);

  async function start(){
    if(!route||!confirmed||working||active.current)return;
    if(!navigator.mediaDevices?.getUserMedia){
      setStatus('浏览器无麦克风权限 API：请通过 localhost 或 HTTPS 打开工作台');return;
    }
    const current=++token.current,roomId=route.room_id;
    const abort=new AbortController();startupAbort.current=abort;
    let stream:MediaStream|undefined,context:AudioContext|undefined,ws:WebSocket|undefined;
    let source:MediaStreamAudioSourceNode|undefined,processor:AudioWorkletNode|undefined,silent:GainNode|undefined;
    let handedOff=false;
    setWorking(true);setStatus('请求浏览器麦克风权限…');
    try{
      // Both calls must start during the original click. Creating/resuming
      // AudioContext after awaiting the permission dialog loses user activation
      // in some browsers, leaving an apparently live graph with zero callbacks.
      const permission=navigator.mediaDevices.getUserMedia({audio:{
        // The server owns AEC. Browser AEC/NS would corrupt the far-end
        // reference relationship; never also multiply the microphone samples.
        channelCount:1,echoCancellation:false,
        noiseSuppression:false,autoGainControl:false,
        ...(selectedInput?{deviceId:{exact:selectedInput}}:{}),
      }});
      context=acquireAudioContext();
      const resume=context.resume();
      stream=await permission;
      if(current!==token.current)return;
      void refreshDevices(); // permitted device labels become available after getUserMedia
      await resume;
      if(current!==token.current)return;
      await loadAudioWorklet(context,'/vendor/libsamplerate-2.1.2.worklet.js');
      if(current!==token.current)return;
      await loadAudioWorklet(context,'/room-capture-worklet.js?v=best-2.1.2');
      if(current!==token.current)return;
      ws=await connect(route.mic_url,roomId);
      if(current!==token.current)return;
      source=context.createMediaStreamSource(stream);
      processor=new AudioWorkletNode(context,'room-capture',{
        outputChannelCount:[1],processorOptions:{gain},
      });
      silent=context.createGain();silent.gain.value=0;
      // Keep the worklet alive while initializing, but do not attach mic audio yet.
      processor.connect(silent);silent.connect(context.destination);
      setStatus('初始化高质量重采样…');
      await waitForCaptureReady(processor,abort.signal);
      if(current!==token.current)return;
      if(ws.readyState!==WebSocket.OPEN)throw new Error('Room 在麦克风初始化期间断开');
      // Keep immutable handles: the capture callback must not read locals
      // cleared/changed by startup cleanup after it has been installed.
      const captureWs=ws,captureContext=context,captureStream=stream;
      let sent=0;
      processor.port.onmessage=event=>{
        if(active.current?.ws!==captureWs||captureWs.readyState!==WebSocket.OPEN)return;
        try{
          if(event.data.type==='error')throw new Error(String(event.data.message));
          if(event.data.type==='level'){
            setLevel(event.data.value);setClipped(event.data.clipped>0);
            onLevel?.(event.data.value);
            onDiagnostics?.({
              dbfs:event.data.dbfs,noiseFloor:event.data.noiseFloor,
              snr:event.data.snr,clipped:event.data.clipped>0,
            });
            return;
          }
          if(event.data.type!=='audio')return;
          if(captureWs.bufferedAmount>651*40)throw new Error('Room 发送积压超过 800ms');
          captureWs.send(pcmFrame(new Uint8Array(event.data.buffer)));
          const previous=sent++;
          // Keep frame feedback responsive without rendering React 50 times/s.
          if(previous===0){
            window.clearTimeout(active.current?.watchdog);
            setStatus('正在发送麦克风 → '+roomId);
          }
          if(previous===0||Math.floor(previous/10)!==Math.floor(sent/10))setFrames(sent);
        }catch(error){stop();setStatus('麦克风发送失败：'+String(error));}
      };
      processor.onprocessorerror=()=>{
        if(active.current?.ws===captureWs){stop();setStatus('麦克风音频处理器失败，请重新开启');}
      };
      const watchdog=window.setTimeout(()=>{
        if(active.current?.ws!==captureWs||sent>0)return;
        const state=captureContext.state,track=captureStream.getAudioTracks()[0];
        stop();
        setStatus('3 秒未收到麦克风音频回调：AudioContext='+state+
          '；设备='+ (track?.readyState??'none')+'；请检查浏览器麦克风授权与音频设备');
      },3000);
      const instanceId=browserInstanceId('microphone');
      active.current={ws,stream,context,source,processor,silent,watchdog,voiceWs:null,
        instanceId,heartbeat:null};
      handedOff=true;
      source.connect(processor);
      if(route.urls?.voice){
        const voiceWs=new WebSocket(route.urls.voice);
        active.current.voiceWs=voiceWs;
        voiceWs.onmessage=event=>{
          if(active.current?.ws!==captureWs||typeof event.data!=='string')return;
          try{
            const message=JSON.parse(event.data) as {
              type?:string;ok?:boolean;room?:string;robotId?:string;
            };
            if(message.type==='WELCOME'&&message.ok===true&&
               message.room===roomId&&message.robotId==='reb_voice'){
              // The same report shape as the board microphone: Brain counts
              // microphones per Room and warns when there is more than one.
              const report=(kind:'started'|'progress')=>voiceWs.send(JSON.stringify({
                type:'mic.capture.'+kind,data:{sender:'microphone',source:'microphone',
                  instance_id:instanceId,host:'browser',
                  device_name:captureStream.getAudioTracks()[0]?.label||'浏览器麦克风',
                  sample_rate:16000,sent_frames:sent,
                  room_connected:captureWs.readyState===WebSocket.OPEN}}));
              report('started');
              if(active.current?.ws===captureWs&&active.current.heartbeat===null)
                active.current.heartbeat=window.setInterval(()=>{
                  if(voiceWs.readyState===WebSocket.OPEN)report('progress');
                },2000);
            }
          }catch{/* mic PCM must not be blocked by optional status signaling */}
        };
      }
      captureWs.onclose=()=>{if(active.current?.ws===captureWs){stop();setStatus('Room 麦克风连接已断开');}};
      stream.getAudioTracks().forEach(track=>{track.onended=()=>{
        if(active.current?.stream===captureStream){stop();setStatus('麦克风设备已停止');}
      };
        track.onmute=()=>{
          if(active.current?.stream===captureStream){
            setTrackInfo('设备已被静音（track.muted=true）');
            setStatus('录音设备已被 Windows 或浏览器静音');
          }
        };
        track.onunmute=()=>{
          if(active.current?.stream===captureStream){
            setTrackInfo(track.label+' · track.muted=false');
          }
        };
      });
      setFrames(0);setLevel(0);setClipped(false);onLevel?.(0);
      const track=stream.getAudioTracks()[0];
      const settings=track?.getSettings();
      setTrackInfo('实际输入：'+(track?.label||'名称未提供')+
        ' · '+(settings?.sampleRate??'?')+'Hz'+
        ' · '+(settings?.channelCount??'?')+'ch'+
        (track?.muted?' · 系统静音':'')+
        (selectedInput&&settings?.deviceId!==selectedInput?' · 设备ID与选择不一致':'')+
        ' · BEST 连续重采样（未开启浏览器 AEC/NS/AGC）');
      setListening(true);setStatus('已连接 '+roomId+'，等待首帧…');
      onActive?.(true);
    }catch(error){
      if(current===token.current)setStatus('开启失败：'+String(error));
    }finally{
      if(startupAbort.current===abort)startupAbort.current=null;
      if(!handedOff){
        if(processor){
          processor.port.postMessage({type:'stop'});
          processor.port.onmessage=null;processor.onprocessorerror=null;
          processor.disconnect();
        }
        source?.disconnect();silent?.disconnect();
        ws?.close();
        stream?.getTracks().forEach(track=>track.stop());
        if(context)releaseAudioContext(context);
      }
      if(current===token.current)setWorking(false);
    }
  }
  // One raw PCM publisher; AEC is performed centrally before KWS and ASR.
  return <div className="voice-input voice-input-compact">
    <div className="voice-compact-bar">
      <div className="voice-compact-group mic-device-wrap">
        <select aria-label="选择麦克风输入设备" className="compact-device-select" value={selectedInput}
          disabled={listening||working} onChange={event=>setSelectedInput(event.target.value)}>
          <option value="">浏览器默认麦克风</option>
          {inputs.filter(item=>item.deviceId&&item.deviceId!=='default').map(item=>
            <option key={item.deviceId} value={item.deviceId}>{item.label}</option>)}
        </select>
        <button className="quiet-button compact-icon-btn" type="button" disabled={listening||working}
          onClick={()=>void refreshDevices()} title="刷新设备">刷新</button>
      </div>

      <div className="voice-compact-group voice-compact-action-row">
        <button type="button" className={listening||working?'stop-button compact-btn':'primary-button compact-btn'}
          onClick={()=>listening||working?stop():void start()}
          disabled={!listening&&!working&&(!route||!confirmed)}>
          {listening?'■ 停止麦克风':working?'取消开启':'🎙 开启麦克风'}</button>
        <span className="compact-status-badge" role="status">
          {listening?`${frames}帧 · ${(level??0).toFixed(0)}%`:status}
          {clipped?' ⚠️削波':''}
        </span>
      </div>

      <div className="voice-compact-group voice-compact-sub-row">
        <label className="voice-confirm compact-confirm">
          <input type="checkbox" checked={confirmed} disabled={listening||working}
            onChange={event=>setConfirmed(event.target.checked)}/>
          <span>此 Room 没有另一个麦克风占用</span>
        </label>
        <span className="mic-gain-control compact-gain">原始 PCM · 云端 AEC</span>
      </div>
    </div>
    {deviceError&&<small role="alert" className="mic-signal" style={{marginTop:'2px'}}>{deviceError}</small>}
    {listening&&frames>100&&level<0.1&&<small className="mic-signal" style={{marginTop:'2px'}}>仅检测到近静音，请核对实际输入设备与音量</small>}
  </div>;
}
