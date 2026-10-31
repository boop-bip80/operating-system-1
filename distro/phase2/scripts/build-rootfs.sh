#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase2_require mmdebstrap curl sha256sum dpkg-deb dpkg-query chroot python3 mktemp
[[ "$EUID" == 0 ]] || phase2_die 'rootfs provisioning needs explicit root privileges in an isolated build VM; see docs/os/PHASE_02.md'
phase2_prepare_output
phase2_assert_source
phase2_assert_regular "$OUTPUT_DIR/kernelrelease"
KERNEL_RELEASE=$(<"$OUTPUT_DIR/kernelrelease")
[[ "$KERNEL_RELEASE" =~ ^[A-Za-z0-9._+-]+-phase2$ ]] || phase2_die 'invalid kernel release'
export KERNEL_RELEASE
# Never recursively remove a root-owned tree or reuse a partly configured chroot.
[[ ! -e "$ROOTFS_DIR" && ! -L "$ROOTFS_DIR" ]] || \
    phase2_die 'rootfs staging already exists; move .build/phase2/rootfs aside explicitly or use a fresh checkout'
for file in logs/build-rootfs.log rootfs-packages.tsv rootfs-sources.list apt-release-metadata.sha256 rootfs-complete; do
    phase2_output_file "$OUTPUT_DIR/$file"
done
exec > >(tee "$OUTPUT_DIR/logs/build-rootfs.log") 2>&1

keyring_deb="$DOWNLOAD_DIR/debian-archive-keyring_${DEBIAN_KEYRING_VERSION}_all.deb"
phase2_output_file "$keyring_deb"
if [[ ! -f "$keyring_deb" ]]; then
    curl --fail --location --proto '=https' --proto-redir '=https' \
        --retry 3 --connect-timeout 20 --max-time 300 \
        "$DEBIAN_KEYRING_URL" --output "$keyring_deb"
fi
printf '%s  %s\n' "$DEBIAN_KEYRING_SHA256" "$keyring_deb" | sha256sum --check --strict -
# The key package is not installed on the host. A fresh private extraction
# directory prevents a previous/partial build from adding keys to this one.
keyring_dir=$(mktemp -d -- "$DOWNLOAD_DIR/debian-keyring-${DEBIAN_KEYRING_VERSION}.XXXXXX")
chmod 755 "$keyring_dir"
dpkg-deb --extract "$keyring_deb" "$keyring_dir"
# Debian 2025.1 ships the real bytes as .pgp and a compatibility .gpg symlink.
# Validate the real file, then give older host APT a regular .gpg-named copy.
archive_keyring_source="$keyring_dir/usr/share/keyrings/debian-archive-keyring.pgp"
phase2_assert_regular "$archive_keyring_source"
archive_keyring="$keyring_dir/bootstrap-keyring.gpg"
phase2_output_file "$archive_keyring"
install -m 644 "$archive_keyring_source" "$archive_keyring"
phase2_assert_regular "$archive_keyring"

# Check-Valid-Until is disabled only for these immutable archived indices;
# HTTPS, Debian InRelease signatures and package hash checks remain mandatory.
printf '%s\n' \
    "deb [check-valid-until=no] $DEBIAN_MIRROR $DEBIAN_SUITE main" \
    "deb [check-valid-until=no] $DEBIAN_MIRROR ${DEBIAN_SUITE}-updates main" \
    "deb [check-valid-until=no] $DEBIAN_SECURITY_MIRROR ${DEBIAN_SUITE}-security main" \
    > "$OUTPUT_DIR/rootfs-sources.list"
packages=(
    apt bash ca-certificates debian-archive-keyring systemd-sysv udev
    dbus dbus-user-session libpam-systemd login passwd
    weston wayland-utils xkb-data fonts-dejavu-core
    python3 python3-gi python3-cairo python3-gi-cairo gir1.2-gtk-3.0
    iproute2 iputils-ping e2fsprogs util-linux kbd
)
package_list=$(IFS=,; printf '%s' "${packages[*]}")
phase2_log "provisioning signed Debian $DEBIAN_SUITE snapshot $DEBIAN_SNAPSHOT"
mmdebstrap --mode=root --variant=minbase --format=directory \
    --architectures="$DEBIAN_ARCH" --components=main \
    --keyring="$archive_keyring" --include="$package_list" \
    --aptopt='Acquire::Retries "3"' \
    --aptopt='Acquire::Languages "none"' \
    --skip=cleanup/apt/lists \
    "$DEBIAN_SUITE" "$ROOTFS_DIR" "$OUTPUT_DIR/rootfs-sources.list"

# Keep the runtime package manager on exactly the same signed snapshot. The
# archive keyring inside the guest was itself installed from that signed archive.
sed 's/\[check-valid-until=no\]/[check-valid-until=no signed-by=\/usr\/share\/keyrings\/debian-archive-keyring.gpg]/' \
    "$OUTPUT_DIR/rootfs-sources.list" > "$ROOTFS_DIR/etc/apt/sources.list"
install -m 644 "$ROOTFS_DIR/etc/apt/sources.list" "$OUTPUT_DIR/rootfs-sources.list"
bash "$PHASE_DIR/scripts/configure-rootfs.sh" "$ROOTFS_DIR"
# dpkg-query expands these field names, not the host shell.
# shellcheck disable=SC2016
chroot "$ROOTFS_DIR" dpkg-query -W \
    '-f=${binary:Package}\t${Version}\t${Architecture}\t${source:Package}\t${source:Version}\n' \
    | LC_ALL=C sort > "$OUTPUT_DIR/rootfs-packages.tsv"
[[ -s "$OUTPUT_DIR/rootfs-packages.tsv" ]] || phase2_die 'empty package inventory'
metadata_dir="$OUTPUT_DIR/apt-release-metadata"
[[ ! -e "$metadata_dir" && ! -L "$metadata_dir" ]] || phase2_die 'apt metadata output already exists'
mkdir "$metadata_dir"
shopt -s nullglob
release_files=("$ROOTFS_DIR"/var/lib/apt/lists/*_InRelease)
[[ "${#release_files[@]}" -ge 3 ]] || phase2_die 'signed release metadata missing for snapshot mirrors'
for release_file in "${release_files[@]}"; do
    phase2_assert_regular "$release_file"
    install -m 644 "$release_file" "$metadata_dir/$(basename -- "$release_file")"
done
shopt -u nullglob
(cd "$OUTPUT_DIR" && sha256sum apt-release-metadata/*) > "$OUTPUT_DIR/apt-release-metadata.sha256"
phase2_assert_source
install -m 644 "$OUTPUT_DIR/source-fingerprint" "$OUTPUT_DIR/rootfs-complete"
phase2_log 'maintained userspace and package/source inventories are ready'
