// Demo v1.7 uses one low-latency AudioContext for capture and audible TTS.
// The Room input/output connections remain separate and robot-scoped.
let shared:AudioContext|null=null;
let users=0;
const modules=new WeakMap<AudioContext,Map<string,Promise<void>>>();

export function acquireAudioContext():AudioContext{
  if(!shared||shared.state==='closed'){
    try{
      shared=new AudioContext({latencyHint:'interactive',sampleRate:48000});
    }catch{
      shared=new AudioContext({latencyHint:'interactive'});
    }
  }
  users++;
  return shared;
}

export function releaseAudioContext(context:AudioContext){
  if(context!==shared)return;
  users=Math.max(0,users-1);
  if(users===0){
    shared=null;
    void context.close();
  }
}

export function loadAudioWorklet(context:AudioContext,url:string):Promise<void>{
  let loaded=modules.get(context);
  if(!loaded){loaded=new Map();modules.set(context,loaded);}
  const prior=loaded.get(url);
  if(prior)return prior;
  const promise=context.audioWorklet.addModule(url).catch(error=>{
    loaded!.delete(url);
    throw error;
  });
  loaded.set(url,promise);
  return promise;
}
