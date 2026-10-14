import time

import pytest

from annie_runtime.language import ReplyLanguageState, explicit_language_request

IDENTITY = dict(robot_id="a", room_id="r", chat_id="c", persona_id="p", generation=1,
                session_epoch=1)


def observation(language="en", **overrides):
    return {**IDENTITY, "language": language, "emotion": "neutral", "text": "hello there",
            "dialect": "unknown", "observed_monotonic_ms": time.monotonic()*1000,
            **overrides}


def state():
    result = ReplyLanguageState()
    result.reset(IDENTITY)
    result.load_preferences({"robot": {"language": "zh", "dialect": "mandarin"},
                             "chat": {"language": "zh", "dialect": "sichuan"}})
    return result


def test_first_follow_overrides_defaults_once_without_persisting():
    s = state()
    assert s.observe(observation())
    assert s.selection()["language"] == "en"
    s.confirm(s.revision)
    s.response_started()
    s.first_text("t1")
    assert s.style_for("t1").language == "en"
    s.response_finished()
    assert s.preferences["chat"]["dialect"] == "sichuan"
    assert not s.first_pending
    assert s.observe(observation())
    assert not s.first_pending


def test_late_observation_does_not_rewrite_response_style():
    s = state()
    s.confirm(s.revision)
    s.response_started()
    assert s.observe(observation())
    s.first_text("t1")
    assert s.style_for("t1").dialect == "sichuan"
    assert s.snapshot()["application"] == "next_turn"
    s.response_finished()
    s.confirm(s.revision)
    s.response_started()
    s.first_text("t2")
    assert s.style_for("t2").language == "en"


def test_unconfirmed_candidate_never_claims_current_turn():
    s = state()
    s.observe(observation())
    s.response_started()
    s.first_text("t")
    assert s.style_for("t").dialect == "sichuan"
    assert s.snapshot()["application"] in {"unknown", "next_turn"}
    assert s.first_pending


def test_no_stash_inference_expired_or_cross_identity():
    s = state()
    assert not s.observe(observation(text="", stash="河南话", language="zh"))
    assert not s.observe(observation(chat_id="other"))
    assert not s.observe(observation(observed_monotonic_ms=0))
    assert not s.observe(observation(text="嗯", language="zh"))
    s.observe(observation(text="我们去河南旅游吧", language="zh"))
    assert s.snapshot()["input"]["dialect"] == "unknown"
    assert s.snapshot()["input"].get("dialect_candidate") is None


def test_explicit_current_scope_and_negation_validation():
    assert explicit_language_request("以后你都用英语")["scope"] == "robot"
    assert explicit_language_request("本Chat以后用四川话")["scope"] == "chat"
    assert explicit_language_request("这句请用河南话")["scope"] == "turn"
    assert explicit_language_request("我去四川旅游") is None
    assert explicit_language_request("不要用河南话") is None
    assert explicit_language_request("你会说四川话吗") is None
    assert explicit_language_request("请翻译‘以后你都用英语’") is None
    s = state()
    s.user_request("这句请用河南话，语速慢一点")
    s.response_started()
    s.first_text("t")
    assert s.style_for("t").dialect == "henan"
    assert s.style_for("t").rate < 1
    assert s.style_for("t").source == "explicit_user"


def test_reset_keeps_loaded_preferences_but_retires_transient_and_styles():
    s = state()
    s.observe(observation())
    s.reset({**IDENTITY, "generation": 2})
    assert s.snapshot()["input"] is None
    assert s.selection()["dialect"] == "sichuan"
    assert not s.first_pending


def test_llm_inference_needs_current_stable_evidence_and_cannot_persist():
    s = state()
    s.observe(observation(language="zh", text="今天吃啥，中不中？"))
    s.reply_intent(language="zh", dialect="henan", evidence="中不中", emotion="playful")
    assert s.snapshot()["input"]["dialect_candidate"] == "henan"
    assert s.preferences["chat"]["dialect"] == "sichuan"
    assert s.selection()["source"] == "llm_inferred"


def test_five_zh_observations_do_not_rearm_consumed_dialect():
    s = state()
    for index in range(5):
        s.observe(observation(language='zh', text='中不中，咱们去看看'))
        s.reply_intent(language='zh', dialect='henan', evidence='中不中', emotion='playful')
        if index:
            assert not s.first_pending
        s.confirm(s.revision)
        s.response_started()
        s.first_text(str(index))
        s.response_finished()


def test_unchanged_hint_preserves_confirmation_between_responses():
    s = state()
    s.confirm(s.revision)
    revision = s.revision
    for index in range(5):
        s.response_started()
        assert s.application == 'current'
        s.first_text(str(index))
        s.response_finished()
        assert s.revision == revision


def test_stable_prefix_request_is_revoked_by_final_negation():
    s = state()
    assert s.user_request('这句请用河南话')
    assert s.explicit['dialect'] == 'henan'
    assert s.user_request('这句请用河南话，不，别用河南话')
    assert s.explicit is None
    assert s.selection()['dialect'] == 'sichuan'


def test_unconsumed_candidate_is_not_lost_by_language_jitter():
    s = state()
    s.observe(observation(language='en'))
    s.observe(observation(language='zh'))
    s.observe(observation(language='en'))
    assert s.first_pending


def test_emotion_can_arrive_after_first_text_before_run_task_only():
    s = state()
    s.response_started()
    s.first_text('t')
    s.reply_intent(language='en',emotion='playful')
    assert s.style_for('t').emotion == 'playful'
    assert s.style_for('t').language == 'zh'
    s.locked_turns.add('t')
    s.reply_intent(language='en',emotion='serious')
    assert s.style_for('t').emotion == 'playful'


def test_serious_user_context_suppresses_sarcastic_intent():
    s = state()
    s.user_request('我很难过，想求助')
    s.response_started()
    s.first_text('t')
    s.reply_intent(language='zh',emotion='sarcastic')
    assert s.style_for('t').emotion in {'calm','serious'}


def test_without_the_sidecar_the_asr_final_is_dialect_evidence():
    s = state()  # no acoustic observation: ANNIE_ACOUSTIC_SIDECAR_ENABLED=0 by default
    with pytest.raises(ValueError):
        s.reply_intent(language="zh", dialect="sichuan", evidence="巴适", emotion="playful",
                       transcript="今天天气好得很")
    s.reply_intent(language="zh", dialect="sichuan", evidence="巴适", emotion="playful",
                   transcript="今天耍得好巴适哦")
    s.confirm(s.revision)
    s.response_started()
    s.first_text("t1")
    assert s.style_for("t1").dialect == "sichuan"
    assert "四川话" in s.style_for("t1").instruction
    assert s.preferences["chat"]["dialect"] == "sichuan"  # nothing persisted


@pytest.mark.parametrize(("text", "expected"), [
    ("今天天气巴适得很，我们去耍嘛", ("zh", "sichuan", "巴适")),
    ("中不中？咱今儿个去吃烩面", ("zh", "henan", "中不中")),
    ("今日天气好好，我哋去饮茶啦", ("yue", "cantonese", "哋")),
    ("你觉得点样？", ("yue", "cantonese", "点样")),  # Qwen ASR writes Cantonese simplified
    ("额们今天去哪儿", ("zh", "shaanxi", "额们")),
    ("我一定要得到它", None), ("这里很安逸", None), ("额，我想想", None),
    ("唔，好吧", None), ("有一点样子了", None), ("你翻译一下巴适是什么意思", None),
])
def test_only_dialect_only_words_count(text, expected):
    from annie_runtime.language import dialect_marker
    assert dialect_marker(text) == expected


def plain_state():
    result = ReplyLanguageState()
    result.reset(IDENTITY)
    return result


def test_a_dialect_heard_in_the_asr_text_is_followed_until_mandarin_is_asked():
    s = plain_state()
    s.user_request("今天天气巴适得很")
    # Unconfirmed prompt: the voice still follows this utterance's own words.
    s.response_started()
    s.first_text("t1")
    assert s.style_for("t1").dialect == "sichuan"
    assert "四川话" in s.style_for("t1").instruction
    assert "巴适" in s.instruction_hint()
    s.response_finished()
    s.user_request("那明天呢")  # no marker: the conversation stays in Sichuanese
    s.confirm(s.revision)
    s.response_started()
    s.first_text("t2")
    assert s.style_for("t2").dialect == "sichuan"
    s.response_finished()
    s.user_request("你说普通话吧")
    s.confirm(s.revision)
    s.response_started()
    s.first_text("t3")
    assert s.style_for("t3").dialect != "sichuan"
    assert s.snapshot()["asr_dialect"] is None


def test_saved_preferences_keep_only_the_first_follow():
    s = state()  # chat preference: Sichuanese
    s.user_request("中不中？咱去吃烩面")
    s.response_started()
    s.first_text("t1")
    assert s.style_for("t1").dialect == "henan"  # first follow overrides once
    s.response_finished()
    s.user_request("中不中嘛")
    s.confirm(s.revision)
    s.response_started()
    s.first_text("t2")
    assert s.style_for("t2").dialect == "sichuan"  # back to the saved choice
    assert s.preferences["chat"]["dialect"] == "sichuan"
