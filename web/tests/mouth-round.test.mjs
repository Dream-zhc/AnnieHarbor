import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {mouthRoundRows,mouthRoundTitle} from '../src/mouth-round.ts';

const round={conversation_id:'t1',age_ms:12_400,text_chars:18,expected_chars:15,
  mouth_received:14,mouth_reported:15,dropped_count:1,audio_total_ms:3820,coverage_ratio:0.913,
  first_delay_ms:420,avg_delay_ms:160,max_delay_ms:310,late_count:2,summary_received:true};

test('last mouth round shows text size, pushed count, and delay against TTS',()=>{
  assert.equal(mouthRoundTitle(round),'上一轮 · 12 秒前');
  assert.deepEqual(mouthRoundRows(round),[
    {label:'文字',value:'18 字 · 应出 15 个口型'},
    {label:'推送',value:'收到 14 条 · 服务端发出 15 条',warn:true},
    {label:'丢字',value:'1 个未对齐',warn:true},
    {label:'首个嘴型',value:'首帧 TTS 发出后 420 ms'},
    {label:'相对 TTS',value:'平均 160 ms · 最大 310 ms'},
    {label:'来晚',value:'2 条（到达时该字已播过）',warn:true},
    {label:'音频',value:'3.8 秒 · 口型覆盖 91%'},
  ]);
});

test('a round without local TTS timing or service summary says so instead of zeros',()=>{
  const bare={...round,text_chars:null,expected_chars:null,mouth_reported:null,dropped_count:null,
    audio_total_ms:null,coverage_ratio:null,first_delay_ms:null,avg_delay_ms:null,
    max_delay_ms:null,late_count:null,summary_received:false,age_ms:90_000};
  assert.equal(mouthRoundTitle(bare),'上一轮 · 2 分钟前 · 服务汇总未到');
  assert.deepEqual(mouthRoundRows(bare),[
    {label:'文字',value:'—'},
    {label:'推送',value:'收到 14 条',warn:false},
    {label:'延迟',value:'无本机 TTS 计时（非本机播报）'},
  ]);
});

test('mouth pill opens an accessible hover/focus popover instead of a native title',()=>{
  const source=readFileSync(new URL('../src/room-observations.tsx',import.meta.url),'utf8');
  assert.match(source,/tabIndex=\{0\} aria-describedby=\{popoverId\}/);
  assert.match(source,/className="mouth-popover" role="tooltip"/);
  assert.doesNotMatch(source,/title=\{visemeTooltip\}/);
  const css=readFileSync(new URL('../src/room-observations.css',import.meta.url),'utf8');
  assert.match(css,/\.room-observation-wrap:focus-within \.mouth-popover/);
  assert.match(css,/prefers-reduced-motion/);
});
