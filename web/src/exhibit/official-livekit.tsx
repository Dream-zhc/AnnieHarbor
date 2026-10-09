import {useEffect, useRef, useState} from 'react';
import {Room, RoomEvent, Track} from 'livekit-client';
import type {RemoteVideoTrack} from 'livekit-client';
import {readVideoCounters, videoRates} from './video-stats';
import type {VideoCounters, VideoRates} from './video-stats';

type Config = {server: string; token: string; source: string; room: string};
type Stats = VideoRates & {width: number; height: number};

/** Single SDK Room per visible robot; subscribe to only the selected camera video. */
export function OfficialLiveKitVideo({robotId}: {robotId: string}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [message, setMessage] = useState('连接 LiveKit 中');
  const [stats, setStats] = useState<Stats | null>(null);
  const [live, setLive] = useState(false);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    let disposed = false;
    let room: Room | null = null;
    let selected: RemoteVideoTrack | null = null;
    let previous: VideoCounters | null = null;
    let retryTimer = 0;
    let statTimer = 0;
    let failures = 0;
    let connecting = false;
    let statsBusy = false;
    let firstFrameShown = false;
    let presentedFrames: number | null = null;
    let presentationCallback = 0;
    const onFirstFrame = () => {
      if (!disposed && selected && !firstFrameShown) {
        firstFrameShown = true;
        setLive(true); setMessage('原生 WebRTC 接收中');
      }
    };
    video.addEventListener('loadeddata', onFirstFrame);
    if (typeof video.requestVideoFrameCallback === 'function') {
      const countPresented: VideoFrameRequestCallback = (_now, metadata) => {
        presentedFrames = metadata.presentedFrames;
        onFirstFrame();
        if (!disposed) presentationCallback = video.requestVideoFrameCallback(countPresented);
      };
      presentationCallback = video.requestVideoFrameCallback(countPresented);
    }

    const detach = () => {
      if (selected) selected.detach(video);
      selected = null;
      firstFrameShown = false;
      previous = null;
      video.srcObject = null;
      if (!disposed) {setLive(false); setStats(null);}
    };
    const attach = (track: RemoteVideoTrack, participant: string, source: string) => {
      if (disposed || participant !== source || track.kind !== Track.Kind.Video) return;
      if (selected === track) return;
      detach();
      selected = track;
      track.attach(video);
      void video.play().catch(() => {if (!disposed) setMessage('浏览器拒绝播放');});
      if (!disposed) {setMessage('等待真实视频首帧'); setLive(false);}
    };
    const connect = async () => {
      if (disposed || connecting) return;
      connecting = true;
      try {
        const res = await fetch(`/api/exhibit/robots/${encodeURIComponent(robotId)}/video-config`,
          {credentials: 'same-origin', cache: 'no-store'});
        if (!res.ok) {
          throw Error(res.status === 503 ? '视频源未配置' : `订阅接口 HTTP ${res.status}`);
        }
        const config = await res.json() as Config;
        if (disposed) return;
        const connection = new Room({adaptiveStream: false, dynacast: false,
          stopLocalTrackOnUnpublish: true, disconnectOnPageLeave: true});
        room = connection;
        connection.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
          if (track.kind === Track.Kind.Video) attach(track as RemoteVideoTrack, participant.identity, config.source);
        });
        connection.on(RoomEvent.TrackUnsubscribed, track => {
          if (track === selected) {detach(); setMessage('等待摄像头视频');}
        });
        connection.on(RoomEvent.TrackPublished, (publication, participant) => {
          if (participant.identity === config.source && publication.kind === Track.Kind.Video)
            publication.setSubscribed(true);
        });
        connection.on(RoomEvent.Disconnected, () => {
          detach();
          if (!disposed && room === connection) {
            room = null;
            setMessage('房间断开，重连中');
            retryTimer = window.setTimeout(() => void connect(), 1500);
          }
        });
        await connection.connect(config.server, config.token, {autoSubscribe: false});
        if (disposed) {await connection.disconnect(); return;}
        failures = 0;
        setMessage('等待摄像头发布');
        for (const participant of connection.remoteParticipants.values()) {
          if (participant.identity !== config.source) continue;
          for (const publication of participant.videoTrackPublications.values()) {
            publication.setSubscribed(true);
            if (publication.track) attach(publication.track as RemoteVideoTrack, participant.identity, config.source);
          }
        }
      } catch (error) {
        if (!disposed) {
          const broken = room;
          room = null;
          if (broken) void broken.disconnect();
          setMessage(String(error instanceof Error ? error.message : error));
          retryTimer = window.setTimeout(() => void connect(),
            Math.min(12000, 800 * 2 ** Math.min(failures++, 4)));
        }
      } finally {connecting = false;}
    };

    // Low-rate RTC stats; never copies decoded video into canvas or React state.
    statTimer = window.setInterval(async () => {
      const track = selected;
      if (!track || disposed || document.hidden || statsBusy) return;
      statsBusy = true;
      try {
        const report = await track.getRTCStatsReport();
        if (!report || track !== selected || disposed) return;
        const quality = video.getVideoPlaybackQuality?.() ?? null;
        // Browser presentation callbacks report actually presented frames;
        // video quality's totalVideoFrames can include dropped frames.
        const presented = presentedFrames ?? (quality
          ? Math.max(0, quality.totalVideoFrames - quality.droppedVideoFrames) : null);
        const current = readVideoCounters(report, presented === null ? null : {
          totalVideoFrames: presented,
          droppedVideoFrames: quality?.droppedVideoFrames ?? 0,
        });
        if (!current) {setStats(null); return;}
        const rates = videoRates(previous, current);
        previous = current;
        setStats({width: video.videoWidth, height: video.videoHeight, ...rates});
      } catch {if (!disposed) setStats(null);}
      finally {statsBusy = false;}
    }, 1000);
    void connect();
    return () => {
      disposed = true;
      window.clearInterval(statTimer);
      window.clearTimeout(retryTimer);
      video.removeEventListener('loadeddata', onFirstFrame);
      if (presentationCallback) video.cancelVideoFrameCallback(presentationCallback);
      detach();
      if (room) void room.disconnect();
    };
  }, [robotId]);

  const fps = (x: number | null | undefined) => x == null ? '--' : x.toFixed(1);
  return <div className="ex-video-wrap">
    <video ref={videoRef} autoPlay playsInline muted aria-label="LiveKit 官方 SDK 实时视频" />
    {!live && <div className="ex-unavailable"><strong>{message}</strong><small>LIVEKIT · 原生 WebRTC</small></div>}
    <div className="ex-video-stats">{stats ?
      `${stats.width}×${stats.height} · 解码 ${fps(stats.decodeFps)} FPS · 显示 ${fps(stats.displayFps)} FPS · ${fps(stats.bitrateMbps)} Mbps · 丢帧 ${stats.dropped ?? '--'} · 丢包 ${stats.packetsLost ?? '--'}`
      : 'LIVEKIT / 等待实测帧率'}</div>
  </div>;
}
