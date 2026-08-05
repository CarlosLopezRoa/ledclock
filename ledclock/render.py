"""A tiny pixel canvas.

Every pixel is a physical LED, so nothing here antialiases, resamples, or
blends: text and icons are stamped one pixel at a time and the result is
saved as a plain RGB PNG.
"""

from __future__ import annotations

import io

from PIL import Image

from .font import Font
from .icons import PALETTE, Icon

RGB = tuple[int, int, int]

WIDTH = 32
HEIGHT = 32

BLACK: RGB = (0, 0, 0)
WHITE: RGB = (255, 255, 255)


#: Absolute temperature-to-colour ramp, in Celsius. Absolute on purpose: if
#: the scale followed each window's own range, the same colour would mean a
#: freezing morning one day and a heatwave the next.
TEMPERATURE_STOPS: list[tuple[float, RGB]] = [
    (-5.0, (70, 90, 255)),     # deep cold
    (5.0, (80, 170, 255)),     # cold
    (14.0, (150, 220, 235)),   # cool
    (20.0, (235, 235, 225)),   # comfortable, near white
    (26.0, (255, 200, 80)),    # warm
    (32.0, (255, 130, 50)),    # hot
    (38.0, (255, 60, 40)),     # dangerous
]


def temperature_color(celsius: float) -> RGB:
    """Blue through white to red, interpolated between the stops."""
    if celsius <= TEMPERATURE_STOPS[0][0]:
        return TEMPERATURE_STOPS[0][1]
    if celsius >= TEMPERATURE_STOPS[-1][0]:
        return TEMPERATURE_STOPS[-1][1]
    for (low, cold), (high, hot) in zip(TEMPERATURE_STOPS, TEMPERATURE_STOPS[1:]):
        if low <= celsius <= high:
            ratio = (celsius - low) / (high - low)
            return (
                round(cold[0] + (hot[0] - cold[0]) * ratio),
                round(cold[1] + (hot[1] - cold[1]) * ratio),
                round(cold[2] + (hot[2] - cold[2]) * ratio),
            )
    return TEMPERATURE_STOPS[-1][1]


def dim(color: RGB, factor: float) -> RGB:
    """Scale a colour toward black. Handy for night/secondary rows."""
    return (
        max(0, min(255, round(color[0] * factor))),
        max(0, min(255, round(color[1] * factor))),
        max(0, min(255, round(color[2] * factor))),
    )


class Canvas:
    def __init__(self, width: int = WIDTH, height: int = HEIGHT,
                 background: RGB = BLACK) -> None:
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width, height), background)
        self._px = self.image.load()

    # -- primitives ---------------------------------------------------------
    def pixel(self, x: int, y: int, color: RGB) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            self._px[x, y] = color

    def rect(self, x: int, y: int, w: int, h: int, color: RGB) -> None:
        for dy in range(h):
            for dx in range(w):
                self.pixel(x + dx, y + dy, color)

    def hline(self, x: int, y: int, length: int, color: RGB) -> None:
        self.rect(x, y, length, 1, color)

    def vline(self, x: int, y: int, length: int, color: RGB) -> None:
        self.rect(x, y, 1, length, color)

    def fill(self, color: RGB) -> None:
        self.rect(0, 0, self.width, self.height, color)

    # -- text ---------------------------------------------------------------
    def text(self, x: int, y: int, text: str, font: Font, color: RGB) -> int:
        """Draw `text` with its top-left at (x, y). Returns the advance width."""
        cursor = x
        for char in text:
            glyph = font.glyph(char)
            if glyph is None:
                continue
            for row_index, row in enumerate(glyph):
                for col_index, cell in enumerate(row):
                    if cell not in (".", " "):
                        self.pixel(cursor + col_index, y + row_index, color)
            cursor += len(glyph[0]) + font.tracking
        return max(0, cursor - font.tracking - x)

    def text_centered(self, y: int, text: str, font: Font, color: RGB) -> int:
        x = (self.width - font.width(text)) // 2
        return self.text(x, y, text, font, color)

    def text_right(self, right: int, y: int, text: str, font: Font, color: RGB) -> int:
        return self.text(right - font.width(text), y, text, font, color)

    # -- icons --------------------------------------------------------------
    def icon(self, x: int, y: int, icon: Icon, color: RGB | None = None) -> None:
        """Stamp an icon. `color` overrides the palette for every lit pixel."""
        for row_index, row in enumerate(icon.rows):
            for col_index, cell in enumerate(row):
                if cell in (".", " "):
                    continue
                self.pixel(x + col_index, y + row_index, color or PALETTE[cell])

    # -- output -------------------------------------------------------------
    def dimmed(self, gain: float) -> Image.Image:
        """The canvas with every channel scaled by `gain`.

        The panel's own brightness command bottoms out at 1, which is still
        bright in a dark room. Scaling the pixel values before encoding takes
        it further down, and continuously rather than in 100 steps. Ratios are
        preserved, so hues survive; very dark colours do round away to black.
        """
        if gain >= 1.0:
            return self.image
        table = [min(255, round(value * gain)) for value in range(256)] * 3
        return self.image.point(table)

    def scale(self, factor: float) -> None:
        """Dim the canvas in place. Used to drop a whole page to night level."""
        if factor >= 1.0:
            return
        table = [min(255, round(value * factor)) for value in range(256)] * 3
        self.image = self.image.point(table)
        self._px = self.image.load()

    def to_png(self, gain: float = 1.0) -> bytes:
        buffer = io.BytesIO()
        self.dimmed(gain).save(buffer, format="PNG")
        return buffer.getvalue()

    def to_preview_png(self, scale: int = 12, gap: int = 1,
                       gain: float = 1.0) -> bytes:
        """A blown-up render with a pixel grid, for eyeballing on a monitor."""
        cell = scale
        source = self.dimmed(gain).load()
        out = Image.new(
            "RGB", (self.width * cell, self.height * cell), (16, 16, 16)
        )
        px = out.load()
        for y in range(self.height):
            for x in range(self.width):
                color = source[x, y]
                for dy in range(cell - gap):
                    for dx in range(cell - gap):
                        px[x * cell + dx, y * cell + dy] = color
        buffer = io.BytesIO()
        out.save(buffer, format="PNG")
        return buffer.getvalue()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Canvas):
            return NotImplemented
        return self.image.tobytes() == other.image.tobytes()
