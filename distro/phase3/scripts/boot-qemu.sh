#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Boot only the standalone Phase 3 UEFI image. Automated scenarios use serial
# stdio, a private QMP socket and test-mode fw_cfg; no direct kernel bypass.
set -Eeuo pipefail
phase_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
repo_root=$(cd -- "$phase_dir/../.." && pwd -P)
out="$repo_root/.build/phase3"
mode=run
name=
disk=
qmp=
vnc=
usage() {
    echo 'usage: boot-qemu.sh --mode run|test|negative [--name round1|round2|negative] [--disk absolute-qcow2-path] [--qmp private-socket] [--vnc N]' >&2
    exit 2
}
die() { echo "phase3 QEMU: $*" >&2; exit 1; }
while (( $# )); do
    case "$1" in
        --mode) (( $# >= 2 )) || usage; mode="$2"; shift 2 ;;
        --name) (( $# >= 2 )) || usage; name="$2"; shift 2 ;;
        --disk) (( $# >= 2 )) || usage; disk="$2"; shift 2 ;;
        --qmp) (( $# >= 2 )) || usage; qmp="$2"; shift 2 ;;
        --vnc) (( $# >= 2 )) || usage; vnc="$2"; shift 2 ;;
        *) usage ;;
    esac
done
[[ "$mode" == run || "$mode" == test || "$mode" == negative ]] || usage
if [[ -z "$name" ]]; then
    if [[ "$mode" == negative ]]; then name=negative; else name=run; fi
fi
[[ "$name" =~ ^[a-z][a-z0-9_-]{0,30}$ ]] || usage
[[ -z "$vnc" || ( "$mode" == run && "$vnc" =~ ^[0-9]{1,2}$ ) ]] || usage
for tool in qemu-system-x86_64 qemu-img realpath mktemp cp; do
    command -v "$tool" >/dev/null || die "missing command: $tool"
done
[[ -d "$out" && ! -L "$out" && "$(realpath "$out")" == "$out" ]] || die 'unsafe or absent build output'
for file in phase3.qcow2 ovmf-code.fd ovmf-vars.fd; do
    [[ -f "$out/$file" && ! -L "$out/$file" ]] || die "missing image input: $file"
done
if [[ -z "$disk" ]]; then
    if [[ "$mode" != run ]]; then die 'test and negative modes require --disk from acceptance harness'; fi
    disk="$out/run.qcow2"
    if [[ ! -e "$disk" && ! -L "$disk" ]]; then
        qemu-img create -f qcow2 -F qcow2 -b "$out/phase3.qcow2" "$disk"
    fi
fi
disk=$(realpath -e -- "$disk")
[[ "$disk" == "$out/"* && -f "$disk" && ! -L "$disk" ]] || die 'disk must be a regular, local Phase 3 output file'
qemu-img info -f qcow2 "$disk" >/dev/null || die 'disk is not valid qcow2'
if [[ -z "$qmp" ]]; then qmp="$out/qmp-$name.sock"; fi
[[ "$qmp" == "$out/"* && ! -e "$qmp" && ! -L "$qmp" ]] || die 'QMP path must be fresh and private to Phase 3 output'
firmware=$(mktemp --suffix=.fd -- "$out/ovmf-$name-XXXXXXXX")
cp -- "$out/ovmf-vars.fd" "$firmware"
chmod 600 "$firmware"
# Each boot gets independent writable firmware state, including persistence
# round two, while preserving the same guest disk overlay.
args=(
    -machine q35,accel=tcg -cpu qemu64 -m 2048 -smp 2
    -drive "if=pflash,format=raw,readonly=on,file=$out/ovmf-code.fd"
    -drive "if=pflash,format=raw,file=$firmware"
    -drive "if=none,id=root,format=qcow2,file=$disk"
    -device virtio-blk-pci,drive=root
    -vga none
    -device virtio-keyboard-pci -device virtio-tablet-pci
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0
    -display none
    -qmp "unix:$qmp,server=on,wait=off"
    -monitor none -no-reboot
)
if [[ "$mode" != negative ]]; then
    args+=( -device virtio-gpu-pci,xres=1024,yres=768 )
fi
if [[ "$mode" == test ]]; then
    args+=( -fw_cfg name=opt/phase3/test,string=1 )
fi
if [[ -n "$vnc" ]]; then
    args+=( -vnc "127.0.0.1:$vnc" )
fi
if [[ "$mode" == run ]]; then
    args+=( -serial "file:$out/logs/run-$name-serial.log" )
else
    # The Python acceptance harness captures this stdout stream itself.
    args+=( -serial stdio )
fi
exec qemu-system-x86_64 "${args[@]}"
