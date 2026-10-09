/** One user turn and one assistant turn are each shown as one message. */
export function splitReplyBubbles(text:string):string[]{
  const source=text.trim();
  return source?[source]:[];
}
