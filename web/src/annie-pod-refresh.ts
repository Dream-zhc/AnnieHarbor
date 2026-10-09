const statusEvents = new Set([
  'wakeword.detected', 'wakeword.error', 'wakeword.transcript_ignored',
  'speech.started', 'speech.stopped', 'transcript.done', 'transcript.ambient',
  'turn.started', 'turn.done', 'turn.cancelled',
  'session.ready', 'session.error', 'session.closed', 'session.replaced',
  'response.created', 'text.done',
  'tts.segment_queued', 'tts.text_submitted', 'tts.first_audio',
  'tts.first_published', 'tts.stream_finished', 'tts.error',
  'tts.stop_requested', 'tts.stop_published',
  'tts.playback.started', 'tts.playback.resumed', 'tts.playback.held',
  'tts.playback.drained', 'tts.playback.stopped', 'tts.playback.control_error',
]);

/** Only state transitions trigger reads, never per-PCM/per-token telemetry. */
export const isPodStatusEvent = (kind: string) => statusEvents.has(kind);

/** Serialize reads; a transition during an in-flight read gets a trailing read. */
export function createPodRefresh(read: () => Promise<void>, visible: () => boolean) {
  let stopped = false;
  let busy = false;
  let pending = false;
  const request = async (): Promise<void> => {
    if (stopped || !visible()) return;
    if (busy) { pending = true; return; }
    busy = true;
    try {
      await read();
    } finally {
      busy = false;
      if (pending && !stopped) {
        pending = false;
        void request();
      }
    }
  };
  return {request, stop: () => { stopped = true; pending = false; }};
}
