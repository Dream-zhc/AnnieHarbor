import test from 'node:test';
import assert from 'node:assert/strict';
import {splitReplyBubbles} from '../src/reply-bubbles.ts';

test('short reply remains one bubble',()=>{
  assert.deepEqual(splitReplyBubbles('你好呀。'),['你好呀。']);
});

test('a natural long reply remains one bubble with original punctuation',()=>{
  const text='今天看起来挺累的，事情肯定不少吧。要不要先歇一歇，喝口水？我这儿先陪你聊聊，不着急。';
  assert.deepEqual(splitReplyBubbles(text),[text]);
});

test('missing punctuation does not invent multiple turns',()=>{
  assert.deepEqual(splitReplyBubbles('一'.repeat(80)),['一'.repeat(80)]);
  assert.deepEqual(splitReplyBubbles(''),[]);
});

test('newlines within a single turn are preserved in one bubble',()=>{
  assert.deepEqual(splitReplyBubbles('第一段。\n\n第二段？'),['第一段。\n\n第二段？']);
});
