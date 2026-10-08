"""The long-running loop: render, push, rotate, hand off, survive.

Two things shape this file.

BLE on this panel is fragile — it drops when the phone app grabs it, when USB
power blips, or for no reason at all — so every failure path leads back to a
reconnect with backoff rather than to an exit.

And the panel holds the last frame it was sent *forever*. That is harmless for
a weather page and actively misleading for a clock: a frozen 22:47 is worse
than no clock at all. The panel does have an RTC and a firmware clock mode
that runs with nothing connected, so whenever the daemon knowingly lets go —
shutdown, logout, the lid closing — it hands the display over to that first.

What it cannot cover is an *unannounced* loss: a crash, a kill -9, walking out
of Bluetooth range. Nothing can be sent after the fact, so the panel keeps the
last frame until something reconnects.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone

from bleak.exc import BleakError

from . import macsleep, pages
from . import control
from .alerts import AlertTracker
from .config import Config, is_active
from .ipixel import IPixel, IPixelError
from .sources import weather as weather_source

log = logging.getLogger(__name__)

RECONNECT_MIN = 3.0
RECONNECT_MAX = 60.0
#: Hard deadline on a connection attempt. CoreBluetooth will otherwise wait
#: indefinitely, which after a wake means the daemon never comes back.
CONNECT_TIMEOUT = 45.0
#: How many fruitless "not found" attempts before pointing at the likely cause.
MISSING_HINT_AFTER = 4
#: How often to re-check the clock while the panel is sleeping.
ASLEEP_POLL = 30.0


class Suspended(Exception):
    """Unwinds the carousel so the link can be dropped before the Mac sleeps."""


#: How soon to try the weather again after a fetch did not produce fresh data.
RETRY_AFTER_FAILURE = 60.0


class WeatherFeed:
    """Keeps the newest weather reading around for the render loop."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self.current: weather_source.Weather | None = None
        #: Resolved alongside the weather, so the pages' sunrise/sunset follows
        #: the Mac when follow_istat_location is on.
        self.location: tuple[float, float] | None = None

    async def refresh(self) -> bool:
        """Fetch, and report whether what we now hold is fresh."""
        latitude, longitude, _ = self._config.weather.resolve_coordinates()
        self.location = (latitude, longitude)
        try:
            fresh = await weather_source.fetch(
                latitude,
                longitude,
                unit=self._config.weather.unit,
                ttl=self._config.weather.refresh_seconds,
            )
        except Exception as exc:  # network, DNS, malformed payload
            log.warning("weather refresh failed: %s", exc)
            return False

        # A failed fetch falls back to the disk cache, and the disk cache can
        # be older than what is already in memory. Taking it anyway once
        # replaced an hour-old forecast with a thirteen-day-old one: the trend
        # window no longer overlapped the data, and the menu bar graph went
        # flat until the next successful fetch. Never trade down.
        held = self.current
        if held is not None and fresh.fetched_at < held.fetched_at:
            log.info(
                "keeping weather from %.0f min ago over a cache from %.0f min ago",
                held.age / 60,
                fresh.age / 60,
            )
            return False
        self.current = fresh
        return not fresh.stale

    async def run(self) -> None:
        while True:
            fresh = await self.refresh()
            # Straight after a wake the network is usually not back yet, so
            # the first fetch fails. Waiting the full interval before trying
            # again left stale data on screen for ten minutes every morning.
            await asyncio.sleep(
                self._config.weather.refresh_seconds if fresh else RETRY_AFTER_FAILURE
            )


async def _wait_for(events: list[asyncio.Event], timeout: float | None) -> None:
    """Sleep up to `timeout`, returning early if any event fires."""
    waiters = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.feed = WeatherFeed(config)
        self.alerts = AlertTracker(config.alerts)

        # Carousel state, all of it reachable over the control socket.
        #: Names the page on screen. Starts before the first so that the
        #: opening pass lands on rotation[0] rather than skipping it.
        self.index = -1
        self._jumped = False
        self.paused = False
        #: When set, the only page shown (bar a fresh alert).
        self.pinned: str | None = None
        #: Pages excluded from the rotation.
        self.disabled: set[str] = set()
        #: What is on the panel right now, and the frame itself — the menu bar
        #: shows that frame as its icon, so it comes from here rather than
        #: being rendered twice and risking disagreement.
        self.showing: str | None = None
        self.frame: bytes | None = None
        self.linked = False
        #: Set when the control socket changes brightness, so the
        #: carousel pushes it to the panel on its next pass.
        self.brightness_dirty = False

        self.stop = asyncio.Event()
        #: Set while the Mac is up. Cleared just before it sleeps.
        self.awake = asyncio.Event()
        self.awake.set()
        #: Fires whenever the awake state flips. `awake` alone cannot do this
        #: job: waiters only ever see an Event being *set*, so going to sleep —
        #: which clears it — would pass unnoticed. Without this the carousel
        #: slept out the rest of its dwell while macOS suspended, and the
        #: handoff to the panel's own clock never ran.
        self.nudge = asyncio.Event()

    # -- external signals ---------------------------------------------------
    def request_stop(self) -> None:
        log.info("stop requested")
        self.stop.set()

    def on_sleep(self) -> None:
        self.awake.clear()
        self.nudge.set()

    def on_wake(self) -> None:
        self.awake.set()
        self.nudge.set()

    # -- handing the panel back --------------------------------------------
    async def release(self, panel: IPixel) -> None:
        """Leave the panel in a state that survives us being gone."""
        mode = self.config.device.idle_mode
        try:
            if mode == "clock":
                await panel.set_clock_mode(
                    style=self.config.device.clock_style,
                    show_date=self.config.device.clock_show_date,
                )
                log.info("handed the panel back to its own clock")
            elif mode == "off":
                await panel.set_power(False)
                log.info("blanked the panel")
            else:
                log.info("leaving the last frame on the panel")
        except (IPixelError, BleakError, OSError, TimeoutError) as exc:
            # The link is often already gone by the time we get here.
            log.warning("could not hand the panel off: %s", exc)

    # -- the control surface ------------------------------------------------
    def handlers(self) -> dict:
        """Everything the menu bar and the CLI can ask for."""
        return {
            "ping": lambda: {},
            "status": self.snapshot,
            "next": lambda: (self.advance(1), self.snapshot())[1],
            "prev": lambda: (self.advance(-1), self.snapshot())[1],
            "pause": self._pause,
            "resume": self._resume,
            "toggle": self._toggle,
            "pin": self._pin,
            "unpin": self._unpin,
            "enable": self._enable,
            "disable": self._disable,
            "brightness": self._brightness,
        }

    def sparkline(self, points: int = 44) -> dict | None:
        """The temperature curve, normalised, for the menu bar icon.

        Sent as numbers rather than as the rendered frame: the panel's frames
        are dark on black, which at menu bar size collapses into a black
        square. The icon has to be drawn for its own medium.
        """
        weather = self.feed.current
        if weather is None or len(weather.hours) < 2:
            return None

        from datetime import timedelta

        half = timedelta(hours=self.config.display.trend_hours / 2)
        now = datetime.now(timezone.utc)
        start, end = now - half, now + half
        values = [
            pages._sample(weather.hours, start + (end - start) * (i / (points - 1)))
            for i in range(points)
        ]
        if any(value is None for value in values):
            return None

        low, high = min(values), max(values)
        # Same floor as the trend page: a flat night should look flat, not be
        # amplified into a mountain range.
        if high - low < pages.TREND_MIN_SPAN:
            middle = (high + low) / 2
            low, high = (middle - pages.TREND_MIN_SPAN / 2,
                         middle + pages.TREND_MIN_SPAN / 2)
        return {
            "values": [(value - low) / (high - low) for value in values],
            "now": 0.5,
        }

    def snapshot(self) -> dict:
        import base64

        return {
            "spark": self.sparkline(),
            "linked": self.linked,
            "showing": self.showing,
            "paused": self.paused,
            "pinned": self.pinned,
            "pages": list(self.config.display.pages),
            "disabled": sorted(self.disabled),
            "rotation": self.rotation(),
            "alert": getattr(self.alerts.current(), "kind", None),
            "brightness": self.config.device.brightness,
            "dwell": self.config.display.dwell_seconds,
            # The panel's own frame, so the menu bar icon cannot drift out of
            # step with what is actually on the wall.
            "frame": base64.b64encode(self.frame).decode() if self.frame else None,
        }

    def _pause(self) -> dict:
        self.paused = True
        return self.snapshot()

    def _resume(self) -> dict:
        self.paused = False
        self.nudge.set()
        return self.snapshot()

    def _toggle(self) -> dict:
        return self._resume() if self.paused else self._pause()

    def _pin(self, page: str) -> dict:
        if page not in pages.PAGES:
            raise ValueError(f"no such page: {page}")
        self.pinned = page
        self.paused = False
        # Report the destination now rather than a poll later, same as advance().
        self.showing = page
        self._jumped = True
        self.nudge.set()
        return self.snapshot()

    def _unpin(self) -> dict:
        self.pinned = None
        self.nudge.set()
        return self.snapshot()

    def _enable(self, page: str) -> dict:
        self.disabled.discard(page)
        self.nudge.set()
        return self.snapshot()

    def _disable(self, page: str) -> dict:
        if page not in pages.PAGES:
            raise ValueError(f"no such page: {page}")
        self.disabled.add(page)
        if self.pinned == page:
            self.pinned = None
        self.nudge.set()
        return self.snapshot()

    def _brightness(self, value: int) -> dict:
        value = max(1, min(100, int(value)))
        self.config = replace(
            self.config, device=replace(self.config.device, brightness=value)
        )
        self.brightness_dirty = True
        self.nudge.set()
        return self.snapshot()

    # -- what to show next --------------------------------------------------
    def rotation(self, include_alert: bool = False) -> list[str]:
        """The pages currently eligible, in order.

        A pin wins over everything except a fresh alert. With every page
        switched off we fall back to the configured list rather than showing
        nothing — an empty panel would look like a crash.
        """
        if self.pinned:
            return [self.pinned]
        pages_in_play = [
            name for name in self.config.display.pages if name not in self.disabled
        ]
        if not pages_in_play:
            pages_in_play = list(self.config.display.pages)
        if include_alert and "alert" not in pages_in_play:
            pages_in_play.append("alert")
        return pages_in_play

    def _next_page(self, include_alert: bool) -> str:
        """The page to draw on this pass.

        `index` names the page *on screen*, not the one after it. That
        distinction matters: with the other convention a manual jump had to
        guess whether the loop had already moved on, and two quick clicks of
        Next landed on the same page twice.
        """
        rotation = self.rotation(include_alert)
        if self.paused and self.showing in rotation:
            # Stepping while paused already chose the page; consume the jump
            # so that resuming moves on rather than repeating it.
            self._jumped = False
            return self.showing
        if self._jumped:
            # advance() already placed us; do not step past it.
            self._jumped = False
        else:
            self.index += 1
        return rotation[self.index % len(rotation)]

    def advance(self, delta: int = 1) -> None:
        """Move `delta` pages from the one on screen, right now.

        Takes effect immediately rather than waiting for the render loop, so
        clicking Next twice in a second moves two pages instead of one.

        A pause is *kept*. Stepping while paused is how you browse: pausing
        says "I am steering now", and having Next hand control back to the
        carousel would undo the thing you just asked for.
        """
        self.index += delta
        self._jumped = True
        rotation = self.rotation()
        # Report the destination now, so a menu opened this instant is right
        # instead of lagging a poll behind.
        self.showing = rotation[self.index % len(rotation)]
        self.nudge.set()

    # -- the carousel -------------------------------------------------------
    async def drive(self, panel: IPixel) -> None:
        last_png: bytes | None = None
        powered = True

        while not self.stop.is_set():
            self.nudge.clear()

            if not self.awake.is_set():
                # Hand over, then let go of the link entirely. Holding it
                # across a sleep is what left the panel unreachable: the Mac
                # side dies without a GATT teardown, the panel keeps believing
                # it is connected, and a connected peripheral stops
                # advertising — so nothing can find it again, sometimes until
                # it is unplugged.
                log.info("the Mac is going away — handing the panel over")
                await self.release(panel)
                raise Suspended

            now = datetime.now(timezone.utc)
            if not is_active(self.config.display.active_hours, now.astimezone()):
                if powered:
                    log.info("outside active hours — blanking the panel")
                    await panel.set_power(False)
                    powered = False
                    last_png = None
                await _wait_for([self.stop, self.nudge], timeout=ASLEEP_POLL)
                continue

            if not powered:
                log.info("back inside active hours — waking the panel")
                await panel.set_power(True)
                powered = True

            if self.brightness_dirty:
                await panel.set_brightness(self.config.device.brightness)
                self.brightness_dirty = False
                last_png = None

            self.alerts.update(self.feed.current, now)
            warning = self.alerts.current()

            # A brand-new warning jumps the queue — ahead of a pin, too. One
            # you only see a full rotation later is not much of a warning, and
            # one suppressed because a page was pinned is no warning at all.
            fresh = self.alerts.take_new()
            if fresh is not None:
                log.info("new alert: %s in %dh", fresh.kind, fresh.lead_hours)
                name = "alert"
            else:
                name = self._next_page(warning is not None)

            ctx = pages.PageContext(
                config=self.config, now=now, weather=self.feed.current,
                location=self.feed.location, alert=warning,
            )
            png = pages.render(name, ctx).to_png(gain=self.config.display.gain)
            self.showing = name
            self.frame = png

            if png != last_png:
                await panel.send_png(png)
                last_png = png
                log.debug("pushed page %s (%d bytes)", name, len(png))

            # `nudge` here is what makes the sleep handoff possible at all: a
            # dwell that only watched `stop` would run to the end while macOS
            # was already suspending, and the link would die first.
            await _wait_for([self.stop, self.nudge],
                            timeout=self.config.display.dwell_seconds)

            if not panel.is_connected:
                raise IPixelError("panel disconnected")

    # -- supervision --------------------------------------------------------
    async def run(self) -> None:
        if not self.config.device.address:
            raise IPixelError(
                "no device address configured — run `ledclock scan` and put the "
                "address into config.yaml under device.address"
            )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_stop)

        controller = control.ControlServer(self.handlers())
        try:
            await controller.start()
        except OSError as exc:
            log.warning("control socket unavailable: %s", exc)

        macsleep.observe(
            self.on_sleep, self.on_wake,
            include_screens=self.config.device.handoff_on_screen_sleep,
        )
        pump = asyncio.create_task(macsleep.pump(), name="runloop-pump")

        await self.feed.refresh()
        refresher = asyncio.create_task(self.feed.run(), name="weather")

        delay = RECONNECT_MIN
        missing = 0
        try:
            while not self.stop.is_set():
                # Never even try while the Mac is on its way out: a link
                # opened now would be torn down uncleanly moments later.
                if not self.awake.is_set():
                    await _wait_for([self.awake, self.stop], timeout=None)
                    if self.stop.is_set():
                        break
                    delay = RECONNECT_MIN

                panel = IPixel(
                    self.config.device.address,
                    ack_timeout=self.config.device.ack_timeout,
                )
                try:
                    log.info("connecting to %s", self.config.device.address)
                    # CoreBluetooth's connect has no deadline of its own, and
                    # after the Mac wakes it can sit there forever: observed
                    # once as a daemon that was alive, refreshing weather, and
                    # never reconnecting for hours. Nothing below this call is
                    # allowed to block without a bound.
                    info = await asyncio.wait_for(panel.connect(), timeout=CONNECT_TIMEOUT)
                    if (info.width, info.height) != (32, 32):
                        log.warning(
                            "panel reports %dx%d but the pages are drawn for 32x32",
                            info.width, info.height,
                        )
                    await panel.set_brightness(self.config.device.brightness)
                    # Unconditionally, because we may have been the ones who
                    # blanked it: idle_mode "off" leaves the panel dark, and
                    # drive() starts out believing the panel is lit, so
                    # nothing else here would ever switch it back on.
                    await panel.set_power(True)
                    self.linked = True
                    delay = RECONNECT_MIN
                    missing = 0
                    await self.drive(panel)
                    # Only reached on a clean stop, while still connected.
                    await self.release(panel)
                except asyncio.CancelledError:
                    raise
                except Suspended:
                    log.info("dropping the link so the panel keeps advertising")
                    delay = RECONNECT_MIN
                    continue
                except (IPixelError, BleakError, OSError, TimeoutError) as exc:
                    if self.stop.is_set():
                        break
                    if "not found" in str(exc).lower():
                        missing += 1
                        if missing == MISSING_HINT_AFTER:
                            log.warning(
                                "the panel has not advertised for %d attempts. It "
                                "may still think it is connected — a peripheral "
                                "holding a stale link stops advertising. "
                                "Unplugging its USB for a few seconds clears that.",
                                missing,
                            )
                    log.warning("panel link lost (%s); retrying in %.0fs", exc, delay)
                    self.nudge.clear()
                    await _wait_for([self.stop, self.nudge], timeout=delay)
                    if self.nudge.is_set():
                        # Woken from sleep: the panel is there again, so do not
                        # sit out a backoff earned while the Mac was suspended.
                        log.info("woken up — retrying the panel straight away")
                        delay = RECONNECT_MIN
                        continue
                    delay = min(delay * 2, RECONNECT_MAX)
                finally:
                    self.linked = False
                    await panel.disconnect()
        finally:
            await controller.stop()
            for task in (refresher, pump):
                task.cancel()
            await asyncio.gather(refresher, pump, return_exceptions=True)


async def run(config: Config) -> None:
    """Connect, drive, and reconnect until asked to stop."""
    await Daemon(config).run()
