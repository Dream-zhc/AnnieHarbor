"""Local exhibition sessions and explicit, expiring visitor display consent.

Exhibition credentials are operator-provisioned through the runtime environment.
Sessions and consent deliberately expire on server restart (fail closed).
"""

import hmac
import os
import secrets
import time

COOKIE = "annie_exhibit_session"
LOCAL_CLIENTS = {"127.0.0.1", "::1", "testclient"}


class ExhibitAccess:
    def __init__(self):
        self.operator_key = os.environ.get("ANNIE_EXHIBIT_OPERATOR_TOKEN", "")
        self.viewer_key = os.environ.get("ANNIE_EXHIBIT_VIEWER_TOKEN", "")
        self.sessions = {}
        self.consents = {}
        self.consent_baselines = {}
        self.consent_started_ms = {}

    @property
    def configured(self):
        return bool(self.operator_key and self.viewer_key and
                    self.operator_key != self.viewer_key and
                    len(self.operator_key) >= 24 and len(self.viewer_key) >= 24)

    def login(self, role, credential):
        if not self.configured or role not in {"operator", "viewer"}:
            return None
        expected = self.operator_key if role == "operator" else self.viewer_key
        if not hmac.compare_digest(credential, expected):
            return None
        now = time.monotonic()
        self.sessions = {key: value for key, value in self.sessions.items()
                         if value[1] > now}
        if len(self.sessions) >= 64:
            return None
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = (role, now + 8 * 3600)
        return session_id

    def role(self, session):
        if not session:
            return None
        record = self.sessions.get(session)
        if not record:
            return None
        if record[1] <= time.monotonic():
            self.sessions.pop(session, None)
            return None
        return record[0]

    def logout(self, session):
        self.sessions.pop(session, None)

    def set_consent(self, robot_id, duration_sec, baseline=None):
        self.consents[robot_id] = time.monotonic() + duration_sec
        self.consent_baselines[robot_id] = baseline
        self.consent_started_ms[robot_id] = time.time_ns() // 1_000_000

    def revoke(self, robot_id):
        self.consents.pop(robot_id, None)
        self.consent_baselines.pop(robot_id, None)
        self.consent_started_ms.pop(robot_id, None)

    def is_consented(self, robot_id):
        expiry = self.consents.get(robot_id, 0)
        if expiry <= time.monotonic():
            self.revoke(robot_id)
            return False
        return True


def local_request(client):
    return bool(client and client.host in LOCAL_CLIENTS)
