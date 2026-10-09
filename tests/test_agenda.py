"""The agenda page: a day column with a line across it saying where you are.

Every event here is invented. The repository is public and `docs/*.png` is
tracked, so a fixture built from a real calendar would publish someone's day
the first time a screenshot was regenerated.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from ledclock import pages
from ledclock.config import AgendaCalendar, AgendaConfig, Config
from ledclock.sources.calendar import Event

PURPLE = (185, 130, 255)
BLUE = (70, 170, 255)

#: A Thursday, mid-morning.
NOW = datetime(2026, 10, 8, 10, 30).astimezone()


def _config(**agenda) -> Config:
    return Config(
        agenda=AgendaConfig(calendars=[AgendaCalendar(name="work")], **agenda)
    )


def _event(start: str, end: str, color=BLUE, title="REVIEW") -> Event:
    def at(text: str) -> datetime:
        hour, minute = (int(part) for part in text.split(":"))
        return NOW.replace(hour=hour, minute=minute)

    return Event(start=at(start), end=at(end), calendar="work",
                 color=color, title=title)


def _ctx(events, now=NOW, config=None) -> pages.PageContext:
    return pages.PageContext(config=config or _config(), now=now, events=events)


def _rows(canvas) -> dict[int, list]:
    """Each row of the panel as a list of colours, for reading shapes."""
    image = canvas.image.convert("RGB")
    return {
        y: [image.getpixel((x, y)) for x in range(image.width)]
        for y in range(image.height)
    }


def _now_line(canvas) -> int | None:
    """The row the now-mark is on, if it is anywhere.

    It is a mark at the right edge, not a rule across the column: drawn full
    width it crossed the hour labels and dirtied the digits.
    """
    for y, row in _rows(canvas).items():
        if y >= pages.AGENDA_TOP and row[-pages.AGENDA_MARK:] == [
            (255, 255, 255)
        ] * pages.AGENDA_MARK:
            return y
    return None


class TestTheNowLine(unittest.TestCase):
    """It is the one thing on the page that must always be findable."""

    def test_it_is_drawn_whenever_the_column_is(self) -> None:
        self.assertIsNotNone(_now_line(pages.agenda(_ctx([_event("14:00", "15:00")]))))

    def test_it_never_sits_outside_an_event_that_is_running(self) -> None:
        """The bug this guards: the mark one row below a live block.

        The mark lands on floor(row(now)); the block used to be drawn at
        floor(row(start)) with a height rounded from the span, so a block
        covering rows 7.93 to 9.93 got two rows while the closing minutes of
        the event genuinely mapped to row 9. It happened on a fifth of the
        minutes any event was live, and only ever near the end of one — which
        is exactly when the page is being looked at.

        Swept minute by minute rather than spot-checked, because whether it
        bites depends on where the block falls between two rows.
        """
        events = [_event("09:30", "10:00", title="STANDUP"),
                  _event("11:00", "12:30", color=PURPLE, title="WORKSHOP"),
                  _event("14:00", "15:00", title="REVIEW"),
                  _event("16:10", "16:40", color=PURPLE, title="GYM")]
        for minute in range(9 * 60, 17 * 60):
            now = NOW.replace(hour=minute // 60, minute=minute % 60)
            live = [e for e in events if e.start <= now < e.end]
            if not live:
                continue
            canvas = pages.agenda(_ctx(events, now=now))
            mark = _now_line(canvas)
            self.assertIsNotNone(mark, now)
            lit = [y for y, row in _rows(canvas).items()
                   if y >= pages.AGENDA_TOP
                   and row[pages.AGENDA_GUTTER] == live[0].color]
            self.assertIn(mark, lit, f"{now:%H:%M} {live[0].title}")

    def test_it_survives_landing_on_an_event(self) -> None:
        """The bug this guards: it vanished exactly when something was running.

        Drawn before the boxes, the mark was painted over by whatever meeting
        was in progress — the moment the column is most worth reading.
        """
        canvas = pages.agenda(_ctx([_event("10:00", "11:00")]))
        self.assertIsNotNone(_now_line(canvas))

    def test_it_is_never_welded_to_the_header(self) -> None:
        """With nothing behind it the line belongs near the top, not on the
        divider — touching it, the two read as one thick rule."""
        canvas = pages.agenda(_ctx([_event("14:00", "15:00")]))
        line = _now_line(canvas)
        self.assertIsNotNone(line)
        self.assertGreater(line, pages.AGENDA_TOP, "flush against the divider")

    def test_it_moves_down_as_the_day_is_spent(self) -> None:
        morning = pages.agenda(_ctx([_event("14:00", "15:00")]))
        afternoon = pages.agenda(
            _ctx([_event("09:00", "10:00"), _event("14:00", "15:00")])
        )
        self.assertGreater(_now_line(afternoon), _now_line(morning))

    def test_it_never_passes_half_way(self) -> None:
        """What is ahead matters more than what is done."""
        crowded = [_event(f"{h:02d}:00", f"{h:02d}:45") for h in range(6, 10)]
        # Something ahead, or the page switches to tomorrow's list and there
        # is no column left to measure.
        crowded.append(_event("23:00", "23:30"))
        line = _now_line(pages.agenda(_ctx(crowded)))
        middle = pages.AGENDA_TOP + (pages.AGENDA_BOTTOM - pages.AGENDA_TOP) / 2
        self.assertLessEqual(line, middle + 1)


class TestTheScale(unittest.TestCase):
    """A row is worth a fixed number of minutes, whatever the day holds.

    The column used to stretch between two and a half and eight hours to
    reach whatever was coming. It reached further and lied about size: at the
    eight-hour end a row was twenty minutes, so half an hour drew as a pixel
    and a half and a short meeting looked like a long one on a quiet day.
    """

    def _height(self, canvas, colour=BLUE) -> int:
        """Rows of the column only.

        Counting the whole panel added five: the header's colour bar is the
        same colour as the event it names, so every box measured five rows
        taller than it was.
        """
        return sum(1 for y, row in _rows(canvas).items()
                   if y >= pages.AGENDA_TOP and colour in row)

    def test_a_row_is_worth_the_configured_minutes(self) -> None:
        per_row = _config().agenda.minutes_per_row
        half_hour = pages.agenda(_ctx([_event("11:00", "11:30")]))
        self.assertEqual(self._height(half_hour), round(30 / per_row))

    def test_twice_as_long_is_twice_as_tall(self) -> None:
        short = pages.agenda(_ctx([_event("11:00", "11:30")]))
        long = pages.agenda(_ctx([_event("11:00", "12:00")]))
        self.assertEqual(self._height(long), 2 * self._height(short))

    def test_the_same_meeting_is_the_same_size_on_any_day(self) -> None:
        """The whole point: the scale cannot move under the reader."""
        alone = _event("11:00", "11:30")
        crowded = [alone] + [_event(f"{h:02d}:00", f"{h:02d}:45", PURPLE)
                             for h in (12, 13, 14, 15)]
        self.assertEqual(self._height(pages.agenda(_ctx([alone]))),
                         self._height(pages.agenda(_ctx(crowded))))

    def test_the_window_is_the_rows_times_the_scale(self) -> None:
        ctx = _ctx([_event("11:00", "11:30")])
        rows = pages.AGENDA_BOTTOM - pages.AGENDA_TOP
        _, window = pages._agenda_span(ctx, NOW)
        self.assertAlmostEqual(window,
                               rows * ctx.config.agenda.minutes_per_row / 60)


class TestWhatItShows(unittest.TestCase):
    def test_finished_events_are_dimmed_not_dropped(self) -> None:
        """Sliding the line down is pointless if the morning is not drawn."""
        canvas = pages.agenda(_ctx([_event("09:00", "10:00", PURPLE),
                                    _event("14:00", "15:00")]))
        colours = {c for row in _rows(canvas).values() for c in row}
        self.assertNotIn(PURPLE, colours, "a finished event should be dimmed")
        dimmed = tuple(int(c * pages.AGENDA_PAST) for c in PURPLE)
        self.assertIn(dimmed, colours, "a finished event should still be drawn")

    def test_the_hour_channel_is_never_crossed(self) -> None:
        """Neither a box nor the now-mark may touch the digits.

        The mark used to run the full width, which put white pixels through
        the hour labels — the one part of the page that has to stay crisp.
        """
        canvas = pages.agenda(_ctx([_event("09:00", "20:00")]))
        for y, row in _rows(canvas).items():
            if y < pages.AGENDA_TOP:
                continue
            for x in range(pages.AGENDA_GUTTER):
                self.assertNotEqual(row[x], BLUE, f"a box reached x={x}")
                self.assertNotEqual(row[x], (255, 255, 255),
                                    f"the now-mark reached x={x}")

    def test_the_header_names_what_is_next(self) -> None:
        blank = pages.agenda(_ctx([]))
        named = pages.agenda(_ctx([_event("14:00", "15:00", title="WORKSHOP")]))
        self.assertNotEqual(blank, named)

    def test_the_colour_bar_is_two_pixels_wide(self) -> None:
        """One pixel beside a glyph reads as a 1: 8:30 looked like 18:30.

        The time is gone from the header now, but the bar sits beside letters
        that can be mistaken the same way, and two pixels reads as a bar.
        """
        # Inside the hour, which is the only time the bar is drawn at all.
        canvas = pages.agenda(_ctx([_event("11:00", "11:30", PURPLE)]))
        rows = _rows(canvas)
        self.assertEqual(rows[2][0], PURPLE)
        self.assertEqual(rows[2][1], PURPLE)


class TestTheHeaderCountdown(unittest.TestCase):
    """The one thing the column cannot say: how many minutes.

    Position answers "when" and height answers "how long", but neither tells
    you whether the next box is ten minutes off or three hours. The count
    only appears inside the last hour, where the name has already been read
    several rotations ago and the minutes have become the useful part.
    """

    def _header(self, canvas) -> list:
        rows = _rows(canvas)
        return [rows[y] for y in range(5)]

    def test_it_appears_inside_the_hour(self) -> None:
        soon = pages.agenda(_ctx([_event("11:00", "11:30")]))     # 30 minutes
        later = pages.agenda(_ctx([_event("14:00", "14:30")]))    # three hours
        self.assertNotEqual(self._header(soon), self._header(later))

    def test_beyond_the_hour_the_header_is_the_date_not_the_name(self) -> None:
        """A name three hours out rotates past a dozen times before it matters.

        By then it has stopped being read, so it is noise. The date is at
        least true all day.
        """
        far = _rows(pages.agenda(_ctx([_event("14:00", "14:30")])))
        near = _rows(pages.agenda(_ctx([_event("11:00", "11:30")])))
        self.assertNotEqual([far[y] for y in range(5)],
                            [near[y] for y in range(5)])
        # The date is drawn in the hour colour, the name in white.
        self.assertNotIn((255, 255, 255), [c for y in range(5) for c in far[y]])

    def test_it_stays_away_once_the_thing_has_started(self) -> None:
        """Counting down to something already under way is nonsense."""
        running = _rows(pages.agenda(_ctx([_event("10:00", "11:00")])))
        for y in range(5):
            self.assertEqual(running[y][28:], [(0, 0, 0)] * 4)

    def test_the_name_gives_up_room_for_it(self) -> None:
        """Both have to fit: a fixed seven characters would collide."""
        long_name = _event("11:00", "11:30", title="RETROSPECTIVE")
        with_count = pages.agenda(_ctx([long_name]))
        rows = _rows(with_count)
        # A clear column between the name and the count, at the gap.
        self.assertTrue(
            any(rows[y][23] == (0, 0, 0) for y in range(5)),
            "the name runs into the countdown",
        )


class TestTheHourLabels(unittest.TestCase):
    def test_the_channel_carries_numbers_not_just_ticks(self) -> None:
        """Ticks gave the column shape but never said which hour."""
        canvas = pages.agenda(_ctx([_event("14:00", "15:00")]))
        rows = _rows(canvas)
        lit = sum(
            1
            for y in range(pages.AGENDA_TOP, pages.AGENDA_BOTTOM)
            for x in range(pages.AGENDA_GUTTER)
            if rows[y][x] == pages.AGENDA_HOUR
        )
        self.assertGreater(lit, 10, "no hour labels in the channel")

    def test_labels_thin_out_when_hours_are_tight(self) -> None:
        """At three pixels an hour, a label every hour would overlap itself.

        Counting lit rows was the obvious check and the wrong one: a label is
        five rows tall, so three spaced labels and two touching ones come out
        the same. The rule itself is what matters.
        """
        rows = pages.AGENDA_BOTTOM - pages.AGENDA_TOP
        self.assertEqual(pages._agenda_label_every(2.5), 1)
        self.assertGreater(pages._agenda_label_every(8.0), 1)
        # Never so often that two labels could touch.
        for window in (2.5, 4.0, 6.0, 8.0):
            every = pages._agenda_label_every(window)
            self.assertGreaterEqual(rows / window * every, 5)


class TestTomorrow(unittest.TestCase):
    """Once today has nothing left, the column has nothing to say.

    There is no "now" to place in tomorrow, and the shape of a day you cannot
    act on yet says nothing — so the page becomes a list of times and names.
    """

    #: Late enough that the summary is allowed to replace the column.
    LATE = NOW.replace(hour=23, minute=10)

    def _ctx(self, events, days_off=1, now=None):
        """A context whose next day with anything in it is `days_off` away."""
        when = now or self.LATE
        day = when.date() + timedelta(days=days_off)
        return pages.PageContext(config=_config(), now=when, events=[],
                                 ahead=(day, events) if events else None)

    def test_the_evening_still_belongs_to_today(self) -> None:
        """Spent is not enough on its own.

        At half past seven the evening is still ahead, and tomorrow's list is
        something to read on the way to bed — not a page that takes over the
        moment the last meeting ends.
        """
        early = self._ctx([_event("09:00", "10:00")],
                          now=NOW.replace(hour=19, minute=30))
        late = self._ctx([_event("09:00", "10:00")])
        self.assertNotEqual(pages.agenda(early), pages.agenda(late))

    def test_a_spent_day_shows_tomorrow(self) -> None:
        column = pages.agenda(_ctx([_event("14:00", "15:00")]))  # NOW, 10:30
        listing = pages.agenda(self._ctx([_event("09:00", "10:00")]))
        self.assertNotEqual(column, listing)
        self.assertIsNone(_now_line(listing), "a list has no now to mark")

    def test_the_times_lose_what_can_be_lost(self) -> None:
        """Four pixels a glyph is the difference between "DE" and "DEMO"."""
        on_the_hour = _event("09:00", "10:00")
        self.assertEqual(pages._agenda_short_time(on_the_hour.start), "9")
        half_past = _event("09:30", "10:00")
        self.assertEqual(pages._agenda_short_time(half_past.start), "9:30")

    def test_a_truncated_list_admits_it(self) -> None:
        """The bug this guards: seven events drew four and looked like four.

        Cutting the list in silence is a lie by omission — the one number
        that has to survive is how much was left out.
        """
        many = [_event(f"{h:02d}:00", f"{h:02d}:30") for h in range(9, 16)]
        few = many[: pages.AGENDA_SUMMARY_ROWS]
        self.assertNotEqual(pages.agenda(self._ctx(many)),
                            pages.agenda(self._ctx(few)))

    def test_an_empty_tomorrow_says_so(self) -> None:
        blank = pages.agenda(self._ctx([]))
        self.assertNotEqual(blank, pages.agenda(self._ctx([_event("09:00", "10:00")])))


class TestImminence(unittest.TestCase):
    def _feed(self, events, minutes=10.0):
        from ledclock.daemon import AgendaFeed

        feed = AgendaFeed(_config(imminent_minutes=minutes))
        feed.events = events
        return feed

    def test_it_interrupts_once_not_every_tick(self) -> None:
        """A page that jumps the queue every tick is not a carousel."""
        feed = self._feed([_event("10:35", "11:00")])
        self.assertIsNotNone(feed.imminent(NOW))
        for offset in range(0, 5):
            self.assertIsNone(feed.imminent(NOW + timedelta(minutes=offset)))

    def test_something_far_off_waits_its_turn(self) -> None:
        self.assertIsNone(self._feed([_event("14:00", "15:00")]).imminent(NOW))

    def test_something_already_running_does_not_interrupt(self) -> None:
        """Interrupting for a meeting that began ten minutes ago is noise."""
        self.assertIsNone(self._feed([_event("10:20", "11:00")]).imminent(NOW))


if __name__ == "__main__":
    unittest.main()
