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
        for invalid in (0, 51, True, 8.5, "8"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                session_update("小安妮", invalid)

    def test_url_does_not_change_model_or_accept_insecure_transport(self):
        self.assertIn("model=" + MODEL, realtime_url("wss://dashscope.aliyuncs.com/api-ws/v1/realtime"))
        with self.assertRaises(ValueError):
            realtime_url("ws://dashscope.aliyuncs.com/api-ws/v1/realtime")
        with self.assertRaises(ValueError):
            realtime_url("wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=other")


class RealtimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_cancel_observed_lifecycle_and_idempotence(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        # A completed provider response has no active cancellation target.
        await client.handle({"type": "response.created",
                             "response": {"id": "done"}}, gen)
        await client.handle({"type": "response.done", "response": {
            "id": "done", "status": "completed"}}, gen)
        before = len(client.socket.outbound)
        self.assertFalse(await client._request_response_cancel(expected_id="done"))
        self.assertEqual(len(client.socket.outbound), before)

        await client.handle({"type": "response.created",
                             "response": {"id": "live"}}, gen)
        self.assertFalse(await client._request_response_cancel(expected_id="other"))
        self.assertTrue(await client._request_response_cancel(expected_id="live"))
        self.assertFalse(await client._request_response_cancel(expected_id="live"))
        cancels = [packet for packet in client.socket.outbound
                   if packet["type"] == "response.cancel"]
        self.assertEqual(len(cancels), 1)
        event_id = cancels[0]["event_id"]
        self.assertTrue(event_id.startswith("annie_cancel_"))
        await client.handle({"type": "error", "event_id": "server-error-1", "error": {
            "code": "invalid_value", "message": "Conversation has no active response.",
            "event_id": event_id}}, gen)
        self.assertTrue(client.ready)
        self.assertTrue(any(e["kind"] == "response.cancel_race_ignored" for e in client.events))
        # An unrelated provider error must never be suppressed.
        with self.assertRaisesRegex(RuntimeError, "no active response"):
            await client.handle({"type": "error", "event_id": "server-error-2", "error": {
                "code": "invalid_value", "message": "Conversation has no active response.",
                "event_id": event_id}}, gen)
        with self.assertRaisesRegex(RuntimeError, "provider refused"):
            await client.handle({"type": "error", "error": {
                "code": "invalid_value", "message": "provider refused a tool"}}, gen)

    async def test_wake_reject_does_not_cancel_already_finished_provider_response(self):
        from annie_runtime.wakeword import WakeAdmission

        class Detector:
            async def detected(self, _):
                return False

        client = self.runtime.realtime["annie-a"]
        client.wake = WakeAdmission(Detector())
        client.wake.presence.wake_detected()
        gen = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "wake-alone"}, gen)
        await client.handle({"type": "response.created", "response": {"id": "fast"}}, gen)
        await client.handle({"type": "response.done", "response": {
            "id": "fast", "status": "completed"}}, gen)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "wake-alone", "transcript": "小安妮"}, gen)
        self.assertFalse(any(x["type"] == "response.cancel" for x in client.socket.outbound))
        self.assertEqual(client.wake.state, "WAKE_WAIT")
        self.assertIsNone(client.turn_id)

    async def test_cancelled_response_events_cannot_finish_or_replace_next_turn(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        await client.submit_text("旧问题")
        old_turn = client.turn_id
        await client.handle({"type": "response.created", "response": {"id": "old-response"}}, gen)
        await client.cancel()
        self.assertEqual(client.robot.turn_cache[old_turn]["status"], "cancelled")
        self.assertTrue(any(e["type"] == "response.cancel" for e in client.socket.outbound))
        await client.submit_text("新问题")
        new_turn = client.turn_id
        await client.handle({"type": "response.created", "response": {"id": "new-response"}}, gen)
        for event in (
            {"type": "response.text.delta", "response_id": "old-response", "delta": "旧回答"},
            {"type": "response.text.done", "response_id": "old-response", "text": "旧回答"},
            {"type": "response.done", "response": {"id": "old-response", "status": "cancelled"}},
        ):
            await client.handle(event, gen)
        self.assertEqual(client.turn_id, new_turn)
        self.assertEqual(client.response_id, "new-response")
        self.assertEqual(client.text, "")
        self.assertEqual(client.robot.turn_cache[new_turn]["status"], "running")
        self.assertEqual(len([e for e in client.events if e["kind"] == "response.stale"]), 3)
        await client.handle({"type": "response.text.delta", "response_id": "new-response",
                             "delta": "本轮回答"}, gen)
        await client.handle({"type": "response.done", "response": {
            "id": "new-response", "status": "completed"}}, gen)
        self.assertEqual(client.robot.turn_cache[new_turn]["assistant_generated_text"], "本轮回答")

    async def test_provider_cancel_deletes_generated_assistant_item_from_server_history(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        await client.submit_text("当前问题")
        await client.handle({"type": "response.created", "response": {"id": "resp-one"}}, gen)
        await client.handle({"type": "response.output_item.added", "response_id": "resp-one",
                             "item": {"id": "assistant-one", "type": "message",
                                      "role": "assistant"}}, gen)
        await client.handle({"type": "response.done", "response": {
            "id": "resp-one", "status": "cancelled", "status_details": {
                "reason": "turn_detected"}}}, gen)
        self.assertIn({"type": "conversation.item.delete", "item_id": "assistant-one"},
                      client.socket.outbound)
        self.assertEqual(client.robot.snapshot.cancelled, 1)
        self.assertFalse(any(e["kind"] == "context.unheard_unverified" for e in client.events))

    async def test_cancel_after_response_done_deletes_unheard_item_but_not_drained_item(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation

        class Speaker:
            turn_id = None

            async def finish(self, turn_id):
                self.turn_id = turn_id

            async def cancel(self, _reason):
                self.turn_id = None

            async def close(self):
                pass

            def snapshot(self):
                return {}

        client.speech = Speaker()
        for label, drained in (("unheard", False), ("drained", True)):
            await client.submit_text(label)
            turn_id = client.turn_id
            await client.handle({"type": "response.created", "response": {"id": label}}, gen)
            await client.handle({"type": "response.done", "response": {
                "id": label, "status": "completed", "output": [{
                    "id": label + "-item", "role": "assistant", "type": "message"}]}}, gen)
            if drained:
                client.publish("tts.playback.drained", turn_id=turn_id, source="speaker")
            await client.cancel()
        deleted = [e["item_id"] for e in client.socket.outbound
                   if e["type"] == "conversation.item.delete"]
        self.assertEqual(deleted, ["unheard-item"])

    async def test_smart_turn_interrupts_completed_reply_still_playing(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation

        class Speaker:
            turn_id = "old-spoken-turn"
            control_state = "held_confirmed"
            cancelled = False

            def request_playback_control(self, kind, *, reason):
                pass

            async def cancel(self, reason):
                self.cancelled = reason
                self.turn_id = None

            async def close(self):
                pass

        speaker = Speaker()
        client.speech = speaker
        client._unheard_output_ids["old-spoken-turn"] = {"unheard-item"}
        self.assertIsNone(client.turn_id)  # response.done already fired
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "new-audio"}, gen)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "new-audio", "transcript": "不是茶，是咖啡"}, gen)
        self.assertEqual(speaker.cancelled, "valid_smart_turn")
        self.assertNotIn("old-spoken-turn", client._unheard_output_ids)
        self.assertIn({"type": "conversation.item.delete", "item_id": "unheard-item"},
                      client.socket.outbound)
        self.assertEqual(client.robot.turn_cache[client.turn_id]["user_text"], "不是茶，是咖啡")

    async def test_native_startup_has_no_cascade_or_idle_keepalive(self):
        client = self.runtime.realtime["annie-a"]
        self.assertFalse(hasattr(client, "external_llm"))
        self.assertFalse(hasattr(client, "_idle_keepalive"))
        self.assertEqual(client.socket.outbound[0]["session"]["modalities"], ["text"])
        self.assertEqual(client.socket.outbound[0]["session"]["tools"][0]["function"]["name"],
                         "preview_action")
        self.assertFalse(any(packet["type"] == "response.create" for packet in client.socket.outbound))

    async def test_native_asr_new_candidate_does_not_rewrite_previous_completed_turn(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "first-audio"}, gen)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "first-audio", "transcript": "我要喝茶"}, gen)
        first_turn = client.turn_id
        await client.handle({"type": "response.text.delta", "delta": "好的。"}, gen)
        await client.handle({"type": "response.done", "response": {"status": "completed"}}, gen)
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "second-audio"}, gen)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "second-audio", "transcript": "不是茶，是咖啡"}, gen)
        self.assertNotEqual(first_turn, client.turn_id)
        self.assertEqual(client.robot.turn_cache[first_turn]["user_text"], "我要喝茶")
        self.assertEqual(client.robot.turn_cache[client.turn_id]["user_text"], "不是茶，是咖啡")
        self.assertEqual(len(client.robot.turn_cache), 2)

    async def test_native_audio_response_does_not_create_second_llm_request(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "transcript": "帮我聊天"}, gen)
        await client.handle({"type": "response.text.delta", "delta": "当然可以。"}, gen)
        await client.handle({"type": "response.done", "response": {"status": "completed"}}, gen)
        self.assertEqual(len(client.robot.turn_cache), 1)
        self.assertEqual(next(iter(client.robot.turn_cache.values()))["assistant_generated_text"],
                         "当然可以。")
        self.assertFalse(any(event["type"] == "response.create"
                             for event in client.socket.outbound))

    async def test_native_asr_final_does_not_cancel_its_own_early_response(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "native-early"}, gen)
        # Realtime text may arrive before the final ASR item; this is the new
        # turn, not old speech to cancel when transcription finally arrives.
        client._create_turn("", audio=True)
        turn_id = client.turn_id

        class Speaker:
            def __init__(self):
                self.turn_id = turn_id
                self.cancelled = False

            async def cancel(self, reason):
                self.cancelled = reason

            async def close(self):
                pass

        speaker = Speaker()
        client.speech = speaker
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "native-early", "transcript": "本轮的新问题"}, gen)
        self.assertEqual(client.turn_id, turn_id)
        self.assertEqual(client.robot.turn_cache[turn_id]["user_text"], "本轮的新问题")
        self.assertFalse(speaker.cancelled)

    async def test_timeline_stage_contract_and_speaker_only_percentiles(self):
        client = self.runtime.realtime["annie-a"]
        turn_id = "timed-turn"
        client.publish("turn.started", turn_id=turn_id, user_text="测试",
                       speech_started_ms=1000, speech_stopped_ms=1500,
                       asr_final_ms=1650)
        client.publish("llm.request_started", turn_id=turn_id)
        client.publish("text.delta", turn_id=turn_id, text="回复")
        client.publish("tts.text_submitted", turn_id=turn_id)
        client.publish("tts.first_audio", turn_id=turn_id)
        client.publish("tts.first_published", turn_id=turn_id)
        snap = client.snapshot()
        timings = snap["last_turn_timeline"]["durations"]
        self.assertEqual(timings["speech_to_asr_final_ms"], 650)
        self.assertEqual(timings["endpoint_to_asr_final_ms"], 150)
        self.assertIsNone(timings["endpoint_to_speaker_start_ms"])
        self.assertEqual(
            snap["latency_percentiles"]["endpoint_to_speaker_start_ms"]["samples"], 0)
        client.publish("tts.playback.started", turn_id=turn_id, source="browser_test")
        self.assertIsNone(client.snapshot()["last_turn_timeline"]["durations"][
            "endpoint_to_speaker_start_ms"])
        client.publish("tts.playback.started", turn_id=turn_id, source="speaker")
        stats = client.snapshot()["latency_percentiles"]["endpoint_to_speaker_start_ms"]
        self.assertEqual(stats["samples"], 1)
        self.assertEqual(stats["p50_ms"], stats["p95_ms"])
        self.assertGreater(stats["p50_ms"], 0)

    async def test_new_speech_does_not_inherit_previous_utterance_endpoint(self):
        client = self.runtime.realtime["annie-a"]
        client.last_provider_stopped_ms = 123456
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "new-utterance"}, client.robot.generation)
        self.assertIsNone(client.last_provider_stopped_ms)
        client._create_turn("新一轮", audio=True)
        timeline = client.snapshot()["last_turn_timeline"]
        self.assertIsNone(timeline["times"]["speech_stopped_ms"])
        self.assertIsNone(timeline["durations"]["endpoint_to_speaker_start_ms"])

    async def test_diagnostics_capture_exact_session_prompt_and_bounded_tool_evidence(self):
        client = self.runtime.realtime["annie-a"]
        other = self.runtime.realtime["annie-b"]

        class Memory:
            async def bootstrap(self, robot, persona, chat):
                self_scope = (robot, persona, chat)
                assert self_scope == ("annie-a", "annie", "default")
                return "\n记忆：\n[当前Chat约束] 本次少开玩笑\n[长期事实] 用户喜欢茶"

            async def recall(self, _robot, _persona, _query):
                return {"status": "ok", "evidence": [{"id": "m1", "text": "用户喜欢茶"}]}

        client.memory = Memory()
        await client.restart()
        for _ in range(100):
            if client.ready:
                break
            await asyncio.sleep(.01)
        self.assertTrue(client.ready)
        self.assertNotIn("prompt_snapshot", client.snapshot())
        self.assertNotIn("prompt_snapshot", self.runtime.realtime_state("annie-a"))
        prompt = client.diagnostic_snapshot()["prompt_snapshot"]
        self.assertEqual(self.runtime.realtime_diagnostics("annie-a")["prompt_snapshot"], prompt)
        outgoing = client.socket.outbound[0]["session"]
        self.assertEqual(prompt["instructions"], outgoing["instructions"])
        self.assertEqual(prompt["bootstrap_count"], 2)
        self.assertIn("recall_memory", prompt["tool_names"])
        self.assertEqual(prompt["provider_session_id"], client.session)
        self.assertEqual(other.diagnostic_snapshot()["prompt_snapshot"]["bootstrap_count"], 0)
        await client.submit_text("以前我喜欢喝什么")
        turn_id = client.turn_id
        await client._tool_call({"call_id": "memory-call", "name": "recall_memory",
                                 "arguments": '{"query":"喜欢喝什么"}'})
        for _ in range(600):
            client.publish("transcript.delta", text="热事件")
        history = client.diagnostic_snapshot()["tool_history"]
        refs = client.diagnostic_snapshot()["turn_prompt_refs"]
        self.assertEqual(refs[-1], {"turn_id": turn_id,
                                    "provider_session_id": prompt["provider_session_id"]})
        self.assertEqual(client.diagnostic_snapshot()["prompt_history"][-1]["instructions"],
                         outgoing["instructions"])
        self.assertEqual([item["kind"] for item in history], ["tool.called", "tool.result"])
        self.assertEqual(history[-1]["turn_id"], turn_id)
        self.assertEqual(history[-1]["evidence_count"], 1)
        self.assertEqual(history[-1]["result"]["evidence"][0]["id"], "m1")
        self.assertEqual(other.diagnostic_snapshot()["tool_history"], [])
        await client.restart()
        self.assertEqual(client.diagnostic_snapshot()["tool_history"], [])
        self.assertEqual(client.diagnostic_snapshot()["turn_prompt_refs"], [])
        self.assertEqual(client.diagnostic_snapshot()["prompt_history"], [])

    async def test_turn_diagnostics_keep_action_viseme_and_timing_per_turn(self):
        client = self.runtime.realtime["annie-a"]
        other = self.runtime.realtime["annie-b"]
        await client.submit_text("请点下头")
        first = client.turn_id
        client.publish("tts.first_audio", turn_id=first)
        client.publish("tts.first_published", turn_id=first)
        client.publish("action.state", turn_id=first, call_id="motion-1",
                       action_kind="action", action_value="agree_micro_nod",
                       status="published_unknown", real_device_executed=False,
                       device_completion="unknown")
        for index in range(100):
            client.publish("viseme.aligned", turn_id=first, viseme="a",
                           start_sample=index * 100, end_sample=index * 100 + 100)
        for _ in range(700):
            client.publish("transcript.delta", text="not_this_turn")
        capture = client.diagnostic_snapshot()
        one = next(item for item in capture["turn_timelines"]
                   if item["turn_id"] == first)
        self.assertEqual(one["viseme_count"], 100)
        self.assertIsNotNone(one["times"]["llm_request_ms"])
        self.assertIsNotNone(one["times"]["tts_first_pcm_ms"])
        selected = [event for event in capture["turn_events"]
                    if event["turn_id"] == first]
        self.assertEqual(len([event for event in selected
                              if event["kind"] == "viseme.aligned"]), 3)
        self.assertEqual(next(event for event in selected
                              if event["kind"] == "action.state")["status"],
                         "published_unknown")
        self.assertEqual(other.diagnostic_snapshot()["turn_events"], [])
        await client.handle({"type": "response.done", "response": {
            "status": "completed"}}, client.robot.generation)
        await client.submit_text("第二轮")
        second = client.turn_id
        client.publish("action.state", turn_id=second, call_id="motion-2",
                       status="rejected_late", real_device_executed=False)
        capture = client.diagnostic_snapshot()
        one_events = [event for event in capture["turn_events"]
                      if event["turn_id"] == first]
        two_events = [event for event in capture["turn_events"]
                      if event["turn_id"] == second]
        self.assertFalse(any(event.get("call_id") == "motion-2"
                             for event in one_events))
        self.assertTrue(any(event.get("call_id") == "motion-2"
                            for event in two_events))

    async def test_speculative_hold_is_reversible_and_keeps_robot_scope(self):
        client = self.runtime.realtime["annie-a"]
        other = self.runtime.realtime["annie-b"]

        class Speaking:
            turn_id = "voice-turn"

            async def close(self):
                pass

            def request_playback_control(self, kind, *, reason):
                return True

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

    async def test_ambient_only_outcome_resumes_held_audio_without_new_reply(self):
        client = self.runtime.realtime["annie-a"]
        other = self.runtime.realtime["annie-b"]

        class Speaking:
            turn_id = "voice-turn"
            control_state = "held_confirmed"

            def __init__(self):
                self.commands = []

            def request_playback_control(self, kind, *, reason):
                self.commands.append((kind, reason))

            async def cancel(self, _reason):
                pass

            async def close(self):
                pass

        speaker = Speaking()
        client.speech = speaker
        generation = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started"}, generation)
        await client.handle({"type": "conversation.item.ambient_audio_transcription.completed",
                             "transcript": "嗯"}, generation)
        self.assertIn(("resume", "ambient_audio"), speaker.commands)
        self.assertEqual(client.events[-2]["kind"], "playback.resume_requested")
        self.assertEqual(client.events[-1]["kind"], "transcript.ambient")
        self.assertFalse(client.speech_active)
        self.assertFalse(client.robot.turn_cache)
        self.assertFalse(any(event["kind"].startswith("playback.") for event in other.events))

    async def test_demo_stable_asr_prefix_confirms_hold_before_final_without_losing_turn(self):
        client = self.runtime.realtime["annie-a"]
        generation = client.robot.generation

        class Speaking:
            turn_id = "old-voice"
            control_state = "held_confirmed"

            def __init__(self):
                self.cancelled = []

            def request_playback_control(self, kind, *, reason):
                pass

            async def cancel(self, reason):
                self.cancelled.append(reason)
                self.turn_id = None
                self.control_state = "unknown"

            async def close(self):
                pass

        speaker = Speaking()
        client.speech = speaker
        client._create_turn("旧轮次")
        client.llm_task = None
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "new-speech"}, generation)
        await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                             "text": "我", "stash": "想说话"}, generation)
        self.assertEqual(speaker.cancelled, [])
        await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                             "text": "我想说", "stash": "话"}, generation)
        self.assertEqual(speaker.cancelled, ["stable_asr_prefix"])
        self.assertTrue(client.speech_active)
        self.assertIsNone(client.turn_id)
        await client.handle({"type": "input_audio_buffer.speech_stopped",
                             "reason": "turn_invalid"}, generation)
        self.assertFalse(any(e["kind"] == "playback.resume_requested"
                             for e in client.events))
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "new-speech", "transcript": "我想说话"}, generation)
        self.assertEqual(client.robot.turn_cache[client.turn_id]["user_text"], "我想说话")
        self.assertEqual(len([e for e in client.events if
                              e["kind"] == "playback.interrupt_confirmed"]), 1)

    async def test_duplicate_speech_start_does_not_rehold_or_reset_asr(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "same"}, gen)
        await client.handle({"type": "conversation.item.input_audio_transcription.delta",
                             "item_id": "same", "text": "我", "stash": "说"}, gen)
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "same"}, gen)
        self.assertEqual(client.speech_started_count, 1)
        self.assertEqual(client.asr_delta_count, 1)
        self.assertEqual(client.events[-1]["kind"], "speech.duplicate")

    async def test_unidentified_new_speech_supersedes_identified_old_candidate(self):
        client = self.runtime.realtime["annie-a"]
        gen = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "old"}, gen)
        await client.handle({"type": "input_audio_buffer.speech_started"}, gen)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "old", "transcript": "上轮问题"}, gen)
        self.assertEqual(client.events[-1]["kind"], "transcript.stale")
        self.assertIsNone(client.turn_id)

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

    async def test_qwen_speech_is_relayed_to_voice_by_brain_not_as_direct_cloud_message(self):
        class Voice:
            def __init__(self):
                self.messages = []

            async def publish(self, message):
                self.messages.append(message)

            async def close(self):
                pass

        voice = Voice()
        client = self.runtime.realtime["annie-a"]
        self.runtime.voice_publishers["annie-a"] = voice
        client.voice_event = lambda kind, event: self.runtime._send_voice_event(
            "annie-a", kind, event,
        )
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "win-mic-speech"}, client.robot.generation)
        for _ in range(20):
            if voice.messages:
                break
            await asyncio.sleep(.005)
        self.assertEqual(voice.messages[0]["type"], "voice.interaction")
        self.assertEqual(list(voice.messages[0])[:3], ["type", "description", "ts"])
        self.assertEqual(voice.messages[0]["description"], "检测到用户开始说话")
        self.assertEqual(voice.messages[0]["data"]["sender"], "brain")
        self.assertEqual(voice.messages[0]["data"]["trigger_source"], "qwen_realtime")
        self.assertEqual(voice.messages[0]["data"]["event"], "speech.started")
        self.assertEqual(voice.messages[0]["data"]["item_id"], "win-mic-speech")
        self.assertEqual(self.runtime.realtime["annie-b"].voice_event, None)

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

    async def test_ambient_never_promotes_even_when_it_looks_like_a_correction(self):
        client = self.runtime.realtime["annie-a"]
        generation = client.robot.generation
        await client.handle({"type": "conversation.item.ambient_audio_transcription.completed",
                             "item_id": "filler", "transcript": "嗯。"}, generation)
        self.assertIsNone(client.turn_id)
        self.assertEqual(client.events[-1]["kind"], "transcript.ambient")
        await client.handle({"type": "conversation.item.ambient_audio_transcription.completed",
                             "item_id": "correction", "transcript": "不是这个。"}, generation)
        self.assertFalse(any(e["kind"] == "transcript.ambient_promoted" for e in client.events))
        self.assertFalse(client.robot.turn_cache)
        self.assertFalse(any(e["type"] == "response.create" for e in client.socket.outbound))

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

    async def test_wake_model_is_shared_but_pcm_admission_isolated_per_robot(self):
        from annie_adapters import VERIFIED_MIC_FORMAT
        from annie_runtime.wakeword import FRAME_BYTES, PRE_ROLL_BYTES, WakeAdmission

        class SharedDetector:
            def __init__(self):
                self.calls = 0
                self.first_samples = []

            async def detected(self, pcm):
                self.calls += 1
                self.first_samples.append(pcm[:4])
                return pcm.startswith(b"\x02\x02")

        detector = SharedDetector()
        a, b = (self.runtime.realtime[key] for key in ("annie-a", "annie-b"))
        a.wake = WakeAdmission(detector)
        b.wake = WakeAdmission(detector)
        await a.handle({"type": "input_audio_buffer.speech_started",
                        "item_id": "stale"}, a.robot.generation)
        await a.handle({"type": "response.created",
                        "response": {"id": "unaddressed"}}, a.robot.generation)
        self.assertIsNone(a.turn_id)
        self.assertTrue(any(e["type"] == "response.cancel" for e in a.socket.outbound))
        for robot_id, sample in (("annie-a", 2), ("annie-b", 4)):
            source = "room:" + self.runtime.robots[robot_id].config.room_id + ":reb_mic"
            self.runtime.bind_input_source(source, [robot_id])
            self.runtime.start_pcm_stream(source, VERIFIED_MIC_FORMAT)
        for _ in range(100):
            for robot_id, sample in (("annie-a", 2), ("annie-b", 4)):
                source = "room:" + self.runtime.robots[robot_id].config.room_id + ":reb_mic"
                self.runtime.ingest_pcm(source, b"\x00\x09MIC_AUDIO" + bytes([sample]) * 640)
                await asyncio.wait_for(self.runtime.robots[robot_id].audio.queue.join(), 1)
        self.assertGreaterEqual(detector.calls, 2)
        self.assertEqual(a.frames_sent, PRE_ROLL_BYTES // FRAME_BYTES, detector.first_samples)
        self.assertEqual(b.frames_sent, 0)
        self.assertEqual(a.wake.state, "WAKE_WAIT")
        self.assertEqual(b.wake.state, "SLEEP")
        a.wake.speech_started("spoken")
        a.wake.speech_stopped("spoken")
        a.wake.transcript_accepted("今天天气怎么样", "spoken")
        a.wake.authorized_turn_id = "turn1"
        a.publish("tts.playback.drained", source="browser_test")
        self.assertEqual(a.wake.state, "ACTIVE")
        a.publish("tts.playback.drained", source="speaker", turn_id="turn1")
        self.assertEqual(a.wake.state, "FOLLOWUP")

    async def test_sherpa_wake_blocks_early_qwen_response_until_valid_asr(self):
        from annie_runtime.wakeword import WakeAdmission

        class Detector:
            async def detected(self, _):
                return False

        client = self.runtime.realtime["annie-a"]
        client.wake = WakeAdmission(Detector())
        client.wake.presence.wake_detected()
        generation = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "wake-only"}, generation)
        for event in (
            {"type": "response.created", "response": {"id": "no-response"}},
            {"type": "response.text.delta", "response_id": "no-response", "delta": "我在呢"},
            {"type": "response.function_call_arguments.done", "response_id": "no-response",
             "call_id": "unwanted", "name": "preview_action",
             "arguments": '{"description":"乱动作"}'},
        ):
            await client.handle(event, generation)
        self.assertIsNone(client.turn_id)
        self.assertFalse(any(evt["kind"] == "tool.called" for evt in client.events))
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "wake-only", "transcript": "小安妮"},
                            generation)
        self.assertIsNone(client.turn_id)
        self.assertTrue(any(e["type"] == "response.cancel" for e in client.socket.outbound))
        self.assertFalse(any(evt["kind"] == "text.delta" for evt in client.events))
        await client.handle({"type": "response.output_item.added",
                             "response_id": "no-response",
                             "item": {"id": "unheard-wake", "type": "message",
                                      "role": "assistant"}}, generation)
        self.assertIn({"type": "conversation.item.delete", "item_id": "unheard-wake"},
                      client.socket.outbound)

        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "valid-question"}, generation)
        await client.handle({"type": "response.created",
                             "response": {"id": "response-for-question"}}, generation)
        await client.handle({"type": "response.text.delta",
                             "response_id": "response-for-question",
                             "delta": "今天天气不错"}, generation)
        self.assertIsNone(client.turn_id)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "valid-question", "transcript": "今天天气怎么样"},
                            generation)
        self.assertIsNotNone(client.turn_id)
        self.assertEqual(client.robot.turn_cache[client.turn_id]["user_text"], "今天天气怎么样")
        self.assertEqual(client.text, "今天天气不错")
        self.assertEqual(client.wake.state, "ACTIVE")
        await client.handle({"type": "response.done", "response": {
            "id": "response-for-question", "status": "completed"}}, generation)
        self.assertEqual(client.wake.state, "SLEEP")  # no physical speaker attached

    async def test_foreign_script_wake_transcript_is_rejected_and_removed_from_provider(self):
        from annie_runtime.wakeword import WakeAdmission

        class Detector:
            async def detected(self, _):
                return False

        client = self.runtime.realtime["annie-a"]
        client.wake = WakeAdmission(Detector())
        client.wake.presence.wake_detected()
        generation = client.robot.generation
        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "wrong-language"}, generation)
        await client.handle({"type": "response.created",
                             "response": {"id": "speculative"}}, generation)
        await client.handle({"type": "response.text.delta", "response_id": "speculative",
                             "delta": "我也爱你"}, generation)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "wrong-language", "transcript": "А не. "}, generation)
        self.assertIsNone(client.turn_id)
        self.assertEqual(client.wake.state, "WAKE_WAIT")
        self.assertTrue(any(e["type"] == "response.cancel" for e in client.socket.outbound))
        self.assertIn({"type": "conversation.item.delete", "item_id": "wrong-language"},
                      client.socket.outbound)
        self.assertTrue(any(event["kind"] == "wakeword.transcript_ignored"
                            for event in client.events))
        self.assertFalse(any(event["kind"] == "transcript.done" for event in client.events))

        await client.handle({"type": "input_audio_buffer.speech_started",
                             "item_id": "actual-question"}, generation)
        await client.handle({"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "actual-question", "transcript": "你好"}, generation)
        self.assertEqual(client.robot.turn_cache[client.turn_id]["user_text"], "你好")

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
