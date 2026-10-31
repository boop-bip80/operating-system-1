#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Build targets are fixed under this checkout, not caller-overridable disk paths.
set -euo pipefail
umask 022

PHASE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
REPO_ROOT=$(cd -- "$PHASE_DIR/../.." && pwd -P)
OUTPUT_DIR="$REPO_ROOT/.build/phase2"
KERNEL_BUILD_DIR="$OUTPUT_DIR/kernel"
ROOTFS_DIR="$OUTPUT_DIR/rootfs"
DOWNLOAD_DIR="$OUTPUT_DIR/downloads"
export PHASE_DIR REPO_ROOT OUTPUT_DIR KERNEL_BUILD_DIR ROOTFS_DIR DOWNLOAD_DIR

phase2_die() { printf 'phase2: ERROR: %s\n' "$*" >&2; exit 1; }
phase2_log() { printf 'phase2: %s\n' "$*"; }
phase2_require() {
    local tool
    for tool in "$@"; do
        command -v "$tool" >/dev/null 2>&1 || \
            phase2_die "required tool is missing: $tool (see docs/os/PHASE_02.md)"
    done
}

case "$REPO_ROOT" in *[[:space:]]*) phase2_die 'Kbuild requires a checkout path without whitespace';; esac
# Export the lock for the separate rootfs configuration process as well.
set -a
# shellcheck source=../inputs.lock
source "$PHASE_DIR/inputs.lock"
set +a
JOBS=${JOBS:-2}
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || phase2_die 'JOBS must be a positive integer'
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git -c safe.directory="$REPO_ROOT" -C "$REPO_ROOT" show -s --format=%ct HEAD)}
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || phase2_die 'SOURCE_DATE_EPOCH must be an integer'
export SOURCE_DATE_EPOCH LC_ALL=C TZ=UTC
export KBUILD_BUILD_USER=phase2 KBUILD_BUILD_HOST=builder KBUILD_BUILD_VERSION=1
# Suppress Kbuild's implicit '+' for an untagged checkout; source identity is
# carried separately and the deliberate kernel ABI suffix is exactly -phase2.
export LOCALVERSION=
KBUILD_BUILD_TIMESTAMP=$(date -u -d "@$SOURCE_DATE_EPOCH" '+%Y-%m-%d %H:%M:%S UTC')
export KBUILD_BUILD_TIMESTAMP

phase2_prepare_output() {
    local directory
    for directory in "$REPO_ROOT/.build" "$OUTPUT_DIR" "$OUTPUT_DIR/logs" "$DOWNLOAD_DIR"; do
        [[ ! -L "$directory" ]] || phase2_die "output directory must not be a symlink: $directory"
        [[ ! -e "$directory" || -d "$directory" ]] || phase2_die "not a directory: $directory"
    done
    mkdir -p "$OUTPUT_DIR/logs" "$DOWNLOAD_DIR"
    [[ "$(realpath "$OUTPUT_DIR")" == "$OUTPUT_DIR" ]] || phase2_die 'unsafe output path'
}

phase2_assert_regular() {
    [[ -f "$1" && ! -L "$1" ]] || phase2_die "expected a regular nonsymlink file: $1"
    [[ "$(stat -c %h -- "$1")" == 1 ]] || phase2_die "hard-linked output is unsafe: $1"
}

# Use before every overwrite/truncate of a build-owned output. Device nodes,
# symlinks (including dangling ones), FIFOs and hard links are never accepted.
phase2_output_file() {
    [[ "$1" == "$OUTPUT_DIR/"* ]] || phase2_die "not a build-owned output: $1"
    [[ "$(realpath -m -- "$(dirname -- "$1")")" == "$(dirname -- "$1")" ]] || \
        phase2_die "unsafe output parent: $1"
    if [[ -e "$1" || -L "$1" ]]; then phase2_assert_regular "$1"; fi
}

phase2_source_revision() {
    local revision
    revision=$(git -c safe.directory="$REPO_ROOT" -C "$REPO_ROOT" rev-parse HEAD)
    if [[ -n "$(git -c safe.directory="$REPO_ROOT" -C "$REPO_ROOT" status --porcelain --untracked-files=normal)" ]]; then
        revision+="-dirty"
    fi
    printf '%s\n' "$revision"
}

phase2_assert_source() {
    phase2_assert_regular "$OUTPUT_DIR/source-fingerprint"
    phase2_assert_regular "$OUTPUT_DIR/source-revision"
    [[ "$(python3 "$PHASE_DIR/scripts/manifest.py" fingerprint)" == "$(<"$OUTPUT_DIR/source-fingerprint")" ]] || \
        phase2_die 'source bytes changed after kernel build; rebuild from a consistent checkout'
    [[ "$(phase2_source_revision)" == "$(<"$OUTPUT_DIR/source-revision")" ]] || \
        phase2_die 'source revision changed after kernel build; rebuild'
}
