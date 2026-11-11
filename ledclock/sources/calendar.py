"""Today's events, read from the Calendar app through EventKit.

Reading the calendar needs the user's permission, and macOS attributes that
permission to the *bundle* — so the usage description has to be in
LedClock.app's Info.plist, exactly as the Bluetooth one had to be. Run the
daemon straight from a terminal and the prompt is attributed to the terminal
instead, which is a different grant and will look like a mystery denial.

Access is never waited for. The request is fired once and the store reports
nothing until it is granted; the agenda page simply has no events to draw in
the meantime, and picks them up on the next refresh. Blocking the render loop
on a modal permission dialog would freeze the panel on whatever frame it
happened to be showing.

Titles are read, but only the first few characters ever reach the panel: the
header is one line of the 3x5 font, which holds seven. An earlier version
refused to read them at all on the grounds that they do not fit, which was
the wrong call — seven letters of a name say far more than two, and whether
that trade is worth making was never the source's decision.

They stay out of the log, and out of anything the repository tracks. The
repository is public and `docs/*.png` is committed, so every fixture and
screenshot uses invented events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]

#: EKEntityTypeEvent.
_EVENTS = 0

try:
    import AppKit
    import EventKit
    import Foundation

    AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the host
    AVAILABLE = False


@dataclass(frozen=True)
class Event:
    """One timed event."""

    start: datetime
    end: datetime
    calendar: str
    color: RGB
    #: Only the opening characters are ever drawn. Kept whole here so the
    #: truncation is the page's decision rather than the source's.
    title: str = ""

    def __post_init__(self) -> None:
        # The page compares these against an aware "now", and comparing an
        # aware datetime with a naive one raises — which in a render loop
        # means the panel stops updating rather than drawing a wrong pixel.
        # The invariant belongs here, where events are made, not at every
        # place one is read.
        for field_name in ("start", "end"):
            value = getattr(self, field_name)
            if value.tzinfo is None:
                object.__setattr__(self, field_name, value.astimezone())

    @staticmethod
    def _aware(now: datetime) -> datetime:
        """Accept a naive `now` rather than raising inside the render loop."""
        return now if now.tzinfo is not None else now.astimezone()

    def minutes_until(self, now: datetime) -> int:
        return int((self.start - self._aware(now)).total_seconds() // 60)

    def running(self, now: datetime) -> bool:
        return self.start <= self._aware(now) < self.end


class _Store:
    """The EventKit store, plus the permission dance around it."""

    def __init__(self) -> None:
        self._store = None
        self._asked = False
        self._complained = False

    def _status(self) -> int:
        return EventKit.EKEventStore.authorizationStatusForEntityType_(_EVENTS)

    def ready(self) -> bool:
        """True when the calendar can actually be read right now."""
        if not AVAILABLE:
            if not self._complained:
                log.info("no EventKit here; the agenda page will stay empty")
                self._complained = True
            return False

        if self._store is None:
            self._store = EventKit.EKEventStore.alloc().init()

        status = self._status()
        # 3 is full access on recent macOS. Write-only access exists too and
        # cannot read events, so anything else counts as not ready.
        if status == 3:
            return True

        if status in (1, 2):  # restricted, denied
            if not self._complained:
                log.warning(
                    "calendar access is denied; grant it to LedClock in System "
                    "Settings > Privacy & Security > Calendars"
                )
                self._complained = True
            return False

        if not self._asked:
            self._asked = True
            log.info("asking for calendar access")
            handler = lambda granted, error: log.info(  # noqa: E731
                "calendar access %s%s",
                "granted" if granted else "refused",
                f" ({error})" if error else "",
            )
            if self._store.respondsToSelector_(
                "requestFullAccessToEventsWithCompletion:"
            ):
                self._store.requestFullAccessToEventsWithCompletion_(handler)
            else:
                self._store.requestAccessToEntityType_completion_(_EVENTS, handler)
        return False

    def events(self, day: datetime, wanted: dict[str, RGB | None]) -> list[Event]:
        """Today's timed events from the named calendars."""
        if not self.ready():
            return []

        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        predicate = self._store.predicateForEventsWithStartDate_endDate_calendars_(
            _nsdate(start), _nsdate(start + timedelta(days=1)), None
        )

        found: list[Event] = []
        for raw in self._store.eventsMatchingPredicate_(predicate) or []:
            # All-day events have no place on a timeline of the working day,
            # and the ones in this account are holidays and birthdays.
            if raw.isAllDay():
                continue
            name = str(raw.calendar().title())
            if name not in wanted:
                continue
            found.append(
                Event(
                    start=datetime.fromtimestamp(
                        raw.startDate().timeIntervalSince1970()
                    ).astimezone(),
                    end=datetime.fromtimestamp(
                        raw.endDate().timeIntervalSince1970()
                    ).astimezone(),
                    calendar=name,
                    color=wanted[name] or _colour_of(raw.calendar()),
                    title=str(raw.title() or ""),
                )
            )
        return sorted(found, key=lambda e: e.start)


def _nsdate(moment: datetime):
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(moment.timestamp())


def _colour_of(cal) -> RGB:
    """The calendar's own colour, as the Calendar app shows it."""
    colour = cal.color()
    if colour is None:
        return (170, 176, 190)
    srgb = colour.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
    return (
        int(srgb.redComponent() * 255),
        int(srgb.greenComponent() * 255),
        int(srgb.blueComponent() * 255),
    )


_SHARED = _Store()


def fetch(day: datetime, calendars: dict[str, RGB | None]) -> list[Event]:
    """Today's events from the named calendars, or nothing at all.

    Never raises: a panel that stops updating because the calendar was busy
    is worse than a panel with an empty agenda page.
    """
    if not calendars:
        return []
    try:
        return _SHARED.events(day, calendars)
    except Exception as exc:  # ObjC bridges fail in unusual ways
        log.warning("could not read the calendar: %s", exc)
        return []
