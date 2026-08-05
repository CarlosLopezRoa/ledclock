"""Moon phase, computed and drawn.

Eight hand-drawn phase icons would be both more code and less accurate than
just solving for the terminator, so the disc is generated: for every row of
the circle the lit region is bounded by an ellipse whose width tracks the
phase angle. That gives every intermediate shape for free, at any size.

The phase itself uses the mean synodic month. That is good to a few hours —
far tighter than the ~3.7 day resolution of a 16-pixel disc, and it needs no
ephemeris.

Orientation is northern hemisphere: a waxing moon is lit on the right.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

#: Mean length of a lunation, in days.
SYNODIC_MONTH = 29.530588853

#: A known new moon: 2000-01-06 18:14 UTC.
_REFERENCE_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

#: J2000.0 epoch, the zero point for the position series.
_J2000 = datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc)

#: Lit pixels use this palette key, the unlit disc that one.
LIT = "M"
UNLIT = "d"

PHASE_NAMES_ES = [
    "NUEVA",
    "CRECIENTE",
    "CUARTO CRECIENTE",
    "GIBOSA CRECIENTE",
    "LLENA",
    "GIBOSA MENGUANTE",
    "CUARTO MENGUANTE",
    "MENGUANTE",
]


def phase(when: datetime | None = None) -> float:
    """Position in the lunation as a fraction: 0 new, 0.25 first quarter,
    0.5 full, 0.75 last quarter."""
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    days = (moment - _REFERENCE_NEW_MOON).total_seconds() / 86400.0
    return (days % SYNODIC_MONTH) / SYNODIC_MONTH


def illumination(phase_value: float) -> float:
    """Fraction of the disc that is lit, 0.0 to 1.0."""
    return (1.0 - math.cos(2.0 * math.pi * phase_value)) / 2.0


def phase_name(phase_value: float) -> str:
    """The nearest of the eight traditional phases."""
    index = int((phase_value * 8.0) + 0.5) % 8
    return PHASE_NAMES_ES[index]


#: Obliquity of the ecliptic, degrees.
_EARTH_TILT = 23.4397


def _days_since_j2000(moment: datetime) -> float:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - _J2000).total_seconds() / 86400.0


def position(moment: datetime, latitude: float, longitude: float) -> tuple[float, float]:
    """Where the moon is in the sky: (altitude, azimuth) in degrees.

    Altitude is measured from the horizon, so negative means below it — which
    is the interesting part: it answers "can I see it right now" rather than
    "how far through the night are we".

    Azimuth is measured from north, increasing eastward: 90 is due east, 180
    due south, 270 due west.

    Low-precision series, good to a few tenths of a degree. That is far finer
    than a seven-pixel arc, and it avoids carrying an ephemeris around.
    """
    d = _days_since_j2000(moment)

    # Moon's orbit: mean longitude, mean anomaly, argument of latitude.
    mean_longitude = 218.316 + 13.176396 * d
    mean_anomaly = math.radians(134.963 + 13.064993 * d)
    argument_of_latitude = math.radians(93.272 + 13.229350 * d)

    ecliptic_longitude = math.radians(
        mean_longitude + 6.289 * math.sin(mean_anomaly)
    )
    ecliptic_latitude = math.radians(5.128 * math.sin(argument_of_latitude))

    tilt = math.radians(_EARTH_TILT)
    right_ascension = math.atan2(
        math.sin(ecliptic_longitude) * math.cos(tilt)
        - math.tan(ecliptic_latitude) * math.sin(tilt),
        math.cos(ecliptic_longitude),
    )
    declination = math.asin(
        math.sin(ecliptic_latitude) * math.cos(tilt)
        + math.cos(ecliptic_latitude) * math.sin(tilt) * math.sin(ecliptic_longitude)
    )

    # Local sidereal time, then the hour angle west of the meridian.
    sidereal = math.radians((280.16 + 360.9856235 * d + longitude) % 360.0)
    hour_angle = sidereal - right_ascension

    phi = math.radians(latitude)
    altitude = math.asin(
        math.sin(phi) * math.sin(declination)
        + math.cos(phi) * math.cos(declination) * math.cos(hour_angle)
    )
    # atan2 here gives the angle west of due south; +180 puts it on the
    # usual compass, measured from north and increasing eastward.
    bearing = math.atan2(
        math.sin(hour_angle),
        math.cos(hour_angle) * math.sin(phi)
        - math.tan(declination) * math.cos(phi),
    )
    return math.degrees(altitude), (math.degrees(bearing) + 180.0) % 360.0


def is_up(moment: datetime, latitude: float, longitude: float) -> bool:
    return position(moment, latitude, longitude)[0] > 0.0


def path(moment: datetime, latitude: float, longitude: float, *,
         step_minutes: int = 12, span_hours: int = 15) -> list[tuple[datetime, float, float]]:
    """The moon's current or next time above the horizon.

    Returns (time, altitude, azimuth) for one continuous pass. If the moon is
    up now that pass is the one containing now; otherwise it is the next one
    to begin, which is what lets the display say "not out yet, and it will
    come up over there".
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    step = timedelta(minutes=step_minutes)
    samples: list[tuple[datetime, float, float]] = []
    when = moment - timedelta(hours=span_hours)
    end = moment + timedelta(hours=span_hours)
    while when <= end:
        altitude, azimuth = position(when, latitude, longitude)
        samples.append((when, altitude, azimuth))
        when += step

    # Split into runs of consecutive above-horizon samples.
    passes: list[list[tuple[datetime, float, float]]] = []
    current: list[tuple[datetime, float, float]] = []
    for sample in samples:
        if sample[1] > 0:
            current.append(sample)
        elif current:
            passes.append(current)
            current = []
    if current:
        passes.append(current)
    if not passes:
        return []

    for one in passes:
        if one[0][0] <= moment <= one[-1][0]:
            return one
    upcoming = [one for one in passes if one[0][0] > moment]
    return upcoming[0] if upcoming else passes[-1]


def render(size: int = 16, phase_value: float | None = None) -> tuple[str, ...]:
    """A square grid of LIT / UNLIT / '.' characters, ready for Canvas.icon."""
    value = phase(None) if phase_value is None else phase_value % 1.0

    centre = (size - 1) / 2.0
    radius = size / 2.0
    # Half-width of the terminator ellipse relative to the disc at each row.
    # +1 at new moon (no lit area), 0 at the quarters, -1 at full.
    squash = math.cos(2.0 * math.pi * value)
    waxing = value < 0.5

    rows: list[str] = []
    for y in range(size):
        offset_y = y - centre
        span = radius * radius - offset_y * offset_y
        half_width = math.sqrt(span) if span > 0 else -1.0

        row: list[str] = []
        for x in range(size):
            offset_x = x - centre
            if half_width < 0 or abs(offset_x) > half_width:
                row.append(".")
                continue
            edge = half_width * squash
            lit = offset_x >= edge if waxing else offset_x <= -edge
            row.append(LIT if lit else UNLIT)
        rows.append("".join(row))
    return tuple(rows)
