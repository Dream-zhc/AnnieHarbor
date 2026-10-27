import assert from 'node:assert/strict';
import test from 'node:test';
import {beijingTime, innerLifeView} from '../src/inner-life-state.ts';

// 2026-10-26 10:20 Beijing
const AT = Date.UTC(2026, 9, 26, 2, 20);

const sample = {
  robot_id: 'annie-a', enabled: true, started: true,
  mood: {label: '心情不错', text: '心情不错（原因：有人轻轻按了你的额头，刚才）；精力还行',
    valence: 0.33, energy: 0.62, energy_label: '精力还行', tone: 'happy',
    reason: '有人轻轻按了你的额头', reason_ms: AT},
  today: [
    {kind: 'arrival', text: '张三来了', count: 1, person: '张三', created_ms: AT - 600000},
    {kind: 'touch', text: '有人轻轻按了你的额头', count: 3, person: null, created_ms: AT},
  ],
  conversation: {open: true, turns: 2, who: '张三', started_ms: AT},
  summarizer: 'qwen-flash', hint_in_session: true,
  stats: {appraisals: 4, appraisal_failures: 0, summaries: 1, summary_failures: 0, write_failures: 0,
    last_appraisal: {choice: 'moved', confidence: 0.91, applied: true, model: 'd3-NVFP4', ts_ms: AT}},
};

test('her mood, energy and day show only what the runtime reported', () => {
  const view = innerLifeView(sample, '已同步');
  assert.equal(view.badge, '心情不错');
  assert.equal(view.energy, '精力还行（62%）');
  assert.equal(view.valence, '+0.33（-1 难过 … +1 开心）');
  assert.deepEqual(view.day.map(row => `${row.time} ${row.text}`),
    ['10:10 张三来了', '10:20 有人轻轻按了你的额头（共3次）']);
  assert.equal(view.conversation, '进行中 · 2 轮 · 和张三');
  assert.equal(view.appraisal, '感动 · 置信度 0.91 · 已影响心情');
  assert.equal(view.session, '已写进当前会话提示词');
  assert.equal(view.failures, '');
});

test('missing, disabled and failing states are told apart', () => {
  assert.equal(innerLifeView(null, '读取失败').badge, '读取失败');
  const off = innerLifeView({robot_id: 'annie-a', enabled: false}, '已同步');
  assert.equal(off.badge, '未启用');
  assert.match(off.mood, /ANNIE_INNER_LIFE_ENABLED/);
  const failing = innerLifeView({...sample, summarizer: null, conversation: {open: false},
    stats: {...sample.stats, appraisal_failures: 2, last_appraisal: null}}, '已同步');
  assert.equal(failing.failures, '心情判断失败 2 次');
  assert.equal(failing.appraisal, '还没有判断过');
  assert.equal(failing.conversation, '没有进行中的对话');
  assert.match(failing.summarizer, /未配置/);
});

test('times are Beijing wall clock', () => {
  assert.equal(beijingTime(AT), '10:20');
});
