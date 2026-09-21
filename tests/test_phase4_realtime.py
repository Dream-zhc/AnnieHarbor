"""Wire and isolation regression for Phase 4 without charging the provider."""

import asyncio
import base64
import inspect
import tempfile
import unittest
from pathlib import Path

from annie_adapters.realtime import MODEL, QwenRealtimeSocket, audio_append, realtime_url, session_update
from annie_core import RobotConfig, RobotUnavailableError
from annie_runtime import HarborRuntime
from annie_runtime.realtime import RealtimeRobot

ROOT = Path(__file__).resolve().parents[1]


class FakeSocket:
    instances = []

    def __init__(self, url, key):
        self.url = url
        self.outbound = []
        self.inbound = asyncio.Queue()
        self.closed = False
        self.__class__.instances.append(self)
        self.id = len(self.__class__.instances)

    async def open(self):
        return self

    async def send(self, event):
        self.outbound.append(event)
        if event.get("type") == "session.update":
            await self.inbound.put({
                "type": "session.updated",
                "session": {"id": f"session-{self.id}", **event["session"]},
            })

    async def recv(self):
        return await self.inbound.get()

    async def close(self):
        self.closed = True


class FakeLlmDelta:
    def __init__(self, type_, text="", tool_call=None):
        self.type = type_
        self.text = text
        self.tool_call = tool_call


class FakeExternalLlm:
    model = "fake-qwen3.7-flash"

    def __init__(self, chunks=None):
        self.chunks = chunks or [FakeLlmDelta("text", "选绿茶。")]
        self.messages = []
        self.closed = False

    async def stream(self, messages, *, tools=True):
        self.messages.append((messages, tools))
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk

    async def close(self):
        self.closed = True


class RealtimeWireTests(unittest.TestCase):
    def test_audio_pump_has_no_idle_sleep_or_nowait_polling(self):
        source = inspect.getsource(RealtimeRobot._audio_pump)
        self.assertIn("await audio.next_frame()", source)
        self.assertNotIn("next_nowait(", source)
        self.assertNotIn("asyncio.sleep(", source)

    def test_text_only_smart_turn_and_tool_are_one_session_update(self):
        payload = session_update("小安妮", 20)
        config = payload["session"]
        self.assertEqual(config["modalities"], ["text"])
        self.assertEqual(config["turn_detection"], {"type": "smart_turn"})
        self.assertEqual(config["max_history_turns"], 20)
        self.assertEqual(config["tools"][0]["function"]["name"], "preview_action")
        self.assertNotIn("enable_search", config)
        frame = bytes(range(256)) * 2 + bytes(128)
        self.assertEqual(base64.b64decode(audio_append(frame)["audio"]), frame)
        with self.assertRaises(ValueError):
            audio_append(b"bad")

    def test_sensor_only_session_uses_demo_modalities_without_realtime_tools(self):
        payload = session_update("sensor", 6, sensor_only=True)
        config = payload["session"]
        self.assertEqual(config["modalities"], ["text", "audio"])
        self.assertEqual(config["turn_detection"], {"type": "smart_turn"})
        self.assertEqual(config["max_history_turns"], 6)
        self.assertNotIn("tools", config)

    def test_url_does_not_change_model_or_accept_insecure_transport(self):
        self.assertIn("model=" + MODEL, realtime_url("wss://dashscope.aliyuncs.com/api-ws/v1/realtime"))
        with self.assertRaises(ValueError):
            realtime_url("ws://dashscope.aliyuncs.com/api-ws/v1/realtime")
        with self.assertRaises(ValueError):
            realtime_url("wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=other")


class RealtimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_speculative_hold_is_reversible_and_keeps_robot_scope(self):
        client = self.runtime.realtime["annie-a"]
        other = self.runtime.realtime["annie-b"]

        class Speaking:
            turn_id = "voice-turn"

            async def close(self):
                pass

        client.external_llm = FakeExternalLlm()
        client.speech = Speaking()
        generation = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "candidate"}, generation)
        hold = client.events[-1]
        self.assertEqual(hold["kind"], "playback.hold_requested")
        self.assertEqual(hold["turn_id"], "voice-turn")
        self.assertEqual(hold["robot_id"], "annie-a")
        await client.handle({"type": "input_audio_buffer.speech_stopped",
                             "item_id": "candidate", "reason": "turn_invalid"}, generation)
        resume = client.events[-1]
        self.assertEqual(resume["kind"], "playback.resume_requested")
        self.assertEqual(resume["turn_id"], hold["turn_id"])
        self.assertFalse(any(event["kind"].startswith("playback.") for event in other.events))

    async def test_push_subscriber_is_bounded_and_released(self):
        # No 250ms HTTP poll; queued events remain scoped to their Realtime robot.
        client = self.runtime.realtime["annie-a"]
        other = self.runtime.realtime["annie-b"]
        queue = client.subscribe()
        self.assertEqual(queue.qsize(), 0)
        try:
            for i in range(140):
                client.publish("probe", index=i)
            self.assertEqual(queue.qsize(), 128)
            self.assertEqual(queue.get_nowait()["index"], 12)
            self.assertFalse(other.event_subscribers)
        finally:
            client.unsubscribe(queue)
        self.assertFalse(client.event_subscribers)

    async def asyncSetUp(self):
        FakeSocket.instances.clear()
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "var", prefix="phase4-realtime-")
        self.addCleanup(self.tmp.cleanup)
        self.runtime = HarborRuntime(
            [RobotConfig("annie-a", room_id="roomv2", fake_delay_ms=0, listen_enabled=True),
             RobotConfig("annie-b", room_id="roomv3", fake_delay_ms=0, listen_enabled=True)],
            Path(self.tmp.name) / "runtime.db",
            realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket,
        )
        await self.runtime.start()
        self.addAsyncCleanup(self.runtime.close)
        for _ in range(100):
            if all(client.ready for client in self.runtime.realtime.values()):
                break
            await asyncio.sleep(0.01)
        self.assertTrue(all(client.ready for client in self.runtime.realtime.values()))

    async def test_per_robot_text_delta_ledger_and_generation(self):
        client = self.runtime.realtime["annie-a"]
        other = self.runtime.realtime["annie-b"]
        self.assertNotEqual(client.session, other.session)
        submitted = await self.runtime.realtime_text("annie-a", "你好")
        self.assertEqual(submitted["status"], "accepted")
        self.assertFalse(any(e["kind"] == "turn.started" for e in other.events))
        sock = client.socket
        self.assertEqual(sock.outbound[-2]["item"]["role"], "user")
        self.assertEqual(sock.outbound[-1]["type"], "response.create")
        await client.handle({"type": "response.created", "response": {"id": "resp-a"}}, client.robot.generation)
        await client.handle({"type": "response.text.delta", "delta": "你"}, client.robot.generation)
        await client.handle({"type": "response.text.delta", "delta": "好"}, client.robot.generation)
        self.assertEqual(self.runtime.realtime_events("annie-a", 0)["events"][-1]["text"], "你好")
        await client.handle({"type": "response.done", "response": {"status": "completed"}}, client.robot.generation)
        await self.runtime.ledger.flush()
        stored = await self.runtime.load_conversation("annie-a", "default", "annie")
        self.assertEqual(stored["turns"][0]["assistant_generated_text"], "你好")
        self.assertEqual(self.runtime.realtime_events("annie-b", 0)["events"][-1]["kind"], "session.ready")
        previous_socket = client.socket
        old_generation = client.robot.generation
        await self.runtime.switch_session("annie-a", chat_id="new", persona_id="annie")
        self.assertTrue(previous_socket.closed)
        await client.handle({"type": "response.text.delta", "delta": "stale"}, old_generation)
        self.assertFalse(any(e.get("delta") == "stale" for e in client.events))

    async def test_invalid_smart_turn_does_not_create_response(self):
        client = self.runtime.realtime["annie-a"]
        before = len(client.socket.outbound)
        generation = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started"}, generation)
        with self.assertRaises(RobotUnavailableError):
            await self.runtime.realtime_text("annie-a", "碰撞请求")
        await client.handle({"type": "input_audio_buffer.speech_stopped", "reason": "turn_invalid"}, generation)
        await client.handle({"type": "conversation.item.ambient_audio_transcription.completed",
                             "transcript": "嗯"}, generation)
        self.assertEqual(client.snapshot()["speech_started_count"], 1)
        self.assertEqual(client.snapshot()["invalid_turns"], 1)
        self.assertEqual(client.snapshot()["transcripts_completed"], 0)
        self.assertEqual(len(client.socket.outbound), before)
        self.assertIsNone(client.turn_id)

    async def test_final_asr_backfills_turn_started_before_transcription(self):
        client = self.runtime.realtime["annie-a"]
        generation = client.robot.generation
        await client.handle({"type": "response.text.delta", "delta": "好的"}, generation)
        self.assertEqual(client.robot.turn_cache[client.turn_id]["user_text"], "")
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "transcript": "我想聊天"}, generation)
        self.assertEqual(client.snapshot()["transcripts_completed"], 1)
        self.assertIsNotNone(client.snapshot()["last_provider_transcript_ms"])
        await client.handle({"type": "response.done", "response": {"status": "completed"}}, generation)
        stored = await self.runtime.load_conversation("annie-a", "default", "annie")
        self.assertEqual(stored["turns"][0]["user_text"], "我想聊天")
        self.assertEqual(stored["turns"][0]["assistant_generated_text"], "好的")

    async def test_final_transcript_corrects_partial_text_after_response(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        await client.handle({"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好"}, gen)
        turn_id = client.turn_id
        await client.handle({"type": "response.text.delta", "delta": "你好！"}, gen)
        await client.handle({"type": "response.done", "response": {"status": "completed"}}, gen)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好，你好。"}, gen)
        await self.runtime.ledger.flush()
        turns = (await self.runtime.load_conversation("annie-a", "default", "annie"))["turns"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["turn_id"], turn_id)
        self.assertEqual(turns[0]["user_text"], "你好，你好。")
        self.assertEqual(client.robot.recent_context()[0]["user_text"], turns[0]["user_text"])
        self.assertEqual(client.events[-1]["turn_id"], turn_id)

    async def test_audio_transcription_does_not_change_manual_text(self):
        client = self.runtime.realtime["annie-a"]
        await self.runtime.realtime_text("annie-a", "manual text")
        await client.handle({"type": "conversation.item.input_audio_transcription.completed", "transcript": "audio"}, client.robot.generation)
        turns = (await self.runtime.load_conversation("annie-a", "default", "annie"))["turns"]
        self.assertEqual(turns[0]["user_text"], "manual text")

    async def test_deleted_active_turn_cannot_resurrect_old_context(self):
        client = self.runtime.realtime["annie-a"]
        await self.runtime.realtime_text("annie-a", "旧会话的句子")
        generation = client.robot.generation
        await client.handle({"type": "response.text.delta", "delta": "迟到文本"}, generation)
        result = await self.runtime.delete_conversation("annie-a", "default", "annie")
        self.assertEqual(result["deleted_turns"], 1)
        self.assertEqual((await self.runtime.load_conversation("annie-a", "default", "annie"))["turns"], [])
        self.assertEqual(client.robot.recent_context(), [])
        await client.handle({"type": "response.text.delta", "delta": "不应出现"}, generation)
        self.assertFalse(any(e.get("delta") == "不应出现" for e in client.events))

    async def test_shutdown_aborts_stuck_proxy_without_waiting_for_close(self):
        class NeverClosingSocket:
            def __init__(self):
                self.transport = self
                self.aborted = False

            def abort(self):
                self.aborted = True

            async def close(self):
                raise AssertionError("must not block shutdown on proxy close handshake")

        client = QwenRealtimeSocket("wss://dashscope.aliyuncs.com/api-ws/v1/realtime", "test")
        fake = NeverClosingSocket()
        client.socket = fake
        await asyncio.wait_for(client.close(), 0.2)
        self.assertTrue(fake.aborted)
        self.assertIsNone(client.socket)

    async def test_tool_args_validated_once_and_fake_only(self):
        client = self.runtime.realtime["annie-a"]
        await self.runtime.realtime_text("annie-a", "请举手")
        generation = client.robot.generation
        event = {"type": "response.function_call_arguments.done", "call_id": "c1",
                 "name": "preview_action", "arguments": '{"description":"举手"}'}
        await client.handle(event, generation)
        await client.handle(event, generation)
        result = [msg for msg in client.socket.outbound if msg["type"] == "conversation.item.create"
                  and msg["item"]["type"] == "function_call_output"]
        self.assertEqual(len(result), 1)
        self.assertIn('"real_device_executed": false', result[0]["item"]["output"])
        await client.handle({"type": "response.done", "response": {"status": "completed"}}, generation)
        self.assertEqual(client.socket.outbound[-1]["type"], "response.create")
        await client.handle({"type": "response.text.delta", "delta": "模拟完成"}, generation)
        await client.handle({"type": "response.done", "response": {"status": "completed"}}, generation)
        await self.runtime.ledger.flush()
        self.assertEqual((await self.runtime.load_conversation("annie-a", "default", "annie"))[
            "turns"][0]["assistant_generated_text"], "模拟完成")

    async def test_external_llm_text_path_skips_realtime_response_generation(self):
        llm = FakeExternalLlm([
            FakeLlmDelta("text", "我选"),
            FakeLlmDelta("tool_call", tool_call={
                "id": "call-1", "name": "preview_action", "arguments": '{"description":"点头"}',
            }),
            FakeLlmDelta("text", "绿茶。"),
        ])
        runtime = HarborRuntime(
            [RobotConfig("annie-x", room_id="room-x", fake_delay_ms=0, listen_enabled=True)],
            Path(self.tmp.name) / "cascade.db",
            realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket,
            external_llm=llm,
        )
        await runtime.start()
        self.addAsyncCleanup(runtime.close)
        for _ in range(100):
            if runtime.realtime["annie-x"].ready:
                break
            await asyncio.sleep(0.01)
        client = runtime.realtime["annie-x"]
        self.assertEqual(client.socket.outbound[0]["session"]["modalities"], ["text", "audio"])
        await runtime.realtime_text("annie-x", "你选一个")
        for _ in range(100):
            if client.turn_id is None:
                break
            await asyncio.sleep(0.01)
        await runtime.ledger.flush()
        stored = await runtime.load_conversation("annie-x", "default", "annie")
        self.assertEqual(stored["turns"][0]["assistant_generated_text"], "我选绿茶。")
        self.assertFalse(any(msg.get("type") == "response.create" for msg in client.socket.outbound))
        self.assertTrue(any(e["kind"] == "tool.result" and e["result"]["status"] == "simulated"
                            for e in client.events))
        self.assertTrue(llm.messages[0][1])

    async def test_external_llm_audio_final_drives_answer_and_ignores_provider_dot(self):
        llm = FakeExternalLlm([FakeLlmDelta("text", "收到语音。")])
        runtime = HarborRuntime(
            [RobotConfig("annie-y", room_id="room-y", fake_delay_ms=0, listen_enabled=True)],
            Path(self.tmp.name) / "cascade-audio.db",
            realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket,
            external_llm=llm,
        )
        await runtime.start()
        self.addAsyncCleanup(runtime.close)
        for _ in range(100):
            if runtime.realtime["annie-y"].ready:
                break
            await asyncio.sleep(0.01)
        client = runtime.realtime["annie-y"]
        gen = client.robot.generation
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "transcript": "你好呀"}, gen)
        await client.handle({"type": "response.text.delta", "delta": "。"}, gen)
        await client.handle({"type": "response.done", "response": {"status": "completed"}}, gen)
        for _ in range(100):
            if client.turn_id is None:
                break
            await asyncio.sleep(0.01)
        await runtime.ledger.flush()
        stored = await runtime.load_conversation("annie-y", "default", "annie")
        self.assertEqual(stored["turns"][0]["user_text"], "你好呀")
        self.assertEqual(stored["turns"][0]["assistant_generated_text"], "收到语音。")
        self.assertEqual(client.snapshot()["provider_text_ignored"], 1)

    async def test_external_llm_duplicate_final_does_not_launch_two_answers(self):
        llm = FakeExternalLlm([FakeLlmDelta("text", "只回复一次。")])
        runtime = HarborRuntime(
            [RobotConfig("annie-one", room_id="room-one", fake_delay_ms=0, listen_enabled=True)],
            Path(self.tmp.name) / "duplicate-final.db", realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket, external_llm=llm,
        )
        await runtime.start()
        self.addAsyncCleanup(runtime.close)
        client = runtime.realtime["annie-one"]
        for _ in range(100):
            if client.ready:
                break
            await asyncio.sleep(0.01)
        final = {"type": "conversation.item.input_audio_transcription.completed",
                 "item_id": "asr-001", "transcript": "你好"}
        await client.handle(final, client.robot.generation)
        await client.handle(final, client.robot.generation)
        for _ in range(100):
            if client.turn_id is None:
                break
            await asyncio.sleep(0.01)
        await runtime.ledger.flush()
        turns = (await runtime.load_conversation("annie-one", "default", "annie"))["turns"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["assistant_generated_text"], "只回复一次。")
        self.assertEqual(len(llm.messages), 1)
        self.assertTrue(any(e["kind"] == "transcript.duplicate" for e in client.events))

    async def test_external_llm_tool_only_response_requires_text_followup(self):
        class ToolOnlyLlm(FakeExternalLlm):
            async def stream(self, messages, *, tools=True):
                self.messages.append((messages, tools))
                if tools:
                    yield FakeLlmDelta("tool_call", tool_call={
                        "id": "call-01", "name": "preview_action",
                        "arguments": '{"description":"点头"}',
                    })
                else:
                    self.assert_tool_messages(messages)
                    yield FakeLlmDelta("text", "只是模拟点头，没有执行真机动作。")

            @staticmethod
            def assert_tool_messages(messages):
                assert messages[-1]["role"] == "tool"
                assert '"real_device_executed": false' in messages[-1]["content"]

        llm = ToolOnlyLlm()
        runtime = HarborRuntime(
            [RobotConfig("annie-one", room_id="room-one", fake_delay_ms=0, listen_enabled=True)],
            Path(self.tmp.name) / "tool-only.db", realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket, external_llm=llm,
        )
        await runtime.start()
        self.addAsyncCleanup(runtime.close)
        client = runtime.realtime["annie-one"]
        for _ in range(100):
            if client.ready:
                break
            await asyncio.sleep(0.01)
        await runtime.realtime_text("annie-one", "请模拟点头")
        for _ in range(100):
            if client.turn_id is None:
                break
            await asyncio.sleep(0.01)
        await runtime.ledger.flush()
        turns = (await runtime.load_conversation("annie-one", "default", "annie"))["turns"]
        self.assertEqual(turns[0]["assistant_generated_text"], "只是模拟点头，没有执行真机动作。")
        self.assertEqual([tools for _messages, tools in llm.messages], [True, False])

    async def test_external_llm_valid_barge_cancels_old_generation(self):
        class SlowLlm(FakeExternalLlm):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def stream(self, messages, *, tools=True):
                self.messages.append((messages, tools))
                if messages[-1]["content"] == "旧问题":
                    self.started.set()
                    await self.release.wait()
                    yield FakeLlmDelta("text", "不应出现")
                else:
                    yield FakeLlmDelta("text", "新回复。")

        llm = SlowLlm()
        runtime = HarborRuntime(
            [RobotConfig("annie-one", room_id="room-one", fake_delay_ms=0, listen_enabled=True)],
            Path(self.tmp.name) / "barge.db", realtime_key="fake-test-key",
            realtime_socket_factory=FakeSocket, external_llm=llm,
        )
        await runtime.start()
        self.addAsyncCleanup(runtime.close)
        client = runtime.realtime["annie-one"]
        for _ in range(100):
            if client.ready:
                break
            await asyncio.sleep(0.01)
        await runtime.realtime_text("annie-one", "旧问题")
        await asyncio.wait_for(llm.started.wait(), 1)
        gen = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started", "item_id": "voice-new"}, gen)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "voice-new", "transcript": "新问题"}, gen)
        llm.release.set()
        for _ in range(100):
            if client.turn_id is None:
                break
            await asyncio.sleep(0.01)
        await runtime.ledger.flush()
        turns = (await runtime.load_conversation("annie-one", "default", "annie"))["turns"]
        self.assertEqual([t["status"] for t in turns], ["cancelled", "completed"])
        self.assertEqual(turns[-1]["user_text"], "新问题")
        self.assertEqual(turns[-1]["assistant_generated_text"], "新回复。")
        self.assertFalse(any(e.get("text") == "不应出现" for e in client.events))

    async def test_llm_history_keeps_persona_system_and_prefers_bounded_spoken_history(self):
        client = self.runtime.realtime["annie-a"]
        messages = client._history_messages("你选哪个？")
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1], {"role": "user", "content": "你选哪个？"})
        self.assertIn("小安妮", messages[0]["content"])

    async def test_audio_gate_drains_to_own_realtime_socket(self):
        source = "room:roomv2:reb_mic"
        self.runtime.bind_input_source(source, ["annie-a"])
        from annie_adapters import VERIFIED_MIC_FORMAT

        self.runtime.start_pcm_stream(source, VERIFIED_MIC_FORMAT)
        # Do not invent a new audio envelope; use the already validated Room wire shape.
        self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes(640))
        for _ in range(50):
            if self.runtime.realtime["annie-a"].frames_sent:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.runtime.realtime["annie-a"].frames_sent, 1)
        self.assertEqual(self.runtime.realtime["annie-b"].frames_sent, 0)

    async def test_audio_pump_waits_for_frame_without_polling(self):
        """An idle pump waits on get(), then sends promptly when put() wakes it."""
        client = self.runtime.realtime["annie-a"]
        source = "room:roomv2:reb_mic"
        from annie_adapters import VERIFIED_MIC_FORMAT

        self.runtime.bind_input_source(source, ["annie-a"])
        self.runtime.start_pcm_stream(source, VERIFIED_MIC_FORMAT)
        await asyncio.sleep(0)
        self.assertFalse(client.audio_task.done())
        self.assertEqual(client.frames_sent, 0)
        self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes(640))
        await asyncio.wait_for(client.robot.audio.queue.join(), timeout=1)
        self.assertEqual(client.frames_sent, 1)
        self.assertEqual(client.robot.audio.queue.qsize(), 0)
        self.assertFalse(client.audio_task.done())

    async def test_audio_inflight_task_done_on_cancel(self):
        """Cancellation during a blocked send must not strand queue.join()."""
        client = self.runtime.realtime["annie-a"]
        source = "room:roomv2:reb_mic"
        from annie_adapters import VERIFIED_MIC_FORMAT

        self.runtime.bind_input_source(source, ["annie-a"])
        self.runtime.start_pcm_stream(source, VERIFIED_MIC_FORMAT)
        started = asyncio.Event()
        release = asyncio.Event()
        original_send = client.socket.send

        async def blocked_send(event):
            if event["type"] == "input_audio_buffer.append":
                started.set()
                await release.wait()
            await original_send(event)

        client.socket.send = blocked_send
        self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes(640))
        await asyncio.wait_for(started.wait(), timeout=1)
        join_task = asyncio.create_task(client.robot.audio.queue.join())
        await asyncio.sleep(0)
        self.assertFalse(join_task.done())
        client.audio_task.cancel()
        await asyncio.gather(client.audio_task, return_exceptions=True)
        await asyncio.wait_for(join_task, timeout=1)
        self.assertEqual(client.frames_sent, 0)

    async def test_audio_queue_overflow_retains_newest_and_balances_join(self):
        client = self.runtime.realtime["annie-a"]
        source = "room:roomv2:reb_mic"
        from annie_adapters import VERIFIED_MIC_FORMAT

        self.runtime.bind_input_source(source, ["annie-a"])
        self.runtime.start_pcm_stream(source, VERIFIED_MIC_FORMAT)
        started = asyncio.Event()
        release = asyncio.Event()
        original_send = client.socket.send

        async def block_first_audio(event):
            if event["type"] == "input_audio_buffer.append" and not started.is_set():
                started.set()
                await release.wait()
            await original_send(event)

        client.socket.send = block_first_audio
        self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes(640))
        await asyncio.wait_for(started.wait(), timeout=1)
        capacity = client.robot.audio.queue.maxsize
        for i in range(capacity + 3):
            self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes([i + 1]) * 640)
        self.assertEqual(client.robot.audio.dropped, 3)
        release.set()
        await asyncio.wait_for(client.robot.audio.queue.join(), timeout=1)
        self.assertEqual(client.frames_sent, capacity + 1)
        sent = [event for event in client.socket.outbound
                if event["type"] == "input_audio_buffer.append"]
        self.assertEqual(base64.b64decode(sent[-1]["audio"]), bytes([capacity + 3]) * 640)

    async def test_audio_expired_after_enqueue_is_dropped(self):
        """Freshness is checked at send, not only when the Room frame arrived."""
        client = self.runtime.realtime["annie-a"]
        source = "room:roomv2:reb_mic"
        from annie_adapters import VERIFIED_MIC_FORMAT

        self.runtime.bind_input_source(source, ["annie-a"])
        self.runtime.start_pcm_stream(source, VERIFIED_MIC_FORMAT)
        # Queue a frame before the pump starts to guarantee stale age at dequeue.
        client.audio_task.cancel()
        await asyncio.gather(client.audio_task, return_exceptions=True)
        self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes(640))
        await asyncio.sleep(client.robot.config.audio_max_age_ms / 1000 + 0.02)
        before = client.frames_dropped
        # Restart only the pump so its first queue.get() sees the expired frame.
        client.audio_task = asyncio.create_task(client._audio_pump(client.robot.generation))
        await asyncio.wait_for(client.robot.audio.queue.join(), timeout=1)
        self.assertEqual(client.frames_dropped, before + 1)
        self.assertEqual(client.frames_sent, 0)

    async def test_stream_restart_does_not_forward_old_frame(self):
        """Same source ID does not make an old stream's frame valid again."""
        client = self.runtime.realtime["annie-a"]
        source = "room:roomv2:reb_mic"
        from annie_adapters import VERIFIED_MIC_FORMAT

        self.runtime.bind_input_source(source, ["annie-a"])
        self.runtime.start_pcm_stream(source, VERIFIED_MIC_FORMAT)
        audio = client.robot.audio
        client.audio_task.cancel()
        await asyncio.gather(client.audio_task, return_exceptions=True)
        acquired = asyncio.Event()
        release = asyncio.Event()
        original_get = audio.next_frame

        async def delayed_get():
            frame, stream_epoch = await original_get()
            acquired.set()
            await release.wait()
            return frame, stream_epoch

        audio.next_frame = delayed_get
        client.audio_task = asyncio.create_task(client._audio_pump(client.robot.generation))
        self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes([1]) * 640)
        await asyncio.wait_for(acquired.wait(), timeout=1)
        old_epoch = audio.stream_epoch
        self.runtime.start_pcm_stream(source, VERIFIED_MIC_FORMAT)
        self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes([2]) * 640)
        self.assertNotEqual(old_epoch, audio.stream_epoch)
        release.set()
        await asyncio.wait_for(audio.queue.join(), timeout=1)
        self.assertEqual(client.frames_dropped, 1)
        self.assertEqual(client.frames_sent, 1)
        pcm_messages = [item for item in client.socket.outbound
                        if item["type"] == "input_audio_buffer.append"]
        self.assertEqual(len(pcm_messages), 1)
        self.assertEqual(base64.b64decode(pcm_messages[0]["audio"]), bytes([2]) * 640)
