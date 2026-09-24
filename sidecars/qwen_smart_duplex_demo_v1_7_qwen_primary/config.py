from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class TTSEndpoint:
    url: str
    send_workspace_header: bool
    label: str

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc


@dataclass(frozen=True, slots=True)
class Settings:
    api_key: str = os.getenv("DASHSCOPE_API_KEY", "").strip()
    workspace_id: str = os.getenv("ALIYUN_WORKSPACE_ID", "").strip()

    audio_model: str = os.getenv("QWEN_AUDIO_MODEL", "qwen-audio-3.0-realtime-flash").strip()
    llm_model: str = os.getenv("LLM_MODEL", "qwen3.7-flash").strip()
    tts_model: str = os.getenv("TTS_MODEL", "qwen-audio-3.0-tts-flash").strip()
    tts_voice: str = os.getenv("TTS_VOICE", "longanhuan_v3.6").strip()

    system_prompt: str = os.getenv(
        "SYSTEM_PROMPT",
        "你叫小安妮，是一个自然、有情感但不夸张的中文语音伙伴。回答简短、口语化。",
    )
    audio_sensor_prompt: str = os.getenv(
        "AUDIO_SENSOR_PROMPT",
        "你只作为实时听觉与轮次判断协处理器。若生成回答，只输出一个句号。",
    )
    audio_max_history_turns: int = _int("AUDIO_MAX_HISTORY_TURNS", 6)
    # Qwen-Audio 线上会在长时间没有完成 response 时主动关闭会话。
    # Demo 通过静默 heartbeat + 自动重连保持 ASR/smart_turn 长会话可用。
    audio_sensor_heartbeat_sec: int = _int("AUDIO_SENSOR_HEARTBEAT_SEC", 120)
    audio_sensor_reconnect_buffer_ms: int = _int("AUDIO_SENSOR_RECONNECT_BUFFER_MS", 1600)
    audio_sensor_reconnect_max_sec: int = _int("AUDIO_SENSOR_RECONNECT_MAX_SEC", 8)
    tts_instruction: str = os.getenv(
        "TTS_INSTRUCTION",
        "自然、亲近、温柔，语速自然偏快，停顿短，不要播音腔。",
    ).strip()
    tts_rate: float = _float("TTS_RATE", 1.0)
    tts_pitch: float = _float("TTS_PITCH", 1.0)
    tts_volume: int = _int("TTS_VOLUME", 50)
    tts_endpoint_mode: str = os.getenv("TTS_ENDPOINT_MODE", "auto").strip().lower()
    tts_ws_url_override: str = os.getenv("TTS_WS_URL", "").strip().rstrip("/")
    tts_preflight: bool = _bool("TTS_PREFLIGHT", True)

    first_tts_chars: int = _int("FIRST_TTS_CHARS", 10)
    next_tts_chars: int = _int("NEXT_TTS_CHARS", 28)
    meaningful_stable_chars: int = _int("MEANINGFUL_STABLE_CHARS", 3)

    # Barge-in 默认由 Qwen-Audio smart_turn 主导。浏览器本地 dBFS/SNR
    # 只用于观测，不再触发静音或丢弃音频。收到云端 speech_started 后
    # 暂停播放并完整保留队列；turn_invalid 时原位续播，有效文本时硬切。
    barge_mode: str = os.getenv("BARGE_MODE", "qwen").strip().lower()
    barge_absolute_dbfs: float = _float("BARGE_ABSOLUTE_DBFS", -46.0)
    barge_min_snr_db: float = _float("BARGE_MIN_SNR_DB", 7.0)
    barge_candidate_frames: int = _int("BARGE_CANDIDATE_FRAMES", 2)
    barge_local_timeout_ms: int = _int("BARGE_LOCAL_TIMEOUT_MS", 700)
    barge_local_arm_delay_ms: int = _int("BARGE_LOCAL_ARM_DELAY_MS", 180)
    barge_local_release_frames: int = _int("BARGE_LOCAL_RELEASE_FRAMES", 4)
    barge_hold_watchdog_ms: int = _int("BARGE_HOLD_WATCHDOG_MS", 950)
    barge_fade_out_ms: float = _float("BARGE_FADE_OUT_MS", 3.0)
    barge_fade_in_ms: float = _float("BARGE_FADE_IN_MS", 12.0)
    # 正常播放队列是完整音频的可靠缓冲，不能像旧版一样限制为 1.2 秒后
    # 直接丢包，否则 TTS 以高于实时速度返回时会把一句话后半段截断。
    playback_max_queue_ms: int = _int("PLAYBACK_MAX_QUEUE_MS", 30000)
    # 首包到达后先积累少量音频再开始消费，吸收 TTS 网络抖动。
    playback_prebuffer_ms: int = _int("PLAYBACK_PREBUFFER_MS", 180)
    # 播放中断粮时重新积累该时长再恢复，避免连续细碎卡顿。
    playback_rebuffer_ms: int = _int("PLAYBACK_REBUFFER_MS", 100)
    # 仅 legacy 本地推测模式使用。Qwen 主导模式会完整保留暂停期间音频，
    # 避免误判恢复后出现缺字、跳句或一句话提前结束。
    barge_hold_queue_ms: int = _int("BARGE_HOLD_QUEUE_MS", 240)
    barge_false_candidate_limit: int = _int("BARGE_FALSE_CANDIDATE_LIMIT", 2)
    barge_false_candidate_window_ms: int = _int("BARGE_FALSE_CANDIDATE_WINDOW_MS", 8000)
    barge_local_suppress_ms: int = _int("BARGE_LOCAL_SUPPRESS_MS", 15000)
    hard_stop_words_raw: str = os.getenv(
        "HARD_STOP_WORDS", "停,等一下,别说了,闭嘴,打住,先别说,换个问题"
    )

    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = _int("PORT", 8765)
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @property
    def hard_stop_words(self) -> tuple[str, ...]:
        return tuple(x.strip() for x in self.hard_stop_words_raw.split(",") if x.strip())

    @property
    def audio_ws_url(self) -> str:
        if self.workspace_id:
            host = f"{self.workspace_id}.cn-beijing.maas.aliyuncs.com"
        else:
            host = "dashscope.aliyuncs.com"
        return f"wss://{host}/api-ws/v1/realtime?model={self.audio_model}"

    @property
    def tts_endpoints(self) -> tuple[TTSEndpoint, ...]:
        """按优先级给出 TTS WebSocket 端点。

        官方固定路径是 /api-ws/v1/inference（无尾随斜杠）。
        auto 模式在业务空间专属域名失败时回退公共域名，公共域名通过
        X-DashScope-WorkSpace 维持业务空间路由。
        """
        if self.tts_ws_url_override:
            return (
                TTSEndpoint(
                    url=self.tts_ws_url_override,
                    send_workspace_header=bool(self.workspace_id),
                    label="override",
                ),
            )

        public = TTSEndpoint(
            url="wss://dashscope.aliyuncs.com/api-ws/v1/inference",
            send_workspace_header=bool(self.workspace_id),
            label="public",
        )
        if not self.workspace_id:
            return (public,)

        dedicated = TTSEndpoint(
            url=f"wss://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference",
            # 专属域名已包含业务空间路由，避免重复注入造成网关差异。
            send_workspace_header=False,
            label="workspace",
        )
        if self.tts_endpoint_mode == "public":
            return (public,)
        if self.tts_endpoint_mode == "workspace":
            return (dedicated,)
        return (dedicated, public)

    @property
    def llm_base_url(self) -> str:
        if self.workspace_id:
            return f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def validate_for_cloud(self) -> None:
        if not self.api_key or self.api_key.startswith("sk-xxxx"):
            raise RuntimeError("请先在 .env 中填写 DASHSCOPE_API_KEY。")
        if self.tts_endpoint_mode not in {"auto", "workspace", "public"}:
            raise RuntimeError("TTS_ENDPOINT_MODE 只能是 auto、workspace 或 public。")
        if not 0 <= self.tts_volume <= 100:
            raise RuntimeError("TTS_VOLUME 必须在 0 到 100 之间。")
        if not 0.5 <= self.tts_rate <= 2.0:
            raise RuntimeError("TTS_RATE 必须在 0.5 到 2.0 之间。")
        if not 0.5 <= self.tts_pitch <= 2.0:
            raise RuntimeError("TTS_PITCH 必须在 0.5 到 2.0 之间。")
        if self.barge_mode not in {"qwen", "cloud-hard", "hybrid", "semantic", "hard", "duck"}:
            raise RuntimeError(
                "BARGE_MODE 只能是 qwen、cloud-hard、hybrid、semantic、hard 或 duck。"
            )
        if self.barge_candidate_frames < 1:
            raise RuntimeError("BARGE_CANDIDATE_FRAMES 必须大于 0。")
        if self.playback_max_queue_ms < 5000:
            raise RuntimeError("PLAYBACK_MAX_QUEUE_MS 不应低于 5000。")
        if not 60 <= self.playback_prebuffer_ms <= 800:
            raise RuntimeError("PLAYBACK_PREBUFFER_MS 建议在 60 到 800 之间。")
        if not 40 <= self.playback_rebuffer_ms <= self.playback_prebuffer_ms:
            raise RuntimeError("PLAYBACK_REBUFFER_MS 应在 40ms 到 PLAYBACK_PREBUFFER_MS 之间。")
        if not 80 <= self.barge_hold_queue_ms <= 500:
            raise RuntimeError("BARGE_HOLD_QUEUE_MS 建议在 80 到 500 之间。")
        if not 0 <= self.barge_local_arm_delay_ms <= 1000:
            raise RuntimeError("BARGE_LOCAL_ARM_DELAY_MS 必须在 0 到 1000 之间。")
        if self.barge_local_release_frames < 1:
            raise RuntimeError("BARGE_LOCAL_RELEASE_FRAMES 必须大于 0。")
        if not 30 <= self.audio_sensor_heartbeat_sec <= 170:
            raise RuntimeError("AUDIO_SENSOR_HEARTBEAT_SEC 必须在 30 到 170 秒之间。")


settings = Settings()
