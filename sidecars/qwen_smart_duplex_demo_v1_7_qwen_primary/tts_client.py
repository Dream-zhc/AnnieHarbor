from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from config import Settings, TTSEndpoint
from utils import compact_json, now_ms

logger = logging.getLogger(__name__)
AudioHandler = Callable[[bytes, int], Awaitable[None]]
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class TTSStartError(RuntimeError):
    def __init__(self, message: str, raw: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.raw = raw or {}


class QwenAudioTTS:
    """Qwen-Audio-TTS 原生双向流式 WebSocket。

    v1.2 增加：
    - 固定端点路径校正（无尾随斜杠）
    - 启动预检，不再把“握手成功”误报为“TTS 可用”
    - 业务空间专属域名失败时回退公共端点
    - instruction 参数失败时降级到官方最小参数集
    - task-failed/1011 后自动重连
    """

    def __init__(
        self,
        cfg: Settings,
        on_audio: AudioHandler,
        on_event: EventHandler,
    ) -> None:
        self.cfg = cfg
        self.on_audio = on_audio
        self.on_event = on_event

        self.ws: ClientConnection | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.send_lock = asyncio.Lock()
        self.lifecycle_lock = asyncio.Lock()

        self.task_started = asyncio.Event()
        self.task_finished = asyncio.Event()
        self.task_finished.set()
        self.start_waiter: asyncio.Future[None] | None = None

        self.active_task_id: str | None = None
        self.active_generation: int | None = None
        self.first_audio_seen = False
        self.run_sent_ms = 0.0
        self.first_text_sent_ms = 0.0
        self.first_audio_ms = 0.0
        self.last_audio_ms = 0.0
        self.first_audio_packet_ms = 0.0
        self.audio_bytes_received = 0

        self.endpoint: TTSEndpoint | None = None
        self.use_instruction = bool(cfg.tts_instruction)
        self.preflight_complete = False
        self.closing = False

    async def connect(self) -> None:
        async with self.lifecycle_lock:
            if self.preflight_complete and self._is_open():
                return
            if self.cfg.tts_preflight:
                await self._preflight_locked()
            else:
                self.endpoint = self.cfg.tts_endpoints[0]
                await self._connect_endpoint_locked(self.endpoint)
                self.preflight_complete = True
                await self.on_event(
                    {
                        "type": "tts.ready",
                        "endpoint": self.endpoint.label,
                        "host": self.endpoint.host,
                        "instruction": self.use_instruction,
                    }
                )

    async def close(self) -> None:
        self.closing = True
        async with self.lifecycle_lock:
            await self._close_socket_locked()

    async def begin(self, generation: int) -> None:
        async with self.lifecycle_lock:
            await self._ensure_ready_locked()
            if not self.task_finished.is_set():
                await self._cancel_locked()
                try:
                    await asyncio.wait_for(self.task_finished.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    await self._reconnect_locked()

            try:
                await self._run_task_locked(generation, self.use_instruction)
            except Exception:
                # task-failed 后服务端常用 1011 主动断开；重连一次再把错误上抛，
                # 确保下一轮不会卡在一个已关闭的连接上。
                await self._safe_reconnect_locked()
                raise

    async def send_text(self, text: str, generation: int) -> None:
        if not text.strip() or generation != self.active_generation or not self.active_task_id:
            return
        await self.task_started.wait()
        first_text = self.first_text_sent_ms <= 0
        sent_at = now_ms()
        await self._send(
            {
                "header": {
                    "action": "continue-task",
                    "task_id": self.active_task_id,
                    "streaming": "duplex",
                },
                "payload": {"input": {"text": text}},
            }
        )
        if first_text:
            self.first_text_sent_ms = sent_at
        await self.on_event(
            {
                "type": "tts.text_sent",
                "text": text,
                "generation": generation,
                "first": first_text,
                "run_to_first_text_ms": round(sent_at - self.run_sent_ms, 1) if first_text else None,
            }
        )

    async def finish(self, generation: int) -> None:
        if generation != self.active_generation or not self.active_task_id:
            return
        await self._send(
            {
                "header": {
                    "action": "finish-task",
                    "task_id": self.active_task_id,
                    "streaming": "duplex",
                },
                "payload": {"input": {}},
            }
        )

    async def cancel(self) -> None:
        async with self.lifecycle_lock:
            await self._cancel_locked()

    async def _ensure_ready_locked(self) -> None:
        if not self.preflight_complete:
            await self._preflight_locked()
        elif not self._is_open():
            await self._reconnect_locked()

    async def _preflight_locked(self) -> None:
        errors: list[str] = []
        modes = [True, False] if self.cfg.tts_instruction else [False]

        for endpoint in self.cfg.tts_endpoints:
            for use_instruction in modes:
                label = f"{endpoint.label}/{'instruction' if use_instruction else 'minimal'}"
                await self.on_event(
                    {
                        "type": "tts.probe",
                        "status": "trying",
                        "endpoint": endpoint.label,
                        "host": endpoint.host,
                        "instruction": use_instruction,
                    }
                )
                try:
                    await self._connect_endpoint_locked(endpoint)
                    await self._run_task_locked(generation=-1, use_instruction=use_instruction)
                    await self._cancel_locked()
                    try:
                        await asyncio.wait_for(self.task_finished.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        # 预检 run-task 已得到 task-started，取消确认超时不影响可用性。
                        pass

                    self.endpoint = endpoint
                    self.use_instruction = use_instruction
                    self.preflight_complete = True
                    await self.on_event(
                        {
                            "type": "tts.ready",
                            "endpoint": endpoint.label,
                            "host": endpoint.host,
                            "instruction": use_instruction,
                        }
                    )
                    return
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
                    await self.on_event(
                        {
                            "type": "tts.probe",
                            "status": "failed",
                            "endpoint": endpoint.label,
                            "host": endpoint.host,
                            "instruction": use_instruction,
                            "message": str(exc),
                        }
                    )
                    await self._close_socket_locked()

        detail = " | ".join(errors[-4:])
        raise RuntimeError(
            "TTS 预检失败。请核对模型、音色、API Key/业务空间地域；"
            f"已尝试端点与参数：{detail}"
        )

    async def _run_task_locked(self, generation: int, use_instruction: bool) -> None:
        if not self._is_open():
            raise RuntimeError("TTS WebSocket 未连接")

        self.task_started.clear()
        self.task_finished.clear()
        self.active_task_id = str(uuid.uuid4())
        self.active_generation = generation
        self.first_audio_seen = False
        self.run_sent_ms = now_ms()
        self.first_text_sent_ms = 0.0
        self.first_audio_ms = 0.0
        self.last_audio_ms = 0.0
        self.first_audio_packet_ms = 0.0
        self.audio_bytes_received = 0
        loop = asyncio.get_running_loop()
        self.start_waiter = loop.create_future()

        parameters: dict[str, Any] = {
            "text_type": "PlainText",
            "voice": self.cfg.tts_voice,
            "format": "pcm",
            "sample_rate": 24000,
            "volume": self.cfg.tts_volume,
            "rate": float(self.cfg.tts_rate),
            "pitch": float(self.cfg.tts_pitch),
            "enable_ssml": False,
        }
        if use_instruction and self.cfg.tts_instruction:
            parameters["instruction"] = self.cfg.tts_instruction

        payload = {
            "header": {
                "action": "run-task",
                "task_id": self.active_task_id,
                "streaming": "duplex",
            },
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self.cfg.tts_model,
                "parameters": parameters,
                "input": {},
            },
        }
        await self._send(payload)
        try:
            await asyncio.wait_for(asyncio.shield(self.start_waiter), timeout=10.0)
        except asyncio.TimeoutError as exc:
            raise TTSStartError("等待 task-started 超时") from exc

    async def _cancel_locked(self) -> None:
        if not self.active_task_id or self.task_finished.is_set():
            self.active_generation = None
            return
        task_id = self.active_task_id
        self.active_generation = None
        try:
            await self._send(
                {
                    "header": {
                        "action": "finish-task",
                        "task_id": task_id,
                        "streaming": "duplex",
                    },
                    "payload": {"input": {"directive": "cancel"}},
                }
            )
            await self.on_event({"type": "tts.cancel_sent", "task_id": task_id})
        except Exception:
            logger.exception("TTS cancel failed")
            self.task_finished.set()

    async def _connect_endpoint_locked(self, endpoint: TTSEndpoint) -> None:
        await self._close_socket_locked()
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "User-Agent": "qwen-smart-duplex-demo/1.7",
        }
        if endpoint.send_workspace_header and self.cfg.workspace_id:
            headers["X-DashScope-WorkSpace"] = self.cfg.workspace_id

        self.closing = False
        self.ws = await websockets.connect(
            endpoint.url,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=3,
        )
        self.endpoint = endpoint
        self.reader_task = asyncio.create_task(self._reader(self.ws), name="qwen-tts-reader")
        await self.on_event(
            {"type": "tts.connected", "endpoint": endpoint.label, "host": endpoint.host}
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self._is_open() or self.ws is None:
            raise RuntimeError("TTS WebSocket 未连接或已关闭")
        async with self.send_lock:
            await self.ws.send(compact_json(payload))

    async def _reader(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    generation = self.active_generation
                    if generation is None or generation < 0:
                        continue
                    now = now_ms()
                    self.audio_bytes_received += len(raw)
                    self.last_audio_ms = now
                    if not self.first_audio_seen:
                        self.first_audio_seen = True
                        self.first_audio_ms = now
                        self.first_audio_packet_ms = len(raw) / 2 / 24000 * 1000
                        await self.on_event(
                            {
                                "type": "tts.first_audio",
                                "generation": generation,
                                "run_to_audio_ms": round(now - self.run_sent_ms, 1),
                                "text_to_audio_ms": (
                                    round(now - self.first_text_sent_ms, 1)
                                    if self.first_text_sent_ms > 0
                                    else None
                                ),
                            }
                        )
                    await self.on_audio(raw, generation)
                    continue

                message = json.loads(raw)
                header = message.get("header") or {}
                event = header.get("event", "")
                if event == "task-started":
                    self.task_started.set()
                    if self.start_waiter and not self.start_waiter.done():
                        self.start_waiter.set_result(None)
                elif event in {"task-finished", "task-failed"}:
                    old_generation = self.active_generation
                    finished_at = now_ms()
                    audio_duration_ms = self.audio_bytes_received / 2 / 24000 * 1000
                    synth_wall_ms = (
                        finished_at - self.first_text_sent_ms
                        if self.first_text_sent_ms > 0
                        else 0.0
                    )
                    delivery_wall_ms = (
                        self.last_audio_ms - self.first_audio_ms + self.first_audio_packet_ms
                        if self.first_audio_ms > 0 and self.last_audio_ms >= self.first_audio_ms
                        else 0.0
                    )
                    self.task_finished.set()
                    self.task_started.clear()
                    self.active_task_id = None
                    self.active_generation = None
                    if event == "task-failed":
                        error_code = header.get("error_code", "")
                        error_message = header.get("error_message", "task-failed")
                        exc = TTSStartError(f"{error_code}: {error_message}", raw=message)
                        if self.start_waiter and not self.start_waiter.done():
                            self.start_waiter.set_exception(exc)
                    elif self.start_waiter and not self.start_waiter.done():
                        self.start_waiter.set_exception(
                            TTSStartError("任务在 task-started 前结束", raw=message)
                        )
                    await self.on_event(
                        {
                            "type": f"tts.{event}",
                            "generation": old_generation,
                            "raw": message,
                            "audio_duration_ms": round(audio_duration_ms, 1),
                            "synthesis_wall_ms": round(synth_wall_ms, 1),
                            "speed_x": (
                                round(audio_duration_ms / delivery_wall_ms, 2)
                                if delivery_wall_ms > 0 and audio_duration_ms > 0
                                else None
                            ),
                            "delivery_wall_ms": round(delivery_wall_ms, 1),
                        }
                    )
                elif event == "result-generated":
                    await self.on_event({"type": "tts.result_generated"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.start_waiter and not self.start_waiter.done():
                self.start_waiter.set_exception(TTSStartError(str(exc)))
            self.task_finished.set()
            if not self.closing:
                logger.warning("TTS reader stopped: %s", exc)
                await self.on_event({"type": "tts.connection_lost", "message": str(exc)})

    async def _safe_reconnect_locked(self) -> None:
        try:
            await self._reconnect_locked()
        except Exception:
            logger.exception("TTS reconnect failed")

    async def _reconnect_locked(self) -> None:
        endpoint = self.endpoint or self.cfg.tts_endpoints[0]
        await self._connect_endpoint_locked(endpoint)

    async def _close_socket_locked(self) -> None:
        self.closing = True
        current_task = asyncio.current_task()
        if self.reader_task and self.reader_task is not current_task:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except (asyncio.CancelledError, Exception):
                pass
        self.reader_task = None
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        self.active_task_id = None
        self.active_generation = None
        self.task_started.clear()
        self.task_finished.set()
        if self.start_waiter and not self.start_waiter.done():
            self.start_waiter.cancel()
        self.start_waiter = None

    def _is_open(self) -> bool:
        return bool(self.ws and self.ws.state.name == "OPEN")
