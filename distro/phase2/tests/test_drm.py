#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute the guest detector against isolated models of Linux's DRM topology."""
from pathlib import Path
import subprocess
import tempfile
import unittest


PHASE = Path(__file__).resolve().parent.parent
HELPER = PHASE / "rootfs/usr/lib/phase2/drm-check.sh"


class VirtioDRMTests(unittest.TestCase):
    def test_boot_check_sources_helper_and_gates_success_on_detection(self):
        boot_check = (PHASE / "rootfs/usr/lib/phase2/boot-check.sh").read_text()
        self.assertIn(
            "reason=DRM_CHECK\n"
            "# shellcheck source=../rootfs/usr/lib/phase2/drm-check.sh\n"
            "source /usr/lib/phase2/drm-check.sh\n"
            "reason=NO_DRM\n"
            "phase2_find_virtio_drm /sys/class/drm /dev/dri\n"
            "emit DRM_OK\n",
            boot_check,
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="phase2-drm-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cards = self.root / "sys/class/drm"
        self.nodes = self.root / "dev/dri"
        self.cards.mkdir(parents=True)
        self.nodes.mkdir(parents=True)
        self.pci = self.root / "sys/devices/pci0000:00/0000:00:05.0"
        self.pci.mkdir(parents=True)
        transport = self.root / "sys/bus/pci/drivers/virtio-pci"
        transport.mkdir(parents=True)
        (self.pci / "driver").symlink_to(transport, target_is_directory=True)

    def card(self, name="card0", character_node=True):
        entry = self.pci / "drm" / name
        entry.mkdir(parents=True)
        (entry / "device").symlink_to(self.pci, target_is_directory=True)
        (self.cards / name).symlink_to(entry, target_is_directory=True)
        if character_node:
            # An existing harmless character node models the -c predicate
            # without privileged mknod. No fixture pretends to exercise DRM I/O.
            (self.nodes / name).symlink_to("/dev/null")
        return entry

    def child(self, driver_name="virtio_gpu", parent=None, child_name="virtio4"):
        parent = self.pci if parent is None else parent
        child = parent / child_name
        child.mkdir(parents=True)
        driver = self.root / "sys/bus/virtio/drivers" / driver_name
        driver.mkdir(parents=True, exist_ok=True)
        (child / "driver").symlink_to(driver, target_is_directory=True)
        return child

    def detect(self):
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c",
             'source "$1"; phase2_find_virtio_drm "$2" "$3"', "phase2-drm-test",
             str(HELPER), str(self.cards), str(self.nodes)],
            capture_output=True, text=True, check=False, timeout=5,
        )

    def test_pci_transport_parent_with_virtio_gpu_child_passes(self):
        self.card()
        self.child()
        self.assertEqual((self.pci / "driver").resolve().name, "virtio-pci")
        result = self.detect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("card=card0", result.stdout)
        self.assertIn(f"device={self.pci}", result.stdout)
        self.assertIn("child=virtio4 driver=virtio_gpu", result.stdout)
        self.assertIn("selected character device", result.stdout)

    def test_no_card_is_rejected_even_with_a_virtio_gpu_device(self):
        self.child()
        self.assertNotEqual(self.detect().returncode, 0)

    def test_missing_character_node_is_rejected(self):
        self.card(character_node=False)
        self.child()
        self.assertNotEqual(self.detect().returncode, 0)

    def test_regular_file_cannot_substitute_for_character_node(self):
        self.card(character_node=False)
        self.child()
        (self.nodes / "card0").write_text("not a character device")
        self.assertNotEqual(self.detect().returncode, 0)

    def test_pci_transport_without_gpu_child_is_rejected(self):
        self.card()
        self.assertNotEqual(self.detect().returncode, 0)

    def test_wrong_associated_driver_is_rejected(self):
        self.card()
        self.child("virtio_net")
        result = self.detect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("driver=virtio_net", result.stdout)

    def test_unrelated_gpu_cannot_satisfy_card(self):
        self.card()
        self.child("virtio_net")
        self.child(parent=self.root / "sys/devices/unrelated")
        self.assertNotEqual(self.detect().returncode, 0)

    def test_symlink_to_unrelated_gpu_is_not_a_real_child(self):
        self.card()
        unrelated = self.child(parent=self.root / "sys/devices/unrelated")
        (self.pci / "virtio4").symlink_to(unrelated, target_is_directory=True)
        self.assertNotEqual(self.detect().returncode, 0)

    def test_connector_cannot_substitute_for_primary_card(self):
        self.card("card0-Virtual-1")
        self.child()
        self.assertNotEqual(self.detect().returncode, 0)

    def test_render_node_cannot_substitute_for_primary_card(self):
        self.card("renderD128")
        self.child()
        self.assertNotEqual(self.detect().returncode, 0)

    def test_non_device_child_name_is_rejected(self):
        self.card()
        self.child(child_name="virtio4-extra")
        self.assertNotEqual(self.detect().returncode, 0)

    def test_missing_driver_target_is_rejected(self):
        self.card()
        child = self.pci / "virtio4"
        child.mkdir()
        (child / "driver").symlink_to(self.root / "missing-driver")
        self.assertNotEqual(self.detect().returncode, 0)


if __name__ == "__main__":
    unittest.main()
