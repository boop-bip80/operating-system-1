<!-- SPDX-License-Identifier: GPL-2.0 -->
# Phase 1 build and boot troubleshooting

Start with the first failed build/test step, its full log under
`.build/phase1/logs/`, and the source/toolchain manifest. Do not replace the
kernel, disable a test, or print a success marker to hide a failure.

## Host setup and compilation

| Symptom | What to check |
| --- | --- |
| Missing `flex`, `bison`, `bc`, OpenSSL or ELF headers | Install the documented Ubuntu prerequisites; confirm the command is on `PATH` and headers match the toolchain. |
| Cannot install packages or change system directories in a restricted workspace | Use the GitHub Actions Ubuntu runner; report local limitations and do not claim a local boot happened. |
| BusyBox checksum mismatch | Stop. Check the fixed URL/hash in `inputs.lock`, proxy/download integrity, and upstream release; never accept changed bytes merely by replacing the expected hash. |
| BusyBox is dynamically linked or an applet is missing | Check `CONFIG_STATIC`, the musl compiler, effective BusyBox config, ELF interpreter/dependencies, and required applet validation. |
| Kconfig option differs from requested fragment | Read dependency resolution in the effective `.config`. A requested `=y` line is not proof that it was enabled. |
| Out-of-tree Kbuild reports a dirty source tree | Use a clean checkout and build only under `.build/phase1`; do not remove user files or run destructive resets. |
| Build killed by memory pressure | Use the default bounded `JOBS=2`, retain diagnostics, and reproduce on the documented runner. |

The source release is determined by Kbuild's `kernelrelease`, including any
local version suffix, rather than a hardcoded display string. Compare it with
the guest's actual `uname -r` before investigating cosmetic differences.

## No boot output, panic, or failure to start PID 1

| Symptom | Likely checks |
| --- | --- |
| No serial output | QEMU serial connection, `console=ttyS0`, built-in 8250 UART/console support, and the exact `bzImage` path. |
| `No working init found` or missing `/init` | Executable archive mode, shell symlink/interpreter, static BusyBox, ELF and script binary-format support. |
| `/dev/console` unavailable | Device-node entries in the generated CPIO archive, `/dev` directory, and explicit devtmpfs mount. |
| `Attempted to kill init` | PID 1 exited. Inspect the earliest command failure; `/init` must remain alive and handle failures deliberately. |
| No `P1:INIT_OK` | The filesystem setup or identity check failed before tests started; do not accept a later echoed marker. |
| QEMU exits 0 but guest did not pass | `-no-reboot` can exit after a reboot/panic. Require ordered exact markers, correct identity, clean poweroff, and no fatal logs. |
| Guest remains running at deadline | Read the last serial lines for DHCP, disk discovery, or shutdown stalls; a timeout is a failure, not a pass. |

The default timeout is 180 seconds **per boot**. Do not blindly increase it to
hide a deterministic hang. If the host is unusually slow, retain timing data
and reproduce on CI before proposing a documented timeout change.

## Virtual storage and networking

- **No `/dev/vda`:** verify built-in PCI, virtio-pci, virtio-blk and block-device
  support, plus the QEMU drive argument. The disk must be a regular, disposable
  image file under the build output directory.
- **ext4 mount failure:** inspect guest kernel/filesystem logs and confirm the
  host-created image is ext4. Never format or mount a physical host device to
  troubleshoot this test.
- **Persistence mismatch:** both rounds must use the same disk file and unique
  nonce, with sync/unmount before poweroff. A fresh disk per round defeats the
  persistence test; an old fixed marker can create a false positive.
- **No DHCP address:** check virtio-net support, interface link state, finite
  `udhcpc` retries, and the lease callback. Receiving a lease without applying
  the IPv4 address is not enough.
- **Internet ping fails:** public Internet connectivity is not an acceptance
  condition. QEMU user-mode DHCP and guest address/route evidence are the test
  target; host ICMP/network restrictions may differ.

## Interactive shell and shutdown

The interactive scenario must send real commands and check their results.
Echoed terminal input is not proof of command execution. `setsid`/`cttyhack`
give the shell a controlling terminal; missing helpers can cause job-control
warnings or noninteractive behavior. Exiting the shell must not terminate PID 1.

This minimal guest uses a shell-based PID 1, not a general init daemon. Use
BusyBox `poweroff -f` after syncing/unmounting data. The QEMU `q35` machine needs
the kernel's ACPI power-management path for the normal poweroff proof. A host
kill of QEMU is not equivalent to a clean guest shutdown.

## Negative control and CI evidence

The deliberately broken initramfs scenario must visibly reach a guest failure
such as a kernel panic/no-init error. A missing host executable, bad host path,
or QEMU command-line error must fail the harness instead of satisfying the
negative control.

For CI failures, download the artifact belonging to the exact run ID and
attempt. Collection/upload run even if build or boot failed, but the artifact
may be partial. Read `test-results.json` alongside the complete serial logs;
do not infer pass from artifact availability. Do not use `continue-on-error`
to make the workflow green.

After a fix, run the full workflow twice on the same commit. A later source
change invalidates earlier evidence for the changed source. Record actual
workflow links in `PHASE_01.md` only after successful runs exist.
