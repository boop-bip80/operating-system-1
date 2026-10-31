#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Boot the packaged disk through firmware, never a host-supplied kernel/initrd.
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
phase2_require qemu-system-x86_64 qemu-img python3 sha256sum
phase2_prepare_output

mode=run
name=
disk=
vnc=
while [[ $# -gt 0 ]]; do
    [[ $# -ge 2 ]] || phase2_die "option requires a value: $1"
    case "$1" in
        --mode) mode=$2 ;;
        --name) name=$2 ;;
        --disk) disk=$2 ;;
        --vnc) vnc=$2 ;;
        *) phase2_die "unknown boot option: $1" ;;
    esac
    shift 2
done
case "$mode" in
    run) name=${name:-run}; [[ "$name" == run ]] || phase2_die 'run name must be run' ;;
    test) [[ "$name" == round1 || "$name" == round2 ]] || phase2_die 'test name must be round1 or round2' ;;
    negative) name=${name:-negative}; [[ "$name" == negative ]] || phase2_die 'negative name must be negative' ;;
    *) phase2_die 'mode must be run, test, or negative' ;;
esac
if [[ -n "$vnc" ]]; then
    [[ "$mode" == run && "$vnc" =~ ^([0-9]|[1-9][0-9])$ ]] || phase2_die '--vnc requires run mode and a display number 0..99'
fi
# QEMU option values use commas and Unix socket addresses use colons.
case "$OUTPUT_DIR" in *','*|*':'*|*'"'*) phase2_die 'QEMU output path must not contain commas, colons, or quotes' ;; esac

base="$OUTPUT_DIR/phase2.img"
code="$OUTPUT_DIR/ovmf-code.fd"
pristine_vars="$OUTPUT_DIR/ovmf-vars.fd"
for input in "$base" "$code" "$pristine_vars"; do phase2_assert_regular "$input"; done
expected_disk="$OUTPUT_DIR/$mode.qcow2"
disk=${disk:-$expected_disk}
[[ "$disk" == "$expected_disk" ]] || phase2_die 'disk must be the dedicated overlay for this mode'

if [[ "$mode" == run ]]; then
    binding="$OUTPUT_DIR/run.base-sha256"
    base_digest=$(sha256sum "$base"); base_digest=${base_digest%% *}
    if [[ -e "$disk" || -L "$disk" ]]; then
        phase2_assert_regular "$disk"
        phase2_assert_regular "$binding"
        [[ "$(<"$binding")" == "$base_digest" ]] || phase2_die 'run overlay belongs to a different base image; move run.qcow2 and run.base-sha256 aside before running a rebuilt image'
    else
        [[ ! -e "$binding" && ! -L "$binding" ]] || phase2_die 'orphan run.base-sha256; move it aside before making a new overlay'
        qemu-img create -q -f qcow2 -F raw -b "$base" "$disk"
        printf '%s\n' "$base_digest" > "$binding"
    fi
fi
phase2_assert_regular "$disk"
# Never permit a copied/malicious overlay to redirect writes to an unrelated image.
qemu-img info --output=json "$disk" | python3 -c '
import json, os, sys
info = json.load(sys.stdin)
expected = sys.argv[1]
backing = info.get("full-backing-filename", info.get("backing-filename", ""))
if (info.get("format") != "qcow2" or os.path.abspath(backing) != expected
        or info.get("backing-filename-format") != "raw"):
    raise SystemExit("phase2: unsafe overlay format or backing image")
' "$base"

vars="$OUTPUT_DIR/vars-$name.fd"
if [[ -e "$vars" || -L "$vars" ]]; then phase2_assert_regular "$vars"; fi
cp --reflink=auto -- "$pristine_vars" "$vars"
qmp_socket="$OUTPUT_DIR/qmp-$name.sock"
[[ ${#qmp_socket} -lt 104 ]] || phase2_die 'checkout path is too long for the QMP Unix socket (use a shorter checkout path)'
if [[ -e "$qmp_socket" || -L "$qmp_socket" ]]; then
    [[ ! -L "$qmp_socket" && -S "$qmp_socket" ]] || phase2_die 'refusing to replace a non-socket QMP path'
    rm -- "$qmp_socket"
fi

args=(
    -machine "$QEMU_MACHINE,accel=tcg" -cpu qemu64
    -m "$QEMU_MEMORY_MIB" -smp "$QEMU_CPU_COUNT"
    -drive "if=pflash,format=raw,readonly=on,file=$code"
    -drive "if=pflash,format=raw,file=$vars"
    -drive "if=none,id=root,format=qcow2,file=$disk"
    -device 'virtio-blk-pci,drive=root'
    -vga none -device virtio-keyboard-pci -device virtio-tablet-pci
    -netdev 'user,id=net0' -device 'virtio-net-pci,netdev=net0'
    -display none -monitor none -serial stdio -no-reboot
    -qmp "unix:$qmp_socket,server=on,wait=off"
)
if [[ "$mode" != negative ]]; then
    args+=(-device 'virtio-gpu-pci,xres=1024,yres=768')
fi
if [[ "$mode" != run ]]; then
    # The challenge is deliberately absent: it reaches the guest only as input.
    args+=(-fw_cfg 'name=opt/phase2/test,string=1')
fi
if [[ -n "$vnc" ]]; then args+=(-vnc "127.0.0.1:$vnc"); fi
exec qemu-system-x86_64 "${args[@]}"
