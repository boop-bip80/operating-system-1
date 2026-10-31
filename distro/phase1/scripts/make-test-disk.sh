#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase1_require truncate mkfs.ext4 stat
phase1_prepare_output
case "${1:-}" in
    test) disk="$OUTPUT_DIR/test-data.ext4" ;;
    run) disk="$OUTPUT_DIR/run-data.ext4" ;;
    *) phase1_die 'usage: make-test-disk.sh test|run' ;;
esac
[[ ! -L "$disk" ]] || phase1_die 'refusing a symlink disk target'
if [[ -e "$disk" ]]; then
    phase1_assert_regular "$disk"
    [[ "$(stat -c %h "$disk")" == 1 ]] || phase1_die 'refusing a hard-linked disk target'
    if [[ "$1" == run ]]; then
        phase1_log "preserving interactive data disk: $disk"
        exit 0
    fi
fi
# These are only validated regular images in the dedicated output directory.
truncate -s 0 "$disk"
truncate -s 128M "$disk"
mkfs.ext4 -q -F -L phase1-data -E lazy_itable_init=0,lazy_journal_init=0 "$disk"
phase1_log "created disposable 128 MiB ext4 image: $disk"
