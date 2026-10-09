import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

const panel=readFileSync(new URL('../src/action-panel.tsx',import.meta.url),'utf8');
const workbench=readFileSync(new URL('../src/workbench.tsx',import.meta.url),'utf8');

test('Phase 7 action view loads only the selected robot and never directly sends motor calls',()=>{
  assert.match(panel,/robotPath\(robotId\)\+'\/actions'/);
  assert.match(panel,/onFillText\(sample\[id\]\)/);
  assert.doesNotMatch(panel,/api\([^\n]+POST/);
  assert.match(workbench,/ActionPanel/);
  assert.match(workbench,/onClick=\{\(\)=>setActionOpen\(true\)\}/);
});

test('Room publication is distinct from physical execution and queued or uncertain send',()=>{
  assert.match(panel,/云端下发完成 · 设备执行未核验/);
  assert.match(panel,/Room 发布/);
  assert.match(panel,/等待回复包含目标词/);
  assert.match(panel,/已错过触发点，动作未发布/);
  assert.match(panel,/发送结果不确定，禁止盲重试/);
  assert.match(panel,/ANNIE_ACTIONS_ENABLED=1/);
});

test('UI does not infer Room publication from a tool call alone',()=>{
  assert.match(workbench,/call_observed:'工具调用待确认'/);
  assert.match(workbench,/status:'call_observed'/);
  assert.doesNotMatch(workbench,/status:'published_unknown'/);
  const diagnostics=readFileSync(new URL('../src/turn-diagnostics.tsx',import.meta.url),'utf8');
  assert.match(diagnostics,/等待回复包含目标词/);
  assert.match(diagnostics,/云端下发完成（设备执行未核验）/);
});

test('cloud-triggered actions display an action icon and badge next to 小安妮',()=>{
  assert.match(workbench,/getTurnActions/);
  assert.match(workbench,/turn-action-badge/);
  assert.match(workbench,/turn-action-icon/);
  assert.match(workbench,/🎭/);
  assert.match(workbench,/ACTION_SCRIPT_NAMES/);
});
