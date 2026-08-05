"""Shim that runs the ledclock CLI from inside the app bundle.

macOS attributes a Bluetooth request to the *responsible* process, which for
a command line tool is whichever terminal started it. A terminal without
NSBluetoothAlwaysUsageDescription gets the whole process SIGABRTed by TCC
before bleak sees anything, and no amount of fixing our own Info.plist helps
while somebody else is being held responsible.

So bin/ledclock launches the bundle through LaunchServices instead, which
makes the bundle its own responsible process. That costs us the inherited
stdio, so this shim hands stdout, stderr, the pid and the exit status back to
the launcher through files.

Invoked as:  ledclock-python bundle_runner.py LOG STATUS PID [cli args...]
"""

from __future__ import annotations

import os
import sys
import traceback


def main() -> None:
    try:
        log_path, status_path, pid_path, *argv = sys.argv[1:]
    except ValueError:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)

    with open(pid_path, "w") as handle:
        handle.write(str(os.getpid()))

    log = open(log_path, "w", buffering=1)
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())

    status = 1
    try:
        from ledclock.cli import main as cli_main

        status = cli_main(argv)
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except KeyboardInterrupt:
        status = 130
    except BaseException:
        traceback.print_exc()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # Written last: the launcher polls for this file to know we are done.
        with open(status_path, "w") as handle:
            handle.write(str(status))


if __name__ == "__main__":
    main()
