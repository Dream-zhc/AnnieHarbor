"""Self-hosted d3-NVFP4 answers decision questions first; DashScope is the fallback.

User decision 2026-10-21: d3 for "is this for her?" and spontaneous faces while
the pay-as-you-go cloud box answers, Aliyun otherwise; the workbench shows
which one is in use.
"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from annie_adapters.decision import D3_MODEL, DecisionClient, error_kind, gateway_url
from annie_brain.decision_policy import (
    ADDRESSEE_FOLLOWUP_QUESTION,
    ADDRESSEE_QUESTION,
    addressee_verdict,
)
from annie_core import RobotConfig
from annie_runtime import HarborRuntime
from annie_runtime.__main__ import d3_options
from annie_runtime.decision_route import D3Route

from tests.test_decision_model import EchoSocket, FakeDecision
from tests.test_live_issues_1020 import live

D3_BASE = "http://d3.example.com:8100"


# ---- adapter ---------------------------------------------------------------------
def test_d3_gateway_address_is_http_or_https_without_credentials():
    assert gateway_url(D3_BASE) == D3_BASE + "/v1/systemone"
    assert gateway_url(D3_BASE + "/") == D3_BASE + "/v1/systemone"
    assert gateway_url(D3_BASE + "/v1/systemone") == D3_BASE + "/v1/systemone"
    assert gateway_url("https://d3.example.com") == "https://d3.example.com/v1/systemone"
    for bad in ("ftp://d3.example.com", "d3.example.com:8100", "http://:8100",
                "http://user:pw@d3.example.com", D3_BASE + "?token=1", D3_BASE + "#x",
                D3_BASE + "/compatible-mode/v1/systemone", D3_BASE + "/v1/chat",
                "http://d3.example.com:port"):
        with pytest.raises(ValueError):
            gateway_url(bad)
    # The DashScope client keeps its strict https rule; a d3 address is not one.
    with pytest.raises(ValueError):
        DecisionClient(D3_BASE + "/v1/systemone", "k")
    with pytest.raises(ValueError):
        DecisionClient(D3_BASE, "", gateway=True)


def test_d3_client_posts_systemone_with_bearer_and_checks_health():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("Authorization"),
                     request.extensions.get("timeout", {}).get("read")))
        if request.url.path == "/health":
            return httpx.Response(200)
        body = json.loads(request.content)
        assert body["model"] == D3_MODEL
        return httpx.Response(200, json={"answers": {
            key: {"choice": "robot", "confidence": .8} for key in body["questions"]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                     headers={"Authorization": "Bearer t"}) as http:
            client = DecisionClient(D3_BASE, "t", model=D3_MODEL, client=http, gateway=True)
            await client.health()
            await client.warm(timeout=40)
            answers = await client.ask({"当前这句话": "那明天呢？"},
                                       {"to": ADDRESSEE_FOLLOWUP_QUESTION})
            assert answers["to"]["choice"] == "robot"
    asyncio.run(run())
    assert [(m, p) for m, p, _a, _t in seen] == [
        ("GET", "/health"), ("POST", "/v1/systemone"), ("POST", "/v1/systemone")]
    assert all(auth == "Bearer t" for _m, _p, auth, _t in seen)
    assert seen[1][3] == 40 and seen[2][3] == 1.5  # only the warm question waits long


def test_health_tolerates_a_missing_endpoint_but_not_an_unhealthy_one():
    status = {"code": 404}

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(status["code"]))
        async with httpx.AsyncClient(transport=transport) as http:
            client = DecisionClient(D3_BASE, "t", client=http, gateway=True)
            await client.health()  # no /health: the warm question decides
            status["code"] = 503
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await client.health()
            assert error_kind(caught.value) == "HTTP 503"  # no URL in logs
    asyncio.run(run())


def test_the_follow_up_question_names_three_concrete_options():
    assert ADDRESSEE_FOLLOWUP_QUESTION["type"] == "choice"
    assert set(ADDRESSEE_FOLLOWUP_QUESTION["criteria"]) == {"robot", "person", "self_or_media"}
    assert addressee_verdict({"choice": "robot", "confidence": .38}) == "robot"
    assert addressee_verdict({"choice": "person", "confidence": .6}) == "not_robot"
    assert addressee_verdict({"choice": "self_or_media", "confidence": .5}) == "not_robot"


# ---- probe -----------------------------------------------------------------------
class FakeD3(FakeDecision):
    model = D3_MODEL

    def __init__(self, *answers, fail=False, healthy=True):
        super().__init__(*answers, fail=fail)
        self.healthy, self.warm_timeouts = healthy, []

    async def health(self):
        if not self.healthy:
            raise httpx.ConnectError("down")

    async def warm(self, *, timeout=None):
        self.warm_timeouts.append(timeout)


def test_probe_marks_d3_ready_only_after_health_and_warm_and_a_failure_demotes_it():
    async def run():
        d3 = FakeD3(healthy=False)
        route = D3Route(d3, model=D3_MODEL)
        assert route.snapshot()["status"] == "probing" and not route.ready
        await route.probe()
        assert not route.ready and route.status == "down" and route.error == "ConnectError"
        assert d3.warm_timeouts == []  # an unhealthy box is not asked
        d3.healthy = True
        await route.probe()
        assert route.ready and route.error is None and d3.warm_timeouts == [40]
        route.failed("ReadTimeout")
        assert not route.ready and route.snapshot()["failures"] == 1
        await route.probe()
        assert route.ready
        snapshot = json.dumps(route.snapshot())
        assert "example" not in snapshot and "Bearer" not in snapshot
        route.start()
        await asyncio.sleep(0)
        await route.close()
        assert route._task is None
    asyncio.run(run())


# ---- runtime ---------------------------------------------------------------------
class Judge:
    model = "qwen-flash"

    def __init__(self):
        self.calls = []

    async def judge(self, system, user):
        self.calls.append(system)
        return "other", .7


def ready_route(*answers, fail=False):
    route = D3Route(FakeD3(*answers, fail=fail), model=D3_MODEL)
    route._mark(True, None)
    return route


def test_d3_answers_first_with_the_follow_up_question_and_an_outage_falls_back(tmp_path):
    async def body(_rt, client):
        judge = Judge()
        client.addressee_judge, client.addressee_enabled = judge, True
        client.d3 = ready_route({"choice": "person", "confidence": .6},
                                {"choice": "robot", "confidence": .4})
        client.wake = SimpleNamespace(state="FOLLOWUP")
        assert await client._decide_addressee("他没接视觉吧", "i1") == "not_robot"
        state, questions = client.d3.client.calls[-1]
        assert questions == {"to": ADDRESSEE_FOLLOWUP_QUESTION}
        assert state["当前这句话"] == "他没接视觉吧" and judge.calls == []
        decided = [e for e in client.events if e["kind"] == "addressee.decided"][-1]
        assert decided["judge"] == "d3" and decided["model"] == D3_MODEL
        assert client._addressee_judge() == ("d3", D3_MODEL)
        # Not waiting for an answer: d3 gets the general question.
        client.wake = None
        assert await client._decide_addressee("那明天呢？", "i2") == "robot"
        assert client.d3.client.calls[-1][1] == {"to": ADDRESSEE_QUESTION}
        # An outage answers her and hands later questions to qwen-flash.
        client.wake = SimpleNamespace(state="FOLLOWUP")
        client.d3.client.fail = True
        assert await client._decide_addressee("你那个激活了吗？", "i3") == "robot"
        failed = [e for e in client.events if e["kind"] == "addressee.decided"][-1]
        assert failed["error"] == "ConnectError" and failed["judge"] == "d3"
        assert not client.d3.ready and client.d3.error == "ConnectError"
        assert await client._decide_addressee("好嘞好，可以好。", "i4") == "not_robot"
        assert len(judge.calls) == 1
        client.wake = None
        decision = client.snapshot()["decision"]
        assert decision["addressee_judge"] == "llm" and decision["addressee_model"] == "qwen-flash"
        assert decision["d3"]["status"] == "down"
    live(tmp_path, body)


def test_d3_picks_faces_at_its_own_threshold_and_dashscope_takes_over_when_down(tmp_path):
    async def body(rt, client):
        client.decision = FakeDecision({"choice": "smirk", "confidence": .6})
        client.d3 = ready_route({"choice": "smirk", "confidence": .65},
                                {"choice": "proud", "confidence": .75})

        async def face(text):
            await client._auto_expression("t1", {}, rt.robots["a"].generation, text,
                                          "我脑子转得快。", ["smirk", "proud"])
            return [e for e in client.events if e["kind"] == "action.auto_decided"][-1]

        low = await face("你还挺幽默")
        assert low["model"] == D3_MODEL and low["choice"] == "smirk" and low["script"] is None
        high = await face("你真聪明")
        assert high["script"] == "proud" and client.decision.calls == []
        client.d3.client.fail = True
        await client._auto_expression("t1", {}, rt.robots["a"].generation, "哈哈",
                                      "哈哈。", ["smirk"])
        failed = [e for e in client.events if e["kind"] == "action.auto_failed"][-1]
        assert failed["error"] == "ConnectError" and not client.d3.ready
        fallback = await face("你还挺幽默")
        assert fallback["model"] == "decision" and fallback["script"] == "smirk"  # 0.6 >= 0.55
        assert client.snapshot()["decision"]["expression_model"] in {None, "decision"}
    live(tmp_path, body)


def test_without_d3_everything_stays_as_before(tmp_path):
    rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "d.db",
                       realtime_key="fake", realtime_socket_factory=EchoSocket,
                       decision_client=FakeDecision(), addressee_enabled=True,
                       auto_expression_enabled=True)
    client = rt.realtime["a"]
    assert rt.d3 is None and client.d3 is None
    decision = client.snapshot()["decision"]
    assert decision["d3"] is None and decision["addressee_judge"] == "decision"
    assert decision["auto_expression"] is True


def test_runtime_builds_the_d3_route_from_config_and_requires_a_token(tmp_path):
    common = dict(realtime_key="fake", realtime_socket_factory=EchoSocket,
                  addressee_enabled=True, auto_expression_enabled=True)
    rt = HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "a.db",
                       d3_url=D3_BASE, d3_key="t", **common)
    client = rt.realtime["a"]
    assert client.d3 is rt.d3 and rt.d3.model == D3_MODEL and rt.d3.expression_threshold == .7
    assert rt.d3.client.url == D3_BASE + "/v1/systemone"
    assert rt.d3.client._client.trust_env is False  # no shell proxy on the d3 path
    assert client.addressee_enabled and client.auto_expression_enabled  # d3 alone is enough
    assert client.snapshot()["decision"]["d3"]["status"] == "probing"
    with pytest.raises(ValueError):
        HarborRuntime([RobotConfig("a", room_id="roomv2")], tmp_path / "b.db",
                      d3_url=D3_BASE, **common)


def test_prefer_in_config_decides_whether_d3_is_contacted():
    env = {"ANNIE_D3_URL": D3_BASE, "ANNIE_D3_TOKEN": "t"}
    on = d3_options({"prefer": "d3", "d3": {"expression_threshold": .7}}, env)
    assert on["d3_url"] == D3_BASE and on["d3_key"] == "t" and on["d3_expression_threshold"] == .7
    assert d3_options({"prefer": "dashscope"}, env)["d3_url"] is None
    assert d3_options({}, env)["d3_url"] is None  # no [decision] table: as before
    assert d3_options({"prefer": "d3"}, {})["d3_url"] is None  # not configured: as before
    with pytest.raises(ValueError):
        d3_options({"prefer": "qwen"}, env)
