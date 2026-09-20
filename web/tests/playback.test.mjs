import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {playbackReceipt} from '../src/playback-contract.ts';

test('browser uses the exact speaker Room receipt fields but labels itself browser_test',()=>{
  const item=playbackReceipt('started',{
    conversation_id:'turn-1',stream_epoch:2,played_sample_count:0,seq:1,
    playback_id:'browser-1',source:'browser_test',
  });
  assert.equal(item.type,'tts.playback.started');
  assert.deepEqual(item.data,{
    protocol_version:1,sample_rate:24000,conversation_id:'turn-1',
    stream_epoch:2,played_sample_count:0,seq:1,playback_id:'browser-1',source:'browser_test',
  });
  assert.throws(()=>playbackReceipt('started',{
    ...item.data,stream_epoch:0,
  }),/invalid playback receipt/);
});

test('browser playback never promotes itself to real speaker and uses actual Room URLs',async()=>{
  const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../src');
  const code=await readFile(path.join(root,'playback.tsx'),'utf8');
  assert.match(code,/route\.urls\.tts/);
  assert.match(code,/route\.urls\.main/);
  assert.match(code,/route\.urls\.sam/);
  assert.match(code,/source:'browser_test'/);
  assert.match(code,/new AudioContext/);
  assert.match(code,/played_sample_count/);
  assert.match(code,/stream_final/);
  assert.doesNotMatch(code,/source:'speaker'/);
});
