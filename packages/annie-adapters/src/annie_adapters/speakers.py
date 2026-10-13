"""Global speaker gallery: embeddings only, no raw enrollment audio."""

import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

import numpy as np

MODEL_FILE = "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
MODEL_ID = "3dspeaker-campplus-zh-cn-16k"
SAMPLE_RATE = 16000
MAX_SPEAKERS = 24
_BAD_NAME = set("【】/\\\n\r\t")


class SpeakerModelMissing(RuntimeError):
    """Enrollment and recognition need the local ONNX file."""


def clean_name(name):
    if not isinstance(name, str):
        raise ValueError("名称不能为空")
    name = " ".join(name.strip().split())
    if not 1 <= len(name) <= 16:
        raise ValueError("名称需要 1 到 16 个字")
    if any(char in _BAD_NAME or not char.isprintable() for char in name):
        raise ValueError("名称不能包含换行或分隔符")
    return name


def unit_vector(values):
    vector = np.asarray(values, dtype=np.float32).reshape(-1)
    if vector.size == 0 or not np.all(np.isfinite(vector)):
        raise ValueError("声纹向量无效")
    norm = float(np.linalg.norm(vector))
    if norm < 1e-6:
        raise ValueError("声纹向量无效")
    return vector / np.float32(norm)


def pack_vector(vector):
    return np.ascontiguousarray(unit_vector(vector), dtype="<f4").tobytes()


def unpack_vector(blob):
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 4 or len(blob) % 4:
        raise ValueError("声纹向量损坏")
    return unit_vector(np.frombuffer(blob, dtype="<f4"))


def match_gallery(query, profiles, *, threshold, margin):
    """Return matched, unknown, or ambiguous. Never guess a close second place."""
    query = unit_vector(query)
    scored = []
    for profile in profiles:
        if profile.get("stale"):
            continue
        score = float(np.dot(query, profile["vector"]))
        scored.append((score, profile))
    if not scored:
        return {"status": "unknown", "score": None}
    scored.sort(key=lambda item: item[0], reverse=True)
    best, winner = scored[0]
    second = scored[1][0] if len(scored) > 1 else None
    payload = {"score": round(best, 3)}
    if best < threshold:
        return {"status": "unknown", **payload}
    if second is not None and best - second < margin:
        return {"status": "ambiguous", **payload}
    return {
        "status": "matched",
        "speaker_id": winner["speaker_id"],
        "name": winner["name"],
        **payload,
    }


def migrate_speaker_profiles(db):
    db.execute("""CREATE TABLE IF NOT EXISTS speaker_profiles (
        speaker_id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        embedding BLOB NOT NULL,
        dimension INTEGER NOT NULL,
        model TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL)""")


def _row(row, *, model_id, include_vector):
    item = {
        "speaker_id": row["speaker_id"],
        "name": row["name"],
        "model": row["model"],
        "duration_ms": row["duration_ms"],
        "created_ms": row["created_ms"],
        "updated_ms": row["updated_ms"],
        "stale": row["model"] != model_id,
    }
    if include_vector:
        item["vector"] = unpack_vector(row["embedding"])
        item["stale"] = item["stale"] or item["vector"].size != row["dimension"]
    return item


def load_speaker_profiles(store, model_id, *, include_vector):
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                """SELECT speaker_id,name,embedding,dimension,model,duration_ms,
                          created_ms,updated_ms
                   FROM speaker_profiles ORDER BY created_ms,name"""
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [_row(row, model_id=model_id, include_vector=include_vector) for row in rows]


def upsert_speaker_profile(store, *, name, vector, model, duration_ms, now_ms):
    name = clean_name(name)
    packed = pack_vector(vector)
    dimension = len(packed) // 4
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT speaker_id,created_ms FROM speaker_profiles WHERE name=?", (name,),
        ).fetchone()
        count = db.execute("SELECT COUNT(*) FROM speaker_profiles").fetchone()[0]
        if existing is None and count >= MAX_SPEAKERS:
            raise ValueError(f"最多登记 {MAX_SPEAKERS} 个声纹")
        speaker_id = existing["speaker_id"] if existing else uuid4().hex
        created_ms = existing["created_ms"] if existing else now_ms
        db.execute(
            """INSERT INTO speaker_profiles
               (speaker_id,name,embedding,dimension,model,duration_ms,created_ms,updated_ms)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 embedding=excluded.embedding, dimension=excluded.dimension,
                 model=excluded.model, duration_ms=excluded.duration_ms,
                 updated_ms=excluded.updated_ms""",
            (speaker_id, name, packed, dimension, model, duration_ms, created_ms, now_ms),
        )
    return {
        "speaker_id": speaker_id,
        "name": name,
        "model": model,
        "duration_ms": duration_ms,
        "created_ms": created_ms,
        "updated_ms": now_ms,
        "status": "updated" if existing else "created",
    }


def delete_speaker_profile(store, speaker_id):
    if not isinstance(speaker_id, str) or not speaker_id:
        raise ValueError("speaker_id required")
    with store.connect() as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT speaker_id,name FROM speaker_profiles WHERE speaker_id=?",
            (speaker_id,),
        ).fetchone()
        if row is None:
            return None
        db.execute("DELETE FROM speaker_profiles WHERE speaker_id=?", (speaker_id,))
    return {"speaker_id": row["speaker_id"], "name": row["name"]}


class SpeakerEmbedder:
    """Shared campplus extractor. compute() is serialized; callers may use a thread."""

    def __init__(self, model_path: Path):
        model_path = Path(model_path)
        if not model_path.is_file():
            raise SpeakerModelMissing(f"声纹模型不存在: {model_path.name}")
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise SpeakerModelMissing("sherpa-onnx 未安装，无法提取声纹") from exc
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=1, debug=False, provider="cpu",
        )
        if not config.validate():
            raise SpeakerModelMissing(f"声纹模型无法加载: {model_path.name}")
        self.model_id = MODEL_ID
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self._lock = threading.Lock()

    def embed(self, samples):
        audio = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
        if audio.size < SAMPLE_RATE:
            raise ValueError("声纹音频太短")
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=audio)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                raise RuntimeError("声纹模型没有产出向量")
            vector = self._extractor.compute(stream)
        return unit_vector(vector)
