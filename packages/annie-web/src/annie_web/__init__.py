"""Local Dashboard plus Phase 3 session, ledger, and PCM diagnostics."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from annie_core import InputRejectedError, RobotQueueFullError, RobotUnavailableError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=128)
    persona_id: str = Field(min_length=1, max_length=128)
    persona_revision: int = Field(default=1, ge=1)

    @field_validator("chat_id", "persona_id")
    @classmethod
    def strip_identity(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("session identity must not be blank")
        return value


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


class SocialStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict
    source: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    source_turn_id: str | None = Field(default=None, max_length=128)
    ttl_ms: int | None = Field(default=None, ge=1, le=86_400_000)


def create_app(runtime, dashboard_dir: Path, phase_test_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="Annie Harbor — Phase 3 Session Control", lifespan=lifespan)

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

    @app.get("/api/robots/{robot_id}/realtime")
    async def realtime_state(robot_id: str):
        require_robot(robot_id)
        return runtime.realtime_state(robot_id)

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

    @app.post("/api/robots/{robot_id}/session")
    async def switch_session(robot_id: str, body: SessionInput):
        require_robot(robot_id)
        try:
            return await runtime.switch_session(
                robot_id,
                chat_id=body.chat_id,
                persona_id=body.persona_id,
                persona_revision=body.persona_revision,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/robots/{robot_id}/conversations")
    async def conversations(robot_id: str):
        require_robot(robot_id)
        return await runtime.list_conversations(robot_id)

    @app.get("/api/robots/{robot_id}/conversations/{chat_id}")
    async def conversation(robot_id: str, chat_id: str, persona_id: str):
        require_robot(robot_id)
        if not chat_id.strip() or not persona_id.strip():
            raise HTTPException(422, "chat_id and persona_id must not be blank")
        return await runtime.load_conversation(robot_id, chat_id, persona_id)

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

    if phase_test_dir is not None and phase_test_dir.is_dir():
        app.mount("/phase-tests", StaticFiles(directory=str(phase_test_dir), html=True), name="phase-tests")
    app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")
    return app
