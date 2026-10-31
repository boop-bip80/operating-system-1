#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
phase_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
for script in "$phase_dir"/scripts/*.sh "$phase_dir"/rootfs/usr/lib/phase2/*.sh; do
    bash -n "$script"
done
if ! command -v shellcheck >/dev/null 2>&1; then
    printf 'shellcheck is required for make check; install documented dependencies\n' >&2
    exit 1
fi
shellcheck --external-sources -P "$phase_dir/scripts" \
    "$phase_dir"/scripts/*.sh "$phase_dir"/rootfs/usr/lib/phase2/*.sh
python3 - "$phase_dir" <<'PY'
import ast
from pathlib import Path
import sys
for path in Path(sys.argv[1]).rglob("*.py"):
    ast.parse(path.read_text(), filename=str(path))
PY
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$phase_dir/tests" -p 'test_*.py' -v
git -C "$phase_dir/../.." diff --check
