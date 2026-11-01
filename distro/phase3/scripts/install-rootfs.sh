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
# shellcheck disable=SC1091 # Trusted, exact repository lock file at runtime.
source "$phase_dir/inputs.lock"
# shellcheck source=../../phase2/inputs.lock
# shellcheck disable=SC1091 # Trusted, exact repository lock file at runtime.
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
# Original Files browser is shipped as source-bound, root-owned read-only code.
# Reject symlinked/missing assets before mutating the signed rootfs overlay.
for asset in files.py safe_paths.py style.css phase3-files.desktop; do
    source_asset="$phase_dir/files/$asset"
    [[ -f "$source_asset" && ! -L "$source_asset" ]] || {
        echo "phase3: missing or symlinked Files source $asset" >&2; exit 1;
    }
done
grep -Fxq 'Exec=/usr/bin/python3 /usr/lib/phase3/files.py' "$phase_dir/files/phase3-files.desktop" || {
    echo 'phase3: Files launcher must use its fixed non-shell entrypoint' >&2; exit 1;
}
grep -Fq "$DEBIAN_MIRROR" "$rootfs/etc/apt/sources.list"
grep -Fq "$DEBIAN_SECURITY_MIRROR" "$rootfs/etc/apt/sources.list"
if grep -Eq '(trusted=yes|allow-unauthenticated)' "$rootfs/etc/apt/sources.list"; then
    echo 'phase3: unsafe unauthenticated APT sources' >&2
    exit 1
fi
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
# P2 intentionally configures 10.0.2.3 for the QEMU guest. Host chroot APT
# cannot resolve through a QEMU-only DNS gateway, so borrow the host resolver
# only while downloading from the EXISTING signed pinned snapshot sources.
# Preserve the exact original guest file bytes even when apt fails.
guest_resolver="$rootfs/etc/resolv.conf"
[[ -f "$guest_resolver" && ! -L "$guest_resolver" && -f /etc/resolv.conf ]]
resolver_backup=$(mktemp -- "$p3_out/guest-resolver.XXXXXXXX")
cp -- "$guest_resolver" "$resolver_backup"
cmp -s -- "$guest_resolver" "$resolver_backup"
restore_guest_resolver() {
    if [[ -f "$resolver_backup" && ! -L "$resolver_backup" ]]; then
        cp -- "$resolver_backup" "$guest_resolver"
        cmp -s -- "$resolver_backup" "$guest_resolver"
        rm -- "$resolver_backup"
    fi
}
trap restore_guest_resolver EXIT
cp --dereference -- /etc/resolv.conf "$guest_resolver"
chroot "$rootfs" apt-get --assume-yes --no-install-recommends install "${guest_packages[@]}"
restore_guest_resolver
[[ ! -e "$resolver_backup" && ! -L "$resolver_backup" ]]
[[ -z "$(chroot "$rootfs" dpkg --audit)" ]]
for guest_package in "${guest_packages[@]}"; do
    chroot "$rootfs" dpkg-query -W "$guest_package" >/dev/null
done
[[ ! -L "$rootfs/usr/share/applications" ]] || {
    echo 'phase3: unsafe symlink for installed Files desktop entry directory' >&2; exit 1;
}
install -d -m 755 "$rootfs/usr/lib/phase3" "$rootfs/usr/share/phase3" "$rootfs/etc/phase3" "$rootfs/usr/local/bin"
install -d -m 755 "$rootfs/usr/share/applications"
install -d -m 755 "$rootfs/var/log/phase3"
install -m 644 "$phase_dir/compositor/sway.conf" "$rootfs/etc/phase3/sway.conf"
install -m 755 "$phase_dir/compositor/start-session.sh" "$rootfs/usr/lib/phase3/start-session.sh"
install -m 755 "$phase_dir/compositor/boot-check.sh" "$rootfs/usr/lib/phase3/boot-check.sh"
install -m 755 "$phase_dir/compositor/p3save" "$rootfs/usr/local/bin/p3save"
install -m 644 "$phase_dir/shell/shell.py" "$rootfs/usr/lib/phase3/shell.py"
install -m 644 "$phase_dir/shell/style.css" "$rootfs/usr/share/phase3/style.css"
install -m 755 "$phase_dir/files/files.py" "$rootfs/usr/lib/phase3/files.py"
install -m 644 "$phase_dir/files/safe_paths.py" "$rootfs/usr/lib/phase3/safe_paths.py"
install -m 644 "$phase_dir/files/style.css" "$rootfs/usr/share/phase3/files.css"
install -m 644 "$phase_dir/files/phase3-files.desktop" "$rootfs/usr/share/applications/phase3-files.desktop"
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
# shellcheck disable=SC2016 # dpkg-query expands these placeholders inside the guest.
chroot "$rootfs" dpkg-query -W \
    '-f=${binary:Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n' \
    | LC_ALL=C sort > "$p3_out/rootfs-packages.tsv"
[[ -s "$p3_out/rootfs-packages.tsv" ]]
install -m 644 "$phase_dir/inputs.lock" "$p3_out/inputs.lock"
printf 'phase3: rootfs overlay ready; Phase 2 image builder is the next intermediate step\n'
