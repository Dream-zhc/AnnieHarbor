import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

function processor(file,rate=48000,options={}){
  const processors=new Map();
  class AudioWorkletProcessor{
    constructor(){this.messages=[];this.port={postMessage:value=>this.messages.push(value)};}
  }
  runInNewContext(readFileSync(new URL(`../public/${file}`,import.meta.url),'utf8'),{
    AudioWorkletProcessor,sampleRate:rate,Float32Array,Int16Array,Math,
    registerProcessor:(name,ctor)=>processors.set(name,ctor),
  });
  return new (processors.values().next().value)({processorOptions:options});
}
const send=(p,message)=>p.port.onmessage({data:message});
function process(p){const output=new Float32Array(128);p.process([],[ [output] ]);return output;}

test('Room player keeps a continuous queue across delayed packets and hold/resume',()=>{
  const p=processor('room-player-worklet.js');
  send(p,{type:'generation',stream:'turn-1'});
  // 240ms input @ 24kHz; >180ms start buffer @ output 48kHz.
  for(let i=0;i<12;i++){
    const values=Float32Array.from({length:480},(_,j)=>
      Math.sin((i*480+j)*2*Math.PI*220/24000)*.5);
    send(p,{type:'push',stream:'turn-1',buffer:values.buffer});
  }
  assert.equal(p.state,'playing');
  const start=process(p);
  assert.ok(start.some(x=>x!==0));
  const beforeHold=p.queued,played=p.played;
  send(p,{type:'hold',stream:'turn-1'});
  for(let i=0;i<20;i++)process(p); // 40ms duck fade still consumes audible output
  assert.equal(p.held,true);
  assert.ok(p.played>played);
  assert.ok(p.queued<beforeHold);
  const frozen=p.played,remaining=p.queued;
  for(let i=0;i<30;i++)assert.ok(process(p).every(x=>x===0));
  assert.equal(p.played,frozen);
  assert.equal(p.queued,remaining);
  send(p,{type:'resume',stream:'turn-1'});
  const after=process(p);
  const expected=Math.sin((frozen)*2*Math.PI*220/48000)*.5;
  assert.ok(Math.abs(after[0]-expected)<.001,'resume skipped the preserved sample cursor');
  assert.equal(p.underruns,0);
  assert.equal(p.messages.filter(item=>item.type==='underflow').length,0);
  send(p,{type:'end',stream:'turn-1'});
  for(let i=0;i<140;i++)process(p);
  assert.equal(p.state,'ended');
  assert.equal(p.queued,0);
  assert.ok(p.messages.some(item=>item.type==='ended'));
  assert.ok(Number.isFinite(p.messages.find(item=>item.type==='ended').playedMs));
});

test('Room player waits for 100ms before recovering from starvation',()=>{
  const p=processor('room-player-worklet.js');
  send(p,{type:'generation',stream:'turn-2'});
  send(p,{type:'push',stream:'turn-2',buffer:new Float32Array(4800).buffer});
  for(let i=0;i<200;i++)process(p);
  assert.equal(p.state,'rebuffering');
  assert.equal(p.underruns,1);
  send(p,{type:'push',stream:'turn-2',buffer:new Float32Array(960).buffer});
  assert.equal(p.state,'rebuffering');
  send(p,{type:'push',stream:'turn-2',buffer:new Float32Array(1920).buffer});
  assert.equal(p.state,'playing');
});

for(const rate of [44100,48000]){
  test(`${rate}Hz capture worklet generates identical PCM for different audio blocks`,()=>{
    const source=Float32Array.from({length:rate},(_,i)=>
      .5*Math.sin(i*2*Math.PI*440/rate));
    function capture(chunk){
      const p=processor('room-capture-worklet.js',rate);
      for(let i=0;i<source.length;i+=chunk){
        p.process([[source.subarray(i,i+chunk)]],[[new Float32Array(128)]]);
      }
      return p.messages.filter(m=>m.type==='audio').flatMap(m=>Array.from(new Int16Array(m.buffer)));
    }
    assert.deepEqual(capture(128),capture(1024));
    assert.equal(capture(128).length,16000);
  });
}

test('mic enhancement changes the actual Room PCM and clamps live gain updates',()=>{
  const wave=Float32Array.from({length:4800},(_,i)=>.08*Math.sin(i*2*Math.PI*220/48000));
  const raw=processor('room-capture-worklet.js',48000,{gain:1});
  const boosted=processor('room-capture-worklet.js',48000,{gain:2});
  for(let i=0;i<wave.length;i+=128){
    raw.process([[wave.subarray(i,i+128)]],[[new Float32Array(128)]]);
    boosted.process([[wave.subarray(i,i+128)]],[[new Float32Array(128)]]);
  }
  const rawAudio=raw.messages.filter(item=>item.type==='audio');
  const boostedAudio=boosted.messages.filter(item=>item.type==='audio');
  assert.equal(rawAudio.length,5);
  assert.equal(boostedAudio.length,5);
  assert.equal(boostedAudio[0].buffer.byteLength,640);
  const peak=items=>Math.max(...items.flatMap(item=>Array.from(
    new Int16Array(item.buffer),sample=>Math.abs(sample))));
  assert.ok(peak(boostedAudio)>peak(rawAudio)*1.95);
  assert.ok(peak(boostedAudio)<peak(rawAudio)*2.05);
  boosted.port.onmessage({data:{type:'gain',value:100}});
  assert.equal(boosted.targetGain,4);
  boosted.port.onmessage({data:{type:'gain',value:0}});
  assert.equal(boosted.targetGain,1);
});

test('Demo-style mic noise/SNR uses measured PCM rather than a fixed fake floor',()=>{
  const p=processor('room-capture-worklet.js',48000,{gain:1});
  function feed(amplitude){
    for(let i=0;i<32;i++){
      p.process([[Float32Array.from({length:128},(_,j)=>
        amplitude*Math.sin((j+i*128)*2*Math.PI*440/48000))]],
      [[new Float32Array(128)]]);
    }
    return p.messages.filter(item=>item.type==='level').at(-1);
  }
  const quiet=feed(.005);
  assert.ok(quiet.noiseFloor<-42&&quiet.noiseFloor>-52);
  const spoken=feed(.2);
  assert.ok(spoken.snr>25,'louder speech must be distinguishable from quiet input');
  assert.ok(Math.abs(spoken.noiseFloor-quiet.noiseFloor)<1,
    'speaking must not teach the floor that the speaker is background noise');
  p.port.onmessage({data:{type:'gain',value:2}});
  assert.equal(p.noiseFloor,null,'gain changes invalidate the old floor');
  const after=feed(.005);
  assert.ok(after.noiseFloor>quiet.noiseFloor,
    'the floor must be recomputed for the actually amplified microphone PCM');
});
