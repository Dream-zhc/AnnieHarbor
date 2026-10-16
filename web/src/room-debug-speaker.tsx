import {useEffect,useRef,useState} from 'react';
import {decodeRoomTts,type RoomTtsPacket} from './room-tts-pcm';
import {acquireAudioContext,loadAudioWorklet,releaseAudioContext} from './shared-audio-context';
import {browserInstanceId} from './audio-devices';

type Speaker={
  ws:WebSocket|null;voiceWs:WebSocket|null;voiceReady:boolean;
  context:AudioContext;node:AudioWorkletNode|null;gain:GainNode|null;
  timer:number|null;stream:string;stopped:string;seq:number;endSample:number;
  frames:number;final:boolean;held:boolean;playbackId:string;reportSeq:number;
  instanceId:string;heartbeat:number|null;
};

/** The audition is a speaker in the Room too; Brain warns if another one plays. */
function presence(s:Speaker,state?:string){
  if(s.voiceWs?.readyState!==WebSocket.OPEN)return;
  s.voiceWs.send(JSON.stringify({type:'speaker.presence',description:'网页试听在线',ts:Date.now(),
    data:{protocol_version:1,sender:'browser_test',source:'browser_test',instance_id:s.instanceId,
      host:'browser',device_name:'网页试听',state:state??(s.held?'held':s.stream?'playing':'idle')}}));
}

/** Mac/browser audition reports ONLY browser_test, never claims physical speaker success. */
export function RoomDebugSpeaker({url,voiceUrl,roomId,available,command,onBuffer,onActive}:{url?:string;voiceUrl?:string;roomId?:string;
  available:boolean;command?:{seq:number;kind:'hold'|'resume';turnId:string};
  onBuffer?:(bufferMs:number|null)=>void;onActive?:(active:boolean)=>void}){
  const active=useRef<Speaker|null>(null);
  const [status,setStatus]=useState('未开启');
  const [frames,setFrames]=useState(0);
  const [started,setStarted]=useState(false);
  const [bufferMs,setBufferMs]=useState(0);
  const [underruns,setUnderruns]=useState(0);

  function report(s:Speaker,type:string,playedMs:number,commandSeq?:number|null){
    if(!s.voiceReady||s.voiceWs?.readyState!==WebSocket.OPEN||!s.stream)return;
    const [conversationId,epoch]=s.stream.split('\u0000');
    const data:Record<string,unknown>={
      protocol_version:1,conversation_id:conversationId,stream_epoch:Number(epoch),
      playback_id:s.playbackId,source:'browser_test',sender:'browser_test',
      sample_rate:24000,seq:++s.reportSeq,
      played_sample_count:Math.min(s.endSample,Math.max(0,Math.round(playedMs*24))),
    };
    if(commandSeq!=null)data.command_seq=commandSeq;
    const descriptions:Record<string,string>={
      started:'网页试听开始播放',progress:'网页试听已消费的音频进度',
      held:'网页试听已暂停',resumed:'网页试听已恢复',
      stopped:'网页试听已停止',drained:'网页试听已播放完毕',
    };
    s.voiceWs.send(JSON.stringify({type:'tts.playback.'+type,
      description:descriptions[type],ts:Date.now(),data}));
  }

  function control(s:Speaker,kind:'hold'|'resume',commandSeq?:number,fadeMs?:number){
    if(!s.node||!s.gain)return;
    const gain=s.gain.gain,now=s.context.currentTime;
    if(kind==='hold'&&!s.held){
      s.held=true;
      gain.cancelScheduledValues(now);gain.setValueAtTime(gain.value,now);
      gain.linearRampToValueAtTime(0,now+Math.max(0,Math.min(fadeMs??40,150))/1000);
      s.node.port.postMessage({type:'hold',stream:s.stream,command_seq:commandSeq,
        fade_ms:fadeMs??40});
      setStatus('用户起声 · 渐弱并冻结未播 PCM');
    }else if(kind==='resume'&&s.held){
      s.held=false;
      gain.cancelScheduledValues(now);gain.setValueAtTime(0,now);
      gain.linearRampToValueAtTime(1,now+Math.max(0,Math.min(fadeMs??120,250))/1000);
      s.node.port.postMessage({type:'resume',stream:s.stream,command_seq:commandSeq});
      setStatus('无效打断 · 原位置渐强恢复');
    }
  }

  function clearAudio(s:Speaker){
    if(s.stream)s.node?.port.postMessage({type:'clear',stream:s.stream});
    s.held=false;
  }
  function stop(update=true){
    const s=active.current;active.current=null;
    if(s){
      if(s.timer!==null)window.clearTimeout(s.timer);
      if(s.heartbeat!==null)window.clearInterval(s.heartbeat);
      presence(s,'stopped');
      if(s.ws){s.ws.onmessage=s.ws.onerror=s.ws.onclose=null;s.ws.close();}
      if(s.voiceWs){s.voiceWs.onmessage=s.voiceWs.onerror=s.voiceWs.onclose=null;s.voiceWs.close();}
      clearAudio(s);s.node?.disconnect();s.gain?.disconnect();releaseAudioContext(s.context);
    }
    onBuffer?.(null);
    onActive?.(false);
    if(update){setStarted(false);setStatus('已关闭，网页不再接收或播放 Room TTS');}
  }
  useEffect(()=>{
    const hide=()=>stop(false);
    window.addEventListener('pagehide',hide);
    return()=>{window.removeEventListener('pagehide',hide);stop(false);};
  },[url,voiceUrl,roomId]);

  useEffect(()=>{
    const s=active.current;
    if(!s||s.voiceReady||!command||!s.stream.startsWith(command.turnId+'\u0000'))return;
    control(s,command.kind);
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
      s.playbackId=crypto.randomUUID();s.reportSeq=0;
      s.gain?.gain.setValueAtTime(1,s.context.currentTime);
      s.node?.port.postMessage({type:'generation',stream:key});
      setBufferMs(0);onBuffer?.(0);setUnderruns(0);
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
      onBuffer?.(0);
      const context=acquireAudioContext();
      s={context,ws:null,voiceWs:null,voiceReady:false,node:null,gain:null,timer:null,
        stream:'',stopped:'',seq:0,endSample:0,frames:0,final:false,held:false,
        playbackId:'',reportSeq:0,instanceId:browserInstanceId('speaker'),heartbeat:null};
      active.current=s;setStatus('正在连接 Room TTS…');setFrames(0);
      await context.resume();
      if(active.current!==s)return;
      await loadAudioWorklet(context,'/room-player-worklet.js');
      if(active.current!==s)return;
      const node=new AudioWorkletNode(context,'room-player',{outputChannelCount:[1]});
      const gain=context.createGain();node.connect(gain).connect(context.destination);
      s.node=node;s.gain=gain;
      const current=s;
      node.port.onmessage=event=>{
        if(active.current!==current||event.data.stream!==current.stream)return;
        const message=event.data;
        if(message.type==='buffer'){
          setBufferMs(Math.round(message.bufferMs));onBuffer?.(Math.round(message.bufferMs));setUnderruns(message.underruns);
          // AudioWorklet buffer reports are local UI telemetry, not Room heartbeats.
          // Lifecycle receipts carry the actual sample cursor when needed.
        }else if(message.type==='started'){
          report(current,'started',message.playedMs);
        }else if(message.type==='held'||message.type==='resumed'){
          if(Number.isInteger(message.command_seq))
            report(current,message.type,message.playedMs,message.command_seq);
        }else if(message.type==='ended'){
          if(current.final)report(current,'drained',message.playedMs);
          setBufferMs(0);onBuffer?.(0);setStatus('本轮网页试听结束（非真机播放回执）');
        }else if(message.type==='cleared'){
          report(current,'stopped',message.playedMs);
        }else if(message.type==='underflow'){
          setUnderruns(message.count);setStatus('播放断粮，正在重新预缓冲');
        }else if(message.type==='overflow'){
          stop(false);setStarted(false);
          setStatus('音频队列超过灾难保护上限，停止试听而非静默吞字');
        }
      };
      if(voiceUrl){
        const voice=new WebSocket(voiceUrl);current.voiceWs=voice;
        let voiceStage:'welcome'|'ack'|'listening'='welcome';
        voice.onerror=()=>setStatus('reb_voice 无法连接，仍可试听但无播放回执');
        voice.onclose=()=>{current.voiceReady=false;};
        voice.onmessage=event=>{
          if(active.current!==current||typeof event.data!=='string')return;
          try{
            const signal=JSON.parse(event.data) as Record<string,unknown>;
            if(voiceStage==='welcome'){
              if(signal.type!=='WELCOME'||signal.ok!==true||signal.room!==roomId||
                signal.robotId!=='reb_voice')throw Error('reb_voice 身份不匹配');
              voiceStage='ack';voice.send(JSON.stringify({type:'SUBSCRIBE',data:{types:['*']}}));return;
            }
            if(voiceStage==='ack'){
              const data=signal.data as {what?:string;subscriptions?:string[]}|undefined;
              if(signal.type!=='ACK'||data?.what!=='subscribed'||
                !data.subscriptions?.includes('*'))throw Error('reb_voice 订阅失败');
              voiceStage='listening';current.voiceReady=true;
              presence(current);
              if(current.heartbeat===null)
                current.heartbeat=window.setInterval(()=>presence(current),2000);
              return;
            }
            if(!['tts.playback.hold','tts.playback.resume','tts.playback.stop'].includes(
              String(signal.type)))return;
            const data=signal.data as Record<string,unknown>|undefined;
            if(!data||data.sender!=='brain'||!Number.isInteger(data.stream_epoch)||
              typeof data.conversation_id!=='string'||
              current.stream!==data.conversation_id+'\u0000'+data.stream_epoch)return;
            if(signal.type==='tts.playback.stop'){
              current.stopped=current.stream;
              clearAudio(current);current.final=false;
              setStatus('已确认打断 · 清空旧音频');
            }else if(Number.isInteger(data.command_seq)){
              control(current,signal.type==='tts.playback.hold'?'hold':'resume',
                Number(data.command_seq),Number(data.fade_ms)||undefined);
            }
          }catch{current.voiceReady=false;voice.close();
            setStatus('reb_voice 信令无效，保留 TTS 试听');}
        };
      }
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
            current.timer=null;setStarted(true);onActive?.(true);
            setStatus('已订阅 '+roomId+' / reb_tts，等待新语音');return;
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
  </div>;
}
