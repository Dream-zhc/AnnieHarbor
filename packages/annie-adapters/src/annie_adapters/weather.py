"""Bounded, shared real Amap weather lookup; never confuse similarly named cities."""

import asyncio
import re
import time

import httpx


class AmapWeather:
    def __init__(self, key: str, *, client=None, timeout_ms=1200, cache_sec=180,
                 default_city=None):
        self.key = key
        # The robot's configured home city, used only when the user names none.
        self.default_city = self._city(default_city) if default_city else None
        self.timeout = timeout_ms / 1000
        self.cache_sec = cache_sec
        self.client = client or httpx.AsyncClient(
            timeout=self.timeout,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )
        self.owns_client = client is None
        self.cache = {}
        self.inflight = {}

    @staticmethod
    def _city(city):
        if (not isinstance(city, str) or
                not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]{2,30}", city)):
            raise ValueError("city must be an explicit Chinese name or adcode")
        # Amap's bare '西安' resolves to Heilongjiang's 西安区, not 西安市.
        # Tool contract is city-level weather; preserve explicit districts/adcodes.
        return city + "市" if re.fullmatch(r"[\u4e00-\u9fff]{2,29}", city) and not city.endswith(
            ("市", "区", "县", "州", "盟", "旗")
        ) else city

    async def current(self, city):
        if not self.key:
            return {"status": "not_configured", "source": "amap"}
        city = self._city(city)
        now = time.monotonic()
        entry = self.cache.get(city)
        if entry and now - entry[0] < self.cache_sec:
            return {**entry[1], "cache_hit": True,
                    "cache_age_ms": round((now - entry[0]) * 1000)}
        task = self.inflight.get(city)
        if task is None:
            task = asyncio.create_task(self._fetch(city))
            self.inflight[city] = task

            def cleanup(done):
                if self.inflight.get(city) is done:
                    self.inflight.pop(city, None)

            task.add_done_callback(cleanup)
        # A cancelled robot turn must not cancel another robot's shared lookup.
        return dict(await asyncio.shield(task))

    async def _fetch(self, city):
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.client.get(
                    "https://restapi.amap.com/v3/weather/weatherInfo",
                    params={"key": self.key, "city": city, "extensions": "base"},
                )
            response.raise_for_status()
            content = response.json()
            if not isinstance(content, dict):
                return {"status": "unavailable", "source": "amap",
                        "reason": "invalid_response"}
            if str(content.get("status")) != "1":
                # Safe provider error code only. Never return the request URL,
                # which carries the API key in its query string.
                code = str(content.get("infocode") or "")
                return {"status": "unavailable", "source": "amap",
                        "reason": "provider_rejected",
                        "infocode": code if re.fullmatch(r"[0-9]{1,12}", code) else None}
            if not content.get("lives"):
                return {"status": "unavailable", "source": "amap",
                        "reason": "no_current_weather"}
            live = content["lives"][0]
            if not isinstance(live, dict) or not live.get("weather"):
                return {"status": "unavailable", "source": "amap",
                        "reason": "invalid_response"}
            if (city.isascii() and city.isdigit() and live.get("adcode") != city) or (
                not city.isascii() and
                str(live.get("city", "")).removesuffix("市") != city.removesuffix("市")
            ):
                return {"status": "unavailable", "source": "amap",
                        "reason": "city_mismatch"}
            result = {
                "status": "ok", "source": "amap",
                "city": live.get("city"), "adcode": live.get("adcode"),
                "weather": live.get("weather"), "temperature_c": live.get("temperature"),
                "humidity_percent": live.get("humidity"),
                "wind_direction": live.get("winddirection"),
                "wind_power": live.get("windpower"),
                "report_time": live.get("reporttime"),
                "cache_hit": False,
                "fetch_ms": round((time.monotonic() - started) * 1000),
            }
            if len(self.cache) >= 64:
                self.cache.pop(next(iter(self.cache)))
            self.cache[city] = (time.monotonic(), result)
            return result
        except (TimeoutError, httpx.TimeoutException):
            reason = "timeout"
        except httpx.HTTPStatusError as exc:
            reason = f"http_{exc.response.status_code}"
        except httpx.HTTPError:
            reason = "network_error"
        except (ValueError, KeyError, IndexError, TypeError):
            reason = "invalid_response"
        return {"status": "unavailable", "source": "amap", "reason": reason}

    async def close(self):
        for task in tuple(self.inflight.values()):
            task.cancel()
        if self.inflight:
            await asyncio.gather(*self.inflight.values(), return_exceptions=True)
        self.inflight.clear()
        if self.owns_client:
            await self.client.aclose()
