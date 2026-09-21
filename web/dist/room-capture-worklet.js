// Audio-thread capture. The Room wire header is applied on the main thread;
// this worklet posts only transferable 16kHz mono S16LE 20ms PCM frames.
class RoomCaptureProcessor extends AudioWorkletProcessor {
  constructor(){
    super();this.sourceCount=0;this.nextPosition=0;this.last=0;this.hasLast=false;
    this.pending=[];this.levelSum=0;this.levelCount=0;this.levelBlocks=0;
  }

  process(inputs,outputs){
    const input=inputs[0]?.[0];
    if(outputs[0]?.[0])outputs[0][0].fill(0);
    if(!input?.length)return true;
    for(let i=0;i<input.length;i+=16){this.levelSum+=input[i]*input[i];this.levelCount++;}
    if(++this.levelBlocks>=32){
      this.port.postMessage({type:'level',value:Math.round(
        Math.sqrt(this.levelSum/Math.max(1,this.levelCount))*1000)/10});
      this.levelBlocks=0;this.levelSum=0;this.levelCount=0;
    }
    const ratio=sampleRate/16000;
    const base=this.sourceCount-(this.hasLast?1:0);
    const end=this.sourceCount+input.length-1;
    while(this.nextPosition<=end+1e-9){
      const index=Math.floor(this.nextPosition+1e-9);
      const a=index-base,b=Math.min(index+1,end)-base;
      const first=a===0&&this.hasLast?this.last:input[a-(this.hasLast?1:0)];
      const second=b===0&&this.hasLast?this.last:input[b-(this.hasLast?1:0)];
      const fraction=Math.max(0,Math.min(1,this.nextPosition-index));
      this.pending.push(first+(second-first)*fraction);
      this.nextPosition+=ratio;
    }
    this.sourceCount+=input.length;this.last=input[input.length-1];this.hasLast=true;
    while(this.pending.length>=320){
      const pcm=new Int16Array(320);
      for(let i=0;i<320;i++){
        const value=Math.max(-1,Math.min(1,this.pending[i]));
        pcm[i]=value<0?value*32768:value*32767;
      }
      this.pending.splice(0,320);
      this.port.postMessage({type:'audio',buffer:pcm.buffer},[pcm.buffer]);
    }
    return true;
  }
}
registerProcessor('room-capture',RoomCaptureProcessor);
