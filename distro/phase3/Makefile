# SPDX-License-Identifier: GPL-2.0-only
SHELL := /bin/bash
.DEFAULT_GOAL := help
.NOTPARALLEL:
.PHONY: help check kernel userspace image manifest test verify artifact run

help:
	@printf '%s\n' 'Phase 3 original Wayland desktop (see docs/os/PHASE_03.md):' \
	  '  make -C distro/phase3 check       syntax, lint and host tests' \
	  '  make -C distro/phase3 kernel      own Phase 2 kernel, source-bound build' \
	  '  sudo make -C distro/phase3 userspace  signed Debian and Phase 3 overlay' \
	  '  sudo make -C distro/phase3 image      UEFI disk with original shell' \
	  '  make -C distro/phase3 manifest    record Phase 3 artifact identity' \
	  '  make -C distro/phase3 test        QEMU framebuffer and input tests' \
	  '  make -C distro/phase3 verify      verify Phase 3 evidence' \
	  '  make -C distro/phase3 artifact    curated downloadable evidence' \
	  '  make -C distro/phase3 run         interactive VNC session'

check:
	bash scripts/check.sh

kernel:
	mkdir -p ../../.build/phase3/logs ../../.build/phase3/screenshots
	$(MAKE) -C ../phase2 kernel

userspace:
	$(MAKE) -C ../phase2 userspace
	bash scripts/install-rootfs.sh

image:
	$(MAKE) -C ../phase2 image
	bash scripts/package-image.sh

manifest:
	python3 scripts/manifest.py create

test:
	python3 scripts/smoke-test.py

verify:
	python3 scripts/manifest.py verify

artifact:
	bash scripts/artifact.sh

run:
	bash scripts/boot-qemu.sh --mode run
