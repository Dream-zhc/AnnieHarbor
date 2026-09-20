import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import vm from 'node:vm';
import {decodeRoomPcm} from '../src/pcm-codec.ts';

const source=await readFile(path.resolve(path.dirname(fileURLToPath(import.meta.url)),
  '../public/pcm-playback-processor.js'),'utf8');
function worklet(rate){
  let Constructor;
  class Processor{
    constructor(){this.messages=[];this.port={
      onmessage:null,postMessage:message=>this.messages.push(message),
    };}
  }
  vm.runInNewContext(source,{
    AudioWorkletProcessor:Processor,
    registerProcessor:(name,cls)=>{
      assert.equal(name,'room-pcm-playback');Constructor=cls;
    },
    sampleRate:rate,Float32Array,DataView,Math,
  });
  return new Constructor();
}
function signal(sampleCount){
  const bytes=new Uint8Array(sampleCount*2),view=new DataView(bytes.buffer);
  for(let i=0;i<sampleCount;i++){
    view.setInt16(i*2,Math.round(20000*Math.sin(i*2*Math.PI*440/24000)),true);
  }
  return bytes;
}
function packet(bytes,start=0){
  return {
    protocol_version:1,format:'pcm_s16le',sample_rate:24000,channels:1,
    conversation_id:'t1',stream_epoch:1,start_sample:start,
    end_sample:start+bytes.byteLength/2,
    audio:Buffer.from(bytes).toString('base64'),
  };
}

test('PCM decoder verifies exact 24 kHz S16LE bytes and contiguous sample cursor',()=>{
  const bytes=signal(480),p=packet(bytes);
  assert.deepEqual(decodeRoomPcm(p,0),bytes);
  assert.throws(()=>decodeRoomPcm({...p,sample_rate:16000},0),/格式/);
  assert.throws(()=>decodeRoomPcm({...p,format:'wav'},0),/格式/);
  assert.throws(()=>decodeRoomPcm({...p,channels:2},0),/格式/);
  assert.throws(()=>decodeRoomPcm(p,480),/游标/);
  assert.throws(()=>decodeRoomPcm({...p,end_sample:479},0),/长度/);
  assert.throws(()=>decodeRoomPcm({...p,audio:p.audio.slice(1)},0),/Base64|长度/);
  assert.throws(()=>decodeRoomPcm({...p,audio:Buffer.from([0]).toString('base64')},0),/长度/);
});

for(const rate of [24000,44100,48000]){
  test(`AudioWorklet PCM tone retains waveform without per-frame gaps @ ${rate} Hz`,()=>{
    const p=worklet(rate);
    const src=signal(24000);
    // Prebuffer exactly 120 ms before callback, then stream in fragmented
    // 20 ms packets over the remainder of the test.
    let sent=0;
    const push=()=>{
      const bytes=src.slice(sent*2,(sent+480)*2);
      p.port.onmessage({data:{type:'pcm',buffer:bytes.buffer}});
      sent+=480;
    };
    for(let i=0;i<6;i++)push();
    const out=[];
    let lastReport=0;
    const total=Math.ceil(rate*1.4/128);
    for(let i=0;i<total;i++){
      const output=[new Float32Array(128),new Float32Array(128)];
      p.process([],[output]);
      out.push(...output[0]);
      assert.deepEqual(output[0],output[1],'mono must be copied identically to both channels');
      const elapsed=(i+1)*128/rate;
      while(sent<24000 && elapsed>=(sent-6*480)/24000)push();
      if(sent===24000&&lastReport===0){
        p.port.onmessage({data:{type:'final'}});lastReport=1;
      }
    }
    const nonzero=out.findIndex(x=>Math.abs(x)>0.02);
    assert.ok(nonzero>=0,'audio remained silent');
    const events=p.messages.map(item=>item.type);
    assert.equal(events.filter(e=>e==='started').length,1);
    assert.ok(events.includes('drained'),'no completed playback');
    assert.ok(!events.includes('underrun'),'network-paced PCM must not stutter');
    const rendered=out.slice(nonzero,nonzero+rate*.8);
    const rms=Math.sqrt(rendered.reduce((s,x)=>s+x*x,0)/rendered.length);
    assert.ok(rms>.38&&rms<.46,`waveform amplitude incorrect: rms=${rms}`);
    // Verify exact source frequency without claiming real TTS acoustic quality.
    const period=Math.round(rate/440);
    const sampleError=rendered.slice(0,5000).reduce((s,x,i)=>s+Math.abs(x-
      rendered[i+period]),0)/5000;
    assert.ok(sampleError<.04,`PCM sounded discontinuous: period error=${sampleError}`);
    assert.equal(p.played,24000);
  });
}

test('AudioWorklet stop clears queued audio; undersupply is visible, not hidden',()=>{
  const p=worklet(48000);
  p.port.onmessage({data:{type:'pcm',buffer:signal(480*6).buffer}});
  for(let i=0;i<350;i++)p.process([],[new Array(2).fill(0).map(()=>new Float32Array(128))]);
  assert.ok(p.messages.some(m=>m.type==='underrun'));
  p.port.onmessage({data:{type:'stop'}});
  const previous=p.played;
  for(let i=0;i<30;i++)p.process([],[new Array(2).fill(0).map(()=>new Float32Array(128))]);
  assert.equal(p.played,previous);
  assert.equal(p.length,0);
});
