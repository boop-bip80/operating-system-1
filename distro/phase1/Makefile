# SPDX-License-Identifier: GPL-2.0-only
SHELL := /bin/bash
.DEFAULT_GOAL := help
.NOTPARALLEL:
.PHONY: help check build kernel userspace initramfs manifest test run verify artifact

help:
	@printf '%s\n' 'Phase 1 commands (run as an ordinary user):' \
	  '  make -C distro/phase1 check     syntax, lint, and harness unit tests' \
	  '  make -C distro/phase1 build     kernel + static userspace + provenance' \
	  '  make -C distro/phase1 test      persistence, shell, and bad-image boots' \
	  '  make -C distro/phase1 run       interactive QEMU serial shell' \
	  '  make -C distro/phase1 verify    verify provenance and all test results' \
	  '  make -C distro/phase1 artifact  collect curated artifacts and checksums'

check:
	bash scripts/check.sh

kernel:
	bash scripts/build-kernel.sh

userspace:
	bash scripts/build-busybox.sh

initramfs: kernel userspace
	bash scripts/package-initramfs.sh

manifest: initramfs
	python3 scripts/manifest.py create

build: manifest

# Do not rebuild or silently change the artifacts being tested.
test:
	bash scripts/smoke-test.sh

run:
	bash scripts/make-test-disk.sh run
	bash scripts/boot-qemu.sh --mode interactive

verify:
	python3 scripts/manifest.py verify

artifact:
	bash scripts/artifact.sh
