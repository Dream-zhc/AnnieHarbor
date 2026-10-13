/** Gate microphone connection on WASM initialization; never drop its first words. */
export function waitForCaptureReady(processor:AudioWorkletNode,signal:AbortSignal,
  timeoutMs=8000):Promise<void>{
  return new Promise((resolve,reject)=>{
    let timer:ReturnType<typeof setTimeout>|undefined;
    const finish=(error?:Error)=>{
      clearTimeout(timer);
      signal.removeEventListener('abort',cancel);
      processor.port.onmessage=null;
      processor.onprocessorerror=null;
      if(error)reject(error);else resolve();
    };
    const cancel=()=>finish(new Error('麦克风初始化已取消'));
    if(signal.aborted){cancel();return;}
    processor.port.onmessage=event=>{
      if(event.data?.type==='ready')finish();
      else if(event.data?.type==='error')finish(new Error(String(event.data.message)));
    };
    processor.onprocessorerror=()=>finish(new Error('麦克风音频处理器初始化失败'));
    signal.addEventListener('abort',cancel,{once:true});
    timer=setTimeout(()=>finish(new Error('麦克风重采样初始化超时')),timeoutMs);
  });
}
