// Latest mouth round as reported by /room-observations (viseme.last_round).
export type MouthRound={conversation_id:string;age_ms:number;text_chars:number|null;
  expected_chars:number|null;mouth_received:number;mouth_reported:number|null;
  dropped_count:number|null;audio_total_ms:number|null;coverage_ratio:number|null;
  first_delay_ms:number|null;avg_delay_ms:number|null;max_delay_ms:number|null;
  late_count:number|null;summary_received:boolean};

export type MouthRoundRow={label:string;value:string;warn?:boolean};

const ago=(ms:number)=>ms<60_000?`${Math.max(0,Math.round(ms/1000))} 秒前`:`${Math.round(ms/60_000)} 分钟前`;

export function mouthRoundTitle(round:MouthRound){
  return `上一轮 · ${ago(round.age_ms)}${round.summary_received?'':' · 服务汇总未到'}`;
}

export function mouthRoundRows(round:MouthRound):MouthRoundRow[]{
  const text=round.text_chars===null?'—':`${round.text_chars} 字`;
  const rows:MouthRoundRow[]=[
    {label:'文字',value:round.expected_chars===null?text:`${text} · 应出 ${round.expected_chars} 个口型`},
    {label:'推送',value:`收到 ${round.mouth_received} 条`+
      (round.mouth_reported===null?'':` · 服务端发出 ${round.mouth_reported} 条`),
     warn:round.mouth_reported!==null&&round.mouth_reported!==round.mouth_received},
  ];
  if(round.dropped_count)rows.push({label:'丢字',value:`${round.dropped_count} 个未对齐`,warn:true});
  if(round.first_delay_ms===null){
    rows.push({label:'延迟',value:'无本机 TTS 计时（非本机播报）'});
  }else{
    rows.push({label:'首个嘴型',value:`首帧 TTS 发出后 ${round.first_delay_ms} ms`});
    rows.push({label:'相对 TTS',value:`平均 ${round.avg_delay_ms} ms · 最大 ${round.max_delay_ms} ms`});
    rows.push({label:'来晚',value:round.late_count?`${round.late_count} 条（到达时该字已播过）`:'0 条',
      warn:Boolean(round.late_count)});
  }
  if(round.audio_total_ms!==null){
    rows.push({label:'音频',value:`${(round.audio_total_ms/1000).toFixed(1)} 秒`+
      (round.coverage_ratio===null?'':` · 口型覆盖 ${Math.round(round.coverage_ratio*100)}%`)});
  }
  return rows;
}
