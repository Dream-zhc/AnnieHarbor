from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from config import Settings
from llm_client import ExternalLLM
from qwen_audio_sensor import QwenAudioSensor
from tts_client import QwenAudioTTS
from utils import PhraseChunker, cancel_and_wait, compact_json, meaningful_text, now_ms

logger = logging.getLogger(__name__)


class VoiceSession:
    """可替换 LLM/TTS 的实时语音会话。

    v1.7 默认让 Qwen-Audio smart_turn 主导打断。浏览器本地声级仅用于
    观测，不参与状态裁决。云端 speech_started 到达时先暂停并完整保存
    TTS 队列；turn_invalid/ambient 时原位续播，有效 ASR 前缀或 final 时
    才清空播放器并取消外部 LLM/TTS。这样误判不会再造成缺字或截断。
    """

    def __init__(self, browser: WebSocket, cfg: Settings) -> None:
        self.browser = browser
        self.cfg = cfg
        self.send_lock = asyncio.Lock()
        self.sensor = QwenAudioSensor(cfg, self.on_sensor_event)
        self.tts = QwenAudioTTS(cfg, self.on_tts_audio, self.on_tts_event)
        self.llm = ExternalLLM(cfg)

        self.generation = 0
        self.llm_task: asyncio.Task[None] | None = None
        self.history: list[dict[str, str]] = []

        # “TTS 任务已结束”不等于“浏览器已经播完”。ai_speaking 由播放器
        # 首音频/清空/drained 事件驱动，避免尾部缓冲期间无法正确打断。
        self.ai_speaking = False
        self.playback_generation: int | None = None
        self.tts_source_active = False

        self.barge_mode = cfg.barge_mode
        self.interrupt_state = "idle"  # idle | holding | confirmed
        self.interrupt_sources: set[str] = set()
        self.hold_started_ms: float | None = None
        self.hold_watchdog_task: asyncio.Task[None] | None = None
        self.cloud_speech_active = False
        self.last_asr_preview = ""

        self.current_user_item: str | None = None
        self.handled_items: set[str] = set()
        self.turn_clock: dict[str, float] = {}

        # 不在 LLM done 时立刻把完整回答写入上下文；只有浏览器真正播完才提交。
        # 若中途被打断，写入“回答被打断”，避免模型误以为用户听完了全文。
        self.turn_users: dict[int, str] = {}
        self.turn_answers: dict[int, str] = {}
        self.committed_generations: set[int] = set()

    async def run(self) -> None:
        self.cfg.validate_for_cloud()
        await self.send_event(
            {
                "type": "server.config",
                "models": {
                    "sensor": self.cfg.audio_model,
                    "llm": self.cfg.llm_model,
                    "tts": self.cfg.tts_model,
                    "voice": self.cfg.tts_voice,
                },
                "barge": {
                    "mode": self.barge_mode,
                    "absolute_dbfs": self.cfg.barge_absolute_dbfs,
                    "min_snr_db": self.cfg.barge_min_snr_db,
                    "candidate_frames": self.cfg.barge_candidate_frames,
                    "local_timeout_ms": self.cfg.barge_local_timeout_ms,
                    "local_arm_delay_ms": self.cfg.barge_local_arm_delay_ms,
                    "local_release_frames": self.cfg.barge_local_release_frames,
                    "fade_out_ms": self.cfg.barge_fade_out_ms,
                    "fade_in_ms": self.cfg.barge_fade_in_ms,
                    "hold_queue_ms": self.cfg.barge_hold_queue_ms,
                    "false_candidate_limit": self.cfg.barge_false_candidate_limit,
                    "false_candidate_window_ms": self.cfg.barge_false_candidate_window_ms,
                    "local_suppress_ms": self.cfg.barge_local_suppress_ms,
                },
                "playback": {
                    "max_queue_ms": self.cfg.playback_max_queue_ms,
                    "prebuffer_ms": self.cfg.playback_prebuffer_ms,
                    "rebuffer_ms": self.cfg.playback_rebuffer_ms,
                },
            }
        )
        try:
            await asyncio.gather(self.sensor.connect(), self.tts.connect())
            await self.send_event({"type": "server.ready"})

            while True:
                message = await self.browser.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    await self.sensor.send_audio(message["bytes"])
                elif message.get("text"):
                    await self.on_browser_event(message["text"])
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        await cancel_and_wait(self.hold_watchdog_task)
        await cancel_and_wait(self.llm_task)
        await self.tts.close()
        await self.sensor.close()
        await self.llm.close()

    async def on_browser_event(self, raw: str) -> None:
        import json

        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        event_type = event.get("type")

        if event_type == "client.settings":
            mode = str(event.get("barge_mode", "")).lower()
            if mode in {"qwen", "cloud-hard", "hybrid", "semantic", "hard", "duck"}:
                self.barge_mode = mode
                await self.send_event(
                    {"type": "trace", "message": f"打断策略已切换：{mode}"}
                )
            return

        if event_type == "local.barge_candidate" and self.ai_speaking:
            # qwen/cloud-hard 模式下，本地声级只展示，不参与打断。
            if self.barge_mode in {"hybrid", "semantic", "hard", "duck"}:
                await self.begin_interrupt_candidate("local", preserve_audio=False)
            return

        if event_type in {"local.barge_release", "local.candidate_timeout"}:
            # v1.5 不再只靠固定 500ms 定时器。浏览器会在本地声能真正释放
            # 后主动上报 local.barge_release；timeout 仅作为 AEC 残留/异常噪音
            # 导致候选一直不释放时的安全阀。
            if (
                self.interrupt_state == "holding"
                and "local" in self.interrupt_sources
                and "smart_turn" not in self.interrupt_sources
                and not self.cloud_speech_active
                and not meaningful_text(self.last_asr_preview, 1)
            ):
                reason = "local_release" if event_type == "local.barge_release" else "local_timeout"
                await self.reject_interrupt(reason)
            return

        if event_type == "client.playback.held":
            value = event.get("candidate_to_silence_ms")
            if isinstance(value, (int, float)):
                await self.send_event(
                    {
                        "type": "metric",
                        "name": "candidate_to_silence_ms",
                        "value": round(float(value), 1),
                    }
                )
            return

        if event_type == "client.playback.cleared":
            self.ai_speaking = False
            value = event.get("confirm_to_clear_ms")
            if isinstance(value, (int, float)):
                await self.send_event(
                    {
                        "type": "metric",
                        "name": "interrupt_confirm_to_clear_ms",
                        "value": round(float(value), 1),
                    }
                )
            return

        if event_type == "client.playback.drained":
            generation = int(event.get("generation") or 0)
            if generation == self.playback_generation:
                self.ai_speaking = False
                self.playback_generation = None
                await self.commit_turn(generation, interrupted=False)
                await self.send_event({"type": "playback.drained", "generation": generation})
            return

        if event_type == "client.stop":
            await self.confirm_interrupt("manual_stop")
            return

    async def on_sensor_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")

        if event_type in {"session.created", "session.updated"}:
            await self.send_event({"type": "sensor.status", "status": event_type})
            return

        if event_type == "input_audio_buffer.speech_started":
            self.current_user_item = event.get("item_id")
            self.turn_clock = {"speech_started": now_ms()}
            self.cloud_speech_active = True
            self.last_asr_preview = ""
            await self.send_event(
                {"type": "vad.speech_started", "item_id": self.current_user_item}
            )
            if self.ai_speaking:
                if self.barge_mode in {"cloud-hard", "hard", "hybrid"}:
                    # 最快/legacy hybrid：Qwen 云端 VAD 起声即永久打断。
                    await self.confirm_interrupt("cloud_speech_started")
                elif self.barge_mode == "qwen":
                    # 默认模式：只信任 Qwen 起声事件，但先暂停不丢音频。
                    # smart_turn 判无效时原位恢复；有效 ASR 出现后再硬切。
                    await self.begin_interrupt_candidate(
                        "smart_turn", preserve_audio=True
                    )
                else:
                    # legacy A/B 模式。
                    await self.begin_interrupt_candidate(
                        "smart_turn", preserve_audio=self.barge_mode == "semantic"
                    )
            return

        if event_type == "conversation.item.input_audio_transcription.delta":
            text = str(event.get("text", ""))
            stash = str(event.get("stash", ""))
            preview = text + stash
            self.last_asr_preview = preview
            await self.send_event(
                {
                    "type": "asr.partial",
                    "text": text,
                    "stash": stash,
                    "preview": preview,
                }
            )
            if self.interrupt_state == "holding" or self.ai_speaking:
                if self._hard_stop(preview):
                    await self.confirm_interrupt("hard_stop_word")
                elif meaningful_text(text, self.cfg.meaningful_stable_chars):
                    await self.confirm_interrupt("stable_asr_prefix")
            return

        if event_type == "conversation.item.ambient_audio_transcription.delta":
            await self.send_event(
                {
                    "type": "asr.ambient_partial",
                    "preview": str(event.get("text", "")) + str(event.get("stash", "")),
                }
            )
            return

        if event_type == "conversation.item.ambient_audio_transcription.completed":
            self.cloud_speech_active = False
            transcript = str(event.get("transcript", "")).strip()
            if self._promote_ambient(transcript):
                # smart_turn 偶尔会把“还没吧”“不对，我说的是……”这类短纠正
                # 放进 ambient。对独立 LLM 级联而言，盲目忽略会直接漏掉用户。
                await self.send_event(
                    {
                        "type": "asr.ambient_promoted",
                        "text": transcript,
                        "reason": "meaningful_ambient",
                    }
                )
                if self.interrupt_state == "holding" or self.ai_speaking or (
                    self.llm_task is not None and not self.llm_task.done()
                ):
                    await self.confirm_interrupt("meaningful_ambient")
                await self.start_external_turn(transcript)
            else:
                await self.send_event(
                    {"type": "asr.ambient_final", "text": transcript}
                )
                if self.interrupt_state == "holding":
                    await self.reject_interrupt("ambient_audio")
            return

        if event_type == "input_audio_buffer.speech_stopped":
            self.turn_clock["speech_stopped"] = now_ms()
            self.cloud_speech_active = False
            reason = event.get("reason")
            await self.send_event({"type": "vad.speech_stopped", "reason": reason})
            if reason == "turn_invalid" and self.interrupt_state == "holding":
                await self.reject_interrupt("turn_invalid")
            return

        if event_type == "conversation.item.input_audio_transcription.completed":
            item_id = str(event.get("item_id", ""))
            transcript = str(event.get("transcript", "")).strip()
            if not transcript or item_id in self.handled_items:
                return
            self.handled_items.add(item_id)
            self.turn_clock["asr_final"] = now_ms()
            await self.send_event({"type": "asr.final", "text": transcript})
            await self.send_turn_metrics()

            if not meaningful_text(transcript, 1):
                if self.interrupt_state == "holding":
                    await self.reject_interrupt("non_meaningful_final")
                return

            if self.interrupt_state == "holding" or self.ai_speaking or (
                self.llm_task is not None and not self.llm_task.done()
            ):
                await self.confirm_interrupt("valid_smart_turn")
            await self.start_external_turn(transcript)
            return

        if event_type in {
            "local.sensor_reconnecting",
            "local.sensor_reconnect_failed",
            "local.sensor_reconnected",
            "local.sensor_warning",
            "local.sensor_heartbeat",
            "local.sensor_heartbeat_skipped",
        }:
            await self.send_event(event)
            return

        if event_type == "local.cloud_error" or event_type == "error":
            await self.send_event(
                {
                    "type": "server.error",
                    "source": "qwen_audio",
                    "message": (event.get("error") or {}).get("message")
                    if isinstance(event.get("error"), dict)
                    else event.get("message", str(event)),
                }
            )
            return

        if event_type in {"response.created", "response.done", "conversation.item.deleted"}:
            await self.send_event({"type": "trace", "message": f"Qwen-Audio: {event_type}"})

    async def begin_interrupt_candidate(
        self, source: str, *, preserve_audio: bool = False
    ) -> None:
        if not self.ai_speaking:
            return
        self.interrupt_sources.add(source)

        if self.barge_mode in {"hard", "cloud-hard"}:
            await self.confirm_interrupt(f"{source}_immediate")
            return

        if self.interrupt_state == "holding":
            return

        self.interrupt_state = "holding"
        self.hold_started_ms = now_ms()
        if self.barge_mode == "duck":
            await self.send_event(
                {"type": "playback.duck", "gain": 0.18, "source": source}
            )
        else:
            await self.send_event(
                {
                    "type": "playback.hold",
                    "source": source,
                    "fade_out_ms": self.cfg.barge_fade_out_ms,
                    "preserve_audio": preserve_audio,
                }
            )
        policy = "完整保留音频" if preserve_audio else "legacy 短缓存"
        await self.send_event(
            {
                "type": "trace",
                "message": f"候选插话：{source}，暂停播放（{policy}），等待 smart_turn 裁决",
            }
        )
        await cancel_and_wait(self.hold_watchdog_task)
        timeout_ms = 10000 if source == "smart_turn" else self.cfg.barge_hold_watchdog_ms
        self.hold_watchdog_task = asyncio.create_task(
            self._hold_watchdog(timeout_ms), name="barge-hold-watchdog"
        )

    async def _hold_watchdog(self, timeout_ms: int) -> None:
        try:
            await asyncio.sleep(timeout_ms / 1000)
            if (
                self.interrupt_state == "holding"
                and not self.cloud_speech_active
                and not meaningful_text(self.last_asr_preview, 1)
            ):
                await self.reject_interrupt("hold_watchdog")
        except asyncio.CancelledError:
            raise

    async def start_external_turn(self, transcript: str) -> None:
        await cancel_and_wait(self.llm_task)
        self.generation += 1
        generation = self.generation
        self.turn_users[generation] = transcript
        self.interrupt_state = "idle"
        self.interrupt_sources.clear()
        self.hold_started_ms = None
        self.last_asr_preview = ""
        self.turn_clock["llm_request"] = now_ms()
        self.llm_task = asyncio.create_task(
            self._generate_and_speak(transcript, generation),
            name=f"external-turn-{generation}",
        )

    async def _generate_and_speak(self, transcript: str, generation: int) -> None:
        chunker = PhraseChunker(self.cfg.first_tts_chars, self.cfg.next_tts_chars)
        answer_parts: list[str] = []
        messages = [
            {"role": "system", "content": self.cfg.system_prompt},
            *self.history[-12:],
            {"role": "user", "content": transcript},
        ]
        try:
            tts_begin_task = asyncio.create_task(self.tts.begin(generation))
            self.tts_source_active = True
            first_token = True
            async for delta in self.llm.stream(messages):
                if generation != self.generation:
                    return
                if first_token:
                    first_token = False
                    self.turn_clock["llm_first_token"] = now_ms()
                    await self.send_event(
                        {
                            "type": "metric",
                            "name": "llm_request_to_first_token_ms",
                            "value": round(
                                self.turn_clock["llm_first_token"]
                                - self.turn_clock["llm_request"],
                                1,
                            ),
                        }
                    )
                    await self.send_turn_metrics()
                answer_parts.append(delta)
                await self.send_event({"type": "llm.delta", "text": delta})
                for phrase in chunker.feed(delta):
                    await tts_begin_task
                    if generation != self.generation:
                        return
                    await self.tts.send_text(phrase, generation)

            for phrase in chunker.flush():
                await tts_begin_task
                if generation != self.generation:
                    return
                await self.tts.send_text(phrase, generation)
            await tts_begin_task
            if generation != self.generation:
                return
            await self.tts.finish(generation)

            answer = "".join(answer_parts).strip()
            if generation == self.generation and answer:
                self.turn_answers[generation] = answer
                await self.send_event({"type": "llm.done", "text": answer})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("external turn failed")
            await self.send_event(
                {"type": "server.error", "source": "cascade", "message": str(exc)}
            )
            await self.tts.cancel()

    async def confirm_interrupt(self, reason: str) -> None:
        if self.interrupt_state == "confirmed" and not self.ai_speaking:
            return

        old_generation = self.generation
        self.interrupt_state = "confirmed"
        self.generation += 1  # 先失效所有在途 LLM token / TTS PCM
        await cancel_and_wait(self.hold_watchdog_task)
        self.hold_watchdog_task = None

        # 先让浏览器在音频线程里清空；云端取消可以随后并行进行。
        await self.send_event(
            {"type": "playback.clear", "reason": reason, "generation": old_generation}
        )

        old_task = self.llm_task
        self.llm_task = None
        results = await asyncio.gather(
            cancel_and_wait(old_task, timeout=0.18),
            self.tts.cancel(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.debug("interrupt cancellation detail: %s", result)

        await self.commit_turn(old_generation, interrupted=True)
        self.ai_speaking = False
        self.playback_generation = None
        self.tts_source_active = False
        self.interrupt_sources.clear()
        self.cloud_speech_active = False
        self.last_asr_preview = ""
        await self.send_event({"type": "trace", "message": f"确认打断：{reason}"})

    async def reject_interrupt(self, reason: str) -> None:
        if self.interrupt_state != "holding":
            return
        await cancel_and_wait(self.hold_watchdog_task)
        self.hold_watchdog_task = None
        self.interrupt_state = "idle"
        self.interrupt_sources.clear()
        self.hold_started_ms = None
        if self.barge_mode == "duck":
            await self.send_event({"type": "playback.restore", "reason": reason})
        else:
            await self.send_event(
                {
                    "type": "playback.resume",
                    "reason": reason,
                    "fade_in_ms": self.cfg.barge_fade_in_ms,
                }
            )
        await self.send_event({"type": "trace", "message": f"拒绝打断并续播：{reason}"})

    async def commit_turn(self, generation: int, interrupted: bool) -> None:
        if generation <= 0 or generation in self.committed_generations:
            return
        user = self.turn_users.pop(generation, "").strip()
        answer = self.turn_answers.pop(generation, "").strip()
        if not user:
            return

        if interrupted:
            assistant_context = "[上一轮回答被用户打断，用户没有听完。]"
        elif answer:
            assistant_context = answer
        else:
            return

        self.history.extend(
            [
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant_context},
            ]
        )
        self.history = self.history[-16:]
        self.committed_generations.add(generation)
        # smart_turn 只需要知道机器人说话状态；被打断时绝不能注入完整未听回答。
        await self.sensor.inject_assistant_text(assistant_context)

    async def on_tts_audio(self, audio: bytes, generation: int) -> None:
        if generation != self.generation:
            return
        if not self.ai_speaking:
            self.ai_speaking = True
            self.playback_generation = generation
            self.turn_clock["first_audio"] = now_ms()
            await self.send_event(
                {"type": "playback.started", "sample_rate": 24000, "generation": generation}
            )
            await self.send_turn_metrics()
        await self.send_bytes(audio)

    async def on_tts_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "tts.text_sent" and event.get("first"):
            await self.send_event(
                {
                    "type": "metric",
                    "name": "tts_run_to_first_text_ms",
                    "value": event.get("run_to_first_text_ms"),
                }
            )
        elif event_type == "tts.first_audio":
            await self.send_event(
                {
                    "type": "metric",
                    "name": "tts_run_to_first_audio_ms",
                    "value": event.get("run_to_audio_ms"),
                }
            )
            if event.get("text_to_audio_ms") is not None:
                await self.send_event(
                    {
                        "type": "metric",
                        "name": "tts_text_to_first_audio_ms",
                        "value": event.get("text_to_audio_ms"),
                    }
                )
        elif event_type == "tts.task-finished":
            self.tts_source_active = False
            if event.get("speed_x") is not None:
                await self.send_event(
                    {
                        "type": "metric",
                        "name": "tts_generation_speed_x",
                        "value": event.get("speed_x"),
                    }
                )
            # 这里只表示云端不再产出，不能恢复音量，也不能把 ai_speaking 置 false。
            await self.send_event(
                {
                    "type": "playback.source_finished",
                    "generation": event.get("generation"),
                }
            )
        elif event_type == "tts.task-failed":
            raw = event.get("raw") or {}
            header = raw.get("header") or {}
            message = (
                f"{header.get('error_code', 'task-failed')}: "
                f"{header.get('error_message', raw)}"
            )
            if event.get("generation") == -1:
                await self.send_event(
                    {"type": "trace", "message": f"TTS 预检任务失败：{message}"}
                )
            else:
                await self.send_event(
                    {"type": "server.error", "source": "tts", "message": message}
                )
        elif event_type == "tts.connection_lost":
            await self.send_event(
                {
                    "type": "trace",
                    "message": f"TTS 连接已关闭，将自动重连：{event.get('message', '')}",
                }
            )
        elif event_type == "tts.connected":
            await self.send_event(
                {
                    "type": "tts.status",
                    "status": "socket_connected",
                    "endpoint": event.get("endpoint"),
                    "host": event.get("host"),
                }
            )
        elif event_type == "tts.probe":
            await self.send_event(
                {
                    "type": "tts.status",
                    "status": f"probe_{event.get('status')}",
                    "endpoint": event.get("endpoint"),
                    "host": event.get("host"),
                    "instruction": event.get("instruction"),
                    "message": event.get("message"),
                }
            )
        elif event_type == "tts.ready":
            await self.send_event(
                {
                    "type": "tts.status",
                    "status": "ready",
                    "endpoint": event.get("endpoint"),
                    "host": event.get("host"),
                    "instruction": event.get("instruction"),
                }
            )

    async def send_turn_metrics(self) -> None:
        stop = self.turn_clock.get("speech_stopped")
        if not stop:
            return
        metrics = {
            "endpoint_to_asr_final_ms": self.turn_clock.get("asr_final"),
            "endpoint_to_first_token_ms": self.turn_clock.get("llm_first_token"),
            "endpoint_to_first_audio_ms": self.turn_clock.get("first_audio"),
        }
        for name, timestamp in metrics.items():
            if timestamp:
                await self.send_event(
                    {"type": "metric", "name": name, "value": round(timestamp - stop, 1)}
                )

    def _promote_ambient(self, text: str) -> bool:
        """把 smart_turn 误归为 ambient 的有效短句兜底提升为用户轮次。"""
        cleaned = text.strip(" ，。！？!?…~～")
        if not meaningful_text(cleaned, 2):
            return False

        compact = cleaned.replace(" ", "")
        backchannels = {
            "嗯", "嗯嗯", "啊", "哦", "噢", "好", "好的", "对", "是",
            "行", "可以", "知道了", "哈哈", "呵呵", "哎", "诶",
        }
        if compact in backchannels:
            return False

        # AI 没在说话时，任何达到两字符的有效 ambient 都不应被吞掉。
        if not self.ai_speaking:
            return True

        # AI 说话期间，明显的否定、纠正、追问或长度较长的短句应视为插话。
        correction_markers = (
            "不对", "不是", "还没", "等等", "等下", "先别", "我说",
            "你听错", "为什么", "怎么", "但是", "不过", "别", "停",
        )
        return len(compact) >= 4 or any(marker in compact for marker in correction_markers)

    def _hard_stop(self, text: str) -> bool:
        return any(word in text for word in self.cfg.hard_stop_words)

    async def send_event(self, event: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.browser.send_text(compact_json(event))

    async def send_bytes(self, data: bytes) -> None:
        async with self.send_lock:
            await self.browser.send_bytes(data)
