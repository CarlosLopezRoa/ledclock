"""A menu bar app for steering the panel.

It never touches Bluetooth. The daemon owns the one connection the panel
allows, so this talks to the daemon over the control socket and nothing else —
which also means none of the CoreBluetooth and TCC trouble that shaped the
rest of this project applies here at all.

The icon is the temperature curve, drawn for the menu bar rather than taken
from the panel. Mirroring the panel's frames was the first attempt and it
failed plainly: those frames are a few lit pixels on black, so at this size
they collapse into a black square.

Text was the other candidate and it does not fit. Four clocks stacked would
give each digit about 2.5 points of height against the seven a digit needs to
be read, and the block would stand 24 points tall in a menu bar with roughly
20 to spare. A curve says its one thing at any size.
"""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSButton,
    NSImage,
    NSImageOnly,
    NSMakeRect,
    NSMakeSize,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSView,
)
from Foundation import NSData, NSObject, NSTimer
from PIL import Image, ImageDraw

from . import control

log = logging.getLogger(__name__)

#: The icon's own size in points. Wider than tall because it is a curve, but
#: only just: the menu bar is shared real estate and 40 points was greedy.
#: The shape still reads down to about 22 — widen or narrow here to taste.
ICON_WIDTH = 24
ICON_HEIGHT = 16
#: Stroke weight in device pixels. On a 2x display one pixel is half a point
#: and all but disappears against the menu bar's own text weight.
LINE_THICKNESS = 3
#: The now marker. Kept under the stroke weight on purpose: scaled with it,
#: the head swallowed the peak of the curve.
HEAD_RADIUS = 2
RULE_THICKNESS = 2
#: How often to ask the daemon what it is doing.
POLL_SECONDS = 2.0

#: The transport row's own view. Wide enough that the menu does not shrink
#: around it and leave the buttons crowding the edges.
TRANSPORT_WIDTH = 200
TRANSPORT_HEIGHT = 34

SPANISH = {
    "world_clocks": "Relojes",
    "weather_now": "Clima",
    "trend": "Tendencia",
    "forecast": "Pronóstico",
    "clock_local": "Hora local",
    "alert": "Alerta",
}


def _label(page: str | None) -> str:
    if not page:
        return "—"
    return SPANISH.get(page, page.replace("_", " ").title())


def draw_sparkline(spark: dict | None, *, paused: bool, linked: bool) -> Image.Image:
    """The temperature curve as a menu bar glyph, at 2x device resolution.

    Drawn for the menu bar rather than borrowed from the panel. Mirroring the
    panel's own frame was the first attempt and it failed for a plain reason:
    those frames are a few lit pixels on black, so at this size they collapse
    into a black square.

    Everything is drawn in black with varying alpha and the image is flagged
    as a template, so macOS tints it — white on a dark menu bar, black on a
    light one. Which also means the past/future distinction has to live in
    alpha, since a template image has no colour of its own.
    """
    width, height = ICON_WIDTH * 2, ICON_HEIGHT * 2
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = image.load()

    def put(x: int, y: int, alpha: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            if alpha > pixels[x, y][3]:
                pixels[x, y] = (0, 0, 0, alpha)

    if not linked or spark is None:
        # Nothing to plot: a flat dashed line says "no signal" without
        # pretending to be data.
        middle = height // 2
        for x in range(0, width, 5):
            for dx in range(3):
                for dy in range(LINE_THICKNESS):
                    put(x + dx, middle + dy, 90)
        return image

    values = spark["values"]
    now_x = int(spark.get("now", 0.5) * (width - 1))
    inset = LINE_THICKNESS
    top, bottom = inset, height - 1 - inset
    ink = 110 if paused else 255
    ahead = 60 if paused else 120

    def row(value: float) -> int:
        return bottom - round(value * (bottom - top))

    points = [
        (round(index * (width - 1) / (len(values) - 1)), row(value))
        for index, value in enumerate(values)
    ]

    # Drawn as two polylines rather than pixel by pixel, so that the stroke
    # keeps its weight on steep segments too — stamping single pixels left
    # the line looking thinner exactly where it climbed fastest.
    pen = ImageDraw.Draw(image)
    split = max(1, next((i for i, (x, _) in enumerate(points) if x > now_x),
                        len(points)) - 1)
    pen.line(points[:split + 1], fill=(0, 0, 0, ink),
             width=LINE_THICKNESS, joint="curve")
    pen.line(points[split:], fill=(0, 0, 0, ahead),
             width=LINE_THICKNESS, joint="curve")

    # Where we are in the day: a full-height rule plus a head on the curve, so
    # the split reads even where the line is flat. The rule matches the
    # stroke's weight; the head is deliberately *smaller* than the stroke
    # scaled up, which at three pixels turned it into a blob.
    for y in range(0, height):
        for dx in range(RULE_THICKNESS):
            put(now_x + dx, y, 55 if not paused else 35)
    head = row(values[min(len(values) - 1,
                          int(spark.get("now", 0.5) * (len(values) - 1)))])
    for dx in range(-HEAD_RADIUS, HEAD_RADIUS + 1):
        for dy in range(-HEAD_RADIUS, HEAD_RADIUS + 1):
            if dx * dx + dy * dy <= HEAD_RADIUS * HEAD_RADIUS:
                put(now_x + dx, head + dy, 255 if not paused else 130)
    return image


def _icon(spark: dict | None, *, paused: bool, linked: bool) -> NSImage:
    rendered = draw_sparkline(spark, paused=paused, linked=linked)
    buffer = io.BytesIO()
    rendered.save(buffer, format="PNG")
    data = NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue()))
    image = NSImage.alloc().initWithData_(data)
    image.setSize_(NSMakeSize(ICON_WIDTH, ICON_HEIGHT))
    # Template: macOS picks the colour for the menu bar it is sitting on.
    image.setTemplate_(True)
    return image


class Controller(NSObject):
    """Owns the status item and keeps it in step with the daemon.

    Every method here becomes an Objective-C selector unless it is marked
    @objc.python_method, and PyObjC checks the argument count against the
    colons in the selector name. The plain helpers are therefore decorated;
    refresh_ and the action methods are real selectors and are not.
    """

    def init(self):
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        self.status = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.state: dict = {}
        self.page_items: dict = {}
        self._build_menu()
        self.refresh_()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_SECONDS, self, "refresh:", None, True
        )
        return self

    # -- menu ---------------------------------------------------------------
    @objc.python_method
    def _item(self, title, action=None, key="", target=None):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, key
        )
        if action is not None:
            item.setTarget_(target or self)
        return item

    @objc.python_method
    def _build_menu(self):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        self.status_item = self._item("…")
        self.status_item.setEnabled_(False)
        menu.addItem_(self.status_item)
        menu.addItem_(NSMenuItem.separatorItem())

        menu.addItem_(self._transport_row())
        menu.addItem_(NSMenuItem.separatorItem())

        heading = self._item("Páginas")
        heading.setEnabled_(False)
        menu.addItem_(heading)
        self.pages_menu_anchor = menu.numberOfItems()
        menu.addItem_(NSMenuItem.separatorItem())

        menu.addItem_(self._item("Abrir config.yaml", "openConfig:"))
        menu.addItem_(self._item("Reiniciar el daemon", "restartDaemon:"))
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self._item("Salir", "quit:", "q"))

        self.menu = menu
        self.status.setMenu_(menu)

    @objc.python_method
    def _symbol(self, name: str, fallback: str):
        """An SF Symbol, or a text stand-in on a macOS too old to have them."""
        try:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                name, None
            )
        except AttributeError:
            image = None
        return image, fallback

    @objc.python_method
    def _transport_button(self, x, symbol, fallback, action, size=26):
        button = NSButton.alloc().initWithFrame_(
            NSMakeRect(x, (TRANSPORT_HEIGHT - size) / 2, size, size)
        )
        button.setBordered_(False)
        button.setTarget_(self)
        button.setAction_(action)
        image, text = self._symbol(symbol, fallback)
        if image is not None:
            button.setImage_(image)
            button.setImagePosition_(NSImageOnly)
        else:
            button.setTitle_(text)
        return button

    @objc.python_method
    def _transport_row(self):
        """Previous / play-pause / next, laid out like a transport control.

        A menu row cannot hold three buttons on its own, so this is an
        NSMenuItem carrying a custom view. One upshot is that the menu stays
        open when they are clicked, which is what you want here: stepping
        through pages is something you do several times in a row.
        """
        width = TRANSPORT_WIDTH
        view = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, width, TRANSPORT_HEIGHT)
        )
        centre = width / 2 - 13
        view.addSubview_(self._transport_button(
            centre - 44, "backward.fill", "◀◀", "prevPage:"))
        self.play_button = self._transport_button(
            centre, "pause.fill", "❚❚", "togglePause:", size=28)
        view.addSubview_(self.play_button)
        view.addSubview_(self._transport_button(
            centre + 44, "forward.fill", "▶▶", "nextPage:"))

        item = NSMenuItem.alloc().init()
        item.setView_(view)
        return item

    @objc.python_method
    def _set_play_symbol(self, paused: bool):
        image, fallback = self._symbol(
            "play.fill" if paused else "pause.fill", "▶" if paused else "❚❚"
        )
        if image is not None:
            self.play_button.setImage_(image)
        else:
            self.play_button.setTitle_(fallback)

    @objc.python_method
    def _sync_pages(self):
        """Rebuild the page rows when the daemon's list changes."""
        names = self.state.get("pages", [])
        if list(self.page_items) == names:
            return
        for item in self.page_items.values():
            self.menu.removeItem_(item)
        self.page_items = {}
        for offset, name in enumerate(names):
            item = self._item(f"  {_label(name)}", "togglePage:")
            item.setRepresentedObject_(name)
            self.menu.insertItem_atIndex_(item, self.pages_menu_anchor + offset)
            self.page_items[name] = item

    # -- polling ------------------------------------------------------------
    def refresh_(self, _timer=None):
        try:
            self.state = control.request("status")
        except control.ControlError:
            self.state = {}

        linked = bool(self.state.get("linked"))
        paused = bool(self.state.get("paused"))
        self.status.button().setImage_(
            _icon(self.state.get("spark"), paused=paused, linked=linked)
        )

        if not self.state:
            self.status_item.setTitle_("Daemon detenido")
        elif not linked:
            self.status_item.setTitle_("Buscando el panel…")
        else:
            showing = _label(self.state.get("showing"))
            pinned = self.state.get("pinned")
            suffix = " (fijada)" if pinned else " (en pausa)" if paused else ""
            self.status_item.setTitle_(f"{showing}{suffix}")

        self._set_play_symbol(paused)
        self._sync_pages()

        disabled = set(self.state.get("disabled", []))
        pinned = self.state.get("pinned")
        for name, item in self.page_items.items():
            if pinned == name:
                item.setState_(1)                       # on
                item.setTitle_(f"  {_label(name)} — sola")
            elif name in disabled:
                item.setState_(0)                       # off
                item.setTitle_(f"  {_label(name)}")
            else:
                item.setState_(1)
                item.setTitle_(f"  {_label(name)}")

    # -- actions ------------------------------------------------------------
    @objc.python_method
    def _send(self, command, **arguments):
        try:
            self.state = control.request(command, **arguments)
        except control.ControlError as exc:
            log.warning("%s failed: %s", command, exc)
        self.refresh_()

    def nextPage_(self, _sender):
        self._send("next")

    def prevPage_(self, _sender):
        self._send("prev")

    def togglePause_(self, _sender):
        self._send("toggle")

    def togglePage_(self, sender):
        """Click cycles a page: on → pinned alone → off → on.

        One control, because a menu bar has no room for three, and the order
        follows what you actually reach for: seeing everything, then only
        this, then never this.
        """
        name = sender.representedObject()
        if self.state.get("pinned") == name:
            self._send("disable", page=name)
        elif name in set(self.state.get("disabled", [])):
            self._send("enable", page=name)
        else:
            self._send("pin", page=name)

    def openConfig_(self, _sender):
        subprocess.run(["open", str(Path(__file__).resolve().parent.parent
                                    / "config.yaml")], check=False)

    def restartDaemon_(self, _sender):
        subprocess.run(
            ["launchctl", "kickstart", "-k",
             f"gui/{__import__('os').getuid()}/com.ledclock.daemon"],
            check=False,
        )

    def quit_(self, _sender):
        NSApplication.sharedApplication().terminate_(self)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    app = NSApplication.sharedApplication()
    # Accessory: a menu bar presence with no Dock icon and no main window.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    controller = Controller.alloc().init()
    app.setDelegate_(controller)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
