#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise build-path guards without root, disk devices, downloads, or QEMU."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid


PHASE = Path(__file__).resolve().parent.parent


def lock_values():
    values = {}
    for line in (PHASE / "inputs.lock").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def config_values():
    values = {}
    for line in (PHASE / "kernel.config.fragment").read_text().splitlines():
        if line.startswith("CONFIG_"):
            key, value = line.split("=", 1)
            values[key] = value
        elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
            values[line[2:-11]] = "n"
    return values


class OutputSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="phase2-platform-test-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.phase = self.repo / "distro" / "phase2"
        self.scripts = self.phase / "scripts"
        self.scripts.mkdir(parents=True)
        shutil.copyfile(PHASE / "scripts" / "common.sh", self.scripts / "common.sh")
        shutil.copyfile(PHASE / "inputs.lock", self.phase / "inputs.lock")
        self.out = self.repo / ".build" / "phase2"

    def run_guard(self, script, *arguments):
        env = os.environ.copy()
        env.update(SOURCE_DATE_EPOCH="1", JOBS="2")
        return subprocess.run(
            ["bash", "-c", 'source "$1"; shift; ' + script, "phase2-test",
             str(self.scripts / "common.sh"), *map(str, arguments)],
            text=True, capture_output=True, env=env, check=False,
        )

    def prepare(self):
        result = self.run_guard("phase2_prepare_output")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_prepare_creates_only_fixed_build_subdirectories(self):
        self.prepare()
        self.assertTrue((self.out / "logs").is_dir())
        self.assertTrue((self.out / "downloads").is_dir())

    def test_rejects_build_root_symlink(self):
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        (self.repo / ".build").symlink_to(elsewhere, target_is_directory=True)
        result = self.run_guard("phase2_prepare_output")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_rejects_output_root_symlink(self):
        (self.repo / ".build").mkdir()
        self.out.symlink_to(Path(self.temporary.name) / "missing")
        self.assertNotEqual(self.run_guard("phase2_prepare_output").returncode, 0)

    def test_rejects_child_directory_symlink(self):
        self.out.mkdir(parents=True)
        (self.out / "logs").symlink_to(Path(self.temporary.name), target_is_directory=True)
        self.assertNotEqual(self.run_guard("phase2_prepare_output").returncode, 0)

    def test_rejects_nondirectory_build_root(self):
        (self.repo / ".build").write_text("sentinel")
        self.assertNotEqual(self.run_guard("phase2_prepare_output").returncode, 0)
        self.assertEqual((self.repo / ".build").read_text(), "sentinel")

    def test_accepts_new_and_regular_fixed_output(self):
        self.prepare()
        image = self.out / "phase2.img"
        self.assertEqual(self.run_guard('phase2_output_file "$1"', image).returncode, 0)
        image.write_bytes(b"sentinel")
        self.assertEqual(self.run_guard('phase2_output_file "$1"', image).returncode, 0)
        self.assertEqual(image.read_bytes(), b"sentinel")

    def test_rejects_symlink_output_without_touching_target(self):
        self.prepare()
        sentinel = Path(self.temporary.name) / "sentinel"
        sentinel.write_bytes(b"do not change")
        image = self.out / "phase2.img"
        image.symlink_to(sentinel)
        self.assertNotEqual(self.run_guard('phase2_output_file "$1"', image).returncode, 0)
        self.assertEqual(sentinel.read_bytes(), b"do not change")

    def test_rejects_dangling_symlink_output(self):
        self.prepare()
        image = self.out / "phase2.img"
        image.symlink_to(self.out / "missing")
        self.assertNotEqual(self.run_guard('phase2_output_file "$1"', image).returncode, 0)

    def test_rejects_hardlink_output_without_touching_target(self):
        self.prepare()
        sentinel = Path(self.temporary.name) / "sentinel"
        sentinel.write_bytes(b"do not change")
        image = self.out / "phase2.img"
        os.link(sentinel, image)
        self.assertNotEqual(self.run_guard('phase2_output_file "$1"', image).returncode, 0)
        self.assertEqual(sentinel.read_bytes(), b"do not change")

    def test_rejects_fifo_output_without_opening_it(self):
        self.prepare()
        fifo = self.out / "phase2.img"
        os.mkfifo(fifo)
        self.assertNotEqual(self.run_guard('phase2_output_file "$1"', fifo).returncode, 0)

    def test_rejects_directory_as_image(self):
        self.prepare()
        image = self.out / "phase2.img"
        image.mkdir()
        self.assertNotEqual(self.run_guard('phase2_output_file "$1"', image).returncode, 0)

    def test_rejects_output_outside_build_directory(self):
        self.prepare()
        outside = Path(self.temporary.name) / "outside.img"
        self.assertNotEqual(self.run_guard('phase2_output_file "$1"', outside).returncode, 0)
        self.assertFalse(outside.exists())

    def test_rejects_parent_traversal(self):
        self.prepare()
        traversal = str(self.out) + "/../outside.img"
        self.assertNotEqual(self.run_guard('phase2_output_file "$1"', traversal).returncode, 0)

    def test_rejects_symlinked_output_parent(self):
        self.prepare()
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        (self.out / "nested").symlink_to(elsewhere, target_is_directory=True)
        target = self.out / "nested" / "phase2.img"
        self.assertNotEqual(self.run_guard('phase2_output_file "$1"', target).returncode, 0)
        self.assertFalse((elsewhere / "phase2.img").exists())

    def test_localversion_is_deliberately_empty(self):
        result = self.run_guard('printf "%s:%s" "${LOCALVERSION+set}" "$LOCALVERSION"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "set:")

    def keyring_fixture(self):
        self.prepare()
        extraction = self.out / "downloads" / "debian-keyring-2025.1.fixture"
        keys = extraction / "usr/share/keyrings"
        keys.mkdir(parents=True)
        return extraction, keys

    def copy_keyring(self, extraction):
        # Execute the actual short copy block, not a duplicate implementation.
        # This isolates package-layout handling from root/chroot/download work.
        script = (PHASE / "scripts/build-rootfs.sh").read_text()
        start = script.index('archive_keyring_source=')
        end = script.index('\n\n', start)
        block = script[start:end]
        return self.run_guard('keyring_dir=$1\n' + block, extraction)

    def test_keyring_compatibility_symlink_uses_real_regular_pgp(self):
        extraction, keys = self.keyring_fixture()
        source = keys / "debian-archive-keyring.pgp"
        source.write_bytes(b"verified-package keyring fixture")
        (keys / "debian-archive-keyring.gpg").symlink_to(source.name)
        result = self.copy_keyring(extraction)
        self.assertEqual(result.returncode, 0, result.stderr)
        copy = extraction / "bootstrap-keyring.gpg"
        self.assertFalse(copy.is_symlink())
        self.assertEqual(copy.stat().st_nlink, 1)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o644)
        self.assertEqual(copy.read_bytes(), source.read_bytes())

    def test_real_keyring_symlink_is_still_rejected(self):
        extraction, keys = self.keyring_fixture()
        source = Path(self.temporary.name) / "external-keyring"
        source.write_bytes(b"not the extracted regular keyring")
        (keys / "debian-archive-keyring.pgp").symlink_to(source)
        self.assertNotEqual(self.copy_keyring(extraction).returncode, 0)
        self.assertFalse((extraction / "bootstrap-keyring.gpg").exists())

    def test_bootstrap_keyring_destination_symlink_is_rejected(self):
        extraction, keys = self.keyring_fixture()
        (keys / "debian-archive-keyring.pgp").write_bytes(b"keyring fixture")
        sentinel = Path(self.temporary.name) / "sentinel"
        sentinel.write_bytes(b"do not change")
        (extraction / "bootstrap-keyring.gpg").symlink_to(sentinel)
        self.assertNotEqual(self.copy_keyring(extraction).returncode, 0)
        self.assertEqual(sentinel.read_bytes(), b"do not change")

    def sources_fixture(self):
        self.prepare()
        sources = self.out / "rootfs/etc/apt/sources.list.d"
        sources.mkdir(parents=True)
        return sources

    def clean_bootstrap_sources(self):
        script = (PHASE / "scripts/build-rootfs.sh").read_text()
        start = script.index('bootstrap_sources=')
        end = script.index('\n\n', start)
        return self.run_guard(script[start:end])

    def test_removes_only_known_bootstrap_duplicate_after_canonical_sources(self):
        sources = self.sources_fixture()
        duplicate = sources / "0000rootfs-sources.list"
        duplicate.write_text("bootstrap duplicate")
        other = sources / "other.list"
        other.write_text("unrelated source")
        canonical = sources.parent / "sources.list"
        canonical.write_text("canonical signed snapshot")
        result = self.clean_bootstrap_sources()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(duplicate.exists())
        self.assertEqual(other.read_text(), "unrelated source")
        self.assertEqual(canonical.read_text(), "canonical signed snapshot")
        script = (PHASE / "scripts/build-rootfs.sh").read_text()
        self.assertLess(script.index('> "$ROOTFS_DIR/etc/apt/sources.list"'),
                        script.index('bootstrap_sources='))

    def test_missing_bootstrap_duplicate_needs_no_cleanup(self):
        self.sources_fixture()
        result = self.clean_bootstrap_sources()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bootstrap_source_symlink_is_not_removed_or_followed(self):
        sources = self.sources_fixture()
        sentinel = Path(self.temporary.name) / "sentinel"
        sentinel.write_text("do not change")
        duplicate = sources / "0000rootfs-sources.list"
        duplicate.symlink_to(sentinel)
        self.assertNotEqual(self.clean_bootstrap_sources().returncode, 0)
        self.assertTrue(duplicate.is_symlink())
        self.assertEqual(sentinel.read_text(), "do not change")

    def test_bootstrap_source_symlink_parent_cannot_delete_external_file(self):
        self.prepare()
        apt_dir = self.out / "rootfs/etc/apt"
        apt_dir.mkdir(parents=True)
        external = Path(self.temporary.name) / "external"
        external.mkdir()
        sentinel = external / "0000rootfs-sources.list"
        sentinel.write_text("do not change")
        (apt_dir / "sources.list.d").symlink_to(external, target_is_directory=True)
        self.assertNotEqual(self.clean_bootstrap_sources().returncode, 0)
        self.assertEqual(sentinel.read_text(), "do not change")


class LockedInputsTests(unittest.TestCase):
    def test_local_package_install_disables_repositories_without_no_download(self):
        script = (PHASE / "rootfs/usr/lib/phase2/boot-check.sh").read_text()
        start = script.index("export DEBIAN_FRONTEND=noninteractive")
        end = script.index('[[ "$(dpkg-query', start)
        # Capture actual command arguments and environment without invoking APT.
        command = 'apt-get() { printf "%s\\0" "$DEBIAN_FRONTEND" "$@"; };\n' + script[start:end]
        result = subprocess.run(["bash", "-c", command], capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().split("\0")[:-1], [
            "noninteractive", "--assume-yes", "--no-install-recommends",
            "-o", "Dir::Etc::sourcelist=/dev/null", "-o", "Dir::Etc::sourceparts=-",
            "install", "/opt/phase2/packages/phase2-proof_1.0_all.deb",
        ])
        control = (PHASE / "rootfs/opt/phase2/package-source/DEBIAN/control").read_text()
        self.assertNotRegex(control, r"(?im)^(Pre-Depends|Depends|Recommends):")
        self.assertIn("apt-get --assume-yes --no-download purge phase2-proof", script)
        self.assertIn('[[ -z "$(dpkg --audit)" ]]', script)

    def test_partition_and_filesystem_ids_are_unique_uuids(self):
        values = lock_values()
        identifiers = [values[key] for key in (
            "PHASE2_DISK_GUID", "PHASE2_ESP_PARTUUID", "PHASE2_ROOT_PARTUUID", "PHASE2_ROOT_UUID",
        )]
        self.assertEqual(len(set(identifiers)), 4)
        for identifier in identifiers:
            self.assertEqual(str(uuid.UUID(identifier)), identifier)

    def test_layout_fits_image_and_leaves_room_for_both_gpt_headers(self):
        values = lock_values()
        total = int(values["PHASE2_IMAGE_MIB"]) * 2048
        esp_start = 2048
        esp_size = int(values["PHASE2_ESP_MIB"]) * 2048
        root_start = esp_start + esp_size
        root_size = (int(values["PHASE2_IMAGE_MIB"]) - int(values["PHASE2_ESP_MIB"]) - 2) * 2048
        self.assertGreater(esp_start, 34)
        self.assertGreater(root_size, 0)
        self.assertLess(root_start + root_size, total - 33)
        self.assertEqual(root_start % 2048, 0)

    def test_sources_and_bootstrap_keyring_are_pinned(self):
        values = lock_values()
        self.assertRegex(values["DEBIAN_SNAPSHOT"], r"^\d{8}T\d{6}Z$")
        for field in ("DEBIAN_MIRROR", "DEBIAN_SECURITY_MIRROR"):
            self.assertTrue(values[field].startswith("https://snapshot.debian.org/archive/"))
            self.assertIn("/" + values["DEBIAN_SNAPSHOT"] + "/", values[field])
        self.assertRegex(values["DEBIAN_KEYRING_SHA256"], r"^[0-9a-f]{64}$")
        self.assertTrue(values["DEBIAN_KEYRING_URL"].startswith("https://"))

    def test_boot_and_graphics_requirements_are_builtin(self):
        values = config_values()
        required = (
            "CONFIG_EFI", "CONFIG_EFI_STUB", "CONFIG_EFI_PARTITION", "CONFIG_EXT4_FS",
            "CONFIG_VIRTIO_PCI", "CONFIG_VIRTIO_BLK", "CONFIG_VIRTIO_NET",
            "CONFIG_DRM", "CONFIG_DRM_VIRTIO_GPU", "CONFIG_DRM_VIRTIO_GPU_KMS",
            "CONFIG_VT", "CONFIG_VT_CONSOLE", "CONFIG_INPUT_EVDEV", "CONFIG_VIRTIO_INPUT",
            "CONFIG_FW_CFG_SYSFS", "CONFIG_CGROUPS", "CONFIG_INOTIFY_USER", "CONFIG_FHANDLE",
        )
        for symbol in required:
            self.assertEqual(values[symbol], "y", symbol)
        self.assertEqual(values["CONFIG_MODULES"], "n")
        self.assertEqual(values["CONFIG_DRM_SIMPLEDRM"], "n")
        self.assertEqual(values["CONFIG_DRM_EFIDRM"], "n")
        self.assertEqual(values["CONFIG_DRM_I915"], "n")
        self.assertEqual(values["CONFIG_LOCALVERSION"], '"-phase2"')


    def test_compressed_distributable_is_compared_with_tested_raw_image(self):
        script = (PHASE / "scripts/build-image.sh").read_text()
        conversion = script.index('qemu-img convert -f raw -O qcow2 -c "$disk_image"')
        structural_check = script.index('qemu-img check -f qcow2 "$OUTPUT_DIR/phase2.qcow2"')
        byte_comparison = script.index(
            'qemu-img compare -f raw -F qcow2 "$disk_image" "$OUTPUT_DIR/phase2.qcow2"'
        )
        self.assertLess(conversion, structural_check)
        self.assertLess(structural_check, byte_comparison)


class ArtifactCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="phase2-artifact-test-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.phase = self.repo / "distro" / "phase2"
        self.scripts = self.phase / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("common.sh", "artifact.sh"):
            shutil.copyfile(PHASE / "scripts" / name, self.scripts / name)
        for name in ("inputs.lock", "kernel.config.fragment"):
            shutil.copyfile(PHASE / name, self.phase / name)
        self.out = self.repo / ".build" / "phase2"
        self.out.mkdir(parents=True)
        self.artifacts = self.out / "artifacts"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=Phase2Test",
             "-c", "user.email=phase2-test@example.invalid", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-q", "-m", "fixture"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def collect(self):
        env = os.environ.copy()
        env.update(SOURCE_DATE_EPOCH="1", JOBS="2")
        return subprocess.run(["bash", str(self.scripts / "artifact.sh")],
                              text=True, capture_output=True, env=env, check=False, timeout=20)

    def test_missing_build_still_has_diagnostics_and_checksums(self):
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        verdict = json.loads((self.artifacts / "test-results.json").read_text())
        self.assertEqual(verdict["status"], "not_validated")
        subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"],
                       cwd=self.artifacts, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_previous_upload_cannot_leave_a_stale_pass(self):
        self.artifacts.mkdir()
        original = '{"status":"pass","stale":true}'
        (self.artifacts / "test-results.json").write_text(original)
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        verdict = json.loads((self.artifacts / "test-results.json").read_text())
        self.assertEqual(verdict["status"], "not_validated")
        preserved = list(self.out.glob("artifacts.previous.*/artifacts/test-results.json"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(), original)

    def test_rejects_existing_symlink_artifact_directory(self):
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        self.artifacts.symlink_to(elsewhere, target_is_directory=True)
        self.assertNotEqual(self.collect().returncode, 0)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_skips_symlink_and_hardlinked_source_files(self):
        sentinel = Path(self.temporary.name) / "sentinel"
        sentinel.write_bytes(b"private fixture bytes")
        (self.out / "phase2.qcow2").symlink_to(sentinel)
        os.link(sentinel, self.out / "ovmf-code.fd")
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.artifacts / "phase2.qcow2").exists())
        self.assertFalse((self.artifacts / "ovmf-code.fd").exists())
        self.assertEqual(sentinel.read_bytes(), b"private fixture bytes")

    def test_skips_symlinked_log_ancestor(self):
        # prepare_output correctly refuses a redirected logs directory, before
        # collection gets a chance to read or copy any of its contents.
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "private.log").write_text("private fixture bytes")
        (self.out / "logs").symlink_to(elsewhere, target_is_directory=True)
        self.assertNotEqual(self.collect().returncode, 0)
        self.assertFalse(self.artifacts.exists())

    def test_failed_verifier_cannot_publish_a_pass(self):
        original = '{"status":"pass","cases":[]}'
        (self.out / "test-results.json").write_text(original)
        (self.out / "manifest.json").write_text("{}")
        (self.scripts / "manifest.py").write_text("raise SystemExit(1)\n")
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        verdict = json.loads((self.artifacts / "test-results.json").read_text())
        self.assertEqual(verdict["status"], "not_validated")
        self.assertEqual((self.artifacts / "diagnostics/test-results.unverified.json").read_text(), original)

    def test_confirmed_verifier_copies_exact_report(self):
        original = '{"status":"pass","cases":[]}'
        (self.out / "test-results.json").write_text(original)
        (self.out / "manifest.json").write_text("{}")
        (self.scripts / "manifest.py").write_text("raise SystemExit(0)\n")
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.artifacts / "test-results.json").read_text(), original)

    def test_collects_compressed_image_not_raw_components(self):
        for name in ("phase2.qcow2", "phase2.img", "esp.img", "rootfs.img"):
            (self.out / name).write_bytes(b"image fixture")
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.artifacts / "phase2.qcow2").is_file())
        for name in ("phase2.img", "esp.img", "rootfs.img"):
            self.assertFalse((self.artifacts / name).exists())


if __name__ == "__main__":
    unittest.main()
