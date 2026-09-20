import {useEffect,useRef,useState} from 'react';
import {createMicPcmPump} from './mic-pump';

type Route={room_id:string;mic_url:string};
type Microphone={ws:WebSocket;stream:MediaStream;context:AudioContext;
  source:MediaStreamAudioSourceNode;processor:ScriptProcessorNode;silent:GainNode;watchdog:number};

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
export function RealtimeMicrophone({route}:{route:Route|undefined}){
  const active=useRef<Microphone|null>(null);
  const token=useRef(0);
  const [confirmed,setConfirmed]=useState(false);
  const [working,setWorking]=useState(false);
  const [listening,setListening]=useState(false);
  const [status,setStatus]=useState('未开启');
  const [frames,setFrames]=useState(0);

  function stop(update=true){
    token.current++;
    const old=active.current;active.current=null;
    if(old){
      window.clearTimeout(old.watchdog);
      old.processor.onaudioprocess=null;
      old.processor.disconnect();old.source.disconnect();old.silent.disconnect();
      old.stream.getTracks().forEach(track=>track.stop());
      old.ws.onclose=null;old.ws.close();void old.context.close();
    }
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
    let stream:MediaStream|undefined,context:AudioContext|undefined,ws:WebSocket|undefined;
    let source:MediaStreamAudioSourceNode|undefined,processor:ScriptProcessorNode|undefined,silent:GainNode|undefined;
    let handedOff=false;
    setWorking(true);setStatus('请求浏览器麦克风权限…');
    try{
      // Both calls must start during the original click. Creating/resuming
      // AudioContext after awaiting the permission dialog loses user activation
      // in some browsers, leaving an apparently live graph with zero callbacks.
      const permission=navigator.mediaDevices.getUserMedia({audio:{
        channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:false,
      }});
      context=new AudioContext();
      const resume=context.resume();
      stream=await permission;
      if(current!==token.current)return;
      await resume;
      if(current!==token.current)return;
      ws=await connect(route.mic_url,roomId);
      if(current!==token.current)return;
      source=context.createMediaStreamSource(stream);
      processor=context.createScriptProcessor(1024,1,1);
      silent=context.createGain();silent.gain.value=0;
      // Keep immutable handles: the capture callback must not read locals
      // cleared/changed by startup cleanup after it has been installed.
      const captureWs=ws,captureContext=context,captureStream=stream;
      const pump=createMicPcmPump();
      let sent=0;
      processor.onaudioprocess=event=>{
        if(active.current?.ws!==captureWs||captureWs.readyState!==WebSocket.OPEN)return;
        try{
          const previous=sent;
          sent=pump(event.inputBuffer.getChannelData(0),captureContext.sampleRate,frame=>{
            if(captureWs.bufferedAmount>640*40)throw new Error('Room 发送积压超过 800ms');
            captureWs.send(frame);
          });
          // Keep frame feedback responsive without rendering React 50 times/s.
          if(previous===0&&sent>0){
            window.clearTimeout(active.current?.watchdog);
            setStatus('正在发送麦克风 → '+roomId);
          }
          if(previous===0&&sent>0||Math.floor(previous/10)!==Math.floor(sent/10))setFrames(sent);
        }catch(error){stop();setStatus('麦克风发送失败：'+String(error));}
      };
      source.connect(processor);processor.connect(silent);silent.connect(context.destination);
      const watchdog=window.setTimeout(()=>{
        if(active.current?.ws!==captureWs||sent>0)return;
        const state=captureContext.state,track=captureStream.getAudioTracks()[0];
        stop();
        setStatus('3 秒未收到麦克风音频回调：AudioContext='+state+
          '；设备='+ (track?.readyState??'none')+'；请检查浏览器麦克风授权与音频设备');
      },3000);
      active.current={ws,stream,context,source,processor,silent,watchdog};
      handedOff=true;
      captureWs.onclose=()=>{if(active.current?.ws===captureWs){stop();setStatus('Room 麦克风连接已断开');}};
      stream.getAudioTracks().forEach(track=>{track.onended=()=>{
        if(active.current?.stream===captureStream){stop();setStatus('麦克风设备已停止');}
      };});
      setFrames(0);setListening(true);setStatus('已连接 '+roomId+'，等待首帧…');
    }catch(error){
      if(current===token.current)setStatus('开启失败：'+String(error));
    }finally{
      if(!handedOff){
        ws?.close();
        stream?.getTracks().forEach(track=>track.stop());
        if(context)void context.close();
      }
      if(current===token.current)setWorking(false);
    }
  }
  return <div className="voice-input">
    <label className="voice-confirm"><input type="checkbox" checked={confirmed} disabled={listening||working}
      onChange={event=>setConfirmed(event.target.checked)}/>
      <span>此 Room 没有另一个麦克风或机器人音源正在发送 PCM</span></label>
    <div className="voice-actions">
      <button type="button" className={listening||working?'stop-button':'quiet-button'}
        onClick={()=>listening||working?stop():void start()}
        disabled={!listening&&!working&&(!route||!confirmed)}>
        {listening?'停止麦克风':working?'取消开启':'开启麦克风'}</button>
      <span role="status">{status}{listening?' · 已发 '+frames+' 帧':''}</span>
    </div>
  </div>;
}
