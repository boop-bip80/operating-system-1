#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase1_require python3 gzip cmp
phase1_prepare_output
exec > >(tee "$OUTPUT_DIR/logs/package-initramfs.log") 2>&1
stage="$OUTPUT_DIR/rootfs"
[[ ! -L "$stage" ]] || phase1_die 'rootfs staging directory must not be a symlink'
phase1_assert_regular "$stage/bin/busybox"
phase1_assert_regular "$OUTPUT_DIR/kernelrelease"
phase1_assert_regular "$OUTPUT_DIR/source-revision"
[[ -x "$KERNEL_BUILD_DIR/usr/gen_init_cpio" ]] || phase1_die 'build kernel/gen_init_cpio first'

install -d -m 755 "$stage/etc" "$stage/proc" "$stage/sys" "$stage/dev" \
    "$stage/tmp" "$stage/mnt/data" "$stage/root" "$stage/var/run"
install -m 755 "$PHASE_DIR/rootfs/init" "$stage/init"
install -m 755 "$PHASE_DIR/rootfs/etc/udhcpc.script" "$stage/etc/udhcpc.script"
install -m 644 "$OUTPUT_DIR/kernelrelease" "$stage/etc/phase1-release"
install -m 644 "$OUTPUT_DIR/source-revision" "$stage/etc/phase1-source"
python3 "$PHASE_DIR/scripts/initramfs.py" manifest "$stage" "$OUTPUT_DIR/initramfs.list"
"$KERNEL_BUILD_DIR/usr/gen_init_cpio" -t "$SOURCE_DATE_EPOCH" "$OUTPUT_DIR/initramfs.list" \
    | gzip -n -9 > "$OUTPUT_DIR/initramfs.cpio.gz"
# Repeat the actual packager, not just its inputs, to prove deterministic packaging.
"$KERNEL_BUILD_DIR/usr/gen_init_cpio" -t "$SOURCE_DATE_EPOCH" "$OUTPUT_DIR/initramfs.list" \
    | gzip -n -9 > "$OUTPUT_DIR/initramfs-repeat.cpio.gz"
cmp "$OUTPUT_DIR/initramfs.cpio.gz" "$OUTPUT_DIR/initramfs-repeat.cpio.gz"
rm -f -- "$OUTPUT_DIR/initramfs-repeat.cpio.gz"
python3 "$PHASE_DIR/scripts/initramfs.py" verify "$OUTPUT_DIR/initramfs.cpio.gz" \
    "$OUTPUT_DIR/kernelrelease" "$OUTPUT_DIR/source-revision"
# A valid archive with NO executable init candidates; /bin/sh must also be absent.
printf '%s\n' 'dir /dev 0755 0 0' 'nod /dev/console 0600 0 0 c 5 1' \
    'nod /dev/null 0666 0 0 c 1 3' > "$OUTPUT_DIR/broken-initramfs.list"
"$KERNEL_BUILD_DIR/usr/gen_init_cpio" -t "$SOURCE_DATE_EPOCH" "$OUTPUT_DIR/broken-initramfs.list" \
    | gzip -n -9 > "$OUTPUT_DIR/broken-initramfs.cpio.gz"
phase1_log 'AC-04: deterministic initramfs and no-init negative-control image built'
