"""Weather must be accurate, bounded, and shared across concurrent heads."""

import asyncio

import httpx

from annie_adapters.weather import AmapWeather


def test_xian_not_confused_with_xian_district_and_shared_cache():
    async def run():
        requests = []

        async def handler(request):
            requests.append(request.url.params["city"])
            await asyncio.sleep(.03)
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "西安市", "adcode": "610100", "weather": "晴",
                "temperature": "25", "reporttime": "2026-09-24 14:00:00",
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tool = AmapWeather("test-key", client=client)
            a, b = await asyncio.gather(tool.current("西安"), tool.current("西安市"))
            assert requests == ["西安市"]
            assert a["adcode"] == b["adcode"] == "610100"
            assert a["cache_hit"] is b["cache_hit"] is False
            cached = await tool.current("西安")
            assert cached["cache_hit"] is True
            assert cached["cache_age_ms"] >= 0
            assert requests == ["西安市"]

    asyncio.run(run())


def test_never_return_weather_for_a_different_city():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "1", "lives": [{
                "city": "西安区", "adcode": "231005", "weather": "晴",
            }]})
        )) as client:
            tool = AmapWeather("test-key", client=client)
            result = await tool.current("西安")
            assert result == {"status": "unavailable", "source": "amap",
                              "reason": "city_mismatch"}

    asyncio.run(run())


def test_slow_provider_returns_timeout_without_a_fake_weather_report():
    async def run():
        async def slow(_request):
            await asyncio.sleep(.3)
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "西安市", "weather": "晴",
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            tool = AmapWeather("test-key", client=client, timeout_ms=20)
            result = await tool.current("西安")
            assert result == {"status": "unavailable", "source": "amap",
                              "reason": "timeout"}

    asyncio.run(run())


def test_cancelled_waiter_does_not_cancel_other_robot_weather():
    async def run():
        requests = []

        async def handler(request):
            requests.append(request.url.params["city"])
            await asyncio.sleep(.03)
            return httpx.Response(200, json={"status": "1", "lives": [{
                "city": "西安市", "adcode": "610100", "weather": "晴",
            }]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tool = AmapWeather("test-key", client=client)
            first = asyncio.create_task(tool.current("西安"))
            second = asyncio.create_task(tool.current("西安"))
            await asyncio.sleep(.005)
            first.cancel()
            result = await second
            assert result["status"] == "ok"
            assert requests == ["西安市"]
            try:
                await first
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
