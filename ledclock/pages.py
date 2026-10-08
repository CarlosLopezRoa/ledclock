"""The carousel. Each page renders one 32x32 frame.

Space is the binding constraint: 32x32 is 1024 LEDs total, so every page
gets one idea. Layouts are written as explicit pixel rows rather than a
layout engine because at this size hand-placement wins every time.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import icons, moon, sun
from .config import ClockCity, Config
from .font import FONT_3x5, FONT_5x7
from .render import WHITE, Canvas, dim, temperature_color
from .alerts import STORM_CODES, Alert
from .sources.weather import (
    Weather, format_high, format_low, format_temperature,
)

RGB = tuple[int, int, int]

GREY: RGB = (150, 158, 170)
FAINT: RGB = (48, 52, 62)
HOT: RGB = (255, 138, 66)
COLD: RGB = (92, 168, 255)
STALE: RGB = (120, 40, 40)

#: Fallback evening/morning hours, used only where coordinates are unknown
#: and real sunrise/sunset cannot be worked out.
NIGHT_START = 21
NIGHT_END = 7

#: Pages that decide day and night per element rather than page-wide. The
#: world clocks dim each row against its own city's sun, so applying the
#: page-level rule on top would dim the night rows twice.
SELF_DIMMING = frozenset({"world_clocks"})

#: How far a row where the sun is up is pulled toward the night level once
#: the sun is down *here*. The exemption above was read for years as "this
#: page ignores local night", and the cost was never measured: at two in the
#: morning a daytime row drew full white, 44% brighter than anything else the
#: panel shows at that hour, which is what finally made it annoying.
#:
#: The fix is a blend rather than the page-level rule, because the night rows
#: must not move — they already sit at the night level and dimming them again
#: is the double-dimming the exemption exists to prevent. 1.0 would be the old
#: behaviour and 0.0 would flatten the two kinds of row into one, taking with
#: it the only thing the page has to say beyond the hour.
#:
#: A quarter of the way leaves the daytime rows 11% over the ceiling — twelve
#: steps out of 255. That is deliberately close to the floor: the complaint
#: was glare in a dark room, and a dark-adapted eye separates those twelve
#: steps far better than this reads on a lit screen.
CLOCK_DAY_AT_NIGHT = 0.25

# English throughout. Weekdays that come from strftime arrive as THU, and a
# panel that said THU on one page and JUE on the next was simply inconsistent.
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
DAYS_SHORT = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


@dataclass(frozen=True)
class PageContext:
    config: Config
    now: datetime
    weather: Weather | None = None
    #: Where the panel is, for working out its own sunrise and sunset.
    #: Left out, it is taken from the weather configuration.
    location: tuple[float, float] | None = None
    #: The warning the `alert` page should draw, if any.
    alert: Alert | None = None
    #: Today's events, already filtered to the configured calendars.
    events: list = field(default_factory=list)
    #: The next day with anything in it, as (date, events), or None. The
    #: column has nothing to say once today is spent — there is no "now" to
    #: place in it — so the page shows this instead.
    ahead: tuple | None = None

    def local_now(self) -> datetime:
        if self.config.timezone:
            return self.now.astimezone(ZoneInfo(self.config.timezone))
        return self.now.astimezone()

    def here(self) -> tuple[float, float]:
        if self.location is not None:
            return self.location
        latitude, longitude, _ = self.config.weather.resolve_coordinates()
        return latitude, longitude

    def is_night_here(self) -> bool:
        latitude, longitude = self.here()
        # local_now() so that the no-coordinates fallback reads a local hour.
        return _is_night(self.local_now(), latitude, longitude)


def _is_night(moment: datetime, latitude: float | None = None,
              longitude: float | None = None) -> bool:
    """Night at that spot on the globe.

    Real sunset moves by hours across the year and by far more across the
    time zones on the world-clock page, so it is solved for rather than
    guessed. Places with no coordinates fall back to fixed hours.
    """
    if latitude is None or longitude is None:
        # `moment` is already in the zone we are asking about, so read its
        # hour directly — converting here would silently answer for the Mac's
        # own zone instead of the city's.
        return moment.hour >= NIGHT_START or moment.hour < NIGHT_END
    return sun.is_night(moment, latitude, longitude)


def night_factor(display) -> float:
    """How much to dim night content at draw time.

    `night_gain` is an absolute target, but the whole frame is scaled by
    `gain` on the way out, so the draw-time factor has to divide that back
    out. Without this the two dimmings multiply: at gain 0.4 a night row
    landed at 0.4 x 0.42 = 17% of full and vanished off the panel.

    Clamped at 1.0, since night must never outshine day.
    """
    if display.gain <= 0:
        return 1.0
    return min(1.0, display.night_gain / display.gain)


def _clock_text(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _fit(text: str, font, limit: int) -> str:
    """Shorten `text` until it fits `limit` pixels, at a word break if possible."""
    if font.width(text) <= limit:
        return text
    if " " in text:
        head = text.split(" ", 1)[0]
        if font.width(head) <= limit:
            return head
    while text and font.width(text) > limit:
        text = text[:-1]
    return text


#: Only show the apparent temperature once it is this far from the real one.
#: Below that it is noise: 28.5 against 28.6 rounds to "28" over "29" and
#: reads like a contradiction rather than information.
APPARENT_THRESHOLD = 1.0

#: How far the tenths digit is knocked back from the whole degrees.
DECIMAL_DIM = 0.5


def _split_decimal(value: float) -> tuple[str, str]:
    """Split a temperature into its whole part and one decimal digit.

    Done on the magnitude so that -5.25 becomes ("-5", "3") rather than the
    ("-6", "3") that floor division would give.
    """
    sign = "-" if value < 0 else ""
    scaled = round(abs(value) * 10)
    return f"{sign}{scaled // 10}", str(scaled % 10)


def _draw_temperature(canvas: Canvas, x: int, y: int, value: float,
                      color: RGB, budget: int) -> None:
    """Whole degrees in 5x7, with the tenths and the degree sign stacked.

    '28.5°' in 5x7 is 23px and the slot beside the hero icon is 15, so the
    tenths step down a font size. There is no decimal point: a single pixel
    was too weak to read as one anyway ("28.9" came out looking like "289"),
    and dropping it frees the column that lets the degree sign back in.

    Instead the two small marks share the same columns at different heights —
    a degree mark level with the top of the big digits, the tenths below it.
    Height does the separating that the point failed to do, and the tenths
    are dimmed as well.

    The degree mark is a single pixel rather than the font's 2x2 block: at
    this size one pixel still reads as a degree, and the two rows it gives
    back are what let the tenths sit on the baseline instead of hanging
    below it.

    Falls back to whole degrees when there is not room.
    """
    whole, tenths = _split_decimal(value)
    whole_width = FONT_5x7.width(whole)
    side_width = max(1, FONT_3x5.width(tenths))

    if whole_width + 1 + side_width <= budget:
        canvas.text(x, y, whole, FONT_5x7, color)
        side_x = x + whole_width + 1
        canvas.pixel(side_x, y, color)
        # y + 2 bottom-aligns the 3x5 tenths with the 7-tall digits' baseline.
        canvas.text(side_x, y + FONT_5x7.height - FONT_3x5.height, tenths,
                    FONT_3x5, dim(color, DECIMAL_DIM))
        return

    text = f"{whole}°"
    if FONT_5x7.width(text) > budget:
        text = whole
    canvas.text(x, y, text, FONT_5x7, color)


#: The sun's dome: the part of the day already spent, and the outline of what
#: is left. The moon does not share this — it gets its real path.
ARC_DONE: RGB = (150, 120, 50)
ARC_AHEAD: RGB = (52, 48, 40)
MOON_MARK: RGB = (244, 248, 255)
#: The track the moon follows. Faint, but not as faint as the "still to
#: come" grey of the sun arc: here the track is the frame of reference, not
#: a countdown, so it has to be legible on its own.
MOON_TRACK: RGB = (74, 80, 92)

#: The compass sweep the moon slot covers, east through south to west. Real
#: passes at this latitude run roughly 60 to 290 degrees, so this frames them
#: without wasting pixels on sky the moon never crosses.
MOON_AZIMUTH_FROM = 60.0
MOON_AZIMUTH_TO = 300.0
#: Altitude at the top of the slot. The moon tops out near 69 degrees here.
MOON_ALTITUDE_TOP = 70.0


def _draw_moon_sky(canvas: Canvas, left: int, baseline: int, width: int,
                   height: int, now: datetime, latitude: float,
                   longitude: float) -> bool:
    """The moon's real track across the sky, and where on it the moon is.

    Not a progress bar. Position across is compass bearing — east at the left,
    south in the middle, west at the right — and height is altitude above the
    horizon, so a low winter moon hugs the bottom and a high one arcs over.

    The whole point is the question "is it out, and where". So the track is
    always faint and the moon itself is only drawn when it is actually above
    the horizon: an empty track means it has not risen yet, and its left end
    is where it will come up.

    Returns False when there is nothing to draw at all.
    """
    track = moon.path(now, latitude, longitude)
    if not track:
        return False

    def place(altitude: float, azimuth: float) -> tuple[int, int]:
        across = (azimuth - MOON_AZIMUTH_FROM) / (MOON_AZIMUTH_TO - MOON_AZIMUTH_FROM)
        up = max(0.0, min(1.0, altitude / MOON_ALTITUDE_TOP))
        return (
            left + round(max(0.0, min(1.0, across)) * (width - 1)),
            baseline - round(up * (height - 1)),
        )

    # No drawn horizon: the track already ends at it, and a line here sat
    # directly above the page separator and read as a thickening of it.
    for _, altitude, azimuth in track:
        x, y = place(altitude, azimuth)
        canvas.pixel(x, y, MOON_TRACK)

    altitude, azimuth = moon.position(now, latitude, longitude)
    if altitude > 0:
        x, y = place(altitude, azimuth)
        canvas.pixel(x, y, MOON_MARK)
        # A second pixel so it reads as a body rather than as track.
        canvas.pixel(x, y - 1, MOON_MARK)
    return True


def _wedge_fraction(px: int, py: int, radius: int, height: int) -> float | None:
    """Where a pixel falls along the dome's sweep, 0 at rise and 1 at set.

    None if the pixel is outside the dome. Coordinates are relative to the
    dome's centre, with py negative upward — the same parametrisation
    the dome is parametrised, solved backwards so the area can be filled
    rather than only outlined.
    """
    if py > 0:
        return None
    # Scale y back up when the dome is squashed, so the test stays circular.
    stretched = py * (radius / height) if height else py
    if px * px + stretched * stretched > radius * radius:
        return None
    # abs(), not -stretched: on the baseline row that value is negative zero,
    # and atan2(-0.0, negative) returns -pi rather than +pi. The right-hand
    # half of the base then read as fraction -1 and was drawn as spent all
    # day long.
    return math.atan2(abs(stretched), -px) / math.pi


def _draw_sky_wedge(canvas: Canvas, centre_x: int, baseline: int, radius: int,
                    progress: float, *, done: RGB, ahead: RGB,
                    elevation: float | None = None) -> None:
    """The same dome, filled in as the day is spent.

    A slice that starts as a sliver at dawn and reaches half a disc at dusk.
    Area reads faster than position: you can see how much daylight is left
    without first having to find a dot and judge where along a line it sits.

    Only the spent part is filled; what is left of the day is drawn as an
    outline. Filling both was the first attempt and it read as one dark mass
    with a bright corner — the eye went to the block, not to the boundary
    between them, which is the only thing that moves.
    """
    height = radius
    if elevation is not None:
        height = max(2, round(radius * math.sin(math.radians(max(0.0, elevation)))))

    progress = min(1.0, max(0.0, progress))

    # The dome the day is spent against, so there is something to fill toward.
    steps = radius * 12
    for step in range(steps + 1):
        angle = math.pi * (step / steps)
        canvas.pixel(round(centre_x - radius * math.cos(angle)),
                     round(baseline - height * math.sin(angle)), ahead)

    for py in range(-height, 1):
        for px in range(-radius, radius + 1):
            fraction = _wedge_fraction(px, py, radius, height)
            if fraction is None or fraction > progress:
                continue
            canvas.pixel(centre_x + px, baseline + py, done)


def _mark_stale(canvas: Canvas, weather: Weather | None) -> None:
    if weather is not None and weather.stale:
        canvas.pixel(canvas.width - 1, 0, STALE)


def _sky_icon(weather: Weather, now: datetime, size: int) -> icons.Icon:
    """What the sky looks like: the condition by day, the moon by night.

    Under an overcast night sky the moon is not visible anyway, so the
    condition icon keeps the slot — a crisp full moon drawn while it rains
    would be worse than useless.
    """
    current = weather.current
    if current.is_day or current.code not in icons.CLEAR_CODES:
        return icons.for_code(current.code, size)
    return icons.Icon(moon.render(size, moon.phase(now)))


# --------------------------------------------------------------------------
# Page 1 — the local clock, with the date and current conditions beneath.
# --------------------------------------------------------------------------
def clock_local(ctx: PageContext) -> Canvas:
    canvas = Canvas()
    now = ctx.local_now()

    canvas.text_centered(1, _clock_text(now), FONT_5x7, WHITE)

    date_text = f"{DAYS[now.weekday()]} {now.day} {MONTHS[now.month - 1]}"
    canvas.text_centered(10, _fit(date_text, FONT_3x5, canvas.width), FONT_3x5, GREY)

    canvas.hline(4, 17, canvas.width - 8, FAINT)

    weather = ctx.weather
    if weather is None:
        canvas.text_centered(22, "-- -", FONT_3x5, FAINT)
        return canvas

    canvas.icon(2, 21, _sky_icon(weather, ctx.now, 8))
    _draw_temperature(canvas, 13, 21, weather.current.temperature, WHITE,
                      budget=canvas.width - 13)
    _mark_stale(canvas, weather)
    return canvas


# --------------------------------------------------------------------------
# Page 2 — four world clocks, one per 8px row.
# --------------------------------------------------------------------------
def world_clocks(ctx: PageContext) -> Canvas:
    canvas = Canvas()
    cities: list[ClockCity] = list(ctx.config.clocks)

    if ctx.config.display.clock_order == "utc":
        # Sorted by live offset rather than by a number in the config, so
        # daylight saving reshuffles the rows on its own.
        cities.sort(key=lambda city: ctx.now.astimezone(
            ZoneInfo(city.timezone)).utcoffset() or timedelta(0))
    cities = cities[:4]

    there_night = night_factor(ctx.config.display)
    # A row is dimmed for its own city's night, and the whole page is pulled
    # down for ours — but only the daytime rows, and only part of the way.
    there_day = 1.0
    if ctx.is_night_here():
        there_day = there_night + CLOCK_DAY_AT_NIGHT * (1.0 - there_night)

    for index, city in enumerate(cities):
        y = 1 + index * 8
        there = ctx.now.astimezone(ZoneInfo(city.timezone))
        night = _is_night(there, city.latitude, city.longitude)

        factor = there_night if night else there_day
        code_color = dim(city.color, factor)
        time_color = dim(WHITE, factor)

        clock = _clock_text(there)
        # The clock is right-aligned, so whatever it leaves is the code's budget.
        # 'M' and 'W' are 5px wide, so MEX needs 13 of the 13 available.
        budget = canvas.width - FONT_3x5.width(clock) - 2
        canvas.text(0, y, _fit(city.code, FONT_3x5, budget), FONT_3x5, code_color)
        canvas.text_right(canvas.width, y, clock, FONT_3x5, time_color)

    _mark_stale(canvas, ctx.weather)
    return canvas


# --------------------------------------------------------------------------
# Page 3 — current conditions, the one page that earns a 16x16 hero.
# --------------------------------------------------------------------------
#: Stat name -> (marker, colour, how to read it, how to write it).
#: The formatter matters for the two that are *bounds*: a day's low and high
#: round outwards so the pair actually contains the day, rather than to
#: nearest, which let "max 34" sit next to a current reading of 35.
STATS: dict[
    str, tuple[str, RGB, Callable[[Weather], float | int | None], Callable[[float], str]]
] = {
    "humidity": ("humidity", (96, 168, 255), lambda w: w.current.humidity,
                 format_temperature),
    "wind": ("wind", (176, 186, 200), lambda w: w.current.wind, format_temperature),
    "gusts": ("wind", (214, 178, 120), lambda w: w.current.gusts, format_temperature),
    "clouds": ("clouds", (176, 186, 200), lambda w: w.current.clouds,
               format_temperature),
    "pressure": ("pressure", (150, 158, 170), lambda w: w.current.pressure,
                 format_temperature),
    "low": ("low", COLD, lambda w: w.days[0].low if w.days else None, format_low),
    "high": ("high", HOT, lambda w: w.days[0].high if w.days else None, format_high),
    "precip": ("precip", (96, 168, 255),
               lambda w: w.days[0].precipitation_probability if w.days else None,
               format_temperature),
    "uv": ("uv", (255, 186, 72), lambda w: w.days[0].uv_max if w.days else None,
           format_temperature),
}

#: Half the panel, minus a two-pixel gutter down the middle.
_STAT_CELL_WIDTH = 15
_STAT_ROWS = (19, 26)


def _draw_stat(canvas: Canvas, name: str, weather: Weather, x: int, y: int,
               align_right: bool) -> None:
    """One marker-plus-number cell. Drops the marker if the number is wide."""
    entry = STATS.get(name)
    if entry is None:
        return
    marker_name, color, extract, render = entry
    try:
        value = extract(weather)
    except (IndexError, AttributeError):
        value = None
    if value is None:
        return

    text = render(value)
    marker = icons.MARKERS[marker_name]
    with_marker = marker.width + 1 + FONT_3x5.width(text)

    if with_marker <= _STAT_CELL_WIDTH:
        left = x + _STAT_CELL_WIDTH - with_marker if align_right else x
        canvas.icon(left, y, marker, color)
        canvas.text(left + marker.width + 1, y, text, FONT_3x5, color)
        return

    # No room for both; the number is the part that carries information.
    text = _fit(text, FONT_3x5, _STAT_CELL_WIDTH)
    left = x + _STAT_CELL_WIDTH - FONT_3x5.width(text) if align_right else x
    canvas.text(left, y, text, FONT_3x5, color)


def weather_now(ctx: PageContext) -> Canvas:
    canvas = Canvas()
    weather = ctx.weather
    if weather is None:
        canvas.text_centered(12, "NO DATA", FONT_3x5, FAINT)
        return canvas

    current = weather.current
    canvas.icon(0, 0, _sky_icon(weather, ctx.now, 16))

    _draw_temperature(canvas, 17, 2, current.temperature, WHITE,
                      budget=canvas.width - 17)

    # Apparent temperature, only once it is far enough away to mean something.
    # When it has nothing to say, the slot goes to the sun's path across the
    # sky instead of sitting empty.
    if abs(current.apparent - current.temperature) > APPARENT_THRESHOLD:
        canvas.text(18, 11, f"{format_temperature(current.apparent)}°", FONT_3x5, GREY)
    else:
        latitude, longitude = ctx.here()
        # baseline 15 leaves row 16 clear, so a flat winter arc does not merge
        # into the separator underneath it.
        geometry = dict(centre_x=24, baseline=15, radius=6)
        daylight = sun.day_progress(ctx.now, latitude, longitude)
        if daylight is not None:
            _draw_sky_wedge(
                canvas, **geometry, progress=daylight,
                done=ARC_DONE, ahead=ARC_AHEAD,
                elevation=(sun.max_elevation(ctx.now.date(), latitude)
                           if ctx.config.display.sun_arc_seasonal else None),
            )
        else:
            # After dark the slot shows the moon's actual track rather than
            # how far through the night we are. The moon does not cross the
            # sky in step with the night, and the useful question is whether
            # it is out and where to look.
            _draw_moon_sky(canvas, left=18, baseline=15, width=13, height=7,
                           now=ctx.now, latitude=latitude, longitude=longitude)

    canvas.hline(2, 17, canvas.width - 4, FAINT)

    # Four stats in a 2x2 grid, outer edges aligned to the panel edges.
    for index, name in enumerate(ctx.config.weather.stats[:4]):
        _draw_stat(
            canvas,
            name,
            weather,
            x=0 if index % 2 == 0 else canvas.width - _STAT_CELL_WIDTH,
            y=_STAT_ROWS[index // 2],
            align_right=index % 2 == 1,
        )

    _mark_stale(canvas, weather)
    return canvas


# --------------------------------------------------------------------------
# Page 4 — three days across, ten pixels each.
# --------------------------------------------------------------------------
def forecast(ctx: PageContext) -> Canvas:
    canvas = Canvas()
    weather = ctx.weather
    if weather is None or not weather.days:
        canvas.text_centered(12, "NO DATA", FONT_3x5, FAINT)
        return canvas

    column_width = 10
    origins = [0, 11, 22]
    canvas.vline(10, 2, 27, FAINT)
    canvas.vline(21, 2, 27, FAINT)

    for origin, day in zip(origins, weather.days[:3]):
        def centered(text: str, y: int, color: RGB, font=FONT_3x5) -> None:
            x = origin + (column_width - font.width(text)) // 2
            canvas.text(x, y, text, font, color)

        centered(DAYS_SHORT[day.day.weekday()], 1, GREY)
        icon = icons.for_code(day.code, 8)
        canvas.icon(origin + (column_width - 8) // 2, 7, icon)
        centered(format_high(day.high), 17, HOT)
        centered(format_low(day.low), 24, COLD)

    _mark_stale(canvas, weather)
    return canvas


# --------------------------------------------------------------------------
# Page 5 — the temperature either side of now.
# --------------------------------------------------------------------------
#: Rows the curve may use. Above it sits the scale, which the curve needs:
#: a line with no numbers on it says only "the shape of something". Below it
#: sits the rain band.
TREND_TOP = 7
TREND_BOTTOM = 27
#: Rows given to precipitation. Rain lives on its own channel rather than in
#: the line's colour, because the line's colour is already saying temperature
#: — and "blue because cold" against "blue because wet" is unreadable.
RAIN_TOP = 29
RAIN_BOTTOM = 31
#: Below this the band is noise, not news. Open-Meteo happily reports 3% and
#: 8% chances, and a one-pixel bar for those made a dry day look showery.
RAIN_MIN_PROBABILITY = 15.0
#: Keep at least this much spread on the vertical axis, so a flat couple of
#: hours does not get amplified into dramatic-looking noise.
TREND_MIN_SPAN = 3.0
# There is deliberately no past/future dimming here any more. Brightness and
# hue were both in play at once and they fought: dimming a warm colour makes
# it read browner, so a *cooler* forecast point looked *redder* than the
# warmer present one — 28.9C drawn as (140,91,36) beside 29.9C as
# (255,154,60). The line's colour now says temperature and nothing else; the
# vertical marker is what separates measured from forecast.

NOW_LINE: RGB = (60, 64, 76)
RAIN: RGB = (72, 150, 255)
STORM: RGB = (255, 214, 72)


def _sample(hours: list, moment: datetime, attribute: str = "temperature"):
    """A reading at `moment`, interpolated between the hourly points."""
    if not hours:
        return None

    def value(hour):
        return getattr(hour, attribute, None)

    if moment <= hours[0].time:
        return value(hours[0])
    if moment >= hours[-1].time:
        return value(hours[-1])
    for earlier, later in zip(hours, hours[1:]):
        if earlier.time <= moment <= later.time:
            first, second = value(earlier), value(later)
            if first is None or second is None:
                return first if first is not None else second
            span = (later.time - earlier.time).total_seconds()
            if span <= 0:
                return first
            ratio = (moment - earlier.time).total_seconds() / span
            return first + (second - first) * ratio
    return value(hours[-1])


def _nearest(hours: list, moment: datetime):
    """The hour a moment falls in. For things that do not interpolate, like
    a weather code."""
    if not hours:
        return None
    return min(hours, key=lambda h: abs((h.time - moment).total_seconds()))


def trend(ctx: PageContext) -> Canvas:
    canvas = Canvas()
    weather = ctx.weather
    if weather is None or len(weather.hours) < 2:
        canvas.text_centered(12, "NO DATA", FONT_3x5, FAINT)
        return canvas

    half = timedelta(hours=ctx.config.display.trend_hours / 2)
    start, end = ctx.now - half, ctx.now + half
    columns = canvas.width
    moments = [start + (end - start) * (x / (columns - 1)) for x in range(columns)]

    # One reading per column: the hourly data is coarser than the panel, so
    # interpolate rather than drawing a staircase.
    readings = [_sample(weather.hours, m) for m in moments]
    if any(value is None for value in readings):
        canvas.text_centered(12, "NO DATA", FONT_3x5, FAINT)
        return canvas

    now_column = (columns - 1) // 2

    # The middle label comes from the API's current observation rather than
    # the interpolated curve, so it agrees with the big figure on
    # `weather_now`. The two are usually within a rounding error, but the
    # observation is a 15-minute product while the series is hourly means, so
    # at a sharp peak it can sit above every point on the curve. Folding it
    # into the extremes keeps the header self-consistent: "max 34, now 35"
    # was on screen before this.
    present = weather.current.temperature if weather.current else readings[now_column]
    extremes = readings + [present]
    coolest, warmest = min(extremes), max(extremes)

    # The axis is padded when the window is flat, so that two tenths of a
    # degree do not get stretched into a dramatic-looking swing. The labels
    # still report the real extremes: the padding is scaffolding, not data.
    axis_low, axis_high = coolest, warmest
    if axis_high - axis_low < TREND_MIN_SPAN:
        middle = (axis_high + axis_low) / 2
        axis_low = middle - TREND_MIN_SPAN / 2
        axis_high = middle + TREND_MIN_SPAN / 2

    def row_for(value: float) -> int:
        ratio = (value - axis_low) / (axis_high - axis_low)
        return TREND_BOTTOM - round(ratio * (TREND_BOTTOM - TREND_TOP))

    # Fahrenheit readings have to come back to Celsius before the colour ramp
    # sees them, or every value would land at the hot end.
    fahrenheit = ctx.config.weather.unit == "fahrenheit"

    def hue(value: float) -> RGB:
        celsius = (value - 32) / 1.8 if fahrenheit else value
        return temperature_color(celsius)

    # Coldest, current, warmest — left to right, each in the colour the ramp
    # gives it. That makes the header double as the graph's legend: the same
    # temperature is the same colour in both, so the numbers explain the line
    # without needing an axis. Arrows are redundant once position says which
    # is which.
    #
    # The middle number comes from the API's current reading rather than the
    # interpolated curve, so it agrees with the big figure on `weather_now`.
    # The two track each other to a rounding error in practice.
    labels = [
        (format_low(coolest), hue(coolest), "left"),
        (format_temperature(present), hue(present), "centre"),
        (format_high(warmest), hue(warmest), "right"),
    ]
    widths = [FONT_3x5.width(text) for text, _, _ in labels]
    if sum(widths) + 2 <= canvas.width:
        positions = [
            0,
            (canvas.width - widths[1]) // 2,
            canvas.width - widths[2],
        ]
    else:
        # Three wide readings at once — a deep freeze. Drop the middle rather
        # than let them collide; the marker on the curve still shows it.
        positions = [0, None, canvas.width - widths[2]]
    for (text, color, _), x in zip(labels, positions):
        if x is not None:
            canvas.text(x, 0, text, FONT_3x5, color)

    # Runs the full height, rain band included: it is now the only thing
    # separating measured from forecast, so it has to be unmissable.
    canvas.vline(now_column, TREND_TOP - 1, RAIN_BOTTOM - TREND_TOP + 2, NOW_LINE)

    previous: int | None = None
    for x, value in enumerate(readings):
        y = row_for(value)
        color = hue(value)
        if previous is not None:
            # Join to the previous column so a steep hour reads as a line
            # rather than two unrelated dots.
            step = 1 if y > previous else -1
            for fill in range(previous + step, y, step):
                canvas.pixel(x, fill, color)
        canvas.pixel(x, y, color)
        previous = y

    # Rain on its own band along the bottom: bar height is the chance of it,
    # and thunder recolours the bar rather than adding a fourth thing to read.
    band = RAIN_BOTTOM - RAIN_TOP + 1
    for x, moment in enumerate(moments):
        chance = _sample(weather.hours, moment, "precipitation_probability")
        if not chance or chance < RAIN_MIN_PROBABILITY:
            continue
        # Scale from the floor, so the shortest bar means "worth noticing"
        # rather than "the model twitched".
        above = (min(100.0, chance) - RAIN_MIN_PROBABILITY) / (100.0 - RAIN_MIN_PROBABILITY)
        height = 1 + round(above * (band - 1))
        hour = _nearest(weather.hours, moment)
        color = STORM if hour is not None and hour.code in STORM_CODES else RAIN
        canvas.rect(x, RAIN_BOTTOM - height + 1, 1, height, color)

    # The reading at "now" gets the brightest pixel on the page.
    canvas.pixel(now_column, row_for(readings[now_column]), (255, 255, 255))
    _mark_stale(canvas, weather)
    return canvas


# --------------------------------------------------------------------------
# Page 6 — something is coming.
# --------------------------------------------------------------------------
ALERT_COLORS: dict[str, RGB] = {
    "storm": (255, 214, 72),
    "wind": (198, 208, 226),
    "frost": (140, 200, 255),
    "rain": (96, 168, 255),
}


def alert(ctx: PageContext) -> Canvas:
    canvas = Canvas()
    warning = ctx.alert
    if warning is None:
        canvas.text_centered(12, "NO ALERT", FONT_3x5, FAINT)
        return canvas

    color = ALERT_COLORS.get(warning.kind, WHITE)
    canvas.icon(0, 0, icons.for_code(None, 16)
                if warning.kind not in icons.ALERT_ICONS
                else icons.ICONS_16[icons.ALERT_ICONS[warning.kind]])

    # Lead time is the point of the page, so it gets the big font. The "H"
    # steps down a size for the same reason the temperature's tenths do:
    # 15px does not fit two big glyphs and a letter.
    hours = str(warning.lead_hours)
    canvas.text(17, 2, hours, FONT_5x7, color)
    canvas.text(17 + FONT_5x7.width(hours) + 1,
                2 + FONT_5x7.height - FONT_3x5.height, "H", FONT_3x5,
                dim(color, 0.55))

    canvas.text_centered(18, _fit(warning.label, FONT_3x5, canvas.width),
                         FONT_3x5, color)

    # A frost warning rounds down: -0.4 shown as 0 would hide that it is
    # going below freezing, which is the entire point of the warning.
    render = format_low if warning.unit == "°" else format_temperature
    magnitude = f"{render(warning.magnitude)}{warning.unit}"
    canvas.text_centered(25, magnitude, FONT_3x5, GREY)

    _mark_stale(canvas, ctx.weather)
    return canvas


#: Everything the carousel can show. Add a function here to add a page.

# -- the agenda ------------------------------------------------------------
#
# A day column, the way a calendar app draws one: time runs downwards, each
# event is a box as tall as it is long, and a white line marks now.
#
# An earlier version laid the day out horizontally as a four-pixel band. It
# was rejected on sight — a horizontal strip reads as a progress bar, not as
# a day, and there is nowhere to put a name.
#
# Two behaviours earn their complexity, and both came from drawing the thing
# against real days rather than invented ones:
#
#   * The window stretches to fit. Fixed at six hours it looked right on a
#     busy invented day and wasted the panel on a real one — at half past
#     three, with the previous meeting over at half one, twenty-four rows
#     held a single box.
#   * The line is never flush against the header. It belongs near the top
#     when nothing has happened yet, but touching the divider it reads as
#     part of it.

AGENDA_DIM = (64, 68, 78)
#: The minutes-to-go in the header. Bright on purpose: it is the one number
#: on the page that changes while you look at it.
AGENDA_INK = (245, 248, 255)
AGENDA_NOW = (255, 255, 255)
#: Width of the now-mark, at the right edge.
AGENDA_MARK = 3
#: Rows of tomorrow's list that fit above the "and more" line.
AGENDA_SUMMARY_ROWS = 3
#: The hour labels. These began much dimmer, on the reasoning that a scale
#: should not compete with the content — but at the gain this panel runs at,
#: a dim grey lands near (58, 61, 69) and simply cannot be read across a
#: room. Legibility won.
AGENDA_HOUR = (185, 192, 208)

#: Rows the column occupies: the header takes the first five and a divider
#: takes the sixth.
AGENDA_TOP, AGENDA_BOTTOM = 7, 31
#: Left channel, holding the hour labels. Two digits of the 3x5 font reach
#: x=6, so eight leaves a single pixel of air before the boxes — nine left
#: two, which bought nothing and cost the boxes a column.
#:
#: Tick marks lived here first and said nothing about *which* hour, so the
#: column had shape but no anchor.
AGENDA_GUTTER = 8
#: How far a finished event is dimmed. Still legible as shape, never
#: mistakable for something still to come.
AGENDA_PAST = 0.35


def _agenda_span(ctx: PageContext, now: datetime) -> tuple[float, float]:
    """Hours of past to show, and hours the whole column spans.

    The length is fixed, from the configured minutes per row: a half-hour
    meeting has to be the same two rows at eight in the morning as at six in
    the evening, or the column stops being a scale and becomes a diagram.

    Only the line moves. It sits at `back / window` down the column, so
    "never past half way" is a rule about those two together — not, as a
    first attempt had it, a cap on the hours of past alone, which put the
    line at seventy per cent on a crowded morning.

    What this gives up is reach: beyond the window there is nothing, so about
    a fifth of the day's remaining events are not drawn until they come
    closer. The scale was judged worth more than the horizon.
    """
    config = ctx.config.agenda
    window = (AGENDA_BOTTOM - AGENDA_TOP) * config.minutes_per_row / 60

    behind = [e for e in ctx.events if e.end <= now]
    if behind:
        oldest = min(e.start for e in behind)
        wanted = (now - oldest).total_seconds() / 3600
    else:
        wanted = 0.0
    back = max(config.look_back_min_hours, min(wanted, window / 2))
    return back, window


def _agenda_label_every(window: float) -> int:
    """How many hours between labels, given how much room an hour has.

    A fixed rule does not work across a window that stretches from two and a
    half hours to eight: every two hours left the short window showing a
    single number, which is an anchor easy to miss, and every hour would
    overlap itself on the long one.
    """
    rows_per_hour = (AGENDA_BOTTOM - AGENDA_TOP) / window
    if rows_per_hour >= 7:
        return 1
    return 2 if rows_per_hour >= 3.5 else 3


def _agenda_row(moment: datetime, start: datetime, window: float) -> float:
    hours = (moment - start).total_seconds() / 3600
    return AGENDA_TOP + (hours / window) * (AGENDA_BOTTOM - AGENDA_TOP)


def _agenda_fits(left: int, right: int = 32) -> int:
    """How many 3x5 glyphs fit between two columns.

    Each advances four pixels but only paints three, so the last one needs no
    trailing pixel — counting every glyph as four undercounted the room by
    one whenever the space divided awkwardly.
    """
    return max(0, (right - left + 1) // 4)


def _agenda_minutes_until(start: datetime, now: datetime) -> int:
    """Minutes to go, rounded up, so the count never promises less time.

    Floor division is the obvious thing and it is wrong here: with a minute
    and fifty-nine seconds to go it printed "1", and anything under a minute
    printed "0" — a nought that then sat there for the best part of a minute
    saying nothing. Rounded up, "1" means "under a minute, now-ish", which is
    what someone reading a countdown takes it to mean.
    """
    seconds = (start - now).total_seconds()
    return -int(-seconds // 60)


def _agenda_header(canvas: Canvas, nxt, now: datetime) -> None:
    """The next thing by name — but only once it is nearly here.

    A name three hours out is noise: it will have rotated past a dozen times
    before it matters, and by then it has stopped being read. So the header
    stays quiet until the thing is inside the hour or already running, and
    shows the date instead. The minutes appear alongside, because they are
    the one thing the column cannot express.

    The colour bar is two pixels wide because one, beside a glyph, reads as
    a 1: an early header showed 8:30 and it looked like 18:30.
    """
    minutes = None if nxt is None else _agenda_minutes_until(nxt.start, now)
    close = nxt is not None and minutes < 60      # running counts: minutes <= 0

    if not close:
        canvas.text(1, 0, now.strftime("%a").upper(), FONT_3x5, AGENDA_HOUR)
        canvas.text_right(31, 0, now.strftime("%d"), FONT_3x5, AGENDA_HOUR)
        return

    canvas.rect(0, 0, 2, 5, nxt.color)
    countdown = str(minutes) if minutes > 0 else ""
    if countdown:
        # One colour throughout. A red threshold was tried and dropped: the
        # number shrinking towards zero already says how close it is, and a
        # second signal saying the same thing only competes for attention
        # with the colour bar, which carries information nothing else does.
        canvas.text_right(31, 0, countdown, FONT_3x5, AGENDA_INK)

    # Whatever is left over, rather than a fixed seven: the glyphs are four
    # pixels with their tracking, and the gap keeps name and count apart.
    # Same reasoning on the other side: the countdown is a separate field, so
    # it keeps a pixel of its own beyond the font's tracking.
    canvas.text(3, 0,
                (nxt.title or nxt.calendar)[
                    :_agenda_fits(3, 32 - (len(countdown) * 4 + 1 if countdown else 0))
                ].upper(),
                FONT_3x5, AGENDA_NOW)


def _agenda_short_time(moment: datetime) -> str:
    """Drop what can be dropped: the leading zero, and ":00" on the hour.

    Worth four pixels a glyph, and most things start on the hour — which in
    a list buys the difference between "DE" and "DEMO".
    """
    if moment.minute == 0:
        return str(moment.hour)
    return f"{moment.hour}:{moment.minute:02d}"


def _agenda_ahead(ctx: PageContext, today: date) -> Canvas:
    """The next day with anything in it, as a list.

    Not boxes. The column exists to answer "where am I in the day", and a day
    you are not in has no now to place — its shape says nothing while its
    times and names say everything.

    Nor necessarily tomorrow: on a Friday evening tomorrow is empty and the
    useful answer is Monday, so the title has to name the day rather than
    assume it.
    """
    canvas = Canvas()
    canvas.hline(0, 6, 32, AGENDA_DIM)

    if ctx.ahead is None:
        canvas.text(0, 0, "AHEAD", FONT_3x5, AGENDA_HOUR)
        canvas.text_centered(16, "CLEAR", FONT_3x5, AGENDA_DIM)
        return canvas

    day, events = ctx.ahead
    if (day - today).days == 1:
        canvas.text(0, 0, "TOMORROW", FONT_3x5, AGENDA_HOUR)
    else:
        canvas.text(0, 0, day.strftime("%a").upper(), FONT_3x5, AGENDA_HOUR)
        canvas.text_right(31, 0, day.strftime("%d"), FONT_3x5, AGENDA_HOUR)

    shown = events[:AGENDA_SUMMARY_ROWS]
    for row, event in enumerate(shown):
        y = 8 + row * 6
        canvas.rect(0, y, 2, 5, event.color)
        clock = _agenda_short_time(event.start)
        canvas.text(3, y, clock, FONT_3x5, AGENDA_INK)
        # Two pixels, deliberately. The font already advances one past each
        # glyph, so a single pixel here makes the jump from the time to the
        # name identical to the jump between two letters of the name — and
        # "10MURPR" stops having anywhere for the eye to break. The extra
        # pixel is what marks these as two different fields.
        used = 3 + len(clock) * 4 + 1
        canvas.text(used, y, (event.title or event.calendar)[:_agenda_fits(used)].upper(),
                    FONT_3x5, AGENDA_NOW)

    # Cutting the list in silence is a lie by omission: seven events showed
    # four and looked like a four-event day.
    hidden = len(events) - len(shown)
    if hidden:
        canvas.text_right(31, 8 + AGENDA_SUMMARY_ROWS * 6,
                          f"+{hidden}", FONT_3x5, AGENDA_HOUR)
    return canvas


def agenda(ctx: PageContext) -> Canvas:
    """Today as a column — or tomorrow as a list, once today is spent."""
    canvas = Canvas()
    now = ctx.local_now()

    nxt = next((e for e in ctx.events if e.end > now), None)
    # Spent is not enough on its own: at half past seven the evening is still
    # ahead and the next working day is not yet the useful thing to read.
    if nxt is None and now.time() >= ctx.config.agenda.summary_after:
        return _agenda_ahead(ctx, now.date())
    _agenda_header(canvas, nxt, now)
    canvas.hline(0, 5, 32, AGENDA_DIM)

    back, window = _agenda_span(ctx, now)
    visible_start = now - timedelta(hours=back)

    every = _agenda_label_every(window)
    hour = visible_start.replace(minute=0, second=0, microsecond=0)
    while hour <= visible_start + timedelta(hours=window):
        y = _agenda_row(hour, visible_start, window)
        if AGENDA_TOP <= y and y + 5 <= AGENDA_BOTTOM and hour.hour % every == 0:
            canvas.text(0, int(y), f"{hour.hour:02d}", FONT_3x5, AGENDA_HOUR)
        hour += timedelta(hours=1)

    for event in ctx.events:
        top = _agenda_row(event.start, visible_start, window)
        bottom = _agenda_row(event.end, visible_start, window)
        if bottom < AGENDA_TOP or top > AGENDA_BOTTOM:
            continue
        top, bottom = max(AGENDA_TOP, top), min(AGENDA_BOTTOM, bottom)
        colour = event.color
        if event.end <= now:
            colour = tuple(int(c * AGENDA_PAST) for c in colour)
        canvas.rect(AGENDA_GUTTER, int(top), 32 - AGENDA_GUTTER,
                    max(1, int(round(bottom - top))), colour)

    # A mark at the right edge rather than a rule across the column. Drawn
    # full width it crossed the hour labels and dirtied the digits, which is
    # the one thing in the channel that has to stay crisp.
    #
    # The cost, and it is a real one: three pixels against black are easy to
    # miss, and this used to be the first thing the eye found. Against a box
    # it reads fine, and that is the trade this makes deliberately.
    canvas.rect(32 - AGENDA_MARK, int(_agenda_row(now, visible_start, window)),
                AGENDA_MARK, 1, AGENDA_NOW)
    return canvas


PAGES = {
    "clock_local": clock_local,
    "world_clocks": world_clocks,
    "weather_now": weather_now,
    "forecast": forecast,
    "trend": trend,
    "alert": alert,
    "agenda": agenda,
}


def render(name: str, ctx: PageContext) -> Canvas:
    """Draw a page, then drop it to night level if the sun is down here.

    The whole-page dimming lives out here rather than in each page so that
    every page gets it for free — and so that adding a page cannot forget it.
    """
    try:
        page = PAGES[name]
    except KeyError:
        raise KeyError(
            f"unknown page {name!r}; available: {', '.join(sorted(PAGES))}"
        ) from None

    canvas = page(ctx)
    if name not in SELF_DIMMING and ctx.is_night_here():
        canvas.scale(night_factor(ctx.config.display))
    return canvas
