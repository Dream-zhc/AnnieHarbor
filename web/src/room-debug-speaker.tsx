import {useEffect,useRef,useState} from 'react';
import {decodeRoomTts,type RoomTtsPacket} from './room-tts-pcm';

type Speaker={
  ws:WebSocket|null;context:AudioContext;node:AudioWorkletNode|null;gain:GainNode|null;
  timer:number|null;stream:string;stopped:string;seq:number;endSample:number;
  frames:number;final:boolean;held:boolean;
};

/** Opt-in local audition of the existing Room stream. No Provider call or playback receipt. */
export function RoomDebugSpeaker({url,roomId,available,command}:{url?:string;roomId?:string;
  available:boolean;command?:{seq:number;kind:'hold'|'resume';turnId:string}}){
  const active=useRef<Speaker|null>(null);
  const [status,setStatus]=useState('未开启');
  const [frames,setFrames]=useState(0);
  const [started,setStarted]=useState(false);
  const [bufferMs,setBufferMs]=useState(0);
  const [underruns,setUnderruns]=useState(0);

  function clearAudio(s:Speaker){
    if(s.stream)s.node?.port.postMessage({type:'clear',stream:s.stream});
    s.held=false;
  }
  function stop(update=true){
    const s=active.current;active.current=null;
    if(s){
      if(s.timer!==null)window.clearTimeout(s.timer);
      if(s.ws){s.ws.onmessage=s.ws.onerror=s.ws.onclose=null;s.ws.close();}
      clearAudio(s);s.node?.disconnect();s.gain?.disconnect();void s.context.close();
    }
    if(update){setStarted(false);setStatus('已关闭，网页不再接收或播放 Room TTS');}
  }
  useEffect(()=>{
    const hide=()=>stop(false);
    window.addEventListener('pagehide',hide);
    return()=>{window.removeEventListener('pagehide',hide);stop(false);};
  },[url,roomId]);

  useEffect(()=>{
    const s=active.current;
    if(!s?.node||!s.gain||!command||!s.stream.startsWith(command.turnId+'\u0000'))return;
    if(command.kind==='hold'&&!s.held){
      s.held=true;
      const gain=s.gain.gain,now=s.context.currentTime;
      gain.cancelScheduledValues(now);gain.setValueAtTime(gain.value,now);
      gain.linearRampToValueAtTime(0,now+.003);
      s.node.port.postMessage({type:'hold',stream:s.stream});
      setStatus('暂时静音，保留完整音频等待 smart_turn 判定');
    }else if(command.kind==='resume'&&s.held){
      s.held=false;
      s.node.port.postMessage({type:'resume',stream:s.stream});
      const gain=s.gain.gain,now=s.context.currentTime;
      gain.cancelScheduledValues(now);gain.setValueAtTime(gain.value,now);
      gain.linearRampToValueAtTime(1,now+.012);
      setStatus('误判已恢复，从原采样位置继续播放');
    }
  },[command?.seq,command?.kind,command?.turnId]);

  function play(s:Speaker,packet:RoomTtsPacket){
    const key=packet.conversationId+'\u0000'+packet.epoch;
    if(packet.kind==='stop'){
      s.stopped=key;
      if(s.stream===key){clearAudio(s);s.stopped=key;s.final=false;
        setStatus('已收到 Room 停播指令，网页音频已停止');}
      return;
    }
    if(packet.kind==='end'){
      if(s.stream===key){s.final=true;s.node?.port.postMessage({type:'end',stream:key});}
      return;
    }
    if(s.stopped===key)return;
    if(s.stream!==key){
      clearAudio(s);s.stream=key;s.seq=0;s.endSample=packet.startSample;s.final=false;s.held=false;
      s.gain?.gain.setValueAtTime(1,s.context.currentTime);
      s.node?.port.postMessage({type:'generation',stream:key});
      setBufferMs(0);setUnderruns(0);
    }
    if(packet.seq<=s.seq)return;
    if(s.seq&&packet.startSample!==s.endSample){
      // A missing packet cannot be recovered locally. Do not discard all
      // already buffered, valid speech because of a downstream cursor gap.
      setStatus('Room TTS 游标断续，已保留已有音频；缺失部分无法恢复');
    }
    s.seq=packet.seq;s.endSample=packet.endSample;
    s.node?.port.postMessage({type:'push',stream:key,buffer:packet.samples.buffer},
      [packet.samples.buffer]);
    s.frames++;
    if(s.frames===1||s.frames%25===0){
      setFrames(s.frames);if(!s.held)setStatus('正在试听 '+roomId+' / reb_tts');
    }
  }

  async function start(){
    if(active.current||!available||!roomId||!url)return;
    let s:Speaker|null=null;
    try{
      // AudioContext starts inside the click gesture, before any await.
      const context=new AudioContext();
      s={context,ws:null,node:null,gain:null,timer:null,stream:'',stopped:'',seq:0,
        endSample:0,frames:0,final:false,held:false};
      active.current=s;setStatus('正在连接 Room TTS…');setFrames(0);
      await context.resume();
      if(active.current!==s)return;
      await context.audioWorklet.addModule('/room-player-worklet.js');
      if(active.current!==s)return;
      const node=new AudioWorkletNode(context,'room-player',{outputChannelCount:[1]});
      const gain=context.createGain();node.connect(gain).connect(context.destination);
      s.node=node;s.gain=gain;
      const current=s;
      node.port.onmessage=event=>{
        if(active.current!==current||event.data.stream!==current.stream)return;
        const message=event.data;
        if(message.type==='buffer'){
          setBufferMs(Math.round(message.bufferMs));setUnderruns(message.underruns);
        }else if(message.type==='ended'){
          setBufferMs(0);setStatus('本轮网页试听结束（非真机播放回执）');
        }else if(message.type==='underflow'){
          setUnderruns(message.count);setStatus('播放断粮，正在重新预缓冲');
        }else if(message.type==='overflow'){
          stop(false);setStarted(false);
          setStatus('音频队列超过灾难保护上限，停止试听而非静默吞字');
        }
      };
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
    <small role="status">{available?status:'当前机器人未启用 TTS'}{started&&frames>0?
      ' · 已接收 '+frames+' 帧 · 缓冲 '+bufferMs+'ms · 断粮 '+underruns+' 次':''}</small>
    <p>只听当前 Room 新到达的 reb_tts 音频；不回放旧语音、不二次合成、不上报真机已播。请使用耳机，避免麦克风回声。</p>
  </div>;
}
