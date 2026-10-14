import test from 'node:test';
import assert from 'node:assert/strict';
import {decisionEntry,formatDecision,isProviderAmbient} from '../src/decision-log.ts';

test('declined side talk reads as a decision that keeps listening',()=>{
  const entry=decisionEntry({seq:3,kind:'addressee.decided',verdict:'not_robot',choice:'person',
    confidence:.318,latency_ms:409.6,text:'老王，周末一起去钓鱼吧'});
  assert.equal(formatDecision(entry),'对谁说话：对别人说 → 不回复，继续听 · 置信 0.32 · 410 ms 「老王，周末一起去钓鱼吧」');
});

test('a failed decision says the robot answered anyway',()=>{
  const entry=decisionEntry({seq:4,kind:'addressee.decided',verdict:'robot',error:'ConnectError',text:'讲个笑话'});
  assert.equal(entry.choice,'决策失败');
  assert.equal(entry.outcome,'回复');
  assert.equal(entry.detail,'按对机器人说处理');
});

test('expressions name the chosen face and whether the model was asked',()=>{
  assert.equal(formatDecision(decisionEntry({seq:5,kind:'action.auto_decided',choice:'surprise',
    script:'surprise',confidence:.71,latency_ms:352,text:'我中了五百万'})),
  '表情动作：惊讶 → 执行惊讶 · 置信 0.71 · 352 ms 「我中了五百万」');
  const named=decisionEntry({seq:6,kind:'action.auto_decided',choice:'agree_micro_nod',
    script:'agree_micro_nod',source:'explicit',text:'点个头'});
  assert.equal(named.detail,'用户点名，未调用模型');
  assert.equal(decisionEntry({seq:7,kind:'action.auto_decided',choice:'none',script:null}).outcome,'不做');
});

test('recovered calls are listed; unrelated events are not',()=>{
  assert.equal(decisionEntry({seq:8,kind:'turn.recovered',text:'小安妮，我们刚才说什么'}).topic,'判停补发');
  assert.equal(decisionEntry({seq:9,kind:'transcript.done',text:'你好'}),null);
});

test('only Qwen ambient audio stays in the ambient card',()=>{
  assert.equal(isProviderAmbient({kind:'transcript.ambient'}),true);
  assert.equal(isProviderAmbient({kind:'transcript.ambient',reason:'not_addressed'}),false);
});
