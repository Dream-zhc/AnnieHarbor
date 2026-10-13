"""Persistent cloned voices and bounded independent TTS audition (not reb_tts)."""

import asyncio
import io
import os
import re
import shutil
import time
import wave
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from annie_adapters.tts import QwenTtsSocket
from annie_adapters.tts import command as tts_command
from annie_adapters.tts import task_id as new_task_id

TARGET_MODEL = "qwen-audio-3.0-tts-flash"
MAX_BYTES = 10 * 1024 * 1024
DEFAULT = "__default__"
VOICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{4,199}$")
PRESETS = (
    ("mandarin", "普通话·自然", "你好，我是小安妮。今天来试听一下我的新声音，自然吗？", "请用自然亲切的年轻女性语气表达，不要播音腔。", "zh", 1., 1.),
    ("henan", "河南话·郑州", "恁咋才回来啊？俺都搁这儿等恁半天了！", "请用自然地道的河南郑州话表达，保持原本声线。", "zh", 1., 1.),
    ("sichuan", "四川话·成都", "你娃儿终于回来咯，莫磨蹭，今天吃点巴适的！", "请用自然明显的四川成都话，像本地朋友聊天。", "zh", 1., 1.),
    ("cantonese", "粤语", "你终于返嚟喇，唔好再拖拖拉拉啦。", "请用自然地道的广东粤语表达，保持原本声线。", "zh", 1., 1.),
    ("dongbei", "东北话", "你可算回来了，别磨叽，麻溜儿的！", "请用自然东北口吻表达，语气爽朗幽默。", "zh", 1., 1.),
    ("shanghai", "上海话", "侬总算回来啦，今朝要请我吃好小菜。", "请用自然明显的上海话表达，保持原本声线。", "zh", 1., 1.),
    ("english", "英语", "Hey, you're finally back! I've been waiting for you.", "Speak naturally in American English with the original voice.", "en", 1., 1.),
    ("russian", "俄语", "Ты наконец-то вернулся! Я ждала тебя очень долго.", "Говорите естественно и тепло, сохраняя исходный голос.", "ru", 1., 1.),
    ("emotion", "情绪标签", "[serious]请注意！[excited]太好啦！[sad]不过我有点委屈。", "请让情感标签的情绪清楚但自然。", "zh", 1., 1.),
    ("expressions", "笑声与叹气", "[gasp]哇！[giggles]嘿嘿。[sighing]唉，总算回来了。", "保持自然对话感，适度呈现拟声标签。", "zh", 1., 1.),
    ("gentle", "指令·疲惫温柔", "今天确实有一点累。不过见到你之后，安心多了。", "请用疲惫但温柔、安心的语气，稍轻、略慢，保留声线。", "zh", .95, 1.),
    ("playful", "指令·调皮兴奋", "抓到你啦！偷偷跑出去那么久，还假装没事？", "请用聪明兴奋、略带坏笑的调皮语气表达。", "zh", 1.06, 1.),
    ("slow", "慢速低音", "请听听这段偏慢偏低的声音效果。", "请稳定清晰、语速偏慢地表达。", "zh", .65, .75),
    ("fast", "快速高音", "这次请听听语速较快的效果，有没有更活泼？", "请清晰活泼、略快地表达。", "zh", 1.45, 1.20),
    ("whisper", "耳语", "[asmr]请靠近一点听。[whispers]我现在用很轻的声音说话。", "请用轻柔自然、贴近耳语的方式表达。", "zh", 1., 1.),
)
PRESET_MAP = {item[0]: item for item in PRESETS}
AUTO_PREVIEWS = tuple(row[0] for row in PRESETS)


def check_audio(filename: str, audio: bytes):
    suffix = Path(filename).suffix.lower()
    if suffix not in {".wav", ".mp3", ".m4a"} or not 1024 <= len(audio) <= MAX_BYTES:
        raise ValueError("音频须为 WAV/MP3/M4A，大小不得超过 10MB")
    if suffix == ".wav":
        try:
            with wave.open(io.BytesIO(audio), "rb") as source:
                duration = source.getnframes() / max(1, source.getframerate())
                if (source.getsampwidth() != 2 or source.getframerate() < 16000
                        or not 5 <= duration <= 60):
                    raise ValueError("WAV 须为16bit、16kHz以上、5至60秒")
        except (wave.Error, EOFError) as exc:
            raise ValueError("无效的 WAV 音频") from exc
    elif suffix == ".mp3" and not (audio.startswith(b"ID3") or audio[0] == 255
                                   and audio[1] & 224 == 224):
        raise ValueError("文件内容不是 MP3")
    elif suffix == ".m4a" and audio[4:8] != b"ftyp":
        raise ValueError("文件内容不是 M4A")
    return suffix


def voice_name(value: str):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40:
        raise ValueError("音色备注须为1至40字符")
    return value.strip()


class DashscopeVoiceProvider:
    def __init__(self, key: str, ws_url: str, *, http_base: str | None = None):
        self.key, self.ws_url = key, ws_url
        parsed = urlparse(ws_url)
        if (parsed.scheme != "wss" or parsed.path != "/api-ws/v1/inference"
                or parsed.query or parsed.username or parsed.password):
            raise ValueError("unsupported clone TTS URL")
        host = parsed.hostname or ""
        if host == "dashscope.aliyuncs.com":
            default = "https://dashscope.aliyuncs.com/api/v1"
        elif re.fullmatch(r"[a-zA-Z0-9-]{1,80}\.cn-beijing\.maas\.aliyuncs\.com", host):
            default = f"https://{host}/api/v1"
        else:
            raise ValueError("voice cloning only supports Beijing DashScope endpoint")
        if http_base is not None and http_base.rstrip("/") != default:
            raise ValueError("custom clone endpoint differs from verified TTS endpoint")
        self.http_base = default

    async def clone(self, filename: str, audio: bytes, prefix: str, language: str):
        suffix = check_audio(filename, audio)
        if not re.fullmatch(r"[a-zA-Z0-9]{1,10}", prefix):
            raise ValueError("音色前缀不合法")
        if language not in {"zh", "en", "ru"}:
            raise ValueError("不支持的参考音频语言")
        headers = {"Authorization": f"Bearer {self.key}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(30., read=180.)) as client:
            policy = await client.get("https://dashscope.aliyuncs.com/api/v1/uploads",
                                      headers=headers,
                                      params={"action": "getPolicy", "model": TARGET_MODEL})
            policy.raise_for_status()
            data = policy.json()["data"]
            host = data["upload_host"]
            # Never send user audio or provider-issued credentials to other hosts.
            if not (isinstance(host, str) and host.startswith("https://")
                    and host.split("/", 3)[2].endswith(".aliyuncs.com")):
                raise ValueError("不可信的 OSS 上传主机")
            key = f'{data["upload_dir"].rstrip("/")}/{uuid4().hex}{suffix}'
            form = {
                "OSSAccessKeyId": data["oss_access_key_id"],
                "Signature": data["signature"], "policy": data["policy"],
                "x-oss-object-acl": data["x_oss_object_acl"],
                "x-oss-forbid-overwrite": data["x_oss_forbid_overwrite"],
                "key": key, "success_action_status": "200",
            }
            upload = await client.post(host, data=form,
                                       files={"file": (Path(filename).name, audio,
                                                       "application/octet-stream")})
            upload.raise_for_status()
            result = await client.post(
                self.http_base + "/services/audio/tts/customization",
                headers={**headers, "X-DashScope-OssResourceResolve": "enable"},
                json={"model": "voice-enrollment", "input": {
                    "action": "create_voice", "target_model": TARGET_MODEL,
                    "prefix": prefix, "url": f"oss://{key}", "language_hints": [language],
                }},
            )
            result.raise_for_status()
        output = result.json()
        value = (output.get("output") or {}).get("voice_id")
        if not isinstance(value, str) or not VOICE_ID.fullmatch(value):
            raise ValueError("复刻接口未返回合法 voice_id")
        return value

    async def synthesize(self, voice: str, preset: tuple):
        # Preview uses the same 24kHz PCM wire as production, not the Room.
        # Preview parameters intentionally match the isolated clone test cases;
        # live conversational SpeechStyle retains separate tighter limits.
        _key, _name, text, instruction, language, rate, pitch = preset
        task = new_task_id()
        socket = QwenTtsSocket(self.ws_url, self.key)
        pcm = bytearray()
        try:
            await socket.open()
            await socket.send(tts_command("run-task", task, voice=voice,
                                          instruction=instruction, rate=rate, pitch=pitch,
                                          language_hint=language))
            async with asyncio.timeout(50):
                while True:
                    item = await socket.recv()
                    if isinstance(item, bytes):
                        pcm.extend(item)
                        if len(pcm) > 24_000 * 2 * 60:
                            raise ValueError("试听音频超过60秒")
                        continue
                    event = (item.get("header") or {}).get("event")
                    if event == "task-started":
                        await socket.send(tts_command("continue-task", task, text=text))
                        await socket.send(tts_command("finish-task", task))
                    elif event == "task-finished":
                        break
                    elif event in {"task-failed", "error"}:
                        raise RuntimeError("试听 TTS 返回失败")
            if len(pcm) < 2 or len(pcm) % 2:
                raise ValueError("TTS 没有返回有效 PCM")
        finally:
            await socket.close()
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(pcm)
        return output.getvalue()


class VoiceProfiles:
    """One voice library shared by robots; preferences scoped by robot and chat."""
    def __init__(self, store, provider, default_voice: str):
        self.store, self.provider, self.default_voice = store, provider, default_voice
        self.voices = {}
        self.selections = {}
        self.tasks = set()
        self.lock = asyncio.Lock()
        self.preview_locks = {}
        self.preview_root = store.path.parent / "voice_previews"
        self.errors = {}

    def _initialize(self):
        with self.store.connect() as db:
            voices = db.execute(
                "SELECT voice_id,name,region,model,created_ms FROM cloned_voices ORDER BY created_ms DESC"
            ).fetchall()
            selections = db.execute(
                "SELECT robot_id,chat_id,persona_id,voice_id FROM voice_preferences"
            ).fetchall()
        return voices, selections

    async def start(self):
        voices, selections = await asyncio.to_thread(self._initialize)
        self.voices = {row[0]: dict(zip(
            ("voice_id", "name", "region", "model", "created_ms"), row
        )) for row in voices}
        self.selections = {(a, b, c): d for a, b, c, d in selections}

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    def choices(self):
        return sorted(self.voices.values(), key=lambda row: row["created_ms"], reverse=True)

    def match(self, name):
        if name in {DEFAULT, "默认", "默认音色", "原来的声音", "原声"}:
            return DEFAULT
        matches = [voice_id for voice_id, row in self.voices.items()
                   if name in (voice_id, row["name"])]
        if len(matches) != 1:
            raise ValueError("没有唯一匹配的已克隆音色")
        return matches[0]

    def label(self, voice_id):
        return (self.voices[voice_id]["name"] if voice_id in self.voices
                else "系统默认" if voice_id == self.default_voice else "未知音色")

    def resolve_request(self, identity, requested_name, user_text):
        """Resolve user intent, not an arbitrary voice guessed by the LLM.

        If the user says only '换一个音色', pick automatically *only* when exactly
        one different voice is available. With multiple alternatives the LLM
        must ask; the provider's proposed name is not authorization.
        """
        defaults = ("默认音色", "原来的声音", "原声", "默认")
        explicit_default = next((label for label in defaults if label in user_text), None)
        explicit_matches = [
            value for value, row in self.voices.items() if row["name"] in user_text
        ]
        if explicit_default and explicit_matches:
            return None, ["请明确选择默认音色还是克隆音色"]
        if explicit_default:
            if requested_name not in (DEFAULT, *defaults):
                raise ValueError("voice choice disagrees with explicitly requested default")
            return DEFAULT, None
        if len(explicit_matches) == 1:
            if requested_name not in (explicit_matches[0], self.voices[explicit_matches[0]]["name"]):
                raise ValueError("voice choice disagrees with user's named voice")
            return explicit_matches[0], None
        if len(explicit_matches) > 1:
            return None, ["系统默认", *(self.voices[v]["name"] for v in explicit_matches)]
        # A specified but unknown voice must not silently choose a different one.
        if requested_name and requested_name in user_text:
            return self.match(requested_name), None
        if not re.search(
            r"(?:换|切换)(?:一(?:个|种)|个|种|其他|别的|另一(?:个|种))?"
            r"(?:新)?(?:声音|音色|声线)|(?:来|用)(?:个|一种)?不同的(?:声音|音色)",
            user_text,
        ):
            return None, [self.label(v["voice_id"]) for v in self.choices()]
        current = self.resolved(identity)
        alternatives = ([DEFAULT] if current != self.default_voice else [])
        alternatives.extend(voice_id for voice_id in self.voices if voice_id != current)
        if len(alternatives) == 1:
            return alternatives[0], None
        return None, [self.label(voice_id) if voice_id != DEFAULT else "系统默认"
                      for voice_id in alternatives]

    def resolved(self, identity):
        robot, chat, persona = (identity[key] for key in ("robot_id", "chat_id", "persona_id"))
        selected = self.selections.get((robot, chat, persona))
        if selected is None:
            selected = self.selections.get((robot, "", ""))
        return selected if selected in self.voices else self.default_voice

    def snapshot(self, identity):
        robot, chat, persona = (identity[k] for k in ("robot_id", "chat_id", "persona_id"))
        active = self.resolved(identity)
        return {"voices": self.choices(), "active_voice_id": active,
                "default_voice_id": self.default_voice,
                "robot_voice_id": self.selections.get((robot, "", ""), DEFAULT),
                "chat_voice_id": self.selections.get((robot, chat, persona), DEFAULT),
                "previews": [
                    {"key": key, "name": name,
                     "ready": self.preview_path(active, key).exists() if active in self.voices else False}
                    for key, name, *_ in PRESETS
                ]}

    async def select(self, identity, voice_id: str, scope="chat"):
        if scope not in {"chat", "robot"}:
            raise ValueError("scope must be chat or robot")
        if voice_id != DEFAULT and voice_id not in self.voices:
            raise ValueError("音色不在已克隆列表")
        previous = self.resolved(identity)
        robot = identity["robot_id"]
        chat, persona = ((identity["chat_id"], identity["persona_id"])
                         if scope == "chat" else ("", ""))
        key = robot, chat, persona
        def save():
            with self.store.connect() as db:
                if voice_id == DEFAULT:
                    db.execute("DELETE FROM voice_preferences WHERE robot_id=? AND chat_id=? AND persona_id=?", key)
                else:
                    db.execute("""INSERT INTO voice_preferences VALUES(?,?,?,?,?)
                        ON CONFLICT(robot_id,chat_id,persona_id) DO UPDATE SET
                        voice_id=excluded.voice_id,updated_ms=excluded.updated_ms""",
                        (*key, voice_id, time.time_ns() // 1_000_000))
        await asyncio.to_thread(save)
        if voice_id == DEFAULT:
            self.selections.pop(key, None)
        else:
            self.selections[key] = voice_id
        active = self.resolved(identity)
        return {"status": "selected" if active != previous else "already_selected",
                "scope": scope, "voice_name": self.label(active),
                "active_voice_id": active, "effective": "next_tts_task"}

    async def rename(self, voice_id, name):
        name = voice_name(name)
        if voice_id not in self.voices:
            raise ValueError("unknown voice")
        if any(row["name"].casefold() == name.casefold() and key != voice_id
               for key, row in self.voices.items()):
            raise ValueError("音色名称已被占用")
        def update():
            with self.store.connect() as db:
                db.execute("UPDATE cloned_voices SET name=? WHERE voice_id=?", (name, voice_id))
        await asyncio.to_thread(update)
        self.voices[voice_id]["name"] = name
        return self.voices[voice_id]

    async def delete(self, voice_id):
        if voice_id not in self.voices:
            raise ValueError("unknown voice")
        def remove():
            with self.store.connect() as db:
                db.execute("DELETE FROM cloned_voices WHERE voice_id=?", (voice_id,))
                db.execute("DELETE FROM voice_preferences WHERE voice_id=?", (voice_id,))
        await asyncio.to_thread(remove)
        self.voices.pop(voice_id, None)
        self.selections = {k: v for k, v in self.selections.items() if v != voice_id}
        folder = self.preview_root / sha256(voice_id.encode()).hexdigest()[:24]
        await asyncio.to_thread(shutil.rmtree, folder, True)
        # Does not delete provider-hosted clone, only local membership.
        return {"status": "deleted_locally"}

    async def clone(self, name, filename, audio, *, language="zh"):
        name = voice_name(name)
        async with self.lock:
            if any(row["name"].casefold() == name.casefold()
                   for row in self.voices.values()):
                raise ValueError("音色名称已被占用")
            prefix = "annie" + uuid4().hex[:5]
            voice_id = await self.provider.clone(filename, audio, prefix, language)
            now = time.time_ns() // 1_000_000
            def save():
                with self.store.connect() as db:
                    db.execute("INSERT INTO cloned_voices VALUES(?,?,?,?,?)",
                               (voice_id, name, "beijing", TARGET_MODEL, now))
            await asyncio.to_thread(save)
            self.voices[voice_id] = {
                "voice_id": voice_id, "name": name, "region": "beijing",
                "model": TARGET_MODEL, "created_ms": now,
            }
            self._schedule(self.build_previews(voice_id, AUTO_PREVIEWS))
            return {"voice_id": voice_id, "name": name,
                    "preview_status": "generating", "preview_count": len(AUTO_PREVIEWS)}

    def preview_path(self, voice_id, key):
        if voice_id not in self.voices or key not in PRESET_MAP:
            raise ValueError("unknown voice or preview")
        folder = sha256(voice_id.encode()).hexdigest()[:24]
        return self.preview_root / folder / (key + ".wav")

    async def preview(self, voice_id, key):
        path = self.preview_path(voice_id, key)
        if path.is_file():
            return path
        lock = self.preview_locks.setdefault((voice_id, key), asyncio.Lock())
        async with lock:
            if path.is_file():
                return path
            content = await self.provider.synthesize(voice_id, PRESET_MAP[key])
            if voice_id not in self.voices:
                raise ValueError("voice removed during preview generation")
            def save():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp-" + uuid4().hex)
                try:
                    tmp.write_bytes(content)
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
            await asyncio.to_thread(save)
        return path

    def _schedule(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def build_previews(self, voice_id, keys):
        for key in keys:
            try:
                await self.preview(voice_id, key)
                self.errors.pop((voice_id, key), None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors[(voice_id, key)] = type(exc).__name__

    def preview_state(self, voice_id):
        return [{"key": key, "name": name, "text": text,
                 "instruction": instruction, "language_hint": language,
                 "rate": rate, "pitch": pitch,
                 "ready": self.preview_path(voice_id, key).is_file(),
                 "error": self.errors.get((voice_id, key))}
                for key, name, text, instruction, language, rate, pitch in PRESETS]
