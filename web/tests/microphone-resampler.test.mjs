import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,existsSync} from 'node:fs';
import {createRequire} from 'node:module';
import {runInNewContext} from 'node:vm';
const require=createRequire(import.meta.url);
const library=require('@alexanderolsen/libsamplerate-js');

async function helper(){
  const path=new URL('../src/capture-ready.ts',import.meta.url);
  assert.ok(existsSync(path),'capture readiness helper exists');
  return (await import(path)).waitForCaptureReady;
}
function fake(){return {port:{onmessage:null},onprocessorerror:null};}
for(const scenario of ['ready','error','processorerror','abort','timeout','already-aborted']){
  test(`capture initialization settles and removes listeners: ${scenario}`,async()=>{
    const wait=await helper(),p=fake(),abort=new AbortController();
    if(scenario==='already-aborted')abort.abort();
    const result=wait(p,abort.signal,10);
    if(scenario==='ready')p.port.onmessage({data:{type:'ready'}});
    if(scenario==='error')p.port.onmessage({data:{type:'error',message:'wasm failed'}});
    if(scenario==='processorerror')p.onprocessorerror();
    if(scenario==='abort')abort.abort();
    if(scenario==='ready')await result;
    else await assert.rejects(result);
    assert.equal(p.port.onmessage,null);
    assert.equal(p.onprocessorerror,null);
  });
}
async function worklet(rate,lib=library){
  let Processor;const messages=[];
  runInNewContext(readFileSync(new URL('../public/room-capture-worklet.js',import.meta.url),'utf8'),{
    AudioWorkletProcessor:class {constructor(){this.port={postMessage:m=>messages.push(m)};}},
    sampleRate:rate,Float32Array,Int16Array,Math,LibSampleRate:lib,
    registerProcessor:(_,ctor)=>{Processor=ctor;},
  });
  const p=new Processor({processorOptions:{gain:1}});
  for(let n=0;n<100&&!messages.length;n++)await new Promise(r=>setTimeout(r,2));
  assert.ok(messages.length,'initialization reports ready/error');
  return {p,messages};
}
for(const rate of [44100,48000]){
  test(`BEST capture suppresses aliasing and bounds first PCM: ${rate}`,async()=>{
    async function tone(hz){
      const {p,messages}=await worklet(rate);
      assert.equal(messages[0].type,'ready');let first=null;
      for(let offset=0;offset<rate;offset+=128){
        const input=Float32Array.from({length:Math.min(128,rate-offset)},(_,i)=>
          .2*Math.sin((offset+i)*2*Math.PI*hz/rate));
        const output=new Float32Array(128).fill(1);
        p.process([[input]],[[output]]);
        assert.ok(output.every(x=>x===0));
        if(first===null&&messages.some(m=>m.type==='audio'))first=(offset+input.length)/rate*1000;
      }
      const frames=messages.filter(m=>m.type==='audio');
      assert.ok(first<=35&&first>=20,`first PCM ${first}ms`);
      assert.ok(frames.every(m=>m.buffer.byteLength===640));
      const samples=frames.slice(2).flatMap(m=>Array.from(new Int16Array(m.buffer)));
      p.port.onmessage({data:{type:'stop'}});
      assert.equal(p.process([],[[new Float32Array(128)]]),false);
      return Math.sqrt(samples.reduce((sum,x)=>sum+x*x,0)/samples.length);
    }
    assert.ok(await tone(10000)<await tone(1000)*.01,'out-of-band audio must not alias into speech');
  });
}
test('capture selects BEST once, destroys on stop, and reports decoder errors',async()=>{
  let created=0,destroyed=0;
  const {p,messages}=await worklet(48000,{create:async(ch,from,to,options)=>{
    created++;assert.deepEqual([ch,from,to,options.converterType],[1,48000,16000,0]);
    return {full:()=>{throw new Error('decode fixture');},destroy:()=>destroyed++};
  }});
  assert.equal(p.process([[new Float32Array(128)]],[[new Float32Array(128)]]),false);
  assert.ok(messages.some(m=>m.type==='error'));
  p.port.onmessage({data:{type:'stop'}});
  assert.equal(created,1);assert.equal(destroyed,1);
});
test('vendored worklet matches pinned package and startup gates microphone connection',()=>{
  const vendor=new URL('../public/vendor/libsamplerate-2.1.2.worklet.js',import.meta.url);
  assert.ok(existsSync(vendor));
  assert.deepEqual(readFileSync(vendor),readFileSync(require.resolve('@alexanderolsen/libsamplerate-js/dist/libsamplerate.worklet.js')));
  const mic=readFileSync(new URL('../src/microphone.tsx',import.meta.url),'utf8');
  assert.ok(mic.indexOf('await waitForCaptureReady(')<mic.indexOf('source.connect(processor)'));
  assert.match(mic,/startupAbort\.current\?\.abort\(\)/);
});
test('Room closed during capture initialization never activates microphone',()=>{
  const mic=readFileSync(new URL('../src/microphone.tsx',import.meta.url),'utf8');
  const start=mic.indexOf('await waitForCaptureReady(');
  const end=mic.indexOf('// Keep immutable handles:',start);
  const afterReady='(()=>{'+mic.slice(mic.indexOf(';',start)+1,end)+'})()';
  const state={current:1,token:{current:1},WebSocket:{OPEN:1}};
  assert.throws(()=>runInNewContext(afterReady,{...state,ws:{readyState:3}}),
    /Room.*断开/);
  assert.doesNotThrow(()=>runInNewContext(afterReady,{...state,ws:{readyState:1}}));
  assert.ok(end<mic.indexOf('active.current={ws,stream'));
  assert.ok(end<mic.indexOf('source.connect(processor)'));
});
