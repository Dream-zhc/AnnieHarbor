"""8C Qwen compatible-mode wire contract; no billing or hardware required."""

import asyncio
import json

import httpx
import pytest
from annie_adapters.idle import QwenIdleModel
from annie_brain.idle_plan import build_idle_graph


def test_max_snapshot_payload_is_anonymous_bounded_and_structured():
    async def run():
        requests = []

        def respond(request):
            requests.append(request)
            assert request.url.path.endswith("/chat/completions")
            assert request.headers["authorization"] == "Bearer fake"
            payload = json.loads(request.content)
            assert payload["model"] == "qwen3.8-max-0902"
            assert payload["enable_thinking"] is False
            assert payload["response_format"] == {"type": "json_object"}
            assert payload["max_tokens"] <= 180
            user = json.loads(payload["messages"][1]["content"])
            assert user == {"attempt": 2, "scene": "现场有一杯茶。", "previous": "聊书"}
            assert "我的账户" not in str(payload)
            return httpx.Response(200, json={"choices": [{"message": {"content":
                '{"decision":"SPEAK","topic":"聊喝茶","new_value":true}'}}]})

        model = QwenIdleModel("fake", "https://example.test/v1", transport=
                              httpx.MockTransport(respond))
        try:
            graph = build_idle_graph(model)
            result = await graph.ainvoke({"attempt": 2, "scene": "现场有一杯茶。",
                                          "previous": "聊书"})
            assert result["new_value"] is True
            assert result["topic"] == "聊喝茶"
            assert len(requests) == 1
        finally:
            await model.close()

    asyncio.run(run())


def test_planning_thinking_mode_follows_config_without_changing_anonymous_input():
    async def run():
        def respond(request):
            payload = json.loads(request.content)
            assert payload["enable_thinking"] is True
            assert payload["reasoning_effort"] == "low"
            assert payload["response_format"] == {"type": "json_object"}
            assert "聊天" not in payload["messages"][1]["content"]
            return httpx.Response(200, json={"choices": [{"message": {
                "reasoning_content": "内部推理不进入话题",
                "content": '{"decision":"NO_SPEAK","topic":"","new_value":false}',
            }}]})

        model = QwenIdleModel("fake", "https://example.test/v1",
                              enable_thinking=True, reasoning_effort="low",
                              transport=httpx.MockTransport(respond))
        try:
            result = await model.plan({"attempt": 1, "scene": "有人", "previous": ""})
            assert result == {"decision": "NO_SPEAK", "topic": "", "new_value": False}
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize("content", [
    "not-json", "[]", "{}",
    '{"decision":"SPEAK","topic":"","new_value":true}',
    '{"decision":"SPEAK","topic":"hello","new_value":"yes"}',
    '{"decision":"MAYBE","topic":"hello","new_value":true}',
    '{"decision":"SPEAK","topic":"' + "大" * 71 + '","new_value":true}',
])
def test_bad_provider_result_is_fail_closed(content):
    async def run():
        def respond(_request):
            return httpx.Response(200, json={"choices": [{"message": {
                "content": content}}]})

        model = QwenIdleModel("fake", "https://example.test/v1",
                              transport=httpx.MockTransport(respond))
        try:
            with pytest.raises((ValueError, TypeError)):
                await model.plan({"attempt": 1, "scene": "有人", "previous": ""})
        finally:
            await model.close()

    asyncio.run(run())


def test_provider_raises_on_403_and_no_retry():
    async def run():
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(403, json={"message": "no permissions"})

        model = QwenIdleModel("fake", "https://example.test/v1",
                              transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await model.plan({"attempt": 1, "scene": "有人", "previous": ""})
            assert len(calls) == 1
        finally:
            await model.close()

    asyncio.run(run())
