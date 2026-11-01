#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Runs only after Phase 2's actual source, network, package and DRM preflight.
set -Eeuo pipefail
[[ "$(id -u)" == 0 && "$(cat /proc/1/comm)" == systemd ]]
[[ -f /etc/phase3-source && -f /etc/phase3-kernelrelease ]]
source_revision=$(< /etc/phase3-source)
kernel_release=$(uname -r)
[[ "$source_revision" =~ ^[a-f0-9]{40}(-dirty)?$ ]]
[[ "$kernel_release" == "$(< /etc/phase3-kernelrelease)" ]]
printf 'P3:SOURCE:%s\n' "$source_revision"
printf 'P3:KERNEL:%s\n' "$kernel_release"
# shellcheck source=/dev/null
source /etc/os-release
[[ "$ID" == debian && "$(cat /proc/1/comm)" == systemd ]]
printf 'P3:USERSPACE:debian\n'
[[ "$(findmnt --noheadings --output FSTYPE /)" == ext4 ]]
[[ "$(findmnt --noheadings --output OPTIONS /)" == rw,* ]]
printf 'P3:ROOT_OK\n'
# The earlier Phase 2 check already validated this exact virtio DRM device.
# Repeat it explicitly so Phase 3 reports its own platform readiness.
# shellcheck source=/dev/null
source /usr/lib/phase2/drm-check.sh
phase2_find_virtio_drm /sys/class/drm /dev/dri
printf 'P3:DRM_OK\n'
install -d -m 755 /run/phase3
flag=/sys/firmware/qemu_fw_cfg/by_name/opt/phase3/test/raw
if [[ -f "$flag" && "$(tr -d '\0' < "$flag")" == 1 ]]; then
    touch /run/phase3/test-mode
fi
# A separate fw_cfg key can request a deliberately broken *GTK child only*.
# It cannot arm outside test mode; /run is volatile, not persisted in the image.
shell_fault=/sys/firmware/qemu_fw_cfg/by_name/opt/phase3/fail-shell/raw
if [[ -f "$shell_fault" && "$(tr -d '\0' < "$shell_fault")" == 1 ]]; then
    [[ -f /run/phase3/test-mode && ! -L /run/phase3/test-mode ]]
    install -m 600 /dev/null /run/phase3/fail-shell
fi
state=/home/desktop/.local/state/phase3/nonce
if [[ ! -e "$state" && ! -L "$state" ]]; then
    printf 'P3:PERSISTED:EMPTY\n'
else
    [[ -f "$state" && ! -L "$state" ]]
    nonce=$(< "$state")
    [[ "$nonce" =~ ^[0-9a-f]{32}$ ]]
    printf 'P3:PERSISTED:%s\n' "$nonce"
fi
# The original shell stores this preference as a normal unprivileged file.
# Inspect actual disk contents independently before the compositor starts.
python3 - <<'PY'
import json
import os
from pathlib import Path
import stat
path = Path("/home/desktop/.config/phase3/preferences.json")
if not path.exists() and not path.is_symlink():
    print("P3:PREFERENCE_DISK:compact_dock=absent", flush=True)
else:
    if path.parent.is_symlink() or path.is_symlink():
        raise SystemExit("phase3: unsafe preferences path")
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 1000:
        raise SystemExit("phase3: preferences file is not a user-owned regular file")
    if metadata.st_size > 4096:
        raise SystemExit("phase3: oversized preference file")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("compact_dock")) is not bool:
        raise SystemExit("phase3: invalid preference schema")
    value = str(data["compact_dock"]).lower()
    print(f"P3:PREFERENCE_DISK:compact_dock={value}:uid=1000", flush=True)
PY
# Avoid serial evidence from a prior boot being replayed by a fresh tail.
[[ -f /var/log/phase3/session.log && ! -L /var/log/phase3/session.log ]]
truncate -s 0 /var/log/phase3/session.log
