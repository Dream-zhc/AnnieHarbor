"""Local Dashboard, Fake text controls, and explicit Phase 2 PCM debug ingress."""

from contextlib import asynccontextmanager
from pathlib import Path

from annie_core import InputRejectedError, RobotQueueFullError, RobotUnavailableError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator


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


def create_app(runtime, dashboard_dir: Path, phase_test_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="Annie Harbor — Phase 2 PCM Gateway", lifespan=lifespan)

    @app.get("/health")
    async def health():
        data = runtime.health()
        return JSONResponse(data, status_code=200 if data["ready"] else 503)

    @app.get("/api/robots")
    async def robots():
        return runtime.health()["robots"]

    @app.get("/api/robots/{robot_id}/inputs")
    async def inputs(robot_id: str):
        if robot_id not in runtime.robots:
            raise HTTPException(404, "unknown robot_id")
        return runtime.input_stats(robot_id)

    @app.post("/api/debug/robots/{robot_id}/room-pcm")
    async def debug_room_pcm(robot_id: str, request: Request, source: str):
        if robot_id not in runtime.robots:
            raise HTTPException(404, "unknown robot_id")
        if not source.strip():
            raise HTTPException(422, "source must not be blank")
        try:
            return runtime.debug_ingest_room_pcm(robot_id, source, await request.body())
        except InputRejectedError as exc:
            raise HTTPException(422, exc.reason) from exc

    async def operation(robot_id, name, text=None):
        if robot_id not in runtime.robots:
            raise HTTPException(404, "unknown robot_id")

        try:
            if name == "turns":
                return (await runtime.submit(robot_id, text)).to_dict()
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

    @app.post("/api/robots/{robot_id}/{action}")
    async def control(robot_id: str, action: str):
        if action not in ("cancel", "stop", "start", "fail"):
            raise HTTPException(404, "unknown Fake control")
        return await operation(robot_id, action)

    if phase_test_dir is not None and phase_test_dir.is_dir():
        app.mount("/phase-tests", StaticFiles(directory=str(phase_test_dir), html=True), name="phase-tests")
    app.mount("/", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")
    return app
