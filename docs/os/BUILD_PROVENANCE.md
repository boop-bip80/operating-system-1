<!-- SPDX-License-Identifier: GPL-2.0 -->
# Phase 1 build provenance and evidence

## Source identity

The initial baseline is `1c542b368c25323551c74278e64a886ed0c6f129` on
`rcu-free-v3`; its root Makefile describes Linux 7.3-rc7. This is historical
provenance, not a hardcoded claim that future builds use that commit.

Each build records the checked-out commit, dirty status, effective configuration,
kernel release, dependency inputs, and tool versions. A developer build with
local edits is not equivalent to a clean CI run of the named commit. CI must
compare `git rev-parse HEAD` with `GITHUB_SHA`. Detached HEAD is expected; a PR
build may use GitHub's synthetic merge commit rather than the PR branch tip.

The guest also prints the embedded expected source identity and its actual
`uname -r`; the host validates both against the generated build metadata.
The build scripts compile the kernel in this tree and do not copy a distro
kernel from `/boot` or from the host machine.

## Pinned inputs and toolchain records

`distro/phase1/inputs.lock` is authoritative for the BusyBox source URL,
version, and SHA-256. The archive must pass checksum verification before use.
Do not label the selected version "latest" or silently update the pin to get a
different download to pass. A dependency update is a reviewed change with new
build/boot evidence.

GitHub workflow action revisions are immutable commit SHAs:

| Action | Version label | Commit |
| --- | --- | --- |
| `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| `actions/upload-artifact` | v7.0.2 | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` |

These action pins do **not** make the whole toolchain immutable. Ubuntu's
runner image and apt packages can change. Preserve the runner/image identifiers,
GCC/binutils/make versions, musl package version, QEMU version, Python version,
and installed package-version listing with each CI artifact. Pinning a
container by digest while installing changing apt packages would still not be
a fully hermetic toolchain.

Phase 1's required reproducibility claim is that the documented inputs and
commands produce the same functional result in two clean runs of one source
commit. Bit-for-bit whole-kernel reproducibility is not claimed unless it is
separately demonstrated with normalized metadata and a fully pinned environment.
Initramfs packaging does normalize ordering, owners, and timestamps and uses
gzip without a timestamp. The kernel's `gen_init_cpio` encodes device nodes
without requiring host root privileges.

## Evidence bundle

`make -C distro/phase1 artifact` prepares a curated bundle under
`.build/phase1/artifacts/`. It must collect whatever evidence is available even
if an earlier stage failed; incomplete bundles are diagnostic evidence, not
passing releases. Depending on the stage reached, the bundle includes:

- Repository-built `bzImage` and `initramfs.cpio.gz`.
- Effective kernel/BusyBox configurations and locked dependency inputs.
- Build provenance manifest and `kernelrelease`.
- SHA-256 checksums, configuration/archive validation, and host package versions.
- Kernel/BusyBox build logs; both persistence boot logs; interactive-session
  log; broken-image negative-test log; structured test results.

The workflow names artifacts using source SHA, run ID, and attempt number.
It uploads only this curated directory, not the whole checkout, download cache,
home directory, or host environment. Hidden config files are intentionally
included. No credentials or secret environment dumps belong in an artifact.

For acceptance, rerun **all jobs** on the same source SHA using a fresh runner.
Keep both run/attempt URLs. A failed attempt must remain visible in the history;
do not overwrite evidence or change a status document to suggest the failure
never happened. Two independent matrix jobs are useful but are not described as
two separate workflow runs. Artifacts retain for 14 days under the initial
workflow policy; export evidence needed for long-term release/review records.

## Licensing and redistribution

`COPYING` is restored byte-for-byte from the kernel tree at
`549531755d329ec48439e3263535351ba1c5acd5`, before it was blanked. The matching
Git blob is `a635a38ef9405fdfcfe97f3a435393c1e9cae971`. It directs readers to
`LICENSES/preferred/GPL-2.0`, the explicit syscall exception at
`LICENSES/exceptions/Linux-syscall-note`, and the per-file licensing rules.
Do not remove original copyright/SPDX notices or change the kernel's license
as part of distro branding.

BusyBox is GPL-2.0 software; preserve its license and provide corresponding
source/configuration as required when distributing its binary. musl has its own
MIT-style license; retain the applicable copyright/license notices for the
static C library. The pinned source URL and hash are provenance, not a substitute
for fulfilling redistribution requirements. The Phase 1 boot bundle is not a
claim that a public product release has completed all licensing/release review.

The planned macOS-inspired desktop uses original implementation and assets;
this repository is Linux-based and is neither macOS nor affiliated with Apple.

## Reference material

- [Linux build instructions](https://docs.kernel.org/admin-guide/README.html)
- [Linux initramfs format and device-node generation](https://docs.kernel.org/filesystems/ramfs-rootfs-initramfs.html)
- [QEMU direct Linux boot](https://www.qemu.org/docs/master/system/linuxboot.html)
- [BusyBox sources](https://busybox.net/downloads/)
- [GitHub workflow artifacts](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts)
