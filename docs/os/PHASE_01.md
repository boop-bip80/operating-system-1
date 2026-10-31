<!-- SPDX-License-Identifier: GPL-2.0 -->
# Phase 1 — Reproducible kernel-to-shell foundation

**Product direction:** a complete macOS-inspired Linux desktop experience.

**Status:** **DONE for reference commit `4cc3727310198a1df85f47a818873477bcd31f5b`**:
two fresh GitHub Actions attempts passed all 14 acceptance criteria on that
revision. This is an x86-64/QEMU-only qualification, not a desktop release.
Any later source revision requires its own clean validation before being
accepted; see the linked evidence below.

**Source baseline:** `rcu-free-v3`, `1c542b368c25323551c74278e64a886ed0c6f129`, Linux 7.3-rc7.

**Specification date:** 2026-10-31.

## Objective

Build this repository's own x86-64 Linux kernel, a static BusyBox userspace,
and a deterministic initramfs. Prove that the resulting guest boots, provides
an interactive shell, can read/write persistent ext4 storage, obtains a DHCP
lease, and shuts down cleanly. Capture the complete evidence in GitHub Actions.

This phase is **not** an installer, live ISO, GUI, general-purpose desktop
distribution, hardware qualification, or macOS application compatibility layer.
It is the prerequisite for those later workstreams. Do not replace boot
validation with desktop feature work or a screenshot of a different OS.

## Technical contract

| Component | Phase 1 choice |
| --- | --- |
| Guest CPU/machine | x86-64, QEMU `q35`, TCG, two virtual CPUs, 1 GiB RAM |
| Boot | Direct Linux `-kernel` / `-initrd` boot and serial console |
| Kernel | Compiled from the exact checked-out repository commit |
| Userspace | Statically linked BusyBox; shell-based `/init` as PID 1 |
| Initial filesystem | Ordered `newc` CPIO archive, deterministic gzip |
| Persistent storage | Disposable 128 MiB ext4 file image attached via virtio-blk |
| Networking | virtio-net and QEMU user-mode DHCP; no public Internet requirement |
| Host/CI | Linux x86-64; Ubuntu 24.04 GitHub-hosted runner |
| Build outputs | `.build/phase1/`, excluded from Git |
| Evidence | Exact-line serial markers, guest/kernel identity, exit status, logs, checksums |

The `-rc7` source is a release candidate, and the branch name is not evidence of
production readiness. Phase 1 qualifies one virtual target only. UEFI, GRUB,
Secure Boot, GPUs, real storage controllers, suspend, and real hardware remain
unqualified.

## Reproduction commands

On Ubuntu 24.04 x86-64, install the build and test prerequisites:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential bc bison flex libssl-dev libelf-dev pkg-config \
  musl-tools rsync cpio gzip bzip2 xz-utils curl ca-certificates \
  qemu-system-x86 e2fsprogs shellcheck file python3 git perl binutils
```

From a clean checkout of the commit being reviewed:

```sh
make -C distro/phase1 check
make -C distro/phase1 build
make -C distro/phase1 test
make -C distro/phase1 verify
make -C distro/phase1 artifact
```

To use the serial shell manually after a build:

```sh
make -C distro/phase1 run
```

Inside that VM, `uname -r` must match the generated `kernelrelease` file.
Use `poweroff -f` to shut down the shell-based PID 1 guest. All persistent
test operations target a regular image file, never a physical host disk.
QEMU itself and filesystem-image creation require no host root privileges.

Keep `.build/phase1/` local. Do not commit images, downloaded archives, compiler
outputs, test disks, or logs into the kernel source tree. Use the curated CI
artifact for evidence. For an entirely fresh reproduction, use a new checkout
or a fresh CI runner rather than deleting broad directories.

## Implementation work packages

### P1.1 — Source and dependency provenance

- Record actual `git rev-parse HEAD`, source dirty state, kernel release,
  dependency/source checksums, configuration hashes, and tool versions.
- Preserve the historical baseline without assuming every later commit equals
  it. CI checkouts may be detached; PR runs may build a synthetic merge commit.
- Keep the pinned BusyBox archive and action commit SHAs explicit. Record the
  Ubuntu runner and installed package versions; do not claim the changing
  distribution package repository is a fully hermetic toolchain.
- Restore upstream `COPYING` and meaningful project/kernel documentation,
  preserving `LICENSES/` and all other upstream notices.

### P1.2 — Configure and compile the kernel

- Use an out-of-tree Kbuild directory under `.build/phase1/kernel`.
- Start from `x86_64_defconfig`, apply `kernel.config.fragment`, and run
  `olddefconfig`; validate the effective options after dependency resolution.
- Build boot-critical facilities into the kernel: ELF and script execution,
  initramfs/gzip, serial console, ACPI poweroff, virtual filesystems, PCI and
  virtio devices, IPv4/packet networking, and ext4.
- Build a nonempty `arch/x86/boot/bzImage`. Never substitute the host's kernel.
- Preserve the effective config, source identity, kernel release, diagnostics,
  and image hash for review.

### P1.3 — Static userspace and initramfs

- Fetch the fixed BusyBox source archive, verify its SHA-256, compile a static
  musl-linked executable, and verify that no dynamic interpreter/libraries are
  required.
- Include the shell, filesystem and network tools, finite DHCP client, and
  controlling-terminal helpers required by the guest and test harness.
- Package executable `/init`, the DHCP lease callback, expected source/release
  identity, and required device nodes. The kernel's `gen_init_cpio` creates the
  archive's `/dev/console` and `/dev/null` nodes without host `mknod` privileges.
- `/init` remains alive as PID 1, mounts procfs/sysfs/devtmpfs/tmpfs, handles
  failures explicitly, and supervises the interactive shell. Test failures
  must not fall through to success markers.
- Generate the archive with stable ordering, ownership, and timestamps, and
  compress it without a gzip timestamp. Validate the required members.

### P1.4 — Direct boot and runtime identity

- Start QEMU `q35` in software emulation with the locally built `bzImage` and
  initramfs. Do not require KVM or a privileged runner.
- Capture all serial output. The host checks the guest's actual `uname -r`
  against the build's expected release and checks the source marker.
- Reach PID 1 without a panic within 180 seconds per boot. `P1:INIT_OK` is a
  milestone marker, not by itself proof that the entire boot test passed.

### P1.5 — Four boot scenarios

1. **Persistence round 1:** create a fresh disposable ext4 image; boot with a
   unique host-generated nonce; prove temporary filesystem read/write; write
   and sync the nonce to ext4; obtain an IPv4 DHCP lease; unmount and power off.
2. **Persistence round 2:** boot the same kernel/initramfs and image; read back
   the exact nonce from round 1; repeat networking checks; power off cleanly.
3. **Interactive session:** boot without test mode, wait for the real shell,
   send shell commands, verify command output and file read/write, then shut
   down. An echoed input command is not accepted as successful command output.
4. **Negative control:** boot an intentionally broken initramfs and verify
   actual guest boot failure and missing success markers. A missing QEMU binary,
   invalid host path, or unrelated host startup error cannot satisfy this test.

Test markers include `P1:INIT_OK`, `P1:FS_OK`, `P1:DISK_WRITE_OK`,
`P1:DISK_PERSIST_OK`, `P1:NET_OK`, `P1:ROUND1_OK`, and `P1:PASS` as appropriate.
The host validates the expected ordering, kernel/source identity, explicit
clean poweroff, zero QEMU exit status, and absence of panic/oops/failure output.
`-no-reboot` can cause an abnormal guest to exit QEMU normally, so zero exit
status alone is not proof of success. Every boot has a bounded timeout.

### P1.6 — CI and handover evidence

`.github/workflows/phase1-boot.yml` runs on pushes to `rcu-free-v3`, pull
requests, and manual dispatch. It uses a shallow checkout of the exact event
SHA, read-only repository permissions, and no build cache for the initial
independent reproduction checks.

The workflow executes `check`, `build`, `test`, and `verify`, then always
collects and uploads any available evidence with a name containing source SHA,
run ID, and attempt number. Do not suppress failures with `continue-on-error`.
Artifacts expire after 14 days; preserve release/reviewer evidence separately
when longer retention is needed.

At least **two clean workflow runs or full workflow attempts on the same source
commit** must pass. Two jobs in one matrix are not described as two separate
workflow runs. Record both run/attempt URLs and compare their source/toolchain
manifests. A source change requires new validation for that source revision.

## Mandatory acceptance record

The verdicts below are scoped to the independently tested reference commit
`4cc3727310198a1df85f47a818873477bcd31f5b`. Each is backed by the
linked CI runs, their per-attempt artifacts, or reviewed repository contents.
Do not transfer these verdicts to a later commit without fresh evidence.

| ID | Mandatory assertion | Required evidence | Verdict |
| --- | --- | --- | --- |
| AC-01 | Correct repository commit and baseline recorded | Actual source SHA and manifest | Verified |
| AC-02 | `bzImage` built from this kernel | Build log, config, checksum | Verified |
| AC-03 | Boot-critical options effective as built-ins | Configuration validation output | Verified |
| AC-04 | Executable `/init`, static BusyBox, valid archive | ELF and archive validation | Verified |
| AC-05 | PID 1 reached without panic within 180 seconds | Round 1 serial log and timing | Verified |
| AC-06 | Real interactive shell accepts and executes commands | Non-test serial session | Verified |
| AC-07 | procfs/sysfs/devtmpfs/tmpfs and temporary writes work | Guest assertions and `P1:FS_OK` | Verified |
| AC-08 | Virtio ext4 data survives a second boot | Unique-nonce write/read logs | Verified |
| AC-09 | Virtio-net receives and applies DHCP IPv4 configuration | Guest network evidence | Verified |
| AC-10 | Both persistence boots power off cleanly on time | Exit status, shutdown evidence, timing | Verified |
| AC-11 | Broken initramfs is rejected for a guest boot failure | Negative-control log | Verified |
| AC-12 | Two clean CI runs/attempts pass on the same commit | Two workflow attempt URLs and artifacts | Verified |
| AC-13 | README and license provenance are correct | Reviewed `README`, `COPYING`, provenance | Verified |
| AC-14 | Documented commands reproduce the tested result | Reviewer reproduction or CI evidence | Verified |

**DONE requires AC-01 through AC-14 to be verified.** An implementation, a PR,
syntax-only checks, an untested artifact, or a screenshot does not close the
phase. Keep implementation status separate from acceptance status.

### Accepted CI evidence (2026-10-31)

- Tested commit: [`4cc3727310198a1df85f47a818873477bcd31f5b`](https://github.com/boop-bip80/operating-system-1/commit/4cc3727310198a1df85f47a818873477bcd31f5b).
- First independent success: [workflow attempt 1](https://github.com/boop-bip80/operating-system-1/actions/runs/41235925218/attempts/1), [artifact 13056445678](https://github.com/boop-bip80/operating-system-1/actions/runs/41235925218/artifacts/13056445678).
- Second independent success: [workflow attempt 2](https://github.com/boop-bip80/operating-system-1/actions/runs/41235925218/attempts/2), [artifact 13056193831](https://github.com/boop-bip80/operating-system-1/actions/runs/41235925218/artifacts/13056193831).
- Both artifact names include source SHA `4cc3727310198a1df85f47a818873477bcd31f5b`,
  run ID `41235925218`, and their respective attempt numbers. Each bundle
  contains `SHA256SUMS`, `manifest.json`, kernel/BusyBox configuration and
  build logs, serial logs, and `test-results.json` bound to its manifest hash.
- Both executions built Linux `7.3.0-rc7-phase1+`, validated static BusyBox and
  65 initramfs entries, passed the 89 host tests and all four QEMU cases
  (`round1`, `round2`, `interactive`, `negative`), and passed manifest verification.
- Reviewer verdict: **AC-01–AC-14 verified for the tested commit**. No known
  Phase 1 acceptance blockers remain for the QEMU reference target. A new
  commit—even a documentation-only follow-up—must pass its own CI gate.

## Failure contract and deferred work

Fail nonzero for a missing/mismatched build artifact, wrong kernel/source
identity, absent init, timeout, panic/oops, missing/out-of-order markers,
unconfigured DHCP interface, ext4 mismatch, or failed shutdown. Keep the full
log and a clear failure reason. The deliberately broken-image case must prove
that the test harness rejects the guest correctly rather than hiding a host
failure.

After all acceptance rows pass, Phase 2 can introduce a maintained distribution
userspace, package management, and a bootable image. Desktop/compositor work,
the Dock, global menu, file manager, settings, search, animations, accessibility,
installer, and real hardware support are later milestones. There is no macOS
binary compatibility claim and no proprietary Apple component requirement.

See [build provenance](BUILD_PROVENANCE.md) and
[boot troubleshooting](BOOT_TROUBLESHOOTING.md) for the supporting procedures.
