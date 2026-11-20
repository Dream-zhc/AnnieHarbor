"""Raw audio per robot and Chat (user 11-20: keep what is uploaded, so there is original sound)."""

import asyncio
import wave
from datetime import date, datetime

from annie_runtime.audio_archive import AudioArchive

from tests.test_companion_speech import speech


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


IDENTITY = {"chat_id": "chat-abcdef123", "persona_id": "annie", "session_epoch": 1}


def frames(path):
    with wave.open(str(path)) as f:
        return f.getframerate(), f.getnframes()


def test_heard_and_said_line_up_and_a_new_chat_starts_new_files(tmp_path):
    clock = Clock()
    archive = AudioArchive(tmp_path, "annie-a", clock=clock,
                           now=lambda: datetime(2026, 11, 20, 14, 21, 35))
    for _ in range(50):  # 1 s she heard
        archive.heard(bytes(640), IDENTITY)
        clock.t += .02
    archive.said(bytes(48000), IDENTITY)  # 1 s she said, starting 1 s in
    archive.close()
    folder = tmp_path / "20261120" / "annie-a"
    assert frames(folder / "142135-chat-abc-heard.wav") == (16000, 16000)
    rate, said = frames(folder / "142135-chat-abc-said.wav")
    assert rate == 24000 and said == 24000 + 24000  # one second of silence, then her voice
    assert '"chat_id": "chat-abcdef123"' in (folder / "142135-chat-abc.json").read_text()

    archive = AudioArchive(tmp_path, "annie-a", clock=clock,
                           now=lambda: datetime(2026, 11, 20, 15, 0, 0))
    archive.heard(bytes(640), IDENTITY)
    archive.heard(bytes(640), {**IDENTITY, "chat_id": "chat-zz"})
    clock.t += 3600
    archive.heard(bytes(640), {**IDENTITY, "chat_id": "chat-zz"})  # long quiet: new files
    archive.close()
    assert len(list(folder.glob("150000-*-heard.wav"))) == 2


def test_a_failing_disk_never_reaches_the_conversation(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    archive = AudioArchive(blocker, "annie-a")  # a file where the folder should be
    archive.heard(bytes(640), IDENTITY)
    archive.said(bytes(960), IDENTITY)
    assert archive.failed and archive.files == {}


def test_only_old_day_folders_are_deleted(tmp_path):
    for name in ("20261110", "20261114", "20261120", "notes"):
        (tmp_path / name).mkdir()
    removed = AudioArchive.prune(tmp_path, 7, today=date(2026, 11, 20))
    assert sorted(removed) == ["20261110"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["20261114", "20261120", "notes"]


def test_what_she_says_is_archived_as_it_goes_to_the_room():
    class Archive:
        def __init__(self):
            self.said_bytes = 0

        def said(self, pcm, identity):
            self.said_bytes += len(pcm)

    async def run():
        output = speech(first_chars=2, min_chars=4, max_chars=30, first_wait_ms=5)
        output.robot.audio_archive = archive = Archive()
        try:
            identity = output.robot.session_state()
            await output.feed("turn", identity, "你好呀。")
            await output.finish("turn")
            await asyncio.wait_for(output.worker, 2)
            assert archive.said_bytes > 0 and archive.said_bytes == output.samples * 2
        finally:
            await output.close()
    asyncio.run(run())
