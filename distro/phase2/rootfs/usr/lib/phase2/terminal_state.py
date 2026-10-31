# SPDX-License-Identifier: GPL-2.0-only
"""Validate a fresh terminal's PTY receipt and the process it actually started."""
import os
from pathlib import Path
import re
import stat
import sys


RECEIPT = re.compile(
    r"([A-Za-z0-9_.-]{1,64})\t([1-9][0-9]*)\t([1-9][0-9]*)\t(/dev/pts/[0-9]+)\n\Z"
)


def terminal_ready(path, session, terminal_pid, *, proc_root=Path("/proc"), uid=1000):
    """Return the Bash PID, None while starting, or raise for invalid evidence.

    proc_root and uid are explicit test seams. The guest CLI always uses the
    real /proc and the fixed desktop UID; it accepts neither as an override.
    """
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600 or not 1 <= info.st_size <= 256):
        raise ValueError("unsafe terminal readiness receipt")
    match = RECEIPT.fullmatch(path.read_text(encoding="ascii"))
    if match is None:
        raise ValueError("malformed terminal readiness receipt")
    observed_session, child, parent, terminal_tty = match.groups()
    child, parent = int(child), int(parent)
    if observed_session != session or parent != terminal_pid or child == parent:
        raise ValueError("terminal receipt does not belong to this session/process")
    process = Path(proc_root) / str(child)
    try:
        fields = {}
        for line in (process / "status").read_text(encoding="ascii").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                fields[key] = value.split()
        if fields.get("Uid") != [str(uid)] * 4 or fields.get("PPid") != [str(terminal_pid)]:
            raise ValueError("terminal shell has the wrong identity or parent")
        if any(os.readlink(process / "fd" / str(number)) != terminal_tty for number in (0, 1)):
            raise ValueError("terminal shell is not attached to its reported PTY")
        command = (process / "cmdline").read_bytes()
        if command != b"/bin/bash\0-i\0":
            # The helper publishes just before exec. Never count its receipt
            # until the PID has become the real interactive shell.
            return None
        if os.path.realpath(process / "exe") != os.path.realpath("/bin/bash"):
            raise ValueError("terminal shell executable is not Bash")
    except FileNotFoundError:
        return None
    return child


def main():
    if len(sys.argv) != 4 or not re.fullmatch(r"[1-9][0-9]*", sys.argv[2]):
        print("usage: terminal_state.py RECEIPT TERMINAL_PID SESSION_ID", file=sys.stderr)
        return 2
    try:
        child = terminal_ready(sys.argv[1], sys.argv[3], int(sys.argv[2]))
        if child is None:
            return 1
        print(child)
        return 0
    except (OSError, UnicodeError, ValueError) as error:
        print("phase2 terminal: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
