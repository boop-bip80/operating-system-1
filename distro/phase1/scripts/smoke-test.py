#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Exercise the Phase 1 image through real QEMU boots; fail closed on weak evidence."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass


MAX_LOG_BYTES = 32 * 1024 * 1024
SOURCE_RE = re.compile(r"[0-9a-f]{40}(?:-dirty)?\Z")
KERNEL_FAILURE_RE = re.compile(
    r"Kernel panic|\bOops:|\bBUG:|(?:^|\n)P1:FAIL:|"
    r"No working init found|EXT4-fs error|EXT4-fs.*(?:corrupt|abort)",
    re.IGNORECASE,
)
POWER_DOWN_RE = re.compile(
    r"(?m)^(?:\[[\d .]+\]\s*)?reboot:\s*Power down\.?\s*$"
)
EXPECTED_NEGATIVE_RE = re.compile(
    r"No working init found|Requested init .* failed|"
    r"VFS: Unable to mount root fs|Failed to execute /init",
    re.IGNORECASE,
)
ROUND_MARKERS = {
    1: (
        "P1:INIT_OK",
        "P1:FS_OK",
        "P1:DISK_WRITE_OK",
        "P1:NET_OK",
        "P1:ROUND1_OK",
        "P1:POWER_OFF",
    ),
    2: (
        "P1:INIT_OK",
        "P1:FS_OK",
        "P1:DISK_PERSIST_OK",
        "P1:NET_OK",
        "P1:PASS",
        "P1:POWER_OFF",
    ),
}


class SmokeFailure(RuntimeError):
    """An acceptance criterion lacks reliable evidence."""


@dataclass
class BootResult:
    name: str
    output: str
    returncode: int
    duration_seconds: float = 0.0
    timed_out: bool = False
    output_limit: bool = False
    interaction_sent: bool = False


def manifest_digest(path: Path) -> str:
    """Bind boot evidence to these exact build artifacts, not only the source SHA."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_manifest_unchanged(path: Path, expected_digest: str) -> None:
    if manifest_digest(path) != expected_digest:
        raise SmokeFailure("AC-01: build manifest changed during the boot suite; rebuild and retest")


def serial_lines(output: str) -> list[str]:
    """Keep markers exact: don't accept prompts, echoed commands, or substrings."""
    return output.replace("\r\n", "\n").replace("\r", "\n").splitlines()


def require_once(lines: list[str], marker: str, criterion: str) -> int:
    matches = [index for index, line in enumerate(lines) if line == marker]
    if len(matches) != 1:
        raise SmokeFailure(
            f"{criterion}: expected exactly one complete {marker!r} line, got {len(matches)}"
        )
    return matches[0]


def validate_identity(
    lines: list[str], expected_release: str, expected_source: str
) -> tuple[int, int]:
    kernel_lines = [line for line in lines if line.startswith("P1:KERNEL:")]
    source_lines = [line for line in lines if line.startswith("P1:SOURCE:")]
    if kernel_lines != [f"P1:KERNEL:{expected_release}"]:
        raise SmokeFailure(f"AC-02: unexpected guest kernel identity: {kernel_lines!r}")
    if source_lines != [f"P1:SOURCE:{expected_source}"]:
        raise SmokeFailure(f"AC-01: unexpected guest source identity: {source_lines!r}")
    return (
        lines.index(f"P1:KERNEL:{expected_release}"),
        lines.index(f"P1:SOURCE:{expected_source}"),
    )


def validate_clean_exit(result: BootResult) -> None:
    if result.timed_out:
        raise SmokeFailure("AC-05/AC-10: guest exceeded its boot deadline")
    if result.output_limit:
        raise SmokeFailure("AC-05: guest serial output exceeded the safety limit")
    if result.returncode != 0:
        raise SmokeFailure(f"AC-10: QEMU exited with status {result.returncode}")
    if KERNEL_FAILURE_RE.search(result.output):
        raise SmokeFailure("AC-05/AC-10: kernel or guest failure appeared in serial output")
    normalized = "\n".join(serial_lines(result.output))
    if not POWER_DOWN_RE.search(normalized):
        raise SmokeFailure("AC-10: missing kernel power-down evidence")


def validate_test_boot(
    result: BootResult, round_number: int, expected_release: str, expected_source: str
) -> None:
    validate_clean_exit(result)
    lines = serial_lines(result.output)
    identity = validate_identity(lines, expected_release, expected_source)
    positions = [
        require_once(lines, marker, "AC-05/AC-07/AC-08/AC-09")
        for marker in ROUND_MARKERS[round_number]
    ]
    if positions != sorted(positions):
        raise SmokeFailure("AC-05/AC-08: required serial markers are out of order")
    if any(index > positions[-2] for index in identity):
        raise SmokeFailure("AC-01/AC-02: identity was not established before test completion")
    other_success = "P1:PASS" if round_number == 1 else "P1:ROUND1_OK"
    if other_success in lines:
        raise SmokeFailure("AC-08: guest reported success for the wrong persistence round")
    # The poweroff marker cannot be used to hide a log where the guest had
    # already powered down before the final assertions ran.
    power_down_position = next(
        (index for index, line in enumerate(lines) if POWER_DOWN_RE.search(line)), -1
    )
    if power_down_position <= positions[-1]:
        raise SmokeFailure("AC-10: kernel power-down preceded the guest shutdown marker")


def validate_interactive_boot(
    result: BootResult, expected_release: str, expected_source: str, nonce: str
) -> None:
    validate_clean_exit(result)
    if not result.interaction_sent:
        raise SmokeFailure("AC-06: host never sent commands to the non-test shell")
    lines = serial_lines(result.output)
    validate_identity(lines, expected_release, expected_source)
    positions = [
        require_once(lines, "P1:INIT_OK", "AC-06"),
        require_once(lines, "P1:SHELL_READY", "AC-06"),
        require_once(lines, f"P1:INTERACTIVE_KERNEL:{expected_release}", "AC-06"),
        require_once(lines, f"P1:INTERACTIVE_OK:{nonce}", "AC-06"),
    ]
    if positions != sorted(positions):
        raise SmokeFailure("AC-06: interactive shell evidence is out of order")
    if any(line in lines for line in ("P1:PASS", "P1:ROUND1_OK")):
        raise SmokeFailure("AC-06: non-test shell boot unexpectedly ran automated guest tests")


def validate_negative_boot(result: BootResult, expected_release: str) -> None:
    """A host launch error or arbitrary crash must never satisfy the negative control."""
    if result.timed_out or result.output_limit:
        raise SmokeFailure("AC-11: negative guest did not terminate within its safety limits")
    if result.returncode != 0:
        raise SmokeFailure("AC-11: negative control had a host/QEMU process failure")
    if not re.search(r"Linux version " + re.escape(expected_release) + r"\s", result.output):
        raise SmokeFailure("AC-11: no evidence that the expected kernel actually booted")
    if "Kernel panic" not in result.output or not EXPECTED_NEGATIVE_RE.search(result.output):
        raise SmokeFailure("AC-11: missing expected no-init/root-filesystem guest panic")
    if any(line.startswith("P1:") for line in serial_lines(result.output)):
        raise SmokeFailure("AC-11: invalid initramfs unexpectedly reached Phase 1 userspace")


def interactive_commands(nonce: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise ValueError("interactive nonce must be 32 lowercase hex characters")
    # Marker and value are separate printf arguments so the echoed command can
    # never be mistaken for the full, exact output marker, even without stty.
    commands = (
        "stty -echo\n"
        "printf '\\n%s%s\\n' 'P1:INTERACTIVE_KERNEL:' \"$(uname -r)\"; "
        f"printf '%s\\n' '{nonce}' > /tmp/phase1-interactive; "
        f"if [ \"$(cat /tmp/phase1-interactive)\" = '{nonce}' ]; then "
        f"printf '\\n%s%s\\n' 'P1:INTERACTIVE_OK:' '{nonce}'; "
        "else printf '\\n%s%s\\n' 'P1:' 'INTERACTIVE_FAIL'; fi; "
        "sync; poweroff -f\n"
    )
    return commands.encode("ascii")


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def run_boot(
    name: str,
    command: list[str],
    log_path: Path,
    timeout_seconds: float,
    interactive_nonce: str | None = None,
) -> BootResult:
    started = time.monotonic()
    output = bytearray()
    timed_out = False
    output_limit = False
    interaction_sent = False
    if log_path.is_symlink():
        raise SmokeFailure(f"refusing symlink log path: {log_path}")
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if interactive_nonce else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdout is not None
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        timed_out = True
                        terminate_process(process)
                        break
                    events = selector.select(timeout=min(0.25, remaining))
                    for key, _ in events:
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        log.write(data)
                        log.flush()
                        output.extend(data)
                        if len(output) > MAX_LOG_BYTES:
                            output_limit = True
                            terminate_process(process)
                            break
                        if interactive_nonce and not interaction_sent:
                            decoded = output.decode("utf-8", errors="replace")
                            if "P1:SHELL_READY" in serial_lines(decoded):
                                assert process.stdin is not None
                                process.stdin.write(interactive_commands(interactive_nonce))
                                process.stdin.flush()
                                interaction_sent = True
                    if output_limit:
                        break
            if process.poll() is None:
                remaining = timeout_seconds - (time.monotonic() - started)
                try:
                    process.wait(timeout=max(0.01, remaining))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_process(process)
        finally:
            terminate_process(process)
            process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()
    assert process.returncode is not None
    return BootResult(
        name=name,
        output=output.decode("utf-8", errors="replace"),
        returncode=process.returncode,
        duration_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        output_limit=output_limit,
        interaction_sent=interaction_sent,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.timeout > 180:
        parser.error("--timeout must be greater than zero and no more than 180 seconds")
    output_dir = args.output.resolve(strict=True)
    phase_dir = args.phase_dir.resolve(strict=True)
    log_dir = output_dir / "logs"
    result_path = output_dir / "test-results.json"
    if log_dir.is_symlink() or result_path.is_symlink():
        parser.error("refusing symlink log/result output")
    log_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "failed",
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cases": [],
        "scope": "one four-boot smoke suite; two separate clean CI runs remain a release gate",
    }
    cases: list[dict[str, object]] = []
    report["cases"] = cases
    try:
        required = (
            output_dir / "kernel/arch/x86/boot/bzImage",
            output_dir / "initramfs.cpio.gz",
            output_dir / "broken-initramfs.cpio.gz",
            output_dir / "kernelrelease",
            output_dir / "source-revision",
            output_dir / "manifest.json",
        )
        for path in required:
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise SmokeFailure(f"AC-01/AC-02/AC-04: missing or unsafe build artifact: {path}")
        manifest_path = output_dir / "manifest.json"
        initial_manifest_digest = manifest_digest(manifest_path)
        report["manifest_sha256"] = initial_manifest_digest
        expected_release = (output_dir / "kernelrelease").read_text().strip()
        expected_source = (output_dir / "source-revision").read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9._+\-]+", expected_release):
            raise SmokeFailure("AC-02: invalid expected kernelrelease")
        if not SOURCE_RE.fullmatch(expected_source):
            raise SmokeFailure("AC-01: source-revision is not a SHA with optional -dirty suffix")
        source_head = subprocess.check_output(
            ["git", "-C", str(phase_dir.parent.parent), "rev-parse", "HEAD"], text=True
        ).strip()
        if expected_source.removesuffix("-dirty") != source_head:
            raise SmokeFailure("AC-01: artifacts belong to a different source commit; rebuild")
        nonce = secrets.token_hex(16)
        report.update(
            source_revision=expected_source, kernel_release=expected_release, nonce=nonce
        )
        subprocess.run(["bash", str(phase_dir / "scripts/make-test-disk.sh"), "test"], check=True)
        disk = output_dir / "test-data.ext4"
        if disk.is_symlink() or not disk.is_file():
            raise SmokeFailure("AC-08: missing or unsafe disposable test disk")
        first_round_ok = False
        for name, mode, round_number in (
            ("round1", "test", 1),
            ("round2", "test", 2),
            ("interactive", "interactive", 1),
            ("negative", "negative", 1),
        ):
            case: dict[str, object] = {"name": name, "status": "failed"}
            cases.append(case)
            if name == "round2" and not first_round_ok:
                case.update(status="failed", error="AC-08: round 2 skipped because round 1 did not pass")
                continue
            command = [
                "bash", str(phase_dir / "scripts/boot-qemu.sh"),
                "--mode", mode, "--round", str(round_number),
                "--nonce", nonce, "--disk", str(disk),
            ]
            log_path = log_dir / f"boot-{name}.log"
            case.update(command=command, serial_log=str(log_path.relative_to(output_dir)))
            print(f"Phase 1: booting {name} (deadline {args.timeout:g}s)", flush=True)
            result: BootResult | None = None
            try:
                result = run_boot(
                    name, command, log_path, args.timeout,
                    nonce if mode == "interactive" else None,
                )
                case.update(
                    returncode=result.returncode,
                    duration_seconds=result.duration_seconds,
                    timed_out=result.timed_out,
                    output_limit=result.output_limit,
                    interaction_sent=result.interaction_sent,
                )
                if mode == "test":
                    validate_test_boot(result, round_number, expected_release, expected_source)
                    if round_number == 1:
                        first_round_ok = True
                elif mode == "interactive":
                    validate_interactive_boot(result, expected_release, expected_source, nonce)
                else:
                    validate_negative_boot(result, expected_release)
                case["status"] = "passed"
                print(f"Phase 1: {name} passed", flush=True)
            except (SmokeFailure, OSError, subprocess.SubprocessError) as exc:
                case["error"] = str(exc)
                print(f"Phase 1: {name} FAILED: {exc}", file=sys.stderr, flush=True)
                if result is not None:
                    print("\n".join(serial_lines(result.output)[-45:]), file=sys.stderr)
        if all(case["status"] == "passed" for case in cases) and len(cases) == 4:
            validate_manifest_unchanged(manifest_path, initial_manifest_digest)
            report["status"] = "passed"
    except (SmokeFailure, OSError, ValueError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        print(f"Phase 1: preflight FAILED: {exc}", file=sys.stderr)
    finally:
        report["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result_path.write_text(json.dumps(report, indent=2) + "\n")
    if report["status"] != "passed":
        return 1
    print("Phase 1: all four boot cases passed; evidence saved in test-results.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
