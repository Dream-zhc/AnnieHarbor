"""The robot camera's face library over its HTTP API (standalone vision service).

Wire format from the vision team (2026-10-20):

- POST /api/face/register {"name", "samples"} -> 202, body carries face_id
- GET  /api/face/register/{face_id} -> state queued/started/progress/completed/
  failed/cancelled, saved, target_samples, phase, error
- POST /api/face/delete {"name"} ; GET /api/face/people -> people[] with
  face_id, name, samples, registration_state
- errors: {"ok": false, "error": code}; 400 invalid_name/invalid_samples,
  404 registration_not_found, 409 registration_in_progress, 503 command_queue_full

Every call returns a dict and never raises for HTTP or network trouble:
{"ok": False, "error": "unreachable"} when the service cannot be reached.
"""

import re

import httpx

TERMINAL_STATES = ("completed", "failed", "cancelled")


def _code(value):
    text = str(value or "")
    return text if re.fullmatch(r"[a-z0-9_]{1,48}", text) else "unknown_error"


class FaceRegistry:
    def __init__(self, base_url: str, *, client=None, timeout_ms=3000):
        if not re.fullmatch(r"https?://[^\s/]+(?::\d+)?/?", base_url or ""):
            raise ValueError("face registry base_url must be http(s)://host[:port]")
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(
            timeout=timeout_ms / 1000,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )
        self.owns_client = client is None

    async def _call(self, method, path, body=None):
        try:
            response = await self.client.request(method, self.base_url + path, json=body)
        except httpx.HTTPError:
            return {"ok": False, "error": "unreachable"}
        try:
            content = response.json()
        except ValueError:
            content = None
        if not isinstance(content, dict):
            return {"ok": False, "error": "invalid_response", "http_status": response.status_code}
        if response.status_code >= 400 or content.get("ok") is False:
            return {"ok": False, "error": _code(content.get("error")),
                    "http_status": response.status_code}
        return {**content, "ok": True}

    async def people(self):
        result = await self._call("GET", "/api/face/people")
        if result["ok"]:
            people = result.get("people")
            result["people"] = [
                {"face_id": str(row.get("face_id") or "")[:64],
                 "name": str(row.get("name") or "")[:40],
                 "samples": row.get("samples") if type(row.get("samples")) is int else None,
                 "registration_state": str(row.get("registration_state") or "")[:24]}
                for row in people if isinstance(row, dict)
            ] if isinstance(people, list) else []
        return result

    async def register(self, name, samples):
        result = await self._call("POST", "/api/face/register", {"name": name, "samples": samples})
        if result["ok"] and not isinstance(result.get("face_id"), str):
            return {"ok": False, "error": "missing_face_id"}
        return result

    async def registration(self, face_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", face_id or ""):
            return {"ok": False, "error": "invalid_face_id"}
        return await self._call("GET", "/api/face/register/" + face_id)

    async def delete(self, name):
        return await self._call("POST", "/api/face/delete", {"name": name})

    async def close(self):
        if self.owns_client:
            await self.client.aclose()
