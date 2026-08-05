"""Notice when macOS is about to sleep or has just woken.

The panel holds whatever frame it was last sent, forever. That is fine for a
weather page and actively misleading for a clock, so the daemon wants to hand
the display back to the panel's own firmware clock *before* the link goes
away — and the lid closing is the common way it goes away.

macOS announces that through NSWorkspace, whose notifications arrive on the
main thread's run loop. Nothing else in this process spins that run loop, so
`pump()` gives it a slice periodically from inside the asyncio loop.

Availability is optional: without pyobjc (or off macOS) `observe()` returns
False and the daemon simply does without the early warning.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

try:
    import objc
    from AppKit import (
        NSWorkspace,
        NSWorkspaceDidWakeNotification,
        NSWorkspaceScreensDidSleepNotification,
        NSWorkspaceScreensDidWakeNotification,
        NSWorkspaceWillSleepNotification,
    )
    from Foundation import NSDate, NSObject, NSRunLoop

    AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the host
    AVAILABLE = False


#: Seconds between run-loop slices. macOS gives only a few seconds of warning
#: before sleeping, so this has to be well under that.
PUMP_INTERVAL = 0.5
#: How long each slice may spend draining the run loop.
PUMP_SLICE = 0.02


if AVAILABLE:

    class _Observer(NSObject):
        """Bridges NSWorkspace notifications back into plain callables."""

        def initWithHandlers_(self, handlers):  # noqa: N802 - ObjC naming
            # objc.super, not builtins.super: this is an ObjC class.
            self = objc.super(_Observer, self).init()
            if self is None:
                return None
            self._on_sleep, self._on_wake = handlers
            return self

        def workspaceWillSleep_(self, notification):  # noqa: N802
            log.info("macOS is going away (%s)", notification.name())
            self._on_sleep()

        def workspaceDidWake_(self, notification):  # noqa: N802
            log.info("macOS is back (%s)", notification.name())
            self._on_wake()


_observer = None  # kept alive for the process lifetime; ObjC does not retain it


def observe(on_sleep: Callable[[], None], on_wake: Callable[[], None], *,
            include_screens: bool = True) -> bool:
    """Register for sleep/wake. Returns False when unsupported.

    `include_screens` also treats the display going dark as sleep, and that
    is not belt-and-braces — it is the only reliable warning of the two.
    Observed on this machine: macOS tore Bluetooth down *before* posting
    NSWorkspaceWillSleepNotification, so the handoff found the panel already
    unreachable 110 ms after the notification arrived. The screen sleeps
    minutes earlier, while the radio is still up.

    It also matches intent: a dark screen means nobody is at the desk, which
    is exactly when the panel should be showing a clock that keeps time on
    its own rather than a dashboard nobody is reading.
    """
    global _observer
    if not AVAILABLE:
        log.info("no AppKit here; sleep handoff disabled")
        return False

    _observer = _Observer.alloc().initWithHandlers_((on_sleep, on_wake))
    centre = NSWorkspace.sharedWorkspace().notificationCenter()

    events = [
        ("workspaceWillSleep:", NSWorkspaceWillSleepNotification),
        ("workspaceDidWake:", NSWorkspaceDidWakeNotification),
    ]
    if include_screens:
        events += [
            ("workspaceWillSleep:", NSWorkspaceScreensDidSleepNotification),
            ("workspaceDidWake:", NSWorkspaceScreensDidWakeNotification),
        ]

    for selector, name in events:
        centre.addObserver_selector_name_object_(_observer, selector, name, None)
    return True


async def pump(interval: float = PUMP_INTERVAL) -> None:
    """Give the main run loop a slice now and then, forever."""
    if not AVAILABLE:
        return
    run_loop = NSRunLoop.currentRunLoop()
    while True:
        run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(PUMP_SLICE))
        await asyncio.sleep(interval)
