import type {Robot,RobotSwitches} from './api';
import {headLabel} from './audio-devices.ts';

/** Robots run side by side in their own Rooms; whichever board is powered on is the one working.
 * These say, per robot button, whether its board is online, whether she is in a conversation and
 * which board came online last, and decide when the workbench follows a board that just came online. */

/** After the last speech or transcript the provider heard, she still counts as talking this long. */
export const TALKING_WINDOW_MS=30_000;

/** The board's ROBOT_HEAD_NAME while it is online, otherwise the robot id. */
export const robotName=(robot:Robot)=>robot.head?.head_name||robot.robot_id;

export function isTalking(robot:Robot,now:number){
  const realtime=robot.realtime;
  if(!realtime?.ready)return false;
  if(realtime.response_id||realtime.speech_active||realtime.speech_output?.state==='active')return true;
  const last=Math.max(realtime.last_provider_speech_ms??0,realtime.last_provider_transcript_ms??0);
  return now-last<TALKING_WINDOW_MS;
}

/** The online board with the shortest time online; null when no board is online. */
export function latestOnline(robots:Robot[]){
  let latest:Robot|null=null;
  for(const robot of robots)
    if(robot.head&&(!latest?.head||(robot.head.active_for_ms??0)<(latest.head.active_for_ms??0)))latest=robot;
  return latest;
}

export const switchesText=(switches:RobotSwitches)=>[
  `唤醒词${switches.wake?'开':'关'}`,`回声消除${switches.aec?'开':'关'}`,`嘴型${switches.mouth?'开':'关'}`,
  `摄像头${switches.camera?'开':'关'}`,`动作 ${switches.actions.length} 个`,
].join('，');

export type RobotPresence={online:boolean;talking:boolean;latest:boolean;title:string};

/** "后上线" is only said while two or more boards are online. */
export function robotPresence(robots:Robot[],now:number){
  const latest=robots.filter(robot=>robot.head).length>1?latestOnline(robots):null;
  return new Map<string,RobotPresence>(robots.map(robot=>{
    const talking=isTalking(robot,now);
    const title=[
      robot.head?`板子在线：${headLabel(robot.head)}`:'板子离线',
      ...(talking?['对话中']:[]),
      ...(robot===latest?['后上线：比其它在线的板子晚开机']:[]),
      ...(robot.switches?[switchesText(robot.switches)]:[]),
    ].join('\n');
    return [robot.robot_id,{online:!!robot.head,talking,latest:robot===latest,title}];
  }));
}

export const headKey=(robot:Robot)=>robot.head?`${robot.robot_id}/${robot.head.instance_id}`:null;

/** After a /health poll: the robot whose board has just come online, if it came online after every
 * other online board. `seen` holds the boards of earlier polls and is null on the first one, when
 * the page has just opened and nothing counts as new. */
export function cameOnline(seen:ReadonlySet<string>|null,robots:Robot[]){
  const next=new Set(seen??[]);
  const latest=latestOnline(robots);
  let robot:Robot|null=null;
  for(const item of robots){
    const key=headKey(item);
    if(key===null||next.has(key))continue;
    next.add(key);
    if(seen&&item===latest)robot=item;
  }
  return {seen:next,robot};
}
