class PCMPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0;
    this.samplesQueued = 0;
    this.generation = 0;
    this.playedSamples = 0;

    // 打断增益状态和网络抖动状态分离。旧版把“是否播放”只交给一个状态机，
    // 首包一到就消费；当后续 TTS 包抖动时会反复断粮，听起来像卡顿。
    this.mode = 'playing'; // playing | fading_out | held | fading_in
    this.streamState = 'idle'; // idle | prebuffering | playing | rebuffering | ended
    this.sourceFinished = false;

    this.gain = 1.0;
    this.targetGain = 1.0;
    this.fadeSamplesLeft = 0;
    this.fadeStep = 0;
    this.pendingHoldRequestId = null;
    this.holdPreserveAudio = true;

    // 常规播放绝不能用 1.2 秒队列截断整句话。30 秒只是灾难保护，不是
    // 延迟目标；实际缓冲通常只有数百毫秒到几秒。
    this.maxQueueSamples = Math.round(sampleRate * 30);
    this.holdQueueSamples = Math.round(sampleRate * 0.24);
    this.startBufferSamples = Math.round(sampleRate * 0.18);
    this.rebufferSamples = Math.round(sampleRate * 0.10);

    this.reportCounter = 0;
    this.underrunCount = 0;
    this.underrunReported = false;

    this.port.onmessage = (event) => this.onMessage(event.data || {});
  }

  onMessage(msg) {
    if (msg.type === 'push') {
      const arr = new Float32Array(msg.buffer);
      if (!arr.length) return;

      // Qwen 主导模式下，speech_started 只是“暂定暂停”。必须完整保留
      // 已排队和后续到达的 TTS；若 smart_turn 判 turn_invalid，才能从
      // 原采样点无缺字续播。legacy 本地候选模式仍可选择滚动短缓存。
      if (this.mode === 'held' || this.mode === 'fading_out') {
        if (this.holdPreserveAudio) {
          const room = Math.max(0, this.maxQueueSamples - this.samplesQueued);
          if (room <= 0) {
            this.port.postMessage({ type: 'hard_overflow', droppedSamples: arr.length });
            return;
          }
          const accepted = room >= arr.length ? arr : arr.slice(0, room);
          this.queue.push(accepted);
          this.samplesQueued += accepted.length;
          if (accepted.length < arr.length) {
            this.port.postMessage({
              type: 'hard_overflow',
              droppedSamples: arr.length - accepted.length,
            });
          }
        } else {
          this.queue.push(arr);
          this.samplesQueued += arr.length;
          const dropped = this.trimTo(this.holdQueueSamples);
          if (dropped > 0) {
            this.port.postMessage({ type: 'hold_trim', droppedSamples: dropped });
          }
        }
        return;
      }

      const room = Math.max(0, this.maxQueueSamples - this.samplesQueued);
      if (room <= 0) {
        this.port.postMessage({ type: 'hard_overflow', droppedSamples: arr.length });
        return;
      }
      const accepted = room >= arr.length ? arr : arr.slice(0, room);
      this.queue.push(accepted);
      this.samplesQueued += accepted.length;
      if (accepted.length < arr.length) {
        this.port.postMessage({
          type: 'hard_overflow',
          droppedSamples: arr.length - accepted.length,
        });
      }
      this.maybeStartStream('push');
      return;
    }

    if (msg.type === 'generation') {
      const nextGeneration = Number(msg.generation || 0);
      this.queue = [];
      this.offset = 0;
      this.samplesQueued = 0;
      this.generation = nextGeneration;
      this.playedSamples = 0;
      this.mode = 'playing';
      this.streamState = 'prebuffering';
      this.sourceFinished = false;
      this.gain = 1;
      this.targetGain = 1;
      this.fadeSamplesLeft = 0;
      this.fadeStep = 0;
      this.pendingHoldRequestId = null;
      this.holdPreserveAudio = true;
      this.underrunCount = 0;
      this.underrunReported = false;

      this.maxQueueSamples = Math.max(
        Math.round(sampleRate * 5),
        Math.round(sampleRate * Number(msg.maxQueueMs || 30000) / 1000),
      );
      this.holdQueueSamples = Math.max(
        Math.round(sampleRate * 0.08),
        Math.min(
          Math.round(sampleRate * 0.5),
          Math.round(sampleRate * Number(msg.holdQueueMs || 240) / 1000),
        ),
      );
      this.startBufferSamples = Math.max(
        Math.round(sampleRate * 0.06),
        Math.min(
          Math.round(sampleRate * 0.8),
          Math.round(sampleRate * Number(msg.prebufferMs || 180) / 1000),
        ),
      );
      this.rebufferSamples = Math.max(
        Math.round(sampleRate * 0.04),
        Math.min(
          this.startBufferSamples,
          Math.round(sampleRate * Number(msg.rebufferMs || 100) / 1000),
        ),
      );
      return;
    }

    if (msg.type === 'source_finished') {
      this.sourceFinished = true;
      this.maybeStartStream('source_finished');
      if (this.samplesQueued <= 0 && this.mode !== 'held') {
        this.streamState = 'ended';
      }
      return;
    }

    if (msg.type === 'hold') {
      const requestId = msg.requestId || null;
      this.pendingHoldRequestId = requestId;
      this.holdPreserveAudio = msg.preserveAudio !== false;
      if (!this.holdPreserveAudio) {
        const dropped = this.trimTo(this.holdQueueSamples);
        if (dropped > 0) this.port.postMessage({ type: 'hold_trim', droppedSamples: dropped });
      }
      if (this.mode === 'held') {
        this.port.postMessage(this.snapshot('held', requestId));
        return;
      }
      const fadeMs = Math.max(0, Number(msg.fadeMs || 0));
      const samples = Math.max(1, Math.round(sampleRate * fadeMs / 1000));
      this.mode = 'fading_out';
      this.targetGain = 0;
      this.fadeSamplesLeft = samples;
      this.fadeStep = (0 - this.gain) / samples;
      return;
    }

    if (msg.type === 'resume') {
      const requestId = msg.requestId || null;
      const fadeMs = Math.max(0, Number(msg.fadeMs || 0));
      const samples = Math.max(1, Math.round(sampleRate * fadeMs / 1000));
      this.mode = 'fading_in';
      this.gain = Math.max(0, this.gain);
      this.targetGain = 1;
      this.fadeSamplesLeft = samples;
      this.fadeStep = (1 - this.gain) / samples;
      this.pendingHoldRequestId = null;

      if (this.samplesQueued > 0) {
        this.streamState = 'playing';
      } else if (this.sourceFinished) {
        this.streamState = 'ended';
      } else {
        this.streamState = 'rebuffering';
      }
      this.port.postMessage(this.snapshot('resumed', requestId));
      return;
    }

    if (msg.type === 'clear') {
      const requestId = msg.requestId || null;
      this.queue = [];
      this.offset = 0;
      this.samplesQueued = 0;
      this.mode = 'playing';
      this.streamState = 'ended';
      this.sourceFinished = true;
      this.gain = 1;
      this.targetGain = 1;
      this.fadeSamplesLeft = 0;
      this.fadeStep = 0;
      this.pendingHoldRequestId = null;
      this.holdPreserveAudio = true;
      this.port.postMessage(this.snapshot('cleared', requestId));
      return;
    }

    if (msg.type === 'gain') {
      this.gain = Math.max(0, Math.min(1, Number(msg.value)));
      this.targetGain = this.gain;
      this.mode = 'playing';
      this.fadeSamplesLeft = 0;
      this.fadeStep = 0;
    }
  }

  maybeStartStream(reason) {
    if (this.mode === 'held' || this.mode === 'fading_out') return;
    let threshold = 0;
    if (this.streamState === 'prebuffering') threshold = this.startBufferSamples;
    else if (this.streamState === 'rebuffering') threshold = this.rebufferSamples;
    else return;

    if (this.sourceFinished ? this.samplesQueued > 0 : this.samplesQueued >= threshold) {
      const was = this.streamState;
      this.streamState = 'playing';
      this.underrunReported = false;
      this.port.postMessage({
        type: was === 'prebuffering' ? 'buffer_ready' : 'rebuffered',
        reason,
        generation: this.generation,
        bufferMs: (this.samplesQueued / sampleRate) * 1000,
      });
    }
  }

  snapshot(type, requestId = null) {
    return {
      type,
      requestId,
      generation: this.generation,
      bufferMs: (this.samplesQueued / sampleRate) * 1000,
      playedMs: (this.playedSamples / sampleRate) * 1000,
      held: this.mode === 'held',
      streamState: this.streamState,
    };
  }

  dropOldest(count) {
    let remaining = Math.max(0, Math.floor(count));
    const requested = remaining;
    while (remaining > 0 && this.queue.length) {
      const current = this.queue[0];
      const available = current.length - this.offset;
      if (remaining < available) {
        this.offset += remaining;
        this.samplesQueued -= remaining;
        remaining = 0;
      } else {
        remaining -= available;
        this.samplesQueued -= available;
        this.queue.shift();
        this.offset = 0;
      }
    }
    this.samplesQueued = Math.max(0, this.samplesQueued);
    return requested - remaining;
  }

  trimTo(limitSamples) {
    const excess = Math.max(0, this.samplesQueued - Math.max(0, limitSamples));
    return excess > 0 ? this.dropOldest(excess) : 0;
  }

  consumeOne() {
    if (!this.queue.length) return null;
    const current = this.queue[0];
    const sample = current[this.offset];
    this.offset += 1;
    this.samplesQueued -= 1;
    this.playedSamples += 1;
    if (this.offset >= current.length) {
      this.queue.shift();
      this.offset = 0;
    }
    return sample;
  }

  advanceFade() {
    if (this.fadeSamplesLeft <= 0) return;
    this.gain += this.fadeStep;
    this.fadeSamplesLeft -= 1;
    if (this.fadeSamplesLeft <= 0) {
      this.gain = this.targetGain;
      if (this.targetGain === 0) {
        this.mode = 'held';
        if (!this.holdPreserveAudio) {
          const dropped = this.trimTo(this.holdQueueSamples);
          if (dropped > 0) this.port.postMessage({ type: 'hold_trim', droppedSamples: dropped });
        }
        this.port.postMessage(this.snapshot('held', this.pendingHoldRequestId));
      } else {
        this.mode = 'playing';
      }
    }
  }

  enterRebuffering() {
    if (this.sourceFinished) {
      this.streamState = 'ended';
      return;
    }
    if (this.streamState !== 'rebuffering') {
      this.streamState = 'rebuffering';
      this.underrunCount += 1;
      if (!this.underrunReported) {
        this.underrunReported = true;
        this.port.postMessage({
          type: 'underflow',
          generation: this.generation,
          count: this.underrunCount,
        });
      }
    }
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    out.fill(0);

    this.maybeStartStream('process');

    for (let i = 0; i < out.length; i++) {
      if (this.mode === 'held') break;

      let sample = null;
      if (this.streamState === 'playing') {
        sample = this.consumeOne();
        if (sample === null) {
          this.enterRebuffering();
        }
      }

      if (sample !== null) out[i] = sample * this.gain;
      if (this.mode === 'fading_out' || this.mode === 'fading_in') this.advanceFade();
    }

    if (this.sourceFinished && this.samplesQueued <= 0 && this.mode !== 'held') {
      this.streamState = 'ended';
    }

    if (++this.reportCounter % 20 === 0) {
      const logicallyActive =
        this.mode === 'held' ||
        this.samplesQueued > 0 ||
        (!this.sourceFinished && ['prebuffering', 'playing', 'rebuffering'].includes(this.streamState));
      this.port.postMessage({
        type: 'buffer',
        generation: this.generation,
        bufferMs: (this.samplesQueued / sampleRate) * 1000,
        playedMs: (this.playedSamples / sampleRate) * 1000,
        active: logicallyActive,
        held: this.mode === 'held' || this.mode === 'fading_out',
        gain: this.gain,
        streamState: this.streamState,
        sourceFinished: this.sourceFinished,
        underrunCount: this.underrunCount,
      });
    }
    return true;
  }
}

registerProcessor('pcm-player-processor', PCMPlayerProcessor);
