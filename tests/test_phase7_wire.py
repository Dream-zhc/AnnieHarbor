"""Verified reb_main wire identity; synthetic transport is never device success."""

import asyncio
import json

import pytest
from annie_adapters.tts import RoomTtsPublisher

from tests.test_phase7_runtime import room_server


class Transport:
    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True


class FakeRoom:
    def __init__(self, correct=True, channel="main"):
        self.correct = correct
        self.channel = channel
        self.sent = []
        self.transport = Transport()

    async def recv(self):
        return json.dumps({
            "type": "WELCOME", "ok": True, "clientId": "conn-test",
            "robotId": f"reb_{self.channel}" if self.correct else "reb_mic",
            "room": "roomv2", "size": 1, "subscriptions": [], "catalog": [],
        })

    async def send(self, message):
        self.sent.append(message)


def test_main_room_publisher_validates_welcome_before_action_send():
    async def run():
        seen = []
        socket = FakeRoom()

        async def connect(url, **kwargs):
            seen.append(url)
            return socket

        publisher = RoomTtsPublisher(room_server(), "roomv2",
                                     socket_factory=connect, channel="main")
        await publisher.open()
        packet = {"type": "action_trigger", "data": {"script_id": "look_left"}}
        await publisher.publish(packet)
        assert seen == ["ws://127.0.0.1:1/robot/roomv2/reb_main"]
        assert json.loads(socket.sent[0]) == packet
        await publisher.close()
        assert socket.transport.aborted

        wrong = FakeRoom(correct=False)

        async def bad_connect(url, **kwargs):
            return wrong

        blocked = RoomTtsPublisher(room_server(), "roomv2",
                                   socket_factory=bad_connect, channel="main")
        with pytest.raises(ValueError, match="WELCOME identity"):
            await blocked.publish(packet)
        assert wrong.sent == []
        assert wrong.transport.aborted

    asyncio.run(run())


def test_expression_condition_checked_after_acquiring_pcm_socket_lock():
    async def run():
        socket = FakeRoom(channel="tts")

        async def connect(_url, **_kwargs):
            return socket

        publisher = RoomTtsPublisher(room_server(), "roomv2",
                                     socket_factory=connect, channel="tts")
        await publisher.open()
        speech = {"first_pcm_in_flight": False}
        packet = {"type": "expression", "data": {"expression": "喜"}}
        # PCM may acquire the websocket lock first and set its inflight
        # marker before an expression can send. Verify the conditional send
        # checks the marker *under* that exact lock, not only before await.
        async with publisher.lock:
            late = asyncio.create_task(publisher.publish_if(
                packet, lambda: not speech["first_pcm_in_flight"],
            ))
            await asyncio.sleep(0)
            speech["first_pcm_in_flight"] = True
        assert await late is False
        assert socket.sent == []
        speech["first_pcm_in_flight"] = False
        assert await publisher.publish_if(
            packet, lambda: not speech["first_pcm_in_flight"],
        ) is True
        assert len(socket.sent) == 1
        await publisher.close()

    asyncio.run(run())
