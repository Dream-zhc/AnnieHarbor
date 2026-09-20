import {useEffect,useRef,useState} from 'react';
import type {RoomRoute} from './pcm';
import {playbackReceipt,type PlaybackKind} from './playback-contract';

type AudioStream={
  cid:string;epoch:number;playbackId:string;seq:number;received:number;plannedAt:number;
  startAt:number;started:boolean;ended:boolean;final:boolean;played:number;
  nodes:Set<AudioBufferSourceNode>;mouth:Map<string,{start:number;end:number;viseme:string}>;
};
type Receiver={tts:WebSocket;main:WebSocket;sam:WebSocket;audio:AudioContext;stream:AudioStream|null;timer:number;suppressed:Set<string>;ttsSubscribed:boolean;samSubscribed:boolean};

function connectRoom(url:string,room:string,channel:string):Promise<WebSocket>{
  return new Promise((resolve,reject)=>{
    const ws=new WebSocket(url);
    const timeout=window.setTimeout(()=>{ws.close();reject(new Error('Room WELCOME 超时'));},4000);
    ws.onerror=()=>{window.clearTimeout(timeout);reject(new Error(channel+' 连接失败'));};
    ws.onmessage=event=>{
      if(typeof event.data!=='string')return;
      try{
        const packet=JSON.parse(event.data) as {type?:string;room?:string;robotId?:string;ok?:boolean};
        if(packet.type==='WELCOME'){
          window.clearTimeout(timeout);
          if(packet.room!==room||packet.robotId!==channel||!packet.ok){
            ws.close();reject(new Error('Room 身份不匹配'));return;
          }
          resolve(ws);
        }
      }catch{/* discard non-JSON control messages */}
    };
  });
}

export function RoomPlaybackTester({route,onCancel}:{route:RoomRoute|undefined;onCancel:()=>void}){
  const active=useRef<Receiver|null>(null);
  const [enabled,setEnabled]=useState(false),[confirmed,setConfirmed]=useState(false);
  const [status,setStatus]=useState('未连接');
  const [cursor,setCursor]=useState(0),[mouth,setMouth]=useState('—'),[report,setReport]=useState('unknown');
  function send(receiver:Receiver,stream:AudioStream,kind:PlaybackKind,samples:number){
    if(receiver.main.readyState!==WebSocket.OPEN)return;
    stream.seq++;
    const message=playbackReceipt(kind,{
      conversation_id:stream.cid,stream_epoch:stream.epoch,playback_id:stream.playbackId,
      source:'browser_test',seq:stream.seq,played_sample_count:samples,
    });
    receiver.main.send(JSON.stringify(message));
    setReport('browser_test_reported_'+kind);
  }
  function played(receiver:Receiver,stream:AudioStream){
    return stream.started?Math.max(stream.played,Math.min(stream.received,
      Math.floor(Math.max(0,receiver.audio.currentTime-stream.startAt)*24000))):0;
  }
  function stopStream(receiver:Receiver,sendReceipt:boolean){
    const stream=receiver.stream;
    if(!stream)return;
    receiver.suppressed.add(stream.cid+':'+stream.epoch);
    const count=played(receiver,stream);
    const wasEnded=stream.ended;
    stream.ended=true;
    for(const node of stream.nodes){try{node.stop();}catch{/* already stopped */}node.disconnect();}
    stream.nodes.clear();
    if(sendReceipt&&!wasEnded)send(receiver,stream,'stopped',count);
    receiver.stream=null;
    setCursor(count);setMouth('—');
  }
  function close(){
    const receiver=active.current;active.current=null;
    if(receiver){
      stopStream(receiver,false);
      window.clearInterval(receiver.timer);
      for(const ws of [receiver.tts,receiver.main,receiver.sam])ws.close();
      void receiver.audio.close();
    }
    setStatus('已断开');setEnabled(false);
  }
  useEffect(()=>{
    const onHide=()=>close();
    window.addEventListener('pagehide',onHide);
    return()=>{window.removeEventListener('pagehide',onHide);close();};
  },[]);
  function tick(receiver:Receiver){
    if(active.current!==receiver)return;
    const stream=receiver.stream;
    if(!stream||stream.ended)return;
    if(!stream.started&&receiver.audio.currentTime>=stream.startAt){
      stream.started=true;
      send(receiver,stream,'started',0);
      setStatus('浏览器已开始播放（测试报告，非真机回执）');
    }
    if(!stream.started)return;
    const count=played(receiver,stream);
    if(count>stream.played){
      stream.played=count;setCursor(count);
      if(!stream.final||count<stream.received)send(receiver,stream,'progress',count);
    }
    const current=[...stream.mouth.values()].find(item=>item.start<=count&&count<item.end);
    setMouth(current?.viseme??'—');
    if(stream.final&&count>=stream.received&&stream.nodes.size===0){
      stream.ended=true;
      send(receiver,stream,'drained',stream.received);
      setStatus('浏览器队列已播放完（不代表真实机器人）');
    }
  }
  function handleTts(receiver:Receiver,message:unknown){
    if(!message||typeof message!=='object')return;
    const packet=message as {type?:string;data?:Record<string,unknown>};
    if(packet.type!=='tts'||!packet.data||!receiver.ttsSubscribed)return;
    const d=packet.data;
    if(typeof d.conversation_id!=='string'||typeof d.stream_epoch!=='number')return;
    if(d.protocol_version!==1||d.sample_rate!==24000||d.channels!==1)return;
    const streamKey=d.conversation_id+':'+d.stream_epoch;
    let stream=receiver.stream;
    if(d.stop===true){
      receiver.suppressed.add(streamKey);
      if(stream&&stream.cid===d.conversation_id&&stream.epoch===d.stream_epoch){
        stopStream(receiver,true);setStatus('收到 Room stop，浏览器已截断测试音频');
      }
      return;
    }
    if(receiver.suppressed.has(streamKey))return;
    if(stream&&(stream.cid!==d.conversation_id||stream.epoch!==d.stream_epoch)){
      stopStream(receiver,true);stream=null;
    }
    if(!stream){
      stream={cid:d.conversation_id,epoch:d.stream_epoch,
        playbackId:crypto.randomUUID?.()??('browser-'+Date.now()+'-'+Math.random().toString(36).slice(2)),seq:0,
        received:0,plannedAt:0,startAt:0,started:false,ended:false,final:false,played:0,
        nodes:new Set(),mouth:new Map()};
      receiver.stream=stream;setCursor(0);setMouth('—');setReport('unknown');
    }
    if(stream.ended)return;
    if(d.stream_final===true){stream.final=true;return;}
    if(typeof d.audio!=='string'||!d.audio)return;
    let encoded:Uint8Array;
    try{encoded=Uint8Array.from(atob(d.audio),c=>c.charCodeAt(0));}catch{return;}
    if(encoded.length%2!==0||d.start_sample!==stream.received||d.end_sample!==stream.received+encoded.length/2){
      setStatus('PCM 采样连续性失败；已停止');stopStream(receiver,true);return;
    }
    if(stream.received-stream.played>24000*3){
      setStatus('浏览器 PCM 积压超过 3s；停止而非无限缓冲');
      stopStream(receiver,true);return;
    }
    const samples=encoded.length/2;
    const buffer=receiver.audio.createBuffer(1,samples,24000);
    const pcm=new DataView(encoded.buffer,encoded.byteOffset,encoded.byteLength);
    const mono=buffer.getChannelData(0);
    for(let i=0;i<samples;i++)mono[i]=pcm.getInt16(i*2,true)/32768;
    const node=receiver.audio.createBufferSource();node.buffer=buffer;
    node.connect(receiver.audio.destination);
    const at=Math.max(receiver.audio.currentTime+0.08,stream.plannedAt);
    if(stream.received===0)stream.startAt=at;
    stream.plannedAt=at+samples/24000;
    stream.received+=samples;
    stream.nodes.add(node);
    node.onended=()=>{stream.nodes.delete(node);node.disconnect();tick(receiver);};
    node.start(at);
    setStatus('reb_tts PCM 缓冲播放中 / 浏览器测试');
  }
  async function start(){
    if(active.current||!route)return;
    let tts:WebSocket|undefined,main:WebSocket|undefined,sam:WebSocket|undefined,audio:AudioContext|undefined;
    try{
      // User activation is required by browsers before an AudioContext can play.
      audio=new AudioContext({sampleRate:24000});
      await audio.resume();
      tts=await connectRoom(route.urls.tts,route.room_id,'reb_tts');
      main=await connectRoom(route.urls.main,route.room_id,'reb_main');
      sam=await connectRoom(route.urls.sam,route.room_id,'reb_sam');
      const receiver:Receiver={tts,main,sam,audio,stream:null,timer:0,
        suppressed:new Set(),ttsSubscribed:false,samSubscribed:false};
      active.current=receiver;
      tts.onmessage=event=>{
        if(typeof event.data!=='string')return;
        try{
          const packet=JSON.parse(event.data) as {type?:string;data?:{what?:string;subscriptions?:string[]}};
          if(packet.type==='ACK'){
            receiver.ttsSubscribed=packet.data?.what==='subscribed'&&!!packet.data.subscriptions?.includes('*');
            if(!receiver.ttsSubscribed)throw new Error('reb_tts SUBSCRIBE 未确认');
            setStatus('reb_tts 已订阅，等待音频');return;
          }
          handleTts(receiver,packet);
        }catch(error){setStatus(String(error));}
      };
      sam.onmessage=event=>{
        if(typeof event.data!=='string')return;
        try{
          const message=JSON.parse(event.data) as {type?:string;conversation_id?:string;
            seq?:number;start_sample?:number;end_sample?:number;viseme?:string;timing_source?:string};
          if(message.type==='ACK'){
            receiver.samSubscribed=true;return;
          }
          const stream=receiver.stream;
          if(message.type!=='mouth_script'||!receiver.samSubscribed||!stream||stream.ended||
            message.conversation_id!==stream.cid||
            !message.timing_source?.startsWith('qwen3_forced_aligner'))return;
          if(!Number.isSafeInteger(message.start_sample)||!Number.isSafeInteger(message.end_sample)||
            message.end_sample!<=message.start_sample!||message.end_sample!>stream.received)return;
          stream.mouth.set(String(message.seq),{
            start:message.start_sample!,end:message.end_sample!,viseme:message.viseme??'—',
          });
        }catch{/* discard malformed/unrelated mouth packet */}
      };
      for(const ws of [tts,sam])ws.send(JSON.stringify({type:'SUBSCRIBE',data:{types:['*']}}));
      receiver.timer=window.setInterval(()=>tick(receiver),50);
      for(const ws of [tts,main,sam])ws.onclose=()=>{if(active.current===receiver){
        setStatus('Room WebSocket 已断开；不推断设备已停止');close();
      }};
      setEnabled(true);setStatus('WebSocket 已连接，等待 reb_tts/reb_sam 订阅确认');
    }catch(error){
      tts?.close();main?.close();sam?.close();if(audio)void audio.close();
      setStatus(String(error));
    }
  }
  return <div className="mic">
    <p>本页用同一份 reb_tts PCM 和 reb_sam 声学嘴型播放；通过 reb_main 汇报 browser_test 游标。仅供浏览器测试，不能冒充硬件确认。</p>
    <label><input type="checkbox" checked={confirmed} onChange={e=>setConfirmed(e.target.checked)}/>
      我已确认本 Room 没有同时运行的真实扬声器/其他网页播放器，测试时避免麦克风回授</label>
    <div className="actions">
      <button disabled={enabled||!route||!confirmed} onClick={()=>void start()}>开启网页测试扬声器</button>
      <button disabled={!enabled} onClick={()=>{const receiver=active.current;if(receiver){
        stopStream(receiver,true);setStatus('浏览器本地已停止；未取消机器人 Realtime');
      }}}>本地停止测试播放</button>
      <button disabled={!route} onClick={onCancel}>取消机器人回复（发 Room stop）</button>
      <button disabled={!enabled} onClick={close}>断开网页播放器</button>
    </div>
    <p role="status">{status}</p>
    <p>已播采样：{cursor} / 嘴型：{mouth} / 回执：{report}</p>
  </div>;
}
