import time

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
