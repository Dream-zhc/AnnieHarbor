import {useEffect,useRef,useState} from 'react';
import {playbackReceipt,type PlaybackKind} from './playback-contract';
import {decodeRoomPcm,type TtsPcmPacket} from './pcm-codec';

type Route={robot_id:string;room_id:string;urls:Record<string,string>};
type Mouth={start:number;end:number;viseme:string};
type Stream={
  id:string;cid:string;epoch:number;seq:number;received:number;played:number;
  final:boolean;stopped:boolean;started:boolean;mouth:Mouth[];node:AudioWorkletNode;
  peak:number;rmsSquares:number;sampleCount:number;clipped:number;underflows:number;
};
type Receiver={
  audio:AudioContext;gain:GainNode;tts:WebSocket;main:WebSocket;sam:WebSocket;
  stream:Stream|null;subscribed:boolean;samSubscribed:boolean;suppressed:Set<string>;
};
function connect(url:string,room:string,channel:string):Promise<WebSocket>{
  return new Promise((resolve,reject)=>{
    const ws=new WebSocket(url);
    let done=false;
    const fail=(reason:string)=>{if(done)return;done=true;clearTimeout(timer);ws.close();reject(new Error(reason));};
    const timer=window.setTimeout(()=>fail(channel+' 欢迎握手超时'),4000);
    ws.onerror=()=>fail(channel+' WebSocket 连接失败');
    ws.onclose=()=>fail(channel+' WebSocket 已断开');
    ws.onmessage=event=>{
      if(typeof event.data!=='string')return;
      try{
        const packet=JSON.parse(event.data) as {type?:string;ok?:boolean;room?:string;robotId?:string};
        if(packet.type!=='WELCOME')return;
        if(!packet.ok||packet.room!==room||packet.robotId!==channel){
          fail(channel+' Room 身份不匹配');return;
        }
        done=true;clearTimeout(timer);resolve(ws);
      }catch{/* invalid Room control is not a WELCOME */}
    };
  });
}

/** Real reb_tts output, not a test-frame generator or a second synthesizer. */
export function RoomPlayback({route,available}:{route:Route|undefined;available:boolean}){
  const receiver=useRef<Receiver|null>(null);
  const generation=useRef(0);
  const [confirmed,setConfirmed]=useState(false);
  const [opening,setOpening]=useState(false);
  const [connected,setConnected]=useState(false);
  const [status,setStatus]=useState('未开启本机播放');
  const [samples,setSamples]=useState(0);
  const [mouth,setMouth]=useState('—');
  const [mouthEvents,setMouthEvents]=useState(0);
  const [underflows,setUnderflows]=useState(0);
  const [sourcePeak,setSourcePeak]=useState(0);
  const [sourceClipped,setSourceClipped]=useState(0);
  const [volume,setVolume]=useState(.5);
  const latestVolume=useRef(.5);

  const receipt=(r:Receiver,s:Stream,kind:PlaybackKind,count:number)=>{
    if(r.main.readyState!==WebSocket.OPEN)return;
    try{
      r.main.send(JSON.stringify(playbackReceipt(kind,{
        conversation_id:s.cid,stream_epoch:s.epoch,playback_id:s.id,
        source:'browser_test',seq:++s.seq,played_sample_count:count,
      })));
    }catch{setStatus('浏览器播放回执发送失败');}
  };
  const stopStream=(r:Receiver,reportStopped:boolean)=>{
    const s=r.stream;if(!s)return;
    r.suppressed.add(s.cid+':'+s.epoch);
    const alreadyStopped=s.stopped;
    s.stopped=true;
    if(reportStopped&&!alreadyStopped)receipt(r,s,'stopped',s.played);
    s.node.port.onmessage=null;
    s.node.port.postMessage({type:'stop'});
    s.node.disconnect();
    r.stream=null;
    setMouth('—');
  };
  const stop=()=>{
    generation.current++;
    const r=receiver.current;receiver.current=null;
    if(r){
      stopStream(r,false);
      for(const ws of [r.tts,r.main,r.sam]){ws.onclose=null;ws.close();}
      void r.audio.close();
    }
    setConnected(false);setOpening(false);setStatus('本机播放已关闭；不代表真机已停播');
  };
  // Route switch, unmount and browser navigation must not leave another player.
  useEffect(()=>{
    const pageHide=()=>stop();
    window.addEventListener('pagehide',pageHide);
    return()=>{window.removeEventListener('pagehide',pageHide);stop();};
  },[]);
  useEffect(()=>{latestVolume.current=volume;if(receiver.current)receiver.current.gain.gain.value=volume;},[volume]);

  const failStream=(r:Receiver,message:string)=>{
    stopStream(r,true);
    setStatus(message+'；本轮已停止本机播放');
  };
  const beginStream=(r:Receiver,cid:string,epoch:number)=>{
    const node=new AudioWorkletNode(r.audio,'room-pcm-playback',{outputChannelCount:[2]});
    node.connect(r.gain);
    const stream:Stream={id:crypto.randomUUID(),cid,epoch,seq:0,received:0,played:0,
      final:false,stopped:false,started:false,mouth:[],node,
      peak:0,rmsSquares:0,sampleCount:0,clipped:0,underflows:0};
    node.port.onmessage=({data}:MessageEvent<{type:string;played?:number;count?:number;reason?:string}>)=>{
      if(r.stream!==stream||stream.stopped)return;
      const played=data.played??stream.played;
      if(data.type==='error'){failStream(r,data.reason??'AudioWorklet 错误');return;}
      if(data.type==='underrun'){
        stream.underflows=data.count??stream.underflows+1;
        setUnderflows(stream.underflows);
        setStatus('播放缓冲欠载：'+stream.underflows+' 次；检查网络或音频源');
        return;
      }
      if(data.type==='started'&&!stream.started){
        stream.started=true;receipt(r,stream,'started',0);
        setStatus('本机扬声器正在播放（browser_test）');
      }
      if(data.type==='progress'||data.type==='drained'){
        stream.played=played;
        if(stream.started&&data.type==='progress')receipt(r,stream,'progress',played);
        setSamples(played);
        setMouth(stream.mouth.find(item=>item.start<=played&&played<item.end)?.viseme??'—');
      }
      if(data.type==='drained'){
        stream.stopped=true;
        receipt(r,stream,'drained',played);
        setStatus('本机播放完成；真实设备播放证据仍须单独确认');
        stream.node.disconnect();
      }
    };
    return stream;
  };
  const onTts=(r:Receiver,packet:{type?:string;data?:TtsPcmPacket})=>{
    if(packet.type!=='tts'||!r.subscribed||!packet.data)return;
    const d=packet.data;
    if(typeof d.conversation_id!=='string'||!Number.isSafeInteger(d.stream_epoch))return;
    const key=d.conversation_id+':'+d.stream_epoch;
    if(d.stop){
      r.suppressed.add(key);
      if(r.stream&&key===r.stream.cid+':'+r.stream.epoch){
        stopStream(r,true);setStatus('收到 Room 停播；浏览器音频已截断');
      }
      return;
    }
    if(r.suppressed.has(key))return;
    let s=r.stream;
    if(s&&key!==s.cid+':'+s.epoch){stopStream(r,true);s=null;}
    if(!s&&!d.audio)return;
    if(!s){
      s=beginStream(r,d.conversation_id,d.stream_epoch);
      if(receiver.current!==r){s.node.disconnect();return;}
      r.stream=s;
      setSamples(0);setUnderflows(0);setMouthEvents(0);setMouth('—');
      setSourcePeak(0);setSourceClipped(0);
    }
    if(s.stopped)return;
    if(d.stream_final){
      s.final=true;
      setSourcePeak(Math.round(s.peak/32768*100));
      setSourceClipped(s.clipped);
      s.node.port.postMessage({type:'final'});
      return;
    }
    if(!d.audio)return; // segment-final marker, not a stream end
    let bytes:Uint8Array<ArrayBuffer>;
    try{bytes=decodeRoomPcm(d,s.received);}
    catch(error){failStream(r,String(error));return;}
    if(s.received-s.played>24000*2){
      failStream(r,'音频积压超过 2 秒');return;
    }
    const ints=new DataView(bytes.buffer);
    for(let i=0;i<bytes.byteLength;i+=2){
      const amplitude=Math.abs(ints.getInt16(i,true));
      s.peak=Math.max(s.peak,amplitude);
      s.rmsSquares+=amplitude*amplitude;
      s.sampleCount++;
      if(amplitude>=32760)s.clipped++;
    }
    s.received+=bytes.byteLength/2;
    if(s.received===bytes.byteLength/2||s.received%4800<bytes.byteLength/2){
      setSourcePeak(Math.round(s.peak/32768*100));
      setSourceClipped(s.clipped);
    }
    s.node.port.postMessage({type:'pcm',buffer:bytes.buffer},[bytes.buffer]);
  };
  async function start(){
    if(opening||receiver.current||!route||!available||!confirmed)return;
    const ticket=++generation.current;
    let audio:AudioContext|undefined,tts:WebSocket|undefined,main:WebSocket|undefined,sam:WebSocket|undefined;
    setOpening(true);setStatus('正在启动本机音频…');
    try{
      // Browser user activation must precede any awaited network request.
      audio=new AudioContext();
      const resumed=audio.resume();
      await audio.audioWorklet.addModule('/pcm-playback-processor.js');
      await resumed;
      if(audio.state!=='running')throw new Error('AudioContext 没有运行');
      if(ticket!==generation.current)return;
      tts=await connect(route.urls.tts,route.room_id,'reb_tts');
      if(ticket!==generation.current)return;
      main=await connect(route.urls.main,route.room_id,'reb_main');
      if(ticket!==generation.current)return;
      sam=await connect(route.urls.sam,route.room_id,'reb_sam');
      if(ticket!==generation.current)return;
      const gain=audio.createGain();gain.gain.value=latestVolume.current;gain.connect(audio.destination);
      const r:Receiver={audio,gain,tts,main,sam,stream:null,subscribed:false,
        samSubscribed:false,suppressed:new Set()};
      receiver.current=r;
      tts.onmessage=event=>{
        if(typeof event.data!=='string')return;
        try{
          const packet=JSON.parse(event.data);
          if(packet.type==='ACK'){
            r.subscribed=packet.data?.what==='subscribed'&&packet.data.subscriptions?.includes('*');
            if(r.subscribed){setConnected(true);setStatus('本机已订阅 reb_tts，等待实时回复');}
            else setStatus('reb_tts 订阅失败');
          }else{
            try{onTts(r,packet);}catch(error){failStream(r,String(error));}
          }
        }catch{setStatus('reb_tts 收到无效 JSON');}
      };
      sam.onmessage=event=>{
        if(typeof event.data!=='string')return;
        try{
          const p=JSON.parse(event.data);
          if(p.type==='ACK'){r.samSubscribed=true;return;}
          const s=r.stream;
          if(p.type!=='mouth_script'||!r.samSubscribed||!s||s.stopped||
            p.conversation_id!==s.cid||p.stream_epoch!=null&&p.stream_epoch!==s.epoch||
            !p.timing_source?.startsWith('qwen3_forced_aligner')||
            !Number.isSafeInteger(p.start_sample)||!Number.isSafeInteger(p.end_sample)||
            p.start_sample<0||p.end_sample<=p.start_sample)return;
          s.mouth.push({start:p.start_sample,end:p.end_sample,viseme:String(p.viseme??'—')});
          if(s.mouth.length>2048)s.mouth.shift();
          setMouthEvents(s.mouth.length);
        }catch{/* ignore malformed observer packet */}
      };
      for(const ws of [tts,sam])ws.send(JSON.stringify({type:'SUBSCRIBE',data:{types:['*']}}));
      for(const ws of [tts,main,sam])ws.onclose=()=>{
        if(receiver.current===r){stop();setStatus('本机播放 Room 已断开');}
      };
      setStatus('已连接 Room，正在订阅音频；设备输出 '+audio.sampleRate+' Hz');
      // Connection ownership transferred: only stop() may close these handles.
      audio=undefined;tts=undefined;main=undefined;sam=undefined;
    }catch(error){
      if(ticket===generation.current)setStatus('本机播放失败：'+String(error));
    }finally{
      for(const ws of [tts,main,sam])ws?.close();
      if(audio)void audio.close();
      if(ticket===generation.current)setOpening(false);
    }
  }
  return <div className="room-playback">
    <h4>本机语音播放</h4>
    <label className="voice-confirm"><input type="checkbox" checked={confirmed} disabled={opening||connected}
      onChange={event=>setConfirmed(event.target.checked)}/>
      <span>已佩戴耳机／确认不会与机器人扬声器产生回授，且没有第二个网页播放器</span></label>
    <div className="voice-actions">
      <button type="button" className={connected?'stop-button':'quiet-button'}
        disabled={!connected&&(opening||!available||!confirmed||!route)}
        onClick={()=>connected?stop():void start()}>
        {connected?'关闭本机播放':opening?'连接中…':'开启本机播放'}</button>
      <label className="playback-volume">音量 <input aria-label="本机播放音量" type="range" min="0" max="1" step=".05"
        value={volume} onChange={event=>setVolume(Number(event.target.value))}/></label>
    </div>
    <p className="playback-state" role="status">{status}</p>
    <div className="playback-metrics">
      <span>PCM：24kHz · mono · S16LE</span>
      <span>已播放：{samples} 样本</span>
      <span>嘴型：{mouth}（事件 {mouthEvents}）</span>
      <span>欠载：{underflows}</span>
      <span>源峰值：{sourcePeak}%</span>
      <span>削波样本：{sourceClipped}</span>
    </div>
    {sourceClipped>0&&<p className="inline-error" role="status">收到的 TTS 源 PCM 已出现削波样本：{sourceClipped}。降低网页音量无法恢复源音频；请先用耳机对比，排除麦克风回授，并核对原始 TTS 音频。</p>}
    <small>浏览器回执为 browser_test，不代表机器人真实扬声器已播；开启时避免扬声器与麦克风回授。</small>
  </div>;
}
