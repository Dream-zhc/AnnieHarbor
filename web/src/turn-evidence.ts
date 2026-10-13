import type {PromptSnapshot,RealtimeDiagnostics} from './api';

/** Bind evidence only to the selected turn, never to another turn in its Session. */
export function selectTurnEvidence(diagnostics:RealtimeDiagnostics|null,turnId:string|undefined){
  const belongs=(item:Partial<RealtimeDiagnostics>)=>!!diagnostics&&
    (['robot_id','chat_id','persona_id','session_epoch','generation'] as const)
      .every(key=>item[key]===undefined||item[key]===diagnostics[key]);
  const timeline=diagnostics?.turn_timelines?.find(item=>item.turn_id===turnId&&belongs(item));
  const ref=diagnostics?.turn_prompt_refs?.find(item=>item.turn_id===turnId);
  const sessionPrompt:PromptSnapshot|null=ref?.provider_session_id?
    diagnostics?.prompt_history?.find(item=>item.provider_session_id===ref.provider_session_id&&
      item.generation===diagnostics.generation&&belongs(item))??null:null;
  const prompt:PromptSnapshot|null=
    diagnostics?.turn_prompt_snapshots?.find(item=>item.turn_id===turnId&&belongs(item.prompt))?.prompt??sessionPrompt;
  const tools=(diagnostics?.tool_history??[]).filter(item=>item.turn_id===turnId&&
    item.generation===diagnostics?.generation&&belongs(item));
  const events=(diagnostics?.turn_events??[]).filter(item=>item.turn_id===turnId&&
    item.generation===diagnostics?.generation&&belongs(item));
  return {timeline,tts:timeline?.tts??null,prompt,tools,events};
}
