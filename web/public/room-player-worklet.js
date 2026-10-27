// Room's 24kHz S16LE is decoded before it reaches this audio-thread player.
// The queue is sample-continuous; neither a delayed packet nor a speculative
// hold is permission to discard earlier or subsequently received audio.
class RoomPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue=[];this.offset=0;this.queued=0;
    this.stream='';this.finished=false;this.held=false;this.state='idle';
    this.holdAfter=0;this.holdCommandSeq=null;
    this.played=0;this.underruns=0;this.report=0;
    this.sourceCount=0;this.nextPosition=0;this.last=0;this.hasLast=false;
    this.prebuffer=Math.round(sampleRate*.18);
    this.rebuffer=Math.round(sampleRate*.10);
    this.maximum=Math.round(sampleRate*30); // failure guard, not a latency target
    this.port.onmessage=event=>this.receive(event.data||{});
  }

  receive(message) {
    if(message.type==='generation'){
      this.queue=[];this.offset=0;this.queued=0;
      this.stream=message.stream;this.finished=false;this.held=false;
      this.holdAfter=0;this.holdCommandSeq=null;
      this.state='prebuffering';this.played=0;this.underruns=0;
      this.sourceCount=0;this.nextPosition=0;this.last=0;this.hasLast=false;
    }else if(message.type==='push'&&message.stream===this.stream){
      const input=new Float32Array(message.buffer);
      const base=this.sourceCount-(this.hasLast?1:0);
      const end=this.sourceCount+input.length-1;
      const step=24000/sampleRate;
      const output=[];
      while(this.nextPosition<=end+1e-9){
        const index=Math.floor(this.nextPosition+1e-9),a=index-base;
        const b=Math.min(index+1,end)-base;
        const first=a===0&&this.hasLast?this.last:input[a-(this.hasLast?1:0)];
        const second=b===0&&this.hasLast?this.last:input[b-(this.hasLast?1:0)];
        const frac=Math.max(0,Math.min(1,this.nextPosition-index));
        output.push(first+(second-first)*frac);
        this.nextPosition+=step;
      }
      this.sourceCount+=input.length;
      if(input.length){this.last=input[input.length-1];this.hasLast=true;}
      if(this.queued+output.length>this.maximum){
        this.port.postMessage({type:'overflow',stream:this.stream,
          excessMs:(this.queued+output.length-this.maximum)/sampleRate*1000});
        return;
      }
      if(output.length){this.queue.push(Float32Array.from(output));this.queued+=output.length;}
      this.maybeStart();
    }else if(message.type==='end'&&message.stream===this.stream){
      this.finished=true;this.maybeStart();
    }else if(message.type==='hold'&&message.stream===this.stream){
      this.holdCommandSeq=message.command_seq??null;
      this.holdAfter=this.state==='playing'?
        Math.max(1,Math.round(sampleRate*(message.fade_ms??40)/1000)):0;
      if(!this.holdAfter){
        this.held=true;
        this.port.postMessage({type:'held',stream:this.stream,
          command_seq:this.holdCommandSeq,playedMs:this.played/sampleRate*1000});
      }
    }else if(message.type==='resume'&&message.stream===this.stream){
      this.holdAfter=0;this.held=false;this.maybeStart();
      this.port.postMessage({type:'resumed',stream:this.stream,
        command_seq:message.command_seq??null,playedMs:this.played/sampleRate*1000});
    }else if(message.type==='clear'&&message.stream===this.stream){
      this.queue=[];this.offset=0;this.queued=0;this.held=false;this.holdAfter=0;
      this.finished=true;this.state='ended';
      this.port.postMessage({type:'cleared',stream:this.stream,playedMs:this.played/sampleRate*1000});
    }
  }

  maybeStart(){
    if(this.held)return;
    const required=this.state==='prebuffering'?this.prebuffer:
      this.state==='rebuffering'?this.rebuffer:null;
    if(required===null)return;
    if(this.queued>=required||(this.finished&&this.queued>0)){
      this.state='playing';
      this.port.postMessage({type:'buffer_ready',stream:this.stream,bufferMs:this.queued/sampleRate*1000});
    }
  }

  process(_inputs,outputs){
    const out=outputs[0][0];out.fill(0);
    this.maybeStart();
    if(!this.held&&this.state==='playing'){
      for(let i=0;i<out.length;i++){
        if(!this.queue.length){
          if(this.finished){this.state='ended';this.port.postMessage({type:'ended',stream:this.stream,playedMs:this.played/sampleRate*1000,playedSamples:this.sourceCount});}
          else {this.state='rebuffering';this.underruns++;
            this.port.postMessage({type:'underflow',stream:this.stream,count:this.underruns});}
          break;
        }
        if(this.played===0)this.port.postMessage({type:'started',stream:this.stream,playedMs:0});
        const item=this.queue[0];out[i]=item[this.offset++];
        this.queued--;this.played++;
        if(this.offset===item.length){this.queue.shift();this.offset=0;}
        if(this.holdAfter&&--this.holdAfter===0){
          this.held=true;
          this.port.postMessage({type:'held',stream:this.stream,
            command_seq:this.holdCommandSeq,playedMs:this.played/sampleRate*1000});
          break;
        }
      }
    }
    if(this.finished&&!this.held&&this.queued===0&&this.state!=='ended'){
      this.state='ended';this.port.postMessage({type:'ended',stream:this.stream,playedMs:this.played/sampleRate*1000,playedSamples:this.sourceCount});
    }
    if(++this.report%20===0)this.port.postMessage({type:'buffer',stream:this.stream,
      bufferMs:this.queued/sampleRate*1000,playedMs:this.played/sampleRate*1000,
      underruns:this.underruns,held:this.held,state:this.state});
    return true;
  }
}
registerProcessor('room-player',RoomPlayerProcessor);
