#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Qwen-Audio-TTS -> PCM -> Allosaurus phone timestamps -> 6-viseme mapper
-> WebSocket script_id POC.

POC goals:
1) Use Alibaba Cloud Qwen-Audio-TTS with streaming PCM output.
2) Use g2pW to derive the expected Mandarin pronunciation/viseme sequence from text.
3) Use Allosaurus to obtain approximate phone timestamps from the synthesized PCM.
4) Align the audio-derived vowel sequence with the text-derived expected viseme sequence.
5) Replay the timeline in real time and send script_id events to the embedded WebSocket.

This is intentionally a validation POC. It does NOT modify the existing robot project.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import wave
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from dotenv import load_dotenv


DEFAULT_TEXT = (
    "阿。哦。鹅。衣。乌。鱼。"
    "阿姨今天和我一起出去，我觉得非常有趣。"
)

DEFAULT_WS_URL = "ws://192.168.1.47:8090/robot/room/reb_sam"
SAMPLE_RATE = 22050
CHANNELS = 1
SAMPLE_WIDTH = 2  # PCM s16le

SCRIPT_IDS = {
    "a": 1,
    "o": 2,
    "e": 3,
    "i": 4,
    "u": 5,
    "ü": 6,
}

# Phones from Allosaurus are IPA-ish.  We only care about the six mouth classes.
# Consonants are intentionally ignored in this POC because the hardware only
# has six vowel mouth scripts.
IPA_TO_VISEME_GROUPS = {
    "ü": set("yʏøœɥ"),
    "i": set("iɪɨɿʅ"),
    "u": set("uʊʉ"),
    "o": set("oɔɒɵ"),
    "a": set("aɑæɐä"),
    "e": set("eəɛɘɤɜʌɯә"),
}


@dataclass
class PhoneEvent:
    start_s: float
    duration_s: float
    phone: str
    detected_viseme: Optional[str]


@dataclass
class VisemeEvent:
    seq: int
    viseme: str
    script_id: int
    start_sample: int
    duration_samples: int
    source_phone: Optional[str] = None
    detected_viseme: Optional[str] = None

    @property
    def start_ms(self) -> float:
        return self.start_sample * 1000.0 / SAMPLE_RATE

    @property
    def duration_ms(self) -> float:
        return self.duration_samples * 1000.0 / SAMPLE_RATE


def log(tag: str, message: str) -> None:
    print(f"[{tag:<8}] {message}", flush=True)


def load_environment(env_path: str) -> None:
    p = Path(env_path)
    if p.exists():
        load_dotenv(p, override=False)
        log("ENV", f"loaded {p}")
    else:
        load_dotenv(override=False)
        log("ENV", f"{p} not found; using process environment")


def strip_tone(pinyin: str) -> str:
    return re.sub(r"[1-5]$", "", pinyin.strip().lower()).replace("u:", "ü").replace("v", "ü")


INITIALS = (
    "zh", "ch", "sh",
    "b", "p", "m", "f",
    "d", "t", "n", "l",
    "g", "k", "h",
    "j", "q", "x",
    "r", "z", "c", "s",
)


def pinyin_syllable_to_visemes(pinyin: str) -> List[str]:
    """Convert one Pinyin syllable into a small sequence of visible vowel targets.

    This is deliberately visual/engineering-oriented, not a linguistic phoneme
    transcriber. It preserves the transitions that are most useful to a six-mouth rig.
    """
    s = strip_tone(pinyin)
    if not s:
        return []

    # Common orthographic y/w syllables -> actual vowel-ish sequence.
    y_map = {
        "yi": "i", "ya": "ia", "yao": "iao", "ye": "ie", "you": "iou",
        "yan": "ian", "yang": "iang", "yin": "in", "ying": "ing",
        "yong": "iong",
        "yu": "ü", "yue": "üe", "yuan": "üan", "yun": "ün",
    }
    w_map = {
        "wu": "u", "wa": "ua", "wo": "uo", "wai": "uai", "wei": "uei",
        "wan": "uan", "wang": "uang", "wen": "uen", "weng": "ueng",
    }
    if s in y_map:
        final = y_map[s]
    elif s in w_map:
        final = w_map[s]
    else:
        initial = ""
        final = s
        for ini in INITIALS:
            if s.startswith(ini):
                initial = ini
                final = s[len(ini):]
                break

        # In standard Pinyin, j/q/x + u is actually ü.
        if initial in {"j", "q", "x"} and final.startswith("u"):
            final = "ü" + final[1:]

        # Expand common spelling contractions; this gives a better mouth transition.
        if final == "iu":
            final = "iou"
        elif final == "ui":
            final = "uei"
        elif final == "un":
            final = "uen"

    # Special finals where written "i" is an apical vowel.  The I mouth is still
    # a useful approximation for a six-mouth mechanism.
    if s in {"zhi", "chi", "shi", "ri", "zi", "ci", "si"}:
        return ["i"]

    result: List[str] = []
    for ch in final:
        if ch in {"a", "o", "e", "i", "u", "ü"}:
            if not result or result[-1] != ch:
                result.append(ch)
    return result


def get_pinyin_g2pw(text: str) -> List[str]:
    from g2pw import G2PWConverter
    conv = G2PWConverter(style="pinyin", enable_non_tradional_chinese=True)
    result = conv(text)
    # g2pW returns list-of-list even for one sentence in current examples.
    if result and isinstance(result[0], list):
        result = result[0]
    return [x for x in result if x]


def get_pinyin_pypinyin(text: str) -> List[str]:
    from pypinyin import Style, lazy_pinyin
    return lazy_pinyin(
        text,
        style=Style.TONE3,
        neutral_tone_with_five=True,
        errors="ignore",
    )


def text_to_expected_visemes(text: str, g2p_engine: str = "g2pw") -> Tuple[List[str], List[str]]:
    pinyins: List[str]
    if g2p_engine == "g2pw":
        try:
            pinyins = get_pinyin_g2pw(text)
        except Exception as exc:
            log("G2P", f"g2pW failed ({exc!r}); falling back to pypinyin")
            pinyins = get_pinyin_pypinyin(text)
    else:
        pinyins = get_pinyin_pypinyin(text)

    visemes: List[str] = []
    for py in pinyins:
        visemes.extend(pinyin_syllable_to_visemes(py))

    # Remove exact adjacent duplicates to avoid useless repeated script triggers.
    compact: List[str] = []
    for v in visemes:
        if not compact or compact[-1] != v:
            compact.append(v)
    return pinyins, compact


def pcm_bytes_to_wav_bytes(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    bio = BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    bio.seek(0)
    return bio.getvalue()


def save_wav(path: Path, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def normalize_phone(phone: str) -> str:
    # Strip combining diacritics but preserve IPA base characters.
    n = unicodedata.normalize("NFD", phone)
    return "".join(ch for ch in n if unicodedata.category(ch) != "Mn")


def phone_to_viseme(phone: str) -> Optional[str]:
    p = normalize_phone(phone)
    # Priority matters for mixed/combined phone strings.
    for viseme in ("ü", "i", "u", "o", "a", "e"):
        if any(ch in IPA_TO_VISEME_GROUPS[viseme] for ch in p):
            return viseme
    return None


def parse_allosaurus_timestamps(raw: str) -> List[PhoneEvent]:
    events: List[PhoneEvent] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            start_s = float(parts[0])
            duration_s = float(parts[1])
        except ValueError:
            continue
        phone = parts[2]
        events.append(
            PhoneEvent(
                start_s=start_s,
                duration_s=duration_s,
                phone=phone,
                detected_viseme=phone_to_viseme(phone),
            )
        )
    return events


def recognize_phones_allosaurus(wav_bytes: bytes, lang: str = "ipa") -> List[PhoneEvent]:
    from allosaurus.app import read_recognizer

    log("PHONE", "loading Allosaurus model (first run may download the model)...")
    model = read_recognizer()

    # Allosaurus' documented Python API expects an audio file path.
    # Write the in-memory WAV to a temporary file instead of passing BytesIO.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(wav_bytes)
            tmp.flush()
            tmp_path = tmp.name

        log("PHONE", f"recognizing temporary WAV with lang={lang}")
        raw = model.recognize(tmp_path, lang_id=lang, timestamp=True, emit=1.0)
        return parse_allosaurus_timestamps(raw)
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


def align_sequences(
    expected: Sequence[str],
    detected: Sequence[str],
) -> List[Tuple[Optional[int], Optional[int]]]:
    """Needleman-Wunsch alignment.

    Returns pairs (expected_index, detected_index). Either side can be None.
    """
    n, m = len(expected), len(detected)
    match_score = 4
    mismatch_score = -2
    gap_score = -2

    dp = [[0] * (m + 1) for _ in range(n + 1)]
    back = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i * gap_score
        back[i][0] = "U"
    for j in range(1, m + 1):
        dp[0][j] = j * gap_score
        back[0][j] = "L"

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            diag = dp[i - 1][j - 1] + (
                match_score if expected[i - 1] == detected[j - 1] else mismatch_score
            )
            up = dp[i - 1][j] + gap_score
            left = dp[i][j - 1] + gap_score
            best = max(diag, up, left)
            dp[i][j] = best
            back[i][j] = "D" if best == diag else ("U" if best == up else "L")

    pairs: List[Tuple[Optional[int], Optional[int]]] = []
    i, j = n, m
    while i > 0 or j > 0:
        direction = back[i][j]
        if i > 0 and j > 0 and direction == "D":
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif i > 0 and (j == 0 or direction == "U"):
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    pairs.reverse()
    return pairs


def interpolate_expected_times(
    expected_count: int,
    matched_times: dict[int, float],
    audio_duration_s: float,
) -> List[float]:
    if expected_count == 0:
        return []
    if not matched_times:
        # Last-resort fallback: evenly spaced events across the utterance.
        start = min(0.12, audio_duration_s * 0.05)
        end = max(start, audio_duration_s - 0.08)
        if expected_count == 1:
            return [start]
        step = (end - start) / max(1, expected_count - 1)
        return [start + i * step for i in range(expected_count)]

    result: List[Optional[float]] = [None] * expected_count
    for idx, t in matched_times.items():
        if 0 <= idx < expected_count:
            result[idx] = t

    anchors = sorted((idx, t) for idx, t in matched_times.items())
    first_idx, first_t = anchors[0]
    if first_idx > 0:
        start_t = max(0.0, first_t - min(0.4, first_t))
        step = (first_t - start_t) / (first_idx + 1)
        for k in range(first_idx):
            result[k] = start_t + step * (k + 1)

    for (left_idx, left_t), (right_idx, right_t) in zip(anchors, anchors[1:]):
        gap = right_idx - left_idx
        if gap > 1:
            step = (right_t - left_t) / gap
            for k in range(left_idx + 1, right_idx):
                result[k] = left_t + step * (k - left_idx)

    last_idx, last_t = anchors[-1]
    if last_idx < expected_count - 1:
        end_t = max(last_t, audio_duration_s - 0.06)
        count = expected_count - 1 - last_idx
        step = (end_t - last_t) / max(1, count)
        for k in range(last_idx + 1, expected_count):
            result[k] = last_t + step * (k - last_idx)

    # Safety for pathological alignments.
    fallback_step = audio_duration_s / max(1, expected_count)
    for i, value in enumerate(result):
        if value is None:
            result[i] = i * fallback_step

    # Enforce non-decreasing times.
    out = [float(v) for v in result]
    for i in range(1, len(out)):
        if out[i] < out[i - 1]:
            out[i] = out[i - 1]
    return out


def build_viseme_timeline(
    expected: Sequence[str],
    phones: Sequence[PhoneEvent],
    audio_duration_s: float,
) -> List[VisemeEvent]:
    vowel_phones = [p for p in phones if p.detected_viseme is not None]
    detected = [p.detected_viseme for p in vowel_phones if p.detected_viseme]

    pairs = align_sequences(expected, detected)
    matched_times: dict[int, float] = {}
    matched_phone: dict[int, PhoneEvent] = {}

    for exp_idx, det_idx in pairs:
        if exp_idx is None or det_idx is None:
            continue
        phone = vowel_phones[det_idx]
        # Exact class matches are strongest.  Mismatches are still useful as timing anchors
        # because Allosaurus may confuse nearby vowels while the text tells us WHAT to play.
        matched_times[exp_idx] = phone.start_s
        matched_phone[exp_idx] = phone

    times = interpolate_expected_times(len(expected), matched_times, audio_duration_s)

    raw_events: List[VisemeEvent] = []
    for i, (viseme, start_s) in enumerate(zip(expected, times)):
        next_s = times[i + 1] if i + 1 < len(times) else audio_duration_s
        if next_s <= start_s:
            next_s = start_s + 0.06

        start_sample = max(0, int(round(start_s * SAMPLE_RATE)))
        end_sample = min(
            int(round(audio_duration_s * SAMPLE_RATE)),
            max(start_sample + int(0.04 * SAMPLE_RATE), int(round(next_s * SAMPLE_RATE))),
        )

        phone = matched_phone.get(i)
        raw_events.append(
            VisemeEvent(
                seq=i,
                viseme=viseme,
                script_id=SCRIPT_IDS[viseme],
                start_sample=start_sample,
                duration_samples=max(1, end_sample - start_sample),
                source_phone=phone.phone if phone else None,
                detected_viseme=phone.detected_viseme if phone else None,
            )
        )

    # Collapse repeated adjacent mouth shapes.  Keep the first timestamp and extend duration.
    compact: List[VisemeEvent] = []
    for e in raw_events:
        if compact and compact[-1].viseme == e.viseme:
            prev = compact[-1]
            prev_end = max(
                prev.start_sample + prev.duration_samples,
                e.start_sample + e.duration_samples,
            )
            prev.duration_samples = prev_end - prev.start_sample
        else:
            e.seq = len(compact)
            compact.append(e)

    return compact


class TTSCollector:
    def __init__(self, pcm_path: Path):
        self.pcm_path = pcm_path
        self.pcm = bytearray()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.error: Optional[str] = None
        self._fh = None

    def build_callback(self):
        from dashscope.audio.tts_v2 import ResultCallback

        owner = self

        class Callback(ResultCallback):
            def on_open(self):
                owner._fh = open(owner.pcm_path, "wb")
                log("TTS", "DashScope WebSocket opened")

            def on_data(self, data: bytes) -> None:
                with owner.lock:
                    owner.pcm.extend(data)
                    total_samples = len(owner.pcm) // SAMPLE_WIDTH
                if owner._fh:
                    owner._fh.write(data)
                    owner._fh.flush()
                log(
                    "TTS",
                    f"PCM chunk bytes={len(data)} "
                    f"total_samples={total_samples} "
                    f"timeline={total_samples / SAMPLE_RATE:.3f}s",
                )

            def on_event(self, message):
                try:
                    d = json.loads(message)
                    out = d.get("payload", {}).get("output", {})
                    et = out.get("type")
                    if et:
                        log("TTS_EVT", f"{et}: {out.get('original_text', '')}")
                except Exception:
                    pass

            def on_complete(self):
                log("TTS", "synthesis complete")
                owner.done.set()

            def on_error(self, message: str):
                owner.error = message
                log("TTS_ERR", message)
                owner.done.set()

            def on_close(self):
                if owner._fh:
                    owner._fh.close()
                    owner._fh = None
                log("TTS", "DashScope WebSocket closed")

        return Callback()


def split_text_for_streaming(text: str) -> List[str]:
    chunks = [x for x in re.split(r"(?<=[。！？；，,.!?])", text) if x.strip()]
    if not chunks:
        return [text]
    return chunks


def synthesize_qwen_streaming(text: str, output_dir: Path) -> bytes:
    import dashscope
    from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

    api_key = os.getenv("DASHSCOPE_API_KEY")
    workspace_id = os.getenv("ALIYUN_WORKSPACE_ID")
    model = os.getenv("TTS_MODEL", "qwen-audio-3.0-tts-flash")
    voice = os.getenv("TTS_VOICE")
    instruction = os.getenv("TTS_INSTRUCTION", "").strip()
    volume = int(float(os.getenv("TTS_VOLUME", "50")))
    speech_rate = float(os.getenv("TTS_RATE", "1"))
    pitch_rate = float(os.getenv("TTS_PITCH", "1"))

    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY is missing")
    if not workspace_id:
        raise RuntimeError("ALIYUN_WORKSPACE_ID is missing")
    if not voice:
        raise RuntimeError("TTS_VOICE is missing")

    dashscope.api_key = api_key
    dashscope.base_websocket_api_url = (
        f"wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )

    pcm_path = output_dir / "qwen_tts_output.pcm"
    collector = TTSCollector(pcm_path)
    callback = collector.build_callback()

    kwargs = dict(
        model=model,
        voice=voice,
        format=AudioFormat.PCM_22050HZ_MONO_16BIT,
        volume=volume,
        speech_rate=speech_rate,
        pitch_rate=pitch_rate,
        callback=callback,
    )
    if instruction:
        kwargs["instruction"] = instruction

    log("TTS", f"model={model}")
    log("TTS", f"voice={voice[:30]}{'...' if len(voice) > 30 else ''}")
    log("TTS", f"format=PCM {SAMPLE_RATE}Hz mono s16le")

    synthesizer = SpeechSynthesizer(**kwargs)

    for chunk in split_text_for_streaming(text):
        log("TTS_TX", chunk)
        synthesizer.streaming_call(chunk)
        time.sleep(0.08)

    synthesizer.streaming_complete()
    collector.done.wait(timeout=30)

    if collector.error:
        raise RuntimeError(f"TTS failed: {collector.error}")

    pcm = bytes(collector.pcm)
    if not pcm:
        raise RuntimeError("TTS returned no PCM")

    log(
        "TTS",
        f"done bytes={len(pcm)}, samples={len(pcm)//SAMPLE_WIDTH}, "
        f"duration={len(pcm)/SAMPLE_WIDTH/SAMPLE_RATE:.3f}s",
    )
    try:
        delay = synthesizer.get_first_package_delay()
        log("METRIC", f"TTS first-package delay={delay} ms")
    except Exception:
        pass

    return pcm


class WebSocketSender:
    def __init__(self, url: str, payload_mode: str = "full-json"):
        self.url = url
        self.payload_mode = payload_mode
        self.ws = None

    def connect(self):
        import websocket
        log("WS", f"connecting {self.url}")
        self.ws = websocket.create_connection(self.url, timeout=5)
        try:
            self.ws.settimeout(0.1)
        except Exception:
            pass
        log("WS", "connected")

    def close(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None
            log("WS", "closed")

    def _send_obj(self, obj: dict):
        if not self.ws:
            return
        if self.payload_mode == "plain":
            payload = str(obj.get("script_id", ""))
        elif self.payload_mode == "simple-json":
            payload = json.dumps(
                {"script_id": obj.get("script_id")},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:
            payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        self.ws.send(payload)
        log("WS_TX", payload)

    def send_start(self, stream_id: str):
        if self.payload_mode != "full-json":
            return
        self._send_obj(
            {
                "type": "viseme_stream_start",
                "sender": "qwen_tts_viseme_poc",
                "initiator": "tts_viseme",
                "stream_id": stream_id,
                "sample_rate": SAMPLE_RATE,
                "pcm_format": "s16le",
                "script_map": SCRIPT_IDS,
            }
        )

    def send_event(self, stream_id: str, event: VisemeEvent):
        self._send_obj(
            {
                "type": "mouth_script",
                "sender": "qwen_tts_viseme_poc",
                "initiator": "tts_viseme",
                "stream_id": stream_id,
                "seq": event.seq,
                "script_id": event.script_id,
                "viseme": event.viseme,
                "start_sample": event.start_sample,
                "duration_samples": event.duration_samples,
                "start_ms": round(event.start_ms, 2),
                "duration_ms": round(event.duration_ms, 2),
                "source_phone": event.source_phone,
                "detected_viseme": event.detected_viseme,
            }
        )

    def send_end(self, stream_id: str):
        if self.payload_mode != "full-json":
            return
        self._send_obj(
            {
                "type": "viseme_stream_end",
                "sender": "qwen_tts_viseme_poc",
                "initiator": "tts_viseme",
                "stream_id": stream_id,
            }
        )


def print_phone_debug(phones: Sequence[PhoneEvent]) -> None:
    log("PHONE", f"recognized {len(phones)} phone events")
    for p in phones:
        mapped = p.detected_viseme or "-"
        print(
            f"  {p.start_s:7.3f}s +{p.duration_s:6.3f}s "
            f"phone={p.phone:<8} -> {mapped}",
            flush=True,
        )


def print_timeline(events: Sequence[VisemeEvent]) -> None:
    print("\n=== 6-VISEME TIMELINE ===")
    for e in events:
        print(
            f"#{e.seq:02d} "
            f"{e.viseme:>2} -> script_id={e.script_id} "
            f"start={e.start_ms:8.2f}ms "
            f"sample={e.start_sample:7d} "
            f"duration={e.duration_ms:7.2f}ms "
            f"phone={e.source_phone or '-'}"
        )
    print("=========================\n")


def save_timeline_json(path: Path, stream_id: str, events: Sequence[VisemeEvent]) -> None:
    obj = {
        "stream_id": stream_id,
        "sample_rate": SAMPLE_RATE,
        "script_map": SCRIPT_IDS,
        "events": [
            {
                "seq": e.seq,
                "viseme": e.viseme,
                "script_id": e.script_id,
                "start_sample": e.start_sample,
                "duration_samples": e.duration_samples,
                "start_ms": round(e.start_ms, 3),
                "duration_ms": round(e.duration_ms, 3),
                "source_phone": e.source_phone,
                "detected_viseme": e.detected_viseme,
            }
            for e in events
        ],
    }
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def replay_and_send(
    pcm: bytes,
    events: Sequence[VisemeEvent],
    sender: WebSocketSender,
    stream_id: str,
    play_audio: bool,
    lead_ms: float,
) -> None:
    """Real-time replay. Events are scheduled against the PCM sample timeline.

    This POC uses a wall-clock playback origin. The payload itself also carries
    start_sample, which is what the final embedded integration should ultimately
    use as its authoritative clock.
    """
    audio_duration_s = len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE

    player_proc = None
    temp_play_wav = None
    if play_audio:
        try:
            import subprocess
            if sys.platform == "darwin":
                fd, temp_play_wav = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                save_wav(Path(temp_play_wav), pcm)
                player_proc = subprocess.Popen(
                    ["afplay", temp_play_wav],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                log("PLAY", f"macOS afplay started ({audio_duration_s:.3f}s)")
            else:
                log("PLAY", "local playback is only built-in on macOS in V2; scheduling WS only")
        except Exception as exc:
            log("PLAY", f"local playback unavailable: {exc!r}; scheduling WS only")
            player_proc = None

    sender.send_start(stream_id)
    t0 = time.perf_counter()
    lead_s = lead_ms / 1000.0

    for e in events:
        target_s = max(0.0, e.start_sample / SAMPLE_RATE - lead_s)
        while True:
            remaining = target_s - (time.perf_counter() - t0)
            if remaining <= 0:
                break
            time.sleep(min(0.01, remaining))

        sender.send_event(stream_id, e)
        log(
            "TRIGGER",
            f"script_id={e.script_id} viseme={e.viseme} "
            f"target={e.start_ms:.2f}ms sample={e.start_sample}",
        )

    elapsed = time.perf_counter() - t0
    remaining = audio_duration_s - elapsed
    if remaining > 0:
        time.sleep(remaining)

    if player_proc is not None:
        try:
            player_proc.wait(timeout=max(1.0, audio_duration_s + 2.0))
        except Exception:
            pass
    if temp_play_wav:
        try:
            Path(temp_play_wav).unlink(missing_ok=True)
        except Exception:
            pass

    sender.send_end(stream_id)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Qwen TTS -> Allosaurus -> 6-viseme -> WebSocket POC"
    )
    p.add_argument("--env", default=".env", help="path to existing .env")
    p.add_argument("--text", default=DEFAULT_TEXT, help="fixed test text")
    p.add_argument("--ws", default=DEFAULT_WS_URL, help="embedded WebSocket URL")
    p.add_argument(
        "--g2p",
        choices=["g2pw", "pypinyin"],
        default="pypinyin",
        help="text pronunciation frontend",
    )
    p.add_argument(
        "--payload-mode",
        choices=["full-json", "simple-json", "plain"],
        default="full-json",
        help="WebSocket payload format",
    )
    p.add_argument(
        "--lead-ms",
        type=float,
        default=0.0,
        help="send mouth trigger this many ms before local PCM wall-clock (POC tuning)",
    )
    p.add_argument(
        "--no-play",
        action="store_true",
        help="do not play synthesized PCM locally",
    )
    p.add_argument(
        "--no-ws",
        action="store_true",
        help="do not connect/send to WebSocket; only generate timeline",
    )
    p.add_argument(
        "--output-dir",
        default="output",
        help="directory for PCM/WAV/timeline files",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_environment(args.env)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n=== CONFIG ===")
    print(f"TEXT       : {args.text}")
    print(f"WS         : {args.ws}")
    print(f"SCRIPTS    : {SCRIPT_IDS}")
    print(f"SAMPLE RATE: {SAMPLE_RATE}")
    print("==============\n")

    # 1) Text -> expected six-mouth sequence
    pinyins, expected = text_to_expected_visemes(args.text, args.g2p)
    log("G2P", " ".join(pinyins))
    log("EXPECTED", " ".join(expected))
    if not expected:
        raise RuntimeError("No visemes could be derived from input text")

    # 2) Qwen-Audio-TTS streaming PCM
    pcm = synthesize_qwen_streaming(args.text, output_dir)
    wav_path = output_dir / "qwen_tts_output.wav"
    save_wav(wav_path, pcm)
    log("FILE", f"saved {wav_path}")

    # 3) PCM -> phone + approximate time
    wav_bytes = pcm_bytes_to_wav_bytes(pcm)
    phones = recognize_phones_allosaurus(wav_bytes, lang="ipa")
    print_phone_debug(phones)

    # 4) Text WHAT + audio WHEN -> six-viseme timeline
    duration_s = len(pcm) / SAMPLE_WIDTH / SAMPLE_RATE
    events = build_viseme_timeline(expected, phones, duration_s)
    if not events:
        raise RuntimeError("No viseme events produced")

    stream_id = f"viseme-poc-{uuid.uuid4().hex[:10]}"
    print_timeline(events)
    timeline_path = output_dir / "viseme_timeline.json"
    save_timeline_json(timeline_path, stream_id, events)
    log("FILE", f"saved {timeline_path}")

    # 5) Real-time replay + WebSocket script events
    if args.no_ws:
        log("WS", "--no-ws enabled; not sending")
        return 0

    sender = WebSocketSender(args.ws, args.payload_mode)
    try:
        sender.connect()
        replay_and_send(
            pcm=pcm,
            events=events,
            sender=sender,
            stream_id=stream_id,
            play_audio=not args.no_play,
            lead_ms=args.lead_ms,
        )
    finally:
        sender.close()

    log("DONE", "POC completed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)
    except Exception as exc:
        log("FATAL", repr(exc))
        raise
