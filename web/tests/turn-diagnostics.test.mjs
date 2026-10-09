import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {selectTurnEvidence} from '../src/turn-evidence.ts';

test('per-turn diagnostics separate direct Realtime replies from actual planner calls',()=>{
  const source=readFileSync(new URL('../src/turn-diagnostics.tsx',import.meta.url),'utf8');
  assert.match(source,/source_turn_id===turnId\|\|item\.result_turn_id===turnId/);
  assert.match(source,/普通回合内模型自己生成的问句不代表 LangGraph 审核/);
  assert.match(source,/planning\?\.continuation\.recent/);
  assert.match(source,/get_weather ·/);
  assert.match(source,/诊断是有界 Session 缓存/);
});

test('a selected turn never borrows the latest turn prompt, tools, or motion',()=>{
  const ident={robot_id:'annie-a',chat_id:'测试',persona_id:'annie',
    session_epoch:7,generation:7};
  const prompt=session=>({...ident,provider_session_id:session,instructions:session,
    bootstrap_text:'',bootstrap_count:0,tool_names:[],history_turns_requested:20,
    history_turns_confirmed:20,restored_turns:[]});
  const event=(turn_id,kind)=>({...ident,turn_id,kind,seq:1});
  const data={...ident,ready:true,provider_session_id:'new',
    prompt_snapshot:prompt('new'),prompt_history:[prompt('old'),prompt('new')],
    turn_prompt_refs:[{turn_id:'t1',provider_session_id:'old'},
      {turn_id:'t2',provider_session_id:'new'}],
    turn_timelines:[{turn_id:'t1',times:{},durations:{},viseme_count:4},
      {turn_id:'t2',times:{},durations:{},viseme_count:0}],
    tool_history:[{...event('t1','tool.called'),name:'recall_memory'},
      {...event('t2','tool.called'),name:'perform_action'},
      {...event(null,'tool.result'),name:'perform_action'}],
    turn_events:[{...event('t1','viseme.aligned')},
      {...event('t2','action.state'),status:'published_unknown'}],
  };
  const first=selectTurnEvidence(data,'t1');
  assert.equal(first.prompt.instructions,'old');
  assert.equal(first.timeline.viseme_count,4);
  assert.deepEqual(first.tools.map(item=>item.name),['recall_memory']);
  assert.deepEqual(first.events.map(item=>item.kind),['viseme.aligned']);
  const second=selectTurnEvidence(data,'t2');
  assert.equal(second.prompt.instructions,'new');
  assert.deepEqual(second.tools.map(item=>item.name),['perform_action']);
  assert.deepEqual(second.events.map(item=>item.kind),['action.state']);
  assert.equal(selectTurnEvidence(data,'old-turn-without-prompt').prompt,null);
});

test('an unavailable historical session is not replaced by the current session',()=>{
  const data={generation:8,prompt_snapshot:{instructions:'wrong',provider_session_id:'now'},
    prompt_history:[],turn_prompt_refs:[{turn_id:'old',provider_session_id:'gone'}],
    turn_timelines:[],tool_history:[],turn_events:[]};
  assert.equal(selectTurnEvidence(data,'old').prompt,null);
  assert.equal(selectTurnEvidence(data,'old').timeline,undefined);
});
