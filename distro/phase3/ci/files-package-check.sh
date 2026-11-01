#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Files packaging identity proof ONLY. Native Files QMP qualification is separate
# and remains unvalidated until an independently verified Files UI harness exists.
set -Eeuo pipefail

phase_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
repo_root=$(cd -- "$phase_dir/../.." && pwd -P)
rootfs="$repo_root/.build/phase2/rootfs"
out="$repo_root/.build/phase3/logs/files-package-identity.json"
[[ -d "$rootfs" && ! -L "$rootfs" ]] || {
    echo 'phase3.2: missing or unsafe built guest rootfs' >&2
    exit 1
}
[[ -d "$(dirname -- "$out")" && ! -L "$(dirname -- "$out")" ]] || {
    echo 'phase3.2: unsafe diagnostics directory' >&2
    exit 1
}
[[ ! -e "$out" && ! -L "$out" ]] || {
    echo 'phase3.2: packaging identity result must not be pre-existing' >&2
    exit 1
}

python3 - "$repo_root" "$rootfs" "$out" "$phase_dir/ci/files-package-check.sh" <<'PY'
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

repo = Path(sys.argv[1])
rootfs = Path(sys.argv[2])
report = Path(sys.argv[3])
checker = Path(sys.argv[4])
mapping = (
    ("files/files.py", "usr/lib/phase3/files.py", 0o755),
    ("files/safe_paths.py", "usr/lib/phase3/safe_paths.py", 0o644),
    ("files/style.css", "usr/share/phase3/files.css", 0o644),
    ("files/phase3-files.desktop",
     "usr/share/applications/phase3-files.desktop", 0o644),
)


def require(condition: bool, description: str) -> None:
    if not condition:
        raise RuntimeError("phase3.2 Files packaging: " + description)


def regular(path: Path, *, expected_mode: int | None = None) -> bytes:
    # Never follow a forged guest/source symlink, hardlink, or parent symlink.
    for parent in (path, *path.parents):
        if parent == repo.parent:
            break
        require(not parent.is_symlink(), f"unsafe symlink: {path}")
    record = path.lstat()
    require(stat.S_ISREG(record.st_mode) and record.st_nlink == 1,
            f"not a unique regular file: {path}")
    require(0 < record.st_size <= 1048576, f"empty/oversized file: {path}")
    if expected_mode is not None:
        require(stat.S_IMODE(record.st_mode) == expected_mode,
                f"incorrect installed mode: {path}")
        require(record.st_uid == 0, f"installed file is not root-owned: {path}")
    return path.read_bytes()


sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              text=True).strip()
require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "invalid checkout SHA")
require((rootfs / "etc/phase3-source").read_text().strip() == sha,
        "guest Files package is not staged from this exact checkout")
require(checker == repo / "distro/phase3/ci/files-package-check.sh",
        "CI checker path differs from checked-out source")

records = []
for relative_source, relative_guest, installed_mode in mapping:
    source = repo / "distro/phase3" / relative_source
    installed = rootfs / relative_guest
    expected = regular(source)
    observed = regular(installed, expected_mode=installed_mode)
    require(expected == observed, f"guest Files bytes differ: {relative_guest}")
    records.append({
        "source_path": str(source.relative_to(repo)),
        "guest_path": "/" + relative_guest,
        "sha256": hashlib.sha256(expected).hexdigest(),
        "bytes": len(expected),
        "guest_mode": format(installed_mode, "04o"),
        "guest_uid": 0,
    })

desktop_source = repo / "distro/phase3/files/phase3-files.desktop"
parser = configparser.ConfigParser(interpolation=None, strict=True)
parser.optionxform = str
parser.read_string(desktop_source.read_text(encoding="utf-8"))
require(parser.has_section("Desktop Entry"), "missing freedesktop Desktop Entry")
entry = parser["Desktop Entry"]
require(entry.get("Type") == "Application" and entry.get("Name") == "Files",
        "Files launcher is not a visible application")
require(entry.get("Exec") == "/usr/bin/python3 /usr/lib/phase3/files.py",
        "Files launcher command changed from pinned direct Python target")
require(entry.get("TryExec") == "/usr/bin/python3" and
        entry.get("Icon") == "system-file-manager" and
        entry.get("Categories") == "Utility;FileManager;",
        "Files launcher identity/visibility metadata changed")
require(not entry.getboolean("Terminal", fallback=True) and
        not entry.getboolean("Hidden", fallback=False) and
        not entry.getboolean("NoDisplay", fallback=False),
        "Files launcher must remain visible and must not open a terminal")
payload = {
    "schema_version": 1,
    "phase": "3.2",
    "source_sha": sha,
    "result": "files_packaging_identity_verified",
    "native_files_qmp_status": "not_validated",
    "scope": "Only direct source-to-guest Files hashes, modes and launcher; existing Phase3.1 native QEMU is a regression, not native Files GUI acceptance.",
    "checker_sha256": hashlib.sha256(regular(checker)).hexdigest(),
    "files": records,
}
report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                  encoding="utf-8")
print("phase3.2: four source-bound Files assets and Gio launcher verified; "
      "native Files QMP acceptance NOT VALIDATED")
PY
