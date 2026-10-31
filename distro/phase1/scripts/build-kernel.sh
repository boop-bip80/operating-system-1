#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase1_require make gcc ld bison flex bc perl openssl python3
phase1_prepare_output
[[ ! -L "$KERNEL_BUILD_DIR" ]] || phase1_die 'kernel build directory must not be a symlink'
mkdir -p "$KERNEL_BUILD_DIR"
exec > >(tee "$OUTPUT_DIR/logs/build-kernel.log") 2>&1

phase1_log "building repository kernel at $(phase1_source_revision)"
phase1_source_revision > "$OUTPUT_DIR/source-revision"
python3 "$PHASE_DIR/scripts/manifest.py" fingerprint > "$OUTPUT_DIR/source-fingerprint"
make -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 x86_64_defconfig
KCONFIG_CONFIG="$KERNEL_BUILD_DIR/.config" \
    bash "$REPO_ROOT/scripts/kconfig/merge_config.sh" -m -Q \
    "$KERNEL_BUILD_DIR/.config" "$PHASE_DIR/kernel.config.fragment"
make -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 olddefconfig
python3 "$PHASE_DIR/scripts/validate-config.py" \
    "$PHASE_DIR/kernel.config.fragment" "$KERNEL_BUILD_DIR/.config" \
    | tee "$OUTPUT_DIR/logs/kernel-config-validation.log"
make -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 -j"$JOBS" bzImage
[[ -s "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage" ]] || phase1_die 'AC-02: no bzImage produced'
[[ -x "$KERNEL_BUILD_DIR/usr/gen_init_cpio" ]] || \
    make -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 usr/gen_init_cpio
make -s -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 kernelrelease > "$OUTPUT_DIR/kernelrelease"
install -m 644 "$KERNEL_BUILD_DIR/.config" "$OUTPUT_DIR/kernel.config"
[[ "$(python3 "$PHASE_DIR/scripts/manifest.py" fingerprint)" == "$(<"$OUTPUT_DIR/source-fingerprint")" ]] || \
    phase1_die 'AC-01: source inputs changed during kernel build; rebuild'
phase1_log "kernel build complete: $(<"$OUTPUT_DIR/kernelrelease")"
