"""Lightweight ForcedAligner scheduling regression; never loads model weights."""

import unittest
from unittest.mock import patch

import qwen_stream_room_g2pw as sidecar


class OnlineBudgetTests(unittest.TestCase):
    def test_long_unresolved_pcm_does_not_repeatedly_call_model(self):
        conv = sidecar.ConversationState("conversation", latency_printed=True)
        segment = sidecar.SegmentState(
            "conversation-1", authoritative_text="你好", sample_rate=24000,
            total_samples=24000 * 5,
            pcm_buf=bytearray(24000 * 5 * 2),
        )
        engine = sidecar.VisemeEngine(resolver=None)
        calls = []
        engine._prons_for_text = lambda *_a, **_kw: []
        engine._build_online_events = lambda *_a, **_kw: (
            calls.append("align") or [], {"mode": "test"}
        )
        engine._publish_mouth_events = lambda *_a, **_kw: None
        with patch.object(sidecar, "log") as log:
            for _ in range(40):
                engine._emit_segment(conv, segment)
            self.assertEqual(calls, [])
            self.assertTrue(segment.online_backlog_warned)
            self.assertEqual(
                len([call for call in log.call_args_list
                     if call.args[0] == "ALIGN_BACKLOG"]), 1
            )
            engine._emit_segment(conv, segment, flush=True)
            self.assertEqual(calls, ["align"],
                             "retain complete PCM for one final pass")

    def test_short_audio_still_evaluates_online(self):
        conv = sidecar.ConversationState("conversation", latency_printed=True)
        segment = sidecar.SegmentState(
            "conversation-1", authoritative_text="你好", sample_rate=24000,
            total_samples=24000, pcm_buf=bytearray(24000 * 2),
        )
        engine = sidecar.VisemeEngine(resolver=None)
        calls = []
        engine._prons_for_text = lambda *_a, **_kw: []
        engine._build_online_events = lambda *_a, **_kw: (
            calls.append("align") or [], {"mode": "test"}
        )
        engine._publish_mouth_events = lambda *_a, **_kw: None
        with patch.object(sidecar, "log"):
            engine._emit_segment(conv, segment)
        self.assertEqual(calls, ["align"])
        self.assertFalse(segment.online_backlog_warned)

    def test_no_progress_increases_spacing_and_success_restores_fast_path(self):
        sr = 24000
        conv = sidecar.ConversationState("conversation", latency_printed=True)
        segment = sidecar.SegmentState(
            "turn", authoritative_text="你好", sample_rate=sr,
            total_samples=sr, pcm_buf=bytearray(sr * 2),
        )
        engine = sidecar.VisemeEngine(resolver=None)
        calls = []
        engine._prons_for_text = lambda *_a, **_kw: []
        engine._build_online_events = lambda *_a, **_kw: (
            calls.append(segment.total_samples) or [], {"mode": "test"}
        )
        engine._publish_mouth_events = lambda *_a, **_kw: None
        with patch.object(sidecar, "log"):
            engine._emit_segment(conv, segment)
            self.assertEqual(segment.aligner.empty_eval_count, 1)
            segment.total_samples += int(sr * .12)
            engine._emit_segment(conv, segment)
            self.assertEqual(len(calls), 1)
            segment.total_samples += int(sr * .12)
            engine._emit_segment(conv, segment)
            self.assertEqual(len(calls), 2)
            engine._build_online_events = lambda *_a, **_kw: (
                [{"at_sample": 1}], {"mode": "test"}
            )
            segment.total_samples += int(sr * .48)
            engine._emit_segment(conv, segment)
            self.assertEqual(segment.aligner.empty_eval_count, 0)


if __name__ == "__main__":
    unittest.main()
