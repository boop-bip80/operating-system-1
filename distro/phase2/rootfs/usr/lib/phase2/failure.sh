#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -eu
[[ -f /etc/phase2-source && "$(cat /proc/1/comm)" == systemd ]] || exit 1
# boot-check reports a specific failure itself, including the expected NO_DRM
# negative case. Avoid turning that expected case into a second failure marker.
if [[ ! -f /run/phase2/failure-reported ]]; then
    printf 'P2:FAIL:DESKTOP_SERVICE\n'
fi
systemctl --no-block poweroff
