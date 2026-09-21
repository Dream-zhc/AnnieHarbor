import {useEffect,useRef,useState} from 'react';
import {decodeRoomTts,type RoomTtsPacket} from './room-tts-pcm';

type Speaker={
  ws:WebSocket|null;context:AudioContext;nodes:Set<AudioBufferSourceNode>;
  timer:number|null;stream:string;stopped:string;seq:number;endSample:number;
  nextAt:number;frames:number;final:boolean;
};

/** Opt-in local audition of the existing Room stream. No Provider call or playback receipt. */
export function RoomDebugSpeaker({url,roomId,available}:{url?:string;roomId?:string;available:boolean}){
  const active=useRef<Speaker|null>(null);
  const [status,setStatus]=useState('未开启');
  const [frames,setFrames]=useState(0);
  const [started,setStarted]=useState(false);

  function clearAudio(s:Speaker){
    for(const node of s.nodes){node.onended=null;try{node.stop();}catch{/* already ended */}node.disconnect();}
    s.nodes.clear();s.nextAt=0;
  }
  function stop(update=true){
    const s=active.current;active.current=null;
    if(s){
      if(s.timer!==null)window.clearTimeout(s.timer);
      if(s.ws){s.ws.onmessage=s.ws.onerror=s.ws.onclose=null;s.ws.close();}
      clearAudio(s);void s.context.close();
    }
    if(update){setStarted(false);setStatus('已关闭，网页不再接收或播放 Room TTS');}
  }
  useEffect(()=>{
    const hide=()=>stop(false);
    window.addEventListener('pagehide',hide);
    return()=>{window.removeEventListener('pagehide',hide);stop(false);};
  },[url,roomId]);

  function play(s:Speaker,packet:RoomTtsPacket){
    const key=packet.conversationId+'\u0000'+packet.epoch;
    if(packet.kind==='stop'){
      s.stopped=key;
      if(s.stream===key){clearAudio(s);s.stopped=key;s.final=false;setStatus('已收到 Room 停播指令，网页音频已停止');}
      return;
    }
    if(packet.kind==='end'){
      if(s.stream===key){s.final=true;if(s.nodes.size===0)setStatus('本轮网页试听结束（非真机播放回执）');}
      return;
    }
    if(s.stopped===key)return;
    if(s.stream!==key){
      clearAudio(s);s.stream=key;s.seq=0;s.endSample=packet.startSample;s.final=false;
    }
    if(packet.seq<=s.seq)return;
    if(s.seq&&packet.startSample!==s.endSample){
      clearAudio(s);setStatus('Room TTS 音频序号/游标断续，已跳过缺失部分');
    }
    s.seq=packet.seq;s.endSample=packet.endSample;
    const now=s.context.currentTime;
    if(s.nextAt-now>0.5){
      clearAudio(s);setStatus('网页试听积压超过 500ms，已丢弃旧缓存');
    }
    const buffer=s.context.createBuffer(1,packet.samples.length,24000);
    buffer.getChannelData(0).set(packet.samples);
    const node=s.context.createBufferSource();node.buffer=buffer;
    node.connect(s.context.destination);
    const at=Math.max(now+0.04,s.nextAt);
    s.nextAt=at+packet.samples.length/24000;
    s.nodes.add(node);
    node.onended=()=>{
      node.disconnect();s.nodes.delete(node);
      if(active.current===s&&s.final&&s.nodes.size===0){
        setStatus('本轮网页试听结束（非真机播放回执）');
      }
    };
    node.start(at);
    s.frames++;
    if(s.frames===1||s.frames%25===0){
      setFrames(s.frames);setStatus('正在试听 '+roomId+' / reb_tts');
    }
  }

  async function start(){
    if(active.current||!available||!roomId||!url)return;
    let s:Speaker|null=null;
    try{
      // AudioContext starts inside the click gesture, before any await.
      const context=new AudioContext();
      s={context,ws:null,nodes:new Set(),timer:null,stream:'',stopped:'',seq:0,
        endSample:0,nextAt:0,frames:0,final:false};
      active.current=s;setStatus('正在连接 Room TTS…');setFrames(0);
      await context.resume();
      if(active.current!==s)return;
      const current=s;
      const ws=new WebSocket(url);current.ws=ws;
      let stage:'welcome'|'ack'|'listening'='welcome';
      current.timer=window.setTimeout(()=>{
        if(active.current===current){stop(false);setStarted(false);setStatus('连接 Room TTS 超时');}
      },4000);
      const fail=(error:string)=>{
        if(active.current===current){stop(false);setStarted(false);setStatus('网页调试扬声器：'+error);}
      };
      ws.onerror=()=>fail('Room TTS WebSocket 连接失败');
      ws.onclose=()=>fail('Room TTS 已断开，请重新开启');
      ws.onmessage=event=>{
        if(active.current!==current||typeof event.data!=='string')return;
        try{
          const message=JSON.parse(event.data) as Record<string,unknown>;
          if(stage==='welcome'){
            if(message.type!=='WELCOME'||message.ok!==true||message.room!==roomId||message.robotId!=='reb_tts'){
              throw Error('Room TTS WELCOME 身份不匹配');
            }
            stage='ack';ws.send(JSON.stringify({type:'SUBSCRIBE',data:{types:['tts']}}));return;
          }
          if(stage==='ack'){
            const data=message.data as {what?:string;subscriptions?:string[]}|undefined;
            if(message.type!=='ACK'||data?.what!=='subscribed'||
              !data.subscriptions?.some(name=>name.toLowerCase()==='tts'||name==='*')){
              throw Error('Room TTS 订阅未确认');
            }
            stage='listening';if(current.timer!==null)window.clearTimeout(current.timer);
            current.timer=null;setStarted(true);setStatus('已订阅 '+roomId+' / reb_tts，等待新语音');return;
          }
          const packet=decodeRoomTts(message);
          if(packet)play(current,packet);
        }catch(error){fail(error instanceof Error?error.message:'TTS 数据无效');}
      };
    }catch(error){
      if(s&&active.current===s){stop(false);setStarted(false);setStatus('开启失败：'+String(error));}
    }
  }

  return <div className="room-debug-speaker">
    <button type="button" className={active.current?'stop-button':'quiet-button'}
      aria-pressed={started} disabled={!active.current&&(!url||!roomId||!available)}
      onClick={()=>active.current?stop():void start()}>
      {active.current?'关闭网页调试扬声器':'开启网页调试扬声器'}
    </button>
    <small role="status">{available?status:'当前机器人未启用 TTS'}{started&&frames>0?' · 已接收 '+frames+' 帧':''}</small>
    <p>只听当前 Room 新到达的 reb_tts 音频；不回放旧语音、不二次合成、不上报真机已播。请使用耳机，避免麦克风回声。</p>
  </div>;
}
