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

test('Room transport receipt remains distinct from physical motion completion',()=>{
  assert.match(panel,/已发至Room，设备执行未知/);
  assert.match(panel,/设备完成回执：未确认/);
  assert.match(panel,/发送结果不确定，禁止盲重试/);
  assert.match(panel,/ANNIE_ACTIONS_ENABLED=1/);
});
