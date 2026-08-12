"""BLE transport for iPixel Color LED matrices (B.K.Light ACT1026 and friends).

Frame format, verified against two independent reverse-engineered clients
(sdolphin-JP/ipixel-ctrl and lucagoc/pypixelcolor):

    [LEN_LE16][CMD_LE16][PAYLOAD...]        LEN = 4 + len(payload)

Image upload (cmd 0x0002 PNG / 0x0003 GIF) wraps its own header:

    [option][SIZE_LE32][CRC32_LE32][0x00][slot][DATA...]

where option is 0x00 for the first 12 KiB window and 0x02 for continuations,
and SIZE/CRC always describe the *whole* file, not the window.

The device acks each window with a 5-byte notification on NOTIFY_UUID whose
byte 4 is 0/1 (window done) or 3 (transfer done).
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

log = logging.getLogger(__name__)

WRITE_UUID = "0000fa02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fa03-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "LED_BLE_"

#: ATT payload per write. 244 = 247-byte MTU minus the 3-byte ATT header.
CHUNK_SIZE = 244
#: The firmware acks every 12 KiB of image data.
WINDOW_SIZE = 12 * 1024

# CoreBluetooth imposes no deadline of its own, and after the Mac wakes it
# will happily block forever. Two separate overnight hangs were traced to
# this — once in connect(), once in disconnect() — so every call that touches
# the BLE stack now has a ceiling. A timeout here surfaces as a normal link
# failure and the supervisor reconnects; a hang does not surface at all.
CONNECT_TIMEOUT = 30.0
DISCONNECT_TIMEOUT = 10.0
WRITE_TIMEOUT = 10.0

# --- commands ---------------------------------------------------------------
CMD_PNG = 0x0002
CMD_GIF = 0x0003
CMD_ERASE = 0x0102
CMD_DIY_MODE = 0x0104
CMD_SET_PIXEL = 0x0105
CMD_CLOCK_MODE = 0x0106
CMD_POWER = 0x0107
CMD_DEVICE_INFO = 0x8001
CMD_DEFAULT_MODE = 0x8003
CMD_BRIGHTNESS = 0x8004
CMD_SCREEN = 0x8007
CMD_SHOW_SLOT = 0x8008

# device_type byte -> internal LED type -> (width, height)
_DEVICE_TYPE_TO_LED_TYPE = {
    128: 0, 129: 2, 130: 4, 131: 3, 132: 1, 133: 5, 134: 6, 135: 7,
    136: 8, 137: 9, 138: 10, 139: 11, 140: 12, 141: 13, 142: 14,
    143: 15, 144: 16, 145: 17, 146: 18, 147: 19,
}
_LED_TYPE_TO_SIZE = {
    0: (64, 64), 1: (96, 16), 2: (32, 32), 3: (64, 16), 4: (32, 16),
    5: (64, 20), 6: (128, 32), 7: (144, 16), 8: (192, 16), 9: (48, 24),
    10: (64, 32), 11: (96, 32), 12: (128, 32), 13: (96, 32), 14: (160, 32),
    15: (192, 32), 16: (256, 32), 17: (320, 32), 18: (384, 32), 19: (448, 32),
}


class IPixelError(RuntimeError):
    """Any protocol-level failure talking to the panel."""


@dataclass(frozen=True)
class DeviceInfo:
    device_type: int
    led_type: int
    width: int
    height: int
    password_flag: int
    raw: bytes

    def __str__(self) -> str:
        return (
            f"device_type={self.device_type} led_type={self.led_type} "
            f"size={self.width}x{self.height} password_flag={self.password_flag}"
        )


def frame(cmd: int, payload: bytes = b"") -> bytes:
    """Wrap a payload in the standard [LEN][CMD] envelope."""
    length = 4 + len(payload)
    if length > 0xFFFF:
        raise ValueError(f"payload too large for a 16-bit length field: {length}")
    return length.to_bytes(2, "little") + cmd.to_bytes(2, "little") + payload


def parse_device_info(response: bytes) -> DeviceInfo:
    if len(response) < 5:
        raise IPixelError(f"device info response too short: {response.hex()}")
    device_type = response[4]
    led_type = _DEVICE_TYPE_TO_LED_TYPE.get(device_type)
    if led_type is None:
        raise IPixelError(
            f"unknown device_type {device_type} (raw {response.hex()}); "
            "this panel is not in the known iPixel type table"
        )
    width, height = _LED_TYPE_TO_SIZE[led_type]
    return DeviceInfo(
        device_type=device_type,
        led_type=led_type,
        width=width,
        height=height,
        password_flag=response[10] if len(response) >= 11 else 255,
        raw=bytes(response),
    )


def _is_ack(data: bytes) -> bool:
    return len(data) == 5 and data[0] == 0x05


async def scan(timeout: float = 6.0, prefix: str = NAME_PREFIX) -> list[BLEDevice]:
    """Return every advertising panel whose name starts with `prefix`."""
    found = await BleakScanner.discover(timeout=timeout)
    return [d for d in found if d.name and d.name.startswith(prefix)]


class IPixel:
    """A connected iPixel panel.

    Usage::

        async with IPixel(address) as panel:
            await panel.set_brightness(40)
            await panel.send_png(png_bytes)
    """

    def __init__(self, address: str, *, ack_timeout: float = 8.0,
                 chunk_size: int = CHUNK_SIZE) -> None:
        self.address = address
        self.ack_timeout = ack_timeout
        self.chunk_size = chunk_size
        self.info: DeviceInfo | None = None
        self._client: BleakClient | None = None
        self._window_ack = asyncio.Event()
        self._final_ack = asyncio.Event()
        self._frames: asyncio.Queue[bytes] = asyncio.Queue()
        self._disconnected = asyncio.Event()

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> "IPixel":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self, timeout: float = 20.0) -> DeviceInfo:
        """Connect, subscribe to notifications, and identify the panel."""
        self._disconnected.clear()
        target: str | BLEDevice = self.address

        # On macOS a bare address only resolves if CoreBluetooth has seen the
        # peripheral recently, so fall back to an explicit scan.
        device = await BleakScanner.find_device_by_address(self.address, timeout=8.0)
        if device is not None:
            target = device

        client = BleakClient(
            target, timeout=timeout, disconnected_callback=self._on_disconnect
        )
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        self._client = client
        log.info("connected to %s (mtu=%s)", self.address, client.mtu_size)

        await asyncio.wait_for(
            client.start_notify(NOTIFY_UUID, self._on_notify), timeout=WRITE_TIMEOUT
        )
        self.info = await self.get_device_info()
        log.info("panel identified: %s", self.info)
        return self.info

    async def disconnect(self) -> None:
        """Tear the link down. Never blocks for long, never raises.

        Both halves get a deadline: a hung disconnect once left the daemon
        parked here after a wake, alive but never reconnecting, with nothing
        in the log to say why.
        """
        client, self._client = self._client, None
        if client is None:
            return
        with suppress(Exception):
            await asyncio.wait_for(
                client.stop_notify(NOTIFY_UUID), timeout=DISCONNECT_TIMEOUT
            )
        with suppress(Exception):
            await asyncio.wait_for(client.disconnect(), timeout=DISCONNECT_TIMEOUT)
        log.info("disconnected from %s", self.address)

    def _on_disconnect(self, _client: BleakClient) -> None:
        log.warning("panel %s dropped the connection", self.address)
        self._disconnected.set()
        # Unblock anyone waiting on an ack that will never arrive.
        self._window_ack.set()
        self._final_ack.set()

    async def wait_disconnected(self) -> None:
        await self._disconnected.wait()

    # -- notifications ------------------------------------------------------
    def _on_notify(self, _char, data: bytearray) -> None:
        payload = bytes(data)
        log.debug("notify %s", payload.hex())
        if _is_ack(payload):
            code = payload[4]
            if code in (0, 1):
                self._window_ack.set()
            elif code == 3:
                self._window_ack.set()
                self._final_ack.set()
            return
        self._frames.put_nowait(payload)

    def _drain(self) -> None:
        while not self._frames.empty():
            self._frames.get_nowait()

    # -- writing ------------------------------------------------------------
    def _require_client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise IPixelError("not connected")
        return self._client

    async def _write_raw(self, data: bytes) -> None:
        client = self._require_client()
        for start in range(0, len(data), self.chunk_size):
            await asyncio.wait_for(
                client.write_gatt_char(
                    WRITE_UUID, data[start:start + self.chunk_size], response=True
                ),
                timeout=WRITE_TIMEOUT,
            )

    async def _send(self, data: bytes, *, expect_ack: bool = False) -> None:
        """Write one complete frame, optionally waiting for the window ack."""
        self._window_ack.clear()
        self._final_ack.clear()
        await self._write_raw(data)
        if not expect_ack:
            return
        try:
            await asyncio.wait_for(self._window_ack.wait(), timeout=self.ack_timeout)
        except TimeoutError as exc:
            raise IPixelError("panel never acked the transfer") from exc
        if self._disconnected.is_set():
            raise IPixelError("panel disconnected mid-transfer")

    async def _request(self, data: bytes, timeout: float = 8.0) -> bytes:
        """Write a frame and return the first non-ack notification."""
        self._drain()
        await self._write_raw(data)
        try:
            return await asyncio.wait_for(self._frames.get(), timeout=timeout)
        except TimeoutError as exc:
            raise IPixelError("panel did not answer") from exc

    # -- commands -----------------------------------------------------------
    @staticmethod
    def _time_frame(when: datetime | None = None) -> bytes:
        now = when or datetime.now()
        # The trailing byte is the firmware's language selector; 0 is default.
        return frame(CMD_DEVICE_INFO, bytes([now.hour, now.minute, now.second, 0]))

    async def get_device_info(self) -> DeviceInfo:
        """Query type/size. Doubles as the clock sync the firmware expects."""
        return parse_device_info(await self._request(self._time_frame()))

    async def set_time(self, when: datetime | None = None) -> None:
        await self._write_raw(self._time_frame(when))

    async def set_brightness(self, percent: int) -> None:
        if not 1 <= percent <= 100:
            raise ValueError("brightness must be 1-100")
        await self._send(frame(CMD_BRIGHTNESS, bytes([percent])))

    async def set_power(self, on: bool) -> None:
        await self._send(frame(CMD_POWER, bytes([1 if on else 0])))

    async def set_diy_mode(self, on: bool) -> None:
        await self._send(frame(CMD_DIY_MODE, bytes([1 if on else 0])))

    async def set_clock_mode(self, style: int = 1, *, use_24h: bool = True,
                             show_date: bool = False,
                             when: datetime | None = None) -> None:
        """Hand the display back to the panel's own firmware clock.

        This is the only thing the panel can keep doing on its own: it has an
        RTC, so once it is in this mode it keeps correct time with nothing
        connected. Styles run 1-8 and are baked into the firmware.
        """
        if not 1 <= style <= 8:
            raise ValueError("clock style must be 1-8")
        now = when or datetime.now()
        # The RTC is set separately; sending it first keeps the two in step.
        await self._write_raw(self._time_frame(now))
        payload = bytes([
            style,
            1 if use_24h else 0,
            1 if show_date else 0,
            now.year % 100,
            now.month,
            now.day,
            now.isoweekday(),
        ])
        await self._send(frame(CMD_CLOCK_MODE, payload))

    async def set_default_mode(self) -> None:
        await self._send(frame(CMD_DEFAULT_MODE))

    async def set_pixel(self, x: int, y: int, rgb: tuple[int, int, int]) -> None:
        r, g, b = rgb
        payload = bytes([0x00, r, g, b, x, y])
        await self._send(frame(CMD_SET_PIXEL, payload))

    async def show_slot(self, number: int) -> None:
        await self._send(frame(CMD_SHOW_SLOT, bytes([0x01, 0x00, number & 0xFF])))

    async def set_screen(self, number: int) -> None:
        if not 1 <= number <= 9:
            raise ValueError("screen must be 1-9")
        await self._send(frame(CMD_SCREEN, bytes([number])))

    async def erase(self, buffers: list[int] | None = None) -> None:
        """Erase the given stored buffers, or all of them when omitted."""
        targets = buffers if buffers else list(range(1, 0xFF))
        payload = len(targets).to_bytes(2, "little") + bytes(targets)
        await self._send(frame(CMD_ERASE, payload))

    async def send_png(self, png: bytes, slot: int = 0) -> None:
        """Display a PNG. slot 0 shows it live; 1-255 also stores it."""
        await self._send_media(png, CMD_PNG, tail_marker=0x00, slot=slot)

    async def send_gif(self, gif: bytes, slot: int = 0) -> None:
        await self._send_media(gif, CMD_GIF, tail_marker=0x02, slot=slot)

    async def _send_media(self, blob: bytes, cmd: int, tail_marker: int, slot: int) -> None:
        size = len(blob).to_bytes(4, "little")
        crc = (zlib.crc32(blob) & 0xFFFFFFFF).to_bytes(4, "little")
        tail = bytes([tail_marker, slot & 0xFF])

        for index, start in enumerate(range(0, len(blob), WINDOW_SIZE)):
            window = blob[start:start + WINDOW_SIZE]
            option = 0x00 if index == 0 else 0x02
            body = (
                cmd.to_bytes(2, "little") + bytes([option]) + size + crc + tail + window
            )
            packet = (2 + len(body)).to_bytes(2, "little") + body
            await self._send(packet, expect_ack=True)
