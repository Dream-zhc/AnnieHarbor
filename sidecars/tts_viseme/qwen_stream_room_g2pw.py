#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TTS Viseme room listener.

Listens to reb_sam llm_delta and reb_tts real PCM. Does not call any TTS API.
Realtime pronunciation uses pypinyin. g2pW is an optional background worker.
One Mandarin syllable -> one primary mouth.
"""

from __future__ import annotations

import argparse
import base64
import heapq
import importlib.util
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from dotenv import load_dotenv
from pypinyin import Style, pinyin as pypinyin_fn

from forced_aligner_runtime import AlignedChar, QwenForcedAlignerRuntime

DEFAULT_SAM_URL = "ws://192.168.1.47:8090/robot/roomv2/reb_sam"
DEFAULT_TTS_URL = "ws://192.168.1.47:8090/robot/roomv2/reb_tts"
DEFAULT_TEXT = "阿哦鹅衣乌鱼。阿姨今天和我一起出去，我觉得非常有趣。"
ROOM_CONTROL_TYPES = frozenset({
    "welcome",
    "ack",
    "subscribe",
    "unsubscribe",
    "ping",
    "pong",
    "error",
    "presence",
})

SAMPLE_RATE = 22050
SAMPLE_WIDTH = 2
ONLINE_EVAL_MIN_MS = 24
ONLINE_HOLDBACK_MS = 100
ONLINE_MIN_GAP_MS = 40
FORCED_ALIGN_EVAL_INTERVAL_MS = int(os.getenv("TTS_VISEME_ALIGN_EVAL_INTERVAL_MS") or "120")
FORCED_ALIGN_HOLDBACK_MS = int(os.getenv("TTS_VISEME_ALIGN_HOLDBACK_MS") or "80")
FORCED_ALIGN_FIRST_WINDOW_MS = int(os.getenv("TTS_VISEME_ALIGN_FIRST_WINDOW_MS") or "480")
FORCED_ALIGN_MAX_TEXT_CHARS = int(os.getenv("TTS_VISEME_ALIGN_MAX_TEXT_CHARS") or "24")
FORCED_ALIGN_CHARS_PER_SEC = float(os.getenv("TTS_VISEME_ALIGN_CHARS_PER_SEC") or "5.0")
FORCED_ALIGN_MAX_OVERLAP_MS = int(os.getenv("TTS_VISEME_ALIGN_MAX_OVERLAP_MS") or "120")
IDLE_MIN_MS = int(os.getenv("TTS_VISEME_IDLE_MIN_MS") or "240")
# Online alignment must not rescan minutes of already committed PCM for every
# new provider chunk. Keep a bounded acoustic look-back before the last real
# committed boundary so valley/peak context remains available; final flush is
# still allowed to inspect the complete original PCM once.
ONLINE_ANALYSIS_LOOKBACK_MS = 600
ONLINE_MAX_UNRESOLVED_MS = max(
    1000, int(os.getenv("TTS_VISEME_MAX_UNRESOLVED_MS") or "3600")
)
TRACE_TTS_PCM = os.getenv("TTS_VISEME_TRACE_PCM") == "1"
# Room send follows the syllable sample clock, not compute bursts.
# The first ready event goes out immediately; later events keep their
# at_sample gaps. A small lead releases each slot slightly early.
MOUTH_SEND_LEAD_MS = 0
SINGLETON_PORT = int(os.getenv("TTS_VISEME_SINGLETON_PORT") or "37659")
PCM_BOUNDARY_FRAME_MS = 10
PCM_BOUNDARY_MIN_PEAK_GAP_MS = 90
PCM_BOUNDARY_SMOOTH_FRAMES = 5
PYPINYIN_CONTEXT = 8

SCRIPT_IDS = {
    "a": 1,
    "o": 2,
    "e": 3,
    "i": 4,
    "u": 5,
    "ü": 6,
}

INITIALS = (
    "zh", "ch", "sh",
    "b", "p", "m", "f",
    "d", "t", "n", "l",
    "g", "k", "h",
    "j", "q", "x",
    "r", "z", "c", "s",
)

PRIMARY_VISEME = {
    # a/open
    "a": "a", "ai": "a", "ao": "a", "an": "a", "ang": "a",
    "ia": "a", "iao": "a", "iang": "a",
    "ua": "a", "uai": "a", "uan": "a", "uang": "a",

    # o/rounded
    "o": "o", "ou": "o", "ong": "o",
    "uo": "o", "iou": "o", "iong": "o", "io": "o",

    # e/central/open-front
    "e": "e", "er": "e", "ei": "e", "en": "e", "eng": "e",
    "ie": "e", "ian": "e",
    "uei": "e", "uen": "e", "ueng": "e",

    # i/spread
    "i": "i", "in": "i", "ing": "i",

    # u/back-rounded
    "u": "u",

    # ü/front-rounded
    "ü": "ü", "üe": "ü", "üan": "ü", "ün": "ü",
}

CLAUSE_PUNCT = set("，,、；;：:")
STRONG_PUNCT = set("。！？!?")
ALL_PUNCT = CLAUSE_PUNCT | STRONG_PUNCT


def log(tag: str, msg: str) -> None:
    print(f"[{tag:<16}] {msg}", flush=True)


def viseme_log(msg: str) -> None:
    print(f"[VISEME] {msg}", flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def is_han(ch: str) -> bool:
    return "\u3400" <= ch <= "\u9fff"


def han_chars(text: str) -> List[str]:
    return [ch for ch in (text or "") if is_han(ch)]


def missing_han_sequence(
    expected: Sequence[str],
    actual: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Return expected HAN chars not covered in order, plus leftover seq chars."""
    missing: List[Dict[str, Any]] = []
    cursor = 0
    for index, char in enumerate(expected):
        if cursor < len(actual) and actual[cursor] == char:
            cursor += 1
        else:
            missing.append({"index": index, "char": char})
    extra = [str(ch) for ch in actual[cursor:]]
    return missing, extra


def strip_tone(py: Optional[str]) -> str:
    if not py:
        return ""
    return (
        re.sub(r"[1-5]$", "", py.strip().lower())
        .replace("u:", "ü")
        .replace("v", "ü")
    )


def tone_number(py: Optional[str]) -> Optional[int]:
    if not py:
        return None
    m = re.search(r"([1-5])$", py)
    return int(m.group(1)) if m else None


def normalize_final(pinyin: str) -> str:
    s = strip_tone(pinyin)
    if not s:
        return ""

    y_map = {
        "yi": "i", "ya": "ia", "yao": "iao", "ye": "ie",
        "yo": "io", "you": "iou", "yan": "ian", "yang": "iang",
        "yin": "in", "ying": "ing", "yong": "iong",
        "yu": "ü", "yue": "üe", "yuan": "üan", "yun": "ün",
    }
    w_map = {
        "wu": "u", "wa": "ua", "wo": "uo", "wai": "uai",
        "wei": "uei", "wan": "uan", "wang": "uang",
        "wen": "uen", "weng": "ueng",
    }

    if s in y_map:
        return y_map[s]
    if s in w_map:
        return w_map[s]

    if s in {"zhi", "chi", "shi", "ri", "zi", "ci", "si"}:
        return "i"

    initial = ""
    final = s
    for ini in INITIALS:
        if s.startswith(ini):
            initial = ini
            final = s[len(ini):]
            break

    if initial in {"j", "q", "x"} and final.startswith("u"):
        final = "ü" + final[1:]

    if final == "iu":
        final = "iou"
    elif final == "ui":
        final = "uei"
    elif final == "un":
        final = "uen"

    return final


@dataclass
class Pron:
    index: int
    char: str
    g2pw: Optional[str]
    pypinyin: Optional[str]
    selected: Optional[str]
    reason: str
    final: Optional[str]
    viseme: Optional[str]
    script_id: Optional[int]


class DualG2P:
    def __init__(self, conflict_policy: str = "pypinyin"):
        self.conflict_policy = conflict_policy
        self.g2pw = None
        self.engine = "not_loaded"

    def _ensure_socks(self) -> None:
        values = [
            os.environ.get(k, "")
            for k in (
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy",
            )
        ]
        uses_socks = any(
            v.lower().startswith(("socks://", "socks5://", "socks5h://"))
            for v in values if v
        )
        if uses_socks and importlib.util.find_spec("socksio") is None:
            log("G2PW_NET", "installing missing socksio")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "socksio"]
            )

    def _make_converter(self):
        from g2pw import G2PWConverter

        kwargs = {
            "style": "pinyin",
            "enable_non_tradional_chinese": True,
        }
        local_model_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "G2PWModel",
        )
        if os.path.isdir(local_model_dir):
            kwargs["model_dir"] = local_model_dir
        try:
            import inspect

            params = inspect.signature(G2PWConverter.__init__).parameters
            if "num_workers" in params:
                kwargs["num_workers"] = 0
        except (TypeError, ValueError):
            pass
        converter = G2PWConverter(**kwargs)
        # g2pw 0.1.1 uses `num_workers if num_workers else config.num_workers`,
        # so constructor num_workers=0 is accidentally replaced by its default
        # multiprocessing value. Force DataLoader into the current process after
        # construction; otherwise macOS torch_shm_manager can collide/hang.
        if hasattr(converter, "num_workers"):
            converter.num_workers = 0
        return converter

    def load(self) -> None:
        if self.engine != "not_loaded":
            return

        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

        for key in (
            "http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        ):
            if os.environ.get(key):
                value = re.sub(r"//[^/@]+@", "//***@", os.environ[key])
                log("PROXY", f"{key}={value}")

        self._ensure_socks()

        t0 = time.perf_counter()
        try:
            log("G2PW", "loading g2pW...")
            self.g2pw = self._make_converter()
            self.engine = "g2pw"
            log("G2PW", f"ready in {(time.perf_counter()-t0)*1000:.1f}ms")
            return
        except Exception as exc:
            log("G2PW_ERR", f"normal load failed: {type(exc).__name__}: {exc}")

        try:
            import httpx
            from huggingface_hub import close_session, set_client_factory

            close_session()
            set_client_factory(
                lambda: httpx.Client(
                    trust_env=False,
                    timeout=httpx.Timeout(30.0, connect=15.0),
                    follow_redirects=True,
                )
            )
            self.g2pw = self._make_converter()
            self.engine = "g2pw_no_proxy"
            log("G2PW", "ready after proxy bypass")
        except Exception as exc:
            self.g2pw = None
            self.engine = "pypinyin_only"
            log("G2PW_ERR", f"fallback to pypinyin: {type(exc).__name__}: {exc}")

    @staticmethod
    def pypinyin_context(text: str) -> List[Optional[str]]:
        raw = pypinyin_fn(
            text,
            style=Style.TONE3,
            neutral_tone_with_five=True,
            heteronym=False,
            errors=lambda chars: [None] * len(chars),
        )
        vals = [(x[0] if x else None) for x in raw]
        vals.extend([None] * max(0, len(text) - len(vals)))
        return vals[:len(text)]

    def _merge_prons(
        self,
        text: str,
        g2: Sequence[Optional[str]],
        py_ref: Sequence[Optional[str]],
    ) -> List[Pron]:
        result: List[Pron] = []
        for i, ch in enumerate(text):
            if not is_han(ch):
                result.append(
                    Pron(i, ch, None, None, None, "non_han", None, None, None)
                )
                continue

            a = g2[i] if i < len(g2) else None
            b = py_ref[i] if i < len(py_ref) else None

            if a and b and strip_tone(a) == strip_tone(b):
                selected = a
                reason = "consensus"
            elif a and b:
                if self.conflict_policy == "g2pw":
                    selected = a
                    reason = "CONFLICT->g2pw"
                else:
                    selected = b
                    reason = "CONFLICT->pypinyin"
            elif a:
                selected = a
                reason = "g2pw_only"
            elif b:
                selected = b
                reason = "pypinyin_only"
            else:
                selected = None
                reason = "unresolved"

            final = normalize_final(selected) if selected else None
            viseme = PRIMARY_VISEME.get(final) if final else None
            script_id = SCRIPT_IDS.get(viseme) if viseme else None
            result.append(
                Pron(i, ch, a, b, selected, reason, final, viseme, script_id)
            )
        return result

    def resolve_pypinyin(self, text: str) -> List[Pron]:
        """Millisecond-level pronunciation. Never loads or calls g2pW."""
        py_ref = self.pypinyin_context(text)
        return self._merge_prons(text, [None] * len(text), py_ref)

    def resolve_pypinyin_incremental(
        self,
        text: str,
        prev_text: str = "",
        prev_prons: Optional[Sequence[Pron]] = None,
    ) -> List[Pron]:
        """Resolve only the new suffix plus a short left context."""
        if prev_prons and prev_text == text:
            return list(prev_prons)
        if not prev_prons or not text.startswith(prev_text):
            return self.resolve_pypinyin(text)
        start = max(0, len(prev_text) - PYPINYIN_CONTEXT)
        tail = self.resolve_pypinyin(text[start:])
        out: List[Pron] = []
        for i, _ch in enumerate(text):
            if i < start:
                out.append(prev_prons[i])
                continue
            p = tail[i - start]
            out.append(
                Pron(
                    i, p.char, p.g2pw, p.pypinyin, p.selected,
                    p.reason, p.final, p.viseme, p.script_id,
                )
            )
        return out

    def resolve(self, text: str) -> List[Pron]:
        """Use g2pW only if it is already loaded. Never call load() here."""
        py_ref = self.pypinyin_context(text)
        if self.g2pw is None:
            return self._merge_prons(text, [None] * len(text), py_ref)
        try:
            raw = self.g2pw(text)
            if raw and isinstance(raw[0], list):
                raw = raw[0]
            g2 = list(raw)
            g2.extend([None] * max(0, len(text) - len(g2)))
            g2 = g2[:len(text)]
        except Exception as exc:
            log("G2PW_ERR", f"inference failed: {exc!r}")
            g2 = [None] * len(text)
        return self._merge_prons(text, g2, py_ref)


def print_prons(text: str, prons: Sequence[Pron], engine: str) -> None:
    print("\n================ DUAL G2P -> ONE PRIMARY MOUTH ================")
    print("idx 字  g2pW       pypinyin   selected    reason                 final   mouth id")
    print("--- --- ---------- ---------- ----------- ---------------------- ------- ----- --")

    conflicts = 0
    unmapped = 0
    consensus = 0
    mapped = []

    for p in prons:
        if not p.selected:
            continue

        if p.reason == "consensus":
            consensus += 1
        if "CONFLICT" in p.reason:
            conflicts += 1
        if not p.viseme:
            unmapped += 1

        print(
            f"{p.index:>3} {p.char:<2} "
            f"{(p.g2pw or '-'): <10} "
            f"{(p.pypinyin or '-'): <10} "
            f"{(p.selected or '-'): <11} "
            f"{p.reason:<22} "
            f"{(p.final or '-'): <7} "
            f"{(p.viseme or '-'): <5} "
            f"{p.script_id if p.script_id is not None else '-'}"
        )

        if p.script_id is not None:
            mapped.append(
                f"{p.char}({p.selected}->{p.viseme}/{p.script_id})"
            )

    total = sum(1 for p in prons if p.selected)
    agreement = (consensus / total * 100.0) if total else 0.0

    print("---------------------------------------------------------------")
    print(
        f"engine={engine} conflicts={conflicts} unmapped={unmapped} "
        f"g2p_agreement={agreement:.1f}%"
    )
    print("===============================================================\n")

    print("================ TEXT TEST =================")
    print(f"text   : {text}")
    print("mapping: " + " ".join(mapped))
    print(
        "scripts: " +
        " ".join(str(p.script_id) for p in prons if p.script_id is not None)
    )
    print("============================================\n")

    for p in prons:
        if "CONFLICT" in p.reason:
            log(
                "PRON_CONFLICT",
                f"char={p.char} idx={p.index} "
                f"g2pw={p.g2pw} pypinyin={p.pypinyin} "
                f"SELECT={p.selected} final={p.final} "
                f"mouth={p.viseme} script={p.script_id}"
            )



def make_normalized_index(text: str) -> Tuple[str, List[int]]:
    """
    Normalize to Han characters only and keep a map back to source indexes.
    This makes matching tolerant of punctuation/whitespace differences.
    """
    chars = []
    indexes = []
    for i, ch in enumerate(text):
        if is_han(ch):
            chars.append(ch)
            indexes.append(i)
    return "".join(chars), indexes


def map_server_sentence_to_source(
    original_text: str,
    server_text: str,
    source_cursor: int,
) -> Tuple[int, int, str]:
    """
    Sequentially map Qwen's actual sentence text back to original source.

    Prefer exact substring. If punctuation differs, fall back to Han-only match.
    """
    if server_text:
        pos = original_text.find(server_text, source_cursor)
        if pos >= 0:
            return pos, pos + len(server_text), "exact"

    server_han, _ = make_normalized_index(server_text)
    if not server_han:
        return source_cursor, source_cursor, "empty"

    source_tail = original_text[source_cursor:]
    source_han, source_map = make_normalized_index(source_tail)

    hp = source_han.find(server_han)
    if hp >= 0:
        start_rel = source_map[hp]
        end_rel = source_map[hp + len(server_han) - 1] + 1

        # Include punctuation immediately after the last matched Han.
        while end_rel < len(source_tail) and source_tail[end_rel] in ALL_PUNCT:
            end_rel += 1

        return (
            source_cursor + start_rel,
            source_cursor + end_rel,
            "han_normalized",
        )

    # Last-resort sequential fallback: consume the same number of Han syllables.
    needed = len(server_han)
    count = 0
    start = source_cursor
    end = source_cursor

    for i in range(source_cursor, len(original_text)):
        if is_han(original_text[i]):
            count += 1
        end = i + 1
        if count >= needed:
            while end < len(original_text) and original_text[end] in ALL_PUNCT:
                end += 1
            break

    return start, end, "sequential_fallback"


def rms_frames(pcm: bytes, frame_ms: int = 20, sample_rate: int = SAMPLE_RATE):
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n = len(samples)
    frame = max(1, int(sample_rate * frame_ms / 1000.0))

    starts = []
    vals = []

    for s in range(0, n, frame):
        x = samples[s:min(n, s + frame)]
        if len(x) == 0:
            continue
        starts.append(s)
        vals.append(float(np.sqrt(np.mean(x * x) + 1e-9)))

    return samples, starts, vals, frame


def detect_active_span(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Tuple[int, int, dict]:
    samples, starts, vals, frame = rms_frames(pcm, sample_rate=sample_rate)
    n = len(samples)

    if not vals:
        return 0, n, {"threshold": 0.0, "peak": 0.0}

    arr = np.asarray(vals, dtype=np.float32)
    peak = float(arr.max())
    floor = float(np.percentile(arr, 20))
    threshold = max(180.0, floor * 3.0, peak * 0.07)

    active = np.where(arr >= threshold)[0]
    if len(active) == 0:
        return 0, n, {"threshold": threshold, "peak": peak}

    first_i = max(0, int(active[0]) - 1)
    last_i = min(len(starts) - 1, int(active[-1]) + 1)

    first = starts[first_i]
    last = min(n, starts[last_i] + frame)

    if last - first < int(sample_rate * 0.20):
        return 0, n, {"threshold": threshold, "peak": peak}

    return first, last, {"threshold": threshold, "peak": peak}


def _pcm_energy_profile(
    pcm: bytes,
    sample_rate: int,
) -> Tuple[np.ndarray, np.ndarray, int]:
    """Return a smoothed short-time RMS envelope on the real PCM sample clock."""
    _samples, starts, vals, frame = rms_frames(
        pcm,
        frame_ms=PCM_BOUNDARY_FRAME_MS,
        sample_rate=sample_rate,
    )
    if not vals:
        return np.asarray([], dtype=np.int64), np.asarray([], dtype=np.float32), frame
    env = np.asarray(vals, dtype=np.float32)
    smooth_n = max(1, int(PCM_BOUNDARY_SMOOTH_FRAMES))
    if smooth_n > 1 and len(env) >= smooth_n:
        kernel = np.ones(smooth_n, dtype=np.float32) / float(smooth_n)
        env = np.convolve(env, kernel, mode="same").astype(np.float32)
    return np.asarray(starts, dtype=np.int64), env, frame


def _energy_peaks(
    starts: np.ndarray,
    env: np.ndarray,
    *,
    active_start: int,
    usable_end: int,
    sample_rate: int,
    min_gap_ms: int = PCM_BOUNDARY_MIN_PEAK_GAP_MS,
) -> List[int]:
    """Find stable vowel-like energy nuclei without assuming a fixed syllable duration."""
    if len(env) == 0:
        return []
    valid = np.where((starts >= active_start) & (starts < usable_end))[0]
    if len(valid) == 0:
        return []
    local = env[valid]
    floor = float(np.percentile(local, 20))
    peak = float(np.max(local))
    threshold = floor + (peak - floor) * 0.18
    candidates: List[int] = []
    for pos, idx in enumerate(valid):
        if env[idx] < threshold:
            continue
        left = env[idx - 1] if idx > 0 else env[idx]
        right = env[idx + 1] if idx + 1 < len(env) else env[idx]
        if env[idx] >= left and env[idx] >= right:
            candidates.append(int(idx))
    if not candidates:
        candidates = [int(valid[int(np.argmax(local))])]

    # Non-maximum suppression avoids treating small ripples inside one vowel as
    # separate Mandarin syllables. The chosen position still comes from PCM.
    min_gap = max(1, int(sample_rate * min_gap_ms / 1000.0))
    ranked = sorted(candidates, key=lambda i: float(env[i]), reverse=True)
    selected: List[int] = []
    for idx in ranked:
        sample = int(starts[idx])
        if all(abs(sample - int(starts[other])) >= min_gap for other in selected):
            selected.append(idx)
    return sorted(int(starts[idx]) for idx in selected)


def _energy_valley_between(
    starts: np.ndarray,
    env: np.ndarray,
    left_sample: int,
    right_sample: int,
    *,
    frame: int,
) -> int:
    """Use the real RMS minimum between two acoustic nuclei as their boundary."""
    if right_sample <= left_sample:
        return left_sample
    valid = np.where((starts >= left_sample) & (starts <= right_sample))[0]
    if len(valid) == 0:
        return (left_sample + right_sample) // 2
    idx = int(valid[int(np.argmin(env[valid]))])
    return int(starts[idx]) + frame // 2


def _fit_final_syllable_nuclei(
    starts: np.ndarray,
    env: np.ndarray,
    *,
    active_start: int,
    active_end: int,
    syllable_count: int,
    sample_rate: int,
) -> List[int]:
    """Select stable acoustic nuclei from the real PCM envelope.

    Prefer actual detected nuclei. If there are too many, remove low-prominence
    ripple peaks. If there are too few, a denser *peak detector* may recover
    weak/fast syllables, but we never manufacture extra nuclei merely because
    the text contains more syllables. Missing evidence remains unaligned.
    """
    if syllable_count <= 0 or len(env) == 0:
        return []

    def _env_at(sample: int) -> float:
        idx = int(np.searchsorted(starts, sample, side="left"))
        idx = max(0, min(idx, len(env) - 1))
        return float(env[idx])

    def _min_env(left: int, right: int) -> float:
        valid = np.where((starts >= left) & (starts <= right))[0]
        if len(valid) == 0:
            return min(_env_at(left), _env_at(right))
        return float(np.min(env[valid]))

    def _prominence(peaks: Sequence[int], index: int) -> float:
        peak = int(peaks[index])
        left = active_start if index == 0 else int(peaks[index - 1])
        right = active_end if index + 1 >= len(peaks) else int(peaks[index + 1])
        left_floor = _min_env(left, peak)
        right_floor = _min_env(peak, right)
        return max(0.0, _env_at(peak) - max(left_floor, right_floor))

    nuclei = _energy_peaks(
        starts,
        env,
        active_start=active_start,
        usable_end=active_end,
        sample_rate=sample_rate,
        min_gap_ms=PCM_BOUNDARY_MIN_PEAK_GAP_MS,
    )

    # If natural speech has weak/fast syllables, a denser scan can recover
    # additional real acoustic nuclei before any fallback insertion is needed.
    if len(nuclei) < syllable_count:
        dense = _energy_peaks(
            starts,
            env,
            active_start=active_start,
            usable_end=active_end,
            sample_rate=sample_rate,
            min_gap_ms=max(45, PCM_BOUNDARY_MIN_PEAK_GAP_MS // 2),
        )
        nuclei = sorted(set(nuclei).union(dense))

    # Extra peaks are usually intra-vowel ripples/diphthong structure. Remove
    # the least acoustically prominent ones rather than choosing by text time.
    while len(nuclei) > syllable_count:
        scores = [_prominence(nuclei, i) for i in range(len(nuclei))]
        remove_i = min(range(len(nuclei)), key=lambda i: scores[i])
        nuclei.pop(remove_i)

    return nuclei[:syllable_count]


def detect_pcm_syllable_spans(
    pcm: bytes,
    syllable_count: int,
    *,
    sample_rate: int,
    flush: bool,
) -> Tuple[List[Tuple[int, int]], dict]:
    """Detect syllable spans directly on the accumulated PCM timeline.

    Online mode emits only spans whose *end boundary* is already visible in
    PCM (one acoustic-nucleus lookahead). Final mode fits exactly the text
    syllable count across the measured active speech span. This removes the old
    `180ms * N` cumulative drift: every boundary is an absolute PCM sample.
    """
    if syllable_count <= 0 or not pcm:
        return [], {"mode": "pcm_boundary_idle"}
    sr = max(1, int(sample_rate or SAMPLE_RATE))
    total = len(pcm) // SAMPLE_WIDTH
    active_start, active_end, vad = detect_active_span(pcm, sample_rate=sr)
    starts, env, frame = _pcm_energy_profile(pcm, sr)
    if len(env) == 0 or active_end <= active_start:
        return [], {
            "mode": "pcm_boundary_empty",
            "active_start": active_start,
            "active_end": active_end,
        }

    if flush:
        nuclei = _fit_final_syllable_nuclei(
            starts,
            env,
            active_start=active_start,
            active_end=active_end,
            syllable_count=syllable_count,
            sample_rate=sr,
        )
        if not nuclei:
            return [], {"mode": "pcm_boundary_final_empty"}
        boundaries = [active_start]
        for left, right in zip(nuclei, nuclei[1:]):
            boundaries.append(
                _energy_valley_between(starts, env, left, right, frame=frame)
            )
        boundaries.append(active_end)
        mode = "pcm_boundary_final"
    else:
        holdback = int(sr * ONLINE_HOLDBACK_MS / 1000.0)
        usable_end = min(active_end, max(active_start, total - holdback))
        nuclei = _energy_peaks(
            starts,
            env,
            active_start=active_start,
            usable_end=usable_end,
            sample_rate=sr,
        )
        # Need the following acoustic nucleus to establish the current
        # syllable's real end boundary. Do not invent a future duration.
        if len(nuclei) < 2:
            return [], {
                "mode": "pcm_boundary_wait",
                "active_start": active_start,
                "active_end": active_end,
                "nuclei": len(nuclei),
            }
        nuclei = nuclei[: syllable_count + 1]
        boundaries = [active_start]
        for left, right in zip(nuclei, nuclei[1:]):
            boundaries.append(
                _energy_valley_between(starts, env, left, right, frame=frame)
            )
        mode = "pcm_boundary_online"

    fixed = [max(0, min(int(boundaries[0]), total))]
    for boundary in boundaries[1:]:
        # start/end must stay on detected PCM boundaries. Never push a
        # boundary forward to enforce a synthetic minimum phoneme duration.
        value = max(0, min(int(boundary), total))
        if value <= fixed[-1]:
            continue
        fixed.append(value)
    spans: List[Tuple[int, int]] = []
    for start, end in zip(fixed, fixed[1:]):
        if end <= start:
            continue
        spans.append((start, end))
        if len(spans) >= syllable_count:
            break
    return spans, {
        "mode": mode,
        "active_start": active_start,
        "active_end": active_end,
        "nuclei": len(nuclei),
        "spans": len(spans),
        "vad": vad,
    }


def _split_counts(n_items: int, n_bins: int) -> List[int]:
    """Distribute n_items into n_bins bins as evenly as possible."""
    if n_bins <= 0:
        return []
    base, extra = divmod(max(0, int(n_items)), int(n_bins))
    return [base + (1 if i < extra else 0) for i in range(n_bins)]


def _subdivide_interval(
    syllables: Sequence[Pron],
    start: int,
    end: int,
) -> List[Tuple[Pron, int, int]]:
    n = len(syllables)
    if n <= 0:
        return []
    start_i = int(start)
    end_i = int(end)
    width = end_i - start_i
    if width < n:
        end_i = start_i + n
        width = n
    out: List[Tuple[Pron, int, int]] = []
    for i, pron in enumerate(syllables):
        local_start = start_i + (i * width) // n
        local_end = start_i + ((i + 1) * width) // n
        if local_end <= local_start:
            local_end = local_start + 1
        out.append((pron, local_start, local_end))
    return out


def _repair_monotonic_pairs(
    pairs: Sequence[Tuple[Pron, int, int]],
) -> List[Tuple[Pron, int, int]]:
    repaired: List[Tuple[Pron, int, int]] = []
    cursor: Optional[int] = None
    for pron, start, end in pairs:
        start_i = int(start)
        end_i = int(end)
        if cursor is not None and start_i < cursor:
            start_i = cursor
        if end_i <= start_i:
            end_i = start_i + 1
        repaired.append((pron, start_i, end_i))
        cursor = end_i
    return repaired


def assign_syllables_to_spans(
    syllables: Sequence[Pron],
    spans: Sequence[Tuple[int, int]],
) -> List[Tuple[Pron, int, int]]:
    """Map every remaining syllable onto the available acoustic spans.

    Equal counts stay 1:1 on the detected nuclei. Extra syllables subdivide
    spans so visemes are not dropped. Extra spans are merged so leftover
    active speech is still covered.
    """
    if not syllables or not spans:
        return []
    n_syl = len(syllables)
    n_span = len(spans)
    if n_syl == n_span:
        return [
            (pron, int(start), int(end))
            for pron, (start, end) in zip(syllables, spans)
        ]
    assigned: List[Tuple[Pron, int, int]] = []
    if n_syl < n_span:
        counts = _split_counts(n_span, n_syl)
        idx = 0
        for pron, count in zip(syllables, counts):
            group = spans[idx:idx + count]
            idx += count
            if not group:
                continue
            assigned.append((pron, int(group[0][0]), int(group[-1][1])))
        return _repair_monotonic_pairs(assigned)

    counts = _split_counts(n_syl, n_span)
    idx = 0
    for (start, end), count in zip(spans, counts):
        if count <= 0:
            continue
        group = syllables[idx:idx + count]
        idx += count
        assigned.extend(_subdivide_interval(group, int(start), int(end)))
    return _repair_monotonic_pairs(assigned)


def _synthetic_syllable_pcm(
    durations_ms: Sequence[int],
    *,
    sample_rate: int = 24000,
    gap_ms: int = 24,
) -> bytes:
    """Generate deterministic speech-like bursts for the PCM boundary self-test."""
    pieces: List[np.ndarray] = [np.zeros(int(sample_rate * 0.08), dtype=np.float32)]
    for i, duration_ms in enumerate(durations_ms):
        n = max(1, int(sample_rate * duration_ms / 1000.0))
        t = np.arange(n, dtype=np.float32) / float(sample_rate)
        freq = 155.0 + (i % 7) * 23.0
        carrier = np.sin(2.0 * np.pi * freq * t)
        envelope = np.hanning(max(3, n)).astype(np.float32)
        if len(envelope) != n:
            envelope = np.ones(n, dtype=np.float32)
        syllable = carrier * envelope * (9000.0 + (i % 5) * 900.0)
        pieces.append(syllable.astype(np.float32))
        if i + 1 < len(durations_ms):
            pieces.append(np.zeros(int(sample_rate * gap_ms / 1000.0), dtype=np.float32))
    pieces.append(np.zeros(int(sample_rate * 0.10), dtype=np.float32))
    audio = np.concatenate(pieces) if pieces else np.asarray([], dtype=np.float32)
    audio = np.clip(audio, -32768.0, 32767.0).astype("<i2")
    return audio.tobytes()


def _split_span_evenly(
    start: int,
    end: int,
    count: int,
    *,
    min_gap: int,
) -> List[Tuple[int, int]]:
    """Partition [start, end] into `count` contiguous spans.

    Never extend beyond ``end``: mouth timing must stay on the real PCM sample
    clock. If the requested gap is too large for the interval, reduce the gap
    down to the largest value that still fits every syllable.
    """
    if count <= 0:
        return []
    start = max(0, int(start))
    end = max(start, int(end))
    width = end - start
    if width < count:
        return []
    min_gap = min(max(1, int(min_gap)), max(1, width // count))
    spans: List[Tuple[int, int]] = []
    cursor = start
    for i in range(count):
        remaining = count - i
        if remaining == 1:
            next_c = end
        else:
            next_c = start + int((i + 1) * width / count)
            next_c = max(cursor + min_gap, next_c)
            next_c = min(next_c, end - (remaining - 1) * min_gap)
        spans.append((cursor, next_c))
        cursor = next_c
    return spans


def _usable_spans_after_cursor(
    spans: Sequence[Tuple[int, int]],
    *,
    local_cursor: int,
) -> List[Tuple[int, int]]:
    cursor = max(0, int(local_cursor))
    usable: List[Tuple[int, int]] = []
    for start, end in spans:
        start_i = int(start)
        end_i = int(end)
        if end_i <= cursor:
            continue
        # A newly re-fitted span that overlaps committed history is unstable.
        # Do not trim it to the old cursor (that would fabricate a new start
        # boundary); wait for the next fully future acoustic span instead.
        if start_i < cursor:
            continue
        if end_i <= start_i:
            continue
        usable.append((start_i, end_i))
        cursor = end_i
    return usable


def run_pcm_boundary_self_test() -> None:
    """Verify short/long segments are anchored to measured PCM, not duration sums."""
    cases = {
        "short": [120, 205, 155, 275, 145],
        "long": [
            120, 205, 155, 275, 145, 230, 135, 190, 260, 150,
            215, 125, 245, 170, 285, 140, 200, 160, 255, 130,
            225, 150, 195, 270, 145, 235, 125, 210, 165, 250,
        ],
    }
    sr = 24000
    gap_ms = 24
    for name, durations in cases.items():
        pcm = _synthetic_syllable_pcm(
            durations,
            sample_rate=sr,
            gap_ms=gap_ms,
        )
        spans, info = detect_pcm_syllable_spans(
            pcm,
            len(durations),
            sample_rate=sr,
            flush=True,
        )
        if len(spans) != len(durations):
            raise AssertionError(
                f"{name}: expected {len(durations)} spans, got {len(spans)} info={info}"
            )
        for i, (start, end) in enumerate(spans):
            if not start < end:
                raise AssertionError(f"{name}: invalid span #{i}: {start}..{end}")
            if i and start != spans[i - 1][1]:
                raise AssertionError(
                    f"{name}: non-contiguous boundary #{i}: "
                    f"prev_end={spans[i - 1][1]} start={start}"
                )
        active_start = int(info.get("active_start") or 0)
        active_end = int(info.get("active_end") or 0)
        if spans[0][0] != active_start or spans[-1][1] != active_end:
            raise AssertionError(
                f"{name}: PCM anchors changed: spans={spans[0][0]}..{spans[-1][1]} "
                f"active={active_start}..{active_end}"
            )
        covered = sum(end - start for start, end in spans)
        if covered != active_end - active_start:
            raise AssertionError(
                f"{name}: accumulated duration drift: covered={covered} "
                f"active={active_end - active_start}"
            )

        # Synthetic PCM has known low-energy gaps. Compare every internal
        # detected boundary against the real gap center and ensure the tail of
        # a 30-syllable case is not progressively worse than the head.
        expected_boundaries: List[int] = []
        cursor = int(sr * 0.08)
        gap_samples = int(sr * gap_ms / 1000.0)
        for duration_ms in durations[:-1]:
            cursor += int(sr * duration_ms / 1000.0)
            expected_boundaries.append(cursor + gap_samples // 2)
            cursor += gap_samples
        detected_boundaries = [end for _start, end in spans[:-1]]
        errors_ms = [
            abs(actual - expected) * 1000.0 / sr
            for actual, expected in zip(detected_boundaries, expected_boundaries)
        ]
        max_error_ms = max(errors_ms, default=0.0)
        if max_error_ms > 45.0:
            raise AssertionError(
                f"{name}: PCM boundary error too large: max={max_error_ms:.1f}ms "
                f"errors={errors_ms}"
            )
        if len(errors_ms) >= 10:
            head = float(np.mean(errors_ms[:5]))
            tail = float(np.mean(errors_ms[-5:]))
            if tail > head + 15.0:
                raise AssertionError(
                    f"{name}: tail drift detected: head={head:.1f}ms tail={tail:.1f}ms"
                )
        print(
            f"[PCM_SELFTEST] {name} syllables={len(spans)} "
            f"active_samples={active_end-active_start} "
            f"end_ms={active_end * 1000.0 / sr:.2f} "
            f"max_boundary_error_ms={max_error_ms:.1f} PASS",
            flush=True,
        )


def syllable_weight(p: Pron) -> float:
    """
    LEGACY duration prior.

    Kept only for the old offline/reference sentence aligner below. The live
    reb_tts path MUST NOT use this to accumulate syllable timing; live timing
    is derived by detect_pcm_syllable_spans() from real PCM boundaries.
    """
    w = 1.0

    # Neutral tone is usually shorter.
    if tone_number(p.selected) == 5:
        w *= 0.78

    # Complex finals usually need a little more articulation time.
    if p.final in {
        "ai", "ao", "ou", "ei",
        "iao", "iou", "uai", "uei",
        "ian", "iang", "uan", "uang",
        "iong", "üe", "üan",
    }:
        w *= 1.10

    return w


def split_clauses(
    source_text: str,
    source_start_index: int,
    prons: Sequence[Pron],
) -> List[List[Pron]]:
    """
    Split only on punctuation that is actually present in the source sentence.
    """
    groups: List[List[Pron]] = []
    current: List[Pron] = []

    end_index = source_start_index + len(source_text)

    for idx in range(source_start_index, end_index):
        p = prons[idx]
        ch = source_text[idx - source_start_index]

        if p.selected and p.viseme and p.script_id is not None:
            current.append(p)

        if ch in CLAUSE_PUNCT or ch in STRONG_PUNCT:
            if current:
                groups.append(current)
                current = []

    if current:
        groups.append(current)

    return groups


def find_energy_valley(
    pcm: bytes,
    expected_sample: int,
    search_ms: int = 260,
    sample_rate: int = SAMPLE_RATE,
) -> int:
    """
    Find local RMS minimum around an expected punctuation boundary.
    """
    samples, starts, vals, frame = rms_frames(pcm, sample_rate=sample_rate)
    if not vals:
        return expected_sample

    radius = int(sample_rate * search_ms / 1000.0)

    best_sample = expected_sample
    best_val = None

    for s, v in zip(starts, vals):
        if abs(s - expected_sample) <= radius:
            if best_val is None or v < best_val:
                best_val = v
                best_sample = s

    return max(0, min(best_sample, len(samples)))


def build_sentence_events(
    source_text: str,
    source_start: int,
    prons: Sequence[Pron],
    pcm: bytes,
    global_start_sample: int,
    stream_id: str,
    seq_start: int,
    send_wall: float,
    first_pcm_wall: Optional[float],
    sample_rate: int = SAMPLE_RATE,
) -> Tuple[List[dict], dict]:
    """
    Build events from REAL Qwen server sentence PCM.

    Cross-sentence drift is zero because every sentence begins at the true
    cumulative PCM sample count.

    Within the sentence:
    - real active speech span
    - punctuation clauses
    - punctuation boundary adjusted to a nearby energy valley
    - weighted syllable slots
    """
    sr = int(sample_rate or SAMPLE_RATE)
    total_samples = len(pcm) // SAMPLE_WIDTH
    active_start, active_end, vad = detect_active_span(pcm, sample_rate=sr)
    active_len = max(1, active_end - active_start)

    clauses = split_clauses(source_text, source_start, prons)
    syllables = [p for group in clauses for p in group]

    if not syllables:
        return [], {
            "total_samples": total_samples,
            "active_start": active_start,
            "active_end": active_end,
            "clauses": 0,
        }

    clause_weights = [
        sum(syllable_weight(p) for p in group)
        for group in clauses
    ]
    total_weight = sum(clause_weights) or 1.0

    # Estimate clause boundaries proportionally, then snap to nearby energy valleys.
    boundaries = [active_start]
    cumulative = 0.0

    for cw in clause_weights[:-1]:
        cumulative += cw
        expected = active_start + int(round(active_len * cumulative / total_weight))
        valley = find_energy_valley(pcm, expected, sample_rate=sr)
        # Keep monotonic and inside active span.
        valley = max(boundaries[-1] + int(sr * 0.06), valley)
        valley = min(valley, active_end - int(sr * 0.06))
        boundaries.append(valley)

    boundaries.append(active_end)

    events = []
    seq = seq_start

    # Estimate what an audio player would have played if playback began at first PCM.
    if first_pcm_wall is not None:
        est_played = max(
            0,
            int((send_wall - first_pcm_wall) * sr),
        )
    else:
        est_played = 0

    for ci, group in enumerate(clauses):
        seg_start = boundaries[ci]
        seg_end = boundaries[ci + 1]
        seg_len = max(1, seg_end - seg_start)

        weights = [syllable_weight(p) for p in group]
        group_total = sum(weights) or 1.0

        cursor = 0.0
        for p, w in zip(group, weights):
            slot_start = seg_start + seg_len * (cursor / group_total)
            cursor += w
            slot_end = seg_start + seg_len * (cursor / group_total)

            # Mouth formation slightly precedes acoustic vowel center.
            local_at = int(round(slot_start + (slot_end - slot_start) * 0.32))
            local_at = max(0, min(local_at, max(0, total_samples - 1)))

            at_sample = global_start_sample + local_at
            lead_samples = at_sample - est_played
            lead_ms = lead_samples * 1000.0 / sr

            events.append(
                {
                    "type": "mouth_script",
                    "sender": "stream_room_v7",
                    "initiator": "tts_viseme",
                    "stream_id": stream_id,
                    "seq": seq,
                    "script_id": p.script_id,
                    "viseme": p.viseme,
                    "char": p.char,
                    "pinyin": p.selected,
                    "final": p.final,
                    "at_sample": at_sample,
                    "at_ms": round(at_sample * 1000.0 / sr, 2),
                    "lead_est_ms": round(lead_ms, 1),
                    "timing_source": "real_sentence_pcm_energy_text_weight",
                    "pronunciation_reason": p.reason,
                }
            )
            seq += 1

    return events, {
        "total_samples": total_samples,
        "active_start": active_start,
        "active_end": active_end,
        "active_ms": (active_end - active_start) * 1000.0 / sr,
        "clauses": len(clauses),
        "boundaries": boundaries,
        "threshold": vad.get("threshold"),
        "peak": vad.get("peak"),
    }



def syllables_for_text(text: str, prons: Sequence[Pron]) -> List[Pron]:
    out: List[Pron] = []
    for i, _ch in enumerate(text):
        if i >= len(prons):
            break
        p = prons[i]
        if p.selected and p.viseme and p.script_id is not None:
            out.append(p)
    return out


def match_forced_chars(
    syllables: Sequence[Pron],
    aligned: Sequence[AlignedChar],
    sample_rate: int,
) -> List[Tuple[Pron, int, int]]:
    """Match Qwen character timestamps to G2PW without inventing boundaries."""
    sr = max(1, int(sample_rate or SAMPLE_RATE))
    out: List[Tuple[Pron, int, int]] = []
    cursor = 0
    for item in aligned:
        token_han = [ch for ch in str(item.text or "") if is_han(ch)]
        if len(token_han) != 1:
            continue
        token = token_han[0]
        if cursor >= len(syllables):
            break
        # Fail closed: partial forced alignment can assign a non-zero timestamp
        # to an as-yet-unspoken future token.  Never search ahead, otherwise a
        # future token permanently skips the characters between it and cursor.
        if syllables[cursor].char != token:
            continue
        pron = syllables[cursor]
        cursor += 1
        start = max(0, int(round(float(item.start_s) * sr)))
        end = max(start, int(round(float(item.end_s) * sr)))
        out.append((pron, start, end))
    return out


def aligned_idle_spans(
    pairs: Sequence[Tuple[Pron, int, int]],
    sample_rate: int,
    *,
    min_idle_ms: int = IDLE_MIN_MS,
) -> List[Tuple[int, int]]:
    """No-phoneme gaps.  These intervals remain idle instead of inheriting a viseme."""
    sr = max(1, int(sample_rate or SAMPLE_RATE))
    threshold = max(1, int(sr * max(0, int(min_idle_ms)) / 1000.0))
    ordered = sorted(pairs, key=lambda item: item[1])
    gaps: List[Tuple[int, int]] = []
    for left, right in zip(ordered, ordered[1:]):
        gap_start = int(left[2])
        gap_end = int(right[1])
        if gap_end - gap_start >= threshold:
            gaps.append((gap_start, gap_end))
    return gaps


def punct_pause_samples(text: str, after_index: int, sample_rate: int) -> int:
    pause = 0
    idx = after_index + 1
    while idx < len(text) and text[idx] in ALL_PUNCT:
        pause += int(sample_rate * (0.16 if text[idx] in STRONG_PUNCT else 0.10))
        idx += 1
    return pause


def make_mouth_event(
    *,
    stream_id: str,
    seq: int,
    pron: Pron,
    at_sample: int,
    end_sample: Optional[int] = None,
    sample_rate: int,
    send_wall: float,
    first_pcm_wall: Optional[float],
    timing_source: str,
) -> Dict[str, Any]:
    sr = max(1, int(sample_rate or SAMPLE_RATE))
    if first_pcm_wall is not None:
        est_played = max(0, int((send_wall - first_pcm_wall) * sr))
    else:
        est_played = 0
    lead_ms = (at_sample - est_played) * 1000.0 / sr
    start_sample = max(0, int(at_sample))
    if end_sample is None:
        end_sample = start_sample
    end_sample = max(start_sample, int(end_sample))
    duration_samples = max(0, end_sample - start_sample)
    start_ms = start_sample * 1000.0 / sr
    end_ms = end_sample * 1000.0 / sr
    duration_ms = duration_samples * 1000.0 / sr
    return {
        "type": "mouth_script",
        "sender": "stream_room_v7",
        "initiator": "tts_viseme",
        "stream_id": stream_id,
        "seq": seq,
        "script_id": pron.script_id,
        "viseme": pron.viseme,
        "char": pron.char,
        "pinyin": pron.selected,
        "final": pron.final,
        "sample_rate": sr,
        # Compatibility: at_* now aliases the detected syllable start.
        "at_sample": start_sample,
        "at_ms": round(start_ms, 2),
        "start_sample": start_sample,
        "end_sample": end_sample,
        "duration_samples": duration_samples,
        "start_ms": round(start_ms, 2),
        "end_ms": round(end_ms, 2),
        "duration_ms": round(duration_ms, 2),
        # Stable absolute wait from this TTS stream's sample zero. Embedded
        # code should compare start_sample against its own played_sample_count.
        "wait_from_stream_start_ms": round(start_ms, 2),
        "lead_est_ms": round(lead_ms, 1),
        "timing_source": timing_source,
        "pronunciation_reason": pron.reason,
    }


def enforce_monotonic_events(
    events: List[Dict[str, Any]],
    last_at_sample: int,
    min_gap: int,
    sample_rate: int,
) -> Tuple[List[Dict[str, Any]], int]:
    sr = max(1, int(sample_rate or SAMPLE_RATE))
    gap = max(1, int(min_gap))
    current = last_at_sample
    for event in events:
        at_sample = int(event["at_sample"])
        if current >= 0:
            at_sample = max(at_sample, current + gap)
        event["at_sample"] = at_sample
        event["at_ms"] = round(at_sample * 1000.0 / sr, 2)
        current = at_sample
    return events, current


@dataclass
class OnlineAligner:
    emitted_indexes: Set[int] = field(default_factory=set)
    skipped_indexes: Set[int] = field(default_factory=set)
    last_at_sample: int = -1
    last_end_sample: int = -1
    speech_cursor: int = 0
    active_started: bool = False
    last_eval_samples: int = 0
    empty_eval_count: int = 0

    def reset(self) -> None:
        self.emitted_indexes.clear()
        self.skipped_indexes.clear()
        self.last_at_sample = -1
        self.last_end_sample = -1
        self.speech_cursor = 0
        self.active_started = False
        self.last_eval_samples = 0
        self.empty_eval_count = 0


@dataclass
class SegmentState:
    turn_id: str
    authoritative_text: str = ""
    text_prefix: str = ""
    char_index_offset: Optional[int] = None
    sample_cursor_timing: bool = False
    prons: Optional[List[Pron]] = None
    prons_text: str = ""
    pcm_buf: bytearray = field(default_factory=bytearray)
    sample_rate: int = SAMPLE_RATE
    channels: int = 1
    start_sample: int = 0
    total_samples: int = 0
    final: bool = False
    first_pcm_recv_ms: Optional[int] = None
    first_pcm_wall: Optional[float] = None
    emitted: bool = False
    mouth_seq_start: Optional[int] = None
    online_backlog_warned: bool = False
    aligner: OnlineAligner = field(default_factory=OnlineAligner)

    @property
    def pcm(self) -> bytes:
        return bytes(self.pcm_buf)


@dataclass
class ConversationState:
    conversation_id: str
    llm_text_buffer: str = ""
    llm_seq: int = 0
    llm_final: bool = False
    speculative_text: str = ""
    speculative_prons: Optional[List[Pron]] = None
    g2p_generation: int = 0
    g2p_submitted_text: str = ""
    segments: Dict[str, SegmentState] = field(default_factory=dict)
    sample_cursor: int = 0
    stream_epoch: Optional[int] = None
    last_tts_text: str = ""
    first_llm_ms: Optional[int] = None
    first_pcm_ms: Optional[int] = None
    first_pcm_wall: Optional[float] = None
    first_script_ms: Optional[int] = None
    script_compute_ms: List[float] = field(default_factory=list)
    script_send_ms: List[float] = field(default_factory=list)
    llm_lag_ms: List[float] = field(default_factory=list)
    mouth_seq: int = 0
    mouth_epoch: int = 0
    mouth_duration_ms_sum: float = 0.0
    mouth_chars: List[str] = field(default_factory=list)
    mouth_event_count: int = 0
    mouth_events_by_seq: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    stream_id: str = ""
    active_turn_id: Optional[str] = None
    cleared: bool = False
    latency_printed: bool = False


def pcm_sample_count(pcm: bytes, channels: int = 1) -> int:
    width = max(1, int(channels) * SAMPLE_WIDTH)
    return len(pcm) // width


def decode_tts_pcm(data: Dict[str, Any]) -> Tuple[bytes, int, int]:
    raw = data.get("audio") or ""
    if not raw or not isinstance(raw, str):
        return b"", int(data.get("sample_rate") or SAMPLE_RATE), int(data.get("channels") or 1)
    pcm = base64.b64decode(raw)
    sample_rate = int(data.get("sample_rate") or SAMPLE_RATE)
    channels = int(data.get("channels") or 1)
    return pcm, sample_rate, channels


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    idx = int(round((q / 100.0) * (len(ordered) - 1)))
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def format_latency_summary(conv: "ConversationState") -> str:
    llm_to_pcm = (
        None
        if conv.first_llm_ms is None or conv.first_pcm_ms is None
        else conv.first_pcm_ms - conv.first_llm_ms
    )
    pcm_to_script = (
        None
        if conv.first_pcm_ms is None or conv.first_script_ms is None
        else conv.first_script_ms - conv.first_pcm_ms
    )
    llm_to_script = (
        None
        if conv.first_llm_ms is None or conv.first_script_ms is None
        else conv.first_script_ms - conv.first_llm_ms
    )
    compute_p50 = percentile(conv.script_compute_ms, 50)
    compute_p95 = percentile(conv.script_compute_ms, 95)
    send_p50 = percentile(conv.script_send_ms, 50)
    llm_lag_p50 = percentile(conv.llm_lag_ms, 50)

    def fmt(value: Optional[float]) -> str:
        return "-" if value is None else f"{value:.1f}"

    return (
        "[VISEME_LATENCY]\n"
        f"conv={conv.conversation_id}\n"
        f"llm_first_to_pcm_first={fmt(llm_to_pcm)}\n"
        f"pcm_first_to_script_first={fmt(pcm_to_script)}\n"
        f"llm_first_to_script_first={fmt(llm_to_script)}\n"
        f"script_compute_p50={fmt(compute_p50)}\n"
        f"script_compute_p95={fmt(compute_p95)}\n"
        f"script_ws_send_p50={fmt(send_p50)}\n"
        f"sam_to_listener_p50={fmt(llm_lag_p50)}"
    )


def build_mouth_sync_summary(
    conv: "ConversationState",
    *,
    sample_rate: int,
    audio_total_samples: int,
    audio_total_ms: float,
    speech_active_end_sample: int,
    speech_active_end_ms: Optional[float],
    last_mouth_end_sample: Optional[int],
    last_mouth_end_ms: Optional[float],
    alignment_gap_ms: Optional[float],
    audio_tail_silence_ms: Optional[float],
) -> Dict[str, Any]:
    registry_events = [
        conv.mouth_events_by_seq[key]
        for key in sorted(conv.mouth_events_by_seq)
    ]
    if registry_events:
        seq_chars = [str(item.get("char") or "") for item in registry_events]
        mouth_event_count = len(registry_events)
        duration_ms_sum = round(
            sum(float(item.get("duration_ms") or 0.0) for item in registry_events),
            2,
        )
    else:
        seq_chars = list(conv.mouth_chars)
        mouth_event_count = int(conv.mouth_event_count or 0)
        duration_ms_sum = round(float(conv.mouth_duration_ms_sum or 0.0), 2)
    seq_han = [ch for ch in seq_chars if is_han(ch)]
    llm_chars = han_chars(conv.llm_text_buffer)
    tts_chars = han_chars(conv.last_tts_text)
    expected = tts_chars or llm_chars
    missing, extra = missing_han_sequence(expected, seq_han)
    last_end = None if last_mouth_end_ms is None else round(float(last_mouth_end_ms), 2)
    first_start_ms: Optional[float] = None
    internal_gaps_ms: List[float] = []
    if registry_events:
        ordered_by_time = sorted(
            registry_events,
            key=lambda item: int(item.get("start_sample") or item.get("at_sample") or 0),
        )
        first_start_ms = float(ordered_by_time[0].get("start_ms") or 0.0)
        prev_end = int(
            ordered_by_time[0].get("end_sample")
            or ordered_by_time[0].get("start_sample")
            or ordered_by_time[0].get("at_sample")
            or 0
        )
        sr = max(1, int(sample_rate or SAMPLE_RATE))
        for item in ordered_by_time[1:]:
            start_sample = int(item.get("start_sample") or item.get("at_sample") or 0)
            if start_sample > prev_end:
                internal_gaps_ms.append((start_sample - prev_end) * 1000.0 / sr)
            prev_end = max(
                prev_end,
                int(item.get("end_sample") or start_sample),
            )
    covered_target_ms = (
        None
        if last_end is None
        else max(0.0, last_end - float(first_start_ms or 0.0))
    )
    covered_ms = (
        None if covered_target_ms is None else round(float(covered_target_ms), 2)
    )
    duration_vs = (
        None
        if covered_target_ms is None
        else round(covered_target_ms - duration_ms_sum, 2)
    )
    duration_matches = last_end is not None and abs(duration_vs or 0.0) <= 8.0
    internal_gap_ms_sum = round(sum(internal_gaps_ms), 2)
    max_internal_gap_ms = round(max(internal_gaps_ms, default=0.0), 2)
    idle_gaps_ms = [gap for gap in internal_gaps_ms if gap >= float(IDLE_MIN_MS)]
    coverage_ratio = (
        None
        if not covered_target_ms
        else round(min(1.0, duration_ms_sum / covered_target_ms), 6)
    )
    max_event = max(
        registry_events,
        key=lambda item: float(item.get("duration_ms") or 0.0),
        default=None,
    )
    max_single_viseme_ms = (
        None if max_event is None else round(float(max_event.get("duration_ms") or 0.0), 2)
    )
    over_800ms_count = sum(
        1 for item in registry_events if float(item.get("duration_ms") or 0.0) > 800.0
    )
    unaligned_tail_ms = (
        None
        if alignment_gap_ms is None
        else round(max(0.0, float(alignment_gap_ms)), 2)
    )

    def _round(value: Optional[float]) -> Optional[float]:
        return None if value is None else round(float(value), 2)

    active_end_sample = None
    if speech_active_end_sample is not None and int(speech_active_end_sample) >= 0:
        active_end_sample = int(speech_active_end_sample)
    last_end_sample = None
    if last_mouth_end_sample is not None and int(last_mouth_end_sample) >= 0:
        last_end_sample = int(last_mouth_end_sample)

    return {
        "type": "mouth_sync_summary",
        "sender": "stream_room_v7",
        "initiator": "tts_viseme",
        "conversation_id": conv.conversation_id,
        "turn_id": conv.active_turn_id,
        "stream_id": conv.stream_id,
        "sample_rate": max(1, int(sample_rate or SAMPLE_RATE)),
        "audio_total_samples": int(audio_total_samples or 0),
        "audio_total_ms": _round(audio_total_ms),
        "speech_active_end_sample": active_end_sample,
        "speech_active_end_ms": _round(speech_active_end_ms),
        "last_mouth_end_sample": last_end_sample,
        "last_mouth_end_ms": last_end,
        "alignment_gap_ms": _round(alignment_gap_ms),
        "audio_tail_silence_ms": _round(audio_tail_silence_ms),
        "seq_count": mouth_event_count,
        "seq_chars": "".join(seq_chars),
        "llm_chars": "".join(llm_chars),
        "tts_chars": "".join(tts_chars),
        "expected_syllable_count": len(expected),
        "mouth_event_count": mouth_event_count,
        "duration_ms_sum": duration_ms_sum,
        "first_mouth_start_ms": _round(first_start_ms),
        "covered_ms": covered_ms,
        "internal_gap_ms_sum": internal_gap_ms_sum,
        "max_internal_gap_ms": max_internal_gap_ms,
        "idle_gap_count": len(idle_gaps_ms),
        "idle_gap_ms_sum": round(sum(idle_gaps_ms), 2),
        "coverage_ratio": coverage_ratio,
        "duration_vs_last_end_ms": duration_vs,
        "duration_matches_last_end": duration_matches,
        "max_single_viseme_ms": max_single_viseme_ms,
        "max_single_viseme_char": None if max_event is None else max_event.get("char"),
        "max_single_viseme_viseme": None if max_event is None else max_event.get("viseme"),
        "max_single_viseme_start_ms": None if max_event is None else max_event.get("start_ms"),
        "max_single_viseme_end_ms": None if max_event is None else max_event.get("end_ms"),
        "over_800ms_count": over_800ms_count,
        # Hard invariant metrics. These remain zero unless code explicitly
        # re-introduces forbidden duration fabrication in the future.
        "synthetic_stretch_count": 0,
        "forced_fill_ms": 0.0,
        "unaligned_tail_ms": unaligned_tail_ms,
        "alignment_source": "qwen3_forced_aligner_0.6b+g2pw+vad_idle",
        "dropped_count": len(missing),
        "dropped_chars": missing[:48],
        "extra_seq_chars": extra[:48],
        "ts": now_ms(),
        "room": "main",
    }


def extract_segment_text(cumulative_text: str, previous_text: str) -> str:
    """reb_tts.data.text is cumulative spoken text; isolate this segment."""
    current = str(cumulative_text or "")
    previous = str(previous_text or "")
    if not current:
        return ""
    if previous and current.startswith(previous):
        return current[len(previous):]
    return current


class G2PWWorker:
    """Optional background g2pW. Coalesces to the latest text per submit."""

    def __init__(
        self,
        resolver: DualG2P,
        on_result: Callable[[str, str, List[Pron], int], None],
    ):
        self.resolver = resolver
        self.on_result = on_result
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stop = threading.Event()
        self._job: Optional[Tuple[int, str, str]] = None
        self.ready = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="viseme-g2pw",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._event.set()

    def submit(self, conversation_id: str, text: str, generation: int) -> None:
        if not text:
            return
        with self._lock:
            self._job = (generation, conversation_id, text)
        self._event.set()

    def cancel(self, conversation_id: str) -> None:
        cid = str(conversation_id or "")
        with self._lock:
            if self._job is not None and (not cid or self._job[1] == cid):
                self._job = None

    def _run(self) -> None:
        try:
            self.resolver.load()
            if self.resolver.g2pw is not None:
                try:
                    self.resolver.g2pw("我")
                except Exception as exc:
                    log("G2PW_ERR", f"warmup failed: {exc!r}")
        except Exception as exc:
            log("G2PW_ERR", f"background load failed: {exc!r}")
        self.ready.set()
        viseme_log(f"g2pW background engine={self.resolver.engine}")
        while not self._stop.is_set():
            if not self._event.wait(timeout=0.5):
                continue
            with self._lock:
                job = self._job
                self._job = None
                self._event.clear()
            if job is None:
                continue
            generation, cid, text = job
            try:
                t0 = time.perf_counter()
                prons = self.resolver.resolve(text)
                log(
                    "G2P_BG",
                    f"conv={cid[:8]} chars={len(text)} "
                    f"{(time.perf_counter() - t0) * 1000:.1f}ms",
                )
                self.on_result(cid, text, prons, generation)
            except Exception as exc:
                log("G2P_ERR", f"background resolve failed: {exc!r}")


class VisemeEngine:
    """Consume room messages, align real PCM, and emit mouth_script."""

    def __init__(
        self,
        resolver: DualG2P,
        send_mouth: Optional[Callable[[Dict[str, Any]], Any]] = None,
        forced_aligner: Optional[QwenForcedAlignerRuntime] = None,
    ):
        self.resolver = resolver
        self.send_mouth = send_mouth
        self.forced_aligner = forced_aligner
        self.g2pw_worker: Optional[G2PWWorker] = None
        self.drop_pending_mouth: Optional[Callable[[], None]] = None
        self.drop_pending_mouth_from_seq: Optional[Callable[[str, int], int]] = None
        self.conversations: Dict[str, ConversationState] = {}
        self.lock = threading.RLock()
        self.client_id = f"anonymous-{uuid.uuid4().hex[:8]}"
        self._active_cid: Optional[str] = None
        self.emit_enabled = True
        # Production uses MouthSendPacer, which can replace not-yet-delivered
        # events with the same (stream_id, seq) when final PCM refines timing.
        # Direct test sinks cannot retract already appended events.
        self.mouth_replacement_enabled = False

    def conversation(self, conversation_id: str) -> ConversationState:
        cid = str(conversation_id or "").strip() or "unknown"
        state = self.conversations.get(cid)
        if state is None:
            state = ConversationState(
                conversation_id=cid,
                stream_id=f"tts-viseme-v7-{cid[:10] or uuid.uuid4().hex[:10]}",
            )
            self.conversations[cid] = state
        return state

    def reset_conversation(
        self,
        conversation_id: str,
        reason: str,
        *,
        reopen: bool = False,
    ) -> None:
        cid = str(conversation_id or "").strip()
        targets = []
        with self.lock:
            if cid:
                conv = self.conversations.get(cid)
                if conv is not None:
                    targets.append(conv)
            else:
                targets.extend(
                    conv for conv in self.conversations.values() if not conv.cleared
                )
            for conv in targets:
                if (
                    not conv.latency_printed
                    and (conv.first_llm_ms or conv.first_pcm_ms or conv.script_compute_ms)
                ):
                    conv.latency_printed = True
                    print(format_latency_summary(conv), flush=True)
                conv.cleared = not reopen
                conv.mouth_epoch += 1
                for segment in conv.segments.values():
                    segment.aligner.reset()
                    segment.pcm_buf.clear()
                    segment.prons = None
                    segment.prons_text = ""
                    segment.authoritative_text = ""
                    segment.emitted = True
                    segment.final = True
                conv.segments.clear()
                conv.llm_text_buffer = ""
                conv.llm_final = False
                conv.speculative_text = ""
                conv.speculative_prons = None
                conv.g2p_generation += 1
                conv.g2p_submitted_text = ""
                conv.sample_cursor = 0
                conv.last_tts_text = ""
                conv.active_turn_id = None
                conv.first_llm_ms = None
                conv.first_pcm_ms = None
                conv.first_pcm_wall = None
                conv.first_script_ms = None
                conv.script_compute_ms.clear()
                conv.script_send_ms.clear()
                conv.llm_lag_ms.clear()
                conv.mouth_seq = 0
                conv.mouth_duration_ms_sum = 0.0
                conv.mouth_chars.clear()
                conv.mouth_event_count = 0
                conv.mouth_events_by_seq.clear()
                conv.latency_printed = False if reopen else True
                if reopen:
                    conv.stream_id = (
                        f"tts-viseme-v7-{conv.conversation_id[:10] or uuid.uuid4().hex[:10]}"
                    )
            if not reopen and (not cid or cid == self._active_cid):
                self._active_cid = None
        if self.g2pw_worker is not None:
            self.g2pw_worker.cancel(cid)
        if self.drop_pending_mouth is not None:
            self.drop_pending_mouth()
        log(
            "TTS_RESET",
            f"reason={reason} conv={(cid or '*')[:8]} "
            f"count={len(targets)} reopen={reopen}",
        )

    def on_sam_message(self, obj: Dict[str, Any]) -> None:
        if not isinstance(obj, dict):
            return
        msg_type = str(obj.get("type") or "").strip()
        if msg_type in ROOM_CONTROL_TYPES:
            return
        if msg_type == "mouth_script":
            return
        if msg_type == "viseme_control":
            self.on_viseme_control(obj)
            return
        if msg_type != "llm_delta":
            return
        if not self.emit_enabled:
            return
        self.on_llm_delta(obj)

    def on_viseme_control(self, obj: Dict[str, Any]) -> None:
        enabled = bool(obj.get("enabled"))
        self.emit_enabled = enabled
        if not enabled:
            self.reset_conversation("", "viseme_disabled")
        viseme_log(f"control enabled={enabled}")

    def on_llm_delta(self, obj: Dict[str, Any]) -> None:
        recv_ms = now_ms()
        cid = str(obj.get("conversation_id") or "").strip()
        turn_id = str(obj.get("turn_id") or cid).strip()
        text = obj.get("text")
        text = "" if text is None else str(text)
        final = bool(obj.get("final"))
        try:
            seq = int(obj.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        created_ms = obj.get("llm_delta_created_ms") or obj.get("ts")
        try:
            created_ms = int(created_ms) if created_ms is not None else recv_ms
        except (TypeError, ValueError):
            created_ms = recv_ms
        lag_ms = max(0.0, recv_ms - created_ms)

        submit_job: Optional[Tuple[str, str, int]] = None
        with self.lock:
            conv = self.conversation(cid or turn_id)
            if conv.cleared:
                return
            if conv.first_llm_ms is None:
                conv.first_llm_ms = recv_ms
            conv.llm_lag_ms.append(lag_ms)
            if text:
                conv.llm_text_buffer += text
            conv.llm_seq = seq
            conv.llm_final = final
            buffer = conv.llm_text_buffer
            if (
                self.g2pw_worker is not None
                and buffer
                and buffer != conv.g2p_submitted_text
            ):
                conv.g2p_generation += 1
                conv.g2p_submitted_text = buffer
                submit_job = (conv.conversation_id, buffer, conv.g2p_generation)

        if submit_job is not None and self.g2pw_worker is not None:
            self.g2pw_worker.submit(*submit_job)

        preview = text.replace("\n", "\\n")
        if len(preview) > 24:
            preview = preview[:24] + "…"
        print(
            f"[LLM_RX] conv={(cid or turn_id)[:8]} turn={turn_id[:8]} "
            f"seq={seq} text={preview!r} lag={lag_ms:.1f}ms final={final}",
            flush=True,
        )
        if final:
            log("LLM_RX", f"stream final conv={cid[:8]} chars={len(buffer)}")

    def apply_g2pw_result(
        self,
        conversation_id: str,
        text: str,
        prons: List[Pron],
        generation: int,
    ) -> None:
        with self.lock:
            conv = self.conversations.get(conversation_id)
            if conv is None or conv.cleared:
                return
            if generation != conv.g2p_generation:
                return
            if not text:
                return
            conv.speculative_text = text
            conv.speculative_prons = prons

    def _prons_for_text(
        self,
        conv: ConversationState,
        text: str,
        *,
        prev_text: str = "",
        prev_prons: Optional[List[Pron]] = None,
        emitted_indexes: Optional[set] = None,
    ) -> List[Pron]:
        base = self.resolver.resolve_pypinyin_incremental(text, prev_text, prev_prons)
        spec_text = conv.speculative_text or ""
        spec = conv.speculative_prons
        if not spec or not spec_text:
            return base
        idx = 0 if spec_text == text else spec_text.find(text)
        if spec_text != text and idx < 0:
            return base
        sliced = spec[idx:idx + len(text)] if spec_text != text else spec
        if len(sliced) != len(text):
            return base
        locked = emitted_indexes or set()
        merged: List[Pron] = []
        for i, p in enumerate(base):
            if i in locked:
                if prev_prons is not None and i < len(prev_prons):
                    merged.append(prev_prons[i])
                else:
                    merged.append(p)
                continue
            s = sliced[i]
            if s.selected and s.reason != "pypinyin_only":
                merged.append(
                    Pron(
                        i, s.char, s.g2pw, s.pypinyin, s.selected,
                        s.reason, s.final, s.viseme, s.script_id,
                    )
                )
            else:
                merged.append(p)
        return merged

    def _emit_segment(
        self,
        conv: ConversationState,
        segment: SegmentState,
        *,
        flush: bool = False,
    ) -> None:
        if conv.cleared:
            return
        epoch = conv.mouth_epoch
        text = segment.authoritative_text
        # A single duplex task can last tens of seconds. If the aligner has
        # stopped resolving characters, the old 120ms loop repeatedly ran MLX
        # over the entire growing tail (observed 13.7s / 3.86s per pass).
        # Keep the original PCM and text for a single final pass instead of
        # monopolizing the machine with speculative, unproductive rescans.
        if not flush:
            sr = max(1, int(segment.sample_rate or SAMPLE_RATE))
            resolved = max(
                0, segment.aligner.last_end_sample - segment.start_sample
            )
            pending_ms = (segment.total_samples - resolved) * 1000 / sr
            if pending_ms > ONLINE_MAX_UNRESOLVED_MS:
                if not segment.online_backlog_warned:
                    segment.online_backlog_warned = True
                    log("ALIGN_BACKLOG",
                        f"conv={conv.conversation_id[:8]} "
                        f"turn={segment.turn_id[:8]} unresolved={pending_ms:.0f}ms "
                        f"limit={ONLINE_MAX_UNRESOLVED_MS}ms; online alignment paused, "
                        "preserving PCM for final alignment")
                return
        pcm = segment.pcm
        if not pcm or not any(is_han(ch) for ch in text):
            if flush:
                print("VISeme skip: no matching text", flush=True)
            return
        sr = max(1, int(segment.sample_rate or SAMPLE_RATE))
        min_eval = max(1, int(sr * ONLINE_EVAL_MIN_MS / 1000.0))
        align_eval = max(1, int(sr * FORCED_ALIGN_EVAL_INTERVAL_MS / 1000.0))
        first_window = max(1, int(sr * FORCED_ALIGN_FIRST_WINDOW_MS / 1000.0))
        aligner = segment.aligner
        if not flush:
            if segment.total_samples < min_eval:
                return
            if not aligner.emitted_indexes and segment.total_samples < first_window:
                return
            # When the model returns no new phoneme, retry on newly arrived
            # audio with increasing spacing rather than rescanning almost the
            # same PCM every 120ms. A successful boundary resets the spacing.
            align_eval *= min(8, 2 ** min(aligner.empty_eval_count, 3))
            if (
                aligner.last_eval_samples
                and segment.total_samples - aligner.last_eval_samples < align_eval
            ):
                return
        t0 = time.perf_counter()
        prons = self._prons_for_text(
            conv,
            text,
            prev_text=segment.prons_text,
            prev_prons=segment.prons,
            emitted_indexes=aligner.emitted_indexes,
        )
        segment.prons = prons
        segment.prons_text = text
        send_wall = time.perf_counter()
        events, info = self._build_online_events(
            conv,
            segment,
            prons,
            pcm,
            send_wall,
            flush=flush,
        )
        aligner.last_eval_samples = segment.total_samples
        aligner.empty_eval_count = 0 if events else min(3, aligner.empty_eval_count + 1)
        compute_ms = (time.perf_counter() - t0) * 1000.0
        if conv.cleared or conv.mouth_epoch != epoch:
            aligner.reset()
            return
        if events:
            conv.script_compute_ms.append(compute_ms)
        log(
            "ALIGN",
            f"conv={conv.conversation_id[:8]} turn={segment.turn_id[:8]} "
            f"samples={segment.total_samples} start={segment.start_sample} "
            f"active={info.get('active_start')}..{info.get('active_end')} "
            f"events={len(events)} flush={flush} "
            f"mode={info.get('mode', 'online')} align={info.get('align_ms')}ms "
            f"window={info.get('analysis_start', 0)}+{info.get('analysis_samples', 0)} "
            f"text_chars={info.get('analysis_text_chars', 0)} "
            f"idle_max={info.get('max_idle_ms', 0)}ms compute={compute_ms:.1f}ms",
        )
        self._publish_mouth_events(conv, segment, events, epoch=epoch)
        if flush and not conv.latency_printed:
            conv.latency_printed = True
            print(format_latency_summary(conv), flush=True)

    def _publish_mouth_events(
        self,
        conv: ConversationState,
        segment: SegmentState,
        events: Sequence[Dict[str, Any]],
        *,
        epoch: int,
    ) -> None:
        if conv.cleared or conv.mouth_epoch != epoch:
            return
        for event in events:
            event_seq = int(event["seq"])
            is_replacement = event_seq in conv.mouth_events_by_seq
            event["conversation_id"] = conv.conversation_id
            event["turn_id"] = segment.turn_id
            event["ts"] = now_ms()
            event["from"] = self.client_id
            event["room"] = "main"
            send_t0 = time.perf_counter()
            accepted = True
            if self.send_mouth is not None and (
                not is_replacement or self.mouth_replacement_enabled
            ):
                # A pacer-backed sink returns False when this seq is already
                # committed/sent (or would insert behind committed history).
                # In that case the robot cannot receive the re-fit version, so
                # the registry/summary must keep the actually playable event.
                accepted = self.send_mouth(event) is not False
            elif is_replacement:
                accepted = False
            conv.script_send_ms.append((time.perf_counter() - send_t0) * 1000.0)
            if not accepted:
                log(
                    "MOUTH_KEEP",
                    f"conv={conv.conversation_id[:8]} seq={event_seq} "
                    "replacement rejected; committed history unchanged",
                )
                continue
            if conv.first_script_ms is None:
                conv.first_script_ms = now_ms()
            conv.mouth_events_by_seq[event_seq] = dict(event)
            conv.mouth_seq = max(conv.mouth_seq, event_seq + 1)
            ordered = [
                conv.mouth_events_by_seq[key]
                for key in sorted(conv.mouth_events_by_seq)
            ]
            conv.mouth_event_count = len(ordered)
            conv.mouth_duration_ms_sum = sum(
                float(item.get("duration_ms") or 0.0) for item in ordered
            )
            conv.mouth_chars = [
                str(item.get("char") or "").strip()
                for item in ordered
                if str(item.get("char") or "").strip()
            ]
            print(
                f"[MOUTH] conv={conv.conversation_id[:8]} seq={event['seq']} "
                f"char={event['char']} pinyin={event['pinyin']} "
                f"viseme={event['viseme']} script={event['script_id']} "
                f"start={event.get('start_sample', event['at_sample'])} "
                f"end={event.get('end_sample')} "
                f"duration_ms={event.get('duration_ms')} "
                f"start_ms={event.get('start_ms', event['at_ms'])} "
                f"replace={is_replacement}",
                flush=True,
            )

    def _build_online_events(
        self,
        conv: ConversationState,
        segment: SegmentState,
        prons: Sequence[Pron],
        pcm: bytes,
        send_wall: float,
        *,
        flush: bool,
    ) -> Tuple[List[Dict[str, Any]], dict]:
        sr = max(1, int(segment.sample_rate or SAMPLE_RATE))
        aligner = segment.aligner
        text = segment.authoritative_text
        syllables = syllables_for_text(text, prons)
        first_pcm_wall = conv.first_pcm_wall or segment.first_pcm_wall
        if not syllables:
            return [], {
                "active_start": aligner.speech_cursor,
                "active_end": segment.total_samples,
                "mode": "qwen_forced_aligner_idle",
            }
        if self.forced_aligner is None:
            return [], {
                "active_start": aligner.speech_cursor,
                "active_end": segment.total_samples,
                "mode": "qwen_forced_aligner_unavailable",
            }

        if segment.mouth_seq_start is None:
            segment.mouth_seq_start = conv.mouth_seq
        seq_start = int(segment.mouth_seq_start)
        ordinal_by_index = {pron.index: i for i, pron in enumerate(syllables)}

        resolved_indexes = aligner.emitted_indexes | aligner.skipped_indexes
        remaining_ordinals = [
            ordinal
            for ordinal, pron in enumerate(syllables)
            if pron.index not in resolved_indexes
        ]
        if not remaining_ordinals:
            return [], {
                "active_start": aligner.speech_cursor,
                "active_end": segment.total_samples,
                "mode": "qwen_forced_aligner_idle",
            }
        first_remaining = remaining_ordinals[0]
        context_ordinal = max(0, first_remaining - 2)
        analysis_start = 0
        if context_ordinal > 0:
            # A zero-duration/unresolved character can leave a seq gap. Anchor
            # the window to the nearest actually emitted predecessor instead
            # of falling back to sample zero and rescanning the whole stream.
            for anchor_ordinal in range(context_ordinal, -1, -1):
                context_event = conv.mouth_events_by_seq.get(seq_start + anchor_ordinal)
                if context_event is None:
                    continue
                context_ordinal = anchor_ordinal
                analysis_start = max(
                    0,
                    int(context_event.get("start_sample") or 0) - int(segment.start_sample),
                )
                break
        width = SAMPLE_WIDTH * max(1, int(segment.channels or 1))
        analysis_pcm = pcm[analysis_start * width:]
        max_text = max(4, int(FORCED_ALIGN_MAX_TEXT_CHARS))
        analysis_samples = len(analysis_pcm) // width
        available_s = analysis_samples / float(sr)
        # Do not force a short partial-audio window against dozens of future
        # characters. Qwen will pin those future tokens to zero width and can
        # withhold otherwise-stable early boundaries until the whole text
        # window is spoken. Keep only a few characters beyond the amount of
        # speech that could plausibly fit in the PCM currently available.
        dynamic_text_chars = max(
            4,
            min(
                max_text,
                int(available_s * max(1.0, FORCED_ALIGN_CHARS_PER_SEC)) + 2,
            ),
        )
        analysis_syllables = syllables[
            context_ordinal:min(len(syllables), context_ordinal + dynamic_text_chars)
        ]
        # Preserve punctuation between the first/last Han character; pauses are
        # acoustic evidence and should remain visible to the forced aligner.
        analysis_text = text[
            analysis_syllables[0].index:analysis_syllables[-1].index + 1
        ]
        aligned = self.forced_aligner.align_pcm(
            analysis_pcm,
            analysis_text,
            sample_rate=sr,
            channels=segment.channels,
            language="Chinese",
        )
        local_pairs = match_forced_chars(analysis_syllables, aligned, sr)
        all_pairs = [
            (pron, int(start) + analysis_start, int(end) + analysis_start)
            for pron, start, end in local_pairs
        ]
        active_start, active_end, vad = detect_active_span(pcm, sample_rate=sr)
        positive_pairs = [pair for pair in all_pairs if int(pair[2]) > int(pair[1])]
        idle_spans = aligned_idle_spans(positive_pairs, sr)
        info = {
            "mode": "qwen_forced_aligner_final" if flush else "qwen_forced_aligner_online",
            "active_start": active_start,
            "active_end": active_end,
            "aligned_items": len(aligned),
            "matched_items": len(all_pairs),
            "analysis_start": analysis_start,
            "analysis_samples": analysis_samples,
            "analysis_text_chars": len(analysis_syllables),
            "idle_spans": idle_spans,
            "max_idle_ms": round(
                max((end - start for start, end in idle_spans), default=0) * 1000.0 / sr,
                2,
            ),
            "align_ms": self.forced_aligner.last_align_ms,
            "vad": vad,
        }

        stable_end = max(
            0,
            segment.total_samples - int(sr * FORCED_ALIGN_HOLDBACK_MS / 1000.0),
        )
        pair_by_ordinal: Dict[int, Tuple[Pron, int, int]] = {}
        for pair in all_pairs:
            ordinal = ordinal_by_index.get(pair[0].index)
            if ordinal is not None:
                pair_by_ordinal[ordinal] = pair

        events: List[Dict[str, Any]] = []
        next_ordinal = first_remaining
        while next_ordinal < len(syllables):
            pron = syllables[next_ordinal]
            if pron.index in aligner.emitted_indexes or pron.index in aligner.skipped_indexes:
                next_ordinal += 1
                continue
            pair = pair_by_ordinal.get(next_ordinal)
            if pair is None:
                break
            _pair_pron, local_start, local_end = pair

            # Partial alignment frequently pins not-yet-spoken future tokens to
            # start==end. Never treat that as evidence that the character should
            # be dropped: online we simply wait for more PCM. On final flush a
            # remaining zero-width token is genuinely unresolved, so consume it
            # without fabricating a mouth duration and continue with later text.
            if int(local_end) <= int(local_start):
                if not flush:
                    break
                aligner.skipped_indexes.add(pron.index)
                log(
                    "ALIGN_ZERO",
                    f"conv={conv.conversation_id[:8]} turn={segment.turn_id[:8]} "
                    f"char={pron.char} ordinal={next_ordinal} sample={local_start}",
                )
                next_ordinal += 1
                continue

            if not flush and int(local_end) > stable_end:
                break
            start_sample = segment.start_sample + int(local_start)
            end_sample = segment.start_sample + int(local_end)
            if (
                not flush
                and aligner.last_end_sample >= 0
                and start_sample < aligner.last_end_sample
            ):
                overlap = aligner.last_end_sample - start_sample
                max_overlap = int(sr * max(0, FORCED_ALIGN_MAX_OVERLAP_MS) / 1000.0)
                # Qwen timestamps are quantized and adjacent phonemes can
                # legitimately overlap a small amount through coarticulation.
                # Preserve both acoustic boundaries exactly; only large
                # backwards jumps are treated as an unstable partial result.
                if start_sample < aligner.last_at_sample or overlap > max_overlap:
                    break

            seq = seq_start + next_ordinal

            event = make_mouth_event(
                stream_id=conv.stream_id,
                seq=seq,
                pron=pron,
                at_sample=start_sample,
                end_sample=end_sample,
                sample_rate=sr,
                send_wall=send_wall,
                first_pcm_wall=first_pcm_wall,
                timing_source=(
                    "qwen3_forced_aligner_final"
                    if flush
                    else "qwen3_forced_aligner_online"
                ),
            )
            if (
                segment.char_index_offset is not None
                and type(pron.index) is int
                and 0 <= pron.index < len(text)
                and text[pron.index] == pron.char
            ):
                # Pron.index is a raw-text codepoint index, not a syllable ordinal.
                event["char_index"] = segment.char_index_offset + pron.index
            if conv.stream_epoch is not None:
                event["stream_epoch"] = conv.stream_epoch
                if segment.sample_cursor_timing:
                    event["timing_mode"] = "speech_sample_v1"
            events.append(event)
            aligner.emitted_indexes.add(pron.index)
            aligner.last_at_sample = start_sample
            aligner.last_end_sample = max(aligner.last_end_sample, end_sample)
            aligner.speech_cursor = max(aligner.speech_cursor, int(local_end))
            next_ordinal += 1

        if flush and len(aligner.emitted_indexes | aligner.skipped_indexes) != len(syllables):
            log(
                "ALIGN_WAIT",
                f"final unresolved conv={conv.conversation_id[:8]} "
                f"turn={segment.turn_id[:8]} syllables={len(syllables)} "
                f"emitted={len(aligner.emitted_indexes)} skipped={len(aligner.skipped_indexes)} "
                f"matched={len(all_pairs)}",
            )
        return events, info

    def on_tts_message(self, obj: Dict[str, Any]) -> None:
        if not isinstance(obj, dict):
            return
        msg_type = str(obj.get("type") or "").strip().lower()
        if msg_type in ROOM_CONTROL_TYPES:
            return
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        if msg_type.startswith("tts.playback.") or bool(data.get("stop")):
            cid = str(data.get("conversation_id") or "").strip()
            log("TTS_CTRL", f"type={msg_type} stop={bool(data.get('stop'))} conv={cid[:8]}")
            if (
                bool(data.get("stop"))
                or msg_type.endswith(".clear")
                or msg_type.endswith(".cleared")
            ):
                with self.lock:
                    current = self.conversations.get(cid)
                    # The output pacer is shared by this one robot. An old or
                    # unidentified stop must not clear the active stream's queue.
                    if not cid or cid != self._active_cid or current is None or current.cleared:
                        return
                    if current is not None and current.stream_epoch is not None:
                        control_epoch = data.get("stream_epoch")
                        if type(control_epoch) is not int or control_epoch != current.stream_epoch:
                            return
                self.reset_conversation(cid, msg_type if not data.get("stop") else "stop")
            return
        if not self.emit_enabled:
            return
        if msg_type not in {"tts", "tts.audio", ""}:
            if not data.get("audio"):
                return

        recv_ms = now_ms()
        cid = str(data.get("conversation_id") or "").strip()
        turn_id = str(data.get("turn_id") or "").strip()
        try:
            seq = int(data.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        epoch_i = data.get("stream_epoch")
        if "stream_epoch" in data and (type(epoch_i) is not int or epoch_i <= 0):
            log("TTS_DROP", "invalid stream_epoch")
            return

        pcm, sample_rate, channels = decode_tts_pcm(data)
        samples = pcm_sample_count(pcm, channels)
        text = str(data.get("text") or "")
        final = bool(data.get("final"))
        try:
            audio_total_samples = int(data.get("audio_total_samples") or 0)
        except (TypeError, ValueError):
            audio_total_samples = 0
        try:
            audio_total_ms = float(data.get("audio_total_ms") or 0.0)
        except (TypeError, ValueError):
            audio_total_ms = 0.0

        if not pcm and not text and not final:
            return

        pending: List[Tuple[ConversationState, SegmentState, bool]] = []
        with self.lock:
            incoming_cid = cid or turn_id or "unknown"
            active = self.conversations.get(self._active_cid or "")
            if (
                active is not None and not active.cleared
                and incoming_cid != active.conversation_id
                and active.stream_epoch is not None
                and (epoch_i is None or epoch_i <= active.stream_epoch)
            ):
                log("TTS_DROP", f"old stream cannot replace active conv={incoming_cid[:8]}")
                return
            conv = self.conversation(incoming_cid)
            if conv.cleared:
                return
            if conv.stream_epoch is not None and (
                epoch_i is None or epoch_i < conv.stream_epoch
            ):
                log("TTS_DROP", f"stale or missing stream_epoch conv={incoming_cid[:8]}")
                return
            if self._active_cid and incoming_cid != self._active_cid:
                prev_cid = self._active_cid
                self.reset_conversation(prev_cid, "conversation_change")
            if (
                epoch_i is not None
                and epoch_i != conv.stream_epoch
                and (conv.stream_epoch is not None or conv.sample_cursor)
            ):
                self.reset_conversation(incoming_cid, "stream_epoch_change", reopen=True)
                conv = self.conversation(incoming_cid)
            if epoch_i is not None:
                conv.stream_epoch = epoch_i
            self._active_cid = incoming_cid
            if pcm:
                if conv.first_pcm_ms is None:
                    conv.first_pcm_ms = recv_ms
                if conv.first_pcm_wall is None:
                    conv.first_pcm_wall = time.perf_counter()
                lookup_id = turn_id
                if lookup_id and conv.active_turn_id and lookup_id != conv.active_turn_id:
                    prev = conv.segments.get(conv.active_turn_id)
                    if prev is not None and not prev.emitted and prev.total_samples:
                        prev.final = True
                        prev.emitted = True
                        conv.last_tts_text = prev.text_prefix + prev.authoritative_text
                        pending.append((conv, prev, True))
                segment = conv.segments.get(lookup_id) if lookup_id else None
                if segment is None:
                    segment = SegmentState(
                        turn_id=lookup_id or f"seg-{len(conv.segments)+1}",
                        start_sample=conv.sample_cursor,
                        sample_rate=sample_rate,
                        channels=channels,
                        text_prefix=conv.last_tts_text,
                        sample_cursor_timing=data.get("playback_protocol") == "sample_cursor_v1",
                    )
                    conv.segments[segment.turn_id] = segment
                    conv.active_turn_id = segment.turn_id
                if segment.first_pcm_recv_ms is None:
                    segment.first_pcm_recv_ms = recv_ms
                    segment.first_pcm_wall = time.perf_counter()
                segment.pcm_buf.extend(pcm)
                segment.total_samples += samples
                segment.sample_rate = sample_rate
                segment.channels = channels
                conv.sample_cursor += samples
                if text:
                    segment.authoritative_text = extract_segment_text(
                        text, segment.text_prefix
                    )
                    segment.char_index_offset = (
                        len(segment.text_prefix) if text.startswith(segment.text_prefix) else None
                    )
                if not segment.emitted and segment.authoritative_text:
                    pending.append((conv, segment, final))
                    if final:
                        segment.final = True
                        segment.emitted = True
                        conv.last_tts_text = segment.text_prefix + segment.authoritative_text
            elif final:
                active = conv.segments.get(turn_id or conv.active_turn_id or "")
                if active is not None and not active.emitted:
                    if text:
                        active.authoritative_text = extract_segment_text(
                            text, active.text_prefix
                        )
                        active.char_index_offset = (
                            len(active.text_prefix) if text.startswith(active.text_prefix) else None
                        )
                    active.final = True
                    active.emitted = True
                    conv.last_tts_text = active.text_prefix + active.authoritative_text
                    pending.append((conv, active, True))

        if TRACE_TTS_PCM:
            print(
                f"[TTS_RX] conv={(cid or 'unknown')[:8]} turn={(turn_id or '-')[:8]} "
                f"seq={seq} bytes={len(pcm)} sr={sample_rate} samples={samples} "
                f"text_chars={len(text)} final={final}",
                flush=True,
            )
        for conv_state, segment, should_flush in pending:
            self._emit_segment(conv_state, segment, flush=should_flush)
        if final:
            with self.lock:
                total_samples = audio_total_samples or conv.sample_cursor
                total_ms = audio_total_ms or (
                    total_samples * 1000.0 / max(1, sample_rate)
                )
                last_segment = max(
                    conv.segments.values(),
                    key=lambda seg: seg.start_sample + seg.total_samples,
                    default=None,
                )
                speech_active_end = -1
                speech_sr = max(1, sample_rate)
                if last_segment is not None and last_segment.total_samples > 0:
                    speech_sr = max(1, int(last_segment.sample_rate or sample_rate))
                    _active_start, active_end, _vad = detect_active_span(
                        last_segment.pcm,
                        sample_rate=speech_sr,
                    )
                    speech_active_end = last_segment.start_sample + int(active_end)
                last_mouth_end = max(
                    (
                        seg.aligner.last_end_sample
                        for seg in conv.segments.values()
                        if seg.aligner.last_end_sample >= 0
                    ),
                    default=-1,
                )
            if last_mouth_end >= 0:
                last_mouth_end_ms = last_mouth_end * 1000.0 / speech_sr
                speech_active_end_ms = (
                    speech_active_end * 1000.0 / speech_sr
                    if speech_active_end >= 0
                    else total_ms
                )
                alignment_gap_ms = speech_active_end_ms - last_mouth_end_ms
                audio_tail_silence_ms = total_ms - speech_active_end_ms
            else:
                last_mouth_end_ms = None
                speech_active_end_ms = (
                    speech_active_end * 1000.0 / speech_sr
                    if speech_active_end >= 0
                    else total_ms
                )
                alignment_gap_ms = None
                audio_tail_silence_ms = (
                    total_ms - speech_active_end_ms
                    if speech_active_end >= 0
                    else None
                )
            summary = build_mouth_sync_summary(
                conv,
                sample_rate=speech_sr,
                audio_total_samples=total_samples,
                audio_total_ms=total_ms,
                speech_active_end_sample=speech_active_end,
                speech_active_end_ms=speech_active_end_ms,
                last_mouth_end_sample=last_mouth_end if last_mouth_end >= 0 else None,
                last_mouth_end_ms=last_mouth_end_ms,
                alignment_gap_ms=alignment_gap_ms,
                audio_tail_silence_ms=audio_tail_silence_ms,
            )
            print(
                f"[SYNC_FINAL] conv={conv.conversation_id[:8]} "
                f"audio_total_ms={total_ms:.2f} "
                f"expected={summary.get('expected_syllable_count')} "
                f"emitted={summary.get('mouth_event_count')} "
                f"seq_chars={summary.get('seq_chars')} "
                f"duration_ms_sum={summary.get('duration_ms_sum')} "
                f"internal_gap_ms={summary.get('internal_gap_ms_sum')} "
                f"coverage={summary.get('coverage_ratio')} "
                f"last_mouth_end_ms={last_mouth_end_ms if last_mouth_end_ms is not None else 'none'} "
                f"speech_end_ms={summary.get('speech_active_end_ms')} "
                f"duration_match={summary.get('duration_matches_last_end')} "
                f"dropped={summary.get('dropped_count')}",
                flush=True,
            )
            summary["from"] = self.client_id
            should_send = bool(
                conv.last_tts_text
                or conv.mouth_event_count
                or conv.llm_text_buffer
                or last_mouth_end >= 0
            )
            if self.send_mouth is not None and should_send:
                self.send_mouth(summary)


class ReconnectingRoom:
    def __init__(
        self,
        url: str,
        name: str,
        on_message: Callable[[Dict[str, Any]], None],
        *,
        outgoing: Optional["queue.Queue[str]"] = None,
        reconnect_min: float = 0.5,
        reconnect_max: float = 5.0,
    ):
        self.url = url
        self.name = name
        self.on_message = on_message
        self.outgoing = outgoing
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self.stopped = threading.Event()
        self._ws = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._sender: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"viseme-{self.name}",
            daemon=True,
        )
        self._thread.start()
        if self.outgoing is not None:
            self._sender = threading.Thread(
                target=self._send_loop,
                name=f"viseme-{self.name}-tx",
                daemon=True,
            )
            self._sender.start()

    def stop(self) -> None:
        self.stopped.set()
        with self._lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self.connected.clear()
        self.subscribed.clear()

    def _run(self) -> None:
        import websocket

        backoff = self.reconnect_min
        while not self.stopped.is_set():
            try:
                viseme_log(f"{self.name} connecting {self.url}")
                ws = websocket.create_connection(
                    self.url,
                    timeout=8,
                    skip_utf8_validation=True,
                )
                ws.settimeout(1.0)
                with self._lock:
                    self._ws = ws
                self.connected.set()
                subscribe = {"type": "SUBSCRIBE", "data": {"types": ["*"]}}
                ws.send(json.dumps(subscribe, ensure_ascii=False, separators=(",", ":")))
                self.subscribed.set()
                backoff = self.reconnect_min
                viseme_log(f"{self.name} connected + subscribed")
                while not self.stopped.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if raw is None:
                        break
                    if isinstance(raw, (bytes, bytearray)):
                        continue
                    try:
                        obj = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        self.on_message(obj)
            except Exception as exc:
                if self.stopped.is_set():
                    break
                viseme_log(f"{self.name} disconnected: {exc}; retry in {backoff:.1f}s")
            finally:
                self.connected.clear()
                self.subscribed.clear()
                with self._lock:
                    ws = self._ws
                    self._ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if self.stopped.wait(backoff):
                break
            backoff = min(backoff * 2, self.reconnect_max)

    def _send_loop(self) -> None:
        assert self.outgoing is not None
        while not self.stopped.is_set():
            try:
                payload = self.outgoing.get(timeout=0.25)
            except queue.Empty:
                continue
            with self._lock:
                ws = self._ws
            if ws is None:
                continue
            try:
                ws.send(payload)
            except Exception as exc:
                log("WS_TX", f"{self.name} send failed: {exc}")


class MouthSendPacer:
    """Pace legacy mouths; promptly release device-owned sample timing."""

    def __init__(
        self,
        send: Callable[[Dict[str, Any]], None],
        *,
        lead_ms: float = MOUTH_SEND_LEAD_MS,
    ) -> None:
        self._send = send
        self._lead_s = max(0.0, float(lead_ms)) / 1000.0
        self._cv = threading.Condition()
        self._heap: List[Tuple[float, int, int, Dict[str, Any]]] = []
        self._counter = 0
        self._epoch = 0
        self._origin_wall: Optional[float] = None
        self._last_due = 0.0
        self._pending_versions: Dict[Tuple[str, int], int] = {}
        self._sent_keys: Set[Tuple[str, int]] = set()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="viseme-mouth-pacer",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, event: Dict[str, Any]) -> bool:
        item = dict(event)
        now = time.perf_counter()
        with self._cv:
            if self._stopped:
                return False
            if str(item.get("type") or "") == "mouth_script":
                key = self._mouth_key(item)
                if key is not None:
                    # Final PCM may re-fit a seq that is still waiting in the
                    # pacer. Replace that pending event instead of sending a
                    # duplicate. Once a seq has actually been released, it is
                    # immutable and the late replacement is ignored.
                    if key in self._sent_keys:
                        return False
                    if any(
                        sent_stream == key[0] and sent_seq > key[1]
                        for sent_stream, sent_seq in self._sent_keys
                    ):
                        # A previously missing seq cannot be inserted behind
                        # already released mouth history; playback has passed
                        # that point on the authoritative sample clock.
                        return False
                    version = self._pending_versions.get(key, 0) + 1
                    self._pending_versions[key] = version
                    item["_pacer_key"] = key
                    item["_pacer_version"] = version
                due = self._mouth_due(item, now)
            else:
                due = max(now, self._last_due)
            self._last_due = max(self._last_due, due)
            heapq.heappush(self._heap, (due, self._counter, self._epoch, item))
            self._counter += 1
            self._cv.notify()
            return True

    def drop(self) -> int:
        with self._cv:
            dropped = len(self._heap)
            self._heap.clear()
            self._epoch += 1
            self._origin_wall = None
            self._last_due = 0.0
            self._pending_versions.clear()
            self._sent_keys.clear()
            self._cv.notify()
        return dropped

    def drop_stream_from_seq(self, stream_id: str, min_seq: int) -> int:
        """Invalidate only not-yet-released mouth events at/after min_seq.

        Sent keys stay immutable. Pending heap entries are lazily discarded by
        version validation in ``_run`` after their key is removed here.
        """
        stream = str(stream_id or "").strip()
        threshold = int(min_seq)
        if not stream:
            return 0
        dropped = 0
        with self._cv:
            for key in list(self._pending_versions):
                if key[0] == stream and key[1] >= threshold and key not in self._sent_keys:
                    self._pending_versions.pop(key, None)
                    dropped += 1
            if dropped:
                self._cv.notify()
        return dropped

    def stop(self) -> None:
        with self._cv:
            self._stopped = True
            self._heap.clear()
            self._pending_versions.clear()
            self._sent_keys.clear()
            self._cv.notify()

    @staticmethod
    def _mouth_key(event: Dict[str, Any]) -> Optional[Tuple[str, int]]:
        stream_id = str(event.get("stream_id") or "").strip()
        if not stream_id:
            return None
        try:
            seq = int(event.get("seq"))
        except (TypeError, ValueError):
            return None
        return stream_id, seq

    def _mouth_due(self, event: Dict[str, Any], now: float) -> float:
        if (
            event.get("timing_mode") == "speech_sample_v1"
            and type(event.get("stream_epoch")) is int
            and event["stream_epoch"] > 0
        ):
            # The device owns this clock. Release alignment as soon as it exists
            # so Runtime can reserve word actions before the speaker reaches it.
            return now
        sr = max(1, int(event.get("sample_rate") or SAMPLE_RATE))
        at_sample = max(
            0,
            int(event.get("start_sample") or event.get("at_sample") or 0),
        )
        if self._origin_wall is None:
            self._origin_wall = now - (at_sample / float(sr))
        return self._origin_wall + (at_sample / float(sr)) - self._lead_s

    def _run(self) -> None:
        while True:
            with self._cv:
                if self._stopped:
                    return
                if not self._heap:
                    self._cv.wait()
                    continue
                due, _counter, epoch, event = self._heap[0]
                if epoch != self._epoch:
                    heapq.heappop(self._heap)
                    continue
                key = event.get("_pacer_key")
                if key is not None:
                    version = int(event.get("_pacer_version") or 0)
                    if self._pending_versions.get(key) != version:
                        heapq.heappop(self._heap)
                        continue
                wait = due - time.perf_counter()
                if wait > 0:
                    self._cv.wait(timeout=wait)
                    continue
                heapq.heappop(self._heap)
                if key is not None:
                    self._pending_versions.pop(key, None)
                    self._sent_keys.add(key)
                    event.pop("_pacer_key", None)
                    event.pop("_pacer_version", None)
            event["ts"] = now_ms()
            try:
                self._send(event)
            except Exception as exc:
                log("MOUTH_PACER", f"send failed: {exc}")


class BrainVisemeObserver:
    """Copy mouth_script to the brain observatory without delaying the room send."""

    def __init__(self, url: str) -> None:
        self.url = str(url or "").strip()
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=64)
        self._thread: Optional[threading.Thread] = None
        self._health_lock = threading.Lock()
        self._health: Dict[str, Any] = {
            "type": "viseme_health",
            "status": "starting",
            "pid": os.getpid(),
            "ts": now_ms(),
        }
        self._health_provider: Optional[Callable[[], Dict[str, Any]]] = None
        if self.url:
            self._thread = threading.Thread(
                target=self._run,
                name="viseme-observe",
                daemon=True,
            )
            self._thread.start()

    def set_health(self, status: str, **fields: Any) -> None:
        with self._health_lock:
            self._health.update(fields)
            self._health["type"] = "viseme_health"
            self._health["status"] = str(status or "unknown").strip().lower() or "unknown"
            self._health["pid"] = os.getpid()
            self._health["ts"] = now_ms()

    def set_health_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        self._health_provider = provider

    def publish(self, event: Dict[str, Any]) -> None:
        if not self.url:
            return
        try:
            self._q.put_nowait(event)
        except queue.Full:
            pass

    def _health_payload(self) -> Dict[str, Any]:
        with self._health_lock:
            payload = dict(self._health)
        provider = self._health_provider
        if provider is not None:
            try:
                extra = provider()
                if isinstance(extra, dict):
                    payload.update(extra)
            except Exception as exc:
                payload.update({"status": "degraded", "reason": f"health_provider:{type(exc).__name__}"})
        payload["type"] = "viseme_health"
        payload["pid"] = os.getpid()
        payload["ts"] = now_ms()
        return payload

    def _post(self, event: Dict[str, Any]) -> None:
        import urllib.request

        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        req = urllib.request.Request(
            self.url,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=0.6).read()

    def _run(self) -> None:
        import urllib.error
        last_health_sent = 0.0
        while True:
            event: Optional[Dict[str, Any]] = None
            try:
                event = self._q.get(timeout=1.0)
            except queue.Empty:
                pass
            try:
                if event is not None:
                    self._post(event)
            except (urllib.error.URLError, TimeoutError, OSError, Exception):
                pass
            now = time.monotonic()
            if now - last_health_sent >= 1.0:
                try:
                    self._post(self._health_payload())
                except (urllib.error.URLError, TimeoutError, OSError, Exception):
                    pass
                last_health_sent = now


def connect_mouth_output(
    engine: VisemeEngine,
    outgoing: "queue.Queue[str]",
    observer: BrainVisemeObserver,
) -> MouthSendPacer:
    """Wire the production mouth sink, pacing, observation and cancellation."""
    def deliver_mouth(event: Dict[str, Any]) -> None:
        timing_source = str(event.get("timing_source") or "").strip().lower()
        if not timing_source.startswith("qwen3_forced_aligner"):
            log(
                "MOUTH_DROP",
                f"reject non-authoritative timing seq={event.get('seq')} "
                f"source={timing_source or '-'}",
            )
            return
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        try:
            outgoing.put_nowait(payload)
        except queue.Full:
            log("MOUTH_DROP", f"seq={event.get('seq')} outgoing full")
        observer.publish(event)

    pacer = MouthSendPacer(deliver_mouth)

    def drop_pending_mouth() -> None:
        dropped = pacer.drop()
        while True:
            try:
                outgoing.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            log("MOUTH_DROP", f"cleared pending={dropped}")

    engine.send_mouth = pacer.enqueue
    engine.mouth_replacement_enabled = True
    engine.drop_pending_mouth = drop_pending_mouth
    engine.drop_pending_mouth_from_seq = pacer.drop_stream_from_seq
    return pacer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Listen reb_sam + reb_tts and prepare viseme state"
    )
    parser.add_argument("--env", default=".env")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument(
        "--sam-url",
        default=os.getenv("TTS_VISEME_SAM_WS_URL") or DEFAULT_SAM_URL,
    )
    parser.add_argument(
        "--tts-url",
        default=os.getenv("TTS_VISEME_TTS_WS_URL") or DEFAULT_TTS_URL,
    )
    parser.add_argument(
        "--conflict-policy",
        choices=["pypinyin", "g2pw"],
        default="pypinyin",
    )
    parser.add_argument("--map-only", action="store_true")
    parser.add_argument(
        "--self-test-pcm",
        action="store_true",
        help="run PCM syllable-boundary drift checks and exit",
    )
    parser.add_argument(
        "--reconnect-min",
        type=float,
        default=float(os.getenv("TTS_VISEME_RECONNECT_MIN_SEC") or "0.5"),
    )
    parser.add_argument(
        "--reconnect-max",
        type=float,
        default=float(os.getenv("TTS_VISEME_RECONNECT_MAX_SEC") or "5.0"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.env, override=False)

    if args.self_test_pcm:
        run_pcm_boundary_self_test()
        return 0

    resolver = DualG2P(args.conflict_policy)

    def _warmup_pypinyin() -> None:
        t0 = time.perf_counter()
        DualG2P.pypinyin_context("我")
        viseme_log(f"pypinyin warm {((time.perf_counter() - t0) * 1000):.0f}ms")

    threading.Thread(
        target=_warmup_pypinyin,
        name="viseme-pypinyin",
        daemon=True,
    ).start()

    if args.map_only:
        viseme_log("g2pW loading for --map-only...")
        resolver.load()
        prons = resolver.resolve(args.text)
        print_prons(args.text, prons, resolver.engine)
        viseme_log("--map-only complete")
        return 0

    # Only one viseme publisher may own reb_sam on a machine.  Multiple
    # sidecars generate the same conversation-derived stream_id/seq values;
    # an older process can therefore win the race with stale timing and make
    # the robot commit a wrong mouth boundary before the Qwen aligner result.
    singleton = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        singleton.bind(("127.0.0.1", SINGLETON_PORT))
        singleton.listen(1)
    except OSError as exc:
        singleton.close()
        raise RuntimeError(
            f"another TTS Viseme publisher is already running "
            f"(singleton port {SINGLETON_PORT} busy)"
        ) from exc

    outgoing: "queue.Queue[str]" = queue.Queue(maxsize=512)
    observer = BrainVisemeObserver(os.getenv("TTS_VISEME_OBSERVE_URL") or "")
    if observer.url:
        viseme_log(f"observe {observer.url}")

    forced_aligner = QwenForcedAlignerRuntime()
    observer.set_health("starting", reason="loading_forced_aligner")
    viseme_log("loading Qwen3-ForcedAligner-0.6B...")
    forced_aligner.load()
    observer.set_health(
        "starting",
        reason="forced_aligner_ready",
        aligner_backend=forced_aligner.backend,
    )
    viseme_log(f"forced aligner ready {forced_aligner.describe()}")

    engine = VisemeEngine(
        resolver,
        forced_aligner=forced_aligner,
    )
    pacer = connect_mouth_output(engine, outgoing, observer)
    # Pypinyin already handles the real-time pronunciation path. Loading a
    # second neural model on every robot is unnecessary for basic visemes.
    worker = None
    if os.getenv("TTS_VISEME_G2PW_ENABLED") == "1":
        worker = G2PWWorker(resolver, engine.apply_g2pw_result)
        engine.g2pw_worker = worker
        worker.start()
        g2pw_warmup_timeout = max(
            0.0, float(os.getenv("TTS_VISEME_G2PW_WARMUP_TIMEOUT_SEC") or "8.0"),
        )
        if g2pw_warmup_timeout > 0:
            worker.ready.wait(timeout=g2pw_warmup_timeout)
        if not worker.ready.is_set():
            log("G2PW_WARN", f"warmup timeout after {g2pw_warmup_timeout:.1f}s")
        else:
            viseme_log(f"g2pW startup ready engine={resolver.engine}")
    else:
        resolver.engine = "pypinyin_only"
        viseme_log("g2pW disabled; pypinyin handles real-time pronunciation")
    # G2PW/ONNX initialization can evict MLX's first online graph from hot
    # caches. Re-prime the exact ~500 ms / 4-char online shape before opening
    # room listeners so the first user turn pays no graph-compilation penalty.
    forced_aligner.warm_online(sample_rate=24000)
    viseme_log(f"forced aligner online warm ready {forced_aligner.describe()}")

    sam = ReconnectingRoom(
        args.sam_url,
        "reb_sam",
        engine.on_sam_message,
        outgoing=outgoing,
        reconnect_min=args.reconnect_min,
        reconnect_max=args.reconnect_max,
    )
    tts = ReconnectingRoom(
        args.tts_url,
        "reb_tts",
        engine.on_tts_message,
        reconnect_min=args.reconnect_min,
        reconnect_max=args.reconnect_max,
    )
    observer.set_health_provider(
        lambda: {
            "status": "ready" if sam.subscribed.is_set() and tts.subscribed.is_set() else "degraded",
            "sam_connected": sam.connected.is_set(),
            "tts_connected": tts.connected.is_set(),
            "sam_subscribed": sam.subscribed.is_set(),
            "tts_subscribed": tts.subscribed.is_set(),
            "aligner_backend": forced_aligner.backend,
            "reason": None if sam.subscribed.is_set() and tts.subscribed.is_set() else "room_not_subscribed",
        }
    )
    viseme_log("starting listeners")
    sam.start()
    tts.start()

    deadline = time.time() + 1.0
    while time.time() < deadline:
        if sam.subscribed.is_set() and tts.subscribed.is_set():
            break
        time.sleep(0.05)
    if sam.subscribed.is_set() and tts.subscribed.is_set():
        viseme_log("ready")
    else:
        viseme_log("rooms not subscribed yet; process stays up and will reconnect")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        viseme_log("stopping")
    finally:
        observer.set_health("stopping")
        pacer.stop()
        if worker is not None:
            worker.stop()
        sam.stop()
        tts.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
