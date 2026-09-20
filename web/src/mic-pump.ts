import {pcmFrame} from './pcm-format.ts';

/** One continuous 16 kHz mono S16LE stream, emitted as 20 ms MIC_AUDIO frames. */
export function createMicPcmPump(){
  let remainder:number[]=[];
  let offset=0;
  let frames=0;
  return (input:Float32Array,rate:number,send:(frame:Uint8Array<ArrayBuffer>)=>void)=>{
    const ratio=rate/16000;
    if(!Number.isFinite(ratio)||ratio<1)throw new Error('麦克风采样率低于 16kHz');
    for(let pos=offset;pos<input.length;pos+=ratio){
      const a=Math.floor(pos),b=Math.min(input.length,Math.max(a+1,Math.floor(pos+ratio)));
      let total=0;for(let i=a;i<b;i++)total+=input[i];
      remainder.push(total/(b-a));
    }
    offset=offset+Math.ceil((input.length-offset)/ratio)*ratio-input.length;
    while(remainder.length>=320){
      const bytes=new Uint8Array(640),view=new DataView(bytes.buffer);
      for(let i=0;i<320;i++){
        const value=Math.max(-1,Math.min(1,remainder[i]));
        view.setInt16(i*2,value<0?value*32768:value*32767,true);
      }
      send(pcmFrame(bytes));
      remainder=remainder.slice(320);
      frames++;
    }
    return frames;
  };
}
