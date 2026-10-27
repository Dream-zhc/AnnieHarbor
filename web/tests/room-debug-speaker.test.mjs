import test from 'node:test';
import assert from 'node:assert/strict';
import {decodeRoomTts} from '../src/room-tts-pcm.ts';

const base={type:'tts',data:{
  conversation_id:'turn-a',stream_epoch:7,seq:1,start_sample:0,end_sample:4,
  format:'pcm_s16le',sample_rate:24000,channels:1,source:'robot_main_brain',
  audio:Buffer.from([0,128,0,0,255,127,0,64]).toString('base64'),
}};

test('Room PCM decoder respects published 24k mono S16LE and sample cursors',()=>{
  const packet=decodeRoomTts(base);
  assert.equal(packet.kind,'audio');
  assert.equal(packet.conversationId,'turn-a');
  assert.equal(packet.epoch,7);
  assert.deepEqual(Array.from(packet.samples),[-1,0,32767/32768,0.5]);
  assert.equal(decodeRoomTts({type:'WELCOME'}),null);
  assert.equal(decodeRoomTts({...base,data:{...base.data,audio:'',final:true}}),null);
});

test('Room stop and stream-final are distinct and never synthesize silence',()=>{
  const stop=decodeRoomTts({...base,data:{...base.data,audio:'',stop:true,final:true}});
  const end=decodeRoomTts({...base,data:{...base.data,audio:'',stream_final:true}});
  assert.equal(stop.kind,'stop');assert.equal(end.kind,'end');
  assert.equal(stop.samples.length,0);assert.equal(end.samples.length,0);
});

test('Room PCM decoder accepts coalesced 800ms and 1s packets matching Brain and speaker',()=>{
  const pcm800=Buffer.alloc(38400);
  pcm800.writeInt16LE(-16384,0);
  pcm800.writeInt16LE(16384,38398);
  const coalesced=decodeRoomTts({...base,data:{...base.data,
    start_sample:480,end_sample:480+19200,audio:pcm800.toString('base64')}});
  assert.equal(coalesced.kind,'audio');
  assert.equal(coalesced.samples.length,19200);
  assert.equal(coalesced.samples[0],-0.5);
  assert.equal(coalesced.samples[19199],0.5);

  const pcm1s=Buffer.alloc(48000);
  const maxPacket=decodeRoomTts({...base,data:{...base.data,
    start_sample:0,end_sample:24000,audio:pcm1s.toString('base64')}});
  assert.equal(maxPacket.samples.length,24000);
});

test('reject invalid PCM without inserting an invented second playback track',()=>{
  for(const data of [
    {...base.data,sample_rate:16000},
    {...base.data,channels:2},
    {...base.data,end_sample:5},
    {...base.data,stream_epoch:0},
    {...base.data,audio:'!'},
    {...base.data,audio:Buffer.from([0]).toString('base64')},
    {...base.data,start_sample:0,end_sample:24001,audio:Buffer.alloc(48002).toString('base64')},
    {...base.data,start_sample:0,end_sample:25000,audio:'A'.repeat(65540)},
  ])assert.throws(()=>decodeRoomTts({...base,data}));
});
