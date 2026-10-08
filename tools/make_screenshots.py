#!/usr/bin/env python3
"""Render the README's screenshots.

Everything here is synthetic on purpose. The renderer is the real one, so the
pictures are faithful, but the weather, the clocks and the place are invented —
a screenshot of a working install would otherwise publish the author's home
temperature, local time and, through the city list, a fair sketch of their
life.

    .venv/bin/python tools/make_screenshots.py
"""

from __future__ import annotations

import io
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledclock import menubar, pages  # noqa: E402
from ledclock.alerts import Alert  # noqa: E402
from ledclock.config import (  # noqa: E402
    AgendaCalendar, AgendaConfig, ClockCity, Config, DisplayConfig, WeatherConfig,
)
from ledclock.sources.calendar import Event  # noqa: E402
from ledclock.sources.weather import Current, Day, Hour, Weather  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs"
SCALE = 9

#: A made-up spot in the north Atlantic — real enough for the sun and moon
#: maths, nobody's address.
DEMO_LAT, DEMO_LON = 45.0, -20.0

DEMO_CLOCKS = [
    ClockCity("SFO", "SAN FRANCISCO", "America/Los_Angeles", (255, 214, 92),
              37.77, -122.42),
    ClockCity("NYC", "NEW YORK", "America/New_York", (255, 138, 66), 40.71, -74.01),
    ClockCity("LON", "LONDON", "Europe/London", (110, 176, 255), 51.51, -0.13),
    ClockCity("TYO", "TOKYO", "Asia/Tokyo", (118, 226, 150), 35.68, 139.69),
]


#: Invented calendars and invented meetings. This matters more here than
#: anywhere else in this file: the agenda page draws the opening letters of
#: real event titles, and `docs/` is committed. A screenshot of a working
#: install would publish who the author met and when.
DEMO_CALENDARS = [
    AgendaCalendar("home", (185, 130, 255)),
    AgendaCalendar("personal", (120, 235, 85)),
    AgendaCalendar("work", (70, 170, 255)),
    AgendaCalendar("clients", (255, 80, 70)),
]


def demo_config() -> Config:
    return Config(
        display=DisplayConfig(gain=1.0, night_gain=0.42, trend_hours=24.0),
        weather=WeatherConfig(latitude=DEMO_LAT, longitude=DEMO_LON, label="DEMO"),
        clocks=DEMO_CLOCKS,
        agenda=AgendaConfig(calendars=DEMO_CALENDARS),
        # Pinned, so the pictures come out the same on whatever machine runs
        # this. Without it the agenda page read `now` in the author's own
        # zone: a 23:20 shot meant to show the next-day summary landed at
        # 01:20 the following morning and drew an empty column instead.
        timezone="UTC",
    )


def demo_events(day: datetime) -> list[Event]:
    """A day with something behind, something running and something coming."""
    def at(hour: int, minute: int = 0) -> datetime:
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    return [
        Event(at(9, 30), at(10, 0), "personal", (120, 235, 85), "STANDUP"),
        Event(at(11, 0), at(12, 30), "clients", (255, 80, 70), "WORKSHOP"),
        Event(at(13, 0), at(13, 45), "work", (70, 170, 255), "REVIEW"),
        Event(at(14, 0), at(15, 0), "work", (70, 170, 255), "DEMO"),
        Event(at(16, 0), at(16, 30), "personal", (120, 235, 85), "GYM"),
        Event(at(19, 0), at(21, 0), "home", (185, 130, 255), "DINNER"),
    ]


def demo_weather(now: datetime, *, is_day: bool = True, code: int = 0) -> Weather:
    """A pleasant day with rain arriving in the evening."""
    shape = [16, 15, 15, 14, 14, 15, 17, 19, 21, 23, 25, 26, 27, 28, 28, 27,
             26, 24, 22, 21, 20, 19, 18, 17, 16, 16, 15, 15, 16, 18, 20, 22,
             24, 26, 27, 27, 26, 25, 23, 21, 20, 19, 18, 17, 17, 16, 16, 15]
    rain = [0] * 30 + [10, 25, 45, 70, 85, 80, 60, 35, 20, 10, 0, 0, 0, 0,
                       0, 0, 0, 0]
    codes = [0] * 30 + [61] * 8 + [3] * 10

    start = now - timedelta(hours=24)
    hours = [
        Hour(time=start + timedelta(hours=i), temperature=float(shape[i]),
             apparent=float(shape[i]) + 0.4,
             precipitation_probability=rain[i], code=codes[i], gusts=22.0)
        for i in range(48)
    ]
    return Weather(
        current=Current(temperature=24.3, apparent=24.6, code=code, is_day=is_day,
                        humidity=52, wind=14.0, gusts=26.0, pressure=1014.0,
                        clouds=20, precipitation=0.0),
        days=[
            Day(now.date(), 0, 28.0, 16.0, precipitation_probability=10, uv_max=6.2),
            Day(now.date() + timedelta(days=1), 61, 22.0, 15.0,
                precipitation_probability=80, uv_max=3.0),
            Day(now.date() + timedelta(days=2), 3, 25.0, 14.0,
                precipitation_probability=20, uv_max=5.1),
        ],
        fetched_at=0.0,
        hours=hours,
    )


def shot(name: str, ctx: pages.PageContext) -> Image.Image:
    canvas = pages.render(name, ctx)
    return Image.open(io.BytesIO(canvas.to_preview_png(scale=SCALE)))


def contact_sheet(tiles: list[Image.Image], gap: int = 12) -> Image.Image:
    width = sum(t.width for t in tiles) + gap * (len(tiles) - 1)
    sheet = Image.new("RGB", (width, tiles[0].height), (0, 0, 0))
    x = 0
    for tile in tiles:
        sheet.paste(tile, (x, 0))
        x += tile.width + gap
    return sheet


def main() -> int:
    OUT.mkdir(exist_ok=True)
    config = demo_config()
    midday = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    # Chosen for the picture: a waxing gibbous, 73% lit and 26 degrees up,
    # so the hero disc and the sky track both have something to show.
    night = datetime(2026, 6, 24, 22, 0, tzinfo=timezone.utc)

    def context(when, **kwargs):
        return pages.PageContext(config=config, now=when,
                                 weather=demo_weather(when, **kwargs),
                                 location=(DEMO_LAT, DEMO_LON))

    day = context(midday)
    dark = context(night, is_day=False)

    # Twenty past two: the morning is behind, the two o'clock is running and
    # the gym is close enough to name in the header.
    afternoon = midday.replace(hour=14, minute=20)
    agenda_now = pages.PageContext(config=config, now=afternoon,
                                   location=(DEMO_LAT, DEMO_LON),
                                   events=demo_events(afternoon))
    # Late enough that today is spent and the page switches to the next day
    # that has anything in it.
    bedtime = midday.replace(hour=23, minute=20)
    tomorrow = bedtime + timedelta(days=1)
    agenda_late = pages.PageContext(config=config, now=bedtime,
                                    location=(DEMO_LAT, DEMO_LON),
                                    events=[],
                                    ahead=(tomorrow.date(),
                                           demo_events(tomorrow)))

    singles = {
        "page-world-clocks": shot("world_clocks", day),
        "page-weather-now": shot("weather_now", day),
        "page-weather-night": shot("weather_now", dark),
        "page-trend": shot("trend", day),
        "page-forecast": shot("forecast", day),
        "page-agenda": shot("agenda", agenda_now),
        "page-agenda-tomorrow": shot("agenda", agenda_late),
    }
    alerts = [
        Alert("storm", 2, 85, "STORM", "%"),
        Alert("rain", 3, 70, "RAIN", "%"),
        Alert("wind", 1, 78, "WIND"),
        Alert("frost", 5, -2, "FROST", "°"),
    ]
    for warning in alerts:
        singles[f"alert-{warning.kind}"] = shot(
            "alert",
            pages.PageContext(config=config, now=midday,
                              weather=demo_weather(midday),
                              location=(DEMO_LAT, DEMO_LON), alert=warning),
        )

    for name, image in singles.items():
        image.save(OUT / f"{name}.png")

    contact_sheet([singles[n] for n in (
        "page-world-clocks", "page-agenda", "page-weather-now", "page-trend",
        "page-forecast",
    )]).save(OUT / "pages.png")
    contact_sheet([singles[f"alert-{k}"]
                   for k in ("storm", "rain", "wind", "frost")]).save(
        OUT / "alerts.png")

    # Menu bar icon, on both menu bar colours, magnified so it is visible in
    # a README.
    spark = {"values": [(v - 14) / 14 for v in
                        [16, 18, 21, 24, 26, 27, 28, 27, 25, 23, 21, 19, 18,
                         17, 16, 16, 15, 15, 16, 18, 20, 22]],
             "now": 0.5}
    for tag, background, tint in (("dark", (28, 28, 30), (255, 255, 255)),
                                  ("light", (246, 246, 246), (0, 0, 0))):
        strips = []
        for paused, linked in ((False, True), (True, True), (False, False)):
            drawn = menubar.draw_sparkline(spark, paused=paused, linked=linked)
            solid = Image.new("RGBA", drawn.size, tint + (0,))
            solid.putalpha(drawn.split()[3])
            strips.append(solid)
        width = sum(s.width for s in strips) + 30 * (len(strips) + 1)
        bar = Image.new("RGB", (width, strips[0].height + 20), background)
        x = 30
        for strip in strips:
            bar.paste(strip, (x, 10), strip)
            x += strip.width + 30
        bar.resize((bar.width * 3, bar.height * 3), Image.NEAREST).save(
            OUT / f"menubar-{tag}.png")

    print(f"wrote {len(list(OUT.glob('*.png')))} images to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
