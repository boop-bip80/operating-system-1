#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase1_require qemu-system-x86_64
phase1_prepare_output
mode=interactive
round=1
nonce=
disk="$OUTPUT_DIR/run-data.ext4"
while [[ $# -gt 0 ]]; do
    [[ $# -ge 2 ]] || phase1_die "option requires a value: $1"
    case "$1" in
        --mode) mode=$2 ;;
        --round) round=$2 ;;
        --nonce) nonce=$2 ;;
        --disk) disk=$2 ;;
        *) phase1_die "unknown boot option: $1" ;;
    esac
    shift 2
done
case "$mode" in interactive|test|negative) ;; *) phase1_die 'invalid boot mode';; esac
case "$disk" in "$OUTPUT_DIR/test-data.ext4"|"$OUTPUT_DIR/run-data.ext4") ;; *) phase1_die 'disk must be a dedicated Phase 1 image';; esac
phase1_assert_regular "$disk"
[[ "$(stat -c %h "$disk")" == 1 ]] || phase1_die 'disk must not be hard linked'
phase1_assert_regular "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage"
initrd="$OUTPUT_DIR/initramfs.cpio.gz"
command_line='console=ttyS0 loglevel=6 panic=-1 oops=panic'
case "$mode" in
    test)
        [[ "$round" == 1 || "$round" == 2 ]] || phase1_die 'test round must be 1 or 2'
        [[ "$nonce" =~ ^[0-9a-f]{1,128}$ ]] || phase1_die 'invalid test nonce'
        command_line+=" phase1.test=1 phase1.round=$round phase1.nonce=$nonce"
        ;;
    negative) initrd="$OUTPUT_DIR/broken-initramfs.cpio.gz" ;;
esac
phase1_assert_regular "$initrd"
exec qemu-system-x86_64 \
    -machine "$QEMU_MACHINE,accel=tcg" -cpu qemu64 \
    -m "$QEMU_MEMORY_MIB" -smp "$QEMU_CPU_COUNT" \
    -kernel "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage" -initrd "$initrd" \
    -append "$command_line" -display none -monitor none -serial stdio -no-reboot \
    -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
    -drive "file=$disk,format=raw,if=none,id=data" -device virtio-blk-pci,drive=data
