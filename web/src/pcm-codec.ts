/** reb_tts: unframed little-endian signed 16-bit mono, 24 kHz, Base64 in JSON. */
export type TtsPcmPacket={
  protocol_version:number;format:string;sample_rate:number;channels:number;
  conversation_id:string;stream_epoch:number;audio?:string;stop?:boolean;
  stream_final?:boolean;start_sample:number;end_sample:number;
};
export function decodeRoomPcm(data:TtsPcmPacket,expected:number):Uint8Array<ArrayBuffer>{
  if(data.protocol_version!==1||data.format!=='pcm_s16le'||
     data.sample_rate!==24000||data.channels!==1||
     !Number.isSafeInteger(data.stream_epoch)||data.stream_epoch<1||
     typeof data.conversation_id!=='string'||!data.conversation_id||
     data.start_sample!==expected||typeof data.audio!=='string'){
    throw new Error('reb_tts PCM 格式、stream 或采样游标不匹配');
  }
  let decoded:string;
  try{decoded=atob(data.audio);}catch{throw new Error('reb_tts Base64 损坏');}
  const bytes=Uint8Array.from(decoded,char=>char.charCodeAt(0));
  if(bytes.byteLength===0||bytes.byteLength>960*50||bytes.byteLength%2||
     data.end_sample!==expected+bytes.byteLength/2){
    throw new Error('reb_tts PCM 长度或连续性错误');
  }
  return bytes;
}
