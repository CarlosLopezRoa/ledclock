"""Hand-drawn bitmap fonts.

At 32 px wide no TTF survives rasterisation, so every glyph is a literal
pixel grid. Glyphs are lists of equal-length strings; any character other
than '.' or ' ' lights the pixel. Digits are deliberately monospaced so a
ticking clock does not jitter; letters are proportional to buy back width.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Blank columns inserted between glyphs.
TRACKING = 1


@dataclass(frozen=True)
class Font:
    glyphs: dict[str, tuple[str, ...]]
    height: int
    tracking: int = TRACKING

    def glyph(self, char: str) -> tuple[str, ...] | None:
        return self.glyphs.get(char) or self.glyphs.get(char.upper())

    def char_width(self, char: str) -> int:
        g = self.glyph(char)
        return len(g[0]) if g else 0

    def width(self, text: str) -> int:
        """Advance width of `text`, excluding trailing tracking."""
        total = 0
        for char in text:
            w = self.char_width(char)
            if w:
                total += w + self.tracking
        return max(0, total - self.tracking)


def _f(*rows: str) -> tuple[str, ...]:
    return tuple(rows)


# --------------------------------------------------------------------------
# 3x5 — the workhorse. City codes, dates, forecast numbers.
# --------------------------------------------------------------------------
_G3 = {
    "0": _f("XXX", "X.X", "X.X", "X.X", "XXX"),
    "1": _f(".X.", "XX.", ".X.", ".X.", "XXX"),
    "2": _f("XXX", "..X", "XXX", "X..", "XXX"),
    "3": _f("XXX", "..X", ".XX", "..X", "XXX"),
    "4": _f("X.X", "X.X", "XXX", "..X", "..X"),
    "5": _f("XXX", "X..", "XXX", "..X", "XXX"),
    "6": _f("XXX", "X..", "XXX", "X.X", "XXX"),
    "7": _f("XXX", "..X", "..X", "..X", "..X"),
    "8": _f("XXX", "X.X", "XXX", "X.X", "XXX"),
    "9": _f("XXX", "X.X", "XXX", "..X", "XXX"),
    ":": _f(".", "X", ".", "X", "."),
    ".": _f(".", ".", ".", ".", "X"),
    ",": _f(".", ".", ".", "X", "X"),
    "-": _f("...", "...", "XXX", "...", "..."),
    "+": _f("...", ".X.", "XXX", ".X.", "..."),
    "/": _f("..X", "..X", ".X.", "X..", "X.."),
    "°": _f("XX", "XX", "..", "..", ".."),
    "%": _f("X.X", "..X", ".X.", "X..", "X.X"),
    "'": _f("X", "X", ".", ".", "."),
    " ": _f(".", ".", ".", ".", "."),
    "A": _f(".X.", "X.X", "XXX", "X.X", "X.X"),
    "B": _f("XX.", "X.X", "XX.", "X.X", "XX."),
    "C": _f(".XX", "X..", "X..", "X..", ".XX"),
    "D": _f("XX.", "X.X", "X.X", "X.X", "XX."),
    "E": _f("XXX", "X..", "XX.", "X..", "XXX"),
    "F": _f("XXX", "X..", "XX.", "X..", "X.."),
    "G": _f(".XX", "X..", "X.X", "X.X", ".XX"),
    "H": _f("X.X", "X.X", "XXX", "X.X", "X.X"),
    "I": _f("X", "X", "X", "X", "X"),
    "J": _f("..X", "..X", "..X", "X.X", ".X."),
    "K": _f("X.X", "X.X", "XX.", "X.X", "X.X"),
    "L": _f("X..", "X..", "X..", "X..", "XXX"),
    "M": _f("X...X", "XX.XX", "X.X.X", "X...X", "X...X"),
    "N": _f("XX.", "X.X", "X.X", "X.X", "X.X"),
    "O": _f("XXX", "X.X", "X.X", "X.X", "XXX"),
    "P": _f("XX.", "X.X", "XX.", "X..", "X.."),
    "Q": _f("XXX", "X.X", "X.X", "XXX", "..X"),
    "R": _f("XX.", "X.X", "XX.", "X.X", "X.X"),
    "S": _f(".XX", "X..", ".X.", "..X", "XX."),
    "T": _f("XXX", ".X.", ".X.", ".X.", ".X."),
    "U": _f("X.X", "X.X", "X.X", "X.X", "XXX"),
    "V": _f("X.X", "X.X", "X.X", "X.X", ".X."),
    "W": _f("X...X", "X...X", "X.X.X", "XX.XX", "X...X"),
    "X": _f("X.X", "X.X", ".X.", "X.X", "X.X"),
    "Y": _f("X.X", "X.X", ".X.", ".X.", ".X."),
    "Z": _f("XXX", "..X", ".X.", "X..", "XXX"),
}

FONT_3x5 = Font(glyphs=_G3, height=5)


# --------------------------------------------------------------------------
# 5x7 — the hero clock and the current temperature.
# --------------------------------------------------------------------------
_G5 = {
    "0": _f(".XXX.", "X...X", "X...X", "X...X", "X...X", "X...X", ".XXX."),
    "1": _f("..X..", ".XX..", "..X..", "..X..", "..X..", "..X..", ".XXX."),
    "2": _f(".XXX.", "X...X", "....X", "...X.", "..X..", ".X...", "XXXXX"),
    "3": _f("XXXXX", "...X.", "..XX.", "....X", "....X", "X...X", ".XXX."),
    "4": _f("...X.", "..XX.", ".X.X.", "X..X.", "XXXXX", "...X.", "...X."),
    "5": _f("XXXXX", "X....", "XXXX.", "....X", "....X", "X...X", ".XXX."),
    "6": _f("..XX.", ".X...", "X....", "XXXX.", "X...X", "X...X", ".XXX."),
    "7": _f("XXXXX", "....X", "...X.", "..X..", ".X...", ".X...", ".X..."),
    "8": _f(".XXX.", "X...X", "X...X", ".XXX.", "X...X", "X...X", ".XXX."),
    "9": _f(".XXX.", "X...X", "X...X", ".XXXX", "....X", "...X.", ".XX.."),
    ":": _f(".", ".", "X", ".", "X", ".", "."),
    "-": _f(".....", ".....", ".....", "XXXXX", ".....", ".....", "....."),
    "°": _f("XXX", "X.X", "XXX", "...", "...", "...", "..."),
    " ": _f("..", "..", "..", "..", "..", "..", ".."),
}

FONT_5x7 = Font(glyphs=_G5, height=7)
