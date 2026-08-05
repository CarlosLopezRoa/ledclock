"""Command line entry points.

`preview` is deliberately the richest command: designing for 32x32 is a
tight iteration loop, and doing it against a scaled PNG on the laptop is far
faster than doing it over a 1 KB/s Bluetooth link.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import daemon, ipixel, pages
from .config import Config, load
from .render import Canvas
from .sources import istat
from .sources import weather as weather_source

log = logging.getLogger("ledclock")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
async def _weather_for(config: Config) -> weather_source.Weather | None:
    latitude, longitude, _ = config.weather.resolve_coordinates()
    try:
        return await weather_source.fetch(
            latitude, longitude, unit=config.weather.unit
        )
    except Exception as exc:
        log.warning("could not load weather (%s); rendering without it", exc)
        return None


def _context(config: Config, weather) -> pages.PageContext:
    latitude, longitude, _ = config.weather.resolve_coordinates()
    return pages.PageContext(
        config=config, now=datetime.now(timezone.utc), weather=weather,
        location=(latitude, longitude),
    )


def _require_address(config: Config, override: str | None) -> str:
    address = override or config.device.address
    if not address:
        raise SystemExit(
            "No device address. Run `ledclock scan`, then set device.address "
            "in config.yaml (or pass --address)."
        )
    return address


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
async def cmd_scan(args, config: Config) -> int:
    print(f"Scanning {args.timeout:.0f}s for {ipixel.NAME_PREFIX}* ...")
    devices = await ipixel.scan(timeout=args.timeout)
    if not devices:
        print(
            "\nNothing found. Check that:\n"
            "  - the panel is powered over USB (5V, 2A or better)\n"
            "  - the iPixel Color app on your phone is closed (BLE is "
            "single-client; the phone will hold the panel hostage)\n"
            "  - your terminal has Bluetooth permission in System Settings > "
            "Privacy & Security > Bluetooth"
        )
        return 1
    for device in devices:
        print(f"  {device.address}  {device.name}")
    print("\nCopy the address into config.yaml under device.address.")
    return 0


async def cmd_probe(args, config: Config) -> int:
    address = _require_address(config, args.address)
    async with ipixel.IPixel(address) as panel:
        info = panel.info
        print(info)
        if (info.width, info.height) != (32, 32):
            print(
                f"\nWarning: pages are drawn for 32x32 but this panel is "
                f"{info.width}x{info.height}."
            )
    return 0


async def cmd_cities(args, config: Config) -> int:
    cities = istat.read_cities()
    if not cities:
        print("No cities found in iStat Menus preferences.")
    else:
        print("Cities configured in iStat Menus:\n")
        for city in cities:
            print(
                f"  - {{code: {city.code}, name: {city.name}, "
                f"tz: {city.timezone}}}   # {city.latitude:.4f}, {city.longitude:.4f}"
            )
    fix = istat.last_known_location()
    if fix:
        print(
            f"\nLast known location: {fix.latitude:.6f}, {fix.longitude:.6f}"
            f"  ({fix.name}, {fix.country}) at {fix.at.astimezone():%Y-%m-%d %H:%M}"
        )
    return 0


def _gain(args, config: Config) -> float:
    """An explicit --gain wins; otherwise show what the panel actually gets."""
    return config.display.gain if args.gain is None else args.gain


async def cmd_preview(args, config: Config) -> int:
    weather = None if args.no_weather else await _weather_for(config)
    ctx = _context(config, weather)
    gain = _gain(args, config)

    names = list(pages.PAGES) if args.page in (None, "all") else [args.page]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for name in names:
        canvas = pages.render(name, ctx)
        path = out_dir / f"{name}.png"
        path.write_bytes(canvas.to_preview_png(scale=args.scale, gain=gain))
        if args.raw:
            (out_dir / f"{name}.raw.png").write_bytes(canvas.to_png(gain=gain))
        written.append(path)
        print(f"  {path}")

    if args.open and written:
        subprocess.run(["open", *map(str, written)], check=False)
    return 0


async def cmd_push(args, config: Config) -> int:
    address = _require_address(config, args.address)
    png = Path(args.image).read_bytes()
    async with ipixel.IPixel(address) as panel:
        if args.brightness:
            await panel.set_brightness(args.brightness)
        await panel.send_png(png, slot=args.slot)
    print(f"Sent {len(png)} bytes.")
    return 0


async def cmd_page(args, config: Config) -> int:
    """Render one page and push it — the fastest hardware feedback loop."""
    address = _require_address(config, args.address)
    weather = None if args.no_weather else await _weather_for(config)
    canvas = pages.render(args.page, _context(config, weather))
    gain = _gain(args, config)
    async with ipixel.IPixel(address) as panel:
        await panel.set_brightness(args.brightness or config.device.brightness)
        await panel.send_png(canvas.to_png(gain=gain))
    print(f"Sent page {args.page} at gain {gain:.2f}.")
    return 0


async def cmd_test_pattern(args, config: Config) -> int:
    """Coloured quadrants plus a corner dot: proves orientation and channel order."""
    address = _require_address(config, args.address)
    canvas = Canvas()
    half = canvas.width // 2
    canvas.rect(0, 0, half, half, (255, 0, 0))        # top-left    red
    canvas.rect(half, 0, half, half, (0, 255, 0))     # top-right   green
    canvas.rect(0, half, half, half, (0, 0, 255))     # bottom-left blue
    canvas.rect(half, half, half, half, (60, 60, 60)) # bottom-right grey
    canvas.rect(0, 0, 2, 2, (255, 255, 255))          # origin marker

    print(
        "Expect: red top-left, green top-right, blue bottom-left, grey "
        "bottom-right, white 2x2 dot in the top-left corner."
    )
    async with ipixel.IPixel(address) as panel:
        await panel.set_brightness(args.brightness or config.device.brightness)
        await panel.send_png(canvas.to_png())
    return 0


async def cmd_clock(args, config: Config) -> int:
    """Hand the panel back to its own firmware clock."""
    address = _require_address(config, args.address)
    async with ipixel.IPixel(address) as panel:
        if args.brightness:
            await panel.set_brightness(args.brightness)
        await panel.set_clock_mode(
            style=args.style, use_24h=not args.twelve_hour, show_date=args.date
        )
    print(
        f"Panel switched to its built-in clock (style {args.style}). "
        "It keeps time on its own from here — nothing needs to stay connected."
    )
    return 0


async def cmd_show_slot(args, config: Config) -> int:
    address = _require_address(config, args.address)
    async with ipixel.IPixel(address) as panel:
        await panel.show_slot(args.slot)
    print(f"Asked the panel to show slot {args.slot}.")
    return 0


#: Must match tools/install_agent.sh.
AGENT_LABEL = "com.ledclock.daemon"
AGENT_LOG = Path.home() / "Library" / "Logs" / "ledclock.log"


def _agent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *arguments], capture_output=True, text=True, check=False
    )


def _agent_pid() -> int | None:
    listing = _launchctl("list").stdout
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[2] == AGENT_LABEL:
            return int(parts[0]) if parts[0].isdigit() and parts[0] != "-" else None
    return None


async def cmd_agent(args, config: Config) -> int:
    """Drive the LaunchAgent, so the launchctl incantations live in one place.

    Worth having because only one thing can hold the panel at a time: every
    hands-on command means stopping the agent first and starting it after.
    """
    domain = f"gui/{os.getuid()}"
    plist = _agent_plist()

    if args.action == "status":
        pid = _agent_pid()
        print(f"agent   {'running, pid ' + str(pid) if pid else 'not running'}")
        print(f"plist   {plist if plist.exists() else 'not installed'}")
        print(f"log     {AGENT_LOG}")
        if AGENT_LOG.exists():
            interesting = [
                line for line in AGENT_LOG.read_text(errors="replace").splitlines()
                if "httpx" not in line
            ]
            if interesting:
                print("\nlast few lines:")
                for line in interesting[-5:]:
                    print(f"  {line}")
        return 0

    if not plist.exists():
        raise SystemExit("No LaunchAgent installed. Run tools/install_agent.sh first.")

    if args.action == "stop":
        _launchctl("bootout", f"{domain}/{AGENT_LABEL}")
        print("Agent stopped — the panel is free for hands-on commands.")
    elif args.action == "start":
        result = _launchctl("bootstrap", domain, str(plist))
        if result.returncode != 0 and "already" not in result.stderr.lower():
            _launchctl("load", "-w", str(plist))
        print("Agent started.")
    elif args.action == "restart":
        # kickstart -k also re-reads config.yaml, which is usually the point.
        _launchctl("kickstart", "-k", f"{domain}/{AGENT_LABEL}")
        print("Agent restarted with the current config.yaml.")
    elif args.action == "logs":
        subprocess.run(["tail", "-f", str(AGENT_LOG)], check=False)
    return 0


async def cmd_show(args, config: Config) -> int:
    """Drive the running daemon's carousel from the terminal.

    Same control socket the menu bar uses, so the two can never disagree
    about what is on the panel.
    """
    from . import control

    try:
        if args.action == "status":
            state = control.request("status")
        elif args.action in ("next", "prev", "pause", "resume", "toggle", "unpin"):
            state = control.request(args.action)
        elif args.action in ("pin", "enable", "disable"):
            if not args.page:
                raise SystemExit(f"{args.action} needs a page name")
            state = control.request(args.action, page=args.page)
        else:
            raise SystemExit(f"unknown action {args.action}")
    except control.ControlError as exc:
        raise SystemExit(f"{exc}\nIs the daemon running?  ledclock agent status")

    print(f"showing   {state['showing'] or '-'}"
          f"{'  (paused)' if state['paused'] else ''}"
          f"{'  (pinned)' if state['pinned'] else ''}")
    print(f"link      {'up' if state['linked'] else 'down'}")
    print(f"rotation  {', '.join(state['rotation'])}")
    off = state.get("disabled")
    if off:
        print(f"off       {', '.join(off)}")
    return 0


async def cmd_run(args, config: Config) -> int:
    if args.address:
        config = replace(config, device=replace(config.device, address=args.address))
    try:
        await daemon.run(config)
    except asyncio.CancelledError:
        pass
    return 0


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledclock",
        description="Drive a B.K.Light / iPixel Color 32x32 LED panel from macOS.",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="find panels advertising over BLE")
    p.add_argument("--timeout", type=float, default=8.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("probe", help="connect and report type and size")
    p.add_argument("--address")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("cities", help="dump the cities iStat Menus knows about")
    p.set_defaults(func=cmd_cities)

    p = sub.add_parser("preview", help="render pages to PNG without any hardware")
    p.add_argument("--page", help="page name, or 'all' (default)")
    p.add_argument("--scale", type=int, default=12)
    p.add_argument("--out-dir", default="preview")
    p.add_argument("--raw", action="store_true", help="also write the 32x32 original")
    p.add_argument("--open", action="store_true", help="open the results")
    p.add_argument("--no-weather", action="store_true")
    p.add_argument("--gain", type=float,
                   help="pixel intensity 0-1; defaults to display.gain")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("push", help="send an image file to the panel")
    p.add_argument("image")
    p.add_argument("--address")
    p.add_argument("--slot", type=int, default=0, help="0 = show live, 1-255 = store")
    p.add_argument("--brightness", type=int)
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("page", help="render one page and send it once")
    p.add_argument("page")
    p.add_argument("--address")
    p.add_argument("--brightness", type=int)
    p.add_argument("--no-weather", action="store_true")
    p.add_argument("--gain", type=float,
                   help="pixel intensity 0-1; the way to go dimmer than "
                        "brightness 1. Defaults to display.gain")
    p.set_defaults(func=cmd_page)

    p = sub.add_parser("test-pattern", help="send colour quadrants to check orientation")
    p.add_argument("--address")
    p.add_argument("--brightness", type=int)
    p.set_defaults(func=cmd_test_pattern)

    p = sub.add_parser("clock", help="switch to the panel's own firmware clock")
    p.add_argument("--style", type=int, default=1, help="firmware clock style, 1-8")
    p.add_argument("--date", action="store_true", help="show the date too")
    p.add_argument("--twelve-hour", action="store_true")
    p.add_argument("--address")
    p.add_argument("--brightness", type=int)
    p.set_defaults(func=cmd_clock)

    p = sub.add_parser("show-slot", help="display a stored slot (1-255)")
    p.add_argument("slot", type=int)
    p.add_argument("--address")
    p.set_defaults(func=cmd_show_slot)

    p = sub.add_parser("agent", help="control the background LaunchAgent")
    p.add_argument(
        "action", nargs="?", default="status",
        choices=["status", "start", "stop", "restart", "logs"],
        help="stop it before any hands-on command; only one thing can hold the panel",
    )
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("show", help="steer the running carousel")
    p.add_argument("action", choices=["status", "next", "prev", "pause", "resume",
                                      "toggle", "pin", "unpin", "enable", "disable"])
    p.add_argument("page", nargs="?", help="page name, for pin/enable/disable")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("run", help="run the carousel daemon")
    p.add_argument("--address")
    p.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.WARNING - min(args.verbose, 2) * 10
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    config = load(args.config)
    try:
        return asyncio.run(args.func(args, config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
