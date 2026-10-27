/** The existing reb_tts JSON contract, never a second synthesis path. */
export type RoomTtsPacket={
  kind:'audio'|'stop'|'end';conversationId:string;epoch:number;seq:number;
  startSample:number;endSample:number;samples:Float32Array;
};

const SAMPLE_RATE=24000;
const MAX_TTS_PCM_BYTES=SAMPLE_RATE*2; // 1s (48000B), matching room-speaker; Brain coalesces up to 800ms
const MAX_TTS_AUDIO_BASE64=65536;

export function decodeRoomTts(message:unknown):RoomTtsPacket|null{
  if(!message||typeof message!=='object'||(message as {type?:unknown}).type!=='tts')return null;
  const data=(message as {data?:unknown}).data;
  if(!data||typeof data!=='object')throw Error('TTS 缺少 data');
  const d=data as Record<string,unknown>;
  if(typeof d.conversation_id!=='string'||!d.conversation_id||
    !Number.isSafeInteger(d.stream_epoch)||Number(d.stream_epoch)<1||
    !Number.isSafeInteger(d.seq)||Number(d.seq)<1||
    !Number.isSafeInteger(d.start_sample)||Number(d.start_sample)<0||
    !Number.isSafeInteger(d.end_sample)||Number(d.end_sample)<Number(d.start_sample)){
    throw Error('TTS 游标或会话身份无效');
  }
  const base={conversationId:d.conversation_id,epoch:Number(d.stream_epoch),
    seq:Number(d.seq),startSample:Number(d.start_sample),endSample:Number(d.end_sample)};
  const empty=new Float32Array(0);
  if(d.stop===true)return {...base,kind:'stop',samples:empty};
  if(d.stream_final===true)return {...base,kind:'end',samples:empty};
  if(typeof d.audio!=='string'||!d.audio)return null; // segment-final has no PCM
  if(d.format!=='pcm_s16le'||d.sample_rate!==SAMPLE_RATE||d.channels!==1){
    throw Error('TTS PCM 格式不是 24kHz/mono/S16LE');
  }
  if(d.audio.length>MAX_TTS_AUDIO_BASE64)throw Error('TTS 包超出 1s 上限');
  let bytes:string;
  try{bytes=atob(d.audio);}catch{throw Error('TTS PCM Base64 无效');}
  if(!bytes.length||bytes.length%2||bytes.length>MAX_TTS_PCM_BYTES||
    bytes.length/2!==base.endSample-base.startSample){
    throw Error('TTS PCM 长度与 sample cursor 不符');
  }
  const samples=new Float32Array(bytes.length/2);
  for(let i=0;i<samples.length;i++){
    const unsigned=bytes.charCodeAt(i*2)|(bytes.charCodeAt(i*2+1)<<8);
    samples[i]=(unsigned>=32768?unsigned-65536:unsigned)/32768;
  }
  return {...base,kind:'audio',samples};
}
