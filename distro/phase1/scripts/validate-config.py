#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fail when Kconfig dropped a requested value; absent disabled symbols are OK."""
import re
import sys
from pathlib import Path


def parse_config(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        match = re.fullmatch(r"(CONFIG_\w+)=(.*)", line)
        disabled = re.fullmatch(r"# (CONFIG_\w+) is not set", line)
        if match:
            result[match[1]] = match[2]
        elif disabled:
            result[disabled[1]] = "n"
    return result


def main():
    requested, actual = (parse_config(p) for p in sys.argv[1:3])
    failed = []
    for name, value in requested.items():
        got = actual.get(name, "n")
        if got != value:
            failed.append(f"{name}: required {value}, resolved {got}")
    if failed:
        print("AC-03: effective config mismatch:\n" + "\n".join(failed), file=sys.stderr)
        return 1
    print(f"AC-03: {len(requested)} configuration requirements verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
