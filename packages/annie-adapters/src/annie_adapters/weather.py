"""One bounded real Amap current-weather query; no synthetic forecast or location."""

import re

import httpx


class AmapWeather:
    def __init__(self, key: str, *, client=None):
        self.key = key
        self.client = client or httpx.AsyncClient(
            timeout=3, limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )
        self.owns_client = client is None

    async def current(self, city):
        if not self.key:
            return {"status": "not_configured", "source": "amap"}
        if (not isinstance(city, str) or
                not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]{2,30}", city)):
            raise ValueError("city must be an explicit Chinese name or adcode")
        try:
            response = await self.client.get(
                "https://restapi.amap.com/v3/weather/weatherInfo",
                params={"key": self.key, "city": city, "extensions": "base"},
            )
            response.raise_for_status()
            content = response.json()
            if str(content.get("status")) != "1" or not content.get("lives"):
                raise ValueError("amap_weather_unavailable")
            live = content["lives"][0]
            return {
                "status": "ok", "source": "amap",
                "city": live.get("city"), "adcode": live.get("adcode"),
                "weather": live.get("weather"), "temperature_c": live.get("temperature"),
                "humidity_percent": live.get("humidity"),
                "wind_direction": live.get("winddirection"),
                "wind_power": live.get("windpower"),
                "report_time": live.get("reporttime"),
            }
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            return {"status": "unavailable", "source": "amap"}

    async def close(self):
        if self.owns_client:
            await self.client.aclose()
