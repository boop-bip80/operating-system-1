#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -Eeuo pipefail
phase_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
scripts=(
    "$phase_dir/scripts/check.sh"
    "$phase_dir/scripts/install-rootfs.sh"
    "$phase_dir/scripts/package-image.sh"
    "$phase_dir/scripts/boot-qemu.sh"
    "$phase_dir/scripts/artifact.sh"
    "$phase_dir/compositor/boot-check.sh"
    "$phase_dir/compositor/start-session.sh"
)
for script in "${scripts[@]}"; do
    [[ -f "$script" ]] || { echo "phase3: missing script: $script" >&2; exit 1; }
    bash -n "$script"
done
if command -v shellcheck >/dev/null 2>&1; then
    shellcheck -s bash "${scripts[@]}"
else
    echo 'phase3: ERROR: ShellCheck must be available for qualification' >&2
    exit 1
fi
python3 - "$phase_dir" <<'PY'
import ast
from pathlib import Path
import sys
phase = Path(sys.argv[1])
for path in (phase / "compositor/p3save", phase / "shell/shell.py",
             phase / "files/files.py", phase / "files/safe_paths.py",
             phase / "scripts/manifest.py", phase / "scripts/smoke-test.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("phase3: Python syntax validated")
PY
python3 -m unittest discover -s "$phase_dir/tests" -p 'test_*.py' -v
