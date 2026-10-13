// Audio-thread capture. The Room wire header is applied on the main thread;
// this worklet posts only transferable 16kHz mono S16LE 20ms PCM frames.
class RoomCaptureProcessor extends AudioWorkletProcessor {
  constructor(options){
    super();this.resampler=null;this.stopped=false;
    globalThis.LibSampleRate.create(1,sampleRate,16000,{converterType:0}).then(src=>{
      if(this.stopped){src.destroy();return;}
      this.resampler=src;this.port.postMessage({type:'ready',resampler:'libsamplerate-best-2.1.2'});
    }).catch(error=>{this.stopped=true;this.port.postMessage({type:'error',message:String(error)});});
    this.pending=[];this.levelSum=0;this.levelCount=0;this.levelBlocks=0;
    this.noiseFloor=null;
    this.gain=this.targetGain=Math.max(1,Math.min(4,options?.processorOptions?.gain??1));
    this.clipped=0;
    this.port.onmessage=event=>{
      if(event.data?.type==='stop'){this.stopped=true;this.resampler?.destroy();this.resampler=null;return;}
      if(event.data?.type==='gain'&&Number.isFinite(event.data.value)){
        this.targetGain=Math.max(1,Math.min(4,event.data.value));
        this.noiseFloor=null; // old floor is not comparable after a gain change
      }
    };
  }

  process(inputs,outputs){
    const input=inputs[0]?.[0];
    if(outputs[0]?.[0])outputs[0][0].fill(0);
    if(this.stopped)return false;
    if(!input?.length||!this.resampler)return true;
    const step=(this.targetGain-this.gain)/input.length;
    // Amplify the same PCM sent to Room; the meter must not show a different signal.
    const amplified=new Float32Array(input.length);
    for(let i=0;i<input.length;i++){
      this.gain+=step;
      const value=input[i]*this.gain;
      if(Math.abs(value)>1)this.clipped++;
      amplified[i]=Math.max(-1,Math.min(1,value));
      if(i%16===0){this.levelSum+=amplified[i]*amplified[i];this.levelCount++;}
    }
    if(++this.levelBlocks>=32){
      const rms=Math.sqrt(this.levelSum/Math.max(1,this.levelCount));
      const dbfs=20*Math.log10(Math.max(rms,1e-7));
      // Demo v1.7: estimate the acoustic floor only from quiet frames;
      // local dBFS/SNR are observational, never used to decide a turn.
      if(this.noiseFloor===null)this.noiseFloor=dbfs;
      else if(dbfs<this.noiseFloor+8){
        this.noiseFloor=this.noiseFloor*.985+dbfs*.015;
      }
      this.port.postMessage({type:'level',value:Math.round(rms*1000)/10,
        dbfs,noiseFloor:this.noiseFloor,snr:dbfs-this.noiseFloor,
        clipped:this.clipped});
      this.levelBlocks=0;this.levelSum=0;this.levelCount=0;this.clipped=0;
    }
    // Continuous full() state; never use simple() per audio block.
    let converted;
    try{converted=this.resampler.full(amplified);}
    catch(error){
      this.resampler.destroy();this.resampler=null;this.stopped=true;
      this.port.postMessage({type:'error',message:String(error)});return false;
    }
    for(let i=0;i<converted.length;i++)this.pending.push(converted[i]);
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
