import type {PromptSnapshot,RealtimeDiagnostics} from './api';

/** Bind evidence only to the selected turn, never to another turn in its Session. */
export function selectTurnEvidence(diagnostics:RealtimeDiagnostics|null,turnId:string|undefined){
  const timeline=diagnostics?.turn_timelines?.find(item=>item.turn_id===turnId);
  const ref=diagnostics?.turn_prompt_refs?.find(item=>item.turn_id===turnId);
  const sessionPrompt:PromptSnapshot|null=ref?.provider_session_id?
    diagnostics?.prompt_history?.find(item=>item.provider_session_id===ref.provider_session_id&&
      item.generation===diagnostics.generation)??null:null;
  const prompt:PromptSnapshot|null=
    diagnostics?.turn_prompt_snapshots?.find(item=>item.turn_id===turnId)?.prompt??sessionPrompt;
  const tools=(diagnostics?.tool_history??[]).filter(item=>item.turn_id===turnId&&
    item.generation===diagnostics?.generation);
  const events=(diagnostics?.turn_events??[]).filter(item=>item.turn_id===turnId&&
    item.generation===diagnostics?.generation);
  return {timeline,prompt,tools,events};
}
