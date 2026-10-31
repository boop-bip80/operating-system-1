#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase2_require make gcc ld bison flex bc perl openssl python3
phase2_prepare_output
[[ ! -L "$KERNEL_BUILD_DIR" ]] || phase2_die 'kernel build directory must not be a symlink'
mkdir -p "$KERNEL_BUILD_DIR"
for file in source-revision source-fingerprint kernelrelease kernel.config \
    logs/build-kernel.log logs/kernel-config-validation.log; do
    phase2_output_file "$OUTPUT_DIR/$file"
done
exec > >(tee "$OUTPUT_DIR/logs/build-kernel.log") 2>&1

phase2_log "building repository kernel at $(phase2_source_revision)"
phase2_source_revision > "$OUTPUT_DIR/source-revision"
python3 "$PHASE_DIR/scripts/manifest.py" fingerprint > "$OUTPUT_DIR/source-fingerprint"
make -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 x86_64_defconfig
KCONFIG_CONFIG="$KERNEL_BUILD_DIR/.config" \
    bash "$REPO_ROOT/scripts/kconfig/merge_config.sh" -m -Q \
    "$KERNEL_BUILD_DIR/.config" "$PHASE_DIR/kernel.config.fragment"
make -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 olddefconfig
# Reuse the generic parser without changing any Phase 1 build inputs or outputs.
python3 "$REPO_ROOT/distro/phase1/scripts/validate-config.py" \
    "$PHASE_DIR/kernel.config.fragment" "$KERNEL_BUILD_DIR/.config" \
    | tee "$OUTPUT_DIR/logs/kernel-config-validation.log"
make -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 -j"$JOBS" bzImage
phase2_assert_regular "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage"
[[ -s "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage" ]] || phase2_die 'kernel build produced an empty bzImage'
kernel_release=$(make -s --no-print-directory -C "$REPO_ROOT" \
    O="$KERNEL_BUILD_DIR" ARCH=x86_64 kernelrelease)
[[ "$kernel_release" =~ ^[A-Za-z0-9._+-]+-phase2$ ]] || \
    phase2_die 'kernelrelease is not one valid token ending in -phase2'
printf '%s\n' "$kernel_release" > "$OUTPUT_DIR/kernelrelease"
install -m 644 "$KERNEL_BUILD_DIR/.config" "$OUTPUT_DIR/kernel.config"
phase2_assert_source
phase2_log "kernel build complete: $kernel_release"
