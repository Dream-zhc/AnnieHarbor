import type {Robot,SpeechStatus} from './api';

type Realtime=NonNullable<Robot['realtime']>;

export const duration=(a:number|null|undefined,b:number|null|undefined)=>
  a!=null&&b!=null&&b>=a?b-a:null;

/** A missing or unrelated event must not turn into an apparent fast response. */
export function latencyMetrics(realtime?:Realtime,speech?:SpeechStatus){
  const timeline=realtime?.last_turn_timeline;
  const times=timeline?.times;
  const speakerStart=times?.speaker_started_ms;
  return {
    speakerStart:speakerStart??null,
    endToEnd:duration(times?.speech_stopped_ms,speakerStart),
    firstAudio:speech?.turn_id!=null&&speech.turn_id===timeline?.turn_id
      ?speech.first_audio_ms??null:null,
    firstRoom:duration(times?.tts_first_pcm_ms,times?.room_first_publish_ms),
  };
}
