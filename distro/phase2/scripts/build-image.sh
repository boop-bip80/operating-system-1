#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase2_require grub-mkstandalone mkfs.vfat mmd mcopy mdir mkfs.ext4 e2fsck \
    truncate sfdisk dd qemu-img python3 sha256sum
[[ "$EUID" == 0 ]] || phase2_die 'image staging requires root to preserve guest file ownership; see docs/os/PHASE_02.md'
phase2_prepare_output
phase2_assert_source
[[ -d "$ROOTFS_DIR" && ! -L "$ROOTFS_DIR" ]] || phase2_die 'missing or unsafe rootfs staging'
phase2_assert_regular "$OUTPUT_DIR/rootfs-complete"
[[ "$(<"$OUTPUT_DIR/rootfs-complete")" == "$(<"$OUTPUT_DIR/source-fingerprint")" ]] || \
    phase2_die 'rootfs does not match this kernel/source build'
phase2_assert_regular "$OUTPUT_DIR/kernelrelease"
phase2_assert_regular "$OUTPUT_DIR/kernel.config"
phase2_assert_regular "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage"
kernel_release=$(<"$OUTPUT_DIR/kernelrelease")
[[ "$kernel_release" =~ ^[A-Za-z0-9._+-]+-phase2$ ]] || phase2_die 'invalid kernel release'
for file in logs/build-image.log grub.cfg BOOTX64.EFI esp.img rootfs.img \
    phase2.img phase2.qcow2 partition-layout.sfdisk partition-table.json \
    ovmf-code.fd ovmf-vars.fd image-components.sha256; do
    phase2_output_file "$OUTPUT_DIR/$file"
done
exec > >(tee "$OUTPUT_DIR/logs/build-image.log") 2>&1

[[ "$PHASE2_IMAGE_MIB" =~ ^[0-9]+$ && "$PHASE2_ESP_MIB" =~ ^[0-9]+$ ]] || \
    phase2_die 'invalid image sizes in inputs.lock'
[[ "$PHASE2_IMAGE_MIB" -ge 2048 && "$PHASE2_IMAGE_MIB" -le 16384 && \
   "$PHASE2_ESP_MIB" -ge 64 && "$PHASE2_ESP_MIB" -le 512 ]] || phase2_die 'image sizes outside supported bounds'
for uuid in "$PHASE2_DISK_GUID" "$PHASE2_ESP_PARTUUID" "$PHASE2_ROOT_PARTUUID" "$PHASE2_ROOT_UUID"; do
    [[ "$uuid" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || \
        phase2_die 'invalid UUID in inputs.lock'
done
[[ "$PHASE2_ESP_ID" =~ ^[0-9A-F]{8}$ && "$PHASE2_ROOT_LABEL" == PHASE2ROOT ]] || phase2_die 'invalid volume label/ID'

install -d -m 755 "$ROOTFS_DIR/boot"
install -m 644 "$KERNEL_BUILD_DIR/arch/x86/boot/bzImage" "$ROOTFS_DIR/boot/vmlinuz-$kernel_release"
install -m 644 "$OUTPUT_DIR/kernel.config" "$ROOTFS_DIR/boot/config-$kernel_release"
# All boot-critical drivers are built in. PARTUUID is parsed by the kernel
# without an initramfs; filesystem UUID= would require userspace resolution.
printf '%s\n' \
    'serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1' \
    'terminal_input console serial' \
    'terminal_output console serial' \
    'set timeout=0' 'set default=0' \
    "menuentry 'Phase 2 graphical foundation' {" \
    "    search --no-floppy --fs-uuid --set=root $PHASE2_ROOT_UUID" \
    '    echo P2:BOOT:GRUB' \
    "    linux /boot/vmlinuz-$kernel_release root=PARTUUID=$PHASE2_ROOT_PARTUUID rootfstype=ext4 rw rootwait console=tty0 console=ttyS0,115200n8 panic=5" \
    '}' > "$OUTPUT_DIR/grub.cfg"
grub-mkstandalone --format=x86_64-efi \
    --modules='part_gpt fat ext2 normal linux search search_fs_uuid serial terminal echo' \
    --locales='' --fonts='' --output="$OUTPUT_DIR/BOOTX64.EFI" \
    "boot/grub/grub.cfg=$OUTPUT_DIR/grub.cfg"
phase2_assert_regular "$OUTPUT_DIR/BOOTX64.EFI"

esp_image="$OUTPUT_DIR/esp.img"
root_image="$OUTPUT_DIR/rootfs.img"
disk_image="$OUTPUT_DIR/phase2.img"
esp_start=2048
esp_sectors=$((PHASE2_ESP_MIB * 2048))
root_start=$(((PHASE2_ESP_MIB + 1) * 2048))
root_mib=$((PHASE2_IMAGE_MIB - PHASE2_ESP_MIB - 2))
root_sectors=$((root_mib * 2048))
phase2_log 'creating filesystems in regular image files (no mounts or loop devices)'
truncate -s 0 "$esp_image"
truncate -s "${PHASE2_ESP_MIB}M" "$esp_image"
mkfs.vfat --invariant -F 32 -i "$PHASE2_ESP_ID" -n PHASE2EFI "$esp_image"
mmd -i "$esp_image" ::/EFI ::/EFI/BOOT
mcopy -i "$esp_image" "$OUTPUT_DIR/BOOTX64.EFI" ::/EFI/BOOT/BOOTX64.EFI
mdir -i "$esp_image" ::/EFI/BOOT
truncate -s 0 "$root_image"
truncate -s "${root_mib}M" "$root_image"
E2FSPROGS_FAKE_TIME="$SOURCE_DATE_EPOCH" mkfs.ext4 -F -q -d "$ROOTFS_DIR" \
    -L "$PHASE2_ROOT_LABEL" -U "$PHASE2_ROOT_UUID" -m 0 \
    -E "lazy_itable_init=0,lazy_journal_init=0,hash_seed=$PHASE2_ROOT_UUID" \
    -O '^metadata_csum_seed' "$root_image"
e2fsck -fn "$root_image"
truncate -s 0 "$disk_image"
truncate -s "${PHASE2_IMAGE_MIB}M" "$disk_image"
printf '%s\n' 'label: gpt' "label-id: $PHASE2_DISK_GUID" 'unit: sectors' 'sector-size: 512' \
    "start=$esp_start, size=$esp_sectors, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, uuid=$PHASE2_ESP_PARTUUID, name=PHASE2EFI" \
    "start=$root_start, size=$root_sectors, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, uuid=$PHASE2_ROOT_PARTUUID, name=$PHASE2_ROOT_LABEL" \
    > "$OUTPUT_DIR/partition-layout.sfdisk"
sfdisk --no-reread --no-tell-kernel "$disk_image" < "$OUTPUT_DIR/partition-layout.sfdisk"
dd if="$esp_image" of="$disk_image" bs=1M seek=1 conv=notrunc,sparse status=none
dd if="$root_image" of="$disk_image" bs=1M seek=$((PHASE2_ESP_MIB + 1)) conv=notrunc,sparse status=none
sfdisk --verify "$disk_image"
sfdisk --json "$disk_image" > "$OUTPUT_DIR/partition-table.json"
qemu-img convert -f raw -O qcow2 -c "$disk_image" "$OUTPUT_DIR/phase2.qcow2"
qemu-img check -f qcow2 "$OUTPUT_DIR/phase2.qcow2"

# Firmware inputs travel with the image/evidence and are hashed by the manifest.
# Each boot copies the variable store; acceptance never mutates these originals.
for firmware in CODE VARS; do
    phase2_assert_regular "/usr/share/OVMF/OVMF_${firmware}_4M.fd"
done
install -m 644 /usr/share/OVMF/OVMF_CODE_4M.fd "$OUTPUT_DIR/ovmf-code.fd"
install -m 644 /usr/share/OVMF/OVMF_VARS_4M.fd "$OUTPUT_DIR/ovmf-vars.fd"
(cd "$OUTPUT_DIR" && sha256sum BOOTX64.EFI esp.img rootfs.img grub.cfg partition-layout.sfdisk) \
    > "$OUTPUT_DIR/image-components.sha256"
phase2_assert_source
phase2_log 'standalone UEFI raw disk and compressed qcow2 are ready'
