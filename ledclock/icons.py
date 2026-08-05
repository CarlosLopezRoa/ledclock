"""Weather icons as pixel grids, keyed by WMO weather code.

Each row string maps characters to colours through PALETTE, so a single
icon can be multicoloured (yellow sun, grey cloud, blue rain) without
carrying an image file around. '.' is transparent.
"""

from __future__ import annotations

from dataclasses import dataclass

RGB = tuple[int, int, int]

PALETTE: dict[str, RGB] = {
    "S": (255, 196, 40),    # sun
    "M": (216, 224, 196),   # moon, lit
    "d": (34, 36, 44),      # moon, in shadow — keeps the disc readable at new moon
    "C": (198, 208, 226),   # cloud, lit
    "D": (104, 116, 140),   # cloud, shadowed
    "R": (72, 150, 255),    # rain
    "W": (238, 246, 255),   # snow
    "B": (255, 224, 60),    # lightning
    "F": (146, 158, 176),   # fog
    "X": (255, 255, 255),   # stat markers, recoloured at draw time
}


@dataclass(frozen=True)
class Icon:
    rows: tuple[str, ...]

    @property
    def width(self) -> int:
        return len(self.rows[0])

    @property
    def height(self) -> int:
        return len(self.rows)


def _i(*rows: str) -> Icon:
    return Icon(tuple(rows))


# --------------------------------------------------------------------------
# 8x8 — the corner slot on the clock page and the forecast columns.
# --------------------------------------------------------------------------
ICONS_8 = {
    "clear": _i(
        "...SS...",
        ".S....S.",
        "..SSSS..",
        "S.SSSS.S",
        "S.SSSS.S",
        "..SSSS..",
        ".S....S.",
        "...SS...",
    ),
    "partly": _i(
        ".S..S...",
        "..SS....",
        ".SSSS...",
        "..SS.CC.",
        "....CCCC",
        "..CCCCCC",
        "..CCCCC.",
        "........",
    ),
    "cloudy": _i(
        "........",
        "..DDD...",
        ".DDDDD..",
        "DDDDDDD.",
        "..CCCC..",
        ".CCCCCCC",
        "CCCCCCCC",
        ".CCCCCC.",
    ),
    "fog": _i(
        "........",
        ".FFFFFF.",
        "........",
        "FFFFFFFF",
        "........",
        ".FFFFFF.",
        "........",
        "FFFFFF..",
    ),
    "drizzle": _i(
        "..CCC...",
        ".CCCCC..",
        "CCCCCCCC",
        ".CCCCCC.",
        "........",
        "..R..R..",
        "........",
        "...R..R.",
    ),
    "rain": _i(
        "..CCC...",
        ".CCCCC..",
        "CCCCCCCC",
        ".CCCCCC.",
        "........",
        ".R..R.R.",
        "R..R.R..",
        "........",
    ),
    "snow": _i(
        "..CCC...",
        ".CCCCC..",
        "CCCCCCCC",
        ".CCCCCC.",
        "........",
        ".W...W..",
        "...W...W",
        ".W...W..",
    ),
    "thunder": _i(
        "..CCC...",
        ".CCCCC..",
        "CCCCCCCC",
        ".CCCCCC.",
        "...BB...",
        "..BB....",
        ".BBBB...",
        "..BB....",
    ),
}


# --------------------------------------------------------------------------
# 16x16 — the hero slot on the current-weather page.
# --------------------------------------------------------------------------
ICONS_16 = {
    "clear": _i(
        ".......SS.......",
        ".......SS.......",
        "..S..........S..",
        "...S........S...",
        "......SSSS......",
        ".....SSSSSS.....",
        "....SSSSSSSS....",
        "SS..SSSSSSSS..SS",
        "SS..SSSSSSSS..SS",
        "....SSSSSSSS....",
        ".....SSSSSS.....",
        "......SSSS......",
        "...S........S...",
        "..S..........S..",
        ".......SS.......",
        ".......SS.......",
    ),
    "partly": _i(
        "..S..S..........",
        "...SS...........",
        "..SSSS..........",
        "S.SSSS.S........",
        "..SSSS......CC..",
        "...SS.....CCCCC.",
        "..S..S...CCCCCCC",
        "........CCCCCCCC",
        ".....CCCCCCCCCCC",
        "...CCCCCCCCCCCCC",
        "..CCCCCCCCCCCCCC",
        ".CCCCCCCCCCCCCCC",
        "CCCCCCCCCCCCCCCC",
        ".CCCCCCCCCCCCCC.",
        "................",
        "................",
    ),
    "cloudy": _i(
        "................",
        "....DDDD........",
        "..DDDDDDDD......",
        ".DDDDDDDDDDD....",
        "DDDDDDDDDDDDDD..",
        "DDDDDDDDDDDDDDD.",
        ".DDDDDDDDDDDDDD.",
        "......CCCC......",
        "....CCCCCCCC....",
        "..CCCCCCCCCCCC..",
        ".CCCCCCCCCCCCCC.",
        "CCCCCCCCCCCCCCCC",
        "CCCCCCCCCCCCCCCC",
        ".CCCCCCCCCCCCCC.",
        "................",
        "................",
    ),
    "fog": _i(
        "................",
        "................",
        "..FFFFFFFFFF....",
        "................",
        "FFFFFFFFFFFFFF..",
        "................",
        "..FFFFFFFFFFFF..",
        "................",
        "FFFFFFFFFFFF....",
        "................",
        "...FFFFFFFFFFFF.",
        "................",
        "FFFFFFFFFFFF....",
        "................",
        "................",
        "................",
    ),
    "drizzle": _i(
        "....CCCC........",
        "..CCCCCCCC......",
        ".CCCCCCCCCCC....",
        "CCCCCCCCCCCCCC..",
        "CCCCCCCCCCCCCCC.",
        "CCCCCCCCCCCCCCCC",
        ".CCCCCCCCCCCCCC.",
        "..CCCCCCCCCCCC..",
        "................",
        "...R.....R......",
        "...R.....R......",
        "................",
        "......R.....R...",
        "......R.....R...",
        "................",
        "................",
    ),
    "rain": _i(
        "....CCCC........",
        "..CCCCCCCC......",
        ".CCCCCCCCCCC....",
        "CCCCCCCCCCCCCC..",
        "CCCCCCCCCCCCCCC.",
        "CCCCCCCCCCCCCCCC",
        ".CCCCCCCCCCCCCC.",
        "..CCCCCCCCCCCC..",
        "................",
        "..R....R....R...",
        "..R....R....R...",
        "................",
        ".R....R....R....",
        ".R....R....R....",
        "................",
        "................",
    ),
    "snow": _i(
        "....CCCC........",
        "..CCCCCCCC......",
        ".CCCCCCCCCCC....",
        "CCCCCCCCCCCCCC..",
        "CCCCCCCCCCCCCCC.",
        "CCCCCCCCCCCCCCCC",
        ".CCCCCCCCCCCCCC.",
        "..CCCCCCCCCCCC..",
        "................",
        "..W....W....W...",
        ".WWW..WWW..WWW..",
        "..W....W....W...",
        "................",
        "....W....W......",
        "...WWW..WWW.....",
        "....W....W......",
    ),
    "thunder": _i(
        "....CCCC........",
        "..CCCCCCCC......",
        ".CCCCCCCCCCC....",
        "CCCCCCCCCCCCCC..",
        "CCCCCCCCCCCCCCC.",
        "CCCCCCCCCCCCCCCC",
        ".CCCCCCCCCCCCCC.",
        "..CCCCCCCCCCCC..",
        ".......BBB......",
        "......BBB.......",
        ".....BBB........",
        "....BBBBBB......",
        ".......BBB......",
        "......BBB.......",
        ".....BB.........",
        "................",
    ),
    # Alert-only. Three gusts, each trailing off into a curl. An earlier
    # version used closed loops and read as the digit 3.
    "wind": _i(
        "................",
        "................",
        "..CCCCCCCCCC....",
        "...........CC...",
        ".........CCC....",
        "................",
        "CCCCCCCCCCCCCC..",
        ".............CC.",
        "...........CCC..",
        "................",
        "..CCCCCCCC......",
        ".........CC.....",
        ".......CCC......",
        "................",
        "................",
        "................",
    ),
    "frost": _i(
        "................",
        ".......WW.......",
        "..W....WW....W..",
        "...W...WW...W...",
        "....W..WW..W....",
        ".....W.WW.W.....",
        "......WWWW......",
        ".WWWWWWWWWWWWWW.",
        ".WWWWWWWWWWWWWW.",
        "......WWWW......",
        ".....W.WW.W.....",
        "....W..WW..W....",
        "...W...WW...W...",
        "..W....WW....W..",
        ".......WW.......",
        "................",
    ),
}


#: Alert kind -> 16x16 icon. Storms and rain reuse the condition art.
ALERT_ICONS = {
    "storm": "thunder",
    "rain": "rain",
    "wind": "wind",
    "frost": "frost",
}


# --------------------------------------------------------------------------
# 5x5 stat markers. Drawn beside a number on the weather page, always
# recoloured by the caller, so they are monochrome here.
# --------------------------------------------------------------------------
# Markers sit immediately left of a number, so the one thing they must not do
# is read as a digit. An earlier wind glyph made 15 km/h look like "215".
MARKERS = {
    "humidity": _i(
        "..X..",
        ".XXX.",
        ".XXX.",
        "XXXXX",
        ".XXX.",
    ),
    "wind": _i(
        "XXXX.",
        ".....",
        "XXXXX",
        ".....",
        ".XXXX",
    ),
    "low": _i(
        "..X..",
        "..X..",
        "X.X.X",
        ".XXX.",
        "..X..",
    ),
    "high": _i(
        "..X..",
        ".XXX.",
        "X.X.X",
        "..X..",
        "..X..",
    ),
    "precip": _i(
        ".XXX.",
        "XXXXX",
        "..X..",
        "..X..",
        ".XX..",
    ),
    "uv": _i(
        "X.X.X",
        ".XXX.",
        "XXXXX",
        ".XXX.",
        "X.X.X",
    ),
    "pressure": _i(
        ".XXX.",
        "X..XX",
        "X.X.X",
        "X...X",
        ".XXX.",
    ),
    "clouds": _i(
        ".....",
        ".XXX.",
        "XXXXX",
        "XXXXX",
        ".....",
    ),
}


#: WMO 4677 code -> icon name. Anything unlisted falls back to "cloudy".
_WMO_TO_ICON = {
    0: "clear",
    1: "clear",
    2: "partly",
    3: "cloudy",
    45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "drizzle", 57: "drizzle",
    61: "rain", 63: "rain", 65: "rain",
    66: "rain", 67: "rain",
    71: "snow", 73: "snow", 75: "snow", 77: "snow",
    80: "rain", 81: "rain", 82: "rain",
    85: "snow", 86: "snow",
    95: "thunder", 96: "thunder", 99: "thunder",
}


#: Codes where the sky is clear enough to bother drawing the moon.
CLEAR_CODES = frozenset({0, 1})


def icon_name(wmo_code: int | None) -> str:
    return _WMO_TO_ICON.get(wmo_code if wmo_code is not None else -1, "cloudy")


def for_code(wmo_code: int | None, size: int = 8) -> Icon:
    """The condition icon. Night skies are the moon's job — see ledclock.moon."""
    table = ICONS_16 if size >= 16 else ICONS_8
    return table[icon_name(wmo_code)]
