<!-- SPDX-License-Identifier: GPL-2.0 -->
# Phase 2 — Bootable distribution and graphical baseline

**Product direction:** a complete macOS-inspired Linux desktop experience.

**Status:** implemented; qualification is evaluated separately for each source
commit using the CI runs and artifacts described below. A revision is Phase 2
DONE only when all P2-01 through P2-16 requirements have evidence for that
exact SHA. Without the required evidence, the revision is not yet qualified.
This specification is not a blanket acceptance verdict: a successful build,
an uploaded image, or a desktop screenshot alone does not close the phase.

**Starting point:** `rcu-free-v3`,
`16a28b193ffcbe213b876960b2cb910627feaee2`, following the Phase 1 foundation.

**Specification date:** 2026-10-31.

## Objective and boundary

Turn this repository's own kernel into a firmware-bootable prototype
distribution with maintained Debian userspace, APT/dpkg, a normal unprivileged
desktop account, a real Wayland desktop, keyboard/mouse input, and persistent
storage. Prove the complete path in software-emulated QEMU and preserve the
bootable image and test evidence.

This combines the distribution/image milestone described after Phase 1 with
the first graphical foundation. Weston supplies an existing reference desktop
and terminal; it is not the final product shell. A macOS-inspired Dock, global
menu, file manager, settings, search, animations, accessibility integration,
application ecosystem, and visual design remain later work. There is no macOS
binary compatibility claim and no proprietary Apple component requirement.

The target is a **QEMU development image**, not an installable or secure
general-purpose desktop release. Real hardware, GPU acceleration, Secure Boot,
an installer, encrypted storage, signed updates, recovery, and production
multi-user/login policy are not qualified here.

## Technical contract

| Component | Phase 2 choice |
| --- | --- |
| Guest machine | x86-64, QEMU `q35`, TCG, two virtual CPUs, 2048 MiB RAM |
| Firmware and bootloader | OVMF 4 MiB UEFI firmware and GRUB EFI from the disk's ESP |
| Boot path | UEFI -> GRUB -> this repository's kernel -> persistent Debian root filesystem |
| Kernel | Exact checkout, separate Phase 2 config/build, essential drivers built in |
| Distribution | Debian trixie amd64; signed Debian and Debian Security snapshot dated `20261001T000000Z` |
| Package management | APT/dpkg with recorded package versions and source-package identities |
| PID 1 and devices | systemd, udev, D-Bus, systemd-logind |
| Desktop session | `desktop`, UID/GID 1000; PAM-backed local session on tty1 |
| Compositor | Weston, real DRM/KMS backend, explicit Pixman software renderer |
| Display and input | virtio GPU at 1024x768, virtio keyboard, absolute-pointer tablet |
| Desktop applications | Weston desktop shell and terminal; a test-only Wayland input probe |
| Networking | virtio-net, systemd-networkd DHCP, QEMU user-mode gateway and DNS |
| Disk | 4 GiB GPT regular file: 256 MiB FAT ESP, 3838 MiB ext4 root, alignment/reserved space |
| Test storage | Disposable QCOW2 overlays; round 2 reuses round 1's overlay |
| Host/CI | Linux x86-64; Ubuntu 24.04 GitHub-hosted runner; no KVM requirement |
| Build directory | `.build/phase2/`, isolated from `.build/phase1/` and excluded from Git |

QEMU must boot from the disk and OVMF. Passing `-kernel` or `-initrd` to QEMU
would bypass the bootloader acceptance gate. A headless, nested-X11, nested
Wayland, or remote-rendering Weston backend cannot satisfy the graphics gate.
QEMU itself may use `-display none`: its emulated GPU still runs the DRM/KMS
path and QMP captures that device's actual framebuffer.

The kernel source is still a release candidate. Debian userspace does not
turn that kernel into a production-stable kernel or confer hardware support
that has not been tested.

## Reproduction commands

On Ubuntu 24.04 x86-64, install the host prerequisites:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential bc bison flex libssl-dev libelf-dev pkg-config \
  rsync cpio gzip bzip2 xz-utils curl ca-certificates \
  mmdebstrap debian-archive-keyring dpkg-dev gnupg \
  qemu-system-x86 qemu-utils ovmf grub-efi-amd64-bin \
  mtools dosfstools fdisk e2fsprogs util-linux \
  shellcheck file python3 python3-pil git perl binutils
```

From a clean checkout of the revision to qualify:

```sh
export JOBS=2
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
make -C distro/phase2 check
make -C distro/phase2 kernel
sudo --preserve-env=SOURCE_DATE_EPOCH,JOBS \
  make -C distro/phase2 userspace image
make -C distro/phase2 manifest
make -C distro/phase2 test
make -C distro/phase2 verify
make -C distro/phase2 artifact
```

Only distribution staging and image assembly run through the documented
privileged build invocation. Kernel compilation, QEMU, evidence verification,
and artifact collection run as the invoking user. Privileged build steps must
leave the intended output files accessible to that user; do not recursively
change ownership of the checkout to compensate for an error.

`mmdebstrap` needs access to the pinned archives. A missing archive, signature
failure, failed download, or failed package configuration must fail the build;
do not silently substitute the current Debian mirror or unsigned packages.
The staging root filesystem is not reused after a partial or successful
userspace build. Use a fresh checkout/build directory for an independent
reproduction rather than deleting broad directories or modifying host disks.

The normal build produces `.build/phase2/phase2.img` and a compressed
`.build/phase2/phase2.qcow2`. Test boots use overlays so the reference image is
not modified. `SOURCE_DATE_EPOCH` stabilizes selected metadata but is not a
claim of byte-identical disk images: filesystem metadata, generated machine
state, package tooling, the Ubuntu toolchain, and firmware versions are
separately recorded inputs or remaining sources of variation.

For an interactive session after building, use the dedicated persistent run
overlay and a VNC viewer on the same host:

```sh
bash distro/phase2/scripts/boot-qemu.sh --mode run --vnc 1
```

Connect the viewer to `127.0.0.1:5901`. The VNC listener is loopback-only; do not
expose this auto-login development session on a public interface. Without
`--vnc`, the launcher runs with no host display window. Normal run mode uses
`.build/phase2/run.qcow2`, does not enable the automated guest test, and does
not modify the base image. Shut down the guest cleanly before restarting or
copying its writable overlay.

### Run a downloaded artifact

The curated CI bundle contains a standalone `phase2.qcow2`, `ovmf-code.fd`
and `ovmf-vars.fd`, **not** the raw `phase2.img` expected by the checkout
launcher. Extract the matching artifact into a fresh directory on an x86-64
Linux host with `qemu-system-x86` and `qemu-utils` installed, then run these
commands from that directory:

```sh
(
  set -eu
  sha256sum -c SHA256SUMS
  # Refuse an existing run directory; never overwrite a previous guest disk.
  mkdir -m 700 -- desktop-run
  qemu-img create -f qcow2 -F qcow2 \
    -b "$(realpath -- phase2.qcow2)" desktop-run/disk.qcow2
  cp -- ovmf-vars.fd desktop-run/vars.fd
  qemu-system-x86_64 \
    -machine q35,accel=tcg -cpu qemu64 -m 2048 -smp 2 \
    -drive if=pflash,format=raw,readonly=on,file=ovmf-code.fd \
    -drive if=pflash,format=raw,file=desktop-run/vars.fd \
    -drive if=none,id=root,format=qcow2,file=desktop-run/disk.qcow2 \
    -device virtio-blk-pci,drive=root \
    -vga none -device virtio-gpu-pci,xres=1024,yres=768 \
    -device virtio-keyboard-pci -device virtio-tablet-pci \
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
    -display none -vnc 127.0.0.1:1 \
    -monitor stdio -serial file:desktop-run/serial.log -no-reboot
)
```

Connect VNC to `127.0.0.1:5901`. This is a normal desktop boot: no test-mode
firmware flag is supplied. The base disk and supplied firmware remain unchanged;
user changes live in `desktop-run/disk.qcow2`. Type `system_powerdown` at the
QEMU monitor prompt to request a clean ACPI shutdown, and wait for QEMU to exit.
To resume that guest later, rerun only the QEMU command from the same directory.
Keep the base image at its original absolute path while its overlay is in use.

## Implementation work packages

### P2.1 — Distribution trust and provenance

- Pin the Debian suite, architecture, archive timestamp, image identity,
  downloaded trust-keyring package, and its SHA-256 in the build configuration.
- Use the SHA-256-verified `debian-archive-keyring_2025.1_all.deb` trust bundle
  explicitly for the snapshot bootstrap. The host Ubuntu keyring alone may
  predate trixie's archive keys.
- Preserve Release-file signature and package checksum verification. Disable
  `Valid-Until` checking only for the explicit archived snapshot sources;
  never use `trusted=yes`, `--allow-unauthenticated`, or disabled TLS checks.
- Record the actual repository SHA/dirty state, effective kernel config,
  kernel release, package/source inventory, archive inputs, firmware hashes,
  host package versions, build commands, and image checksums.
- Exercise APT/dpkg with an offline, project-owned proof package: install it,
  check its installed state and payload, purge it, and verify that package
  audit reports no inconsistent installation. No external network download
  is needed to pass this runtime package-manager check.
- Keep Debian package copyright notices and upstream kernel licensing intact.
  A source inventory supports auditing but does not replace the corresponding
  source and other license obligations when distributing a release.

A frozen snapshot is a reproducibility input, not an automatic security-update
policy. Refreshing it is an explicit dependency change requiring fresh tests.
Availability of the selected snapshot and the entire bootstrap path must be
established by CI before this phase is accepted.

### P2.2 — Graphical and distribution-capable kernel

- Build into `.build/phase2/kernel` from this checkout; do not use the host
  kernel, a Debian kernel package, or Phase 1's serial-only `bzImage`.
- Validate effective options after Kconfig dependency resolution, including
  UEFI support, EFI/GPT partition discovery, ext4, virtio block, serial console,
  systemd prerequisites, devtmpfs, virtual terminals, DRM, and evdev input.
- Explicitly require both `CONFIG_DRM_VIRTIO_GPU=y` and this tree's separate
  `CONFIG_DRM_VIRTIO_GPU_KMS=y`, as well as `CONFIG_VIRTIO_INPUT=y` and
  `CONFIG_INPUT_EVDEV=y`.
- Include root-storage and display/input drivers in the kernel so the fixed
  QEMU target does not depend on unproven module/initramfs loading.
- Preserve the resulting config, `kernelrelease`, build log, and image hash.

### P2.3 — Bootable, persistent disk image

- Create a GPT disk with its first partition starting at 1 MiB: 256 MiB ESP,
  then the ext4 root at 257 MiB, with the final 1 MiB reserved for alignment
  and backup GPT metadata.
- Build the FAT and ext4 filesystems as regular files. Use filesystem-image
  tools to populate them and assemble the final regular disk image. Never
  format a host block device, and do not require loop devices or host mounts.
- Install GRUB at the removable-media UEFI boot path and put this build's
  kernel and root-filesystem selection in its configuration.
- Boot the root by its explicitly configured PARTUUID, with `rootwait`;
  do not depend on host filesystem paths or unpredictable drive ordering.
- Copy OVMF code and a fresh variables template into the output directory.
  Firmware code stays read-only; each run receives its own writable variables
  copy. A test must not mutate the host's installed firmware files.
- Produce a compact QCOW2 distribution artifact and retain enough provenance
  to verify it against the build. The raw disk remains a normal local file.

### P2.4 — Unprivileged graphical session

- Start systemd, udev, D-Bus and logind normally from the Debian root.
- Create a `desktop` account with UID/GID 1000 and a persistent home directory.
  Development-image auto-login is explicit; it is not production login policy.
- Give the desktop system service a PAM login session and controlling tty1,
  with the conflicting getty disabled. Let `pam_systemd` manage the user
  runtime directory and seat. Starting a user service with lingering alone
  is not a substitute for creating a real local login session.
- Launch Weston as that user with `--backend=drm --renderer=pixman` and force
  libseat's logind backend. No root compositor, globally writable input
  devices, or unconditional `video`/`input` group access is required.
- Load the normal Weston desktop shell and make a terminal available. The
  guest's native graphical input path must work independently of the serial
  console or test-reporting mechanism.
- If systemd notification readiness is used, load `systemd-notify.so` and use
  `NotifyAccess=main`, not `NotifyAccess=all` with `PAMName=`. The graphical
  probe still has to verify a usable client connection and rendered output.

The test probe is an acceptance instrument, not a custom product shell.
Only test-mode boots start the input probe; the host acceptance harness drives
its input and successful shutdown. Normal boots still perform prerequisite
health checks and fail closed on startup errors, but otherwise remain usable
as a desktop session.

### P2.5 — End-to-end QEMU scenarios

All three scenarios have a **300-second total timeout per boot**, capture
serial output and QMP activity, and verify actual guest/kernel/source identity.
The QMP monitor must be private to the local test process, with no public
network listener. The host must not send the nonce through the kernel command
line, firmware test flag, serial input, a shared directory, or a prewritten
guest file.

1. **`round1`: real input and storage.** Boot a fresh disposable overlay through
   UEFI and GRUB. Prove systemd, offline package install/removal, DHCP and
   reachability of the QEMU gateway, non-root Weston
   using DRM/Pixman, and the visible client. Capture the ready framebuffer.
   Generate a fresh host nonce, type it using QMP keyboard events, and require
   the Wayland client to receive the exact value. Move and click the pointer
   using QMP input events and require the client to acknowledge that event.
   Persist the input-derived nonce in the user's home, sync it, show the
   normal desktop/terminal, capture evidence, and shut down cleanly.
2. **`round2`: independent boot, same persistent disk.** Boot the same overlay
   with a fresh firmware-variables copy. Read and match the exact round 1
   nonce, repeat graphical/input checks, capture the visible result, and
   shut down cleanly. Regenerating the expected file at boot is not a valid
   persistence test.
3. **`negative`: no graphics device.** Boot the valid image with no emulated
   GPU, using a separate disposable overlay. Require evidence that the guest
   reached the relevant initialization stage but no usable DRM desktop could
   start. Missing QEMU, firmware, disk files, or an unrelated host launch error
   cannot count as the expected failure. No positive desktop success marker
   or headless-backend fallback is allowed.

Capture `logs/boot-{scenario}.log`, `logs/qmp-{scenario}.jsonl`, and the
round 1/2 `ready`, `keyboard`, `pointer`, and `desktop` screenshots under
`.build/phase2/screenshots/`, retaining both PPM and PNG forms. Screenshots
must come from the emulated framebuffer, not a generated mockup or another
desktop. Bind `test-results.json` to the SHA-256 of the build manifest.

A zero QEMU exit status alone is insufficient: a panic or reboot can terminate
the process without proving a clean shutdown. Positive cases need explicit
shutdown evidence, correct assertions, bounded timing, and no panic/oops or
unexpected failure output. An input command being present in the QMP log is
not evidence that the guest received it.

### P2.6 — CI, artifact and regression qualification

`.github/workflows/phase2-desktop.yml` uses Ubuntu 24.04, read-only repository
permissions, SHA-pinned actions, an exact event-SHA checkout, `JOBS=2`,
and no build cache. It runs `check`, `kernel`, `userspace`, `image`, `manifest`,
`test`, and `verify`. Only the documented userspace/image step uses sudo.

Artifact collection and upload run even after a failure, without suppressing
the failed step. The curated artifact includes available bootable compressed
disk/firmware outputs, checksums, configuration/provenance, package inventory,
build and boot logs, QMP transcripts, screenshots, and the machine-readable
verdict. Its name contains source SHA, workflow run ID and attempt number;
retention is 14 days. Preserve release evidence separately if longer retention
is required.

Require **two clean Phase 2 workflow runs or complete workflow attempts on the
same source SHA**, using fresh hosted runners, and a successful Phase 1
regression workflow on that same SHA. Compare provenance from the two Phase 2
attempts; do not describe two jobs inside a single run as two independent
runs. A later source change needs its own validation. Cancelling obsolete
push runs does not erase completed acceptance evidence.

## Mandatory acceptance requirements

Evaluate these requirements for the exact commit being reviewed. Evidence paths
below are relative to that attempt's curated artifact unless stated otherwise;
the table defines checks, not a permanent pending or passed verdict.

| ID | Mandatory assertion | Verification evidence and location |
| --- | --- | --- |
| P2-01 | Exact source and dependency identity is recorded | `manifest.json`, `source-revision`, `rootfs-packages.tsv`, `logs/host-packages.log` and `logs/ci-environment.json` agree on the clean source and inputs |
| P2-02 | Correct own kernel and effective graphical/systemd config | `logs/build-kernel.log`, `logs/kernel-config-validation.log`, `kernelrelease`, `kernel.config` and kernel checksum |
| P2-03 | Signed pinned Debian bootstrap completes without trust bypass | `inputs.lock`, `logs/build-rootfs.log`, `apt-release-metadata/` and `rootfs-sources.list` |
| P2-04 | APT/dpkg installs and removes a package coherently | Positive boot logs: offline proof-package install/payload/purge, empty `dpkg --audit`, and `P2:PACKAGE_OK`; package inventory |
| P2-05 | Standalone GPT/ESP image boots through OVMF and GRUB | `partition-table.json`, `grub.cfg`, image-build log, and source-bound QEMU command without direct kernel boot |
| P2-06 | Expected kernel/source, systemd PID 1 and QEMU network operate | Positive boot logs: runtime identity, DHCP/default route and gateway-reachability assertions |
| P2-07 | Desktop runs unprivileged through a local logind session | Positive boot logs: UID 1000, session/runtime-directory assertions and compositor diagnostics |
| P2-08 | Real DRM/KMS output uses Pixman, not a headless/nested backend | Fresh Weston diagnostics in boot logs, native rendered probe, and framebuffer captures in `screenshots/` |
| P2-09 | Real keyboard and pointer events reach a Wayland client | `test-results.json` nonce, matching guest acknowledgments in boot logs, and `logs/qmp-{round1,round2}.jsonl` |
| P2-10 | User data survives a separate second boot | Round 1 input-derived nonce matches round 2's persisted value in results and boot logs |
| P2-11 | Normal reference desktop and terminal are visible | `P2:TERMINAL_READY` from the PTY/shell checks and actual post-probe `screenshots/{round1,round2}-desktop.*` |
| P2-12 | Both positive boots and expected negative outcome are bounded | `test-results.json` scenario verdicts/timings and complete boot logs with shutdown/failure evidence |
| P2-13 | Missing GPU is correctly rejected without a false-positive fallback | `logs/boot-negative.log`: guest-stage `P2:FAIL:NO_DRM`, no desktop pass, and no host-launch substitution |
| P2-14 | Bootable artifact and evidence are verifiable | `SHA256SUMS`, manifest-bound results, `diagnostics/acceptance-verification.log`, `phase2.qcow2` and OVMF copies |
| P2-15 | Independent reproduction and Phase 1 regression pass | Two successful same-SHA Phase 2 full run/attempt URLs and artifacts, plus a successful same-SHA Phase 1 result and artifact |
| P2-16 | Documentation, license provenance and scope are accurate | Review the exact revision's instructions/notices, `SOURCE.txt`, `licenses/` and package/source inventory; no unsupported release claims |

### Determine the qualification of a commit

1. Select the full source SHA, not merely the branch name or latest workflow
   badge. Find **two successful clean full runs or full attempts** for that SHA
   on the [Phase 2 workflow page](https://github.com/boop-bip80/operating-system-1/actions/workflows/phase2-desktop.yml).
   Both must complete the build, all three QEMU scenarios, verification and
   artifact upload. An in-progress run or a successful host-check step is not
   acceptance evidence.
2. Download each matching `phase2-{sha}-{run_id}-attempt-{attempt}` artifact.
   Check its `SHA256SUMS`; confirm the manifest and CI metadata identify the
   chosen clean SHA and the respective run/attempt. The canonical
   `test-results.json` must say `pass`, contain passing `round1`, `round2` and
   `negative` cases, and bind to the actual manifest hash. Review the verifier
   log and the evidence in P2-01 through P2-16. A diagnostic-only report or a
   `not_validated` verdict cannot qualify a commit, even if upload succeeded.
3. Confirm a successful complete run on the
   [Phase 1 workflow page](https://github.com/boop-bip80/operating-system-1/actions/workflows/phase1-boot.yml)
   for the **same SHA**, including its four boot cases and verified artifact.
   The historical Phase 1 reference verdict does not replace this regression.
4. Compare both Phase 2 manifests and record the exact SHA, run/attempt URLs,
   artifact identities/checksums and review outcome in the release, PR or
   reviewer evidence record. Keep the required evidence beyond the 14-day
   artifact retention window when a lasting qualification record is needed.

**DONE requires all P2-01 through P2-16 for that same source revision.** The
matching CI/artifact evidence establishes the verdict; no source edit is
needed to change this specification from pending to passed. A newer commit,
including a documentation-only commit, requires its own qualification. Missing
or incomplete evidence must be reported as unqualified, never inferred from
an earlier revision's success.

## Failure contract and next milestone

Fail nonzero for a failed trust check, unsupported host, wrong source/kernel,
missing required kernel option, unbootable disk, incorrect root selection,
failed PAM/local session, privileged compositor, missing DRM output/input,
wrong or missing nonce, persistence mismatch, timeout, panic/oops, or invalid
shutdown. Preserve the evidence and identify the failing layer. Never change
the test into a headless-only run to make the graphical gate pass.

After acceptance, the next milestone is an original desktop shell and user
experience built on this verified distribution/Wayland foundation. The
reference Weston desktop is deliberately replaceable. Track hardware,
installation, update/recovery, app delivery, security policy, and compatibility
as separate, explicitly tested workstreams rather than implying they follow
automatically from a bootable image.

See [Phase 1](PHASE_01.md), [build provenance](BUILD_PROVENANCE.md), and
[boot troubleshooting](BOOT_TROUBLESHOOTING.md) for the existing foundation.
Primary session and compositor references are the
[Weston startup documentation](https://wayland.pages.freedesktop.org/weston/toc/running-weston.html),
[trixie Weston CLI](https://manpages.debian.org/trixie/weston/weston.1.en.html),
[trixie DRM backend](https://manpages.debian.org/trixie/weston/weston-drm.7.en.html),
[systemd execution settings](https://manpages.debian.org/trixie/systemd/systemd.exec.5.en.html),
and [pam_systemd](https://manpages.debian.org/trixie/libpam-systemd/pam_systemd.8.en.html).
