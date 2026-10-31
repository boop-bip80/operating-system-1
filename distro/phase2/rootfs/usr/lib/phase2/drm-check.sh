#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# The DRM device belongs to virtio's PCI parent; its driver is virtio-pci.
# Find virtio_gpu only among that same parent's actual virtio device children.
# shellcheck disable=SC2317,SC2329 # Sourced and called by boot-check.sh.
phase2_find_virtio_drm() {
    local class_root=${1:-/sys/class/drm} node_root=${2:-/dev/dri}
    local card name device child child_name driver
    for card in "$class_root"/card[0-9]*; do
        name=${card##*/}
        [[ "$name" =~ ^card[0-9]+$ ]] || continue
        [[ -c "$node_root/$name" && -e "$card/device" ]] || continue
        device=$(readlink -e -- "$card/device") || continue
        for child in "$device"/virtio[0-9]*; do
            child_name=${child##*/}
            [[ "$child_name" =~ ^virtio[0-9]+$ ]] || continue
            [[ -d "$child" && ! -L "$child" && -L "$child/driver" ]] || continue
            driver=$(readlink -e -- "$child/driver") || continue
            printf 'phase2-drm: card=%s device=%s child=%s driver=%s\n' \
                "$name" "$device" "$child_name" "${driver##*/}"
            [[ "${driver##*/}" == virtio_gpu ]] || continue
            printf 'phase2-drm: selected character device %s/%s\n' "$node_root" "$name"
            return 0
        done
    done
    printf 'phase2-drm: no primary DRM character device associated with virtio_gpu\n' >&2
    return 1
}
