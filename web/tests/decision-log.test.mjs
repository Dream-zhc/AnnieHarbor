import test from 'node:test';
import assert from 'node:assert/strict';
import {decisionEntry,formatDecision,isProviderAmbient,parseDecision} from '../src/decision-log.ts';

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

test('parseDecision clarifies expressions when no motion is triggered',()=>{
  const entry=decisionEntry({seq:7,kind:'action.auto_decided',choice:'none',script:null,confidence:0.62,latency_ms:92,text:'你休息吧。'});
  assert(entry);
  const parsed=parseDecision(entry);
  assert.equal(parsed.topicLabel,'表情动作');
  assert.equal(parsed.outcomeText,'保持自然');
  assert.equal(parsed.outcomeSub,'无需动作');
  assert.equal(parsed.outcomeType,'quiet');
  assert.deepEqual(parsed.metrics,['置信 0.62','92 ms']);
});

test('parseDecision formats active motion and declined side talk',()=>{
  const active=decisionEntry({seq:5,kind:'action.auto_decided',choice:'surprise',script:'surprise',confidence:0.71,latency_ms:352,text:'我中了五百万'});
  assert(active);
  const parsedActive=parseDecision(active);
  assert.equal(parsedActive.outcomeText,'执行动作：惊讶');
  assert.equal(parsedActive.outcomeType,'action');

  const declined=decisionEntry({seq:3,kind:'addressee.decided',verdict:'not_robot',choice:'person',confidence:.318,latency_ms:409.6,text:'老王，周末一起去钓鱼吧'});
  assert(declined);
  const parsedDeclined=parseDecision(declined);
  assert.equal(parsedDeclined.outcomeText,'旁话忽略');
  assert.equal(parsedDeclined.outcomeSub,'判定：对别人说');
  assert.equal(parsedDeclined.outcomeType,'declined');
});

test('the decision card names the model in use: d3 while up, Aliyun as fallback',async()=>{
  const {decisionSource,modelName}=await import('../src/decision-log.ts');
  const base={addressee:true,auto_expression:true};
  const up=decisionSource({...base,addressee_model:'d3-NVFP4',expression_model:'d3-NVFP4',
    d3:{model:'d3-NVFP4',status:'ready',ready:true,error:null,checked_ms:1,failures:0}});
  assert.equal(up?.label,'d3 在线');
  assert.equal(up?.tone,'good');
  const down=decisionSource({...base,addressee_model:'qwen-flash',expression_model:'decision-model-preview',
    d3:{model:'d3-NVFP4',status:'down',ready:false,error:'ConnectError',checked_ms:1,failures:1}});
  assert.equal(down?.label,'阿里保底 · d3 离线');
  assert.match(down?.title??'',/ConnectError.*对话对象 阿里 qwen-flash，表情 阿里决策模型/);
  assert.equal(decisionSource({...base,addressee_model:'qwen-flash',expression_model:'decision-model-preview',d3:null})?.label,'阿里');
  assert.equal(decisionSource({addressee:false,auto_expression:false}),null);
  assert.equal(modelName('d3-NVFP4'),'d3');
  const decided=decisionEntry({seq:12,kind:'addressee.decided',verdict:'not_robot',choice:'person',
    confidence:.6,latency_ms:160,model:'d3-NVFP4',text:'他没接视觉吧'});
  assert.equal(decided?.detail,'d3 · 置信 0.60 · 160 ms');
});
