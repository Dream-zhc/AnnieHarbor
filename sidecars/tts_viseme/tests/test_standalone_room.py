"""Standalone transport checks; no model load or real room required."""

import json
import queue
import threading
import time
import unittest

from qwen_stream_room_g2pw import ReconnectingRoom


class FakeSocket:
    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        pass


class StandaloneRoomTests(unittest.TestCase):
    def test_heartbeat_and_mouth_share_room_sender(self):
        out = queue.Queue()
        sock = FakeSocket()
        room = ReconnectingRoom(
            "ws://unused", "reb_sam", lambda _: None,
            outgoing=out,
            heartbeat=lambda: {"type": "viseme_health", "status": "ready"},
        )
        room._ws = sock
        room.connected.set()
        room.subscribed.set()
        sender = threading.Thread(target=room._send_loop)
        sender.start()
        try:
            out.put(json.dumps({"type": "mouth_script", "seq": 1}))
            deadline = time.monotonic() + 2
            while len(sock.sent) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual([msg["type"] for msg in sock.sent[:2]], ["viseme_health", "mouth_script"])
            self.assertEqual(room.sent_count, 1)
        finally:
            room.stop()
            sender.join(timeout=2)
            self.assertFalse(sender.is_alive())

    def test_disconnected_mouth_is_not_replayed(self):
        out = queue.Queue()
        room = ReconnectingRoom("ws://unused", "reb_sam", lambda _: None, outgoing=out)
        out.put(json.dumps({"type": "mouth_script", "seq": 1}))
        sender = threading.Thread(target=room._send_loop)
        sender.start()
        try:
            deadline = time.monotonic() + 2
            while room.dropped_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(room.dropped_count, 1)
            self.assertTrue(out.empty())
        finally:
            room.stop()
            sender.join(timeout=2)
            self.assertFalse(sender.is_alive())


if __name__ == "__main__":
    unittest.main()
