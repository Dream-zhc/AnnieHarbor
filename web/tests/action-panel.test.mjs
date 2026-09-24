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

test('successful Room dispatch is the business execution criterion, not a queued or uncertain send',()=>{
  assert.match(panel,/已执行（已发至 Room）/);
  assert.match(panel,/发至 Room 即完成/);
  assert.match(panel,/发送结果不确定，禁止盲重试/);
  assert.match(panel,/ANNIE_ACTIONS_ENABLED=1/);
});

test('cloud-triggered actions display an action icon and badge next to 小安妮',()=>{
  assert.match(workbench,/getTurnActions/);
  assert.match(workbench,/turn-action-badge/);
  assert.match(workbench,/turn-action-icon/);
  assert.match(workbench,/🎭/);
  assert.match(workbench,/ACTION_SCRIPT_NAMES/);
});
