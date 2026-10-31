#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
phase_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for script in "$phase_dir"/scripts/*.sh; do bash -n "$script"; done
sh -n "$phase_dir/rootfs/init"
sh -n "$phase_dir/rootfs/etc/udhcpc.script"
if ! command -v shellcheck >/dev/null 2>&1; then
    printf 'shellcheck is required for make check; install documented dependencies\n' >&2
    exit 1
fi
shellcheck --external-sources -P "$phase_dir/scripts" \
    "$phase_dir"/scripts/*.sh "$phase_dir/rootfs/init" "$phase_dir/rootfs/etc/udhcpc.script"
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$phase_dir/tests" -p 'test_*.py' -v
git -C "$phase_dir/../.." diff --check
