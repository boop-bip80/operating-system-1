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
        "scope": "Sway DRM/Pixman shell; real GtkLayerShell Dock, foot HID, persistence, no-GPU and GTK-failure controls",
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


def check_native_layout(items: list[str]) -> None:
    """Independent P3-07 workarea oracle from SHA-bound Sway IPC serial bytes.

    GtkLayerShell setter calls alone cannot prove the compositor actually
    respected exclusive zones. The original shell projects two live Sway IPC
    replies plus the mapped GTK getters before it declares layer readiness.
    """
    prefix = "P3:IPC_LAYOUT:"
    evidence = [(index, marker[len(prefix):])
                for index, marker in enumerate(items) if marker.startswith(prefix)]
    require(len(evidence) == 1, "missing or duplicated native Sway workspace layout proof")
    position, raw = evidence[0]
    require(len(raw) <= 1200, "oversized Sway layout IPC evidence")
    value = json.loads(raw)
    require(json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) == raw,
            "Sway layout marker is not a canonical source-bound IPC projection")
    require(isinstance(value, dict) and set(value) == {
        "source", "output", "output_rect", "scale", "mode",
        "workspace", "workspace_rect", "top_exclusive", "dock_exclusive",
    }, "malformed or incomplete Sway output/workspace layout projection")
    require(value["source"] == "sway.get_outputs+get_workspaces" and
            value["output"] == "Virtual-1" and
            value["workspace"] == "1",
            "workarea projection did not originate from the required Sway output/workspace")
    require(type(value["scale"]) in (int, float) and value["scale"] == 1 and
            type(value["top_exclusive"]) is int and value["top_exclusive"] == 38 and
            type(value["dock_exclusive"]) is int and value["dock_exclusive"] == 68,
            "GTK layer-shell exclusive zones or native Sway output scale mismatch")
    def rect(name: str) -> dict:
        result = value[name]
        require(isinstance(result, dict) and set(result) ==
                {"x", "y", "width", "height"} and
                all(type(result[field]) is int for field in ("x", "y", "width", "height")),
                f"invalid real Sway {name} rectangle")
        return result
    output = rect("output_rect")
    workspace = rect("workspace_rect")
    mode = value["mode"]
    require(isinstance(mode, dict) and set(mode) == {"width", "height"} and
            type(mode["width"]) is int and type(mode["height"]) is int and
            mode == {"width": 1024, "height": 768} and
            output == {"x": 0, "y": 0, "width": 1024, "height": 768},
            "Sway native KMS output geometry/mode mismatch")
    # Independently re-evaluate Sway's 4px configured inner-gap allowance.
    # Requiring symmetric 0..4 horizontal insets plus the bounded top/bottom
    # surplus preserves proof of actual 38px bar and 68px Dock reservations.
    left = workspace["x"] - output["x"]
    right = output["x"] + output["width"] - (
        workspace["x"] + workspace["width"]
    )
    top_extra = workspace["y"] - (output["y"] + value["top_exclusive"])
    bottom_extra = output["y"] + output["height"] - value["dock_exclusive"] - (
        workspace["y"] + workspace["height"]
    )
    require(0 <= left == right <= 4 and 0 <= top_extra <= 4 and
            0 <= bottom_extra <= 12 and
            workspace["width"] > 0 and workspace["height"] > 0,
            "real Sway workspace usable rectangle did not reserve top bar and Dock")
    sway = next((i for i, marker in enumerate(items) if marker == "P3:SWAY_READY"), None)
    ipc = next((i for i, marker in enumerate(items) if marker.startswith("P3:IPC_TREE:")), None)
    layer = next((i for i, marker in enumerate(items) if marker == "P3:LAYER_SHELL_READY"), None)
    shell = next((i for i, marker in enumerate(items) if marker == "P3:SHELL_READY"), None)
    require(None not in (sway, ipc, layer, shell) and
            sway < ipc < position < layer < shell,
            "real Sway applied layout was not observed before GTK layer-shell readiness")


def check_native_app_index(items: list[str]) -> None:
    """Independently bind installed eligible and actually displayed Gio IDs.

    The original shell inventories real Gio.AppInfo entries; the output
    preserves only sanitized desktop IDs and counts, never names or Exec
    commands. A mapped pinned foot row is distinct from the Gio row subset.
    """
    prefix = "P3:APP_INDEX:"
    evidence = [(i, marker[len(prefix):])
                for i, marker in enumerate(items) if marker.startswith(prefix)]
    require(len(evidence) == 1, "missing/duplicated real Gio application index marker")
    position, raw = evidence[0]
    require(0 < len(raw) <= 4096, "unbounded Gio launcher index")
    value = json.loads(raw)
    require(isinstance(value, dict) and set(value) ==
            {"displayed_count", "displayed_ids", "eligible_count", "eligible_ids",
             "sha256", "source"} and
            json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) == raw,
            "malformed or noncanonical native Gio launcher index")
    eligible = value["eligible_ids"]
    displayed = value["displayed_ids"]
    def safe_ids(value: object, lower: int) -> bool:
        if not isinstance(value, list) or not lower <= len(value) <= 60:
            return False
        if not all(isinstance(name, str) and len(name) <= 128 and name.isascii() and
                   re.fullmatch(r"[A-Za-z0-9_.+-]+[.]desktop", name) is not None
                   for name in value):
            return False
        return value == sorted(set(value))
    require(value["source"] == "Gio.AppInfo.get_all" and
            type(value["eligible_count"]) is int and
            type(value["displayed_count"]) is int and
            safe_ids(eligible, 1) and safe_ids(displayed, 0) and
            value["eligible_count"] == len(eligible) and
            value["displayed_count"] == len(displayed) and
            set(displayed) <= set(eligible) and "foot.desktop" not in displayed and
            (len(displayed) > 0 or "foot.desktop" in eligible),
            "Gio index fails eligible/displayed subset, uniqueness or mapped Terminal fallback")
    inventory = {"eligible_ids": eligible, "displayed_ids": displayed}
    canonical = json.dumps(inventory, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    require(isinstance(value["sha256"], str) and
            re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None and
            hashlib.sha256(canonical).hexdigest() == value["sha256"],
            "Gio displayed/eligible desktop ID inventories have a forged digest")
    opened = next((i for i, item in enumerate(items) if item == "P3:LAUNCHER_OPEN"), None)
    compact = next((i for i, item in enumerate(items)
                    if item.startswith("P3:COMPACT_TARGET:")), None)
    terminal = next((i for i, item in enumerate(items)
                     if item.startswith("P3:TERMINAL_TARGET:")), None)
    launched = next((i for i, item in enumerate(items)
                     if item == "P3:APP_LAUNCHED:foot"), None)
    require(None not in (opened, compact, terminal, launched) and
            opened < compact < terminal < position < launched,
            "real installed Gio launcher index was not captured before the GTK Terminal click")


def verify() -> None:
    manifest = check_build()
    installed = {line.split("\t", 1)[0]
                 for line in safe_file("rootfs-packages.tsv").read_text().splitlines()}
    require({"foot", "sway"} <= installed,
            "native app index lacks the source-bound installed foot/Sway package inventory")
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
    require(isinstance(cases, list) and len(cases) == 4 and
            all(isinstance(case, dict) for case in cases) and
            [case.get("name") for case in cases] ==
            ["round1", "round2", "negative", "shell-negative"],
            "missing, malformed or reordered Phase 3 scenarios")
    required_stages = (
        "ready", "launcher", "preferences", "foot", "saved", "second",
        "focused", "menu_minimize", "hidden", "restored", "menu_close",
        "closed", "desktop",
    )
    require(tuple(smoke.SCREEN_STAGES) == required_stages and
            tuple(smoke.FOOT_CONTENT_BOX) == (132, 116, 680, 284) and
            tuple(smoke.MENU_POPUP_BOX) == (380, 495, 690, 755) and
            tuple(smoke.LAUNCHER_BOX) == (20, 52, 375, 420) and
            tuple(smoke.DOCK_ICON_BOX) == (492, 704, 532, 740),
            "Phase 3 IPC/Dock/launcher QMP screenshot/ROI contract changed without verifier review")
    require(smoke.BOOT_MARKERS.index("P3:PIXMAN_RENDERER_CREATED") ==
            smoke.BOOT_MARKERS.index("P3:DRM_MODESET_REQUESTED:Virtual-1") + 1,
            "live wlroots Pixman renderer proof must follow real DRM modeset marker")
    first_nonce = None
    first_ready_frame = None
    for case in cases:
        name = case["name"]
        require(case.get("status") == "pass", f"{name} did not pass")
        require(type(case.get("returncode")) is int and case["returncode"] == 0 and
                type(case.get("timed_out")) is bool and not case["timed_out"] and
                type(case.get("output_limit")) is bool and not case["output_limit"],
                f"{name} lacks a clean native QEMU process/shutdown result")
        require(type(case.get("pointer_clicks")) is int and case["pointer_clicks"] >= 0,
                f"{name} has invalid host pointer input count")
        for flag in ("second_opened", "minimized", "restored", "close_via_dock",
                     "preference_toggled", "keyboard_sent", "exit_sent", "powerdown_sent"):
            require(type(case.get(flag)) is bool,
                    f"{name} lacks a strict boolean for {flag}")
        for key in ("window_id", "second_window_id"):
            identifier = case.get(key)
            require(identifier is None or
                    (isinstance(identifier, str) and
                     re.fullmatch(r"[1-9][0-9]*", identifier) is not None),
                    f"{name} has invalid Sway IPC container identifier {key}")
        require(isinstance(case.get("screenshots"), dict),
                f"{name} screenshots evidence is not a mapping")
        for target_key in ("compact_target", "terminal_target"):
            target = case.get(target_key)
            if name in ("round1", "round2"):
                require(isinstance(target, list) and len(target) == 4 and
                        all(type(value) is int for value in target),
                        f"{name} lacks real four-dimensional mapped GTK {target_key} geometry")
            else:
                require(target is None,
                        f"{name} negative case claims a mapped GTK {target_key}")
        menu_targets = case.get("menu_targets")
        require(isinstance(menu_targets, dict),
                f"{name} lacks recorded, framebuffer-calibrated popup menu targets")
        if name in ("round1", "round2"):
            require(set(menu_targets) == {"Minimize", "Close"} and all(
                isinstance(point, list) and len(point) == 2 and
                all(type(value) is int for value in point)
                for point in menu_targets.values()),
                f"{name} has malformed real GTK Dock menu click coordinates")
        else:
            require(menu_targets == {},
                    f"{name} negative case unexpectedly claimed GUI Dock menu clicks")
        mode = ("negative" if name == "negative" else
                ("shell-negative" if name == "shell-negative" else "test"))
        disk = ("negative.qcow2" if name == "negative" else
                ("shell-negative.qcow2" if name == "shell-negative" else "test.qcow2"))
        command_line = ["bash", str(PHASE / "scripts" / "boot-qemu.sh"),
                        "--mode", mode, "--name", name, "--disk", str(OUT / disk)]
        require(case.get("mode") == mode and case.get("command") == command_line,
                f"{name} QEMU mode/overlay command is not bound to its acceptance scenario")
        if name == "shell-negative":
            # Independent writable disk, not a reused positive test overlay.
            safe_file(disk)
        for field, relative in (("serial_log", f"logs/boot-{name}.log"),
                                ("qmp_log", f"logs/qmp-{name}.jsonl")):
            require(case.get(field) == relative, f"{name} has invalid evidence path")
        serial = safe_file(case["serial_log"])
        qmp = safe_file(case["qmp_log"])
        require(case.get("serial_log_sha256") == digest(serial) and
                case.get("qmp_log_sha256") == digest(qmp), f"{name} evidence hashes differ")
        if name in ("negative", "shell-negative"):
            require(not any(marker.startswith(("P3:IPC_LAYOUT:", "P3:APP_INDEX:"))
                            for marker in smoke.lines(serial.read_text(errors="replace"))),
                    f"{name} negative control contains a healthy Sway/Gio UI claim")
        result = smoke.BootResult(
            name=name, output=serial.read_text(errors="replace"),
            returncode=case.get("returncode", -1), timed_out=case.get("timed_out", True),
            output_limit=case.get("output_limit", True), previous_nonce=case.get("previous_nonce"),
            nonce=case.get("nonce"), window_id=case.get("window_id"),
            second_window_id=case.get("second_window_id"),
            second_opened=case["second_opened"], minimized=case["minimized"],
            restored=case["restored"], close_via_dock=case["close_via_dock"],
            menu_targets=case["menu_targets"],
            compact_target=case.get("compact_target"),
            terminal_target=case.get("terminal_target"),
            pointer_clicks=case["pointer_clicks"],
            preference_toggled=case["preference_toggled"],
            keyboard_sent=case.get("keyboard_sent", False), exit_sent=case.get("exit_sent", False),
            powerdown_sent=case.get("powerdown_sent", False), screenshots=case.get("screenshots", {}),
        )
        if name == "negative":
            smoke.validate_negative(result, manifest["kernel_release"], manifest["source_revision"])
        elif name == "shell-negative":
            smoke.validate_shell_negative(result, manifest["kernel_release"], manifest["source_revision"])
        else:
            require(result.previous_nonce == (first_nonce if name == "round2" else None),
                    f"{name} nonce lineage is broken")
            smoke.validate_positive(result, manifest["kernel_release"], manifest["source_revision"])
            check_native_layout(smoke.lines(result.output))
            check_native_app_index(smoke.lines(result.output))
            # Parse the mapped GTK allocations directly from the SHA-bound
            # serial log. The JSON summary cannot supply arbitrary hitboxes
            # even if its QMP event list is otherwise internally consistent.
            for kind, field in (("COMPACT", "compact_target"),
                                ("TERMINAL", "terminal_target")):
                require(getattr(result, field) == list(smoke.gtk_target_from_output(
                    result.output, kind)),
                    f"{name} recorded {field} disagrees with native GTK allocation")
            if name == "round1":
                first_nonce = result.nonce
            # Reconstruct the genuine two-foot Sway get_tree sequence from
            # the SHA-bound serial bytes before checking QMP framebuffer data.
            # The production validator checks each id/PID/focus/scratchpad
            # transition against its corresponding on-wire IPC snapshots.
            snapshots = smoke.ipc_states(smoke.lines(result.output))
            require(snapshots and snapshots[-1][1]["seq"] == len(snapshots),
                    f"{name} Sway IPC state sequence was not complete")
            require(set(result.screenshots) == set(required_stages) and
                    len(result.screenshots) == len(required_stages),
                    f"{name} lacks one of the 13 required real QMP screen stages")
            frames = {}
            for stage in required_stages:
                proof = result.screenshots[stage]
                require(isinstance(proof, dict) and set(proof) ==
                        {"path", "sha256", "png_path", "png_sha256", "width", "height"},
                        f"{name} {stage} screenshot metadata is malformed")
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
            require(smoke.changed_fraction(frames["ready"], frames["launcher"],
                                           box=smoke.LAUNCHER_BOX) >= .015,
                    "native Applications launcher did not visibly map before GTK clicks")
            if name == "round1":
                require(smoke.changed_fraction(frames["launcher"], frames["preferences"],
                                               box=smoke.DOCK_ICON_BOX) >= .025,
                        "actual GTK Compact checkbox did not visibly shrink Dock icon")
            else:
                require(smoke.changed_fraction(frames["ready"], frames["preferences"],
                                               box=smoke.LAUNCHER_BOX) >= .015,
                        "second boot did not visibly display the GTK launcher")
            # Recompute the exact two-tier real-foot scanout oracle over
            # independently SHA-bound PPM bytes. Dark foot canvas differs
            # from wallpaper by 19 max-channel RGB units, while terminal
            # glyphs/borders provide additional >25 high-contrast detail.
            require(smoke.changed_fraction(frames["ready"], frames["foot"],
                                           pixel_delta=18) >= .07,
                    "real foot window did not change at least 7% of framebuffer")
            require(smoke.changed_fraction(frames["ready"], frames["foot"]) >= .005,
                    "real foot framebuffer lacks high-contrast terminal details")
            require(smoke.changed_fraction(
                frames["saved"], frames["second"],
                box=smoke.FOOT_CONTENT_BOX) >= .001,
                    "a second real Sway terminal did not change its real window pixels")
            require(smoke.changed_fraction(
                frames["second"], frames["focused"],
                box=smoke.FOOT_CONTENT_BOX) >= .001,
                    "Dock focus switch between two live foot clients was not visible")
            require(smoke.changed_fraction(
                frames["focused"], frames["menu_minimize"],
                box=smoke.MENU_POPUP_BOX) >= .003,
                    "real Dock Minimize menu was not visible")
            require(smoke.changed_fraction(
                frames["focused"], frames["hidden"],
                box=smoke.FOOT_CONTENT_BOX) >= .001,
                    "Sway scratchpad minimization did not change the real foot pixels")
            require(smoke.changed_fraction(
                frames["hidden"], frames["restored"],
                box=smoke.FOOT_CONTENT_BOX) >= .001,
                    "Sway scratchpad restoration did not change the real foot pixels")
            require(smoke.changed_fraction(
                frames["restored"], frames["menu_close"],
                box=smoke.MENU_POPUP_BOX) >= .003,
                    "real Dock Close menu was not visible")
            require(smoke.changed_fraction(
                frames["restored"], frames["closed"],
                box=smoke.FOOT_CONTENT_BOX) >= .001,
                    "Dock Close did not remove its target from the real foot pixels")
            require(smoke.changed_fraction(frames["closed"], frames["desktop"],
                                           pixel_delta=18) >= .07,
                    "surviving terminal did not visibly exit")
            # A self-consistent forged coordinates/QMP transcript is insufficient:
            # independently infer both real GTK popup rows from the exact
            # SHA-bound PPM framebuffer bytes, then check the recorded QMP HID.
            calibrated_targets = {
                "Minimize": list(smoke.popup_menu_target(
                    frames["focused"], frames["menu_minimize"], "Minimize")),
                "Close": list(smoke.popup_menu_target(
                    frames["restored"], frames["menu_close"], "Close")),
            }
            require(result.menu_targets == calibrated_targets,
                    f"{name} recorded Dock context menu targets do not match real QEMU pixels")
            if name == "round1":
                first_ready_frame = frames["ready"]
            else:
                require(first_ready_frame is not None,
                        "second boot cannot be compared with first GPU ready scanout")
                # Verify the real persisted icon is visibly smaller before any
                # second-boot input. Both frames were read from independently
                # SHA-256-bound QMP PPMs, not JSON claims or generated previews.
                smoke.prove_compact_dock_visible(first_ready_frame, frames["ready"])
                roi = (492, 704, 532, 740)
                require(smoke.changed_fraction(first_ready_frame, frames["ready"],
                                               box=roi) >= .025,
                        "second-boot Compact Dock pixels did not change in icon ROI")
                def bright_count(frame):
                    x1, y1, x2, y2 = roi
                    bright = 0
                    for y in range(y1, y2):
                        for x in range(x1, x2):
                            start = (y * frame.width + x) * 3
                            r, g, b = frame.pixels[start:start + 3]
                            bright += (r >= 150 and g >= 160 and b >= 170)
                    return bright
                require(bright_count(first_ready_frame) >=
                        bright_count(frames["ready"]) + 20,
                        "second-boot Dock icon did not shrink in original QMP scanout")
        # Replay every successful QMP screendump retry, real keyboard chord,
        # GTK Dock popup pointer clicks and powerdown from the hashed JSONL log.
        # Delayed GTK mapping cannot be hidden by skipped/unacknowledged QMP.
        # No screenshot or serial IPC marker alone can qualify.
        smoke.transcript(qmp.read_text(), result.nonce, result.screenshots,
                         preference_toggled=result.preference_toggled,
                         menu_targets=result.menu_targets,
                         compact_target=result.compact_target,
                         terminal_target=result.terminal_target)
    print(f"phase3: four source-bound graphical/IPC/fault cases, 13 QMP stages per positive boot verified for {manifest['source_sha']}")


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
