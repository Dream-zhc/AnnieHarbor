import test from 'node:test';
import assert from 'node:assert/strict';

test('Demo-style mic and TTS share one context without duplicate worklet registrations',async()=>{
  const contexts=[];
  class FakeContext{
    constructor(options){
      this.options=options;this.state='running';this.modules=[];
      this.audioWorklet={addModule:async url=>{
        if(this.modules.includes(url))throw Error('duplicate registerProcessor');
        this.modules.push(url);
      }};
      contexts.push(this);
    }
    async close(){this.state='closed';}
  }
  const before=globalThis.AudioContext;
  globalThis.AudioContext=FakeContext;
  try{
    const {acquireAudioContext,releaseAudioContext,loadAudioWorklet}=
      await import('../src/shared-audio-context.ts');
    const microphone=acquireAudioContext(),speaker=acquireAudioContext();
    assert.equal(microphone,speaker);
    assert.equal(microphone.options.sampleRate,48000);
    await Promise.all([
      loadAudioWorklet(microphone,'/room-capture-worklet.js'),
      loadAudioWorklet(microphone,'/room-capture-worklet.js'),
      loadAudioWorklet(speaker,'/room-player-worklet.js'),
    ]);
    assert.deepEqual(microphone.modules,
      ['/room-capture-worklet.js','/room-player-worklet.js']);
    releaseAudioContext(microphone);
    assert.equal(speaker.state,'running');
    releaseAudioContext(speaker);
    assert.equal(speaker.state,'closed');
    assert.notEqual(acquireAudioContext(),speaker);
  }finally{globalThis.AudioContext=before;}
});
