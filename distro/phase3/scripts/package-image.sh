#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Copies a P3-customized rootfs disk built through the reviewed P2 image
# assembler. It is NOT a Phase 2 acceptance image or P2 test artifact.
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'phase3: image packaging requires explicit root' >&2; exit 1; }
phase_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
repo_root=$(cd -- "$phase_dir/../.." && pwd -P)
p2_out="$repo_root/.build/phase2"
p3_out="$repo_root/.build/phase3"
[[ -d "$p3_out/logs" && -f "$p3_out/rootfs-packages.tsv" && -f "$p3_out/inputs.lock" ]]
[[ ! -L "$p2_out" && ! -L "$p3_out" ]]
[[ "$(realpath "$p3_out")" == "$p3_out" ]]
[[ "$(python3 "$repo_root/distro/phase2/scripts/manifest.py" fingerprint)" == "$(< "$p2_out/source-fingerprint")" ]]
# Each destination must be fresh. This forbids silently mixing outputs from
# previous source revisions or failed image attempts.
copy_file() {
    local src="$1" dst="$2"
    [[ -f "$src" && ! -L "$src" ]] || { echo "phase3: absent source $src" >&2; exit 1; }
    [[ ! -e "$dst" && ! -L "$dst" ]] || { echo "phase3: stale destination $dst" >&2; exit 1; }
    cp --sparse=always --reflink=auto -- "$src" "$dst"
    chmod 644 "$dst"
}
copy_file "$p2_out/phase2.img" "$p3_out/phase3.img"
copy_file "$p2_out/phase2.qcow2" "$p3_out/phase3.qcow2"
for name in ovmf-code.fd ovmf-vars.fd kernel.config kernelrelease source-revision \
            source-fingerprint rootfs-sources.list grub.cfg partition-table.json \
            BOOTX64.EFI image-components.sha256 partition-layout.sfdisk apt-release-metadata.sha256; do
    copy_file "$p2_out/$name" "$p3_out/$name"
done
mkdir -p "$p3_out/kernel/arch/x86/boot"
copy_file "$p2_out/kernel/arch/x86/boot/bzImage" "$p3_out/kernel/arch/x86/boot/bzImage"
[[ -d "$p2_out/apt-release-metadata" && ! -L "$p2_out/apt-release-metadata" ]]
[[ ! -e "$p3_out/apt-release-metadata" ]]
cp -a -- "$p2_out/apt-release-metadata" "$p3_out/apt-release-metadata"
for name in build-kernel.log kernel-config-validation.log build-rootfs.log build-image.log; do
    if [[ -f "$p2_out/logs/$name" && ! -L "$p2_out/logs/$name" ]]; then
        copy_file "$p2_out/logs/$name" "$p3_out/logs/$name"
    fi
done
qemu-img compare -f raw -F qcow2 "$p3_out/phase3.img" "$p3_out/phase3.qcow2"
printf 'phase3: standalone P3 disk and independent provenance inputs copied\n'
