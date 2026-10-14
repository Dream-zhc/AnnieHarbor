"""VP-A truth-table regressions; do not claim real microphone or ONNX results."""

import unittest

from annie_runtime.presence import ASR_GRACE_SECONDS, DialoguePresence


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        # Window mechanics with the historical 8 s follow-up; the default is config.
        self.policy = DialoguePresence(clock=self.clock, followup_ms=8000)

    def wake(self):
        self.assertTrue(self.policy.wake_detected())
        return self.policy.epoch

    def authorize(self, item_id="turn-1", text="今天怎么样"):
        self.assertTrue(self.policy.speech_started(item_id))
        self.assertTrue(self.policy.speech_stopped(item_id=item_id))
        return self.policy.transcript_accepted(text, item_id=item_id)

    def test_sleep_hears_kws_but_does_not_forward_pcm(self):
        self.assertFalse(self.policy.permits_pcm())
        self.assertFalse(self.policy.speech_started("ambient"))
        self.assertFalse(self.policy.transcript_accepted("旁人在聊天"))
        self.assertFalse(self.policy.permits_audio_response(0))

    def test_wake_opens_pcm_not_reply_permission(self):
        epoch = self.wake()
        self.assertTrue(self.policy.permits_pcm())
        self.assertEqual(self.policy.state, "WAKE_WAIT")
        self.assertFalse(self.policy.permits_audio_response(epoch))
        self.assertFalse(self.policy.wake_detected())

    def test_only_wake_word_does_not_authorize_response(self):
        epoch = self.wake()
        self.assertFalse(self.authorize(text="小安妮！"))
        self.assertFalse(self.policy.permits_audio_response(epoch))
        self.assertEqual(self.policy.state, "WAKE_WAIT")

    def test_wake_plus_question_is_one_valid_turn(self):
        wake_epoch = self.wake()
        self.assertTrue(self.authorize(text="小安妮，帮我看看天气"))
        epoch = self.policy.epoch
        self.assertNotEqual(wake_epoch, epoch)
        self.assertFalse(self.policy.permits_audio_response(wake_epoch))
        self.assertTrue(self.policy.permits_audio_response(epoch))
        self.assertEqual(self.policy.state, "ACTIVE")

    def test_wake_wait_timeout_without_speech(self):
        self.wake()
        self.clock.advance(5.01)
        self.assertFalse(self.policy.permits_pcm())
        self.assertEqual(self.policy.state, "SLEEP")

    def test_speech_started_in_last_millisecond_protects_long_utterance(self):
        self.wake()
        self.clock.advance(4.999)
        self.assertTrue(self.policy.speech_started("long"))
        self.clock.advance(45)
        self.assertTrue(self.policy.permits_pcm())
        self.assertTrue(self.policy.speech_stopped(item_id="long"))
        self.assertTrue(self.policy.transcript_accepted("这是一个非常长的问题", item_id="long"))
        self.assertIsNone(self.policy.snapshot()["deadline_remaining_ms"])

    def test_invalid_speech_does_not_extend_wake_window(self):
        self.wake()
        self.clock.advance(4.9)
        self.policy.speech_started("bad")
        self.clock.advance(.2)
        self.policy.speech_stopped(item_id="bad", invalid=True)
        self.assertEqual(self.policy.state, "SLEEP")

    def test_asr_delivery_grace_is_bounded(self):
        self.wake()
        self.policy.speech_started("a")
        self.policy.speech_stopped(item_id="a")
        self.clock.advance(ASR_GRACE_SECONDS + .001)
        self.assertFalse(self.policy.transcript_accepted("迟到的提问", item_id="a"))
        self.assertEqual(self.policy.state, "SLEEP")

    def test_bystander_ambient_has_no_transcript_candidate(self):
        epoch = self.wake()
        self.assertFalse(self.policy.transcript_accepted("旁人的话"))
        self.assertFalse(self.policy.permits_audio_response(epoch))

    def test_backchannels_are_not_sufficient_authorization(self):
        self.wake()
        for text in ("嗯", "哦", "对", "好的", "小安妮嗯", "小安妮好的",
                     "妮", "安妮", "小安"):
            self.policy.speech_started(text)
            self.policy.speech_stopped(item_id=text)
            self.assertFalse(self.policy.transcript_accepted(text, item_id=text))
        self.assertEqual(self.policy.state, "WAKE_WAIT")

    def test_mismatched_item_does_not_admit_late_asr(self):
        epoch = self.wake()
        self.policy.speech_started("new")
        self.assertFalse(self.policy.transcript_accepted("旧轮语音", item_id="old"))
        self.assertFalse(self.policy.permits_audio_response(epoch))

    def test_tts_generation_and_browser_callback_do_not_open_followup(self):
        self.wake()
        self.authorize()
        self.assertFalse(self.policy.speaker_drained(verified=False))
        self.assertEqual(self.policy.state, "ACTIVE")

    def test_speaker_drained_opens_followup_for_eight_seconds(self):
        self.wake()
        self.authorize()
        epoch = self.policy.epoch
        self.clock.advance(11)
        self.assertTrue(self.policy.speaker_drained(verified=True))
        self.assertEqual(self.policy.state, "FOLLOWUP")
        self.assertFalse(self.policy.permits_audio_response(epoch))
        self.clock.advance(7.9)
        self.assertTrue(self.policy.permits_pcm())
        self.clock.advance(.2)
        self.assertFalse(self.policy.permits_pcm())

    def test_followup_speech_before_deadline_is_not_cut_off(self):
        self.wake()
        self.authorize()
        previous_epoch = self.policy.epoch
        self.policy.speaker_drained(verified=True)
        self.clock.advance(7.99)
        self.policy.speech_started("followup")
        self.clock.advance(12)
        self.assertTrue(self.policy.speech_stopped(item_id="followup"))
        self.assertTrue(self.policy.transcript_accepted("还有一个问题", item_id="followup"))
        self.assertNotEqual(self.policy.epoch, previous_epoch)
        self.assertFalse(self.policy.permits_audio_response(previous_epoch))
        self.assertTrue(self.policy.permits_audio_response(self.policy.epoch))

    def test_invalid_followup_cannot_keep_robot_awake_forever(self):
        self.wake()
        self.authorize()
        self.policy.speaker_drained(verified=True)
        self.clock.advance(7.8)
        self.policy.speech_started("noise")
        self.clock.advance(1)
        self.policy.speech_stopped(item_id="noise", invalid=True)
        self.assertEqual(self.policy.state, "SLEEP")

    def test_side_talk_restarts_the_window_up_to_a_limit(self):
        policy = DialoguePresence(clock=self.clock, followup_ms=8000, side_talk_listen_ms=20000)
        policy.wake_detected()
        policy.speech_started("u")
        policy.speech_stopped(item_id="u")
        self.assertTrue(policy.transcript_accepted("今天天气怎么样", item_id="u"))
        policy.speaker_drained(verified=True)
        self.clock.advance(7)
        policy.speech_started("side")
        self.clock.advance(5)  # people talk past the old 8 s deadline
        policy.speech_stopped(item_id="side")
        self.assertTrue(policy.side_talk(item_id="side"))
        self.assertEqual(policy.state, "FOLLOWUP")
        self.clock.advance(7.9)
        self.assertTrue(policy.permits_pcm())  # a fresh 8 s from the side talk
        policy.speech_started("side2")
        self.clock.advance(1)
        policy.speech_stopped(item_id="side2")
        policy.side_talk(item_id="side2")
        self.clock.advance(0.2)  # 21.1 s after the window opened: past the 20 s cap
        self.assertEqual(policy.state, "SLEEP")

    def test_close_revokes_late_response_and_reconnect(self):
        self.wake()
        self.authorize()
        epoch = self.policy.epoch
        self.policy.close()
        self.assertFalse(self.policy.permits_audio_response(epoch))
        new_epoch = self.wake()
        self.assertNotEqual(epoch, new_epoch)
        self.assertFalse(self.policy.permits_audio_response(epoch))

    def test_two_robots_independent(self):
        other = DialoguePresence(clock=self.clock)
        self.wake()
        self.authorize()
        self.assertFalse(other.permits_pcm())
        self.assertEqual(other.state, "SLEEP")
        self.assertFalse(other.permits_audio_response(self.policy.epoch))

    def test_invalid_configuration_rejected(self):
        for kwargs in ({"wake_wait_ms": False}, {"wake_wait_ms": 0},
                       {"followup_ms": 40000}):
            with self.assertRaises(ValueError):
                DialoguePresence(**kwargs)


if __name__ == "__main__":
    unittest.main()
