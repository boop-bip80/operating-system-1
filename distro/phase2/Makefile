# SPDX-License-Identifier: GPL-2.0-only
SHELL := /bin/bash
.DEFAULT_GOAL := help
.NOTPARALLEL:
.PHONY: help check build kernel userspace rootfs image manifest test run verify artifact

help:
	@printf '%s\n' 'Phase 2 commands (see docs/os/PHASE_02.md for privilege separation):' \
	  '  make -C distro/phase2 check      syntax, lint, and host-side tests' \
	  '  make -C distro/phase2 kernel     build this repository kernel' \
	  '  make -C distro/phase2 userspace  signed Debian rootfs (explicit root required)' \
	  '  make -C distro/phase2 image      standalone UEFI disk (explicit root required)' \
	  '  make -C distro/phase2 manifest   record artifacts and source provenance' \
	  '  make -C distro/phase2 test       boot image, inject input, test persistence' \
	  '  make -C distro/phase2 run        interactive graphical QEMU boot' \
	  '  make -C distro/phase2 verify     verify provenance and acceptance evidence' \
	  '  make -C distro/phase2 artifact   collect curated artifacts and checksums'

check:
	bash scripts/check.sh

kernel:
	bash scripts/build-kernel.sh

userspace:
	bash scripts/build-rootfs.sh

rootfs: userspace

image:
	bash scripts/build-image.sh

manifest:
	python3 scripts/manifest.py create

build: kernel userspace image manifest

# Never silently rebuild the exact artifacts under acceptance test.
test:
	python3 scripts/smoke-test.py

run:
	bash scripts/boot-qemu.sh --mode run

verify:
	python3 scripts/manifest.py verify

artifact:
	bash scripts/artifact.sh
