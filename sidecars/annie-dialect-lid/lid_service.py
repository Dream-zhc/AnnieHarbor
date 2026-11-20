"""Annie 语种/方言旁路服务（Windows 外挂，和 tts_viseme 一样独立运行，2026-11-20）。

每台机器人一个 Room：订阅 <room>/reb_mic 的 16 kHz 单声道 PCM，按音量切出一句话，交给
FireRedLID（GPU，单线程依次推理）判断语种和方言，把结果和心跳发到同一个 Room 的 reb_sam：
  {"type": "language_id", ...}         每句话一条
  {"type": "language_id_health", ...}  每 2 秒一条
主脑订阅 reb_sam，工作台“语种”显示。只观察：不改音频、不往 mic 写东西。
"""

import asyncio
import json
import logging
import math
import os
import socket
import sys
import tempfile
import time
import wave
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRAME_BYTES = 640  # 20 ms of 16 kHz S16LE mono, the Room MIC_AUDIO contract
SINGLETON_PORT = int(os.environ.get("LID_SINGLETON_PORT", "37661"))
NAMES = {
    "zh mandarin": "普通话", "zh yue": "粤语", "zh wu": "吴语（上海话）", "zh min": "闽语（闽南话）",
    "zh north": "北方官话（山东/河北/山西/陕西/辽宁等）",
    "zh xinan": "西南官话（四川/重庆/云南/贵州/湖北）", "zh xiang": "湘语（湖南话）",
    "bo": "藏语", "en": "英语", "ja": "日语", "ko": "韩语", "ru": "俄语", "fr": "法语",
    "de": "德语", "es": "西班牙语", "pt": "葡萄牙语", "it": "意大利语", "ar": "阿拉伯语",
    "th": "泰语", "vi": "越南语", "id": "印尼语", "ms": "马来语",
}
log = logging.getLogger("lid")


def load_config(path=HERE / "config.json"):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    config["room_root"] = (os.environ.get("LID_ROOM_ROOT") or config["room_root"]).rstrip("/")
    config["robots"] = [r for r in config["robots"] if r.get("enabled", True)]
    if not config["robots"]:
        raise ValueError("config.json 里没有启用的机器人")
    rooms = [r["room"] for r in config["robots"]]
    if len(set(rooms)) != len(rooms):
        raise ValueError("一个 Room 只能对应一台机器人")
    return config


def frame_db(frame):
    """dBFS of one 20 ms frame; None for digital silence (all zeros)."""
    count = len(frame) // 2
    if not count:
        return None
    total = 0
    for value in memoryview(frame).cast("h"):
        total += value * value
    if not total:
        return None
    return 20 * math.log10(math.sqrt(total / count) / 32768)


class Segmenter:
    """Energy VAD over 20 ms frames, one per robot. All-zero frames never pull the noise
    floor down (the 11-20 pause-nod bug: a floor dragged to -inf made the room “speech”)."""

    def __init__(self, vad):
        self.start = max(1, int(vad.get("start_ms", 120)) // 20)
        self.end = max(1, int(vad.get("end_ms", 600)) // 20)
        self.min = max(1, int(vad.get("min_ms", 1000)) // 20)
        self.max = max(self.min, int(vad.get("max_ms", 12000)) // 20)
        self.margin = float(vad.get("margin_db", 12))
        self.min_db = float(vad.get("min_db", -45))
        self.floor = -60.0
        self.pre = deque(maxlen=max(self.start, int(vad.get("preroll_ms", 300)) // 20))
        self.frames, self.voiced, self.silent, self.active = [], 0, 0, False

    def feed(self, frame):
        """Return one finished utterance (bytes) or None."""
        db = frame_db(frame)
        loud = db is not None and db > max(self.min_db, self.floor + self.margin)
        if db is not None and not loud and not self.active:
            self.floor = 0.95 * self.floor + 0.05 * db
        if not self.active:
            self.pre.append(frame)
            self.voiced = self.voiced + 1 if loud else 0
            if self.voiced >= self.start:
                self.active, self.frames, self.silent = True, list(self.pre), 0
                self.pre.clear()
            return None
        self.frames.append(frame)
        self.silent = 0 if loud else self.silent + 1
        if self.silent >= self.end:
            speech = self.frames[:len(self.frames) - self.silent]
            self.frames, self.active, self.voiced = [], False, 0
            return b"".join(speech) if len(speech) >= self.min else None
        if len(self.frames) >= self.max:  # a long talk: judge this part, keep listening
            speech, self.frames = self.frames, []
            return b"".join(speech)
        return None


def result_message(robot, seq, raw, *, duration_s, latency_ms, ended_at_ms):
    label = str(raw.get("lang") or "").strip()
    lang, _, dialect = label.partition(" ")
    return {
        "type": "language_id", "room": robot["room"], "robot_id": robot["robot_id"],
        "seq": seq, "label": label, "lang": lang or None, "dialect": dialect or None,
        "name": NAMES.get(label, label or "未知"),
        "confidence": round(float(raw.get("confidence") or 0), 3),
        "duration_s": round(duration_s, 2), "latency_ms": latency_ms,
        "ended_at_ms": ended_at_ms, "model": "FireRedLID",
    }


class Lid:
    """FireRedLID on the GPU (fp16 by default), one utterance at a time."""

    def __init__(self, config):
        sys.path.insert(0, str(HERE / "FireRedASR2S" / "fireredasr2s"))
        import torch
        from fireredlid.lid import FireRedLid, FireRedLidConfig

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        half = bool(config.get("use_half", True)) and self.device == "cuda"
        started = time.perf_counter()
        self.model = FireRedLid.from_pretrained(
            str(HERE / config["model_dir"]),
            FireRedLidConfig(use_gpu=self.device == "cuda", use_half=half))
        (HERE / "tmp").mkdir(exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="lid-", dir=HERE / "tmp"))
        log.info("model loaded device=%s half=%s load_s=%.1f", self.device, half,
                 time.perf_counter() - started)

    def classify(self, pcm, name):
        path = self.tmp / f"{name}.wav"
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(pcm)
        try:
            return self.model.process([name], [str(path)])[0]
        finally:
            path.unlink(missing_ok=True)


class Service:
    def __init__(self, config):
        self.config = config
        self.queue = asyncio.Queue(maxsize=int(config.get("queue_max", 8)))
        self.lid = None
        self.status, self.reason = "loading", None
        self.mic = {r["room"]: False for r in config["robots"]}
        self.publishers = {}
        self.seq = 0
        self.processed = 0
        self.dropped = 0

    def url(self, room, channel):
        return f"{self.config['room_root']}/{room}/{channel}"

    async def connect(self, room, channel):
        import websockets

        ws = await websockets.connect(self.url(room, channel), open_timeout=5,
                                      ping_interval=20, max_size=None)
        welcome = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if welcome.get("type") != "WELCOME" or welcome.get("ok") is not True:
            await ws.close()
            raise ValueError(f"{room}/{channel} 没有返回 WELCOME")
        if welcome.get("room") != room or welcome.get("robotId") != channel:
            await ws.close()
            raise ValueError(f"WELCOME 不匹配：期望 {room}/{channel}，"
                             f"收到 {welcome.get('room')}/{welcome.get('robotId')}")
        return ws

    async def listen(self, robot):
        """One robot's microphone: its own Room only, so results can never cross robots."""
        room, backoff = robot["room"], 1.0
        while True:
            try:
                ws = await self.connect(room, self.config["mic_channel"])
                await ws.send(json.dumps({"type": "SUBSCRIBE", "data": {"types": ["*"]}}))
                segmenter, rest = Segmenter(self.config["vad"]), b""
                self.mic[room], backoff = True, 1.0
                log.info("mic connected robot=%s room=%s", robot["robot_id"], room)
                async for raw in ws:
                    if not isinstance(raw, bytes):
                        continue  # ACK, mic.start and other control messages
                    rest += raw
                    while len(rest) >= FRAME_BYTES:
                        frame, rest = rest[:FRAME_BYTES], rest[FRAME_BYTES:]
                        speech = segmenter.feed(frame)
                        if speech:
                            self.enqueue(robot, speech)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("mic disconnected robot=%s room=%s error=%s retry=%.0fs",
                            robot["robot_id"], room, exc, backoff)
            self.mic[room] = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    def enqueue(self, robot, speech):
        item = (robot, speech, int(time.time() * 1000), time.perf_counter())
        if self.queue.full():  # overloaded: the oldest sentence is the least useful
            self.queue.get_nowait()
            self.dropped += 1
            log.warning("queue full, dropped the oldest utterance dropped=%d", self.dropped)
        self.queue.put_nowait(item)

    async def work(self):
        loop = asyncio.get_running_loop()
        try:
            self.lid = await loop.run_in_executor(None, Lid, self.config)
            self.status = "ready"
        except Exception as exc:
            self.status, self.reason = "error", f"模型加载失败：{type(exc).__name__}: {exc}"[:200]
            log.exception("model load failed")
            return
        while True:
            robot, speech, ended_at_ms, queued = await self.queue.get()
            self.seq += 1
            started = time.perf_counter()
            try:
                raw = await loop.run_in_executor(None, self.lid.classify, speech, f"u{self.seq}")
            except Exception as exc:
                log.warning("classify failed robot=%s error=%s", robot["robot_id"], exc)
                continue
            message = result_message(
                robot, self.seq, raw, duration_s=len(speech) / 32000,
                latency_ms=round((time.perf_counter() - queued) * 1000), ended_at_ms=ended_at_ms)
            self.processed += 1
            log.info("language robot=%s room=%s label=%s conf=%.3f dur=%.1fs infer_ms=%d wait_ms=%d",
                     robot["robot_id"], robot["room"], message["label"], message["confidence"],
                     message["duration_s"], round((time.perf_counter() - started) * 1000),
                     round((started - queued) * 1000))
            await self.publish(robot, message)

    async def publish(self, robot, message):
        room = robot["room"]
        for attempt in range(2):
            try:
                if room not in self.publishers:
                    self.publishers[room] = await self.connect(room, self.config["publish_channel"])
                await self.publishers[room].send(json.dumps(message, ensure_ascii=False,
                                                            separators=(",", ":")))
                return
            except Exception as exc:
                stale = self.publishers.pop(room, None)
                if stale is not None:
                    try:
                        await stale.close()
                    except Exception:
                        pass
                if attempt:
                    log.warning("publish failed room=%s error=%s", room, exc)

    async def health(self):
        while True:
            for robot in self.config["robots"]:
                await self.publish(robot, {
                    "type": "language_id_health", "room": robot["room"],
                    "robot_id": robot["robot_id"], "status": self.status,
                    "reason": self.reason, "mic_connected": self.mic[robot["room"]],
                    "device": getattr(self.lid, "device", None), "queue": self.queue.qsize(),
                    "processed": self.processed, "dropped": self.dropped, "model": "FireRedLID",
                })
            await asyncio.sleep(float(self.config.get("health_interval_s", 2)))

    async def run(self):
        tasks = [asyncio.create_task(self.work()), asyncio.create_task(self.health())]
        tasks += [asyncio.create_task(self.listen(robot)) for robot in self.config["robots"]]
        await asyncio.gather(*tasks)


def main():
    (HERE / "logs").mkdir(exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout),
                RotatingFileHandler(HERE / "logs" / "lid.log", maxBytes=5_000_000,
                                    backupCount=3, encoding="utf-8")]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", SINGLETON_PORT))
    except OSError:
        log.error("已有一个语种识别服务在运行（端口 %d 被占用），不重复启动。", SINGLETON_PORT)
        sys.exit(2)
    config = load_config()
    log.info("start rooms=%s root=%s", [(r["robot_id"], r["room"]) for r in config["robots"]],
             config["room_root"])
    try:
        asyncio.run(Service(config).run())
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
