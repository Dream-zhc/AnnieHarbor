from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from config import Settings
from utils import compact_json, now_ms

logger = logging.getLogger(__name__)
EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class QwenAudioSensor:
    """Qwen-Audio Realtime 听觉协处理器。

    v1.5 的原则：
    - 不再对每个 response.created 立即发送 response.cancel。线上服务会把
      “长期没有完成过 response”视为失活，并在约 180 秒后关闭会话。
    - 内部回复由极短 sensor prompt 限制为一个句号；文本和音频均丢弃，
      response.done 后删除 assistant item，不污染上下文。
    - 空闲时定期触发静默 heartbeat；连接异常时自动重连，并短暂缓存麦克风帧。
    """

    def __init__(self, cfg: Settings, on_event: EventHandler) -> None:
        self.cfg = cfg
        self.on_event = on_event
        self.ws: ClientConnection | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None
        self.send_lock = asyncio.Lock()
        self.reconnect_lock = asyncio.Lock()
        self.connected = asyncio.Event()

        self._closing = False
        self._response_active = False
        self._turn_active = False
        self._heartbeat_pending = False
        self._last_completed_response_ms = now_ms()
        self._assistant_items: set[str] = set()

        max_context_items = max(4, cfg.audio_max_history_turns * 2)
        self._context: deque[tuple[str, str]] = deque(maxlen=max_context_items)

        self._audio_backlog: deque[bytes] = deque()
        self._audio_backlog_bytes = 0
        self._audio_backlog_limit = max(
            6400,
            int(16000 * 2 * cfg.audio_sensor_reconnect_buffer_ms / 1000),
        )

    async def connect(self) -> None:
        self._closing = False
        await self._open_and_configure(initial=True)
        self.reader_task = asyncio.create_task(self._reader_loop(), name="qwen-audio-reader")
        self.heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="qwen-audio-heartbeat"
        )

    async def close(self) -> None:
        self._closing = True
        self.connected.clear()
        current = asyncio.current_task()
        for task in (self.heartbeat_task, self.reader_task):
            if task and task is not current:
                task.cancel()
        for task in (self.heartbeat_task, self.reader_task):
            if task and task is not current:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self.heartbeat_task = None
        self.reader_task = None
        ws, self.ws = self.ws, None
        if ws:
            try:
                await ws.close()
            except Exception:
                pass

    async def send_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        ws = self.ws
        if not self.connected.is_set() or not self._is_open(ws):
            self._queue_audio(pcm16)
            return
        try:
            await self._send_on(
                ws,
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm16).decode("ascii"),
                },
            )
        except Exception:
            self._queue_audio(pcm16)
            self.connected.clear()
            try:
                await ws.close()
            except Exception:
                pass

    async def inject_assistant_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._remember("assistant", text)
        if not self.connected.is_set():
            return
        await self.send_json(self._context_event("assistant", text))

    async def send_json(self, payload: dict[str, Any]) -> bool:
        ws = self.ws
        if not self.connected.is_set() or not self._is_open(ws):
            return False
        await self._send_on(ws, payload)
        return True

    async def _open_and_configure(self, *, initial: bool) -> None:
        async with self.reconnect_lock:
            headers = {
                "Authorization": f"Bearer {self.cfg.api_key}",
                "User-Agent": "qwen-smart-duplex-demo/1.7",
            }
            if self.cfg.workspace_id:
                headers["X-DashScope-WorkSpace"] = self.cfg.workspace_id

            new_ws: ClientConnection | None = None
            try:
                new_ws = await websockets.connect(
                    self.cfg.audio_ws_url,
                    additional_headers=headers,
                    max_size=8 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=15,
                    close_timeout=3,
                )

                raw = await asyncio.wait_for(new_ws.recv(), timeout=10)
                first = json.loads(raw)
                await self.on_event(first)
                if first.get("type") == "error":
                    raise RuntimeError(self._error_message(first))

                await self._send_on(
                    new_ws,
                    {
                        "type": "session.update",
                        "session": {
                            # 当前线上 smart_turn 要求会话级模态同时包含 text/audio。
                            # 内部音频事件仍会在 reader 中全部丢弃。
                            "modalities": ["text", "audio"],
                            "instructions": self.cfg.audio_sensor_prompt,
                            "input_audio_format": "pcm",
                            "max_history_turns": self.cfg.audio_max_history_turns,
                            "turn_detection": {"type": "smart_turn"},
                        },
                    },
                )

                while True:
                    raw = await asyncio.wait_for(new_ws.recv(), timeout=10)
                    if not isinstance(raw, str):
                        continue
                    event = json.loads(raw)
                    await self.on_event(event)
                    event_type = event.get("type", "")
                    if event_type == "session.updated":
                        break
                    if event_type == "error":
                        raise RuntimeError(
                            f"Qwen-Audio session.update failed: {self._error_message(event)}"
                        )

                # 重连后恢复最近用户/assistant 文本，使 smart_turn 仍能理解
                # “不对”“还没吧”“然后呢”等依赖前文的短句。
                if not initial and self._context:
                    for role, text in tuple(self._context):
                        await self._send_on(new_ws, self._context_event(role, text))

                old_ws, self.ws = self.ws, new_ws
                if old_ws and old_ws is not new_ws:
                    try:
                        await old_ws.close()
                    except Exception:
                        pass
                self._response_active = False
                self._turn_active = False
                self._heartbeat_pending = False
                self._assistant_items.clear()
                self._last_completed_response_ms = now_ms()
                self.connected.set()
                await self._flush_audio_backlog(new_ws)

                if not initial:
                    await self.on_event(
                        {"type": "local.sensor_reconnected", "source": "qwen_audio"}
                    )
            except Exception:
                self.connected.clear()
                if new_ws:
                    try:
                        await new_ws.close()
                    except Exception:
                        pass
                raise

    async def _reader_loop(self) -> None:
        while not self._closing:
            ws = self.ws
            if not ws:
                if not await self._reconnect_forever():
                    return
                continue
            try:
                async for raw in ws:
                    if not isinstance(raw, str):
                        continue
                    event = json.loads(raw)
                    await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._closing:
                    logger.warning("Qwen-Audio connection lost: %s", exc)
            finally:
                if self.ws is ws:
                    self.connected.clear()
                    self.ws = None

            if self._closing:
                return
            await self.on_event(
                {
                    "type": "local.sensor_reconnecting",
                    "source": "qwen_audio",
                    "message": "听觉会话已断开，正在自动重连",
                }
            )
            if not await self._reconnect_forever():
                return

    async def _reconnect_forever(self) -> bool:
        delay = 1.0
        while not self._closing:
            try:
                await self._open_and_configure(initial=False)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.on_event(
                    {
                        "type": "local.sensor_reconnect_failed",
                        "source": "qwen_audio",
                        "message": str(exc),
                        "retry_in_sec": round(delay, 1),
                    }
                )
                await asyncio.sleep(delay)
                delay = min(float(self.cfg.audio_sensor_reconnect_max_sec), delay * 2)
        return False

    async def _heartbeat_loop(self) -> None:
        interval_ms = self.cfg.audio_sensor_heartbeat_sec * 1000
        try:
            while not self._closing:
                await asyncio.sleep(2)
                if not self.connected.is_set():
                    continue
                if self._turn_active or self._response_active or self._heartbeat_pending:
                    continue
                if now_ms() - self._last_completed_response_ms < interval_ms:
                    continue
                self._heartbeat_pending = True
                try:
                    # smart_turn 等待下一轮时允许 response.create。会话 prompt 限制
                    # 输出为一个句号；所有文本/音频都由 reader 丢弃。
                    sent = await self.send_json({"type": "response.create"})
                    if sent:
                        await self.on_event(
                            {"type": "local.sensor_heartbeat", "source": "qwen_audio"}
                        )
                    else:
                        self._heartbeat_pending = False
                except Exception:
                    self._heartbeat_pending = False
        except asyncio.CancelledError:
            raise

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "")

        if event_type == "input_audio_buffer.speech_started":
            self._turn_active = True

        elif event_type == "input_audio_buffer.speech_stopped":
            if event.get("reason") == "turn_invalid":
                self._turn_active = False

        elif event_type == "conversation.item.ambient_audio_transcription.completed":
            self._turn_active = False

        elif event_type == "conversation.item.input_audio_transcription.completed":
            transcript = str(event.get("transcript", "")).strip()
            if transcript:
                self._remember("user", transcript)

        elif event_type == "response.created":
            self._response_active = True
            # 不取消。让极短内部 response 正常完成，既维持服务端会话活性，
            # 也避免 response.cancel 与服务端自动打断之间的竞态报错。

        elif event_type == "response.output_item.added":
            item = event.get("item") or {}
            item_id = item.get("id") or event.get("item_id")
            if item_id:
                self._assistant_items.add(str(item_id))

        elif event_type == "response.done":
            self._response_active = False
            self._turn_active = False
            self._heartbeat_pending = False
            response = event.get("response") or {}
            if response.get("status") == "completed":
                self._last_completed_response_ms = now_ms()
            items = list(self._assistant_items)
            self._assistant_items.clear()
            for item_id in items:
                try:
                    await self.send_json(
                        {"type": "conversation.item.delete", "item_id": item_id}
                    )
                except Exception:
                    logger.debug("delete sensor assistant item failed", exc_info=True)

        elif event_type == "error":
            message = self._error_message(event)
            param = ((event.get("error") or {}).get("param") or "") if isinstance(event.get("error"), dict) else ""
            # heartbeat 与新用户 turn 发生极窄竞态时，response.create 可能被拒绝；
            # 这是可恢复状态，不应该把前端标成红色错误。
            if self._heartbeat_pending and (param == "response.create" or "response" in message.lower()):
                self._heartbeat_pending = False
                await self.on_event(
                    {
                        "type": "local.sensor_heartbeat_skipped",
                        "source": "qwen_audio",
                        "message": message,
                    }
                )
                return
            # 线上 180 秒 watchdog 会先发 error 再关 WS。交给 reader 自动重连，
            # 不把一次可恢复轮换显示为整个 Demo 故障。
            if "session was closed" in message.lower() or "180 seconds" in message.lower():
                await self.on_event(
                    {
                        "type": "local.sensor_warning",
                        "source": "qwen_audio",
                        "message": message,
                    }
                )
                ws = self.ws
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                return

        # 内部模型输出全部吞掉。正式回答只来自外部 LLM + Qwen-Audio-TTS。
        if event_type.startswith("response.audio") or event_type.startswith("response.text"):
            return

        await self.on_event(event)

    async def _flush_audio_backlog(self, ws: ClientConnection) -> None:
        while self._audio_backlog and self._is_open(ws):
            pcm16 = self._audio_backlog.popleft()
            self._audio_backlog_bytes -= len(pcm16)
            await self._send_on(
                ws,
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm16).decode("ascii"),
                },
            )
        self._audio_backlog_bytes = max(0, self._audio_backlog_bytes)

    def _queue_audio(self, pcm16: bytes) -> None:
        self._audio_backlog.append(pcm16)
        self._audio_backlog_bytes += len(pcm16)
        while self._audio_backlog and self._audio_backlog_bytes > self._audio_backlog_limit:
            removed = self._audio_backlog.popleft()
            self._audio_backlog_bytes -= len(removed)

    async def _send_on(self, ws: ClientConnection, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await ws.send(compact_json(payload))

    def _remember(self, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        item = (role, text)
        if self._context and self._context[-1] == item:
            return
        self._context.append(item)

    @staticmethod
    def _context_event(role: str, text: str) -> dict[str, Any]:
        content_type = "input_text" if role == "user" else "output_text"
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, "text": text}],
            },
        }

    @staticmethod
    def _is_open(ws: ClientConnection | None) -> bool:
        if ws is None:
            return False
        state = getattr(ws, "state", None)
        name = getattr(state, "name", "OPEN")
        return name == "OPEN"

    @staticmethod
    def _error_message(event: dict[str, Any]) -> str:
        error = event.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(event.get("message") or event)
