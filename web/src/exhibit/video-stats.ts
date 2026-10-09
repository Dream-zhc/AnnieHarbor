/** LiveKit RTC inbound video + browser presentation stats; no synthetic FPS. */
export type VideoCounters = {
  timestamp: number;
  bytes: number;
  decoded: number;
  presented: number | null;
  dropped: number | null;
  packetsLost: number | null;
};

export type VideoRates = {
  decodeFps: number | null;
  displayFps: number | null;
  bitrateMbps: number | null;
  dropped: number | null;
  packetsLost: number | null;
};

function nonnegative(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

export function readVideoCounters(
  report: RTCStatsReport,
  quality: {totalVideoFrames: number; droppedVideoFrames: number} | null,
): VideoCounters | null {
  let inbound: (RTCStats & {kind?: string; mediaType?: string;
    bytesReceived?: number; framesDecoded?: number; framesDropped?: number;
    packetsLost?: number}) | null = null;
  report.forEach(item => {
    if (item.type !== 'inbound-rtp') return;
    const stat = item as typeof inbound & {kind?: string; mediaType?: string};
    if (stat?.kind === 'video' || stat?.mediaType === 'video') inbound = stat;
  });
  if (!inbound) return null;
  const item = inbound as RTCStats & {bytesReceived?: number; framesDecoded?: number;
    framesDropped?: number; packetsLost?: number};
  const timestamp = nonnegative(item.timestamp);
  const bytes = nonnegative(item.bytesReceived);
  const decoded = nonnegative(item.framesDecoded);
  if (timestamp === null || bytes === null || decoded === null) return null;
  return {timestamp, bytes, decoded,
    presented: nonnegative(quality?.totalVideoFrames),
    dropped: nonnegative(quality?.droppedVideoFrames) ?? nonnegative(item.framesDropped),
    packetsLost: nonnegative(item.packetsLost)};
}

export function videoRates(prev: VideoCounters | null, now: VideoCounters): VideoRates {
  const seconds = prev ? (now.timestamp - prev.timestamp) / 1000 : 0;
  const delta = (a: number | null, b: number | null) =>
    seconds >= .15 && seconds <= 10 && a !== null && b !== null && a >= b ? a - b : null;
  const decoded = delta(now.decoded, prev?.decoded ?? null);
  const presented = delta(now.presented, prev?.presented ?? null);
  const bytes = delta(now.bytes, prev?.bytes ?? null);
  return {
    decodeFps: decoded === null ? null : decoded / seconds,
    displayFps: presented === null ? null : presented / seconds,
    bitrateMbps: bytes === null ? null : bytes * 8 / seconds / 1e6,
    dropped: now.dropped,
    packetsLost: now.packetsLost,
  };
}
