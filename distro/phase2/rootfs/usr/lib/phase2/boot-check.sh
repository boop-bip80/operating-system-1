#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -Eeuo pipefail
[[ "$EUID" == 0 && -f /etc/phase2-source && "$(cat /proc/1/comm)" == systemd ]] || exit 1
install -d -m 755 /run/phase2
reason=BOOT_CHECK
failed() {
    trap - ERR
    printf 'P2:FAIL:%s\n' "$reason"
    touch /run/phase2/failure-reported
    systemctl --no-block poweroff
    exit 1
}
trap failed ERR
emit() { printf 'P2:%s\n' "$1"; }
source_revision=$(cat /etc/phase2-source)
kernel_release=$(uname -r)
[[ "$source_revision" =~ ^[a-f0-9]{40}(-dirty)?$ ]]
emit "SOURCE:$source_revision"
emit "KERNEL:$kernel_release"
reason=KERNEL_IDENTITY
[[ "$kernel_release" == "$(cat /etc/phase2-kernelrelease)" ]]
reason=USERSPACE
# shellcheck source=/dev/null
source /etc/os-release
[[ "$ID" == debian ]]
[[ "$(cat /proc/1/comm)" == systemd ]]
emit USERSPACE:debian
reason=ROOT_FILESYSTEM
[[ "$(findmnt --noheadings --output FSTYPE /)" == ext4 ]]
[[ "$(findmnt --noheadings --output OPTIONS /)" == rw,* ]]
printf 'root writable\n' > /var/lib/phase2-root-check
sync -f /var/lib/phase2-root-check
emit ROOT_OK
# This flag selects the visible input probe. The host's challenge is never in
# firmware, the kernel command line, a guest file, or an environment variable.
test_flag=/sys/firmware/qemu_fw_cfg/by_name/opt/phase2/test/raw
if [[ -f "$test_flag" && "$(tr -d '\0' < "$test_flag")" == 1 ]]; then
    touch /run/phase2/test-mode
fi
reason=PACKAGE_MANAGER
export DEBIAN_FRONTEND=noninteractive
# APT 3's --no-download path mishandles a local .deb's internal filename. Keep
# this dependency-free proof offline by disabling repositories for this command.
apt-get --assume-yes --no-install-recommends \
    -o Dir::Etc::sourcelist=/dev/null -o Dir::Etc::sourceparts=- \
    install /opt/phase2/packages/phase2-proof_1.0_all.deb
[[ "$(dpkg-query -W -f='${Status}' phase2-proof)" == 'install ok installed' ]]
[[ -f /usr/share/phase2-proof/marker ]]
apt-get --assume-yes --no-download purge phase2-proof
[[ ! -e /usr/share/phase2-proof/marker ]]
[[ -z "$(dpkg --audit)" ]]
emit PACKAGE_OK
reason=NETWORK
for ((attempt=0; attempt<60; attempt++)); do
    if ip -4 route show default | grep -q '^default via 10\.0\.2\.2 ' && \
       ip -4 -o address show scope global | grep -q ' inet 10\.0\.2\.'; then
        break
    fi
    sleep 1
done
ip -4 route show default | grep -q '^default via 10\.0\.2\.2 '
ping -c 1 -W 3 10.0.2.2
emit NETWORK_OK
reason=UDEV
udevadm settle --timeout=30
reason=DRM_CHECK
# shellcheck source=../rootfs/usr/lib/phase2/drm-check.sh
source /usr/lib/phase2/drm-check.sh
reason=NO_DRM
phase2_find_virtio_drm
emit DRM_OK
reason=INPUT_DEVICES
keyboard_found=false
pointer_found=false
shopt -s nullglob
for input in /sys/class/input/event*/device/name; do
    name=$(cat "$input")
    case "$name" in
        *'Virtio Keyboard'*) keyboard_found=true ;;
        *'Virtio Tablet'*) pointer_found=true ;;
    esac
done
[[ "$keyboard_found" == true && "$pointer_found" == true ]]
emit INPUT_OK
# Both root and desktop processes have the log open at service start. Reset it
# between boots so old evidence can never satisfy a new host challenge.
truncate -s 0 /var/log/phase2/session.log
