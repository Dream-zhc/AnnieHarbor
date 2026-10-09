import test from 'node:test';
import assert from 'node:assert/strict';
import {memoryText,memoryType,visibleMemory,invalidMemory} from '../src/memory-display.ts';

const row=(kind,text,extra={})=>({
  id:'1',kind,text,persona_id:'annie-lively',fact_key:'fact',source_turn_id:'turn1',
  version:1,status:'active',index_state:'ready',...extra,
});

test('memory is scoped and only active, nonexpired facts are shown',()=>{
  const shared=row('semantic','用户喜欢茶');
  const persona=row('persona','用户要求当前AI助手不要骂他');
  const episode=row('episodes','用户提到下周有朋友来找他');
  const chat=row('chat','本次聊天不要骂我');
  assert.equal(visibleMemory(shared,'shared',10),true);
  assert.equal(visibleMemory(shared,'persona',10),false);
  assert.equal(visibleMemory(persona,'persona',10),true);
  assert.equal(visibleMemory(episode,'persona',10),true);
  assert.equal(visibleMemory(chat,'chat',10),true);
  assert.equal(visibleMemory(chat,'persona',10),false);
  assert.equal(visibleMemory({...shared,status:'deleted'},'shared',10),false);
  assert.equal(visibleMemory({...episode,source_valid:false},'persona',10),false);
  assert.equal(visibleMemory({...chat,expires_at_ms:10},'chat',10),false);
  assert.equal(visibleMemory({...chat,expires_at_ms:11},'chat',10),true);
});

test('legacy extraction summaries and capability probes never count as memories',()=>{
  for(const [key,value] of [
    ['no-extractable-content','用户询问下周是否有朋友找他，属临时日程查询，无明确事实陈述，不提取。'],
    ['no_extraction','placeholder'],
    ['persona_capability_question_01','用户曾询问当前AI助手是否具备自行安装软件的能力。'],
    ['persona_nod_request_01','用户曾要求当前AI助手点下头，测试其物理动作能力。'],
  ]){
    const item=row('persona',value,{fact_key:key});
    assert.equal(invalidMemory(item),true,key);
    assert.equal(visibleMemory(item,'persona',Date.now()),false,key);
  }
  assert.equal(invalidMemory(row('persona','用户要求当前AI助手不要骂他')),false);
});

test('human-facing text stays factual and canonical storage remains untouched',()=>{
  assert.equal(memoryText('用户喜欢茶'),'你喜欢茶');
  assert.equal(memoryText('用户提到下周有朋友来找他。'),'你提到下周有朋友来找你。');
  assert.equal(memoryText('用户要求当前AI助手不要骂他。'),'你要求当前角色不要骂你。');
  assert.equal(memoryText('用户曾向当前 Persona 表达喜欢。'),'你曾向当前角色表达喜欢。');
  assert.equal(memoryText('用户最近准备去看车，进展尚未确认。'),'你最近准备去看车，进展尚未确认。');
  assert.equal(memoryText('用户曾向当前 Persona 表达喜欢，后来更正不喜欢。'),
    '你曾向当前角色表达喜欢，后来更正不喜欢。');
  assert.equal(memoryType(row('persona','')), '关系');
  assert.equal(memoryType(row('episodes','')), '经历');
  assert.equal(memoryType({...row('episodes','用户最近准备去看车。'),fact_key:'ongoing_plan_car_viewing'}),'近期计划');
});
