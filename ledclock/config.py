"""Configuration loading. YAML wins; iStat Menus fills the gaps."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

import yaml

from .sources import istat

RGB = tuple[int, int, int]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

#: Cycled through when a clock entry does not name its own colour.
PALETTE: list[RGB] = [
    (255, 138, 66),
    (110, 176, 255),
    (255, 214, 92),
    (118, 226, 150),
    (226, 140, 255),
    (120, 232, 226),
]

DEFAULT_PAGES = ["world_clocks", "weather_now", "forecast"]

#: The four cells of the weather page's stat grid, read left-to-right,
#: top-to-bottom. Names must exist in ledclock.pages.STATS.
DEFAULT_STATS = ["humidity", "wind", "low", "high"]


class ConfigError(ValueError):
    """The config file says something we cannot act on."""


@dataclass(frozen=True)
class ClockCity:
    code: str
    name: str
    timezone: str
    color: RGB
    #: Used to work out sunrise and sunset for this city's row. Without them
    #: the row falls back to a fixed evening/morning hour.
    latitude: float | None = None
    longitude: float | None = None


#: What to leave the panel doing when the daemon lets go of it.
IDLE_MODES = ("clock", "hold", "off")


@dataclass(frozen=True)
class DeviceConfig:
    address: str | None = None
    brightness: int = 35
    ack_timeout: float = 8.0
    #: "clock" hands the display to the panel's own RTC-backed clock, which
    #: keeps running with nothing connected; "hold" leaves the last frame
    #: frozen; "off" blanks the panel.
    idle_mode: str = "clock"
    clock_style: int = 1
    clock_show_date: bool = False
    #: Also hand the panel over when the display sleeps, not only when the
    #: whole Mac does. macOS has been seen dropping Bluetooth *before* it
    #: announces system sleep, leaving no time to hand anything over; the
    #: screen goes dark minutes earlier, while the radio still works.
    handoff_on_screen_sleep: bool = True


@dataclass(frozen=True)
class DisplayConfig:
    dwell_seconds: float = 8.0
    pages: list[str] = field(default_factory=lambda: list(DEFAULT_PAGES))
    active_hours: tuple[time, time] | None = None
    #: Scales every pixel before the frame is encoded. The panel's own
    #: brightness stops at 1, which is still bright in a dark room; this goes
    #: below that, and continuously.
    gain: float = 1.0
    #: Absolute level for world-clock rows where it is night — *not* a factor
    #: applied on top of `gain`. Keeping the two independent is what stops a
    #: low gain from multiplying night rows down into invisibility.
    night_gain: float = 0.42
    #: "utc" sorts the world-clock rows west to east by live UTC offset;
    #: "config" keeps whatever order config.yaml lists.
    clock_order: str = "utc"
    #: Width of the `trend` page's window, in hours, centred on now.
    trend_hours: float = 24.0
    #: Squash the sun-path arc toward the horizon in winter, matching how high
    #: the sun actually gets. False draws a plain half-circle every day, where
    #: only the sun's speed along it changes with the season.
    sun_arc_seasonal: bool = False


@dataclass(frozen=True)
class WeatherConfig:
    #: Placeholder, not a suggestion: Greenwich, so an unconfigured install
    #: still returns real weather while making it obvious it is unset. Put
    #: your own coordinates in config.yaml, which is not tracked — beyond
    #: about three decimal places they identify a building.
    latitude: float = 51.4779
    longitude: float = -0.0015
    label: str = "HOME"
    unit: str = "celsius"
    follow_istat_location: bool = False
    refresh_seconds: float = 600.0
    stats: list[str] = field(default_factory=lambda: list(DEFAULT_STATS))

    def resolve_coordinates(self) -> tuple[float, float, str]:
        """Where to ask for weather, honouring follow_istat_location."""
        if self.follow_istat_location:
            fix = istat.last_known_location()
            if fix is not None:
                label = (fix.name or self.label).upper()
                return fix.latitude, fix.longitude, label
        return self.latitude, self.longitude, self.label.upper()


@dataclass(frozen=True)
class AlertConfig:
    enabled: bool = True
    #: How far ahead to look. Beyond about six hours a forecast is not
    #: specific enough to act on.
    lookahead_hours: float = 6.0
    rain_probability: int = 60
    gust_kmh: float = 55.0
    frost_celsius: float = 2.0
    #: Once raised, an alert stays up this long even if the reading dips back
    #: under the threshold. Stops a value sitting on the line from flickering.
    hold_minutes: float = 30.0


@dataclass(frozen=True)
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    clocks: list[ClockCity] = field(default_factory=list)
    timezone: str | None = None
    path: Path | None = None


def _idle_mode(raw) -> str:
    """Read device.idle_mode, forgiving YAML's boolean words.

    Unquoted `off` is parsed by YAML as False (as are `no` and `n`), so
    writing the obvious thing would otherwise fail validation with a baffling
    "got 'False'". Treat that as the word it was clearly meant to be.
    """
    if isinstance(raw, bool):
        if raw is False:
            return "off"
        raise ConfigError(
            "device.idle_mode was read as a boolean — YAML turns bare on/off "
            "and yes/no into booleans. Quote it: idle_mode: \"off\""
        )
    return str(raw)


def _parse_active_hours(raw: str | None) -> tuple[time, time] | None:
    if not raw:
        return None
    try:
        start_text, end_text = raw.split("-")
        start = time.fromisoformat(start_text.strip())
        end = time.fromisoformat(end_text.strip())
    except ValueError as exc:
        raise ConfigError(
            f"active_hours must look like '07:00-23:30', got {raw!r}"
        ) from exc
    return start, end


def is_active(window: tuple[time, time] | None, now: datetime) -> bool:
    """True when `now` falls inside the window, which may wrap past midnight."""
    if window is None:
        return True
    start, end = window
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _parse_color(raw, index: int) -> RGB:
    if raw is None:
        return PALETTE[index % len(PALETTE)]
    if isinstance(raw, str):
        text = raw.lstrip("#")
        if len(text) != 6:
            raise ConfigError(f"color must be #RRGGBB, got {raw!r}")
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    values = list(raw)
    if len(values) != 3:
        raise ConfigError(f"color must be three numbers, got {raw!r}")
    return (int(values[0]), int(values[1]), int(values[2]))


def _parse_clocks(raw: list | None) -> list[ClockCity]:
    if not raw:
        return []
    clocks: list[ClockCity] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"clocks[{index}] must be a mapping, got {entry!r}")
        tz = entry.get("tz") or entry.get("timezone")
        if not tz:
            raise ConfigError(f"clocks[{index}] is missing 'tz'")
        name = str(entry.get("name") or tz.split("/")[-1].replace("_", " "))
        code = str(entry.get("code") or istat.city_code(name)).upper()
        latitude = entry.get("lat", entry.get("latitude"))
        longitude = entry.get("lon", entry.get("longitude"))
        clocks.append(
            ClockCity(
                code=code,
                name=name.upper(),
                timezone=str(tz),
                color=_parse_color(entry.get("color"), index),
                latitude=None if latitude is None else float(latitude),
                longitude=None if longitude is None else float(longitude),
            )
        )
    return clocks


def clocks_from_istat(limit: int = 4) -> list[ClockCity]:
    """Bootstrap the world-clock rows from whatever iStat Menus has."""
    return [
        ClockCity(
            code=city.code,
            name=city.name.upper(),
            timezone=city.timezone,
            color=PALETTE[index % len(PALETTE)],
            latitude=city.latitude,
            longitude=city.longitude,
        )
        for index, city in enumerate(istat.read_cities()[:limit])
    ]


def load(path: Path | str | None = None) -> Config:
    """Read config.yaml, falling back to defaults for anything absent."""
    resolved = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict = {}
    if resolved.exists():
        raw = yaml.safe_load(resolved.read_text()) or {}
    elif path is not None:
        raise ConfigError(f"config file not found: {resolved}")

    device_raw = raw.get("device") or {}
    display_raw = raw.get("display") or {}
    weather_raw = raw.get("weather") or {}

    device = DeviceConfig(
        address=device_raw.get("address"),
        brightness=int(device_raw.get("brightness", DeviceConfig.brightness)),
        ack_timeout=float(device_raw.get("ack_timeout", DeviceConfig.ack_timeout)),
        idle_mode=_idle_mode(device_raw.get("idle_mode", DeviceConfig.idle_mode)),
        clock_style=int(device_raw.get("clock_style", DeviceConfig.clock_style)),
        clock_show_date=bool(
            device_raw.get("clock_show_date", DeviceConfig.clock_show_date)
        ),
        handoff_on_screen_sleep=bool(device_raw.get(
            "handoff_on_screen_sleep", DeviceConfig.handoff_on_screen_sleep
        )),
    )
    if device.idle_mode not in IDLE_MODES:
        raise ConfigError(
            f"device.idle_mode must be one of {', '.join(IDLE_MODES)}, "
            f"got {device.idle_mode!r}"
        )
    if not 1 <= device.clock_style <= 8:
        raise ConfigError("device.clock_style must be 1-8")
    display = DisplayConfig(
        dwell_seconds=float(display_raw.get("dwell_seconds", DisplayConfig.dwell_seconds)),
        pages=list(display_raw.get("pages") or DEFAULT_PAGES),
        active_hours=_parse_active_hours(display_raw.get("active_hours")),
        clock_order=str(display_raw.get("clock_order", DisplayConfig.clock_order)),
        trend_hours=float(display_raw.get("trend_hours", DisplayConfig.trend_hours)),
        sun_arc_seasonal=bool(display_raw.get(
            "sun_arc_seasonal", DisplayConfig.sun_arc_seasonal)),
        gain=float(display_raw.get("gain", DisplayConfig.gain)),
        night_gain=float(display_raw.get("night_gain", DisplayConfig.night_gain)),
    )
    for name, value in (("gain", display.gain), ("night_gain", display.night_gain)):
        if not 0.0 < value <= 1.0:
            raise ConfigError(
                f"display.{name} must be greater than 0 and at most 1, got {value}"
            )
    if display.clock_order not in ("utc", "config"):
        raise ConfigError(
            f"display.clock_order must be 'utc' or 'config', got {display.clock_order!r}"
        )
    weather = WeatherConfig(
        latitude=float(weather_raw.get("lat", WeatherConfig.latitude)),
        longitude=float(weather_raw.get("lon", WeatherConfig.longitude)),
        label=str(weather_raw.get("label", WeatherConfig.label)),
        unit=str(weather_raw.get("unit", WeatherConfig.unit)),
        follow_istat_location=bool(
            weather_raw.get("follow_istat_location", WeatherConfig.follow_istat_location)
        ),
        refresh_seconds=float(
            weather_raw.get("refresh_seconds", WeatherConfig.refresh_seconds)
        ),
        stats=list(weather_raw.get("stats") or DEFAULT_STATS),
    )

    from .pages import STATS  # imported here to keep the module import acyclic

    unknown = [name for name in weather.stats if name not in STATS]
    if unknown:
        raise ConfigError(
            f"unknown weather.stats: {', '.join(unknown)}; "
            f"available: {', '.join(sorted(STATS))}"
        )

    alerts_raw = raw.get("alerts") or {}
    alerts = AlertConfig(
        enabled=bool(alerts_raw.get("enabled", AlertConfig.enabled)),
        lookahead_hours=float(
            alerts_raw.get("lookahead_hours", AlertConfig.lookahead_hours)
        ),
        rain_probability=int(
            alerts_raw.get("rain_probability", AlertConfig.rain_probability)
        ),
        gust_kmh=float(alerts_raw.get("gust_kmh", AlertConfig.gust_kmh)),
        frost_celsius=float(
            alerts_raw.get("frost_celsius", AlertConfig.frost_celsius)
        ),
        hold_minutes=float(alerts_raw.get("hold_minutes", AlertConfig.hold_minutes)),
    )

    clocks = _parse_clocks(raw.get("clocks")) or clocks_from_istat()

    if not display.pages:
        raise ConfigError("display.pages is empty — nothing to show")

    return Config(
        device=device,
        display=display,
        weather=weather,
        alerts=alerts,
        clocks=clocks,
        timezone=raw.get("timezone"),
        path=resolved if resolved.exists() else None,
    )
