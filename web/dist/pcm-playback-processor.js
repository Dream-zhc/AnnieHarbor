// 24 kHz mono S16LE Room PCM -> browser output device sample rate.
// One persistent audio callback, not 50 independent AudioBufferSource nodes/sec.
class RoomPcmPlayback extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(24000 * 4);
    this.head = 0;
    this.length = 0;
    this.fraction = 0;
    this.played = 0;
    this.running = false;
    this.final = false;
    this.stopped = false;
    this.underflows = 0;
    this.sinceReport = 0;
    this.port.onmessage = ({data}) => {
      if (data.type === 'stop') {
        this.stopped = true;
        this.length = 0;
        this.port.postMessage({type:'stopped',played:this.played});
      } else if (data.type === 'final') {
        this.final = true;
        if(!this.running && this.length===0 && this.played>0){
          this.stopped=true;
          this.port.postMessage({type:'drained',played:this.played});
        }
      } else if (data.type === 'pcm' && !this.stopped) {
        const bytes = new DataView(data.buffer);
        const samples = bytes.byteLength / 2;
        if (bytes.byteLength % 2 || samples > this.buffer.length - this.length) {
          this.port.postMessage({type:'error',reason:'PCM 队列溢出或无效样本'});
          this.stopped = true;
          return;
        }
        for(let i=0;i<samples;i++){
          const tail=(this.head+this.length)%this.buffer.length;
          this.buffer[tail]=bytes.getInt16(i*2,true)/32768;
          this.length++;
        }
      }
    };
  }
  process(_inputs, outputs) {
    const channels=outputs[0];
    if(!channels?.length)return true;
    const mono=channels[0];
    mono.fill(0);
    if(!this.stopped){
      // Wait for 120 ms before first output or after an underrun.
      if(!this.running && (this.length>=2880 || (this.final && this.length>0))){
        this.running=true;
        if(this.played===0)this.port.postMessage({type:'started',played:0});
      }
      const ratio=24000/sampleRate;
      for(let i=0;i<mono.length && this.running;i++){
        if(this.length<2 && !(this.final && this.length===1)){
          if(this.final && this.length===0)break;
          this.running=false;
          this.underflows++;
          this.port.postMessage({type:'underrun',count:this.underflows});
          break;
        }
        const next=this.length>1?(this.head+1)%this.buffer.length:this.head;
        const current=this.buffer[this.head];
        mono[i]=current+(this.buffer[next]-current)*this.fraction;
        this.fraction+=ratio;
        const take=Math.min(Math.floor(this.fraction),this.length);
        this.fraction-=take;
        this.head=(this.head+take)%this.buffer.length;
        this.length-=take;
        this.played+=take;
      }
      if(this.played && (this.sinceReport+=mono.length)>=sampleRate/20){
        this.sinceReport=0;
        this.port.postMessage({type:'progress',played:this.played,underflows:this.underflows});
      }
      if(this.final && this.length===0 && this.running){
        this.stopped=true;
        this.port.postMessage({type:'drained',played:this.played});
      }
    }
    for(let i=1;i<channels.length;i++)channels[i].set(mono);
    return true;
  }
}
registerProcessor('room-pcm-playback',RoomPcmPlayback);
