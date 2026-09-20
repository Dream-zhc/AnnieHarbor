import {useEffect,useRef,useState} from 'react';
import {pcmFrame} from './pcm-format';

type Route={room_id:string;mic_url:string};
type Microphone={ws:WebSocket;stream:MediaStream;context:AudioContext;
  source:MediaStreamAudioSourceNode;processor:ScriptProcessorNode;silent:GainNode};

function pcm(samples:number[]){
  const bytes=new Uint8Array(640),view=new DataView(bytes.buffer);
  for(let i=0;i<320;i++){
    const sample=Math.max(-1,Math.min(1,samples[i]));
    view.setInt16(i*2,sample<0?sample*32768:sample*32767,true);
  }
  return pcmFrame(bytes);
}

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
    setWorking(true);setStatus('请求浏览器麦克风权限…');
    try{
      // Request permission during the click gesture, before any network handshake.
      stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1}});
      if(current!==token.current)return;
      context=new AudioContext();
      await context.resume();
      if(current!==token.current)return;
      ws=await connect(route.mic_url,roomId);
      if(current!==token.current)return;
      source=context.createMediaStreamSource(stream);
      processor=context.createScriptProcessor(1024,1,1);
      silent=context.createGain();silent.gain.value=0;
      let pending:number[]=[];
      let offset=0;
      let sent=0;
      processor.onaudioprocess=event=>{
        if(ws?.readyState!==WebSocket.OPEN||!context)return;
        const input=event.inputBuffer.getChannelData(0);
        const ratio=context.sampleRate/16000;
        if(ratio<1){stop();setStatus('麦克风采样率低于 16kHz，已停止');return;}
        // Carry fractional resampling phase across browser audio callbacks.
        for(let pos=offset;pos<input.length;pos+=ratio){
          const a=Math.floor(pos),b=Math.min(input.length,Math.max(a+1,Math.floor(pos+ratio)));
          let total=0;for(let i=a;i<b;i++)total+=input[i];
          pending.push(total/(b-a));
        }
        offset=(offset+Math.ceil((input.length-offset)/ratio)*ratio)-input.length;
        while(pending.length>=320){
          if(ws.bufferedAmount>640*40){
            stop();setStatus('Room 发送积压超过 800ms，麦克风已停止');return;
          }
          ws.send(pcm(pending.splice(0,320)));
          // Do not render React 50 times/second during realtime PCM capture.
          if(++sent%25===0)setFrames(sent);
        }
      };
      source.connect(processor);processor.connect(silent);silent.connect(context.destination);
      active.current={ws,stream,context,source,processor,silent};
      ws.onclose=()=>{if(active.current?.ws===ws){stop();setStatus('Room 麦克风连接已断开');}};
      stream.getAudioTracks().forEach(track=>{track.onended=()=>{
        if(active.current?.stream===stream){stop();setStatus('麦克风设备已停止');}
      };});
      setFrames(0);setListening(true);setStatus('正在发送麦克风 → '+roomId);
      ws=undefined;stream=undefined;context=undefined;
    }catch(error){
      if(current===token.current)setStatus('开启失败：'+String(error));
    }finally{
      if(ws)ws.close();
      stream?.getTracks().forEach(track=>track.stop());
      if(context)void context.close();
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
