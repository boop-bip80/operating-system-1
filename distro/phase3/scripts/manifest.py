#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Phase 3 provenance/acceptance: bind the built disk and actual QMP evidence.

`create` invalidates any previous test result. `verify` is deliberately strict:
incomplete artifacts, an older SHA, a textual marker without working QMP
input/frames, or a failed guest must all return nonzero.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

PHASE = Path(__file__).resolve().parents[1]
ROOT = PHASE.parents[1]
OUT = ROOT / ".build" / "phase3"
FILES = {
    "kernel": "kernel/arch/x86/boot/bzImage",
    "disk": "phase3.img", "distribution_disk": "phase3.qcow2",
    "firmware_code": "ovmf-code.fd", "firmware_vars": "ovmf-vars.fd",
    "kernel_config": "kernel.config", "kernel_release": "kernelrelease",
    "source_revision": "source-revision", "source_fingerprint": "source-fingerprint",
    "inputs_lock": "inputs.lock", "userspace_packages": "rootfs-packages.tsv",
    "userspace_sources": "rootfs-sources.list", "signed_release_checksums": "apt-release-metadata.sha256",
    "partition_layout": "partition-layout.sfdisk", "partition_table": "partition-table.json",
    "grub_config": "grub.cfg", "efi_bootloader": "BOOTX64.EFI",
    "image_components": "image-components.sha256",
}
TOOLS = {
    "gcc": ("gcc", "--version"), "binutils": ("ld", "--version"),
    "make": ("make", "--version"), "python": ("python3", "--version"),
    "qemu": ("qemu-system-x86_64", "--version"),
    "qemu_img": ("qemu-img", "--version"), "dpkg": ("dpkg", "--version"),
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def command(arguments: list[str]) -> str:
    if arguments[0] == "git":
        arguments = ["git", "-c", f"safe.directory={ROOT}", *arguments[1:]]
    return subprocess.check_output(arguments, cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def source_fingerprint() -> str:
    result = hashlib.sha256()
    result.update(command(["git", "rev-parse", "HEAD"]).encode())
    git = ["git", "-c", f"safe.directory={ROOT}"]
    result.update(subprocess.check_output([*git, "diff", "--binary", "--full-index", "HEAD"], cwd=ROOT))
    names = subprocess.check_output([*git, "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    for name in sorted(part for part in names.split(b"\0") if part):
        path = ROOT / os.fsdecode(name)
        metadata = path.lstat()
        result.update(b"\0" + name + b"\0" + str(metadata.st_mode).encode() + b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            result.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(metadata.st_mode):
            result.update(digest(path).encode())
        else:
            raise ValueError(f"unsupported untracked source: {path}")
    return result.hexdigest()


def safe_file(relative: str, *, missing: bool = False) -> Path:
    require(isinstance(relative, str), "artifact path must be a string")
    name = Path(relative)
    require(not name.is_absolute() and ".." not in name.parts and bool(name.parts), "unsafe artifact path")
    require(OUT.resolve() == OUT and not OUT.is_symlink(), "unsafe artifact root")
    path = OUT / name
    require(path.parent.resolve() == path.parent, "artifact parent traverses symlink")
    if missing and not path.exists() and not path.is_symlink():
        return path
    entry = path.lstat()
    require(stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1, f"unsafe/non-regular artifact {relative}")
    return path


def record(relative: str) -> dict:
    path = safe_file(relative)
    return {"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)}


def check_record(item: dict, relative: str) -> Path:
    require(isinstance(item, dict) and item.get("path") == relative, "artifact path identity mismatch")
    path = safe_file(relative)
    require(path.stat().st_size == item.get("bytes") and digest(path) == item.get("sha256"),
            f"artifact checksum mismatch: {relative}")
    return path


def write_json(relative: str, payload: dict) -> None:
    safe_file(relative, missing=True).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def ensure_source() -> tuple[str, bool, str]:
    sha = command(["git", "rev-parse", "HEAD"])
    require(bool(re.fullmatch(r"[0-9a-f]{40}", sha)), "invalid checkout SHA")
    dirty = bool(command(["git", "status", "--porcelain", "--untracked-files=normal"]))
    require(os.environ.get("CI") != "true" or not dirty, "dirty CI checkout")
    return sha, dirty, sha + ("-dirty" if dirty else "")


def verify_inputs_lock() -> None:
    """Validate the root-staged immutable input without changing ownership or bytes."""
    require(safe_file("inputs.lock").read_bytes() == (PHASE / "inputs.lock").read_bytes(),
            "staged Phase3 dependency lock differs from checked-out source")


def create() -> None:
    sha, dirty, revision = ensure_source()
    fingerprint = source_fingerprint()
    require(safe_file("source-fingerprint").read_text().strip() == fingerprint,
            "source changed since kernel build; rebuild phase3")
    require(safe_file("source-revision").read_text().strip() == revision,
            "image source revision differs from checkout")
    # Phase 3's privileged rootfs staging already copied this exact lock into
    # the output tree. It is root-owned by design: acceptance (run unprivileged)
    # must verify immutable bytes, never attempt to overwrite them.
    verify_inputs_lock()
    outputs = {key: record(path) for key, path in FILES.items()}
    packages = safe_file("rootfs-packages.tsv").read_text()
    for required in ("sway\t", "foot\t", "python3-gi\t"):
        require(required in packages, f"Phase3 userspace is missing required package: {required.rstrip()}")
    host_packages = command(["dpkg-query", "-W", "-f=${Package}\t${Version}\n"])
    safe_file("logs/host-packages.log", missing=True).write_text(host_packages + "\n")
    ci_fields = ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID",
                 "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion")
    payload = {
        "schema_version": 1, "phase": 3,
        "scope": "Sway DRM/Pixman shell; real GtkLayerShell Dock, foot HID, persistence and no-GPU control",
        "source_sha": sha, "source_revision": revision, "source_dirty": dirty,
        "source_fingerprint": fingerprint,
        "source_tree": command(["git", "rev-parse", "HEAD^{tree}"]),
        "source_url": f"https://github.com/boop-bip80/operating-system-1/tree/{sha}",
        "kernel_release": safe_file("kernelrelease").read_text().strip(),
        "source_date_epoch": int(os.environ.get("SOURCE_DATE_EPOCH") or
                                 command(["git", "show", "-s", "--format=%ct", "HEAD"])),
        "architecture": "x86_64", "tools": {name: command(list(args)).splitlines()[0]
                                             for name, args in TOOLS.items()},
        "ci": {name: os.environ.get(name, "") for name in ci_fields},
        "host_packages": record("logs/host-packages.log"),
        "outputs": outputs,
        "reproducibility": "Signed, dated Debian snapshot and kernel source; host versions recorded, not byte-identical build",
    }
    write_json("manifest.json", payload)
    write_json("test-results.json", {"schema_version": 1, "status": "not_run", "cases": [],
                                     "reason": "New build manifest; QMP acceptance has not run."})
    all_records = list(outputs.values()) + [payload["host_packages"]]
    safe_file("SHA256SUMS", missing=True).write_text("".join(
        f"{entry['sha256']}  {entry['path']}\n" for entry in all_records))
    print(f"phase3: manifest generated for {revision}")


def check_build() -> dict:
    manifest = json.loads(safe_file("manifest.json").read_text())
    sha, dirty, revision = ensure_source()
    require(manifest.get("schema_version") == 1 and manifest.get("phase") == 3,
            "unknown Phase3 build manifest schema")
    require(manifest.get("source_sha") == sha and manifest.get("source_revision") == revision,
            "manifest source SHA does not match checkout")
    require(manifest.get("source_dirty") == dirty and
            manifest.get("source_fingerprint") == source_fingerprint(),
            "source tree changed after Phase3 build")
    require(safe_file("source-fingerprint").read_text().strip() == manifest["source_fingerprint"],
            "build source fingerprint mismatch")
    require(set(manifest.get("outputs", {})) == set(FILES), "incorrect Phase3 output set")
    for key, relative in FILES.items():
        check_record(manifest["outputs"][key], relative)
    verify_inputs_lock()
    check_record(manifest["host_packages"], "logs/host-packages.log")
    required_lines = {f"{entry['sha256']}  {entry['path']}"
                      for entry in list(manifest["outputs"].values()) + [manifest["host_packages"]]}
    actual_lines = set(safe_file("SHA256SUMS").read_text().splitlines())
    require(actual_lines == required_lines, "distribution checksums have changed")
    return manifest


def load_smoke():
    path = PHASE / "scripts" / "smoke-test.py"
    spec = importlib.util.spec_from_file_location("phase3_acceptance", path)
    require(spec is not None and spec.loader is not None, "Phase3 smoke module is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify() -> None:
    manifest = check_build()
    smoke = load_smoke()
    report = json.loads(safe_file("test-results.json").read_text())
    require(report.get("schema_version") == 1 and report.get("status") == "pass",
            "graphical acceptance was not completed successfully")
    require(report.get("source_revision") == manifest["source_revision"] and
            report.get("kernel_release") == manifest["kernel_release"],
            "test-results were generated for another source or kernel")
    require(report.get("manifest_sha256") == digest(safe_file("manifest.json")),
            "test-results are not bound to the exact immutable build manifest")
    cases = report.get("cases")
    require(isinstance(cases, list) and [c.get("name") for c in cases] ==
            ["round1", "round2", "negative"], "missing or reordered graphical scenarios")
    first_nonce = None
    for case in cases:
        name = case["name"]
        require(case.get("status") == "pass", f"{name} did not pass")
        for field, relative in (("serial_log", f"logs/boot-{name}.log"),
                                ("qmp_log", f"logs/qmp-{name}.jsonl")):
            require(case.get(field) == relative, f"{name} has invalid evidence path")
        serial = safe_file(case["serial_log"])
        qmp = safe_file(case["qmp_log"])
        require(case.get("serial_log_sha256") == digest(serial) and
                case.get("qmp_log_sha256") == digest(qmp), f"{name} evidence hashes differ")
        result = smoke.BootResult(
            name=name, output=serial.read_text(errors="replace"),
            returncode=case.get("returncode", -1), timed_out=case.get("timed_out", True),
            output_limit=case.get("output_limit", True), previous_nonce=case.get("previous_nonce"),
            nonce=case.get("nonce"), window_id=case.get("window_id"),
            pointer_clicks=case.get("pointer_clicks", 0),
            preference_toggled=case.get("preference_toggled", False),
            keyboard_sent=case.get("keyboard_sent", False), exit_sent=case.get("exit_sent", False),
            powerdown_sent=case.get("powerdown_sent", False), screenshots=case.get("screenshots", {}),
        )
        if name == "negative":
            smoke.validate_negative(result, manifest["kernel_release"], manifest["source_revision"])
        else:
            require(result.previous_nonce == (first_nonce if name == "round2" else None),
                    f"{name} nonce lineage is broken")
            smoke.validate_positive(result, manifest["kernel_release"], manifest["source_revision"])
            if name == "round1":
                first_nonce = result.nonce
            frames = {}
            for stage in smoke.SCREEN_STAGES:
                proof = result.screenshots[stage]
                expected = f"screenshots/{name}-{stage}.ppm"
                preview = f"screenshots/{name}-{stage}.png"
                require(proof.get("path") == expected and proof.get("png_path") == preview,
                        "screenshot provenance paths mismatch")
                frame_file = safe_file(expected)
                png_file = safe_file(preview)
                require(digest(frame_file) == proof.get("sha256") and
                        digest(png_file) == proof.get("png_sha256"), "screenshot bytes changed")
                frame = smoke.p2.parse_ppm(frame_file.read_bytes())
                smoke.verify_frame(frame, stage)
                require(smoke.p2.png_bytes(frame) == png_file.read_bytes(),
                        "PNG preview is not a lossless export of the QEMU PPM")
                frames[stage] = frame
            require(smoke.changed_fraction(frames["ready"], frames["foot"]) >= .07,
                    "real foot window did not change the framebuffer")
            require(smoke.changed_fraction(frames["ready"], frames["preferences"]) >= .025,
                    "Applications/Compact Dock settings did not render")
            require(smoke.changed_fraction(frames["foot"], frames["desktop"]) >= .07,
                    "closed application did not disappear from framebuffer")
        smoke.transcript(qmp.read_text(), result.nonce, result.screenshots,
                         preference_toggled=result.preference_toggled)
    print(f"phase3: all three original-shell QMP cases verified for {manifest['source_sha']}")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("create", "check-build", "verify"):
        print("usage: manifest.py {create|check-build|verify}", file=sys.stderr)
        return 2
    try:
        {"create": create, "check-build": check_build, "verify": verify}[sys.argv[1]]()
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, UnicodeError,
            subprocess.CalledProcessError) as error:
        print(f"phase3: manifest {sys.argv[1]} FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
