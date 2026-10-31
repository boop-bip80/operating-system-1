#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Record the exact tested inputs; verify artifacts, source and all four VM cases."""
import hashlib
import json
import os
import stat
from pathlib import Path
import subprocess
import sys

PHASE = Path(__file__).resolve().parent.parent
ROOT = PHASE.parent.parent
OUT = ROOT / ".build" / "phase1"
FILES = {
    "kernel": "kernel/arch/x86/boot/bzImage",
    "initramfs": "initramfs.cpio.gz",
    "negative_initramfs": "broken-initramfs.cpio.gz",
    "kernel_config": "kernel.config",
    "busybox_config": "busybox.config",
    "kernel_release": "kernelrelease",
    "source_revision": "source-revision",
    "source_fingerprint": "source-fingerprint",
    "inputs_lock": "inputs.lock",
}
TOOLS = {
    "gcc": ["gcc", "--version"], "binutils": ["ld", "--version"],
    "make": ["make", "--version"], "musl_gcc": ["musl-gcc", "--version"],
    "qemu": ["qemu-system-x86_64", "--version"], "python": ["python3", "--version"],
    "gzip": ["gzip", "--version"], "e2fsprogs": ["mke2fs", "-V"],
}


def command(args):
    return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def source_state():
    sha = command(["git", "rev-parse", "HEAD"])
    dirty = bool(command(["git", "status", "--porcelain", "--untracked-files=normal"]))
    return sha, dirty, sha + ("-dirty" if dirty else "")


def source_fingerprint():
    """Identify dirty checkouts too, without exposing diffs or environment secrets."""
    hasher = hashlib.sha256()
    hasher.update(command(["git", "rev-parse", "HEAD"]).encode())
    hasher.update(subprocess.check_output(["git", "diff", "--binary", "--full-index", "HEAD"], cwd=ROOT))
    names = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    for raw in sorted(name for name in names.split(b"\0") if name):
        path = ROOT / os.fsdecode(raw)
        metadata = path.lstat()
        hasher.update(b"\0" + raw + b"\0" + str(metadata.st_mode).encode() + b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            hasher.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(metadata.st_mode):
            hasher.update(digest(path).encode())
        else:
            raise ValueError(f"unsupported untracked source input: {path}")
    return hasher.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def create():
    sha, dirty, revision = source_state()
    fingerprint = source_fingerprint()
    require((OUT / "source-fingerprint").read_text().strip() == fingerprint,
            "AC-01: source inputs changed after kernel build; rebuild")
    require((OUT / "source-revision").read_text().strip() == revision,
            "AC-01: source changed between kernel build and manifest; rebuild")
    if os.environ.get("CI") == "true":
        require(not dirty, "AC-01: CI source checkout is dirty")
    (OUT / "inputs.lock").write_bytes((PHASE / "inputs.lock").read_bytes())
    outputs = {}
    for name, relative in FILES.items():
        path = OUT / relative
        require(path.is_file() and not path.is_symlink(), f"missing/unsafe build output: {relative}")
        outputs[name] = {"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)}
    versions = {name: command(args).splitlines()[0] for name, args in TOOLS.items()}
    ci_fields = ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID",
                 "GITHUB_RUN_ATTEMPT", "RUNNER_OS", "RUNNER_ARCH", "ImageOS", "ImageVersion")
    packages = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Version}\n"],
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    require(packages.returncode == 0, "could not record Debian/Ubuntu build package versions")
    (OUT / "logs" / "host-packages.log").write_text(packages.stdout)
    payload = {
        "schema_version": 1, "scope": "single-source build; four QEMU boot scenarios",
        "source_sha": sha, "source_tree": command(["git", "rev-parse", "HEAD^{tree}"]),
        "source_revision": revision, "source_dirty": dirty,
        "source_fingerprint": fingerprint,
        "source_url": f"https://github.com/boop-bip80/operating-system-1/tree/{sha}",
        "kernel_release": (OUT / "kernelrelease").read_text().strip(),
        "source_date_epoch": int(os.environ.get("SOURCE_DATE_EPOCH") or command(["git", "show", "-s", "--format=%ct", "HEAD"])),
        "architecture": "x86_64", "tools": versions,
        "ci": {key: os.environ.get(key, "") for key in ci_fields},
        "host_packages_sha256": digest(OUT / "logs" / "host-packages.log"),
        "reproducibility": "Pinned source inputs and functional repeatability; host packages recorded, not hermetic.",
        "outputs": outputs,
    }
    (OUT / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    # A fresh build never inherits a previous test verdict, even at the same SHA.
    (OUT / "test-results.json").write_text(json.dumps({
        "schema_version": 1, "status": "not_run", "cases": [],
        "reason": "Build manifest recreated; rerun make test for these artifacts."
    }, indent=2) + "\n")
    lines = [f"{entry['sha256']}  {entry['path']}" for entry in outputs.values()]
    (OUT / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    print(f"AC-01/AC-02: manifest recorded for {revision}")


def verify():
    manifest = json.loads((OUT / "manifest.json").read_text())
    sha, dirty, revision = source_state()
    require(manifest["schema_version"] == 1, "unknown manifest schema")
    require(manifest["source_sha"] == sha and manifest["source_revision"] == revision,
            "AC-01: checkout changed after build")
    require(manifest["source_fingerprint"] == source_fingerprint(),
            "AC-01: source input bytes changed after build")
    require(not dirty if os.environ.get("CI") == "true" else True, "AC-01: dirty CI checkout")
    for name, relative in FILES.items():
        entry = manifest["outputs"][name]
        require(entry["path"] == relative, f"unexpected artifact path: {name}")
        path = OUT / relative
        require(path.is_file() and not path.is_symlink(), f"missing/unsafe artifact: {relative}")
        require(digest(path) == entry["sha256"], f"artifact changed after build: {relative}")
    result = json.loads((OUT / "test-results.json").read_text())
    require(result.get("manifest_sha256") == digest(OUT / "manifest.json"),
            "AC-01: test evidence belongs to a different build manifest")
    require(result.get("status") == "passed", "AC-05–AC-11: VM test suite has not passed")
    require(result.get("source_revision") == revision, "AC-01: VM suite tested another source")
    require(result.get("kernel_release") == manifest["kernel_release"], "AC-02: VM suite tested another kernel")
    cases = result.get("cases", [])
    require(len(cases) == 4 and {case["name"] for case in cases} == {"round1", "round2", "interactive", "negative"},
            "AC-05–AC-11: required VM cases missing")
    require(all(case["status"] == "passed" for case in cases), "AC-05–AC-11: VM case failed")
    print("AC-01–AC-11: this build's source, hashes, and four boot scenarios verified")
    print("AC-12 sign-off still requires two clean CI attempts on this same commit; see docs/os/PHASE_01.md")


def main():
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("create", "verify", "fingerprint"):
            raise ValueError("usage: manifest.py create|verify|fingerprint")
        if sys.argv[1] == "fingerprint":
            print(source_fingerprint())
        else:
            {"create": create, "verify": verify}[sys.argv[1]]()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"phase1 provenance failure: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
