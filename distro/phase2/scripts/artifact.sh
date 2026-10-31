#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Collect known outputs only, including useful diagnostics when the build failed.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase2_require mktemp install find sort xargs sha256sum python3
phase2_prepare_output
artifacts="$OUTPUT_DIR/artifacts"
[[ ! -L "$artifacts" && ( ! -e "$artifacts" || -d "$artifacts" ) ]] || \
    phase2_die 'artifacts must be a nonsymlink directory or absent'
staging=$(mktemp -d -- "$OUTPUT_DIR/artifacts.new.XXXXXX")
printf '%s\n' \
    'Curated Phase 2 build outputs and diagnostics.' \
    'An artifact upload is not evidence of a successful build or acceptance run.' \
    'Only a verifier-confirmed result is published as the canonical pass verdict.' \
    > "$staging/COLLECTION.txt"

note() {
    phase2_log "$*"
    printf '%s\n' "$*" >> "$staging/COLLECTION.txt"
}

safe_regular() {
    [[ -f "$1" && ! -L "$1" && -r "$1" ]] || return 1
    [[ "$(stat -c %h -- "$1")" == 1 ]] || return 1
    # Also reject symlinks in ancestors; no copied data can come from a redirected
    # logs, kernel, firmware, or documentation directory.
    [[ "$(realpath -e -- "$1")" == "$1" ]]
}

copy_if_present() {
    local source=$1 destination=$2
    if safe_regular "$source"; then
        install -d -m 755 -- "$(dirname -- "$destination")"
        install -m 644 -- "$source" "$destination"
    elif [[ -e "$source" || -L "$source" ]]; then
        note "Skipped unsafe or unreadable input: $source"
    fi
}

copy_direct_files() {
    local source_dir=$1 destination_dir=$2 file name
    if [[ ! -d "$source_dir" || -L "$source_dir" || \
          "$(realpath -e -- "$source_dir")" != "$source_dir" ]]; then
        if [[ -e "$source_dir" || -L "$source_dir" ]]; then
            note "Skipped unsafe input directory: $source_dir"
        fi
        return
    fi
    # No recursion: devices, sockets, links, and unexpected nested trees are not
    # artifacts. The allowed names cover the build and smoke-test filenames.
    for file in "$source_dir"/*; do
        [[ -e "$file" || -L "$file" ]] || continue
        name=$(basename -- "$file")
        if [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
            copy_if_present "$file" "$destination_dir/$name"
        else
            note "Skipped unexpected artifact filename in $source_dir"
        fi
    done
}

for name in phase2.qcow2 ovmf-code.fd ovmf-vars.fd kernelrelease source-revision \
    source-fingerprint kernel.config inputs.lock rootfs-packages.tsv rootfs-sources.list \
    rootfs-complete apt-release-metadata.sha256 image-components.sha256 \
    partition-table.json partition-layout.sfdisk grub.cfg BOOTX64.EFI manifest.json; do
    copy_if_present "$OUTPUT_DIR/$name" "$staging/$name"
done
# The raw image and component files are intentionally not duplicated in the
# upload. The standalone compressed qcow2 contains the kernel and both partitions.
copy_if_present "$OUTPUT_DIR/SHA256SUMS" "$staging/BUILD-SHA256SUMS"
copy_if_present "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage" "$staging/kernel/arch/x86/boot/bzImage"
copy_direct_files "$OUTPUT_DIR/logs" "$staging/logs"
copy_direct_files "$OUTPUT_DIR/screenshots" "$staging/screenshots"
copy_direct_files "$OUTPUT_DIR/apt-release-metadata" "$staging/apt-release-metadata"

install -d -m 755 "$staging/diagnostics"
if safe_regular "$OUTPUT_DIR/test-results.json" && safe_regular "$OUTPUT_DIR/manifest.json" && \
    python3 "$PHASE_DIR/scripts/manifest.py" verify \
        > "$staging/diagnostics/acceptance-verification.log" 2>&1; then
    copy_if_present "$OUTPUT_DIR/test-results.json" "$staging/test-results.json"
    note 'Acceptance evidence verified against the current source and build outputs.'
else
    copy_if_present "$OUTPUT_DIR/test-results.json" "$staging/diagnostics/test-results.unverified.json"
    printf '%s\n' \
        '{' \
        '  "schema_version": 1,' \
        '  "status": "not_validated",' \
        '  "reason": "No verifier-confirmed acceptance result for the current source and outputs. See diagnostics and workflow status; any original report is diagnostic only.",' \
        '  "cases": []' \
        '}' > "$staging/test-results.json"
    note 'No verified acceptance pass; any original test report was retained only as diagnostics.'
fi

copy_if_present "$REPO_ROOT/COPYING" "$staging/licenses/Linux-COPYING"
copy_if_present "$REPO_ROOT/LICENSES/preferred/GPL-2.0" "$staging/licenses/Linux-GPL-2.0"
copy_if_present "$REPO_ROOT/LICENSES/exceptions/Linux-syscall-note" "$staging/licenses/Linux-syscall-note"
copy_if_present /usr/share/doc/ovmf/copyright "$staging/licenses/OVMF-copyright"
copy_if_present /usr/share/doc/grub-efi-amd64-bin/copyright "$staging/licenses/GRUB-copyright"
copy_if_present "$PHASE_DIR/inputs.lock" "$staging/sources/inputs.lock"
copy_if_present "$PHASE_DIR/kernel.config.fragment" "$staging/sources/kernel.config.fragment"
revision=$(git -c safe.directory="$REPO_ROOT" -C "$REPO_ROOT" rev-parse HEAD)
printf '%s\n' \
    "Checkout at collection: $revision" \
    'Built source identity: see source-revision and manifest.json (when available).' \
    "Kernel and Phase 2 source: https://github.com/boop-bip80/operating-system-1/tree/$revision" \
    "Kernel source archive: https://github.com/boop-bip80/operating-system-1/archive/$revision.tar.gz" \
    "Debian snapshot: $DEBIAN_MIRROR" \
    "Debian security snapshot: $DEBIAN_SECURITY_MIRROR" \
    'Exact Debian binary/source package versions: rootfs-packages.tsv.' \
    'For Debian source retrieval, use deb-src entries for the same pinned snapshot/suites in rootfs-sources.list.' \
    'The guest retains Debian package copyright notices in /usr/share/doc.' \
    'SHA256SUMS covers this curated upload; BUILD-SHA256SUMS describes the original build outputs, including the omitted raw disk.' \
    'This is an unsigned QEMU development image, not a production installer or a macOS-compatible system.' \
    > "$staging/SOURCE.txt"
(
    cd "$staging"
    # The checksum file must not include itself in its input enumeration.
    # shellcheck disable=SC2094
    find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 -r sha256sum > SHA256SUMS
)
chmod 755 "$staging"
if [[ -d "$artifacts" ]]; then
    previous=$(mktemp -d -- "$OUTPUT_DIR/artifacts.previous.XXXXXX")
    mv -- "$artifacts" "$previous/artifacts"
    phase2_log "previous artifacts preserved at $previous/artifacts"
fi
mv -- "$staging" "$artifacts"
phase2_log "curated outputs and SHA256SUMS are ready in $artifacts"
