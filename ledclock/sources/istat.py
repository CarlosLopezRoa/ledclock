"""Read what iStat Menus already knows: world-clock cities and last location.

iStat Menus does not expose its weather readings anywhere, but it does keep
the user's configured cities (with tz and coordinates) and the most recent
CoreLocation fix in plain plists. Both are a convenience: config.yaml wins,
and everything here degrades to None if iStat is absent.
"""

from __future__ import annotations

import plistlib
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PREFS = Path.home() / "Library" / "Preferences"
MENUBAR_PLIST = PREFS / "com.bjango.istatmenus.menubar.7.plist"
STATUS_PLIST = PREFS / "com.bjango.istatmenus.status.plist"

#: Cities iStat lists that are not real places.
_PSEUDO_CITIES = {"Moon", "Current Location"}

#: Nicer three-letter codes than a blind truncation would produce.
_CODE_OVERRIDES = {
    "San Francisco": "SFO",
    "Mexico City": "MEX",
    "New York": "NYC",
    "Los Angeles": "LAX",
    "Buenos Aires": "BUE",
    "Sao Paulo": "SAO",
    "Rio de Janeiro": "RIO",
    "Hong Kong": "HKG",
}


@dataclass(frozen=True)
class City:
    name: str
    timezone: str
    latitude: float
    longitude: float

    @property
    def code(self) -> str:
        return city_code(self.name)


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    name: str
    country: str
    at: datetime


def _ascii(text: str) -> str:
    """Fold accents away — the 3x5 font has no Á or ó."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def city_code(name: str) -> str:
    """A three-letter tag that fits the world-clock rows."""
    if name in _CODE_OVERRIDES:
        return _CODE_OVERRIDES[name]
    return _ascii(name)[:3].upper()


def _load(path: Path) -> dict | None:
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return None


def read_cities() -> list[City]:
    """Cities from iStat's world-clock menu, in the order the user put them."""
    data = _load(MENUBAR_PLIST)
    if not data:
        return []
    try:
        items = data["Time"]["Menu"]["Cities"]["Items"]
    except (KeyError, TypeError):
        return []

    cities: list[City] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("city")
        tz = item.get("timezone")
        if not name or not tz or name in _PSEUDO_CITIES:
            continue
        cities.append(
            City(
                name=_ascii(name),
                timezone=tz,
                latitude=float(item.get("latitude", 0.0)),
                longitude=float(item.get("longitude", 0.0)),
            )
        )
    return cities


def last_known_location(max_age: timedelta = timedelta(days=7)) -> Location | None:
    """iStat's most recent CoreLocation fix, if it is still fresh."""
    data = _load(STATUS_PLIST)
    if not data:
        return None
    fix = data.get("Location-Last")
    if not isinstance(fix, dict):
        return None
    latitude, longitude = fix.get("latitude"), fix.get("longitude")
    if latitude is None or longitude is None:
        return None

    stamp = fix.get("time")
    if stamp is None:
        return None
    at = datetime.fromtimestamp(float(stamp), tz=timezone.utc)
    if datetime.now(timezone.utc) - at > max_age:
        return None

    return Location(
        latitude=float(latitude),
        longitude=float(longitude),
        # Region before name: CoreLocation's `name` is a street address
        # ("7 Rue du Stade"), which is a poor label for a weather reading.
        name=_ascii(str(fix.get("region") or fix.get("name") or "")),
        country=str(fix.get("country") or ""),
        at=at,
    )
