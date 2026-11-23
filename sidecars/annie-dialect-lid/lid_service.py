"""Annie 语种/方言旁路服务（Windows 外挂，和 tts_viseme 一样独立运行，2026-11-20）。

每台机器人一个 Room：订阅 <room>/reb_mic 的 16 kHz 单声道 PCM，按音量切出一句话，交给
FireRedLID（GPU，单线程依次推理）判断语种和方言，把结果和心跳发到同一个 Room 的 reb_sam：
  {"type": "language_id", ...}         每句话一条
  {"type": "language_id_health", ...}  每 2 秒一条
主脑订阅 reb_sam，工作台“语种”显示。只观察：不改音频、不往 mic 写东西。

2026-11-23 真实 Room 验证后的三处修正：
  1. Room 的二进制帧带信封：2 字节大端类型长度 + "MIC_AUDIO" + 640 字节 PCM（和主脑
     annie_adapters.decode_room_binary_frame 一致），必须先拆信封再切句；
  2. 机器人自己的声音会被它的麦克风收进来：同时只读订阅同一 Room 的 reb_tts，她出声的时间段里
     大部分重叠的一句话当作回声，不判断；
  3. 往 reb_sam 发消息的连接也会收到 Room 推来的所有消息，必须读掉，否则读缓冲满了会连心跳一起卡住。
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
MIC_TYPE = b"MIC_AUDIO"
ECHO_SHARE = 0.5  # an utterance at least this much inside her own voice is her echo, not the user
SUBSCRIBE = json.dumps({"type": "SUBSCRIBE", "data": {"types": ["*"]}})
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


def load_config(path=None):
    path = path or os.environ.get("LID_CONFIG") or HERE / "config.json"
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    config["room_root"] = (os.environ.get("LID_ROOM_ROOT") or config["room_root"]).rstrip("/")
    config["robots"] = [r for r in config["robots"] if r.get("enabled", True)]
    if not config["robots"]:
        raise ValueError("config.json 里没有启用的机器人")
    rooms = [r["room"] for r in config["robots"]]
    if len(set(rooms)) != len(rooms):
        raise ValueError("一个 Room 只能对应一台机器人")
    return config


def mic_payload(message):
    """The PCM of one Room binary frame: 2-byte big-endian type length, the type, the payload
    (the same envelope annie_adapters.decode_room_binary_frame reads). Only MIC_AUDIO is sound;
    anything else, or a damaged frame, is None."""
    if len(message) < 3 or int.from_bytes(message[:2], "big") != len(MIC_TYPE):
        return None
    if message[2:2 + len(MIC_TYPE)] != MIC_TYPE:
        return None
    return message[2 + len(MIC_TYPE):]


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
        self.trailing = 0  # frames after the returned utterance that were cut off (silence)

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
            self.trailing = self.silent
            self.frames, self.active, self.voiced = [], False, 0
            return b"".join(speech) if len(speech) >= self.min else None
        if len(self.frames) >= self.max:  # a long talk: judge this part, keep listening
            speech, self.frames = self.frames, []
            self.trailing = 0
            return b"".join(speech)
        return None


def tts_seconds(data):
    """Length in seconds of one TTS packet of the brain (24 kHz samples start..end, else the audio)."""
    rate = data.get("sample_rate")
    rate = rate if type(rate) is int and rate > 0 else 24000
    start, end = data.get("start_sample"), data.get("end_sample")
    if type(start) is int and type(end) is int and end >= start:
        return (end - start) / rate
    audio = data.get("audio")
    if isinstance(audio, str) and audio:
        return (len(audio) * 3 // 4 - audio[-2:].count("=")) / 2 / rate
    return 0.0


class Playing:
    """When her own voice is audible in one Room, from the TTS packets on reb_tts.

    Her speaker plays the audio back to back at real time, so every chunk extends the end by its
    own length. The microphone hears her a little later (device delay) and a little after the end
    (room echo), hence the margins. An utterance mostly inside this time is her echo."""

    BEFORE, AFTER = 0.15, 0.6

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.spans = deque(maxlen=64)  # [start, end] in clock seconds, oldest first

    def chunk(self, seconds, now=None):
        now = self.clock() if now is None else now
        last_end = self.spans[-1][1] if self.spans else None
        start = now if last_end is None or now > last_end else last_end
        if last_end is not None and start - last_end <= 0.05:
            self.spans[-1][1] = start + seconds
        else:
            self.spans.append([start, start + seconds])

    def stop(self, now=None):
        now = self.clock() if now is None else now
        if self.spans and self.spans[-1][1] > now:
            self.spans[-1][1] = max(self.spans[-1][0], now)

    def feed(self, message, now=None):
        if not isinstance(message, dict) or message.get("type") != "tts":
            return
        data = message.get("data")
        if not isinstance(data, dict):
            return
        if data.get("stop"):
            self.stop(now)
            return
        seconds = tts_seconds(data)
        if seconds > 0:
            self.chunk(seconds, now)

    def share(self, start, end):
        """Share (0..1) of [start, end] that lies inside her audible time plus the margins."""
        if end <= start:
            return 0.0
        merged = []
        for first, last in self.spans:
            first, last = first - self.BEFORE, last + self.AFTER
            if merged and first <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        covered = sum(max(0.0, min(last, end) - max(first, start)) for first, last in merged)
        return min(1.0, covered / (end - start))


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
        sys.path.insert(0, str(HERE / "vendor"))  # FireRedLID inference code, Apache-2.0
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
    def __init__(self, config, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.queue = asyncio.Queue(maxsize=int(config.get("queue_max", 8)))
        self.lid = None
        self.status, self.reason = "loading", None
        rooms = [r["room"] for r in config["robots"]]
        self.mic = dict.fromkeys(rooms, False)
        self.mic_messages = dict.fromkeys(rooms, 0)
        self.segmenters = {}
        self.playing = {room: Playing(clock) for room in rooms}
        self.publishers = {}
        self._beats = {}
        self._background = set()
        self.seq = 0
        self.processed = 0
        self.dropped = 0
        self.echo_skipped = 0

    def url(self, room, channel):
        return f"{self.config['room_root']}/{room}/{channel}"

    async def connect(self, room, channel):
        import websockets

        options = dict(open_timeout=5, ping_interval=20, max_size=None)
        try:
            # the Room is on the LAN: never through a system proxy (websockets >= 15)
            ws = await websockets.connect(self.url(room, channel), proxy=None, **options)
        except TypeError:  # an older websockets has no proxy argument
            ws = await websockets.connect(self.url(room, channel), **options)
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
                await ws.send(SUBSCRIBE)
                segmenter, rest = Segmenter(self.config["vad"]), b""
                self.segmenters[room] = segmenter
                self.mic[room], backoff = True, 1.0
                log.info("mic connected robot=%s room=%s", robot["robot_id"], room)
                async for raw in ws:
                    if not isinstance(raw, bytes):
                        continue  # ACK and other control messages
                    pcm = mic_payload(raw)
                    if not pcm or len(pcm) % 2:
                        continue  # not MIC_AUDIO, or damaged
                    now = self.clock()
                    self.mic_messages[room] += 1
                    rest += pcm
                    while len(rest) >= FRAME_BYTES:
                        frame, rest = rest[:FRAME_BYTES], rest[FRAME_BYTES:]
                        speech = segmenter.feed(frame)
                        if speech:
                            end = now - segmenter.trailing * 0.02
                            self.enqueue(robot, speech, end - len(speech) / 32000, end)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("mic disconnected robot=%s room=%s error=%s retry=%.0fs",
                            robot["robot_id"], room, exc, backoff)
            self.mic[room] = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    async def watch_tts(self, robot):
        """Her own voice (reb_tts, read only): while it plays, the microphone hears it too."""
        room, backoff = robot["room"], 1.0
        while True:
            try:
                ws = await self.connect(room, self.config.get("tts_channel", "reb_tts"))
                await ws.send(SUBSCRIBE)
                backoff = 1.0
                log.info("tts watch connected robot=%s room=%s", robot["robot_id"], room)
                async for raw in ws:
                    if isinstance(raw, str):
                        try:
                            self.playing[room].feed(json.loads(raw))
                        except ValueError:
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("tts watch disconnected robot=%s room=%s error=%s retry=%.0fs",
                            robot["robot_id"], room, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    def enqueue(self, robot, speech, start, end):
        share = self.playing[robot["room"]].share(start, end)
        if share >= ECHO_SHARE:
            self.echo_skipped += 1
            log.info("skipped robot=%s room=%s dur=%.1fs reason=robot_speaking overlap=%.0f%%",
                     robot["robot_id"], robot["room"], len(speech) / 32000, share * 100)
            return
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
            if not str(raw.get("lang") or "").strip():
                log.info("no result robot=%s dur=%.1fs", robot["robot_id"], len(speech) / 32000)
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

    async def drain(self, room, ws):
        """The Room also pushes everything on reb_sam to this publisher; read and drop it. A socket
        nobody reads stops answering keepalive pings once its queue is full. When the socket
        closes, forget it so the next publish reconnects."""
        try:
            async for _ in ws:
                pass
        except Exception:
            pass
        finally:
            if self.publishers.get(room) is ws:
                self.publishers.pop(room, None)

    async def publish(self, robot, message):
        room = robot["room"]
        for attempt in range(2):
            try:
                if room not in self.publishers:
                    ws = await self.connect(room, self.config["publish_channel"])
                    self.publishers[room] = ws
                    task = asyncio.create_task(self.drain(room, ws))
                    self._background.add(task)
                    task.add_done_callback(self._background.discard)
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

    def health_message(self, robot, now=None):
        room = robot["room"]
        now = self.clock() if now is None else now
        count = self.mic_messages[room]
        last_at, last_count = self._beats.get(room, (now, count))
        self._beats[room] = (now, count)
        fps = round((count - last_count) / max(now - last_at, 1e-3), 1) if now > last_at else None
        return {
            "type": "language_id_health", "room": room,
            "robot_id": robot["robot_id"], "status": self.status,
            "reason": self.reason, "mic_connected": self.mic[room], "mic_fps": fps,
            "device": getattr(self.lid, "device", None), "queue": self.queue.qsize(),
            "processed": self.processed, "dropped": self.dropped,
            "echo_skipped": self.echo_skipped, "model": "FireRedLID",
        }

    async def health(self):
        beats = 0
        while True:
            now = self.clock()
            messages = [(robot, self.health_message(robot, now)) for robot in self.config["robots"]]
            await asyncio.gather(*(self.publish(robot, message) for robot, message in messages))
            beats += 1
            if beats % 30 == 1:
                for robot, message in messages:
                    segmenter = self.segmenters.get(robot["room"])
                    log.info("stats robot=%s room=%s status=%s mic_fps=%s vad_floor=%s processed=%d "
                             "echo_skipped=%d dropped=%d", robot["robot_id"], robot["room"],
                             message["status"], message["mic_fps"],
                             f"{segmenter.floor:.0f}dB" if segmenter else "-", self.processed,
                             self.echo_skipped, self.dropped)
            await asyncio.sleep(float(self.config.get("health_interval_s", 2)))

    async def run(self):
        tasks = [asyncio.create_task(self.work()), asyncio.create_task(self.health())]
        for robot in self.config["robots"]:
            tasks.append(asyncio.create_task(self.listen(robot)))
            tasks.append(asyncio.create_task(self.watch_tts(robot)))
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
