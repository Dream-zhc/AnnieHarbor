/** Phase 2 Room BinaryFrame: two-byte big-endian type length + MIC_AUDIO + 640B PCM. */
export function pcmFrame(samples:Uint8Array){
  if(samples.length!==640)throw new Error('每帧必须包含 640B PCM');
  const type=new TextEncoder().encode('MIC_AUDIO');
  const frame=new Uint8Array(2+type.length+samples.length);
  frame[0]=type.length>>8;frame[1]=type.length&255;
  frame.set(type,2);frame.set(samples,2+type.length);
  return frame;
}
