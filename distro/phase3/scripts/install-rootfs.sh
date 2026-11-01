#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Extends a fresh, signed Phase 2 Debian rootfs before disk image assembly.
# Phase 2 generated files are intermediates only, never Phase 2 acceptance.
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]] || { echo 'phase3: userspace overlay requires explicit root' >&2; exit 1; }
phase_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
repo_root=$(cd -- "$phase_dir/../.." && pwd -P)
p2_out="$repo_root/.build/phase2"
p3_out="$repo_root/.build/phase3"
rootfs="$p2_out/rootfs"
# shellcheck source=../inputs.lock
source "$phase_dir/inputs.lock"
# shellcheck source=../../phase2/inputs.lock
source "$repo_root/distro/phase2/inputs.lock"
[[ "$PHASE3_DEBIAN_SUITE" == "$DEBIAN_SUITE" && "$PHASE3_DEBIAN_SNAPSHOT" == "$DEBIAN_SNAPSHOT" ]]
for dir in "$repo_root/.build" "$p2_out" "$p3_out" "$rootfs"; do
    [[ -d "$dir" && ! -L "$dir" && "$(realpath "$dir")" == "$dir" ]] || {
        echo "phase3: unsafe/missing build-owned directory: $dir" >&2; exit 1;
    }
done
for file in "$p2_out/rootfs-complete" "$p2_out/source-fingerprint" "$p2_out/source-revision" \
            "$p2_out/kernelrelease" "$rootfs/etc/apt/sources.list"; do
    [[ -f "$file" && ! -L "$file" ]] || { echo "phase3: missing build input $file" >&2; exit 1; }
done
[[ "$(< "$p2_out/rootfs-complete")" == "$(< "$p2_out/source-fingerprint")" ]]
[[ "$(python3 "$repo_root/distro/phase2/scripts/manifest.py" fingerprint)" == "$(< "$p2_out/source-fingerprint")" ]]
[[ ! -e "$rootfs/etc/phase3-source" && ! -L "$rootfs/etc/phase3-source" ]] || {
    echo 'phase3: rootfs overlay is not fresh; use a fresh checkout/build' >&2; exit 1;
}
grep -Fq "$DEBIAN_MIRROR" "$rootfs/etc/apt/sources.list"
grep -Fq "$DEBIAN_SECURITY_MIRROR" "$rootfs/etc/apt/sources.list"
! grep -Eq '(trusted=yes|allow-unauthenticated)' "$rootfs/etc/apt/sources.list"
for required in chroot install readlink dpkg-query sha256sum; do
    command -v "$required" >/dev/null || { echo "phase3: missing $required" >&2; exit 1; }
done
[[ -d "$p3_out/logs" && ! -L "$p3_out/logs" ]]
log="$p3_out/logs/install-rootfs.log"
[[ ! -e "$log" && ! -L "$log" ]] || { echo 'phase3: existing install log' >&2; exit 1; }
exec > >(tee "$log") 2>&1
printf 'phase3: installing from existing pinned/signed Debian %s snapshot\n' "$PHASE3_DEBIAN_SNAPSHOT"
read -r -a guest_packages <<< "$PHASE3_GUEST_PACKAGES"
(( ${#guest_packages[@]} >= 4 ))
export DEBIAN_FRONTEND=noninteractive
chroot "$rootfs" apt-get --assume-yes --no-install-recommends install "${guest_packages[@]}"
[[ -z "$(chroot "$rootfs" dpkg --audit)" ]]
for guest_package in "${guest_packages[@]}"; do
    chroot "$rootfs" dpkg-query -W "$guest_package" >/dev/null
done
install -d -m 755 "$rootfs/usr/lib/phase3" "$rootfs/usr/share/phase3" "$rootfs/etc/phase3" "$rootfs/usr/local/bin"
install -d -m 755 "$rootfs/var/log/phase3"
install -m 644 "$phase_dir/compositor/sway.conf" "$rootfs/etc/phase3/sway.conf"
install -m 755 "$phase_dir/compositor/start-session.sh" "$rootfs/usr/lib/phase3/start-session.sh"
install -m 755 "$phase_dir/compositor/boot-check.sh" "$rootfs/usr/lib/phase3/boot-check.sh"
install -m 755 "$phase_dir/compositor/p3save" "$rootfs/usr/local/bin/p3save"
install -m 644 "$phase_dir/shell/shell.py" "$rootfs/usr/lib/phase3/shell.py"
install -m 644 "$phase_dir/shell/style.css" "$rootfs/usr/share/phase3/style.css"
install -m 644 "$p2_out/source-revision" "$rootfs/etc/phase3-source"
install -m 644 "$p2_out/kernelrelease" "$rootfs/etc/phase3-kernelrelease"
units="$rootfs/etc/systemd/system"
for unit in phase3-desktop.service phase3-boot-check.service phase3-log.service; do
    install -m 644 "$phase_dir/compositor/$unit" "$units/$unit"
done
# Disable only the generated Phase 2 Weston autostart; keep its independent
# boot/source/package/network/DRM preflight as a required Phase 3 dependency.
p2_link="$units/graphical.target.wants/phase2-desktop.service"
[[ -L "$p2_link" && "$(readlink "$p2_link")" == ../phase2-desktop.service ]] || {
    echo 'phase3: unexpected Phase 2 desktop enablement' >&2; exit 1;
}
rm -- "$p2_link"
for unit in phase3-desktop.service phase3-boot-check.service phase3-log.service; do
    link="$units/graphical.target.wants/$unit"
    [[ ! -e "$link" && ! -L "$link" ]] || { echo "phase3: duplicate unit: $unit" >&2; exit 1; }
    ln -s "../$unit" "$link"
done
install -d -m 700 -o 1000 -g 1000 "$rootfs/home/desktop/.local/state/phase3"
: > "$rootfs/var/log/phase3/session.log"
chmod 644 "$rootfs/var/log/phase3/session.log"
# Preserve exact installed binary and source package versions for the P3
# manifest. P2's older package inventory must not be reused after overlay.
chroot "$rootfs" dpkg-query -W \
    '-f=${binary:Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n' \
    | LC_ALL=C sort > "$p3_out/rootfs-packages.tsv"
[[ -s "$p3_out/rootfs-packages.tsv" ]]
install -m 644 "$phase_dir/inputs.lock" "$p3_out/inputs.lock"
printf 'phase3: rootfs overlay ready; Phase 2 image builder is the next intermediate step\n'
