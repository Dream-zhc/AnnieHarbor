import {pcmFrame} from './pcm-format.ts';

/** One continuous 16 kHz mono S16LE stream, emitted as 20 ms MIC_AUDIO frames. */
export function createMicPcmPump(){
  let remainder:number[]=[];
  let sourceCount=0;
  let nextPosition=0;
  let lastSample=0;
  let hasLast=false;
  let sourceRate=0;
  let frames=0;
  return (input:Float32Array,rate:number,send:(frame:Uint8Array<ArrayBuffer>)=>void)=>{
    const ratio=rate/16000;
    if(!Number.isFinite(ratio)||ratio<1)throw new Error('麦克风采样率低于 16kHz');
    if(sourceRate&&sourceRate!==rate)throw new Error('麦克风采样率在录音中变化，请重新开启');
    sourceRate=rate;
    if(!input.length)return frames;
    // Absolute source coordinates and the preceding sample keep interpolation
    // identical whether the browser gives 128, 1024 or larger input blocks.
    const base=sourceCount-(hasLast?1:0);
    const end=sourceCount+input.length-1;
    while(nextPosition<=end+1e-9){
      const index=Math.floor(nextPosition+1e-9);
      const a=index-base;
      const first=a<0?lastSample:a===0&&hasLast?lastSample:input[a-(hasLast?1:0)];
      const nextIndex=Math.min(index+1,end);
      const b=nextIndex-base;
      const second=b===0&&hasLast?lastSample:input[b-(hasLast?1:0)];
      const fraction=Math.max(0,Math.min(1,nextPosition-index));
      remainder.push(first+(second-first)*fraction);
      nextPosition+=ratio;
    }
    sourceCount+=input.length;
    lastSample=input[input.length-1];hasLast=true;
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
