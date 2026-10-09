"""Short-lived read-only LiveKit grants for the intranet display.

The exhibition front end must never receive LiveKit API secrets. The source
identity and room are explicit configuration; do not guess a Room mapping.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlparse


def signed_jwt(key, secret, identity, grant, ttl=60):
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"iss": key, "sub": identity, "iat": now, "nbf": now,
              "exp": now + ttl, "video": grant}

    def encoded(obj):
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    signing = encoded(header) + b"." + encoded(claims)
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing, hashlib.sha256).digest()).rstrip(b"=")
    return (signing + b"." + sig).decode()


class ExhibitLiveKit:
    def __init__(self):
        self.server = os.environ.get("ANNIE_EXHIBIT_LIVEKIT_URL", "").strip()
        self.key = os.environ.get("ANNIE_EXHIBIT_LIVEKIT_API_KEY", "").strip()
        self.secret = os.environ.get("ANNIE_EXHIBIT_LIVEKIT_API_SECRET", "").strip()
        self.source = os.environ.get("ANNIE_EXHIBIT_LIVEKIT_SOURCE", "camera-a").strip()

    def room_for(self, robot_id):
        env_key = "ANNIE_EXHIBIT_LIVEKIT_ROOM_" + "".join(
            char if char.isalnum() else "_" for char in robot_id.upper())
        return os.environ.get(env_key, "").strip()

    def _url_is_allowed(self):
        parsed = urlparse(self.server)
        return parsed.scheme in {"ws", "wss"} and bool(parsed.hostname)

    def config(self, robot_id):
        room = self.room_for(robot_id)
        if not (self.server and self.key and self.secret and room and self.source and
                self._url_is_allowed()):
            return None
        identity = "annie-exhibit-" + secrets.token_urlsafe(16)
        token = signed_jwt(self.key, self.secret, identity,
                           {"roomJoin": True, "room": room, "canPublish": False,
                            "canSubscribe": True, "canPublishData": False}, ttl=45)
        return {"server": self.server, "token": token, "source": self.source,
                "room": room, "expires_in_sec": 45}
