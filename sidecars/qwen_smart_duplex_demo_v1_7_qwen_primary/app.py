from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from session import VoiceSession

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Qwen Smart Duplex Demo")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def disable_demo_asset_cache(request, call_next):
    response = await call_next(request)
    # Demo 频繁替换 AudioWorklet；浏览器缓存旧 Worklet 会造成“代码已更新但
    # 听感没变化”。开发测试阶段统一禁用缓存。
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "api_key_configured": bool(settings.api_key and not settings.api_key.startswith("sk-xxxx")),
            "workspace_mode": bool(settings.workspace_id),
            "models": {
                "audio": settings.audio_model,
                "llm": settings.llm_model,
                "tts": settings.tts_model,
            },
        }
    )


@app.websocket("/ws/voice")
async def voice_socket(ws: WebSocket) -> None:
    await ws.accept()
    session = VoiceSession(ws, settings)
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("voice session failed")
        try:
            await ws.send_json({"type": "server.error", "message": str(exc)})
        except Exception:
            pass
        try:
            await session.shutdown()
        except Exception:
            pass
