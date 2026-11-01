#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Host regression for Sway's dynamically selected Wayland display.

Execute the supervisor's *actual* log-parser assignment and display-name
guard against synthetic Sway logs. These are parser fixtures, not claimed
QEMU/DRM acceptance: the smoke test remains the authoritative integration
oracle.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import unittest


SESSION = Path(__file__).resolve().parents[1] / "compositor" / "start-session.sh"


class SessionWaylandDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SESSION.read_text(encoding="utf-8")
        # Extract the actual Bash command substitution, not a copied parser
        # in test code; changes to the production parser cannot silently pass.
        assignment = re.search(
            r'(?m)^[ \t]*display_name=\$\(sed -nE "[^"\n]+" "\$sway_log" \| tail -n 1\)$',
            cls.source,
        )
        if assignment is None:
            raise AssertionError("production Sway log parser is missing/changed")
        cls.expression = re.search(r'\$\(sed -nE "([^"\n]+)"', assignment.group(0)).group(1)
        guard = re.search(
            r'(?m)^\s*if (\[\[ "\$display_name" =~ [^\n]+\]\]); then$',
            cls.source,
        )
        if guard is None:
            raise AssertionError("production Wayland-display validation is missing")
        cls.guard = guard.group(1)

    def discover(self, log: str) -> str:
        # Apply the production sed expression to input bytes via stdin, then
        # apply the production Bash whitelist in a subprocess. No test fixture
        # filesystem or test-only parser implementation is involved.
        parsed = subprocess.run(
            ["sed", "-nE", self.expression], input=log,
            capture_output=True, text=True, check=True, timeout=5,
        )
        display_name = parsed.stdout.splitlines()[-1] if parsed.stdout else ""
        validated = subprocess.run(
            ["bash", "-c", self.guard],
            env={**os.environ, "display_name": display_name},
            capture_output=True, text=True, timeout=5,
        )
        return ("accepted:" if validated.returncode == 0 else "rejected:") + display_name

    def test_sway_can_choose_0_1_or_any_number(self) -> None:
        for display in ("wayland-0", "wayland-1", "wayland-42"):
            with self.subTest(display=display):
                log = ("00:00:01.424 [INFO] [sway/server.c:499] "
                       "Running compositor on wayland display '" + display + "'\n")
                self.assertEqual(self.discover(log), "accepted:" + display)

    def test_last_compositor_ready_line_wins(self) -> None:
        log = ("00:00:00.100 [INFO] Running compositor on wayland display 'wayland-0'\n"
               "00:00:00.700 [INFO] unrelated notice\n"
               "00:00:01.424 [INFO] Running compositor on wayland display 'wayland-1'\n")
        self.assertEqual(self.discover(log), "accepted:wayland-1")

    def test_malformed_and_unrelated_log_lines_rejected(self) -> None:
        samples = (
            "",
            "Sway socket is wayland-1\n",
            "Running compositor on wayland display '../wayland-1'\n",
            "Running compositor on wayland display 'wayland-1/other'\n",
            "Running compositor on wayland display 'wayland-X'\n",
            "Running compositor on wayland display 'wayland-1;touch /tmp/incorrect'\n",
        )
        for log in samples:
            with self.subTest(log=log):
                self.assertTrue(self.discover(log).startswith("rejected:"))

    def test_supervisor_checks_pid_bound_ipc_and_passes_detected_display(self) -> None:
        self.assertIn('sway-ipc.1000.$sway_pid.sock', self.source)
        self.assertIn('SWAYSOCK="$sway_socket" timeout 3 /usr/bin/swaymsg -t get_tree -r',
                      self.source)
        self.assertIn('WAYLAND_DISPLAY="$display_name" timeout 3 /usr/bin/wayland-info',
                      self.source)
        self.assertIn('export SWAYSOCK="$sway_socket" WAYLAND_DISPLAY="$display_name"',
                      self.source)
        self.assertNotRegex(self.source, r'\bWAYLAND_DISPLAY=wayland-0\b')
        self.assertNotRegex(self.source, r'/run/user/1000/wayland-0\b')


if __name__ == "__main__":
    unittest.main()
