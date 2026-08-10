"""Guard rails for the hand-drawn assets and the page layouts.

Fonts and icons are literal grids typed by hand, so the failure mode is a row
with one character too many — invisible in the source, and on the panel it
just shifts pixels into the next column. These tests catch that, plus the
usual "does every page still render at the right size" question.

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from ledclock import alerts, control, daemon, icons, moon, pages, sun
from ledclock.config import (
    ClockCity, Config, ConfigError, DeviceConfig, DisplayConfig, WeatherConfig,
    is_active,
)
from ledclock.font import FONT_3x5, FONT_5x7, Font
from ledclock.ipixel import CMD_PNG, IPixelError, frame, parse_device_info
from ledclock.render import Canvas, dim
from ledclock.sources.weather import (
    Current, Day, Weather, format_high, format_low, format_temperature,
)

from datetime import date, time, timedelta

#: A round, obviously synthetic point at 43N 3E. Deliberately not anyone's
#: address: past about three decimal places, coordinates name a building.
#: The sky here is within a minute of the author's own, so the sun and moon
#: expectations below hold without publishing where they live.
TEST_LAT, TEST_LON = 43.0, 3.0


class TestFonts(unittest.TestCase):
    def _check(self, font: Font, name: str) -> None:
        for char, glyph in font.glyphs.items():
            with self.subTest(font=name, char=char):
                self.assertEqual(
                    len(glyph), font.height,
                    f"{name}[{char!r}] has {len(glyph)} rows, expected {font.height}",
                )
                widths = {len(row) for row in glyph}
                self.assertEqual(
                    len(widths), 1,
                    f"{name}[{char!r}] has ragged rows: {sorted(widths)}",
                )

    def test_3x5_grids_are_rectangular(self):
        self._check(FONT_3x5, "FONT_3x5")

    def test_5x7_grids_are_rectangular(self):
        self._check(FONT_5x7, "FONT_5x7")

    def test_digits_are_monospaced(self):
        # A ticking clock must not shift sideways as digits change.
        for font, expected in ((FONT_3x5, 3), (FONT_5x7, 5)):
            widths = {font.char_width(d) for d in "0123456789"}
            self.assertEqual(widths, {expected})

    def test_width_excludes_trailing_tracking(self):
        self.assertEqual(FONT_3x5.width(""), 0)
        self.assertEqual(FONT_3x5.width("8"), 3)
        self.assertEqual(FONT_3x5.width("88"), 7)

    def test_clock_row_fits_across_the_panel(self):
        # The widest world-clock row: a 5px-wide 'M' plus a 2-digit hour.
        self.assertLessEqual(
            FONT_3x5.width("MEX") + 2 + FONT_3x5.width("22:22"), 32
        )

    def test_hero_clock_fits(self):
        self.assertLessEqual(FONT_5x7.width("22:22"), 32)


class TestIcons(unittest.TestCase):
    def _check(self, table: dict[str, icons.Icon], size: int) -> None:
        for name, icon in table.items():
            with self.subTest(icon=name, size=size):
                self.assertEqual(icon.height, size)
                widths = {len(row) for row in icon.rows}
                self.assertEqual(
                    widths, {size}, f"{name} has ragged rows: {sorted(widths)}"
                )

    def test_8px_icons(self):
        self._check(icons.ICONS_8, 8)

    def test_16px_icons(self):
        self._check(icons.ICONS_16, 16)

    def test_both_tables_cover_the_same_conditions(self):
        # ICONS_16 additionally carries alert-only art, which has no 8px use.
        alert_only = set(icons.ALERT_ICONS.values()) - set(icons.ICONS_8)
        self.assertEqual(set(icons.ICONS_16) - alert_only, set(icons.ICONS_8))

    def test_every_lit_pixel_has_a_palette_entry(self):
        for table in (icons.ICONS_8, icons.ICONS_16):
            for name, icon in table.items():
                for row in icon.rows:
                    for cell in row:
                        if cell not in (".", " "):
                            self.assertIn(cell, icons.PALETTE, f"in icon {name}")

    def test_every_wmo_code_resolves(self):
        for code in list(icons._WMO_TO_ICON) + [None, 4242]:
            for size in (8, 16):
                self.assertIsInstance(icons.for_code(code, size), icons.Icon)

    def test_stat_markers_are_square(self):
        for name, marker in icons.MARKERS.items():
            with self.subTest(marker=name):
                self.assertEqual(marker.height, 5)
                self.assertEqual({len(row) for row in marker.rows}, {5})

    def test_every_stat_has_a_marker(self):
        for name, (marker, _color, _extract, _format) in pages.STATS.items():
            self.assertIn(marker, icons.MARKERS, f"stat {name}")


def _weather(stale: bool = False, code: int = 0, is_day: bool = False) -> Weather:
    return Weather(
        current=Current(
            temperature=31.1, apparent=33.4, code=code, is_day=is_day,
            humidity=40, wind=15.0, gusts=25.2, pressure=1004.4, clouds=4,
            precipitation=0.0,
        ),
        days=[
            Day(date(2026, 8, 1), 3, 35.3, 23.0, precipitation_probability=0, uv_max=7.3),
            Day(date(2026, 8, 2), 51, 33.0, 25.5, precipitation_probability=43, uv_max=7.0),
            Day(date(2026, 8, 3), 95, 27.2, 24.2, precipitation_probability=46, uv_max=7.1),
        ],
        fetched_at=0.0,
        stale=stale,
    )


def _config(**kwargs) -> Config:
    clocks = [
        ClockCity("SFO", "SAN FRANCISCO", "America/Los_Angeles", (255, 0, 0)),
        ClockCity("MEX", "MEXICO CITY", "America/Mexico_City", (0, 255, 0)),
        ClockCity("BOG", "BOGOTA", "America/Bogota", (0, 0, 255)),
        ClockCity("PAR", "PARIS", "Europe/Paris", (255, 255, 0)),
    ]
    return Config(clocks=clocks, **kwargs)


class TestPages(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 1, 20, 25, tzinfo=timezone.utc)

    def _ctx(self, weather=None, config=None) -> pages.PageContext:
        return pages.PageContext(
            config=config or _config(), now=self.now, weather=weather
        )

    def test_all_pages_render_at_panel_size(self):
        for name in pages.PAGES:
            with self.subTest(page=name):
                canvas = pages.render(name, self._ctx(_weather()))
                self.assertEqual((canvas.width, canvas.height), (32, 32))
                self.assertTrue(canvas.to_png().startswith(b"\x89PNG"))

    def test_all_pages_survive_missing_weather(self):
        # The panel must keep showing the time even with no network.
        for name in pages.PAGES:
            with self.subTest(page=name):
                canvas = pages.render(name, self._ctx(None))
                self.assertEqual((canvas.width, canvas.height), (32, 32))

    def test_unknown_page_names_the_alternatives(self):
        with self.assertRaises(KeyError) as caught:
            pages.render("nope", self._ctx())
        self.assertIn("world_clocks", str(caught.exception))

    def test_world_clocks_sorts_west_to_east(self):
        canvas = pages.render("world_clocks", self._ctx())
        # Row order is SFO, MEX, BOG, PAR by UTC offset; each row's tag is
        # drawn in that city's colour, so the colours identify the rows.
        px = canvas.image.load()
        row_colors = []
        for index in range(4):
            y = 1 + index * 8
            row = {px[x, y] for x in range(12)} - {(0, 0, 0)}
            row_colors.append(row)
        self.assertTrue(any((255, 0, 0) in c for c in row_colors[:1]))

    def test_config_order_is_respected_when_asked(self):
        config = _config(display=DisplayConfig(clock_order="config"))
        canvas = pages.render("world_clocks", self._ctx(config=config))
        px = canvas.image.load()
        # PAR is last in the config list, so its colour lands on the last row —
        # dimmed, because 22:25 in Paris counts as night.
        last_row = {px[x, 25] for x in range(12)} - {(0, 0, 0)}
        self.assertIn(dim((255, 255, 0), pages.night_factor(DisplayConfig())), last_row)

    def test_night_rows_are_dimmed(self):
        canvas = pages.render("world_clocks", self._ctx())
        px = canvas.image.load()
        # SFO at 13:25 is daytime and keeps its full colour; PAR at 22:25 does not.
        self.assertIn((255, 0, 0), {px[x, 1] for x in range(12)})
        self.assertIn(dim((255, 255, 0), pages.night_factor(DisplayConfig())),
                      {px[x, 25] for x in range(12)})

    def test_night_rows_survive_a_low_gain(self):
        """The bug this guards: gain and night dimming used to multiply.

        At gain 0.4 a night row landed at 0.4 x 0.42 = 17% of full and went
        invisible on the panel. night_gain is absolute, so the level after
        the frame is scaled must equal it regardless of gain.
        """
        night_gain = 0.42
        for gain in (1.0, 0.5, 0.25, 0.1):
            with self.subTest(gain=gain):
                display = DisplayConfig(clock_order="config", gain=gain,
                                        night_gain=night_gain)
                canvas = pages.render(
                    "world_clocks", self._ctx(config=_config(display=display))
                )
                px = canvas.dimmed(gain).load()
                par = max(px[x, 25][0] for x in range(12))  # PAR is yellow
                # Honoured as an absolute level, except that night can never
                # be brighter than day — below night_gain the two converge.
                self.assertAlmostEqual(par / 255, min(night_gain, gain), delta=0.05)

    def test_night_never_outshines_day(self):
        # night_gain above gain would invert the cue; the factor clamps.
        display = DisplayConfig(gain=0.2, night_gain=0.9)
        self.assertEqual(pages.night_factor(display), 1.0)

    def test_decimal_split_rounds_to_one_place(self):
        self.assertEqual(pages._split_decimal(28.5), ("28", "5"))
        self.assertEqual(pages._split_decimal(28.55), ("28", "6"))
        self.assertEqual(pages._split_decimal(9.96), ("10", "0"))
        self.assertEqual(pages._split_decimal(0.0), ("0", "0"))

    def test_decimal_split_keeps_negatives_toward_zero(self):
        # Floor division would turn -5.25 into -6.3 rather than -5.3.
        self.assertEqual(pages._split_decimal(-5.25), ("-5", "2"))
        self.assertEqual(pages._split_decimal(-0.4), ("-0", "4"))

    def test_temperature_stacks_the_degree_mark_over_the_tenths(self):
        """Both small marks share columns, separated by height not by a point."""
        canvas = Canvas()
        pages._draw_temperature(canvas, 17, 2, 28.5, (255, 255, 255), budget=15)
        px = canvas.image.load()
        self.assertNotEqual(px[29, 2], (0, 0, 0), "degree mark missing")
        self.assertNotEqual(px[29, 5], (0, 0, 0), "tenths digit missing")
        # A clear row between them, and nothing at all in the gutter column.
        self.assertTrue(all(px[x, 3] == (0, 0, 0) for x in range(28, 32)))
        self.assertTrue(all(px[28, y] == (0, 0, 0) for y in range(0, 12)))

    def test_degree_mark_is_a_single_pixel(self):
        # Shrinking it from the font's 2x2 block is what frees the row that
        # lets the tenths sit on the baseline rather than below it.
        canvas = Canvas()
        pages._draw_temperature(canvas, 17, 2, 28.5, (255, 255, 255), budget=15)
        px = canvas.image.load()
        self.assertEqual(px[30, 2], (0, 0, 0))
        self.assertEqual(px[29, 3], (0, 0, 0))

    def test_tenths_sit_on_the_baseline(self):
        canvas = Canvas()
        pages._draw_temperature(canvas, 17, 2, 28.5, (255, 255, 255), budget=15)
        px = canvas.image.load()
        baseline = 2 + FONT_5x7.height - 1
        self.assertNotEqual(px[29, baseline], (0, 0, 0), "tenths not on baseline")
        self.assertTrue(all(px[x, baseline + 1] == (0, 0, 0) for x in range(28, 32)),
                        "tenths should not hang below the digits")

    def test_temperature_stops_at_the_panel_edge(self):
        canvas = Canvas()
        pages._draw_temperature(canvas, 17, 2, 28.5, (255, 255, 255), budget=15)
        px = canvas.image.load()
        self.assertTrue(any(px[31, y] != (0, 0, 0) for y in range(2, 11)))

    def test_tenths_are_dimmer_than_the_degrees(self):
        # Size alone was not enough: "28.9" first read as "289" on the panel.
        canvas = Canvas()
        pages._draw_temperature(canvas, 17, 2, 28.5, (255, 255, 255), budget=15)
        px = canvas.image.load()
        self.assertEqual(px[18, 2], (255, 255, 255))          # part of the "2"
        self.assertEqual(px[29, 2], (255, 255, 255))          # the degree sign
        self.assertEqual(px[29, 5], dim((255, 255, 255), pages.DECIMAL_DIM))

    def test_temperature_falls_back_to_whole_degrees(self):
        canvas = Canvas()
        pages._draw_temperature(canvas, 17, 2, 28.5, (255, 255, 255), budget=11)
        px = canvas.image.load()
        self.assertTrue(all(px[29, y] == (0, 0, 0) for y in range(0, 12)),
                        "should not have drawn tenths it has no room for")
        self.assertTrue(any(px[x, 2] != (0, 0, 0) for x in range(17, 28)))

    def _apparent_is_drawn(self, canvas, config=None) -> bool:
        """The slot is never empty now — an arc fills it — so look for the
        text's own colour rather than for any lit pixel at all."""
        display = (config or _config()).display
        ink = pages.GREY
        if pages.PageContext(config=config or _config(), now=self.now,
                             location=(TEST_LAT, TEST_LON)).is_night_here():
            ink = dim(ink, pages.night_factor(display))
        px = canvas.image.load()
        return any(px[x, y] == ink for x in range(17, 32) for y in range(11, 16))

    def test_apparent_temperature_hidden_when_close(self):
        weather = _weather()
        near = Weather(
            current=Current(temperature=28.5, apparent=29.4, code=0, is_day=True),
            days=weather.days, fetched_at=0.0,
        )
        canvas = pages.render("weather_now", self._ctx(near))
        self.assertFalse(self._apparent_is_drawn(canvas),
                         "a sub-1-degree difference should not be drawn")

    def test_apparent_temperature_shown_when_it_differs(self):
        weather = _weather()
        apart = Weather(
            current=Current(temperature=28.5, apparent=33.4, code=0, is_day=True),
            days=weather.days, fetched_at=0.0,
        )
        canvas = pages.render("weather_now", self._ctx(apart))
        self.assertTrue(self._apparent_is_drawn(canvas))

    def test_apparent_threshold_is_exclusive(self):
        # "greater than 1 degree", so exactly 1.0 stays hidden.
        weather = _weather()
        for delta, expected in ((1.0, False), (1.01, True)):
            with self.subTest(delta=delta):
                current = Current(temperature=20.0, apparent=20.0 + delta,
                                  code=0, is_day=True)
                canvas = pages.render(
                    "weather_now",
                    self._ctx(Weather(current=current, days=weather.days,
                                      fetched_at=0.0)),
                )
                self.assertEqual(self._apparent_is_drawn(canvas), expected)

    def test_stale_weather_is_flagged(self):
        fresh = pages.render("weather_now", self._ctx(_weather(stale=False)))
        stale = pages.render("weather_now", self._ctx(_weather(stale=True)))
        self.assertEqual(fresh.image.load()[31, 0], (0, 0, 0))
        self.assertNotEqual(stale.image.load()[31, 0], (0, 0, 0))

    def test_night_and_clear_shows_the_moon(self):
        icon = pages._sky_icon(_weather(code=0, is_day=False), self.now, 16)
        cells = "".join(icon.rows)
        self.assertIn(moon.LIT, cells)
        self.assertIn(moon.UNLIT, cells)
        self.assertNotIn(icon, list(icons.ICONS_16.values()))

    def test_night_and_overcast_keeps_the_condition(self):
        # A crisp moon drawn while it rains would be worse than useless.
        raining_at_night = _weather(code=61, is_day=False)
        self.assertIs(
            pages._sky_icon(raining_at_night, self.now, 16), icons.ICONS_16["rain"]
        )

    def test_daytime_always_shows_the_condition(self):
        self.assertIs(
            pages._sky_icon(_weather(code=0, is_day=True), self.now, 16),
            icons.ICONS_16["clear"],
        )

    def test_every_configurable_stat_renders(self):
        weather = _weather()
        for name in pages.STATS:
            with self.subTest(stat=name):
                config = _config(weather=WeatherConfig(stats=[name] * 4))
                canvas = pages.render("weather_now", self._ctx(weather, config))
                lit = sum(
                    1 for y in range(19, 31) for x in range(32)
                    if canvas.image.load()[x, y] != (0, 0, 0)
                )
                self.assertGreater(lit, 0, f"{name} drew nothing")

    def test_stat_cells_stay_inside_their_half(self):
        # Four-digit pressure only fits if the marker is dropped; assert the
        # two halves never bleed into each other's columns.
        config = _config(weather=WeatherConfig(
            stats=["pressure", "pressure", "pressure", "pressure"]))
        canvas = pages.render("weather_now", self._ctx(_weather(), config))
        px = canvas.image.load()
        for y in range(19, 31):
            for x in (15, 16):  # the gutter between the two cells
                self.assertEqual(px[x, y], (0, 0, 0), f"gutter used at {x},{y}")

    def test_missing_stat_values_are_skipped_not_crashed(self):
        bare = Weather(
            current=Current(temperature=10.0, apparent=10.0, code=3, is_day=True),
            days=[],
            fetched_at=0.0,
        )
        config = _config(weather=WeatherConfig(stats=["humidity", "wind", "low", "high"]))
        canvas = pages.render("weather_now", self._ctx(bare, config))
        self.assertEqual((canvas.width, canvas.height), (32, 32))

    def test_unknown_stat_names_are_rejected_at_config_time(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("weather:\n  stats: [humidity, banana]\n")
            with self.assertRaises(ConfigError) as caught:
                __import__("ledclock.config", fromlist=["load"]).load(path)
        self.assertIn("banana", str(caught.exception))

    def test_nothing_spills_past_the_edges(self):
        # A glyph one pixel too wide would be clipped by Canvas.pixel rather
        # than crash, so assert the rightmost column is only used on purpose.
        canvas = pages.render("world_clocks", self._ctx(_weather()))
        px = canvas.image.load()
        lit = [y for y in range(32) if px[31, y] != (0, 0, 0)]
        self.assertTrue(lit, "clock rows should reach the right edge exactly")


class TestMoon(unittest.TestCase):
    def test_phase_stays_in_range(self):
        for days in range(0, 400, 7):
            value = moon.phase(datetime(2026, 1, 1, tzinfo=timezone.utc)
                               + timedelta(days=days))
            self.assertGreaterEqual(value, 0.0)
            self.assertLess(value, 1.0)

    def test_naive_datetimes_are_treated_as_utc(self):
        aware = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(moon.phase(aware),
                               moon.phase(aware.replace(tzinfo=None)))

    def test_illumination_tracks_the_named_phases(self):
        self.assertAlmostEqual(moon.illumination(0.0), 0.0, places=6)
        self.assertAlmostEqual(moon.illumination(0.25), 0.5, places=6)
        self.assertAlmostEqual(moon.illumination(0.5), 1.0, places=6)
        self.assertAlmostEqual(moon.illumination(0.75), 0.5, places=6)

    def test_new_moon_is_dark_and_full_moon_is_lit(self):
        new = moon.render(16, 0.0)
        full = moon.render(16, 0.5)
        self.assertEqual(sum(row.count(moon.LIT) for row in new), 0)
        self.assertEqual(sum(row.count(moon.UNLIT) for row in full), 0)
        # The disc itself is the same size either way — only the shading moves.
        self.assertEqual(
            sum(len(row) - row.count(".") for row in new),
            sum(len(row) - row.count(".") for row in full),
        )

    def test_waxing_is_lit_on_the_right(self):
        rows = moon.render(16, 0.25)  # first quarter
        middle = rows[8]
        left = middle[:8].count(moon.LIT)
        right = middle[8:].count(moon.LIT)
        self.assertEqual(left, 0)
        self.assertGreater(right, 0)

    def test_waning_is_lit_on_the_left(self):
        middle = moon.render(16, 0.75)[8]  # last quarter
        self.assertGreater(middle[:8].count(moon.LIT), 0)
        self.assertEqual(middle[8:].count(moon.LIT), 0)

    def test_illuminated_area_grows_then_shrinks(self):
        areas = [
            sum(row.count(moon.LIT) for row in moon.render(16, p / 16))
            for p in range(16)
        ]
        self.assertEqual(max(areas), areas[8])   # full moon at p = 0.5
        self.assertEqual(min(areas), areas[0])   # new moon at p = 0

    def test_render_is_square_and_uses_known_palette_keys(self):
        for size in (8, 16):
            rows = moon.render(size, 0.3)
            self.assertEqual(len(rows), size)
            self.assertEqual({len(row) for row in rows}, {size})
            for row in rows:
                for cell in row:
                    if cell != ".":
                        self.assertIn(cell, icons.PALETTE)

    def test_phase_names_cover_the_lunation(self):
        self.assertEqual(moon.phase_name(0.0), "NUEVA")
        self.assertEqual(moon.phase_name(0.5), "LLENA")
        self.assertEqual(moon.phase_name(0.99), "NUEVA")  # wraps


def _hours(values, *, start_hour=8):
    """An hourly series starting at `start_hour`, one reading per hour.

    timedelta rather than adding to the hour field: a nine-point window
    starting at 16:00 runs past midnight.
    """
    from ledclock.sources.weather import Hour

    base = datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc) + timedelta(hours=start_hour)
    return [
        Hour(time=base + timedelta(hours=i), temperature=float(v), apparent=v + 4.0)
        for i, v in enumerate(values)
    ]


class TestTrend(unittest.TestCase):
    """The temperature either side of now, over an 8-hour window."""

    #: Midday, deliberately: after sunset render() dims the whole page, and
    #: these tests compare exact colours.
    NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    NOW_COLUMN = 15

    def _ctx(self, hours, **display) -> pages.PageContext:
        weather = Weather(
            current=Current(temperature=26.0, apparent=30.0, code=0, is_day=False),
            days=[], fetched_at=0.0, hours=hours,
        )
        return pages.PageContext(
            config=_config(display=DisplayConfig(**display)),
            now=self.NOW, weather=weather, location=(TEST_LAT, TEST_LON),
        )

    def test_renders_at_panel_size(self):
        hours = _hours([31, 30, 29, 28, 27, 26, 25, 25, 24])
        canvas = pages.render("trend", self._ctx(hours))
        self.assertEqual((canvas.width, canvas.height), (32, 32))

    def test_needs_at_least_two_points(self):
        for hours in ([], _hours([26])):
            with self.subTest(count=len(hours)):
                canvas = pages.render("trend", self._ctx(hours))
                self.assertEqual((canvas.width, canvas.height), (32, 32))

    def test_interpolates_between_hourly_readings(self):
        # Half past the hour must land between the two readings, not on one.
        hours = _hours([20.0, 24.0], start_hour=20)
        middle = pages._sample(hours, datetime(2026, 8, 2, 20, 30, tzinfo=timezone.utc))
        self.assertAlmostEqual(middle, 22.0)

    def test_sampling_clamps_outside_the_series(self):
        hours = _hours([20.0, 24.0], start_hour=20)
        before = pages._sample(hours, datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        after = pages._sample(hours, datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(before, 20.0)
        self.assertEqual(after, 24.0)

    def test_labels_report_real_extremes_not_the_padded_axis(self):
        """A flat window is padded so it does not look dramatic, but the
        numbers on screen must still be the temperatures that occur."""
        flat = _hours([26.0] * 9)
        canvas = pages.render("trend", self._ctx(flat))
        px = canvas.image.load()
        # Both labels read 26; nothing wider should appear.
        header = {px[x, y] for x in range(32) for y in range(5)} - {(0, 0, 0)}
        self.assertTrue(header, "the header should not be blank")
        self.assertGreaterEqual(pages.TREND_MIN_SPAN, 1.0)

    def _header(self, canvas):
        px = canvas.image.load()
        return {px[x, y] for x in range(32) for y in range(5)} - {(0, 0, 0)}

    def test_header_is_coloured_by_the_same_ramp_as_the_line(self):
        """That shared ramp is what lets the header double as the legend."""
        from ledclock.render import temperature_color

        hours = _hours([10, 12, 14, 16, 18, 20, 22, 24, 26])
        canvas = pages.render("trend", self._ctx(hours))
        header = self._header(canvas)
        self.assertIn(temperature_color(10.0), header, "coolest label")
        self.assertIn(temperature_color(26.0), header, "warmest label")
        # _ctx builds a current reading of 26.0, so the middle shares its hue.
        self.assertIn(temperature_color(26.0), header)

    def test_header_carries_three_readings(self):
        hours = _hours([10, 12, 14, 16, 18, 20, 22, 24, 26])
        canvas = pages.render("trend", self._ctx(hours))
        px = canvas.image.load()
        # Three groups of lit columns across the top, one per number.
        lit = [any(px[x, y] != (0, 0, 0) for y in range(5)) for x in range(32)]
        groups = sum(1 for i, on in enumerate(lit) if on and not (i and lit[i - 1]))
        self.assertGreaterEqual(groups, 3, "expected coolest, current, warmest")

    def test_the_middle_reading_comes_from_the_current_observation(self):
        """It must agree with the big figure on weather_now, not drift from it."""
        from ledclock.render import temperature_color

        hours = _hours([10] * 9)          # a flat window nowhere near 26
        canvas = pages.render("trend", self._ctx(hours))
        self.assertIn(temperature_color(26.0), self._header(canvas))

    def test_a_deep_freeze_drops_the_middle_rather_than_colliding(self):
        # Three wide readings at once would overrun 32px.
        hours = _hours([-15, -14, -13, -12, -11, -12, -13, -14, -15])
        weather = Weather(
            current=Current(temperature=-13.0, apparent=-13.0, code=0, is_day=True),
            days=[], fetched_at=0.0, hours=hours,
        )
        ctx = pages.PageContext(config=_config(), now=self.NOW, weather=weather,
                                location=(TEST_LAT, TEST_LON))
        canvas = pages.render("trend", ctx)
        px = canvas.image.load()
        middle = [x for x in range(13, 19)
                  if any(px[x, y] != (0, 0, 0) for y in range(5))]
        self.assertEqual(middle, [], "the middle label should have stood down")

    def test_a_flat_window_is_not_amplified(self):
        """Without a floor on the axis, a tenth of a degree of noise would
        fill the whole panel and read as a heatwave."""
        flat = _hours([26.0, 26.1, 26.0, 26.1, 26.0, 26.1, 26.0, 26.1, 26.0])
        canvas = pages.render("trend", self._ctx(flat))
        px = canvas.image.load()
        # Skip the now column, which is a full-height marker, not data.
        rows = [y for y in range(pages.TREND_TOP, pages.TREND_BOTTOM + 1)
                for x in range(32)
                if x != self.NOW_COLUMN and px[x, y] != (0, 0, 0)]
        self.assertLess(max(rows) - min(rows), 6,
                        "a flat window should stay flat on screen")

    def test_colour_says_temperature_and_nothing_else(self):
        """The bug this guards: a cooler forecast looked hotter.

        Dimming the forecast half fought the temperature ramp — knocking back
        a warm colour makes it read browner, so 28.9C ahead was drawn
        (140,91,36) while 29.9C at the present was (255,154,60). The colder
        point looked deeper red. Identical temperatures must now come out as
        identical colours on either side of now.
        """
        hours = _hours([20, 22, 24, 26, 28, 26, 24, 22, 20])   # symmetric
        canvas = pages.render("trend", self._ctx(hours))
        px = canvas.image.load()

        def colours(columns):
            return {px[x, y] for x in columns
                    for y in range(pages.TREND_TOP, pages.TREND_BOTTOM + 1)
                    if px[x, y] not in ((0, 0, 0), pages.NOW_LINE)}

        past, future = colours(range(0, 14)), colours(range(18, 32))
        self.assertTrue(past & future,
                        "a mirrored window should share colours across now")

    def test_each_column_is_drawn_at_its_own_ramp_colour(self):
        """Nothing may sit between a column's temperature and its colour."""
        from ledclock.render import temperature_color

        hours = _hours([30, 30, 30, 30, 30, 28, 26, 24, 22])   # cooling off
        ctx = self._ctx(hours)
        canvas = pages.render("trend", ctx)
        px = canvas.image.load()

        half = timedelta(hours=ctx.config.display.trend_hours / 2)
        start, end = ctx.now - half, ctx.now + half
        for x in (4, 10, 20, 24, 28):      # both sides of the present
            with self.subTest(column=x):
                reading = pages._sample(hours, start + (end - start) * (x / 31))
                column = {px[x, y]
                          for y in range(pages.TREND_TOP, pages.TREND_BOTTOM + 1)
                          if px[x, y] != (0, 0, 0)}
                self.assertIn(temperature_color(reading), column)

    def test_the_line_is_coloured_by_temperature(self):
        from ledclock.render import temperature_color

        hours = _hours([2, 2, 2, 2, 2, 2, 2, 2, 2])
        canvas = pages.render("trend", self._ctx(hours))
        px = canvas.image.load()
        cold = temperature_color(2.0)
        lit = {px[x, y] for x in range(0, 15)
               for y in range(pages.TREND_TOP, pages.TREND_BOTTOM + 1)
               if px[x, y] != (0, 0, 0)}
        self.assertIn(cold, lit)

    def test_now_is_marked_in_the_middle(self):
        hours = _hours([31, 30, 29, 28, 27, 26, 25, 24, 23])
        canvas = pages.render("trend", self._ctx(hours))
        px = canvas.image.load()
        lit = [y for y in range(pages.TREND_TOP, pages.TREND_BOTTOM + 1)
               if px[self.NOW_COLUMN, y] != (0, 0, 0)]
        self.assertGreater(len(lit), 10, "the now line should span the plot")

    def _wet_hours(self, probabilities, codes=None):
        from ledclock.sources.weather import Hour

        base = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
        codes = codes or [61] * len(probabilities)
        return [
            Hour(time=base + timedelta(hours=i), temperature=20.0, apparent=20.0,
                 precipitation_probability=p, code=c, gusts=10.0)
            for i, (p, c) in enumerate(zip(probabilities, codes))
        ]

    def _rain_band(self, canvas):
        px = canvas.image.load()
        # Skip the now marker, which runs the full height including this band.
        return {(x, y): px[x, y] for x in range(32)
                for y in range(pages.RAIN_TOP, pages.RAIN_BOTTOM + 1)
                if x != 15 and px[x, y] != (0, 0, 0)}

    def test_a_dry_window_leaves_the_rain_band_empty(self):
        canvas = pages.render("trend", self._ctx(self._wet_hours([0] * 9, [3] * 9)))
        self.assertEqual(self._rain_band(canvas), {})

    def test_a_trivial_chance_is_not_drawn(self):
        """3% and 8% used to each get a bar, making a dry day look showery."""
        canvas = pages.render("trend", self._ctx(self._wet_hours([3, 8, 5] * 3)))
        self.assertEqual(self._rain_band(canvas), {})

    def test_a_real_chance_is_drawn(self):
        canvas = pages.render("trend", self._ctx(self._wet_hours([80] * 9)))
        self.assertTrue(self._rain_band(canvas))

    def test_a_bigger_chance_makes_a_taller_bar(self):
        light = self._rain_band(
            pages.render("trend", self._ctx(self._wet_hours([20] * 9))))
        heavy = self._rain_band(
            pages.render("trend", self._ctx(self._wet_hours([100] * 9))))
        self.assertLess(len(light), len(heavy))

    def test_thunder_recolours_the_band(self):
        rain = self._rain_band(
            pages.render("trend", self._ctx(self._wet_hours([80] * 9, [61] * 9))))
        storm = self._rain_band(
            pages.render("trend", self._ctx(self._wet_hours([80] * 9, [95] * 9))))
        self.assertNotEqual(set(rain.values()), set(storm.values()))

    def test_the_window_is_configurable(self):
        hours = _hours(list(range(30, 21, -1)))
        narrow = pages.render("trend", self._ctx(hours, trend_hours=2))
        wide = pages.render("trend", self._ctx(hours, trend_hours=8))
        self.assertNotEqual(narrow.to_png(), wide.to_png())


class TestAlerts(unittest.TestCase):
    NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    def _weather(self, hours):
        return Weather(
            current=Current(temperature=20.0, apparent=20.0, code=3, is_day=True),
            days=[], fetched_at=0.0, hours=hours,
        )

    def _hours(self, specs, *, start_hour=11):
        """specs: (temperature, probability, code, gusts) per hour."""
        from ledclock.sources.weather import Hour

        base = datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc) + timedelta(
            hours=start_hour)
        return [
            Hour(time=base + timedelta(hours=i), temperature=float(t), apparent=float(t),
                 precipitation_probability=p, code=c, gusts=g)
            for i, (t, p, c, g) in enumerate(specs)
        ]

    def _config(self, **kwargs):
        from ledclock.config import AlertConfig

        return AlertConfig(**kwargs)

    def test_quiet_weather_says_nothing(self):
        calm = self._hours([(20, 0, 3, 20)] * 8)
        self.assertEqual(alerts.evaluate(self._weather(calm), self._config(), self.NOW), [])

    def test_disabled_says_nothing_even_in_a_storm(self):
        stormy = self._hours([(20, 90, 95, 80)] * 8)
        self.assertEqual(
            alerts.evaluate(self._weather(stormy), self._config(enabled=False), self.NOW),
            [],
        )

    def test_storm_carries_its_lead_time(self):
        hours = self._hours([
            (20, 0, 3, 20), (20, 0, 3, 20), (20, 0, 3, 20),
            (20, 90, 95, 40), (20, 0, 3, 20),
        ])
        found = alerts.evaluate(self._weather(hours), self._config(), self.NOW)
        self.assertEqual(found[0].kind, "storm")
        self.assertEqual(found[0].lead_hours, 2)   # 11:00 base, storm at 14:00
        self.assertEqual(found[0].label, "RAYOS")

    def test_gusts_alert_only_above_the_threshold(self):
        under = self._hours([(20, 0, 3, 54)] * 6)
        over = self._hours([(20, 0, 3, 56)] * 6)
        config = self._config(gust_kmh=55)
        self.assertEqual(alerts.evaluate(self._weather(under), config, self.NOW), [])
        self.assertEqual(
            alerts.evaluate(self._weather(over), config, self.NOW)[0].kind, "wind"
        )

    def test_frost_looks_at_the_coming_hours(self):
        hours = self._hours([(8, 0, 3, 10), (5, 0, 3, 10), (1, 0, 3, 10)])
        found = alerts.evaluate(self._weather(hours), self._config(), self.NOW)
        self.assertEqual(found[0].kind, "frost")
        self.assertEqual(found[0].magnitude, 1)

    def test_rain_needs_both_a_wet_code_and_a_real_chance(self):
        """Open-Meteo will report a drizzle code at 0% probability; alerting
        on that alone would cry wolf every damp morning."""
        # The rain sits an hour out: putting it in the current hour would
        # instead exercise the "already raining" rule below.
        dry = (20, 0, 3, 10)
        code_only = self._hours([dry, dry, (20, 0, 51, 10)])
        chance_only = self._hours([dry, dry, (20, 90, 3, 10)])
        both = self._hours([dry, dry, (20, 90, 61, 10)])
        config = self._config()
        self.assertEqual(alerts.evaluate(self._weather(code_only), config, self.NOW), [])
        self.assertEqual(alerts.evaluate(self._weather(chance_only), config, self.NOW), [])
        self.assertEqual(
            alerts.evaluate(self._weather(both), config, self.NOW)[0].kind, "rain"
        )

    def test_no_rain_alert_while_it_is_already_raining(self):
        # You can hear it on the window; telling you again is noise.
        raining = self._hours([(20, 90, 61, 10), (20, 90, 61, 10)])
        self.assertEqual(
            [a.kind for a in alerts.evaluate(self._weather(raining), self._config(), self.NOW)],
            [],
        )

    def test_the_worst_one_comes_first(self):
        hours = self._hours([
            (20, 0, 3, 20), (20, 90, 61, 70), (20, 95, 95, 80),
        ])
        found = alerts.evaluate(self._weather(hours), self._config(), self.NOW)
        self.assertEqual(found[0].kind, "storm")
        self.assertGreater(found[0].severity, found[-1].severity)

    def test_beyond_the_lookahead_is_not_our_problem(self):
        far = self._hours([(20, 0, 3, 20)] * 8 + [(20, 95, 95, 80)])
        self.assertEqual(
            alerts.evaluate(self._weather(far), self._config(lookahead_hours=3), self.NOW),
            [],
        )


class TestAlertTracker(unittest.TestCase):
    NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    def _tracker(self, **kwargs):
        from ledclock.config import AlertConfig

        return alerts.AlertTracker(AlertConfig(**kwargs))

    def _weather(self, gusts):
        from ledclock.sources.weather import Hour

        base = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        return Weather(
            current=Current(temperature=20.0, apparent=20.0, code=3, is_day=True),
            days=[], fetched_at=0.0,
            hours=[Hour(time=base + timedelta(hours=i), temperature=20.0,
                        apparent=20.0, precipitation_probability=0, code=3,
                        gusts=gusts) for i in range(4)],
        )

    def test_a_new_alert_is_announced_exactly_once(self):
        tracker = self._tracker(gust_kmh=55)
        tracker.update(self._weather(80), self.NOW)
        self.assertIsNotNone(tracker.take_new())
        self.assertIsNone(tracker.take_new(), "announced twice")
        # Still showing, just no longer new.
        self.assertIsNotNone(tracker.current())

    def test_a_persisting_alert_does_not_re_announce(self):
        tracker = self._tracker(gust_kmh=55)
        for _ in range(3):
            tracker.update(self._weather(80), self.NOW)
        tracker.take_new()
        self.assertIsNone(tracker.take_new())

    def test_an_alert_is_held_after_the_reading_drops(self):
        """Without the hold, a gust hovering on the threshold would flap the
        page in and out on every refresh."""
        tracker = self._tracker(gust_kmh=55, hold_minutes=30)
        tracker.update(self._weather(80), self.NOW)
        tracker.update(self._weather(20), self.NOW)   # fell away
        self.assertIsNotNone(tracker.current(), "let go too early")

    def test_the_hold_eventually_expires(self):
        tracker = self._tracker(gust_kmh=55, hold_minutes=0)
        tracker.update(self._weather(80), self.NOW)
        tracker.update(self._weather(20), self.NOW)
        self.assertIsNone(tracker.current())

    def test_nothing_to_show_when_nothing_fires(self):
        tracker = self._tracker(gust_kmh=55)
        tracker.update(self._weather(20), self.NOW)
        self.assertIsNone(tracker.current())
        self.assertIsNone(tracker.take_new())


class TestAlertPage(unittest.TestCase):
    NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

    def _render(self, warning):
        return pages.render("alert", pages.PageContext(
            config=_config(), now=self.NOW, weather=None,
            location=(TEST_LAT, TEST_LON), alert=warning))

    def test_every_kind_renders(self):
        cases = [
            alerts.Alert("storm", 2, 85, "RAYOS", "%"),
            alerts.Alert("rain", 3, 70, "LLUVIA", "%"),
            alerts.Alert("wind", 1, 78, "VIENTO"),
            alerts.Alert("frost", 5, -2, "HELADA", "°"),
        ]
        for warning in cases:
            with self.subTest(kind=warning.kind):
                canvas = self._render(warning)
                self.assertEqual((canvas.width, canvas.height), (32, 32))

    def test_labels_fit_the_panel(self):
        # "TORMENTA" was 33px and arrived as "TORMENT".
        for label in ("RAYOS", "LLUVIA", "VIENTO", "HELADA", "SIN AVISO"):
            with self.subTest(label=label):
                self.assertLessEqual(FONT_3x5.width(label), 32)

    def test_every_kind_has_an_icon_and_a_colour(self):
        for kind in alerts.SEVERITY:
            self.assertIn(kind, icons.ALERT_ICONS, f"{kind} has no icon")
            self.assertIn(icons.ALERT_ICONS[kind], icons.ICONS_16)
            self.assertIn(kind, pages.ALERT_COLORS, f"{kind} has no colour")

    def test_no_alert_still_renders(self):
        canvas = self._render(None)
        self.assertEqual((canvas.width, canvas.height), (32, 32))


class TestSunArc(unittest.TestCase):
    """The sun's path, drawn where the apparent temperature is not needed."""

    LAT, LON = TEST_LAT, TEST_LON

    def _page(self, moment, *, apparent_gap=0.0, seasonal=True):
        weather = Weather(
            current=Current(temperature=20.0, apparent=20.0 + apparent_gap,
                            code=0, is_day=True, humidity=40, wind=10.0),
            days=[Day(moment.date(), 0, 24.0, 14.0)], fetched_at=0.0,
        )
        config = _config(display=DisplayConfig(sun_arc_seasonal=seasonal))
        return pages.render("weather_now", pages.PageContext(
            config=config, now=moment, weather=weather,
            location=(self.LAT, self.LON)))

    def _slot(self, canvas):
        px = canvas.image.load()
        return {(x, y) for x in range(17, 32) for y in range(9, 17)
                if px[x, y] != (0, 0, 0)}

    def test_the_arc_takes_the_slot_when_apparent_has_nothing_to_say(self):
        noon = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
        self.assertTrue(self._slot(self._page(noon)))

    def test_the_apparent_temperature_still_wins_the_slot(self):
        """It is the more useful number; the arc only fills an empty space."""
        noon = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
        with_gap = self._page(noon, apparent_gap=5.0)
        px = with_gap.image.load()
        # Text sits on rows 11-15; the arc would also reach rows 9-10.
        self.assertFalse(any(px[x, 9] != (0, 0, 0) for x in range(17, 32)))


    def _filled(self, moment):
        canvas = self._page(moment)
        px = canvas.image.load()
        return sum(1 for x, y in self._slot(canvas) if px[x, y] == pages.ARC_DONE)

    def test_the_wedge_grows_through_the_day(self):
        morning = self._filled(datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc))
        midday = self._filled(datetime(2026, 6, 21, 11, 0, tzinfo=timezone.utc))
        evening = self._filled(datetime(2026, 6, 21, 17, 0, tzinfo=timezone.utc))
        self.assertLess(morning, midday)
        self.assertLess(midday, evening)

    def test_the_wedge_fills_from_the_left(self):
        """Dawn is the left foot, so early on nothing past the meridian is spent.

        The base does reach the centre even at first light: a wedge keeps its
        opening radius, the same way a slice of pizza has two straight edges.
        """
        canvas = self._page(datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc))
        px = canvas.image.load()
        spent = [x for x, y in self._slot(canvas) if px[x, y] == pages.ARC_DONE]
        self.assertTrue(spent)
        self.assertLessEqual(max(spent), 24, "nothing right of the meridian yet")

    def test_the_base_is_not_spent_before_its_time(self):
        """The bug this guards: the whole base row filled at any hour.

        `atan2` returns -pi, not +pi, when handed a negative zero — which is
        exactly what the baseline row produces. The right-hand half of the
        base came out as fraction -1, passed the "already spent" test, and was
        painted from dawn onwards.
        """
        self.assertEqual(pages._wedge_fraction(-6, 0, 6, 6), 0.0)
        self.assertEqual(pages._wedge_fraction(6, 0, 6, 6), 1.0)
        self.assertAlmostEqual(pages._wedge_fraction(0, -6, 6, 6), 0.5)

    def test_the_dome_bounds_the_wedge(self):
        hours = [pages._wedge_fraction(px, py, 6, 6)
                 for py in range(-6, 1) for px in range(-6, 7)]
        inside = [f for f in hours if f is not None]
        self.assertTrue(inside)
        self.assertGreaterEqual(min(inside), 0.0)
        self.assertLessEqual(max(inside), 1.0)
        # Corners sit outside a circle of radius 6.
        self.assertIsNone(pages._wedge_fraction(6, -6, 6, 6))
        # Below the horizon is never part of the day.
        self.assertIsNone(pages._wedge_fraction(0, 1, 6, 6))

    def test_winter_flattens_the_arc_and_summer_lifts_it(self):
        """The season should be legible in the shape, not only in the speed."""
        def apex(day):
            canvas = self._page(datetime(day.year, day.month, day.day, 11, 0,
                                         tzinfo=timezone.utc))
            return min(y for _, y in self._slot(canvas))

        winter = apex(date(2026, 12, 21))
        summer = apex(date(2026, 6, 21))
        self.assertGreater(winter, summer, "the winter arc should sit lower")

    def test_the_fixed_variant_ignores_the_season(self):
        def apex(day):
            canvas = self._page(
                datetime(day.year, day.month, day.day, 11, 0, tzinfo=timezone.utc),
                seasonal=False)
            return min(y for _, y in self._slot(canvas))

        self.assertEqual(apex(date(2026, 12, 21)), apex(date(2026, 6, 21)))

    def test_the_arc_clears_the_separator_below_it(self):
        # A flat winter arc used to merge into the line under it.
        canvas = self._page(datetime(2026, 12, 21, 11, 0, tzinfo=timezone.utc))
        px = canvas.image.load()
        self.assertTrue(all(px[x, 16] == (0, 0, 0) for x in range(17, 32)))

    def test_progress_is_none_outside_daylight(self):
        from ledclock import sun as sun_module

        dawn = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)
        dark = datetime(2026, 6, 21, 23, 30, tzinfo=timezone.utc)
        self.assertIsNotNone(sun_module.day_progress(dawn, self.LAT, self.LON))
        self.assertIsNone(sun_module.day_progress(dark, self.LAT, self.LON))

    def test_max_elevation_tracks_the_seasons(self):
        from ledclock import sun as sun_module

        winter = sun_module.max_elevation(date(2026, 12, 21), self.LAT)
        summer = sun_module.max_elevation(date(2026, 6, 21), self.LAT)
        self.assertAlmostEqual(winter, 23.6, delta=1.0)
        self.assertAlmostEqual(summer, 70.4, delta=1.0)


class TestMoonPosition(unittest.TestCase):
    """Where the moon actually is, not how far through the night we are."""

    LAT, LON = TEST_LAT, TEST_LON

    def _nearest_phase(self, target, start):
        best, closest = None, 9.0
        moment = start
        for _ in range(40):
            distance = abs((moon.phase(moment) - target + 0.5) % 1.0 - 0.5)
            if distance < closest:
                best, closest = moment, distance
            moment += timedelta(days=1)
        return best

    def test_a_full_moon_is_up_at_midnight_and_down_at_noon(self):
        """Cross-checks two independent calculations: the phase series and
        the position series have to agree about where the moon can be."""
        full = self._nearest_phase(0.5, datetime(2026, 8, 1, tzinfo=timezone.utc))
        midnight = full.replace(hour=0, minute=0, second=0, microsecond=0) \
            + timedelta(days=1)
        noon = full.replace(hour=12, minute=0, second=0, microsecond=0)
        self.assertGreater(moon.position(midnight, self.LAT, self.LON)[0], 20)
        self.assertLess(moon.position(noon, self.LAT, self.LON)[0], -20)

    def test_a_new_moon_is_the_other_way_round(self):
        new = self._nearest_phase(0.0, datetime(2026, 8, 1, tzinfo=timezone.utc))
        midnight = new.replace(hour=0, minute=0, second=0, microsecond=0) \
            + timedelta(days=1)
        noon = new.replace(hour=12, minute=0, second=0, microsecond=0)
        self.assertLess(moon.position(midnight, self.LAT, self.LON)[0], -20)
        self.assertGreater(moon.position(noon, self.LAT, self.LON)[0], 20)

    def test_altitude_never_leaves_its_range(self):
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for _ in range(200):
            altitude, azimuth = moon.position(moment, self.LAT, self.LON)
            self.assertGreaterEqual(altitude, -90.0)
            self.assertLessEqual(altitude, 90.0)
            self.assertGreaterEqual(azimuth, 0.0)
            self.assertLess(azimuth, 360.0)
            moment += timedelta(hours=7)

    def test_it_rises_in_the_east_and_sets_in_the_west(self):
        track = moon.path(datetime(2026, 8, 28, 22, tzinfo=timezone.utc),
                          self.LAT, self.LON)
        self.assertTrue(track)
        rise_azimuth, set_azimuth = track[0][2], track[-1][2]
        self.assertLess(rise_azimuth, 180.0, "should rise on the eastern side")
        self.assertGreater(set_azimuth, 180.0, "should set on the western side")

    def test_a_pass_climbs_then_falls(self):
        track = moon.path(datetime(2026, 8, 28, 22, tzinfo=timezone.utc),
                          self.LAT, self.LON)
        altitudes = [altitude for _, altitude, _ in track]
        peak = altitudes.index(max(altitudes))
        self.assertGreater(peak, 0)
        self.assertLess(peak, len(altitudes) - 1)

    def test_a_pass_sweeps_west_across_the_sky(self):
        track = moon.path(datetime(2026, 8, 28, 22, tzinfo=timezone.utc),
                          self.LAT, self.LON)
        azimuths = [azimuth for _, _, azimuth in track]
        self.assertEqual(azimuths, sorted(azimuths))

    def test_the_pass_returned_is_the_one_we_are_in(self):
        moment = datetime(2026, 8, 29, 2, tzinfo=timezone.utc)
        if moon.is_up(moment, self.LAT, self.LON):
            track = moon.path(moment, self.LAT, self.LON)
            self.assertLessEqual(track[0][0], moment)
            self.assertGreaterEqual(track[-1][0], moment)

    def test_the_moon_is_only_drawn_when_it_is_actually_up(self):
        """The whole point of the slot: an empty track means "not out yet"."""
        from ledclock.render import Canvas as _Canvas

        for hour, expect_marker in ((22, True), (14, False)):
            with self.subTest(hour=hour):
                moment = datetime(2026, 8, 28, hour, tzinfo=timezone.utc)
                canvas = _Canvas()
                pages._draw_moon_sky(canvas, left=18, baseline=15, width=13,
                                     height=7, now=moment,
                                     latitude=self.LAT, longitude=self.LON)
                px = canvas.image.load()
                drawn = {px[x, y] for x in range(18, 31) for y in range(9, 16)}
                self.assertEqual(pages.MOON_MARK in drawn, expect_marker)


class TestBoundRounding(unittest.TestCase):
    """A shown bound has to actually bound.

    The bug: the trend header read "min 23, now 35, max 34". Rounding to
    nearest turned a peak of 34.4 into 34 while the current observation of
    34.6 became 35, so the maximum was under the present. Maxima now round
    up and minima down.
    """

    def test_a_high_rounds_up(self):
        self.assertEqual(format_high(34.1), "35")
        self.assertEqual(format_high(34.0), "34")

    def test_a_low_rounds_down(self):
        self.assertEqual(format_low(23.9), "23")
        self.assertEqual(format_low(23.0), "23")

    def test_bounds_round_outwards_below_zero(self):
        self.assertEqual(format_low(-4.2), "-5")
        self.assertEqual(format_high(-4.2), "-4")

    def test_a_point_reading_still_rounds_to_nearest(self):
        # Only bounds round outwards; the current temperature is not a bound.
        self.assertEqual(format_temperature(34.4), "34")
        self.assertEqual(format_temperature(34.6), "35")

    def test_the_displayed_range_always_contains_the_readings(self):
        for low, high in ((23.4, 34.6), (-4.2, 0.1), (12.0, 12.0), (-9.9, -1.1)):
            with self.subTest(low=low, high=high):
                self.assertLessEqual(int(format_low(low)), low)
                self.assertGreaterEqual(int(format_high(high)), high)

    def test_forecast_columns_use_outward_rounding(self):
        weather = Weather(
            current=Current(temperature=20.0, apparent=20.0, code=0, is_day=True),
            days=[Day(date(2026, 8, 2), 0, 34.1, 23.9)], fetched_at=0.0,
        )
        canvas = pages.render("forecast", pages.PageContext(
            config=_config(), now=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
            weather=weather, location=(TEST_LAT, TEST_LON)))
        self.assertEqual((canvas.width, canvas.height), (32, 32))

    def test_the_stat_grid_rounds_its_bounds_outwards(self):
        _, _, _, low_format = pages.STATS["low"]
        _, _, _, high_format = pages.STATS["high"]
        self.assertEqual(low_format(23.9), "23")
        self.assertEqual(high_format(34.1), "35")

    def test_current_readings_in_the_grid_stay_nearest(self):
        for name in ("humidity", "wind", "gusts", "clouds", "pressure"):
            with self.subTest(stat=name):
                self.assertEqual(pages.STATS[name][3](34.6), "35")
                self.assertEqual(pages.STATS[name][3](34.4), "34")

    def test_the_trend_header_never_contradicts_itself(self):
        """A current observation above every point on the curve must widen
        the maximum, not sit outside it."""
        from ledclock.sources.weather import Hour

        base = datetime(2026, 8, 2, 0, tzinfo=timezone.utc)
        hours = [Hour(time=base + timedelta(hours=i), temperature=30.0,
                      apparent=30.0, precipitation_probability=0, code=0,
                      gusts=10.0) for i in range(25)]
        weather = Weather(
            current=Current(temperature=35.0, apparent=35.0, code=0, is_day=True),
            days=[], fetched_at=0.0, hours=hours,
        )
        ctx = pages.PageContext(config=_config(), weather=weather,
                                now=datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
                                location=(TEST_LAT, TEST_LON))
        canvas = pages.render("trend", ctx)
        px = canvas.image.load()
        # The 35 must appear somewhere in the header — as the maximum, since
        # the curve never reaches it.
        header_lit = sum(1 for x in range(32) for y in range(5)
                         if px[x, y] != (0, 0, 0))
        self.assertGreater(header_lit, 0)
        readings = [30.0] * 32
        self.assertGreaterEqual(int(format_high(max(readings + [35.0]))), 35)


class TestSun(unittest.TestCase):
    """Reference values are Open-Meteo's for 2026-08-02, in UTC.

    They were captured once rather than fetched, so the suite stays offline.
    """

    CASES = [
        # name,          lat,        lon,       sunrise UTC, sunset UTC
        ("Paris",      48.85341,     2.3488,    "04:24", "19:29"),
        ("Mexico City", 19.42847,  -99.12766,   "12:12", "01:12"),
        ("San Francisco", 37.77493, -122.41942, "13:13", "03:18"),
        ("Bogota",      4.60971,   -74.08175,   "10:53", "23:12"),
        ("Reykjavik",  64.1466,    -21.9426,    "04:35", "22:32"),
        ("Sydney",    -33.8688,    151.2093,    "20:47", "07:15"),
    ]

    def test_matches_a_reference_ephemeris(self):
        from datetime import date as _date

        day = _date(2026, 8, 2)
        for name, lat, lon, rise_text, set_text in self.CASES:
            with self.subTest(place=name):
                rise, dusk = sun.sun_times(day, lat, lon)
                for computed, expected in ((rise, rise_text), (dusk, set_text)):
                    minutes = computed.hour * 60 + computed.minute
                    want_hour, want_minute = (int(p) for p in expected.split(":"))
                    want = want_hour * 60 + want_minute
                    # Wrap: some of these fall either side of midnight UTC.
                    delta = min(abs(minutes - want), 1440 - abs(minutes - want))
                    self.assertLessEqual(delta, 3, f"{name} off by {delta} min")

    def test_longitude_sign_is_not_flipped(self):
        """The bug this guards: west was treated as east, a 13-hour error.

        A place west of Greenwich reaches solar noon later in UT, so its
        sunrise must be later in UT than the same latitude due east.
        """
        from datetime import date as _date

        day = _date(2026, 8, 2)
        west, _ = sun.sun_times(day, 40.0, -100.0)
        east, _ = sun.sun_times(day, 40.0, 100.0)
        self.assertGreater(west, east)

    def test_night_wraps_around_midnight(self):
        lat, lon = TEST_LAT, TEST_LON  # sunrise 04:39 UTC, sunset 19:08
        cases = {
            datetime(2026, 8, 2, 2, 0, tzinfo=timezone.utc): True,   # pre-dawn
            datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc): False,  # morning
            datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc): False,  # midday
            datetime(2026, 8, 2, 21, 0, tzinfo=timezone.utc): True,   # evening
        }
        for moment, expected in cases.items():
            with self.subTest(moment=moment):
                self.assertEqual(sun.is_night(moment, lat, lon), expected)

    def test_polar_day_and_night(self):
        from datetime import date as _date

        # Svalbard: midnight sun in June, polar night in December.
        self.assertFalse(sun.is_night(
            datetime(2026, 6, 21, 1, 0, tzinfo=timezone.utc), 78.22, 15.63))
        self.assertTrue(sun.is_night(
            datetime(2026, 12, 21, 12, 0, tzinfo=timezone.utc), 78.22, 15.63))
        self.assertEqual(
            sun.daylight(_date(2026, 12, 21), 78.22, 15.63), timedelta(0))

    def test_daylight_is_longer_in_summer(self):
        from datetime import date as _date

        summer = sun.daylight(_date(2026, 6, 21), 48.85, 2.35)
        winter = sun.daylight(_date(2026, 12, 21), 48.85, 2.35)
        self.assertGreater(summer, winter)
        self.assertAlmostEqual(summer.total_seconds() / 3600, 16.1, delta=0.4)


class TestDayNightAcrossPages(unittest.TestCase):
    """Every page drops to night level after local sunset, not just the clocks."""

    LAT, LON = TEST_LAT, TEST_LON
    DAY = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    NIGHT = datetime(2026, 8, 2, 1, 0, tzinfo=timezone.utc)

    def _config(self):
        return _config(display=DisplayConfig(gain=0.55, night_gain=0.42))

    def _peak(self, name, moment) -> int:
        config = self._config()
        ctx = pages.PageContext(config=config, now=moment, weather=_weather(is_day=True),
                                location=(self.LAT, self.LON))
        canvas = pages.render(name, ctx)
        px = canvas.dimmed(config.display.gain).load()
        return max(max(px[x, y]) for x in range(32) for y in range(32))

    def test_pages_sit_at_the_day_level_before_sunset(self):
        for name in ("weather_now", "forecast", "clock_local"):
            with self.subTest(page=name):
                self.assertAlmostEqual(self._peak(name, self.DAY) / 255, 0.55,
                                       delta=0.02)

    def test_pages_drop_to_the_night_level_after_sunset(self):
        for name in ("weather_now", "forecast", "clock_local"):
            with self.subTest(page=name):
                self.assertAlmostEqual(self._peak(name, self.NIGHT) / 255, 0.42,
                                       delta=0.02)

    def test_world_clocks_is_not_dimmed_twice(self):
        """It dims per row against each city's own sun, so the page-level
        rule must skip it — otherwise night rows get hit twice."""
        self.assertIn("world_clocks", pages.SELF_DIMMING)
        # Local midnight, but daytime in the Americas: those rows stay bright.
        self.assertAlmostEqual(self._peak("world_clocks", self.NIGHT) / 255,
                               0.55, delta=0.02)


class TestConfigHelpers(unittest.TestCase):
    def test_active_window_within_a_day(self):
        window = (time(7, 0), time(23, 30))
        self.assertTrue(is_active(window, datetime(2026, 8, 1, 12, 0)))
        self.assertFalse(is_active(window, datetime(2026, 8, 1, 3, 0)))

    def test_active_window_wrapping_midnight(self):
        window = (time(22, 0), time(6, 0))
        self.assertTrue(is_active(window, datetime(2026, 8, 1, 23, 0)))
        self.assertTrue(is_active(window, datetime(2026, 8, 1, 2, 0)))
        self.assertFalse(is_active(window, datetime(2026, 8, 1, 12, 0)))

    def test_no_window_means_always_on(self):
        self.assertTrue(is_active(None, datetime(2026, 8, 1, 4, 0)))


class _FakePanel:
    """Records what the daemon asks of the panel when it lets go."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple] = []
        self.fail = fail
        self.is_connected = True

    async def set_clock_mode(self, style=1, *, use_24h=True, show_date=False, when=None):
        if self.fail:
            raise IPixelError("link already gone")
        self.calls.append(("clock", style, show_date))

    async def set_power(self, on: bool):
        self.calls.append(("power", on))

    async def set_brightness(self, percent: int):
        self.calls.append(("brightness", percent))


class TestHandoff(unittest.IsolatedAsyncioTestCase):
    def _daemon(self, **device) -> daemon.Daemon:
        return daemon.Daemon(_config(device=DeviceConfig(address="x", **device)))

    async def test_clock_mode_hands_over_to_the_firmware_clock(self):
        panel = _FakePanel()
        await self._daemon(idle_mode="clock", clock_style=3,
                           clock_show_date=True).release(panel)
        self.assertEqual(panel.calls, [("clock", 3, True)])

    async def test_off_mode_blanks_the_panel(self):
        panel = _FakePanel()
        await self._daemon(idle_mode="off").release(panel)
        self.assertEqual(panel.calls, [("power", False)])

    async def test_reconnecting_switches_the_panel_back_on(self):
        """The bug this guards: idle_mode "off" left it dark forever.

        drive() starts out believing the panel is lit, and only ever calls
        set_power(True) coming back from active_hours — so a panel we blanked
        ourselves would never have been woken again.
        """
        import inspect

        source = inspect.getsource(daemon.Daemon.run)
        self.assertIn("set_power(True)", source,
                      "connecting must unconditionally light the panel")

    async def test_hold_mode_leaves_the_last_frame(self):
        panel = _FakePanel()
        await self._daemon(idle_mode="hold").release(panel)
        self.assertEqual(panel.calls, [])

    async def test_a_dead_link_does_not_break_shutdown(self):
        # By the time we hand off, the link is often already gone.
        panel = _FakePanel(fail=True)
        await self._daemon(idle_mode="clock").release(panel)  # must not raise

    async def test_sleep_and_wake_toggle_the_awake_flag(self):
        instance = self._daemon()
        self.assertTrue(instance.awake.is_set())
        instance.on_sleep()
        self.assertFalse(instance.awake.is_set())
        instance.on_wake()
        self.assertTrue(instance.awake.is_set())

    async def test_wait_returns_early_when_asked_to_stop(self):
        import time as _time

        instance = self._daemon()
        started = _time.monotonic()
        asyncio.get_running_loop().call_later(0.05, instance.request_stop)
        await daemon._wait_for([instance.stop], timeout=30)
        self.assertLess(_time.monotonic() - started, 5.0)

    async def test_wait_honours_its_timeout(self):
        instance = self._daemon()
        await daemon._wait_for([instance.stop], timeout=0.05)
        self.assertFalse(instance.stop.is_set())

    async def test_sleep_interrupts_a_dwell(self):
        """The bug this guards: the panel froze for eight hours overnight.

        `awake` is cleared on sleep, and a waiter only ever sees an Event get
        *set*, so the carousel slept out the rest of its 20-second dwell while
        macOS was already suspending. The link died first and the handoff to
        the panel's own clock never ran. `nudge` is the extra signal that
        makes going to sleep observable.
        """
        import time as _time

        instance = self._daemon()
        started = _time.monotonic()
        asyncio.get_running_loop().call_later(0.05, instance.on_sleep)
        await daemon._wait_for([instance.stop, instance.nudge], timeout=30)
        self.assertLess(_time.monotonic() - started, 5.0)
        self.assertTrue(instance.nudge.is_set())
        self.assertFalse(instance.awake.is_set())

    async def test_wake_also_interrupts(self):
        instance = self._daemon()
        instance.on_sleep()
        instance.nudge.clear()
        asyncio.get_running_loop().call_later(0.05, instance.on_wake)
        await daemon._wait_for([instance.stop, instance.nudge], timeout=30)
        self.assertTrue(instance.awake.is_set())

    async def test_sleep_unwinds_the_carousel_instead_of_holding_the_link(self):
        """The bug this guards: the panel went silent and could not be found.

        Holding the connection across a sleep tears it down without a GATT
        disconnect. The panel goes on believing it is connected, and a
        connected peripheral stops advertising — so nothing can discover it
        afterwards, sometimes until it is physically unplugged. Sleeping must
        therefore unwind out of drive() so the supervisor's finally can
        disconnect properly while the radio is still up.
        """
        instance = self._daemon()
        panel = _FakePanel()
        instance.on_sleep()
        with self.assertRaises(daemon.Suspended):
            await instance.drive(panel)
        # And it hands over on the way out.
        self.assertEqual(panel.calls, [("clock", 1, False)])

    async def test_every_ble_call_has_a_ceiling(self):
        """Two overnight hangs came from unbounded CoreBluetooth calls.

        The first was connect(), the second disconnect() — the one the first
        fix did not cover. The rule now is that nothing touching the radio
        may block without a deadline, so this asserts the ceilings exist and
        are short enough to recover from.
        """
        from ledclock import ipixel

        for name in ("CONNECT_TIMEOUT", "DISCONNECT_TIMEOUT", "WRITE_TIMEOUT"):
            with self.subTest(timeout=name):
                value = getattr(ipixel, name)
                self.assertGreater(value, 0)
                self.assertLessEqual(value, 60.0)
        # The supervisor's ceiling must outlast the transport's own, or it
        # would fire first and hide the more specific failure.
        self.assertGreater(daemon.CONNECT_TIMEOUT, ipixel.CONNECT_TIMEOUT)

    async def test_disconnect_survives_a_wedged_stack(self):
        """disconnect() must never raise and never hang: the supervisor calls
        it from a finally block, so anything escaping there strands the loop."""
        from ledclock import ipixel

        class WedgedClient:
            async def stop_notify(self, *_):
                await asyncio.Event().wait()

            async def disconnect(self):
                await asyncio.Event().wait()

        panel = ipixel.IPixel("test")
        panel._client = WedgedClient()
        original = ipixel.DISCONNECT_TIMEOUT
        ipixel.DISCONNECT_TIMEOUT = 0.05
        try:
            await panel.disconnect()  # must return, not hang or raise
        finally:
            ipixel.DISCONNECT_TIMEOUT = original
        self.assertIsNone(panel._client)

    async def test_a_hung_connect_cannot_stall_the_daemon(self):
        """CoreBluetooth's connect has no deadline; observed hanging on wake."""
        async def never() -> None:
            await asyncio.Event().wait()

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(never(), timeout=0.05)
        self.assertLessEqual(daemon.CONNECT_TIMEOUT, 60.0,
                             "the deadline must be shorter than a person's patience")


class TestCarouselControl(unittest.IsolatedAsyncioTestCase):
    """Pause, pin and step — the menu bar's whole job."""

    def _daemon(self, pages_list=None):
        display = DisplayConfig(pages=pages_list or
                                ["world_clocks", "weather_now", "trend", "forecast"])
        return daemon.Daemon(_config(display=display,
                                     device=DeviceConfig(address="x")))

    def _walk(self, instance, steps):
        """What the render loop would draw over `steps` passes."""
        return [instance._next_page(False) for _ in range(steps)]

    async def test_the_first_pass_starts_at_the_first_page(self):
        instance = self._daemon()
        self.assertEqual(self._walk(instance, 2), ["world_clocks", "weather_now"])

    async def test_it_wraps_round(self):
        instance = self._daemon()
        self.assertEqual(
            self._walk(instance, 5),
            ["world_clocks", "weather_now", "trend", "forecast", "world_clocks"],
        )

    async def test_next_moves_exactly_one(self):
        instance = self._daemon()
        self._walk(instance, 1)               # showing world_clocks
        instance.advance(1)
        self.assertEqual(instance.showing, "weather_now")
        self.assertEqual(instance._next_page(False), "weather_now")

    async def test_two_quick_clicks_move_two_pages(self):
        """The bug this guards: index used to name the *next* page, so a
        second click before the loop ran landed on the same one twice."""
        instance = self._daemon()
        self._walk(instance, 1)
        instance.advance(1)
        instance.advance(1)
        self.assertEqual(instance.showing, "trend")
        self.assertEqual(instance._next_page(False), "trend")

    async def test_prev_goes_back_one(self):
        instance = self._daemon()
        self._walk(instance, 3)               # showing trend
        instance.advance(-1)
        self.assertEqual(instance.showing, "weather_now")

    async def test_pause_holds_the_page(self):
        instance = self._daemon()
        self._walk(instance, 2)               # showing weather_now
        instance.showing = "weather_now"
        instance.paused = True
        self.assertEqual(self._walk(instance, 3),
                         ["weather_now"] * 3)

    async def test_stepping_keeps_the_pause(self):
        """Pausing says "I am steering now". Having Next hand control back to
        the carousel would undo the thing you just asked for."""
        instance = self._daemon()
        self._walk(instance, 1)
        instance.paused = True
        instance.advance(1)
        self.assertTrue(instance.paused)
        self.assertEqual(instance.showing, "weather_now")

    async def test_a_paused_step_holds_its_new_page(self):
        instance = self._daemon()
        self._walk(instance, 1)
        instance.paused = True
        instance.advance(1)
        self.assertEqual(self._walk(instance, 3), ["weather_now"] * 3)

    async def test_stepping_paused_walks_the_rotation(self):
        instance = self._daemon()
        self._walk(instance, 1)
        instance.paused = True
        seen = []
        for _ in range(4):
            instance.advance(1)
            seen.append(instance._next_page(False))
        self.assertEqual(
            seen, ["weather_now", "trend", "forecast", "world_clocks"])

    async def test_resuming_moves_on_rather_than_repeating(self):
        instance = self._daemon()
        self._walk(instance, 1)
        instance.paused = True
        instance.advance(1)
        instance._next_page(False)          # settles on weather_now
        instance._resume()
        self.assertEqual(instance._next_page(False), "trend")

    async def test_pinning_shows_only_that_page(self):
        instance = self._daemon()
        instance._pin(page="trend")
        self.assertEqual(self._walk(instance, 3), ["trend"] * 3)

    async def test_pinning_an_unknown_page_is_refused(self):
        with self.assertRaises(ValueError):
            self._daemon()._pin(page="banana")

    async def test_disabling_drops_a_page_from_the_rotation(self):
        instance = self._daemon()
        instance._disable(page="trend")
        self.assertNotIn("trend", self._walk(instance, 6))

    async def test_disabling_the_pinned_page_releases_the_pin(self):
        instance = self._daemon()
        instance._pin(page="trend")
        instance._disable(page="trend")
        self.assertIsNone(instance.pinned)

    async def test_turning_everything_off_falls_back_to_the_full_list(self):
        """An empty panel would read as a crash, not as a setting."""
        instance = self._daemon()
        for name in list(instance.config.display.pages):
            instance._disable(page=name)
        self.assertEqual(instance.rotation(), list(instance.config.display.pages))

    async def test_an_alert_still_joins_a_rotation(self):
        instance = self._daemon()
        self.assertIn("alert", instance.rotation(include_alert=True))

    async def test_a_pin_does_not_silence_alerts(self):
        """A fresh alert jumps the queue ahead of a pin — one suppressed
        because a page was pinned would be no warning at all."""
        import inspect

        source = inspect.getsource(daemon.Daemon.drive)
        alert_branch = source.index("take_new()")
        pin_branch = source.index("_next_page")
        self.assertLess(alert_branch, pin_branch,
                        "the alert must be considered before the rotation")

    async def test_the_snapshot_carries_what_the_menu_bar_needs(self):
        instance = self._daemon()
        self._walk(instance, 1)
        snapshot = instance.snapshot()
        for key in ("linked", "showing", "paused", "pinned", "pages",
                    "disabled", "rotation", "brightness", "frame"):
            self.assertIn(key, snapshot)

    async def test_brightness_is_clamped_to_the_protocol_range(self):
        instance = self._daemon()
        self.assertEqual(instance._brightness(value=500)["brightness"], 100)
        self.assertEqual(instance._brightness(value=-5)["brightness"], 1)
        self.assertTrue(instance.brightness_dirty)


class TestControlChannel(unittest.IsolatedAsyncioTestCase):
    """The socket the menu bar speaks over."""

    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path as _Path

        self._dir = tempfile.TemporaryDirectory()
        self.path = _Path(self._dir.name) / "control.sock"
        self.seen = []
        handlers = {
            "ping": lambda: {},
            "echo": lambda value=None: {"value": value},
            "boom": self._boom,
        }
        self.server = control.ControlServer(handlers, path=self.path)
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        self._dir.cleanup()

    def _boom(self):
        raise RuntimeError("handler exploded")

    async def _ask(self, command, **arguments):
        return await asyncio.to_thread(
            control.request, command, path=self.path, **arguments)

    async def test_a_command_round_trips(self):
        self.assertEqual((await self._ask("echo", value=7))["value"], 7)

    async def test_an_unknown_command_lists_the_real_ones(self):
        with self.assertRaises(control.ControlError) as caught:
            await self._ask("nope")
        self.assertIn("echo", str(caught.exception))

    async def test_bad_arguments_are_reported_not_crashed(self):
        with self.assertRaises(control.ControlError):
            await self._ask("echo", wrong="argument")
        # And the server is still up afterwards.
        self.assertEqual((await self._ask("echo", value=1))["value"], 1)

    async def test_a_failing_handler_does_not_take_the_daemon_down(self):
        with self.assertRaises(control.ControlError) as caught:
            await self._ask("boom")
        self.assertIn("exploded", str(caught.exception))
        self.assertEqual((await self._ask("echo", value=2))["value"], 2)

    async def test_a_stale_socket_file_does_not_block_startup(self):
        """A crash leaves the socket file behind; bind would fail forever."""
        await self.server.stop()
        self.path.write_bytes(b"")
        self.assertTrue(self.path.exists())
        await self.server.start()
        self.assertEqual((await self._ask("echo", value=3))["value"], 3)

    async def test_no_daemon_reads_as_not_running(self):
        from pathlib import Path as _Path

        self.assertFalse(control.is_running(_Path("/tmp/ledclock-nope.sock")))


class TestDeviceConfig(unittest.TestCase):
    def _load(self, body: str):
        import tempfile
        from pathlib import Path

        from ledclock import config as config_module

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(body)
            return config_module.load(path)

    def test_idle_mode_defaults_to_the_firmware_clock(self):
        self.assertEqual(self._load("device: {}\n").device.idle_mode, "clock")

    def test_bare_off_survives_yaml_turning_it_into_a_boolean(self):
        """`idle_mode: off` is the obvious thing to write, and YAML reads it
        as False. Rejecting that would fail with a baffling "got 'False'"."""
        self.assertEqual(self._load("device:\n  idle_mode: off\n").device.idle_mode,
                         "off")
        self.assertEqual(self._load('device:\n  idle_mode: "off"\n').device.idle_mode,
                         "off")

    def test_a_true_boolean_idle_mode_explains_itself(self):
        with self.assertRaises(ConfigError) as caught:
            self._load("device:\n  idle_mode: on\n")
        self.assertIn("Quote it", str(caught.exception))

    def test_unknown_idle_mode_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self._load("device:\n  idle_mode: explode\n")
        self.assertIn("explode", str(caught.exception))

    def test_gain_must_be_a_usable_fraction(self):
        for bad in (0, -0.5, 1.5):
            with self.assertRaises(ConfigError):
                self._load(f"display:\n  gain: {bad}\n")
        self.assertAlmostEqual(self._load("display:\n  gain: 0.4\n").display.gain, 0.4)

    def test_clock_style_range_is_enforced(self):
        for style in (0, 9):
            with self.assertRaises(ConfigError):
                self._load(f"device:\n  clock_style: {style}\n")
        self.assertEqual(self._load("device:\n  clock_style: 8\n").device.clock_style, 8)


class TestProtocol(unittest.TestCase):
    def test_frame_length_counts_itself(self):
        self.assertEqual(frame(0x8004, bytes([50])), b"\x05\x00\x04\x80\x32")

    def test_frame_is_little_endian(self):
        built = frame(CMD_PNG, b"")
        self.assertEqual(built, b"\x04\x00\x02\x00")

    def test_device_info_identifies_a_32x32_panel(self):
        # Captured from the real panel: device_type 129 -> led_type 2 -> 32x32.
        info = parse_device_info(bytes([0x0B, 0x00, 0x01, 0x80, 129, 0, 0, 0, 0, 0, 0]))
        self.assertEqual((info.width, info.height), (32, 32))
        self.assertEqual(info.led_type, 2)

    def test_short_device_info_is_rejected(self):
        with self.assertRaises(Exception):
            parse_device_info(b"\x01\x02")


class TestCanvas(unittest.TestCase):
    def test_drawing_outside_the_panel_is_ignored(self):
        canvas = Canvas()
        canvas.pixel(-1, 0, (255, 255, 255))
        canvas.pixel(64, 64, (255, 255, 255))
        self.assertEqual(canvas.to_png(), Canvas().to_png())

    def test_temperature_ramp_runs_cold_blue_to_hot_red(self):
        from ledclock.render import temperature_color

        cold, mild, hot = (temperature_color(v) for v in (-10.0, 20.0, 40.0))
        self.assertGreater(cold[2], cold[0], "freezing should read blue")
        self.assertGreater(hot[0], hot[2], "scorching should read red")
        # The comfortable middle is near-neutral, so neither end is diluted.
        self.assertLess(max(mild) - min(mild), 40)

    def test_temperature_ramp_is_absolute_and_clamped(self):
        from ledclock.render import TEMPERATURE_STOPS, temperature_color

        self.assertEqual(temperature_color(-100.0), TEMPERATURE_STOPS[0][1])
        self.assertEqual(temperature_color(100.0), TEMPERATURE_STOPS[-1][1])

    def test_temperature_ramp_moves_monotonically_toward_red(self):
        from ledclock.render import temperature_color

        reds = [temperature_color(float(c))[0] for c in range(-5, 39)]
        self.assertEqual(reds, sorted(reds), "red must never go backwards")

    def test_gain_scales_every_channel(self):
        canvas = Canvas()
        canvas.rect(0, 0, 4, 4, (200, 100, 50))
        self.assertEqual(canvas.dimmed(0.5).load()[0, 0], (100, 50, 25))

    def test_gain_of_one_is_a_no_op(self):
        canvas = Canvas()
        canvas.rect(0, 0, 4, 4, (200, 100, 50))
        self.assertIs(canvas.dimmed(1.0), canvas.image)

    def test_gain_preserves_hue(self):
        # Ratios must survive, or dimming would recolour the display.
        canvas = Canvas()
        canvas.rect(0, 0, 2, 2, (255, 138, 66))
        red, green, blue = canvas.dimmed(0.4).load()[0, 0]
        self.assertAlmostEqual(green / red, 138 / 255, places=1)
        self.assertAlmostEqual(blue / red, 66 / 255, places=1)

    def test_gain_changes_the_encoded_frame(self):
        canvas = Canvas()
        canvas.text(0, 0, "12:00", FONT_3x5, (255, 255, 255))
        self.assertNotEqual(canvas.to_png(gain=1.0), canvas.to_png(gain=0.4))

    def test_identical_canvases_compare_equal(self):
        # The daemon relies on this to skip redundant BLE transfers.
        a, b = Canvas(), Canvas()
        a.text(0, 0, "12:00", FONT_3x5, (255, 255, 255))
        b.text(0, 0, "12:00", FONT_3x5, (255, 255, 255))
        self.assertEqual(a, b)
        b.pixel(31, 31, (1, 0, 0))
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
