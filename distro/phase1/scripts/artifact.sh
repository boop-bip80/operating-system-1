#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Collect only known build products, never the full workspace or environment.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase1_prepare_output
artifacts="$OUTPUT_DIR/artifacts"
[[ ! -L "$artifacts" ]] || phase1_die 'artifacts directory must not be a symlink'
mkdir -p "$artifacts" "$artifacts/licenses" "$artifacts/sources"
copy_if_present() {
    if [[ -f "$1" && ! -L "$1" ]]; then
        install -m 644 "$1" "$2"
    fi
}
for name in initramfs.cpio.gz broken-initramfs.cpio.gz kernelrelease source-revision source-fingerprint \
    kernel.config busybox.config inputs.lock manifest.json test-results.json initramfs.list; do
    copy_if_present "$OUTPUT_DIR/$name" "$artifacts/$name"
done
copy_if_present "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage" "$artifacts/bzImage"
copy_if_present "$REPO_ROOT/COPYING" "$artifacts/licenses/Linux-COPYING"
copy_if_present "$REPO_ROOT/LICENSES/preferred/GPL-2.0" "$artifacts/licenses/Linux-GPL-2.0"
copy_if_present "$REPO_ROOT/LICENSES/exceptions/Linux-syscall-note" "$artifacts/licenses/Linux-syscall-note"
copy_if_present "$BUSYBOX_SOURCE_DIR/LICENSE" "$artifacts/licenses/BusyBox-LICENSE"
copy_if_present /usr/share/doc/musl/copyright "$artifacts/licenses/musl-copyright"
copy_if_present "$DOWNLOAD_DIR/busybox-$BUSYBOX_VERSION.tar.bz2" "$artifacts/sources/busybox-$BUSYBOX_VERSION.tar.bz2"
copy_if_present "$PHASE_DIR/busybox.config" "$artifacts/sources/busybox.config.fragment"
copy_if_present "$PHASE_DIR/inputs.lock" "$artifacts/sources/inputs.lock"
if [[ -d "$OUTPUT_DIR/logs" && ! -L "$OUTPUT_DIR/logs" ]]; then
    mkdir -p "$artifacts/logs"
    while IFS= read -r -d '' file; do
        copy_if_present "$file" "$artifacts/logs/$(basename -- "$file")"
    done < <(find "$OUTPUT_DIR/logs" -maxdepth 1 -type f -print0)
fi
revision=$(git -C "$REPO_ROOT" rev-parse HEAD)
printf '%s\n' \
    "Repository source commit: $revision" \
    "Kernel and Phase 1 corresponding source: https://github.com/boop-bip80/operating-system-1/tree/$revision" \
    "Kernel source archive: https://github.com/boop-bip80/operating-system-1/archive/$revision.tar.gz" \
    "BusyBox source: $BUSYBOX_URL" \
    "BusyBox archive SHA-256: $BUSYBOX_SHA256" \
    'A successful artifact upload is not evidence that the build/tests passed.' \
    > "$artifacts/SOURCE.txt"
(
    cd "$artifacts"
    # The output checksum file is explicitly excluded from find's input set.
    # shellcheck disable=SC2094
    find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 -r sha256sum > SHA256SUMS
)
phase1_log "curated diagnostics and SHA256SUMS available in $artifacts"
