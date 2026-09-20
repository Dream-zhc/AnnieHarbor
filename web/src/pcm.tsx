import {useEffect,useRef,useState} from 'react';
import {pcmFrame} from './pcm-format';

export type RoomRoute={robot_id:string;room_id:string;mic_url:string;connected:boolean;accepted:number;frames_received:number;last_error:string|null;urls:unknown};
type Mic={ws:WebSocket;stream:MediaStream;context:AudioContext;source:MediaStreamAudioSourceNode;processor:ScriptProcessorNode;silent:GainNode};
function downsample(input:Float32Array,rate:number){
  const ratio=rate/16000;
  if(ratio<1)throw new Error('采样率低于 16kHz');
  const output:number[]=[];
  for(let i=0;i<Math.floor(input.length/ratio);i++){
    const start=Math.floor(i*ratio),end=Math.min(input.length,Math.floor((i+1)*ratio));
    let sum=0;for(let j=start;j<end;j++)sum+=input[j];
    output.push(sum/Math.max(1,end-start));
  }
  return output;
}
function toPcm(samples:number[]){
  const result=new Uint8Array(640),view=new DataView(result.buffer);
  for(let i=0;i<320;i++){const v=Math.max(-1,Math.min(1,samples[i]));view.setInt16(i*2,v<0?v*32768:v*32767,true);}
  return result;
}
async function openPublisher(url:string){
  return new Promise<WebSocket>((resolve,reject)=>{
    const ws=new WebSocket(url);ws.binaryType='arraybuffer';
    const timeout=window.setTimeout(()=>{ws.close();reject(new Error('Room WELCOME 超时'));},3000);
    ws.onerror=()=>{window.clearTimeout(timeout);reject(new Error('Room WebSocket 连接失败'));};
    ws.onmessage=event=>{
      if(typeof event.data!=='string')return;
      try{if(JSON.parse(event.data).type==='WELCOME'){window.clearTimeout(timeout);resolve(ws);}}catch{/* ignore other Room messages */}
    };
  });
}
export function RoomInput({robotId,route}:{robotId:string;route:RoomRoute|undefined}){
  const mic=useRef<Mic|null>(null),token=useRef(0);
  const [confirmed,setConfirmed]=useState(false),[status,setStatus]=useState('未启用'),[frames,setFrames]=useState(0);
  const [working,setWorking]=useState(false);
  function stop(){
    token.current++;
    const old=mic.current;mic.current=null;
    if(old){
      old.processor.onaudioprocess=null;old.processor.disconnect();old.source.disconnect();old.silent.disconnect();
      old.stream.getTracks().forEach(track=>track.stop());old.ws.close();void old.context.close();
    }
    setWorking(false);setStatus('已停止');
  }
  useEffect(()=>{
    const onHide=()=>stop();
    window.addEventListener('pagehide',onHide);
    return()=>{window.removeEventListener('pagehide',onHide);stop();};
  },[]);
  async function sendFrame(){
    if(!route||!confirmed||working)return;
    setWorking(true);
    let ws:WebSocket|undefined;
    const current=++token.current;
    try{
      ws=await openPublisher(route.mic_url);
      if(current!==token.current){ws.close();return;}
      ws.send(pcmFrame(new Uint8Array(640)));
      setFrames(value=>value+1);setStatus(`测试 PCM 已送往 ${route.room_id}；请确认 accepted 计数。`);
    }catch(error){setStatus(String(error));}
    finally{ws?.close();setWorking(false);}
  }
  async function start(){
    if(!route||!confirmed||mic.current||working)return;
    if(!navigator.mediaDevices?.getUserMedia){setStatus('需要 localhost / HTTPS 麦克风权限');return;}
    const current=++token.current;
    setWorking(true);
    let ws:WebSocket|undefined,stream:MediaStream|undefined,context:AudioContext|undefined;
    try{
      ws=await openPublisher(route.mic_url);
      if(current!==token.current){ws.close();return;}
      stream=await navigator.mediaDevices.getUserMedia({audio:true});
      if(current!==token.current){stream.getTracks().forEach(t=>t.stop());ws.close();return;}
      context=new AudioContext();
      const source=context.createMediaStreamSource(stream),processor=context.createScriptProcessor(1024,1,1);
      const silent=context.createGain();silent.gain.value=0;
      let pending:number[]=[];
      processor.onaudioprocess=event=>{
        if(ws?.readyState!==WebSocket.OPEN)return;
        pending.push(...downsample(event.inputBuffer.getChannelData(0),context!.sampleRate));
        while(pending.length>=320){
          ws.send(pcmFrame(toPcm(pending.splice(0,320))));
          setFrames(n=>n+1);
        }
      };
      source.connect(processor);processor.connect(silent);silent.connect(context.destination);
      mic.current={ws,stream,context,source,processor,silent};setStatus(`麦克风发送中 → ${route.room_id}`);
    }catch(error){
      ws?.close();stream?.getTracks().forEach(track=>track.stop());if(context)void context.close();
      setStatus(String(error));
    }finally{setWorking(false);}
  }
  return <div className="mic">
    <label><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)}/>
      我已确认此 Room 没有其他麦克风或机器人音源正在发布 PCM</label>
    <div className="actions">
      <button disabled={!route||!confirmed||working||!!mic.current} onClick={()=>void sendFrame()}>发送 20ms 测试 PCM</button>
      <button disabled={!route||!confirmed||working||!!mic.current} onClick={()=>void start()}>开启浏览器麦克风</button>
      <button disabled={!mic.current} onClick={stop}>停止麦克风</button>
    </div>
    <p role="status">{robotId}：{status} / 本页发送帧 {frames}</p>
  </div>;
}
