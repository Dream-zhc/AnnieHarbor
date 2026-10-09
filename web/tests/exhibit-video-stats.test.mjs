import assert from 'node:assert/strict';
import test from 'node:test';
import {readVideoCounters, videoRates} from '../src/exhibit/video-stats.ts';

function report(stat) {
  return new Map([['video', {type: 'inbound-rtp', kind: 'video', ...stat}]]);
}

test('decoding FPS is not presented FPS; real bitrate, drop and loss', () => {
  const prev = readVideoCounters(report({timestamp: 1000, bytesReceived: 100000,
    framesDecoded: 40, framesDropped: 1, packetsLost: 2}),
    {totalVideoFrames: 35, droppedVideoFrames: 1});
  const next = readVideoCounters(report({timestamp: 2000, bytesReceived: 700000,
    framesDecoded: 70, framesDropped: 4, packetsLost: 3}),
    {totalVideoFrames: 50, droppedVideoFrames: 4});
  assert.ok(prev && next);
  assert.deepEqual(videoRates(prev, next), {
    decodeFps: 30, displayFps: 15, bitrateMbps: 4.8, dropped: 4, packetsLost: 3,
  });
});

test('first observation, counter reset, no stats and long gaps stay unknown', () => {
  const first = {timestamp: 1000, bytes: 500, decoded: 50, presented: null,
    dropped: null, packetsLost: null};
  assert.equal(videoRates(null, first).decodeFps, null);
  assert.equal(videoRates(first, {...first, timestamp: 2000, decoded: 0}).decodeFps, null);
  assert.equal(videoRates(first, {...first, timestamp: 20000, decoded: 80}).decodeFps, null);
  assert.equal(videoRates(first, {...first, timestamp: 2000, decoded: 80}).displayFps, null);
  assert.equal(readVideoCounters(new Map(), null), null);
  assert.equal(readVideoCounters(report({timestamp: 1000, framesDecoded: 40}), null), null);
});
