"""Sunrise and sunset, computed from latitude and longitude.

The panel dims itself after dark, and "after dark" used to mean a pair of
hardcoded hours. Real sunset moves by hours across the year, and by much more
across the time zones on the world-clock page, so this solves for it instead.

Standard sunrise equation — accurate to about a minute, which is far finer
than a display that only has two brightness levels needs. Doing it here rather
than reading the sunrise/sunset the weather API happily returns keeps it
working with no network, and covers the world-clock cities without four extra
API calls.

Validated against Open-Meteo in the test suite.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

#: Solar altitude counted as sunrise/sunset: the sun's upper limb on the
#: horizon, plus standard atmospheric refraction.
ZENITH_DEGREES = -0.833

#: Obliquity of the ecliptic.
_EARTH_TILT = 23.4397

#: Julian date of the Unix epoch.
_JULIAN_UNIX_EPOCH = 2440587.5
#: Julian date of J2000.0.
_JULIAN_2000 = 2451545.0


class PolarDay(Exception):
    """The sun does not set on this date at this latitude."""


class PolarNight(Exception):
    """The sun does not rise on this date at this latitude."""


def _julian_date(day: date) -> float:
    """Julian date at 00:00 UTC of `day`."""
    return day.toordinal() + 1721424.5


def _from_julian(julian: float) -> datetime:
    return datetime.fromtimestamp(
        (julian - _JULIAN_UNIX_EPOCH) * 86400.0, tz=timezone.utc
    )


def sun_times(day: date, latitude: float, longitude: float) -> tuple[datetime, datetime]:
    """Sunrise and sunset for `day` at that position, both in UTC.

    Raises PolarDay or PolarNight where the sun does not cross the horizon.
    """
    # Days since J2000, shifted so that the solar day starts near local noon.
    # Longitude is east-positive here, and a place west of Greenwich reaches
    # solar noon *later* in UT — hence the minus. Getting this backwards puts
    # Mexico City's sunrise 13 hours out, which is how it was caught.
    n = round(_julian_date(day) - _JULIAN_2000 + 0.0008)
    mean_solar_noon = n - longitude / 360.0

    mean_anomaly = (357.5291 + 0.98560028 * mean_solar_noon) % 360.0
    m = math.radians(mean_anomaly)
    centre = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)

    ecliptic_longitude = (mean_anomaly + centre + 180.0 + 102.9372) % 360.0
    lam = math.radians(ecliptic_longitude)

    transit = (
        _JULIAN_2000
        + mean_solar_noon
        + 0.0053 * math.sin(m)
        - 0.0069 * math.sin(2 * lam)
    )

    declination = math.asin(math.sin(lam) * math.sin(math.radians(_EARTH_TILT)))
    phi = math.radians(latitude)

    numerator = math.sin(math.radians(ZENITH_DEGREES)) - math.sin(phi) * math.sin(
        declination
    )
    denominator = math.cos(phi) * math.cos(declination)
    if denominator == 0:
        raise PolarNight("degenerate geometry at the pole")

    cos_hour_angle = numerator / denominator
    if cos_hour_angle > 1:
        raise PolarNight(f"the sun never rises at {latitude:.2f} on {day}")
    if cos_hour_angle < -1:
        raise PolarDay(f"the sun never sets at {latitude:.2f} on {day}")

    hour_angle = math.degrees(math.acos(cos_hour_angle))
    return (
        _from_julian(transit - hour_angle / 360.0),
        _from_julian(transit + hour_angle / 360.0),
    )


def is_night(moment: datetime, latitude: float, longitude: float) -> bool:
    """True between sunset and sunrise at that position.

    Evaluated against the local calendar day, so the late-evening and
    early-morning halves of one night both resolve correctly.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    local_day = moment.date()
    try:
        sunrise, sunset = sun_times(local_day, latitude, longitude)
    except PolarDay:
        return False
    except PolarNight:
        return True

    if moment < sunrise:
        # Before dawn: this is the tail of the previous day's night.
        return True
    if moment > sunset:
        return True
    return False


def solar_declination(day: date) -> float:
    """The sun's declination in degrees: -23.4 at midwinter, +23.4 at midsummer."""
    n = round(_julian_date(day) - _JULIAN_2000 + 0.0008)
    mean_anomaly = (357.5291 + 0.98560028 * n) % 360.0
    m = math.radians(mean_anomaly)
    centre = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((mean_anomaly + centre + 180.0 + 102.9372) % 360.0)
    return math.degrees(
        math.asin(math.sin(lam) * math.sin(math.radians(_EARTH_TILT)))
    )


def max_elevation(day: date, latitude: float) -> float:
    """How high the sun gets at solar noon, in degrees above the horizon.

    At 43 degrees north that runs from about 24 degrees at the winter
    solstice to 70 at the summer one — the difference an arc can show by its
    height rather than only by how fast the sun crosses it.
    """
    return 90.0 - abs(latitude - solar_declination(day))


def day_progress(moment: datetime, latitude: float, longitude: float) -> float | None:
    """How far through daylight we are: 0.0 at sunrise, 1.0 at sunset.

    None once the sun is down, which is the signal to draw nothing at all
    rather than a sun sitting on the horizon all night.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        sunrise, sunset = sun_times(moment.date(), latitude, longitude)
    except PolarDay:
        return 0.5
    except PolarNight:
        return None
    if not sunrise <= moment <= sunset:
        return None
    span = (sunset - sunrise).total_seconds()
    if span <= 0:
        return None
    return (moment - sunrise).total_seconds() / span


def daylight(day: date, latitude: float, longitude: float) -> timedelta:
    """How long the sun is up. Mostly useful for sanity-checking the maths."""
    try:
        sunrise, sunset = sun_times(day, latitude, longitude)
    except PolarDay:
        return timedelta(days=1)
    except PolarNight:
        return timedelta(0)
    return sunset - sunrise
