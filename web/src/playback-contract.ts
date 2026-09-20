/** The same Room reb_main receipt contract is used by browser tests and speakers. */
export type PlaybackSource='browser_test'|'speaker';
export type PlaybackKind='started'|'progress'|'stopped'|'drained';
export function playbackReceipt(kind:PlaybackKind, data:{
  conversation_id:string;stream_epoch:number;played_sample_count:number;
  seq:number;playback_id:string;source:PlaybackSource;
}){
  if(!data.conversation_id||!data.playback_id||!Number.isSafeInteger(data.played_sample_count)||
    data.played_sample_count<0||!Number.isSafeInteger(data.stream_epoch)||data.stream_epoch<1||
    !Number.isSafeInteger(data.seq)||data.seq<1)throw new Error('invalid playback receipt');
  return {type:'tts.playback.'+kind,data:{protocol_version:1,sample_rate:24000,...data}};
}
