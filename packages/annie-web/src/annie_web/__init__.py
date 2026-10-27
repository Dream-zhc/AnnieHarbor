"""Local Dashboard plus Phase 3 session, ledger, and PCM diagnostics."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from annie_core import InputRejectedError, RobotQueueFullError, RobotUnavailableError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from annie_web.exhibit import exhibit_snapshot
from annie_web.exhibit_video import ExhibitLiveKit

logger = logging.getLogger(__name__)


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class TextViewedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=128)
    persona_id: str = Field(min_length=1, max_length=128)
    session_epoch: int = Field(ge=0)
    generation: int = Field(ge=0)


class SpeechExpressionIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    chat_id: str = Field(min_length=1, max_length=128)
    persona_id: str = Field(min_length=1, max_length=128)
    session_epoch: int = Field(ge=0)
    generation: int = Field(ge=0)


class AcousticSidecarInput(SpeechExpressionIdentity):
    enabled: bool


class LanguagePreferenceClear(SpeechExpressionIdentity):
    scope: str = Field(pattern='^(chat|robot)$')
    request_id: str = Field(min_length=1, max_length=128)


class LanguagePreferenceInput(LanguagePreferenceClear):
    language: str = Field(min_length=2, max_length=8)
    dialect: str = Field(min_length=1, max_length=32)


class VoiceSelectionInput(SpeechExpressionIdentity):
    voice_id: str = Field(min_length=5, max_length=200)
    scope: str = Field(pattern="^(chat|robot)$")


class VoiceRenameInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=40)


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=128)
    persona_id: str = Field(min_length=1, max_length=128)
    persona_revision: int = Field(default=1, ge=1)
    style_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("chat_id", "persona_id")
    @classmethod
    def strip_identity(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("session identity must not be blank")
        return value


class PersonaInstanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=40)


class PlaybackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    published_text: str | None = Field(default=None, max_length=8000)
    spoken_text: str | None = Field(default=None, max_length=8000)
    evidence_status: str = Field(min_length=1, max_length=64)
    playback_state: str = Field(min_length=1, max_length=32)


class ResponseRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=64)
    dedupe_key: str = Field(min_length=1, max_length=256)
    ttl_ms: int = Field(ge=1, le=60000)


class ReminderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=200)
    delay_ms: int = Field(ge=1000, le=86_400_000)
    request_key: str = Field(min_length=1, max_length=128)


class ReminderAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(pause|resume|cancel)$")


class SocialStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict
    source: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    source_turn_id: str | None = Field(default=None, max_length=128)
    ttl_ms: int | None = Field(default=None, ge=1, le=86_400_000)


class MemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(semantic|persona|episodes)$")
    key: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=300)


class ChatMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=300)
    expires_at_ms: int | None = None
    chat_id: str = Field(min_length=1, max_length=128)
    persona_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)


class ChatMemoryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    text: str | None = Field(default=None, min_length=1, max_length=300)
    chat_id: str = Field(min_length=1, max_length=128)
    persona_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)


class MemoryCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=300)


class MemoryForget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


def create_app(runtime, dashboard_dir: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="Annie Harbor — Phase 3 Session Control", lifespan=lifespan)
    exhibit_video = ExhibitLiveKit()

    @app.middleware("http")
    async def same_origin_mutations(request: Request, call_next):
        # The intranet UI uses the existing same-origin API over HTTP or HTTPS.
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "origin_mismatch"}, status_code=403)
        return await call_next(request)

    @app.get("/api/exhibit/session")
    async def exhibit_session():
        # Keep the response compatible with already-open older front-end tabs.
        return {"configured": True, "role": "operator", "access_mode": "intranet"}

    @app.get("/api/exhibit/robots")
    async def exhibit_robots():
        return [{"robot_id": k, "room_id": v.config.room_id}
                for k, v in runtime.robots.items()]

    @app.get("/api/exhibit/robots/{robot_id}/snapshot")
    async def exhibit_robot_snapshot(robot_id: str):
        require_robot(robot_id)
        return await exhibit_snapshot(runtime, robot_id)

    @app.get("/api/exhibit/robots/{robot_id}/video-config")
    async def exhibit_video_config(robot_id: str):
        require_robot(robot_id)
        result = exhibit_video.config(robot_id)
        if result is None:
            raise HTTPException(503, "livekit_not_configured")
        return result

    def require_robot(robot_id: str):
        if robot_id not in runtime.robots:
            raise HTTPException(404, "unknown robot_id")

    @app.get("/health")
    async def health():
        data = runtime.health()
        return JSONResponse(data, status_code=200 if data["ready"] else 503)

    @app.get("/api/robots")
    async def robots():
        return runtime.health()["robots"]

    @app.get("/api/personas")
    async def personas():
        return [{"persona_id": key, "label": value["label"]}
                for key, value in runtime.persona_profiles.items()]

    @app.get("/api/robots/{robot_id}/inputs")
    async def inputs(robot_id: str):
        require_robot(robot_id)
        return runtime.input_stats(robot_id)

    @app.get("/api/phase2/rooms")
    async def phase2_rooms():
        return runtime.room_routes()

    @app.get("/api/robots/{robot_id}/room-observations")
    async def room_observations(robot_id: str):
        require_robot(robot_id)
        return runtime.room_observations(robot_id)

    @app.post("/api/debug/robots/{robot_id}/room-pcm")
    async def debug_room_pcm(robot_id: str, request: Request, source: str):
        require_robot(robot_id)
        if not source.strip():
            raise HTTPException(422, "source must not be blank")
        try:
            body = await request.body()
            result = runtime.debug_ingest_room_pcm(robot_id, source, body)
            logger.info(
                "phase2_pcm_debug accepted robot=%s source=%s frame_bytes=%s queue_depth=%s accepted=%s dropped=%s rejected=%s",
                robot_id,
                source,
                len(body),
                result["queue_depth"],
                result["accepted"],
                result["dropped"],
                result["rejected"],
            )
            return result
        except InputRejectedError as exc:
            logger.warning(
                "phase2_pcm_debug rejected robot=%s source=%s reason=%s",
                robot_id,
                source,
                exc.reason,
            )
            raise HTTPException(422, exc.reason) from exc

    async def operation(robot_id, name, text=None):
        require_robot(robot_id)

        try:
            if name == "turns":
                result = (await runtime.submit(robot_id, text)).to_dict()
                logger.info("phase1_debug action=turn robot=%s request_id=%s", robot_id, result.get("request_id"))
                return result
            if name == "fail":
                return (await runtime.submit(robot_id, "Fake failure probe", fail=True)).to_dict()
            if name == "cancel":
                return await runtime.cancel(robot_id)
            if name == "stop":
                return await runtime.stop_robot(robot_id)
            return await runtime.start_robot(robot_id)
        except RobotUnavailableError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RobotQueueFullError as exc:
            raise HTTPException(429, str(exc)) from exc

    @app.post("/api/robots/{robot_id}/turns")
    async def turn(robot_id: str, body: TurnInput):
        return await operation(robot_id, "turns", body.text)

    @app.get('/api/robots/{robot_id}/speech-expression')
    async def speech_expression(robot_id: str):
        require_robot(robot_id)
        return await runtime.speech_expression_state(robot_id)

    @app.put('/api/robots/{robot_id}/speech-expression/sidecar')
    async def acoustic_sidecar(robot_id: str, body: AcousticSidecarInput):
        require_robot(robot_id)
        try:
            return await runtime.set_acoustic_enabled(robot_id, body.enabled,
                                                      expected_identity=body.model_dump())
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.put('/api/robots/{robot_id}/language-preference')
    async def set_language_preference(robot_id: str, body: LanguagePreferenceInput):
        require_robot(robot_id)
        try:
            return await runtime.set_language_preference(robot_id,
                scope=body.scope, language=body.language, dialect=body.dialect,
                request_id=body.request_id, expected_identity=body.model_dump())
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete('/api/robots/{robot_id}/language-preference')
    async def clear_language_preference(robot_id: str, body: LanguagePreferenceClear):
        require_robot(robot_id)
        try:
            return await runtime.clear_language_preference(robot_id,
                scope=body.scope, request_id=body.request_id,
                expected_identity=body.model_dump())
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/speakers")
    async def list_speakers():
        return runtime.speaker_catalog()

    @app.post("/api/speakers")
    async def enroll_speaker(request: Request, name: str):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 2_000_000:
                raise HTTPException(413, "录音不能超过 2MB")
        try:
            return await runtime.enroll_speaker(name, bytes(content))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.delete("/api/speakers/{speaker_id}")
    async def remove_speaker(speaker_id: str):
        try:
            return await runtime.delete_speaker(speaker_id)
        except KeyError as exc:
            raise HTTPException(404, "没有这个声纹") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/voices")
    async def list_voices(robot_id: str):
        require_robot(robot_id)
        return runtime.voice_library(robot_id)

    @app.post("/api/robots/{robot_id}/voices/clone")
    async def clone_voice(robot_id: str, request: Request, name: str, filename: str,
                          language: str = "zh"):
        require_robot(robot_id)
        from annie_runtime.voice_profiles import MAX_BYTES
        size = request.headers.get("content-length")
        if size and size.isdecimal() and int(size) > MAX_BYTES:
            raise HTTPException(413, "音色参考音频不可超过10MB")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_BYTES:
                raise HTTPException(413, "音色参考音频不可超过10MB")
        try:
            return await runtime.clone_voice(robot_id, name, filename, bytes(content), language)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            logger.warning("voice_clone failed robot=%s error=%s", robot_id, type(exc).__name__)
            raise HTTPException(502, "云端音色复刻失败，请检查模型权限和服务器日志") from exc

    @app.put("/api/robots/{robot_id}/voices/selection")
    async def select_voice(robot_id: str, body: VoiceSelectionInput):
        require_robot(robot_id)
        try:
            return await runtime.select_voice(robot_id, body.voice_id, body.scope,
                                              expected_identity=body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.patch("/api/robots/{robot_id}/voices/{voice_id}")
    async def rename_voice(robot_id: str, voice_id: str, body: VoiceRenameInput):
        require_robot(robot_id)
        try:
            return await runtime.rename_voice(robot_id, voice_id, body.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/robots/{robot_id}/voices/{voice_id}")
    async def delete_voice(robot_id: str, voice_id: str):
        require_robot(robot_id)
        try:
            return await runtime.delete_voice(robot_id, voice_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/voices/{voice_id}/previews")
    async def voice_previews(robot_id: str, voice_id: str):
        require_robot(robot_id)
        if runtime.voices is None:
            raise HTTPException(503, "voice cloning unavailable")
        try:
            return {"previews": runtime.voices.preview_state(voice_id)}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/voices/{voice_id}/previews/{preset}")
    async def voice_preview_audio(robot_id: str, voice_id: str, preset: str):
        require_robot(robot_id)
        if runtime.voices is None:
            raise HTTPException(503, "voice cloning unavailable")
        try:
            path = runtime.voices.preview_path(voice_id, preset)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(404, "preview not generated")
        return FileResponse(path, media_type="audio/wav",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/robots/{robot_id}/voices/{voice_id}/previews/{preset}")
    async def generate_voice_preview(robot_id: str, voice_id: str, preset: str):
        require_robot(robot_id)
        if runtime.voices is None:
            raise HTTPException(503, "voice cloning unavailable")
        try:
            await runtime.voices.preview(voice_id, preset)
            return {"status": "ready"}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        except Exception as exc:
            logger.warning("voice_preview failed preset=%s error=%s",
                           preset, type(exc).__name__)
            raise HTTPException(502, "试听合成失败，请检查服务器日志") from exc

    @app.get("/api/robots/{robot_id}/realtime")
    async def realtime_state(robot_id: str):
        require_robot(robot_id)
        return runtime.realtime_state(robot_id)

    @app.get("/api/robots/{robot_id}/wakeword")
    async def wakeword_state(robot_id: str):
        """Cheap state-machine view for the selected workbench robot only."""
        require_robot(robot_id)
        client = runtime.realtime.get(robot_id)
        return {
            "robot_id": robot_id,
            "generation": runtime.robots[robot_id].generation,
            "ready": bool(client and client.ready),
            "wakeword": (client.wake.snapshot() if client and client.wake
                         else {"enabled": False}),
        }

    @app.get("/api/robots/{robot_id}/diagnostics")
    async def realtime_diagnostics(robot_id: str):
        require_robot(robot_id)
        return runtime.realtime_diagnostics(robot_id)

    @app.get("/api/robots/{robot_id}/inner-life")
    async def inner_life(robot_id: str):
        """Her mood, energy and today's real experiences (read only)."""
        require_robot(robot_id)
        service = getattr(runtime, "inner_life", None)
        motions = getattr(runtime, "motions", None)
        data = (service.snapshot(robot_id) if service is not None
                else {"robot_id": robot_id, "enabled": False})
        data["motions"] = motions.snapshot(robot_id) if motions is not None else None
        return data

    @app.get("/api/robots/{robot_id}/realtime/events")
    async def realtime_events(robot_id: str, after: int = 0):
        require_robot(robot_id)
        if after < 0:
            raise HTTPException(422, "after must be nonnegative")
        return runtime.realtime_events(robot_id, after)

    @app.get("/api/robots/{robot_id}/actions")
    async def action_history(robot_id: str):
        require_robot(robot_id)
        return await runtime.action_list(robot_id)

    @app.get("/api/robots/{robot_id}/external-events")
    async def external_events(robot_id: str):
        require_robot(robot_id)
        return runtime.external_events(robot_id)

    @app.get("/api/robots/{robot_id}/reminders")
    async def reminders(robot_id: str):
        require_robot(robot_id)
        return {"tasks": await runtime.reminder_list(robot_id)}

    @app.post("/api/robots/{robot_id}/reminders")
    async def reminder_create(robot_id: str, body: ReminderInput):
        require_robot(robot_id)
        try:
            return await runtime.reminder_create(
                robot_id, body.text, body.delay_ms, body.request_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/robots/{robot_id}/reminders/{task_id}/control")
    async def reminder_control(robot_id: str, task_id: str, body: ReminderAction):
        require_robot(robot_id)
        try:
            return await runtime.reminder_change(robot_id, task_id, body.action)
        except KeyError as exc:
            raise HTTPException(404, "unknown task_id") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/realtime/stream")
    async def realtime_stream(robot_id: str, after: int = 0):
        require_robot(robot_id)
        if after < 0:
            raise HTTPException(422, "after must be nonnegative")
        client = runtime.realtime.get(robot_id)
        if client is None:
            raise HTTPException(409, "Realtime not configured")

        async def events():
            queue = client.subscribe()  # subscribe before replay to avoid missed events
            last = after
            try:
                initial = client.since(after)
                yield f"data: {json.dumps(initial, ensure_ascii=False)}\n\n"
                last = initial["latest"]
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item["seq"] <= last:
                        continue
                    batch = {"events": [item], "gap": item["seq"] > last + 1,
                             "latest": item["seq"]}
                    last = item["seq"]
                    yield f"data: {json.dumps(batch, ensure_ascii=False)}\n\n"
            finally:
                client.unsubscribe(queue)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/robots/{robot_id}/realtime/text")
    async def realtime_text(robot_id: str, body: TurnInput):
        require_robot(robot_id)
        try:
            return await runtime.realtime_text(robot_id, body.text)
        except RobotUnavailableError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/session")
    async def session(robot_id: str):
        require_robot(robot_id)
        return runtime.session_state(robot_id)

    @app.get("/api/robots/{robot_id}/persona-instances")
    async def persona_instances(robot_id: str):
        require_robot(robot_id)
        return await runtime.list_persona_instances(robot_id)

    @app.post("/api/robots/{robot_id}/persona-instances")
    async def name_persona_instance(robot_id: str, body: PersonaInstanceInput):
        require_robot(robot_id)
        try:
            return await runtime.name_persona_instance(
                robot_id, body.persona_id, body.display_name,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/robots/{robot_id}/session")
    @app.post("/api/robots/{robot_id}/activate")
    async def switch_session(robot_id: str, body: SessionInput):
        require_robot(robot_id)
        try:
            return await runtime.switch_session(
                robot_id,
                chat_id=body.chat_id,
                persona_id=body.persona_id,
                persona_revision=body.persona_revision,
                style_id=body.style_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/conversations")
    async def conversations(robot_id: str):
        require_robot(robot_id)
        return await runtime.list_conversations(robot_id)

    @app.get("/api/robots/{robot_id}/memories")
    async def memory_list(robot_id: str, include_deleted: bool = False):
        require_robot(robot_id)
        # Persona never comes from a browser field or model-provided target.
        persona = runtime.session_state(robot_id)["persona_id"]
        return await runtime.memory_list(robot_id, persona, include_deleted)

    @app.get("/api/robots/{robot_id}/chat-memories")
    async def chat_memories(robot_id: str, include_deleted: bool = False):
        require_robot(robot_id)
        return await runtime.chat_memory_list(robot_id, include_deleted)

    @app.post("/api/robots/{robot_id}/chat-memories")
    async def chat_memory_create(robot_id: str, body: ChatMemoryInput):
        require_robot(robot_id)
        try:
            return await runtime.chat_memory_put(
                robot_id, body.key, body.text, body.expires_at_ms,
                chat_id=body.chat_id, persona_id=body.persona_id,
                generation=body.generation,
            )
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.patch("/api/robots/{robot_id}/chat-memories/{memory_id}")
    async def chat_memory_correct(robot_id: str, memory_id: str, body: ChatMemoryEdit):
        require_robot(robot_id)
        try:
            return await runtime.chat_memory_change(
                robot_id, memory_id, body.version, body.text,
                chat_id=body.chat_id, persona_id=body.persona_id,
                generation=body.generation,
            )
        except KeyError as exc:
            raise HTTPException(404, "chat memory not found") from exc
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/robots/{robot_id}/chat-memories/{memory_id}")
    async def chat_memory_delete(robot_id: str, memory_id: str, body: ChatMemoryEdit):
        require_robot(robot_id)
        try:
            return await runtime.chat_memory_change(
                robot_id, memory_id, body.version,
                chat_id=body.chat_id, persona_id=body.persona_id,
                generation=body.generation,
            )
        except KeyError as exc:
            raise HTTPException(404, "chat memory not found") from exc
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/robots/{robot_id}/memories")
    async def memory_create(robot_id: str, body: MemoryInput):
        require_robot(robot_id)
        persona = runtime.session_state(robot_id)["persona_id"]
        try:
            return await runtime.memory_put(robot_id, persona, body.kind, body.key, body.text)
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.patch("/api/robots/{robot_id}/memories/{memory_id}")
    async def memory_correct(robot_id: str, memory_id: str, body: MemoryCorrection):
        require_robot(robot_id)
        persona = runtime.session_state(robot_id)["persona_id"]
        try:
            return await runtime.memory_change(robot_id, persona, memory_id,
                                               body.version, body.text)
        except KeyError as exc:
            raise HTTPException(404, "memory not found") from exc
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/robots/{robot_id}/memories/{memory_id}")
    async def memory_forget(robot_id: str, memory_id: str, body: MemoryForget):
        require_robot(robot_id)
        persona = runtime.session_state(robot_id)["persona_id"]
        try:
            return await runtime.memory_change(robot_id, persona, memory_id, body.version)
        except KeyError as exc:
            raise HTTPException(404, "memory not found") from exc
        except (RobotUnavailableError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/conversations/{chat_id}")
    async def conversation(robot_id: str, chat_id: str, persona_id: str,
                           limit: int | None = None, before_id: int | None = None):
        require_robot(robot_id)
        if not chat_id.strip() or not persona_id.strip():
            raise HTTPException(422, "chat_id and persona_id must not be blank")
        if (limit is not None and not 1 <= limit <= 200) or (before_id is not None and before_id <= 0):
            raise HTTPException(422, "invalid conversation page cursor")
        if before_id is not None and limit is None:
            raise HTTPException(422, "limit is required with before_id")
        return await runtime.load_conversation(robot_id, chat_id, persona_id,
                                               limit=limit, before_id=before_id)

    @app.delete("/api/robots/{robot_id}/conversations/{chat_id}")
    async def delete_conversation(robot_id: str, chat_id: str, persona_id: str):
        require_robot(robot_id)
        if not chat_id.strip() or not persona_id.strip():
            raise HTTPException(422, "chat_id and persona_id must not be blank")
        return await runtime.delete_conversation(robot_id, chat_id, persona_id)

    @app.get("/api/robots/{robot_id}/context")
    async def context(robot_id: str):
        require_robot(robot_id)
        return await runtime.recent_context(robot_id)

    @app.get("/api/robots/{robot_id}/turns/{turn_id}")
    async def ledger_turn(robot_id: str, turn_id: str):
        require_robot(robot_id)
        try:
            return await runtime.get_turn(robot_id, turn_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown turn_id") from exc

    @app.post("/api/robots/{robot_id}/turns/{turn_id}/viewed")
    async def text_turn_viewed(robot_id: str, turn_id: str, body: TextViewedInput):
        require_robot(robot_id)
        identity = {"robot_id": robot_id, "chat_id": body.chat_id,
                    "persona_id": body.persona_id,
                    "session_epoch": body.session_epoch, "generation": body.generation}
        try:
            accepted = runtime.viewed_text_turn(robot_id, turn_id, identity)
        except RobotUnavailableError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"accepted": accepted, "evidence": "web_text_visible_only"}

    @app.post("/api/robots/{robot_id}/turns/{turn_id}/playback")
    async def playback(robot_id: str, turn_id: str, body: PlaybackInput):
        require_robot(robot_id)
        try:
            return await runtime.update_playback(
                robot_id,
                turn_id,
                published_text=body.published_text,
                spoken_text=body.spoken_text,
                evidence_status=body.evidence_status,
                playback_state=body.playback_state,
            )
        except KeyError as exc:
            raise HTTPException(404, "unknown turn_id") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/coordinator")
    async def coordinator(robot_id: str):
        require_robot(robot_id)
        return runtime.coordinator_state(robot_id)

    @app.post("/api/robots/{robot_id}/response-requests")
    async def response_request(robot_id: str, body: ResponseRequestInput):
        require_robot(robot_id)
        try:
            return runtime.request_response(
                robot_id,
                source=body.source,
                dedupe_key=body.dedupe_key,
                ttl_ms=body.ttl_ms,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/social-state")
    async def social_state(robot_id: str):
        require_robot(robot_id)
        return await runtime.social_state(robot_id)

    @app.put("/api/robots/{robot_id}/social-state")
    async def update_social_state(robot_id: str, body: SocialStateInput):
        require_robot(robot_id)
        try:
            return await runtime.update_social_state(
                robot_id,
                body.fields,
                source=body.source,
                generation=body.generation,
                source_turn_id=body.source_turn_id,
                ttl_ms=body.ttl_ms,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/robots/{robot_id}/{action}")
    async def control(robot_id: str, action: str):
        if action not in ("cancel", "stop", "start", "fail"):
            raise HTTPException(404, "unknown Fake control")
        return await operation(robot_id, action)

    app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")
    return app
