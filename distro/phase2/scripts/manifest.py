#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Bind signed userspace, a disk-bootable kernel, and graphical evidence."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

PHASE = Path(__file__).resolve().parent.parent
ROOT = PHASE.parent.parent
OUT = ROOT / ".build" / "phase2"
FILES = {
    "kernel": "kernel/arch/x86/boot/bzImage",
    "disk": "phase2.img", "distribution_disk": "phase2.qcow2",
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
    "gcc": ["gcc", "--version"], "binutils": ["ld", "--version"],
    "make": ["make", "--version"], "python": ["python3", "--version"],
    "mmdebstrap": ["mmdebstrap", "--version"], "dpkg": ["dpkg", "--version"],
    "qemu": ["qemu-system-x86_64", "--version"], "qemu_img": ["qemu-img", "--version"],
    "grub": ["grub-mkstandalone", "--version"], "e2fsprogs": ["mke2fs", "-V"],
    "sfdisk": ["sfdisk", "--version"],
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(args):
    if args[0] == "git":
        # sudo staging has a different owner from the checkout. Trust only this
        # explicitly selected repository, for this invocation (never globally).
        args = ["git", "-c", f"safe.directory={ROOT}", *args[1:]]
    return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def source_state():
    sha = command(["git", "rev-parse", "HEAD"])
    dirty = bool(command(["git", "status", "--porcelain", "--untracked-files=normal"]))
    return sha, dirty, sha + ("-dirty" if dirty else "")


def source_fingerprint():
    result = hashlib.sha256()
    result.update(command(["git", "rev-parse", "HEAD"]).encode())
    git = ["git", "-c", f"safe.directory={ROOT}"]
    result.update(subprocess.check_output([*git, "diff", "--binary", "--full-index", "HEAD"], cwd=ROOT))
    names = subprocess.check_output([*git, "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    for name in sorted(item for item in names.split(b"\0") if item):
        path = ROOT / os.fsdecode(name)
        metadata = path.lstat()
        result.update(b"\0" + name + b"\0" + str(metadata.st_mode).encode() + b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            result.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(metadata.st_mode):
            result.update(digest(path).encode())
        else:
            raise ValueError(f"unsupported untracked source input: {path}")
    return result.hexdigest()


def safe_file(relative, missing=False):
    require(isinstance(relative, str), "artifact path is not a string")
    name = Path(relative)
    require(not name.is_absolute() and ".." not in name.parts and name.parts, "unsafe artifact path")
    path = OUT / name
    require(OUT.resolve() == OUT and not OUT.is_symlink(), "unsafe output directory")
    require(path.parent.resolve() == path.parent, "symlink in artifact parent")
    if missing and not path.exists() and not path.is_symlink():
        return path
    metadata = path.lstat()
    require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1, f"unsafe artifact: {relative}")
    return path


def write_json(relative, payload):
    safe_file(relative, missing=True).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def file_record(relative):
    path = safe_file(relative)
    return {"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)}


def validate_file_record(entry, relative):
    require(entry.get("path") == relative, f"unexpected artifact path: {relative}")
    path = safe_file(relative)
    require(path.stat().st_size == entry.get("bytes") and digest(path) == entry.get("sha256"),
            f"artifact changed after build: {relative}")


def create():
    sha, dirty, revision = source_state()
    fingerprint = source_fingerprint()
    require(safe_file("source-fingerprint").read_text().strip() == fingerprint,
            "source bytes changed after kernel build; rebuild")
    require(safe_file("source-revision").read_text().strip() == revision,
            "source revision changed after kernel build; rebuild")
    require(os.environ.get("CI") != "true" or not dirty, "CI checkout is dirty")
    safe_file("inputs.lock", missing=True).write_bytes((PHASE / "inputs.lock").read_bytes())
    outputs = {name: file_record(relative) for name, relative in FILES.items()}
    metadata_dir = OUT / "apt-release-metadata"
    require(metadata_dir.is_dir() and metadata_dir.resolve() == metadata_dir, "unsafe/missing release metadata")
    releases = [file_record(str(path.relative_to(OUT))) for path in sorted(metadata_dir.iterdir())]
    require(len(releases) >= 3 and all(item["path"].endswith("_InRelease") for item in releases),
            "missing signed Debian/update/security release evidence")
    versions = {name: command(args).splitlines()[0] for name, args in TOOLS.items()}
    packages = command(["dpkg-query", "-W", "-f=${Package}\t${Version}\n"])
    safe_file("logs/host-packages.log", missing=True).write_text(packages + "\n")
    ci_fields = ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID",
                 "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion")
    payload = {
        "schema_version": 1, "phase": 2,
        "scope": "UEFI disk boot, Debian package lifecycle, DRM/Pixman Wayland, real input and persistence",
        "source_sha": sha, "source_tree": command(["git", "rev-parse", "HEAD^{tree}"]),
        "source_revision": revision, "source_dirty": dirty, "source_fingerprint": fingerprint,
        "source_url": f"https://github.com/boop-bip80/operating-system-1/tree/{sha}",
        "kernel_release": safe_file("kernelrelease").read_text().strip(),
        "source_date_epoch": int(os.environ.get("SOURCE_DATE_EPOCH") or command(["git", "show", "-s", "--format=%ct", "HEAD"])),
        "architecture": "x86_64", "tools": versions,
        "ci": {key: os.environ.get(key, "") for key in ci_fields},
        "host_packages": file_record("logs/host-packages.log"),
        "signed_release_metadata": releases,
        "reproducibility": "Pinned signed guest snapshot and source; host versions recorded. Functional repeatability, not a hermetic or byte-identical-build claim.",
        "outputs": outputs,
    }
    write_json("manifest.json", payload)
    # Rebuilding cannot inherit the last build's acceptance verdict.
    write_json("test-results.json", {"schema_version": 1, "status": "not_run", "cases": [],
                                    "reason": "New manifest; rerun graphical acceptance."})
    records = list(outputs.values()) + releases + [payload["host_packages"]]
    safe_file("SHA256SUMS", missing=True).write_text("".join(f"{item['sha256']}  {item['path']}\n" for item in records))
    print(f"phase2: provenance recorded for {revision}")


def check_build():
    manifest = json.loads(safe_file("manifest.json").read_text())
    sha, dirty, revision = source_state()
    require(manifest.get("schema_version") == 1 and manifest.get("phase") == 2, "unknown manifest schema")
    require(manifest.get("source_sha") == sha and manifest.get("source_revision") == revision,
            "checkout identity changed after build")
    require(manifest.get("source_fingerprint") == source_fingerprint(), "source bytes changed after build")
    require(os.environ.get("CI") != "true" or not dirty, "dirty CI checkout")
    require(manifest.get("source_dirty") == dirty, "dirty-source state changed")
    require(set(manifest["outputs"]) == set(FILES), "missing or unexpected build outputs")
    for name, relative in FILES.items():
        validate_file_record(manifest["outputs"][name], relative)
    require(safe_file("inputs.lock").read_bytes() == (PHASE / "inputs.lock").read_bytes(), "input lock changed")
    releases = manifest["signed_release_metadata"]
    require(len(releases) >= 3 and len({entry["path"] for entry in releases}) == len(releases), "invalid signed metadata set")
    for entry in releases:
        relative = entry["path"]
        require(Path(relative).parent == Path("apt-release-metadata") and relative.endswith("_InRelease"),
                "unexpected signed metadata path")
        validate_file_record(entry, relative)
    validate_file_record(manifest["host_packages"], "logs/host-packages.log")
    print("phase2: source, signed-snapshot inventory and immutable build outputs verified")
    return manifest


def validate_results(manifest, result):
    require(result.get("schema_version") == 1 and result.get("status") == "pass", "graphical suite has not passed")
    require(result.get("manifest_sha256") == digest(safe_file("manifest.json")), "test evidence belongs to another manifest")
    require(result.get("source_revision") == manifest["source_revision"], "suite tested another source")
    require(result.get("kernel_release") == manifest["kernel_release"], "suite tested another kernel")
    cases = result.get("cases", [])
    require([case.get("name") for case in cases] == ["round1", "round2", "negative"], "required boot cases missing or out of order")
    require(all(case.get("status") == "pass" for case in cases), "a boot case failed")
    # Re-evaluate the serial/input evidence, not merely a mutable JSON pass bit.
    spec = importlib.util.spec_from_file_location("phase2_smoke_verifier", PHASE / "scripts/smoke-test.py")
    smoke = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke
    spec.loader.exec_module(smoke)
    previous_nonce = None
    for case in cases:
        name = case["name"]
        logs = {}
        for field, relative in (("serial_log", f"logs/boot-{name}.log"), ("qmp_log", f"logs/qmp-{name}.jsonl")):
            require(case.get(field) == relative, "unexpected evidence path")
            path = safe_file(relative)
            require(digest(path) == case.get(field + "_sha256"), f"{field} changed after test")
            logs[field] = path
        smoke.validate_qmp_log(logs["qmp_log"].read_text(), case["nonce"], case["screenshots"])
        boot = smoke.BootResult(
            name=name, output=logs["serial_log"].read_text(), returncode=case["returncode"],
            timed_out=case["timed_out"], output_limit=case["output_limit"],
            nonce=case["nonce"], previous_nonce=case["previous_nonce"],
            keyboard_sent=case["keyboard_sent"], pointer_sent=case["pointer_sent"],
            escape_sent=case["escape_sent"], powerdown_sent=case["powerdown_sent"],
            screenshots=case["screenshots"],
        )
        if name == "negative":
            smoke.validate_negative_boot(boot, manifest["kernel_release"], manifest["source_revision"])
        else:
            require(case["previous_nonce"] == previous_nonce, "second boot did not verify first boot's persisted data")
            smoke.validate_test_boot(boot, manifest["kernel_release"], manifest["source_revision"])
            for stage, evidence in case["screenshots"].items():
                for path_key, hash_key, extension in (("path", "sha256", "ppm"), ("png_path", "png_sha256", "png")):
                    relative = f"screenshots/{name}-{stage}.{extension}"
                    require(evidence[path_key] == relative, "unexpected screenshot path")
                    path = safe_file(relative)
                    require(digest(path) == evidence[hash_key], "screenshot changed after test")
                smoke.validate_frame(smoke.parse_ppm(safe_file(evidence["path"]).read_bytes()), stage)
            previous_nonce = case["nonce"]


def verify():
    manifest = check_build()
    validate_results(manifest, json.loads(safe_file("test-results.json").read_text()))
    print("phase2: all three disk-boot scenarios and hash-bound graphical evidence verified")
    print("Phase 2 sign-off also requires two clean CI attempts on this SHA plus Phase 1 regression; see docs/os/PHASE_02.md")


def main():
    try:
        actions = {"create": create, "check-build": check_build, "verify": verify,
                   "fingerprint": lambda: print(source_fingerprint())}
        require(len(sys.argv) == 2 and sys.argv[1] in actions, "usage: manifest.py create|check-build|verify|fingerprint")
        actions[sys.argv[1]]()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"phase2 provenance failure: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
