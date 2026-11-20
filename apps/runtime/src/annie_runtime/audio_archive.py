"""What she heard and what she said, per robot and Chat, as WAV under var/recordings (11-20).

heard: exactly the 16 kHz PCM sent to the realtime model (after the input gate and AEC);
said: the 24 kHz PCM published to the Room for her speaker. Both files start at the same
moment and gaps are filled with silence, so they line up to a fraction of a second; exact
timings stay in the runtime log. Day folders older than keep_days are deleted at start.
"""

import json
import logging
import shutil
import time
import wave
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)
TRACKS = {"heard": 16000, "said": 24000}
PAD_SECONDS = 0.2  # a shorter hole is jitter, not silence
NEW_FILES_AFTER_SECONDS = 600  # a long quiet starts new files instead of minutes of zeros


class AudioArchive:
    def __init__(self, root, robot_id, *, clock=time.monotonic, now=datetime.now):
        self.root, self.robot_id, self.clock, self.now = Path(root), robot_id, clock, now
        self.key = self.started = self.last_write = None
        self.files = {}
        self.written = dict.fromkeys(TRACKS, 0)
        self.failed = False

    def heard(self, pcm, identity):
        self._write("heard", pcm, identity)

    def said(self, pcm, identity):
        self._write("said", pcm, identity)

    def _write(self, track, pcm, identity):
        if not pcm or self.failed:
            return
        key = (identity.get("chat_id"), identity.get("session_epoch"))
        now = self.clock()
        if self.files and (key != self.key or now - self.last_write > NEW_FILES_AFTER_SECONDS):
            self.close()
        try:
            if not self.files:
                self._open(identity, key, now)
            rate = TRACKS[track]
            behind = round((now - self.started) * rate) - self.written[track]
            if behind > rate * PAD_SECONDS:
                self.files[track].writeframesraw(bytes(2 * behind))
                self.written[track] += behind
            self.files[track].writeframesraw(pcm)
            self.written[track] += len(pcm) // 2
            self.last_write = now
        except OSError as exc:
            self.failed = True  # a full disk must never touch the conversation
            logger.warning("audio_archive_failed robot=%s error=%s", self.robot_id, exc)

    def _open(self, identity, key, now):
        stamp = self.now()
        folder = self.root / f"{stamp:%Y%m%d}" / self.robot_id
        folder.mkdir(parents=True, exist_ok=True)
        base = folder / f"{stamp:%H%M%S}-{str(identity.get('chat_id') or 'chat')[:8]}"
        for track, rate in TRACKS.items():
            out = wave.open(f"{base}-{track}.wav", "wb")
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            self.files[track] = out
        Path(f"{base}.json").write_text(json.dumps(
            {"robot_id": self.robot_id, "chat_id": identity.get("chat_id"),
             "persona_id": identity.get("persona_id"),
             "session_epoch": identity.get("session_epoch"),
             "started": stamp.isoformat(timespec="seconds")}, ensure_ascii=False))
        self.key, self.started, self.last_write = key, now, now
        self.written = dict.fromkeys(TRACKS, 0)
        logger.info("audio_archive_started robot=%s chat=%s files=%s", self.robot_id,
                    identity.get("chat_id"), base)

    def close(self):
        for out in self.files.values():
            try:
                out.close()  # writes the final WAV header
            except OSError as exc:
                logger.warning("audio_archive_close_failed robot=%s error=%s", self.robot_id, exc)
        self.files = {}
        self.key = None

    @staticmethod
    def prune(root, keep_days, *, today=None):
        """Delete day folders (YYYYMMDD) older than keep_days; nothing else."""
        root = Path(root)
        if not root.is_dir():
            return []
        today = today or datetime.now().date()
        removed = []
        for folder in root.iterdir():
            try:
                day = datetime.strptime(folder.name, "%Y%m%d").date()
            except ValueError:
                continue
            if folder.is_dir() and (today - day).days >= keep_days:
                shutil.rmtree(folder, ignore_errors=True)
                removed.append(folder.name)
        return removed
