/** Presentation only: one Realtime turn remains one persisted turn and one TTS stream. */
export function splitReplyBubbles(text:string):string[]{
  const source=text.trim();
  if(!source||source.length<40)return source?[source]:[];
  const sentences=source.match(/[^。！？!?]+[。！？!?]+|[^。！？!?]+$/g)?.map(s=>s.trim()).filter(Boolean)??[];
  if(sentences.length<2)return [source];
  const result:string[]=[];
  for(const sentence of sentences){
    if(result.length===3){
      result[2]+=' '+sentence;
    }else if(result.length&&result.at(-1)!.length<18){
      result[result.length-1]+=' '+sentence;
    }else{
      result.push(sentence);
    }
  }
  return result.length>1?result:[source];
}
