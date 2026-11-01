#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Curate Phase 3 evidence even when an earlier build or boot test failed.
# An uploaded bundle is not an acceptance verdict.
set -euo pipefail
umask 022

PHASE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
REPO_ROOT=$(cd -- "$PHASE_DIR/../.." && pwd -P)
OUTPUT_DIR="$REPO_ROOT/.build/phase3"
ARTIFACT_DIR="$OUTPUT_DIR/artifacts"

die() {
    printf 'phase3 artifact: ERROR: %s\n' "$*" >&2
    exit 1
}
log() {
    printf 'phase3 artifact: %s\n' "$*" >&2
}
for command in mktemp install find sort xargs sha256sum stat realpath git python3; do
    command -v "$command" >/dev/null || die "missing tool: $command"
done
[[ ! -L "$REPO_ROOT/.build" && ! -L "$OUTPUT_DIR" ]] || \
    die 'build/output directories must not be symlinks'
mkdir -p -- "$OUTPUT_DIR"
[[ -d "$OUTPUT_DIR" && ! -L "$OUTPUT_DIR" && "$(realpath -e -- "$OUTPUT_DIR")" == "$OUTPUT_DIR" ]] || \
    die 'unsafe Phase 3 output directory'
[[ ! -L "$ARTIFACT_DIR" && ( ! -e "$ARTIFACT_DIR" || -d "$ARTIFACT_DIR" ) ]] || \
    die 'artifacts destination must be a directory or absent, never a symlink'

staging=$(mktemp -d -- "$OUTPUT_DIR/artifacts.new.XXXXXX")
printf '%s\n' \
    'Curated Phase 3 original-desktop-shell build outputs and diagnostics.' \
    'An uploaded image or screenshot alone is not a passing Phase 3 result.' \
    'Only test-results.json copied after successful manifest.py verification is a canonical pass.' \
    > "$staging/COLLECTION.txt"

note() {
    log "$*"
    printf '%s\n' "$*" >> "$staging/COLLECTION.txt"
}

safe_regular() {
    [[ -f "$1" && ! -L "$1" && -r "$1" ]] || return 1
    [[ "$(stat -c %h -- "$1")" == 1 ]] || return 1
    [[ "$(realpath -e -- "$1")" == "$1" ]]
}
safe_directory() {
    [[ -d "$1" && ! -L "$1" && "$(realpath -e -- "$1")" == "$1" ]]
}

copy_if_present() {
    local source=$1 destination=$2
    if safe_regular "$source"; then
        install -d -m 755 -- "$(dirname -- "$destination")"
        install -m 644 -- "$source" "$destination"
    elif [[ -e "$source" || -L "$source" ]]; then
        note "Skipped unsafe/unreadable input: $source"
    fi
}

# Only direct regular files are copied. Device nodes, symlinks, sockets,
# nested directories and unexpected names cannot enter the upload.
copy_flat_files() {
    local source_dir=$1 destination_dir=$2 file name
    if ! safe_directory "$source_dir"; then
        if [[ -e "$source_dir" || -L "$source_dir" ]]; then
            note "Skipped unsafe input directory: $source_dir"
        fi
        return
    fi
    for file in "$source_dir"/*; do
        [[ -e "$file" || -L "$file" ]] || continue
        name=$(basename -- "$file")
        if [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
            copy_if_present "$file" "$destination_dir/$name"
        else
            note "Skipped unexpected input filename in $source_dir"
        fi
    done
}

# The compressed, standalone disk is distributable for development and
# includes the ESP/kernel/rootfs. Avoid duplicating a multi-GiB raw .img.
for name in phase3.qcow2 ovmf-code.fd ovmf-vars.fd kernelrelease \
    source-revision source-fingerprint kernel.config inputs.lock \
    rootfs-packages.tsv rootfs-sources.list rootfs-complete \
    apt-release-metadata.sha256 image-components.sha256 \
    partition-table.json partition-layout.sfdisk grub.cfg BOOTX64.EFI \
    manifest.json; do
    copy_if_present "$OUTPUT_DIR/$name" "$staging/$name"
done
copy_if_present "$OUTPUT_DIR/SHA256SUMS" "$staging/BUILD-SHA256SUMS"
copy_if_present "$REPO_ROOT/.build/phase2/kernel/arch/x86/boot/bzImage" \
    "$staging/kernel/arch/x86/boot/bzImage"

copy_flat_files "$OUTPUT_DIR/logs" "$staging/logs"
copy_flat_files "$OUTPUT_DIR/screenshots" "$staging/screenshots"
copy_flat_files "$OUTPUT_DIR/apt-release-metadata" "$staging/apt-release-metadata"

install -d -m 755 -- "$staging/diagnostics"
if safe_regular "$OUTPUT_DIR/test-results.json" && \
   safe_regular "$OUTPUT_DIR/manifest.json" && \
   python3 "$PHASE_DIR/scripts/manifest.py" verify \
      > "$staging/diagnostics/acceptance-verification.log" 2>&1; then
    copy_if_present "$OUTPUT_DIR/test-results.json" "$staging/test-results.json"
    note 'Current source and boot evidence verified; published canonical passing result.'
else
    copy_if_present "$OUTPUT_DIR/test-results.json" \
        "$staging/diagnostics/test-results.unverified.json"
    printf '%s\n' \
        '{' \
        '  "schema_version": 1,' \
        '  "status": "not_validated",' \
        '  "reason": "Phase 3 acceptance verification did not pass for the current source/build; inspect diagnostics and workflow status.",' \
        '  "cases": []' \
        '}' > "$staging/test-results.json"
    note 'No verified acceptance pass; original test report (if any) is diagnostic only.'
fi

# Preserve the upstream Linux notices and relevant redistribution metadata.
copy_if_present "$REPO_ROOT/COPYING" "$staging/licenses/Linux-COPYING"
copy_if_present "$REPO_ROOT/LICENSES/preferred/GPL-2.0" \
    "$staging/licenses/Linux-GPL-2.0"
copy_if_present "$REPO_ROOT/LICENSES/exceptions/Linux-syscall-note" \
    "$staging/licenses/Linux-syscall-note"
copy_if_present "/usr/share/doc/ovmf/copyright" "$staging/licenses/OVMF-copyright"
copy_if_present "/usr/share/doc/grub-efi-amd64-bin/copyright" \
    "$staging/licenses/GRUB-copyright"
copy_if_present "$REPO_ROOT/distro/phase2/inputs.lock" \
    "$staging/sources/phase2-inputs.lock"
copy_if_present "$PHASE_DIR/inputs.lock" \
    "$staging/sources/phase3-inputs.lock"
copy_if_present "$REPO_ROOT/distro/phase2/kernel.config.fragment" \
    "$staging/sources/kernel.config.fragment"

revision=$(git -c "safe.directory=$REPO_ROOT" -C "$REPO_ROOT" rev-parse HEAD)
printf '%s\n' \
    "Checkout at collection: $revision" \
    "Source: https://github.com/boop-bip80/operating-system-1/tree/$revision" \
    'This image combines the repository kernel, a Debian signed-snapshot userspace, and an original Sway/wlroots Wayland/GTK desktop shell.' \
    'Exact tool/package versions, source identity and build digests: manifest.json, rootfs-packages.tsv and logs/.' \
    'Pinned Debian/third-party input identifiers: sources/, rootfs-sources.list, and apt-release-metadata/ when available.' \
    'Guest Debian package copyright notices remain in /usr/share/doc.' \
    'SHA256SUMS covers this upload; BUILD-SHA256SUMS (when present) describes build outputs including the omitted raw disk.' \
    'An upload is not an independently reviewed release qualification; see diagnostics/acceptance-verification.log.' \
    'Unsigned QEMU development image only: no physical installation, hardware support, production security or macOS binary compatibility is claimed.' \
    > "$staging/SOURCE.txt"

(
    cd -- "$staging"
    # Deterministic inventory of only regular files; SHA256SUMS excludes itself.
    # shellcheck disable=SC2094
    find . -type f ! -name SHA256SUMS -print0 |
        sort -z |
        xargs -0 -r sha256sum > SHA256SUMS
)
chmod 755 "$staging"
if [[ -d "$ARTIFACT_DIR" ]]; then
    previous=$(mktemp -d -- "$OUTPUT_DIR/artifacts.previous.XXXXXX")
    mv -- "$ARTIFACT_DIR" "$previous/artifacts"
    log "Previous upload preserved at $previous/artifacts"
fi
mv -- "$staging" "$ARTIFACT_DIR"
log "Curated Phase 3 evidence and SHA256SUMS ready in $ARTIFACT_DIR"
