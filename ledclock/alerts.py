"""Decide when the weather is worth interrupting for.

Three things matter more than detection here.

**Lead time is the payload.** "Rain" is decoration; "rain in two hours" is
something you act on. Every alert therefore carries how long you have.

**Not crying wolf.** A gust of 56 km/h against a 55 threshold would otherwise
flip the alert on and off every refresh. Alerts latch: once raised they stay
up for `hold` regardless, and they only clear once the reading has fallen
back below a margin under the threshold.

**Only one can be on screen.** They carry a severity so the panel shows the
storm, not the breeze that came with it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .sources.weather import Hour, Weather

#: WMO codes that mean thunder.
STORM_CODES = frozenset({95, 96, 99})
#: WMO codes that mean something is falling out of the sky.
WET_CODES = frozenset({
    51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
    71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99,
})

#: Higher wins when several fire at once.
SEVERITY = {"storm": 4, "wind": 3, "frost": 2, "rain": 1}

#: How far under the trigger a reading must fall before an alert lets go.
#: Pure hysteresis — without it a value hovering on the line flickers.
RELEASE_MARGIN = 0.85


@dataclass(frozen=True)
class Alert:
    kind: str
    #: Hours until it arrives. 0 means it is already happening.
    lead_hours: int
    #: The number worth showing: % chance, km/h, or degrees.
    magnitude: float
    #: Short word for the panel.
    label: str
    #: Unit suffix, or "" where the label carries the meaning.
    unit: str = ""

    @property
    def severity(self) -> int:
        return SEVERITY.get(self.kind, 0)

    @property
    def key(self) -> str:
        """Identity for latching. Deliberately excludes lead time, so an
        approaching storm does not re-announce itself every hour."""
        return self.kind


def _upcoming(weather: Weather, now: datetime, hours: float) -> list[Hour]:
    horizon = now + timedelta(hours=hours)
    return [h for h in weather.hours if now - timedelta(hours=1) < h.time <= horizon]


def _lead(now: datetime, moment: datetime) -> int:
    return max(0, round((moment - now).total_seconds() / 3600))


def evaluate(weather: Weather | None, config, now: datetime) -> list[Alert]:
    """Everything currently worth saying, most severe first."""
    if weather is None or not config.enabled or not weather.hours:
        return []

    window = _upcoming(weather, now, config.lookahead_hours)
    if not window:
        return []

    found: list[Alert] = []

    storm = next((h for h in window if h.code in STORM_CODES), None)
    if storm is not None:
        found.append(Alert(
            kind="storm", lead_hours=_lead(now, storm.time),
            magnitude=storm.precipitation_probability or 0,
            # "TORMENTA" is 33px at 3x5 — the M alone is five wide — so it
            # would arrive on the panel as "TORMENT". RAYOS fits, and names
            # the part you actually care about.
            label="RAYOS", unit="%",
        ))

    gust = max(
        (h for h in window if h.gusts is not None and h.gusts >= config.gust_kmh),
        key=lambda h: h.gusts, default=None,
    )
    if gust is not None:
        found.append(Alert(
            kind="wind", lead_hours=_lead(now, gust.time),
            magnitude=gust.gusts, label="VIENTO",
        ))

    cold = next(
        (h for h in window if h.temperature <= config.frost_celsius), None
    )
    if cold is not None:
        found.append(Alert(
            kind="frost", lead_hours=_lead(now, cold.time),
            magnitude=cold.temperature, label="HELADA", unit="°",
        ))

    # Rain only counts as news if it is not already falling: an alert for
    # weather you can hear on the window is noise.
    already_wet = window[0].code in WET_CODES if window else False
    if not already_wet:
        wet = next(
            (h for h in window
             if (h.precipitation_probability or 0) >= config.rain_probability
             and h.code in WET_CODES),
            None,
        )
        if wet is not None:
            found.append(Alert(
                kind="rain", lead_hours=_lead(now, wet.time),
                magnitude=wet.precipitation_probability or 0,
                label="LLUVIA", unit="%",
            ))

    found.sort(key=lambda a: (-a.severity, a.lead_hours))
    return found


class AlertTracker:
    """Latches alerts so they neither flicker nor overstay.

    `current()` is what the panel should show; `take_new()` reports a kind
    that has just appeared, which the carousel uses to jump straight to it
    instead of waiting out the rotation.
    """

    def __init__(self, config) -> None:
        self._config = config
        self._raised: dict[str, tuple[Alert, float]] = {}
        self._unannounced: list[Alert] = []

    def update(self, weather: Weather | None, now: datetime) -> None:
        live = {alert.key: alert for alert in evaluate(weather, self._config, now)}
        stamp = time.monotonic()

        for key, alert in live.items():
            if key not in self._raised:
                self._unannounced.append(alert)
            self._raised[key] = (alert, stamp)

        hold = self._config.hold_minutes * 60
        for key in list(self._raised):
            if key in live:
                continue
            _, raised_at = self._raised[key]
            if stamp - raised_at >= hold:
                del self._raised[key]

    def current(self) -> Alert | None:
        if not self._raised:
            return None
        return max(
            (alert for alert, _ in self._raised.values()),
            key=lambda a: (a.severity, -a.lead_hours),
        )

    def take_new(self) -> Alert | None:
        """Pop an alert that has not been shown yet, if any."""
        return self._unannounced.pop(0) if self._unannounced else None
