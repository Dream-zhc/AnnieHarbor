const $ = (id) => document.getElementById(id);

let ws = null;
let audioContext = null;
let captureNode = null;
let playerNode = null;
let outputGainNode = null;
let playbackResampler = null;
let micStream = null;
let connected = false;
let playbackActive = false;
let playerHeld = false;
let sourceFinished = false;
let currentGeneration = 0;
let drainedSentGeneration = 0;
const interruptedGenerations = new Set();

let noiseFloor = -62;
let candidateFrames = 0;
let candidateReleaseFrames = 0;
let candidateReleaseSent = false;
let candidateActive = false;
let cloudSpeechActive = false;
let localSpeculationDisabledUntil = 0;
let localCandidateArmedAt = 0;
let falseCandidateTimes = [];
let lastHoldTrimLogAt = 0;
let lastUnderflowLogAt = 0;
let underflowCount = 0;
let candidateTimer = null;
let candidateRequestedAt = 0;
let holdRequestId = 0;
let clearRequestId = 0;
let pendingClearRequestedAt = 0;
let currentGain = 1;
let assistantText = '';
let bargeMode = 'qwen';

const params = {
  absoluteDbfs: -46,
  minSnr: 7,
  candidateFrames: 2,
  localTimeoutMs: 700,
  localArmDelayMs: 180,
  localReleaseFrames: 4,
  fadeOutMs: 3,
  fadeInMs: 12,
  maxQueueMs: 30000,
  prebufferMs: 180,
  rebufferMs: 100,
  holdQueueMs: 240,
  falseCandidateLimit: 2,
  falseCandidateWindowMs: 8000,
  localSuppressMs: 15000,
  duckGain: 0.18,
};

function log(message, cls = '') {
  const row = document.createElement('div');
  row.className = `log-row ${cls}`;
  row.textContent = `${new Date().toLocaleTimeString()}  ${message}`;
  $('log').prepend(row);
  while ($('log').children.length > 120) $('log').lastChild.remove();
}

function setStatus(text, kind = 'idle') {
  $('status').textContent = text;
  $('status').className = `status ${kind}`;
}

function sendJson(payload) {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
}

function rampOutputGain(value, fadeMs = 0) {
  currentGain = Math.max(0, Math.min(1, Number(value)));
  if (outputGainNode && audioContext) {
    const now = audioContext.currentTime;
    const param = outputGainNode.gain;
    param.cancelScheduledValues(now);
    param.setValueAtTime(param.value, now);
    if (fadeMs > 0) param.linearRampToValueAtTime(currentGain, now + fadeMs / 1000);
    else param.setValueAtTime(currentGain, now);
  }
  $('gainValue').textContent = currentGain <= 0.001 ? '静音' : `${Math.round(currentGain * 100)}%`;
}

function setPlayerGain(value) {
  // A/B duck 走图输出增益；播放器本身仍持续消费。
  rampOutputGain(value, 4);
}

function resetLocalCandidateState() {
  candidateActive = false;
  candidateFrames = 0;
  candidateReleaseFrames = 0;
  candidateReleaseSent = false;
  candidateRequestedAt = 0;
  clearTimeout(candidateTimer);
}

function registerFalseCandidate(reason) {
  if (!['local_release', 'local_timeout', 'hold_watchdog'].includes(reason)) return;
  const now = performance.now();
  falseCandidateTimes = falseCandidateTimes.filter(
    (t) => now - t <= params.falseCandidateWindowMs,
  );
  falseCandidateTimes.push(now);
  if (falseCandidateTimes.length >= params.falseCandidateLimit) {
    localSpeculationDisabledUntil = now + params.localSuppressMs;
    falseCandidateTimes = [];
    log(`本地候选连续误触发，${Math.round(params.localSuppressMs / 1000)}秒内改用云端 speech_started 打断`, 'warn');
  }
}

function holdPlayback(source = 'server', preserveAudio = true) {
  if (!playerNode || playerHeld) return;
  playerHeld = true;
  candidateActive = true;
  if (!candidateRequestedAt) candidateRequestedAt = performance.now();

  // 第一层：直接在 WebAudio 输出节点 3ms 内拉到 0。即使 Worklet 消息
  // 还在排队，也不会继续听见“小声 TTS”。
  rampOutputGain(0, params.fadeOutMs);

  // 第二层：冻结 Worklet 播放头。误判时可以从同一采样点恢复。
  const requestId = `hold-${++holdRequestId}`;
  playerNode.port.postMessage({
    type: 'hold',
    requestId,
    fadeMs: 0,
    source,
    preserveAudio,
  });
  $('turnState').textContent = 'HOLDING';
}

function resumePlayback(reason = '') {
  if (!playerNode) return;
  const requestId = `resume-${holdRequestId}`;
  playerHeld = false;
  registerFalseCandidate(reason);
  resetLocalCandidateState();
  playerNode.port.postMessage({
    type: 'resume',
    requestId,
    fadeMs: 0,
  });
  rampOutputGain(1, params.fadeInMs);
  localCandidateArmedAt = performance.now() + Math.min(250, params.localArmDelayMs);
  $('gainValue').textContent = '恢复中';
  $('turnState').textContent = `REJECTED → RESUME${reason ? ` (${reason})` : ''}`;
}

function clearPlayback({ notifyServer = false, reason = '', candidateMetric = false } = {}) {
  if (!playerNode) return;
  // 先在音频图上硬静音，再清 Worklet 队列。设备自身已提交的极短尾音
  // 无法追回，但不会再出现几百毫秒的小声续播。
  rampOutputGain(0, Math.min(2, params.fadeOutMs));
  const requestId = `clear-${++clearRequestId}`;
  pendingClearRequestedAt = performance.now();
  playerNode.port.postMessage({ type: 'clear', requestId });
  playbackActive = false;
  playerHeld = false;
  const candidateAt = candidateRequestedAt;
  resetLocalCandidateState();
  candidateRequestedAt = candidateAt;
  sourceFinished = false;
  $('gainValue').textContent = '静音';
  if (notifyServer) {
    // ack 在 Worklet 的 cleared 事件里发送，确保量到的是真正音频线程清空。
    window.__pendingClearMeta = { reason, candidateMetric };
  }
}

class StreamingPCM16Resampler {
  constructor(sourceRate, targetRate) {
    this.sourceRate = sourceRate;
    this.targetRate = targetRate;
    this.step = sourceRate / targetRate;
    this.sourceCount = 0;
    this.nextPosition = 0;
    this.lastSample = 0;
    this.hasLastSample = false;
  }

  push(arrayBuffer) {
    const src16 = new Int16Array(arrayBuffer);
    if (!src16.length) return new Float32Array(0);
    const src = new Float32Array(src16.length);
    for (let i = 0; i < src16.length; i++) src[i] = src16[i] / 32768;

    const startIndex = this.sourceCount;
    const endIndex = startIndex + src.length - 1;
    const baseIndex = this.hasLastSample ? startIndex - 1 : startIndex;
    const data = new Float32Array(src.length + (this.hasLastSample ? 1 : 0));
    if (this.hasLastSample) {
      data[0] = this.lastSample;
      data.set(src, 1);
    } else {
      data.set(src, 0);
    }

    // 输出位置使用绝对源采样坐标。这样无论服务端如何切 binary frame，
    // 小数相位都不会在帧边界归零，分包与整包重采样结果完全一致。
    const estimated = Math.max(0, Math.floor((endIndex - this.nextPosition) / this.step) + 1);
    const out = new Float32Array(estimated);
    let count = 0;
    const epsilon = 1e-9;
    while (this.nextPosition <= endIndex + epsilon) {
      const local = this.nextPosition - baseIndex;
      const i0 = Math.max(0, Math.min(Math.floor(local + epsilon), data.length - 1));
      const i1 = Math.min(i0 + 1, data.length - 1);
      const frac = Math.max(0, Math.min(1, local - Math.floor(local + epsilon)));
      out[count++] = data[i0] * (1 - frac) + data[i1] * frac;
      this.nextPosition += this.step;
    }

    this.sourceCount += src.length;
    this.lastSample = src[src.length - 1];
    this.hasLastSample = true;
    return count === out.length ? out : out.slice(0, count);
  }

  flush() {
    // push() 已输出到最后一个可插值位置；不额外复制尾采样，避免分包数量
    // 不同导致多出一个样本。
    return new Float32Array(0);
  }
}

function pushAudio(arrayBuffer) {
  if (!playerNode || !audioContext) return;
  if (!playbackResampler) {
    playbackResampler = new StreamingPCM16Resampler(24000, audioContext.sampleRate);
  }
  const floatData = playbackResampler.push(arrayBuffer);
  if (floatData.length) {
    playerNode.port.postMessage({ type: 'push', buffer: floatData.buffer }, [floatData.buffer]);
  }
  playbackActive = true;
}

function maybeSendDrained(report) {
  if (
    sourceFinished &&
    !report.active &&
    !report.held &&
    currentGeneration > 0 &&
    drainedSentGeneration !== currentGeneration
  ) {
    drainedSentGeneration = currentGeneration;
    playbackActive = false;
    sendJson({
      type: 'client.playback.drained',
      generation: currentGeneration,
      played_ms: report.playedMs || 0,
    });
    setStatus('正在监听', 'ready');
    $('turnState').textContent = 'IDLE';
  }
}

function onPlayerMessage(data) {
  if (data.type === 'buffer') {
    $('bufferMs').textContent = data.bufferMs.toFixed(0);
    playbackActive = Boolean(data.active || data.held);
    playerHeld = Boolean(data.held);
    if (!playerHeld) $('gainValue').textContent = currentGain <= 0.001 ? '静音' : `${Math.round(currentGain * 100)}%`;
    maybeSendDrained(data);
    return;
  }

  if (data.type === 'held') {
    playerHeld = true;
    $('gainValue').textContent = '静音';
    $('turnState').textContent = 'HELD · 等待 smart_turn';
    const latency = candidateRequestedAt ? performance.now() - candidateRequestedAt : 0;
    sendJson({
      type: 'client.playback.held',
      generation: data.generation,
      candidate_to_silence_ms: latency,
      played_ms: data.playedMs || 0,
    });
    return;
  }

  if (data.type === 'resumed') {
    playerHeld = false;
    $('gainValue').textContent = '恢复中';
    sendJson({
      type: 'client.playback.resumed',
      generation: data.generation,
      played_ms: data.playedMs || 0,
    });
    return;
  }

  if (data.type === 'cleared') {
    const latency = pendingClearRequestedAt ? performance.now() - pendingClearRequestedAt : 0;
    const meta = window.__pendingClearMeta || {};
    sendJson({
      type: 'client.playback.cleared',
      generation: data.generation,
      confirm_to_clear_ms: latency,
      played_ms: data.playedMs || 0,
      reason: meta.reason || '',
    });
    if (meta.candidateMetric && candidateRequestedAt) {
      sendJson({
        type: 'client.playback.held',
        generation: data.generation,
        candidate_to_silence_ms: performance.now() - candidateRequestedAt,
        played_ms: data.playedMs || 0,
      });
    }
    window.__pendingClearMeta = null;
    pendingClearRequestedAt = 0;
    candidateRequestedAt = 0;
    $('bufferMs').textContent = '0';
    return;
  }

  if (data.type === 'hold_trim') {
    // Hold 时使用滚动短缓存，主动丢弃陈旧 TTS，不再把 1 秒以上旧音频
    // 堆在静音后面。避免日志刷屏，只偶尔提示一次。
    const now = performance.now();
    if (now - lastHoldTrimLogAt > 2000) {
      lastHoldTrimLogAt = now;
      log(`推测性静音期间仅保留最近 ${params.holdQueueMs}ms 音频`, 'warn');
    }
    return;
  }

  if (data.type === 'buffer_ready') {
    $('turnState').textContent = `PLAYING · 预缓冲 ${Math.round(data.bufferMs || 0)}ms`;
    return;
  }

  if (data.type === 'underflow') {
    underflowCount += 1;
    if ($('mUnderflow')) $('mUnderflow').textContent = String(underflowCount);
    const now = performance.now();
    if (now - lastUnderflowLogAt > 1200) {
      lastUnderflowLogAt = now;
      log('播放器检测到网络抖动，正在短暂重缓冲，避免碎片化卡顿', 'warn');
    }
    $('turnState').textContent = 'REBUFFERING';
    return;
  }

  if (data.type === 'rebuffered') {
    $('turnState').textContent = `PLAYING · 已恢复（${Math.round(data.bufferMs || 0)}ms）`;
    return;
  }

  if (data.type === 'hard_overflow') {
    log(`播放器灾难保护缓冲已满，丢弃了 ${(data.droppedSamples / audioContext.sampleRate * 1000).toFixed(0)}ms 音频；请提高 PLAYBACK_MAX_QUEUE_MS`, 'warn');
  }
}

function triggerLocalCandidate(dbfs, snr) {
  if (candidateActive || !playbackActive) return;
  if (performance.now() < Math.max(localSpeculationDisabledUntil, localCandidateArmedAt)) return;
  candidateActive = true;
  candidateReleaseFrames = 0;
  candidateReleaseSent = false;
  candidateRequestedAt = performance.now();

  if (bargeMode === 'hybrid' || bargeMode === 'semantic') {
    holdPlayback('local');
  } else if (bargeMode === 'hard') {
    clearPlayback({ notifyServer: true, reason: 'local_immediate', candidateMetric: true });
    $('turnState').textContent = 'LOCAL HARD CUT';
  } else {
    setPlayerGain(params.duckGain);
    $('turnState').textContent = 'DUCKED';
  }

  sendJson({ type: 'local.barge_candidate', dbfs, snr });
  log(`本地候选插话：${dbfs.toFixed(1)} dBFS / SNR ${snr.toFixed(1)} dB`, 'warn');

  clearTimeout(candidateTimer);
  candidateTimer = setTimeout(() => {
    if (candidateActive) sendJson({ type: 'local.candidate_timeout' });
  }, params.localTimeoutMs);
}

function onMicLevel(dbfs) {
  $('dbfs').textContent = dbfs.toFixed(1);
  if (!candidateActive && dbfs < noiseFloor + 8) {
    noiseFloor = noiseFloor * 0.985 + dbfs * 0.015;
  }
  const snr = dbfs - noiseFloor;
  $('noiseFloor').textContent = noiseFloor.toFixed(1);
  $('snr').textContent = snr.toFixed(1);

  if (!playbackActive) {
    candidateFrames = 0;
    candidateReleaseFrames = 0;
    return;
  }

  // 默认 qwen/cloud-hard 模式只展示本地声级，不允许本地阈值改变播放。
  // 真正的暂停/打断只接受 Qwen-Audio 的 speech_started 与 ASR 事件。
  if (bargeMode === 'qwen' || bargeMode === 'cloud-hard') {
    candidateFrames = 0;
    candidateReleaseFrames = 0;
    return;
  }

  const likelySpeech = dbfs >= params.absoluteDbfs && snr >= params.minSnr;

  if (candidateActive) {
    if (likelySpeech) {
      candidateReleaseFrames = 0;
    } else {
      candidateReleaseFrames += 1;
      if (
        !candidateReleaseSent &&
        !cloudSpeechActive &&
        candidateReleaseFrames >= params.localReleaseFrames
      ) {
        candidateReleaseSent = true;
        sendJson({ type: 'local.barge_release' });
      }
    }
    return;
  }

  if (performance.now() < Math.max(localSpeculationDisabledUntil, localCandidateArmedAt)) {
    candidateFrames = 0;
    return;
  }
  candidateFrames = likelySpeech ? candidateFrames + 1 : Math.max(0, candidateFrames - 1);
  if (candidateFrames >= params.candidateFrames) triggerLocalCandidate(dbfs, snr);
}

async function initAudio() {
  // Chrome/macOS 通常可固定到 48kHz；不支持时浏览器会使用设备采样率，
  // 后续由流式有状态重采样器处理。
  try {
    audioContext = new AudioContext({ latencyHint: 'interactive', sampleRate: 48000 });
  } catch (_err) {
    audioContext = new AudioContext({ latencyHint: 'interactive' });
  }
  await audioContext.audioWorklet.addModule('/static/capture-worklet.js?v=1.7.0');
  await audioContext.audioWorklet.addModule('/static/player-worklet.js?v=1.7.0');
  await audioContext.resume();

  playerNode = new AudioWorkletNode(audioContext, 'pcm-player-processor', {
    outputChannelCount: [1],
  });
  outputGainNode = audioContext.createGain();
  outputGainNode.gain.value = 1;
  playerNode.connect(outputGainNode).connect(audioContext.destination);
  playerNode.port.onmessage = (e) => onPlayerMessage(e.data);

  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: false,
      channelCount: 1,
    },
  });
  const source = audioContext.createMediaStreamSource(micStream);
  captureNode = new AudioWorkletNode(audioContext, 'capture-processor');
  const silent = audioContext.createGain();
  silent.gain.value = 0;
  source.connect(captureNode).connect(silent).connect(audioContext.destination);
  captureNode.port.onmessage = (e) => {
    if (e.data.type === 'audio') {
      if (ws?.readyState === WebSocket.OPEN) ws.send(e.data.buffer);
    } else if (e.data.type === 'level') {
      onMicLevel(e.data.dbfs);
    }
  };
}

function handleServerEvent(event) {
  switch (event.type) {
    case 'server.config':
      $('models').textContent = `${event.models.sensor} → ${event.models.llm} → ${event.models.tts}`;
      if (event.barge) {
        bargeMode = event.barge.mode || bargeMode;
        params.absoluteDbfs = Number(event.barge.absolute_dbfs ?? params.absoluteDbfs);
        params.minSnr = Number(event.barge.min_snr_db ?? params.minSnr);
        params.candidateFrames = Number(event.barge.candidate_frames ?? params.candidateFrames);
        params.localTimeoutMs = Number(event.barge.local_timeout_ms ?? params.localTimeoutMs);
        params.localArmDelayMs = Number(event.barge.local_arm_delay_ms ?? params.localArmDelayMs);
        params.localReleaseFrames = Number(event.barge.local_release_frames ?? params.localReleaseFrames);
        params.fadeOutMs = Number(event.barge.fade_out_ms ?? params.fadeOutMs);
        params.fadeInMs = Number(event.barge.fade_in_ms ?? params.fadeInMs);
        params.holdQueueMs = Number(event.barge.hold_queue_ms ?? params.holdQueueMs);
        params.falseCandidateLimit = Number(event.barge.false_candidate_limit ?? params.falseCandidateLimit);
        params.falseCandidateWindowMs = Number(event.barge.false_candidate_window_ms ?? params.falseCandidateWindowMs);
        params.localSuppressMs = Number(event.barge.local_suppress_ms ?? params.localSuppressMs);
        if ($('bargeMode')) $('bargeMode').value = bargeMode;
      }
      if (event.playback) {
        params.maxQueueMs = Number(event.playback.max_queue_ms ?? params.maxQueueMs);
        params.prebufferMs = Number(event.playback.prebuffer_ms ?? params.prebufferMs);
        params.rebufferMs = Number(event.playback.rebuffer_ms ?? params.rebufferMs);
      }
      break;
    case 'server.ready':
      connected = true;
      setStatus('正在监听', 'ready');
      $('startBtn').disabled = true;
      $('stopBtn').disabled = false;
      sendJson({ type: 'client.settings', barge_mode: bargeMode });
      log('云端 ASR/smart_turn、LLM、TTS 已连接', 'ok');
      break;
    case 'sensor.status':
      log(`听觉协处理器：${event.status}`);
      break;
    case 'vad.speech_started':
      cloudSpeechActive = true;
      clearTimeout(candidateTimer);
      setStatus('检测到你在说话', 'listen');
      $('turnState').textContent = playerHeld ? 'HELD · SPEECH_STARTED' : 'SPEECH_STARTED';
      break;
    case 'vad.speech_stopped':
      cloudSpeechActive = false;
      $('turnState').textContent = event.reason === 'turn_invalid' ? 'TURN_INVALID' : (playerHeld ? 'HELD · ENDPOINT' : 'SPEECH_STOPPED');
      break;
    case 'asr.partial':
      $('asrPartial').textContent = event.preview || '…';
      break;
    case 'asr.final':
      $('asrFinal').textContent = event.text;
      $('asrPartial').textContent = '';
      assistantText = '';
      $('assistant').textContent = '';
      $('turnState').textContent = 'VALID_TURN';
      setStatus('主模型思考中', 'think');
      log(`你：${event.text}`);
      break;
    case 'asr.ambient_partial':
      $('ambient').textContent = event.preview || '…';
      break;
    case 'asr.ambient_final':
      $('ambient').textContent = event.text || '环境音/附和';
      log(`smart_turn 忽略：${event.text || '无语义声音'}`);
      break;
    case 'asr.ambient_promoted':
      $('ambient').textContent = `${event.text}（已提升为有效轮次）`;
      $('asrFinal').textContent = event.text;
      $('asrPartial').textContent = '';
      assistantText = '';
      $('assistant').textContent = '';
      $('turnState').textContent = 'VALID_TURN · AMBIENT FALLBACK';
      setStatus('主模型思考中', 'think');
      log(`你（smart_turn 兜底）：${event.text}`, 'warn');
      break;
    case 'llm.delta':
      assistantText += event.text;
      $('assistant').textContent = assistantText;
      break;
    case 'llm.done':
      log(`AI：${event.text}`, 'ai');
      break;
    case 'playback.hold':
      holdPlayback(event.source || 'server', event.preserve_audio !== false);
      break;
    case 'playback.duck':
      candidateActive = true;
      setPlayerGain(event.gain ?? params.duckGain);
      $('turnState').textContent = 'DUCKED';
      break;
    case 'playback.resume':
      resumePlayback(event.reason || 'smart_turn');
      break;
    case 'playback.restore':
      resetLocalCandidateState();
      setPlayerGain(1);
      $('turnState').textContent = 'REJECTED → PLAYING';
      break;
    case 'playback.clear':
      if (event.generation) interruptedGenerations.add(Number(event.generation));
      clearPlayback({ notifyServer: true, reason: event.reason || '' });
      $('turnState').textContent = `INTERRUPTED (${event.reason})`;
      setStatus('已打断，继续听你说', 'listen');
      break;
    case 'playback.started':
      currentGeneration = Number(event.generation || 0);
      underflowCount = 0;
      if ($('mUnderflow')) $('mUnderflow').textContent = '0';
      drainedSentGeneration = 0;
      sourceFinished = false;
      playbackActive = true;
      playbackResampler = new StreamingPCM16Resampler(24000, audioContext.sampleRate);
      // TTS 起播瞬间最容易被扬声器残留回声误判；短暂不启用本地推测
      // 静音，但云端 speech_started 仍随时可以硬切。
      localCandidateArmedAt = performance.now() + params.localArmDelayMs;
      playerNode?.port.postMessage({
        type: 'generation',
        generation: currentGeneration,
        maxQueueMs: params.maxQueueMs,
        prebufferMs: params.prebufferMs,
        rebufferMs: params.rebufferMs,
        holdQueueMs: params.holdQueueMs,
      });
      if (!candidateActive && !playerHeld) {
        rampOutputGain(1, 5);
        setStatus('AI 正在说话，可随时插话', 'speak');
        $('turnState').textContent = 'PLAYING';
      }
      break;
    case 'playback.source_finished':
      if (interruptedGenerations.has(Number(event.generation || 0))) break;
      sourceFinished = true;
      if (playbackResampler) {
        const tail = playbackResampler.flush();
        if (tail.length) {
          playerNode?.port.postMessage({ type: 'push', buffer: tail.buffer }, [tail.buffer]);
        }
      }
      playerNode?.port.postMessage({ type: 'source_finished', generation: currentGeneration });
      setStatus(playerHeld ? '已静音，等待语义裁决' : '播放尾音，可继续插话', 'speak');
      if (!playerHeld) $('turnState').textContent = 'DRAINING';
      break;
    case 'playback.drained':
      playbackActive = false;
      sourceFinished = false;
      setStatus('正在监听', 'ready');
      $('turnState').textContent = 'IDLE';
      break;
    case 'metric':
      updateMetric(event.name, event.value);
      break;
    case 'tts.throughput':
      break;
    case 'tts.status': {
      const mode = event.instruction === false ? '基础参数（instruction 已降级）' : '情感指令开启';
      if (event.status === 'probe_trying') {
        log(`TTS 预检：${event.endpoint}/${event.host}，${mode}`);
      } else if (event.status === 'probe_failed') {
        log(`TTS 预检失败，将自动尝试下一配置：${event.message || ''}`, 'warn');
      } else if (event.status === 'ready') {
        log(`TTS 可用：${event.endpoint}/${event.host}，${mode}`, 'ok');
      }
      break;
    }
    case 'local.sensor_reconnecting':
      setStatus('听觉链路重连中，麦克风帧正在短暂缓存', 'think');
      log(event.message || 'Qwen-Audio 正在自动重连', 'warn');
      break;
    case 'local.sensor_reconnect_failed':
      log(`听觉重连失败，${event.retry_in_sec || 1}秒后重试：${event.message || ''}`, 'warn');
      break;
    case 'local.sensor_reconnected':
      setStatus(playbackActive ? 'AI 正在说话，可随时插话' : '正在监听', playbackActive ? 'speak' : 'ready');
      log('Qwen-Audio 已自动重连并恢复最近上下文', 'ok');
      break;
    case 'local.sensor_warning':
      log(`Qwen-Audio 可恢复警告：${event.message || ''}`, 'warn');
      break;
    case 'local.sensor_heartbeat':
      // 静默内部 response，用于避免线上 180 秒无完成响应 watchdog。
      break;
    case 'local.sensor_heartbeat_skipped':
      break;
    case 'trace':
      log(event.message);
      break;
    case 'server.error':
      setStatus('发生错误', 'error');
      log(`${event.source || 'server'}：${event.message}`, 'error');
      break;
  }
}

function updateMetric(name, value) {
  if (name === 'tts_generation_speed_x') {
    if ($('mTtsSpeed')) $('mTtsSpeed').textContent = `${value}×`;
    return;
  }
  const map = {
    endpoint_to_asr_final_ms: 'mAsr',
    endpoint_to_first_token_ms: 'mToken',
    endpoint_to_first_audio_ms: 'mAudio',
    llm_request_to_first_token_ms: 'mLlm',
    tts_run_to_first_text_ms: 'mTtsTextWait',
    tts_run_to_first_audio_ms: 'mTts',
    tts_text_to_first_audio_ms: 'mTtsText',
    candidate_to_silence_ms: 'mSilence',
    interrupt_confirm_to_clear_ms: 'mInterrupt',
  };
  const id = map[name];
  if (id && $(id)) $(id).textContent = `${value} ms`;
}

async function start() {
  $('startBtn').disabled = true;
  setStatus('初始化麦克风和云连接…', 'think');
  try {
    bargeMode = $('bargeMode')?.value || bargeMode;
    await initAudio();
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/voice`);
    ws.binaryType = 'arraybuffer';
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') handleServerEvent(JSON.parse(e.data));
      else pushAudio(e.data);
    };
    ws.onclose = () => {
      connected = false;
      setStatus('连接已断开', 'error');
      $('startBtn').disabled = false;
      $('stopBtn').disabled = true;
      log('WebSocket 已断开', 'error');
    };
    ws.onerror = () => log('WebSocket 连接失败', 'error');
  } catch (err) {
    setStatus('启动失败', 'error');
    log(err.message || String(err), 'error');
    $('startBtn').disabled = false;
  }
}

function stop() {
  sendJson({ type: 'client.stop' });
  clearPlayback();
  ws?.close();
  micStream?.getTracks().forEach((t) => t.stop());
  audioContext?.close();
  $('startBtn').disabled = false;
  $('stopBtn').disabled = true;
}

$('bargeMode')?.addEventListener('change', (e) => {
  bargeMode = e.target.value;
  sendJson({ type: 'client.settings', barge_mode: bargeMode });
  log(`打断策略：${bargeMode}`);
});
$('startBtn').addEventListener('click', start);
$('stopBtn').addEventListener('click', stop);
$('clearLog').addEventListener('click', () => $('log').replaceChildren());
