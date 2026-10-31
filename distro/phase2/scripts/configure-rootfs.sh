#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Called only after mmdebstrap has produced a fresh, signed userspace tree.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
[[ "$EUID" == 0 ]] || phase2_die 'rootfs configuration requires root'
[[ $# == 1 && "$1" == "$ROOTFS_DIR" ]] || phase2_die 'only the fixed rootfs staging directory is permitted'
[[ -d "$ROOTFS_DIR" && ! -L "$ROOTFS_DIR" && "$(realpath "$ROOTFS_DIR")" == "$ROOTFS_DIR" ]] || phase2_die 'unsafe rootfs staging path'
phase2_assert_source
phase2_require chroot dpkg-deb install cp ln
[[ -x "$ROOTFS_DIR/usr/lib/systemd/systemd" && -f "$ROOTFS_DIR/etc/debian_version" ]] || phase2_die 'not a Debian systemd rootfs'
cp -a --no-preserve=ownership "$PHASE_DIR/rootfs/." "$ROOTFS_DIR/"
chmod 755 "$ROOTFS_DIR"/usr/lib/phase2/*.sh "$ROOTFS_DIR"/usr/lib/phase2/input-probe.py
install -m 644 "$OUTPUT_DIR/source-revision" "$ROOTFS_DIR/etc/phase2-source"
install -m 644 "$OUTPUT_DIR/kernelrelease" "$ROOTFS_DIR/etc/phase2-kernelrelease"
printf 'phase2\n' > "$ROOTFS_DIR/etc/hostname"
truncate -s 0 "$ROOTFS_DIR/etc/machine-id"
printf '127.0.0.1 localhost\n127.0.1.1 phase2\n::1 localhost ip6-localhost\n' > "$ROOTFS_DIR/etc/hosts"
# The supported QEMU user network supplies DNS at this address. No host resolver
# or credentials are copied into the image.
[[ ! -L "$ROOTFS_DIR/etc/resolv.conf" ]] || phase2_die 'unexpected resolver symlink in fresh rootfs'
printf 'nameserver 10.0.2.3\n' > "$ROOTFS_DIR/etc/resolv.conf"
chroot "$ROOTFS_DIR" groupadd --gid 1000 desktop
chroot "$ROOTFS_DIR" useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash desktop
chroot "$ROOTFS_DIR" passwd --lock desktop
chroot "$ROOTFS_DIR" passwd --lock root
install -d -m 700 -o 1000 -g 1000 "$ROOTFS_DIR/home/desktop/.local/share/phase2" "$ROOTFS_DIR/home/desktop/.local/state/phase2"
chroot "$ROOTFS_DIR" chown -R 1000:1000 /home/desktop
install -d -m 755 "$ROOTFS_DIR/var/log/phase2" "$ROOTFS_DIR/opt/phase2/packages"
install -m 644 /dev/null "$ROOTFS_DIR/var/log/phase2/session.log"
dpkg-deb --build --root-owner-group "$ROOTFS_DIR/opt/phase2/package-source" \
    "$ROOTFS_DIR/opt/phase2/packages/phase2-proof_1.0_all.deb"
units="$ROOTFS_DIR/etc/systemd/system"
mkdir -p "$units/graphical.target.wants" "$units/multi-user.target.wants"
ln -sfn /usr/lib/systemd/system/graphical.target "$units/default.target"
ln -sfn ../phase2-desktop.service "$units/graphical.target.wants/phase2-desktop.service"
ln -sfn /usr/lib/systemd/system/systemd-networkd.service "$units/multi-user.target.wants/systemd-networkd.service"
# An ordinary non-root demo session is automatic; no privileged login or SSH is
# enabled. Do not compete with a getty for Weston's logind-managed VT.
ln -sfn /dev/null "$units/getty@tty1.service"
ln -sfn /dev/null "$units/serial-getty@ttyS0.service"
chroot "$ROOTFS_DIR" apt-get clean
phase2_assert_source
