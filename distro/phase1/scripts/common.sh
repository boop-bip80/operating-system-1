#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Shared paths are deliberately not user-overridable destructive-operation targets.
set -euo pipefail

PHASE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
REPO_ROOT=$(cd -- "$PHASE_DIR/../.." && pwd -P)
OUTPUT_DIR="$REPO_ROOT/.build/phase1"
KERNEL_BUILD_DIR="$OUTPUT_DIR/kernel"
KERNEL_UAPI_DIR="$OUTPUT_DIR/kernel-uapi"
BUSYBOX_BUILD_DIR="$OUTPUT_DIR/busybox-build"
BUSYBOX_SOURCE_DIR="$OUTPUT_DIR/busybox-src"
DOWNLOAD_DIR="$OUTPUT_DIR/downloads"
export KERNEL_BUILD_DIR KERNEL_UAPI_DIR BUSYBOX_BUILD_DIR BUSYBOX_SOURCE_DIR

phase1_die() { printf 'phase1: ERROR: %s\n' "$*" >&2; exit 1; }
phase1_log() { printf 'phase1: %s\n' "$*"; }
phase1_require() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || phase1_die "required tool is missing: $tool (see docs/os/BUILD_PROVENANCE.md)"
    done
}

case "$REPO_ROOT" in *[[:space:]]*) phase1_die 'Kbuild/gen_init_cpio require a checkout path without whitespace';; esac
# shellcheck source=../inputs.lock
source "$PHASE_DIR/inputs.lock"
JOBS=${JOBS:-2}
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || phase1_die 'JOBS must be a positive integer'
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -C "$REPO_ROOT" show -s --format=%ct HEAD)}
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || phase1_die 'SOURCE_DATE_EPOCH must be an integer'
export SOURCE_DATE_EPOCH LC_ALL=C TZ=UTC
export KBUILD_BUILD_USER=phase1 KBUILD_BUILD_HOST=builder KBUILD_BUILD_VERSION=1
KBUILD_BUILD_TIMESTAMP=$(date -u -d "@$SOURCE_DATE_EPOCH" '+%Y-%m-%d %H:%M:%S UTC')
export KBUILD_BUILD_TIMESTAMP

phase1_prepare_output() {
    [[ ! -L "$REPO_ROOT/.build" && ! -L "$OUTPUT_DIR" ]] || phase1_die 'output roots must not be symlinks'
    [[ ! -L "$OUTPUT_DIR/logs" && ! -L "$DOWNLOAD_DIR" ]] || phase1_die 'output children must not be symlinks'
    mkdir -p "$OUTPUT_DIR" "$OUTPUT_DIR/logs" "$DOWNLOAD_DIR"
    [[ "$(realpath "$OUTPUT_DIR")" == "$OUTPUT_DIR" ]] || phase1_die 'unsafe output path'
}

phase1_source_revision() {
    local revision
    revision=$(git -C "$REPO_ROOT" rev-parse HEAD)
    if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=normal)" ]]; then
        revision+="-dirty"
    fi
    printf '%s\n' "$revision"
}

phase1_assert_regular() {
    [[ -f "$1" && ! -L "$1" ]] || phase1_die "expected a regular nonsymlink file: $1"
}
