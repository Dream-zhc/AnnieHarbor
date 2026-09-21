import test from 'node:test';
import assert from 'node:assert/strict';
import {createMicPcmPump} from '../src/mic-pump.ts';

function collect(samples,rate,chunks){
  const pump=createMicPcmPump(),output=[];
  let start=0;
  for(const length of chunks){
    if(start>=samples.length)break;
    pump(samples.subarray(start,Math.min(samples.length,start+length)),rate,frame=>output.push(...frame));
    start+=length;
  }
  if(start<samples.length)pump(samples.subarray(start),rate,frame=>output.push(...frame));
  return output;
}

for(const rate of [44100,48000]){
  test(`${rate} Hz PCM is independent of audio callback block size`,()=>{
    const samples=Float32Array.from({length:rate},(_,i)=>
      0.5*Math.sin(i*2*Math.PI*997/rate)+0.13*Math.sin(i*2*Math.PI*301/rate));
    const whole=collect(samples,rate,[rate]);
    for(const size of [1,128,1024,2048,4096]){
      const chunked=collect(samples,rate,Array(Math.ceil(rate/size)).fill(size));
      assert.deepEqual(chunked,whole,`block size ${size} changed the PCM waveform`);
    }
    assert.equal(whole.length,50*651);
  });
}

test('a capture pump rejects mid-stream sample rate changes',()=>{
  const pump=createMicPcmPump();
  pump(new Float32Array(1024),48000,()=>{});
  assert.throws(()=>pump(new Float32Array(1024),44100,()=>{}),/采样率/);
});
