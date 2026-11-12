type StyleProfile={persona_id:string;label:string};
type Styled={persona_style?:string|null};

/** The personality a reply was said in, and whether it changed since the reply before. */
export function personaMark(turn:Styled,previous:Styled|null|undefined,profiles:StyleProfile[]){
  const label=turn.persona_style?profiles.find(item=>item.persona_id===turn.persona_style)?.label??null:null;
  return {label,switched:!!label&&!!previous?.persona_style&&previous.persona_style!==turn.persona_style};
}
