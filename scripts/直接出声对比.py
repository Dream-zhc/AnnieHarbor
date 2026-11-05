"""Qwen-Audio 实时模型“直接出声”对比：现状“实时模型出文字 + 独立 TTS”和“实时模型直接出声”。

同一批合成的用户语音（16 kHz，按 20 ms 实时推流，说完接着推低噪底，像麦克风一样不断流）
分别走两条路，每轮都新开会话、不带历史，人格提示和判停（smart_turn_v2）相同：
- A 现状：实时模型只出文字，按 speech.py 的切句规则送进预热好的 TTS，用默认人格的复刻音色和语气；
- B 直接出声：同一个实时模型同时出文字和音频，音色是用同一段参考录音为实时模型复刻的。
量“最后一帧用户语音发出 → 第一段回复音频到手”，不含发给机器人和扬声器缓冲（两条路一样）。

用法：
  uv run --locked python scripts/直接出声对比.py --rounds 2 --label 第一版
  uv run --locked python scripts/直接出声对比.py --only B --rounds 1 --limit 1
报告和明细写到 var/tmp/直接出声对比/<label>.md、.jsonl。录音在同一目录：用户-N.wav 是合成的用户语音；
<label>-N-A-第R遍.wav、<label>-N-B-第R遍.wav 是两条路的回复；<label>-N-A-对话节奏.wav、
<label>-N-B-对话节奏.wav 把用户语音、实测等待和回复接在一起，直接听就是对话的节奏。
第一次跑会用 tts/ 下默认人格的参考录音为实时模型复刻一个音色（前缀 rtexp），ID 存在同目录
realtime-voice.json 里复用。不打印密钥、地址和完整音色 ID。
"""

import argparse
import asyncio
import base64
import json
import os
import sqlite3
import statistics
import time
import tomllib
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/tmp/直接出声对比"
BEIJING = timezone(timedelta(hours=8))
FRAME = 640  # 20 ms of 16 kHz mono int16, the only size audio_append takes
USER_RATE, REPLY_RATE = 16000, 24000
LEAD_FRAMES = 20  # room noise before the user starts
MAX_WAIT_S = 25
FIRST_WAIT_MS = 360  # speech.py's fallback flush for a short stalled opening
USER_VOICE = "御姐音夯爆"
USER_STYLE = "自然地说话，像平时跟朋友聊天，语速正常。"
UTTERANCES = (
    "小安妮，我今天上班被领导批评了，心情有点差。",
    "你觉得周末去爬山好，还是在家睡一天好？",
    "给我讲个冷笑话吧。",
    "我下周三要去面试，好紧张啊。",
    "你知道天空为什么是蓝色的吗？",
    "哈哈哈，你也太逗了吧。",
)
METRICS = (("speech_stopped", "判停事件"), ("response_created", "开始回复"), ("first_text", "首字"),
           ("first_audio", "首段音频"), ("done", "文字生成完"))
noise = np.random.default_rng(7)


def noise_frame():
    return noise.normal(0, 24, FRAME // 2).astype(np.int16).tobytes()


def masked(voice):
    return f"{voice[:6]}***" if voice else "模型默认"


def write_wav(path, pcm, rate):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)


def setup():
    from annie_adapters.realtime import DEFAULT_URL as REALTIME_URL
    from annie_adapters.realtime import realtime_url
    from annie_adapters.tts import DEFAULT_URL as TTS_URL
    from annie_runtime.__main__ import load_local_env

    load_local_env(ROOT / ".env")
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit("缺少 DASHSCOPE_API_KEY（根 .env）")
    providers = tomllib.loads((ROOT / "config/providers.toml").read_text())
    persona = tomllib.loads((ROOT / "config/personas.toml").read_text())["personas"]["annie"]
    model = os.environ.get("ANNIE_REALTIME_MODEL") or providers["realtime"]["model"]
    tts = providers["tts"]
    return {
        "key": key, "model": model, "persona": persona,
        "realtime_url": realtime_url(os.environ.get("ANNIE_REALTIME_URL") or REALTIME_URL, model),
        "tts_url": os.environ.get("ANNIE_TTS_URL") or TTS_URL,
        "cuts": (tts.get("first_min_chars", 2), tts.get("sentence_min_chars", 4),
                 tts.get("sentence_max_chars", 30)),
    }


def cloned_voice(name):
    db = sqlite3.connect(f"file:{ROOT / 'var/runtime.db'}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT voice_id FROM cloned_voices WHERE name = ?", (name,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise SystemExit(f"音色库里没有“{name}”")
    return row[0]


async def realtime_voice(cfg, name):
    """A clone of the persona's reference recording bound to the Realtime model, made once."""
    from annie_runtime.voice_profiles import DashscopeVoiceProvider

    cache = OUT / "realtime-voice.json"
    if cache.exists():
        saved = json.loads(cache.read_text())
        if saved.get("model") == cfg["model"] and saved.get("source") == name:
            return saved["voice_id"]
    reference = ROOT / "tts" / f"{name}.wav"
    provider = DashscopeVoiceProvider(cfg["key"], cfg["tts_url"])
    provider.model = cfg["model"]  # a clone only works with its target model
    voice = await provider.clone(reference.name, reference.read_bytes(), "rtexp", "zh")
    cache.write_text(json.dumps({"model": cfg["model"], "source": name, "voice_id": voice,
                                 "created": datetime.now(BEIJING).isoformat(timespec="seconds")},
                                ensure_ascii=False))
    return voice


async def synthesize(cfg, text, voice, *, sample_rate=USER_RATE, **style):
    from annie_adapters.tts import QwenTtsSocket, command, task_id

    socket = await QwenTtsSocket(cfg["tts_url"], cfg["key"]).open()
    task, pcm = task_id(), bytearray()
    try:
        await socket.send(command("run-task", task, voice=voice, sample_rate=sample_rate,
                                  language_hint="zh", **style))
        while True:
            message = await asyncio.wait_for(socket.recv(), 20)
            if isinstance(message, bytes):
                pcm += message
                continue
            event = (message.get("header") or {}).get("event")
            if event == "task-started":
                await socket.send(command("continue-task", task, text=text))
                await socket.send(command("finish-task", task))
            elif event == "task-finished":
                return bytes(pcm)
            elif event == "task-failed":
                raise RuntimeError((message.get("header") or {}).get("error_code") or "task-failed")
    finally:
        await socket.close()


def trimmed(pcm):
    """Cut the synthesizer's silent head and tail: the last frame sent is the end of the words."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > 600)
    if not len(loud):
        return pcm
    start, end = max(0, loud[0] - USER_RATE // 20), min(len(samples), loud[-1] + USER_RATE // 50)
    return samples[start:end].tobytes()


async def user_audio(cfg):
    voice = cloned_voice(USER_VOICE)
    index = OUT / "用户语音.json"
    known = json.loads(index.read_text()) if index.exists() else {}
    clips = []
    for n, text in enumerate(UTTERANCES, 1):
        path = OUT / f"用户-{n}.wav"
        if known.get(str(n)) != text or not path.exists():
            write_wav(path, trimmed(await synthesize(cfg, text, voice, instruction=USER_STYLE)), USER_RATE)
            known[str(n)] = text
        with wave.open(str(path), "rb") as source:
            clips.append(source.readframes(source.getnframes()))
    index.write_text(json.dumps(known, ensure_ascii=False, indent=1))
    return clips


def instructions(persona, spoken):
    text = ("你是小安妮，一个放在桌上的陪伴机器人，正在和面前的人当面语音聊天。\n"
            + persona["style"].strip()
            + "\n用口语回答，像当面说话，一般一两句，不要列表、表情符号和括号里的动作。")
    if spoken:
        text += "\n你说话的声音：" + persona["tts"]["instruction"]
    return text


class Speech:
    """Path A's voice: speech.py's sentence cutting into one task on a prewarmed TTS socket."""

    def __init__(self, socket, run_command, cuts):
        from annie_adapters.tts import task_id

        self.socket, self.run_command = socket, run_command
        self.first_chars, self.min_chars, self.max_chars = cuts
        self.task = task_id()
        self.pending, self.segments = "", 0
        self.queue = asyncio.Queue()
        self.started = asyncio.Event()
        self.pcm = bytearray()
        self.first_audio = None
        self.timer = self.sender = self.receiver = None

    def feed(self, delta):
        if not delta:
            return
        if self.sender is None:
            self.sender = asyncio.create_task(self._send())
            self.receiver = asyncio.create_task(self._receive())
        self.pending += delta
        self._drain()
        if self.pending and self.timer is None:
            self.timer = asyncio.create_task(self._first_flush())

    async def _first_flush(self):
        try:
            await asyncio.sleep(FIRST_WAIT_MS / 1000)
            if self.pending and (len(self.pending.strip()) >= 2 or self.pending[-1] in self.ends()[0]):
                self._drain(force=True)
        finally:
            self.timer = None

    @staticmethod
    def ends():
        from annie_runtime.speech import CLAUSE_END, SENTENCE_END

        return SENTENCE_END, CLAUSE_END

    def _drain(self, force=False):
        sentence_end, clause_end = self.ends()
        while self.pending:
            head = self.pending[:self.max_chars]
            least = self.first_chars if self.segments == 0 else self.min_chars
            cut = next((i + 1 for i, char in enumerate(head)
                        if char in sentence_end and i + 1 >= least), None)
            if cut is None and len(self.pending) >= 24:
                cut = next((i + 1 for i, char in enumerate(head)
                            if char in clause_end and i + 1 >= 12), None)
            if cut is None and len(self.pending) >= self.max_chars:
                cut = self.max_chars
            if (cut is None and self.segments == 0 and len(self.pending) >= self.first_chars
                    and not any(char in sentence_end or char in clause_end for char in head)):
                cut = self.first_chars
            if cut is None and force:
                cut = len(self.pending)
            if cut is None:
                return
            segment, self.pending = self.pending[:cut], self.pending[cut:]
            if segment.strip():
                self.queue.put_nowait(segment)
                self.segments += 1

    async def _send(self):
        from annie_adapters.tts import command

        await self.socket.send(self.run_command(self.task))
        await self.started.wait()
        while (segment := await self.queue.get()) is not None:
            await self.socket.send(command("continue-task", self.task, text=segment))
        await self.socket.send(command("finish-task", self.task))

    async def _receive(self):
        while True:
            message = await self.socket.recv()
            if isinstance(message, bytes):
                if self.first_audio is None:
                    self.first_audio = time.perf_counter()
                self.pcm += message
                continue
            header = message.get("header") or {}
            if header.get("event") == "task-started":
                self.started.set()
            elif header.get("event") == "task-finished":
                return
            elif header.get("event") == "task-failed":
                raise RuntimeError(header.get("error_code") or "task-failed")

    async def finish(self, timeout):
        if self.sender is None:
            return
        if self.timer is not None:
            self.timer.cancel()
        self._drain(force=True)
        self.queue.put_nowait(None)
        await asyncio.wait_for(asyncio.gather(self.sender, self.receiver), timeout)

    async def close(self):
        for task in (self.timer, self.sender, self.receiver):
            if task is not None and not task.done():
                task.cancel()
        await self.socket.close()


async def stream(socket, pcm, marks, done):
    """Send like a live microphone: room noise, the user's words, then noise until the reply ends."""
    from annie_adapters.realtime import audio_append

    words = pcm + noise_frame()[:(-len(pcm)) % FRAME]
    frames = ([noise_frame() for _ in range(LEAD_FRAMES)]
              + [words[i:i + FRAME] for i in range(0, len(words), FRAME)])
    loop = asyncio.get_running_loop()
    start, n = loop.time(), 0
    while not done.is_set():
        await asyncio.sleep(max(0.0, start + n * 0.02 - loop.time()))
        await socket.send(audio_append(frames[n] if n < len(frames) else noise_frame()))
        n += 1
        if n == len(frames):
            marks["end"] = time.perf_counter()


def scrub(text, voices):
    for voice in voices.values():
        if voice:
            text = text.replace(voice, masked(voice))
    return text


async def turn(cfg, path, pcm, voices):
    from annie_adapters.realtime import QwenRealtimeSocket
    from annie_adapters.tts import QwenTtsSocket, command

    record = {"path": path, "at": {}, "text": "", "error": None, "other": {}}
    audio, marks, done = bytearray(), {}, asyncio.Event()
    socket, speech, sender = QwenRealtimeSocket(cfg["realtime_url"], cfg["key"]), None, None
    try:
        await socket.open()
        session = {"modalities": ["text"] if path == "A" else ["text", "audio"],
                   "input_audio_format": "pcm", "turn_detection": {"type": "smart_turn_v2"},
                   "max_history_turns": 8, "instructions": instructions(cfg["persona"], path == "B")}
        if path == "B" and voices["B"]:
            session["voice"] = voices["B"]
        await socket.send({"type": "session.update", "session": session})
        while True:
            event = await asyncio.wait_for(socket.recv(), 10)
            if event.get("type") == "session.updated":
                record["session"] = {key: value for key, value in (event.get("session") or {}).items()
                                     if key not in ("instructions", "tools", "voice")}
                break
            if event.get("type") == "error":
                raise RuntimeError(json.dumps(event.get("error"), ensure_ascii=False))
        if path == "A":
            tts = cfg["persona"]["tts"]
            speech = Speech(await QwenTtsSocket(cfg["tts_url"], cfg["key"]).open(), lambda task: command(
                "run-task", task, voice=voices["A"], sample_rate=REPLY_RATE, instruction=tts["instruction"],
                rate=tts.get("rate"), pitch=tts.get("pitch"), volume=tts.get("volume"), language_hint="zh"),
                cfg["cuts"])
        sender = asyncio.create_task(stream(socket, pcm, marks, done))
        deadline = time.perf_counter() + len(pcm) / (USER_RATE * 2) + LEAD_FRAMES * 0.02 + MAX_WAIT_S
        at = record["at"]
        while True:
            event = await asyncio.wait_for(socket.recv(), max(0.1, deadline - time.perf_counter()))
            kind, now = event.get("type", ""), time.perf_counter()
            if kind == "input_audio_buffer.speech_started":
                at.setdefault("speech_started", now)
            elif kind == "input_audio_buffer.speech_stopped":
                at.setdefault("speech_stopped", now)
            elif kind == "response.created":
                at.setdefault("response_created", now)
            elif kind in ("response.text.delta", "response.audio_transcript.delta"):
                at.setdefault("first_text", now)
                record["text"] += event.get("delta") or ""
                if speech is not None:
                    speech.feed(event.get("delta") or "")
            elif kind == "response.audio.delta":
                at.setdefault("first_audio", now)
                audio += base64.b64decode(event.get("delta") or "")
            elif kind == "response.done":
                at["done"] = now
                record["status"] = (event.get("response") or {}).get("status")
                break
            elif kind == "error":
                raise RuntimeError(json.dumps(event.get("error"), ensure_ascii=False))
            else:
                record["other"][kind] = record["other"].get(kind, 0) + 1
        if speech is not None:
            await speech.finish(30)
            if speech.first_audio is not None:
                at["first_audio"] = speech.first_audio
            audio = speech.pcm
    except Exception as exc:
        record["error"] = scrub(f"{type(exc).__name__}: {exc}", voices)[:300]
    finally:
        done.set()
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        if speech is not None:
            await speech.close()
        await socket.close()
    end = marks.get("end")
    record["ms"] = {name: round((value - end) * 1000) for name, value in record.pop("at").items()
                    } if end else {}
    return record, bytes(audio)


def timeline(user_pcm, wait_ms, reply):
    """The user's words, the measured wait, then the reply: what the person in front hears."""
    user = np.frombuffer(user_pcm, dtype=np.int16).astype(np.float32)
    user = np.interp(np.arange(0, len(user), USER_RATE / REPLY_RATE), np.arange(len(user)), user)
    gap = np.zeros(max(0, round(wait_ms * REPLY_RATE / 1000)), dtype=np.float32)
    spoken = np.frombuffer(reply, dtype=np.int16).astype(np.float32)
    return np.concatenate([user, gap, spoken]).astype(np.int16).tobytes()


def report(records, args, cfg, voices):
    lines = [f"# 直接出声对比：{args.label}", "",
             f"- 时间（北京）：{datetime.now(BEIJING):%Y-%m-%d %H:%M}；实时模型 {cfg['model']}；"
             f"每句 {args.rounds} 遍；时间都从“最后一帧用户语音发出”算起",
             f"- A 的 TTS 音色 {masked(voices['A'])}；B 的实时音色 {masked(voices['B'])}", ""]
    for path, title in (("A", "A 现状：文字 + 独立 TTS"), ("B", "B 实时模型直接出声")):
        rows = [row for row in records if row["path"] == path]
        if not rows:
            continue
        failed = [row for row in rows if row["error"] or "first_audio" not in row["ms"]]
        lines += [f"## {title}", "", f"- 共 {len(rows)} 次，出声 {len(rows) - len(failed)} 次"]
        for key, label in METRICS:
            values = sorted(row["ms"][key] for row in rows if key in row["ms"])
            if values:
                lines.append(f"- {label}：中位 {round(statistics.median(values))} ms，"
                             f"最快 {values[0]}，最慢 {values[-1]}（{len(values)} 次）")
        spoken = [row for row in rows if row["audio_s"] and row["text"]]
        if spoken:
            rate = sum(len(row["text"]) for row in spoken) / sum(row["audio_s"] for row in spoken)
            lines.append(f"- 回复平均 {statistics.mean(row['audio_s'] for row in spoken):.1f} 秒，"
                         f"语速约 {rate:.1f} 字/秒")
        for row in failed:
            lines.append(f"- 没出声：第 {row['n']} 句第 {row['round']} 遍 {row['error'] or row.get('status')}")
        lines.append("")
    lines += ["## 逐句（首段音频 ms，各遍）", ""]
    for n, text in enumerate(UTTERANCES, 1):
        cells = [f"{path} " + "/".join(str(row["ms"].get("first_audio", "—")) for row in records
                                       if row["n"] == n and row["path"] == path)
                 for path in ("A", "B") if any(row["path"] == path for row in records)]
        if any(row["n"] == n for row in records):
            lines.append(f"- {n}. {text}｜" + "｜".join(cells))
    lines += ["", "## 回复原文（第 1 遍）", ""]
    for row in records:
        if row["round"] == 1:
            lines.append(f"- {row['n']}{row['path']}（{row['audio_s']} 秒）：{row['text'] or '（无）'}")
    sessions = {row["path"]: row.get("session") for row in records if row.get("session")}
    lines += ["", "## 会话配置（服务端回报，去掉提示词和音色）", ""]
    lines += [f"- {path}：{json.dumps(value, ensure_ascii=False)[:600]}" for path, value in sessions.items()]
    return lines


async def run(args):
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = setup()
    voices = {"A": cloned_voice(cfg["persona"]["tts"]["voice"]), "B": None}
    if args.only != "A":
        try:
            voices["B"] = await realtime_voice(cfg, cfg["persona"]["tts"]["voice"])
        except Exception as exc:
            print(f"为实时模型复刻音色失败，B 用模型默认音色：{type(exc).__name__}: {str(exc)[:200]}")
    print(f"音色：A {masked(voices['A'])}，B {masked(voices['B'])}", flush=True)
    clips = await user_audio(cfg)
    paths = [args.only] if args.only else ["A", "B"]
    records = []
    with (OUT / f"{args.label}.jsonl").open("w") as out:
        for round_ in range(1, args.rounds + 1):
            for n in range(1, min(len(UTTERANCES), args.limit or len(UTTERANCES)) + 1):
                for path in paths:
                    record, audio = await turn(cfg, path, clips[n - 1], voices)
                    record.update(n=n, round=round_, audio_s=round(len(audio) / (REPLY_RATE * 2), 2))
                    records.append(record)
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out.flush()
                    if audio:
                        write_wav(OUT / f"{args.label}-{n}-{path}-第{round_}遍.wav", audio, REPLY_RATE)
                        if round_ == 1 and "first_audio" in record["ms"]:
                            write_wav(OUT / f"{args.label}-{n}-{path}-对话节奏.wav",
                                      timeline(clips[n - 1], record["ms"]["first_audio"], audio), REPLY_RATE)
                    print(f"{path} 第{round_}遍 第{n}句：{record['ms']} {record['error'] or ''}"
                          f" {record['text'][:30]}", flush=True)
                    await asyncio.sleep(1)
    path = OUT / f"{args.label}.md"
    path.write_text("\n".join(report(records, args, cfg, voices)) + "\n")
    print(f"已写入 {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=2, help="每句跑几遍，默认 2")
    parser.add_argument("--only", choices=("A", "B"), help="只跑一条路")
    parser.add_argument("--limit", type=int, default=0, help="只跑前几句")
    parser.add_argument("--label", default="第一版")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
