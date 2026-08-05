"""Local control channel between the daemon and anything driving it.

The panel accepts one BLE client at a time and the daemon holds it, so nothing
else can talk to the hardware. Everything that wants to change what is on
screen — the menu bar app, the CLI — asks the daemon instead.

A Unix domain socket rather than a TCP port: it is unreachable from the
network by construction, needs no port to collide, and filesystem permissions
are the whole access story. Newline-delimited JSON in both directions, so it
stays trivial to poke at with `nc`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path

log = logging.getLogger(__name__)

SOCKET_PATH = (
    Path.home() / "Library" / "Application Support" / "ledclock" / "control.sock"
)

#: Long enough to cover a daemon busy pushing a frame, short enough that the
#: menu bar never feels stuck.
CLIENT_TIMEOUT = 2.0


class ControlError(RuntimeError):
    """The daemon is not listening, or refused the request."""


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
def request(command: str, path: Path = SOCKET_PATH, **arguments) -> dict:
    """Send one command and return the daemon's reply."""
    payload = json.dumps({"command": command, **arguments}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(CLIENT_TIMEOUT)
            client.connect(str(path))
            client.sendall(payload.encode())
            chunks: list[bytes] = []
            while not chunks or not chunks[-1].endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except (OSError, socket.timeout) as exc:
        raise ControlError(f"no daemon listening on {path}: {exc}") from exc

    try:
        reply = json.loads(b"".join(chunks).decode())
    except ValueError as exc:
        raise ControlError("daemon sent something that was not JSON") from exc
    if not reply.get("ok", False):
        raise ControlError(reply.get("error", "refused"))
    return reply


def is_running(path: Path = SOCKET_PATH) -> bool:
    try:
        request("ping", path=path)
        return True
    except ControlError:
        return False


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------
class ControlServer:
    """Serves `handlers` over the socket, inside the daemon's event loop."""

    def __init__(self, handlers: dict, path: Path = SOCKET_PATH) -> None:
        self._handlers = handlers
        self._path = path
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A socket file outlives the process that made it, so a crash would
        # otherwise leave one behind and block the bind for good.
        with_suppressed_error = self._path.exists()
        if with_suppressed_error:
            try:
                self._path.unlink()
            except OSError as exc:
                log.warning("could not clear the old socket: %s", exc)
        self._server = await asyncio.start_unix_server(
            self._serve, path=str(self._path)
        )
        os.chmod(self._path, 0o600)
        log.info("control socket listening on %s", self._path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            self._path.unlink()
        except OSError:
            pass

    async def _serve(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                writer.write(json.dumps(await self._dispatch(line)).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _dispatch(self, line: bytes) -> dict:
        try:
            message = json.loads(line.decode())
            command = message.pop("command")
        except (ValueError, KeyError):
            return {"ok": False, "error": "malformed request"}

        handler = self._handlers.get(command)
        if handler is None:
            return {
                "ok": False,
                "error": f"unknown command {command!r}; "
                         f"try: {', '.join(sorted(self._handlers))}",
            }
        try:
            result = handler(**message)
            if asyncio.iscoroutine(result):
                result = await result
        except TypeError as exc:
            return {"ok": False, "error": f"bad arguments: {exc}"}
        except Exception as exc:  # a handler bug must not kill the daemon
            log.exception("control command %s failed", command)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **(result or {})}
