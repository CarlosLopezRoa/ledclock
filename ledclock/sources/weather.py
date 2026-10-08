"""Open-Meteo client: free, keyless, and generous enough for a 32x32 panel.

The panel redraws every few seconds but the API must not be polled at that
rate, so every fetch is cached on disk. A failed fetch falls back to the last
good payload and flags it stale rather than blanking the weather pages.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CACHE_DIR = Path.home() / ".cache" / "ledclock"
DEFAULT_TTL = 600.0  # seconds

#: How much hourly data to keep either side of now. Fetched generously so the
#: trend page can widen its window without changing the request.
HOURLY_PAST = 24
HOURLY_AHEAD = 24


@dataclass(frozen=True)
class Current:
    temperature: float
    apparent: float
    code: int
    is_day: bool
    humidity: int | None = None
    wind: float | None = None
    gusts: float | None = None
    pressure: float | None = None
    clouds: int | None = None
    uv: float | None = None
    precipitation: float | None = None


@dataclass(frozen=True)
class Hour:
    """One point on the hourly series, either side of now."""

    time: datetime
    temperature: float
    apparent: float
    precipitation_probability: int | None = None
    code: int | None = None
    gusts: float | None = None
    precipitation: float | None = None


@dataclass(frozen=True)
class Day:
    day: date
    code: int
    high: float
    low: float
    precipitation_probability: int | None = None
    uv_max: float | None = None


@dataclass(frozen=True)
class Weather:
    current: Current
    days: list[Day]
    fetched_at: float
    stale: bool = False
    #: Hourly series spanning HOURLY_PAST..HOURLY_AHEAD around now.
    hours: list[Hour] = field(default_factory=list)

    @property
    def age(self) -> float:
        return time.time() - self.fetched_at


def _cache_path(latitude: float, longitude: float, unit: str) -> Path:
    """One cache file per kilometre or so, not per eleven metres.

    The coordinates follow iStat's last known location, which is Wi-Fi
    geolocation and wanders by tens of metres between updates. Keyed at four
    decimals, nearly every update named a new file — 44 of them after six
    weeks at one address — and a failed fetch then fell back to whichever file
    happened to match the current jitter, once one written thirteen days
    earlier. Open-Meteo's grid is a kilometre or more, so two decimals loses
    nothing about the weather and keeps one house in one file.
    """
    return CACHE_DIR / f"weather_{latitude:.2f}_{longitude:.2f}_{unit}.json"


def _optional(series: list, index: int):
    """Open-Meteo omits whole series when a field is unavailable for a site."""
    if index < len(series) and series[index] is not None:
        return series[index]
    return None


def _parse_hours(payload: dict) -> list[Hour]:
    """The hourly series, made timezone-aware.

    Open-Meteo returns local wall-clock strings with no offset in them, plus
    the offset separately — so attach it rather than assuming the Mac and the
    forecast location share a zone.
    """
    hourly = payload.get("hourly") or {}
    times = hourly.get("time", [])
    if not times:
        return []

    tz = timezone(timedelta(seconds=int(payload.get("utc_offset_seconds", 0))))
    temperatures = hourly.get("temperature_2m", [])
    apparent = hourly.get("apparent_temperature", [])
    probability = hourly.get("precipitation_probability", [])
    codes = hourly.get("weather_code", [])
    gusts = hourly.get("wind_gusts_10m", [])
    precipitation = hourly.get("precipitation", [])

    hours: list[Hour] = []
    for index, stamp in enumerate(times):
        temperature = _optional(temperatures, index)
        if temperature is None:
            continue
        try:
            moment = datetime.fromisoformat(stamp).replace(tzinfo=tz)
        except ValueError:
            continue
        code = _optional(codes, index)
        hours.append(
            Hour(
                time=moment,
                temperature=float(temperature),
                apparent=float(_optional(apparent, index) or temperature),
                precipitation_probability=_optional(probability, index),
                code=None if code is None else int(code),
                gusts=_optional(gusts, index),
                precipitation=_optional(precipitation, index),
            )
        )
    return hours


def _parse(payload: dict, fetched_at: float, stale: bool) -> Weather:
    current = payload["current"]
    daily = payload.get("daily", {})
    times = daily.get("time", [])
    probabilities = daily.get("precipitation_probability_max", [])
    uv_maxima = daily.get("uv_index_max", [])

    days = []
    for index, day in enumerate(times):
        try:
            days.append(
                Day(
                    day=date.fromisoformat(day),
                    code=int(daily["weather_code"][index]),
                    high=float(daily["temperature_2m_max"][index]),
                    low=float(daily["temperature_2m_min"][index]),
                    precipitation_probability=_optional(probabilities, index),
                    uv_max=_optional(uv_maxima, index),
                )
            )
        except (KeyError, IndexError, TypeError, ValueError):
            continue

    return Weather(
        current=Current(
            temperature=float(current["temperature_2m"]),
            apparent=float(current.get("apparent_temperature", current["temperature_2m"])),
            code=int(current.get("weather_code", 3)),
            is_day=bool(current.get("is_day", 1)),
            humidity=current.get("relative_humidity_2m"),
            wind=current.get("wind_speed_10m"),
            gusts=current.get("wind_gusts_10m"),
            pressure=current.get("surface_pressure") or current.get("pressure_msl"),
            clouds=current.get("cloud_cover"),
            uv=current.get("uv_index"),
            precipitation=current.get("precipitation"),
        ),
        days=days,
        fetched_at=fetched_at,
        stale=stale,
        hours=_parse_hours(payload),
    )


def _read_cache(path: Path) -> tuple[dict, float] | None:
    try:
        blob = json.loads(path.read_text())
        return blob["payload"], float(blob["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None


def _write_cache(path: Path, payload: dict, fetched_at: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": fetched_at, "payload": payload}))
    except OSError as exc:
        log.warning("could not write weather cache: %s", exc)


async def fetch(
    latitude: float,
    longitude: float,
    *,
    unit: str = "celsius",
    forecast_days: int = 4,
    ttl: float = DEFAULT_TTL,
    timeout: float = 10.0,
) -> Weather:
    """Current conditions plus a short forecast, cached for `ttl` seconds."""
    path = _cache_path(latitude, longitude, unit)
    cached = _read_cache(path)
    if cached and time.time() - cached[1] < ttl:
        return _parse(cached[0], cached[1], stale=False)

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,apparent_temperature,weather_code,is_day,"
                   "relative_humidity_2m,wind_speed_10m,wind_gusts_10m,"
                   "surface_pressure,cloud_cover,precipitation",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,"
                  "weather_code,wind_gusts_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,uv_index_max",
        "timezone": "auto",
        "forecast_days": forecast_days,
        "past_hours": HOURLY_PAST,
        "forecast_hours": HOURLY_AHEAD,
        "temperature_unit": unit,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(ENDPOINT, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        if cached:
            log.warning("weather fetch failed (%s); serving cache from %.0fs ago",
                        exc, time.time() - cached[1])
            return _parse(cached[0], cached[1], stale=True)
        raise

    now = time.time()
    _write_cache(path, payload, now)
    return _parse(payload, now, stale=False)


def format_temperature(value: float) -> str:
    """Round a point reading to a whole degree — no room for decimals."""
    return f"{round(value):d}"


def format_high(value: float) -> str:
    """Round a maximum *up*.

    A displayed bound has to actually bound. Rounding to nearest let a peak
    of 34.4 print as 34 while the current reading of 34.6 printed as 35 —
    "max 34, now 35" on the same screen, which reads as a bug because it is
    one. Ceiling the top and flooring the bottom makes the pair honest at the
    cost of at most a degree of slack.
    """
    return f"{math.ceil(value):d}"


def format_low(value: float) -> str:
    """Round a minimum *down*. See format_high."""
    return f"{math.floor(value):d}"
