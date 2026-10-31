#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Create gen_init_cpio manifests and validate archives without privileged extraction."""
import gzip
import os
import stat
import sys
from pathlib import Path, PurePosixPath


def manifest(stage, destination):
    stage = Path(stage).resolve()
    rows = []
    for path in sorted(stage.rglob("*"), key=lambda p: (len(p.relative_to(stage).parts), p.as_posix())):
        name = "/" + path.relative_to(stage).as_posix()
        if any(c.isspace() for c in str(path)):
            raise ValueError(f"whitespace in gen_init_cpio input: {path}")
        mode = path.lstat().st_mode
        permissions = mode & 0o777
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            if any(c.isspace() for c in target):
                raise ValueError(f"whitespace in symlink: {path}")
            rows.append(f"slink {name} {target} 0777 0 0")
        elif stat.S_ISDIR(mode):
            rows.append(f"dir {name} {permissions:04o} 0 0")
        elif stat.S_ISREG(mode):
            rows.append(f"file {name} {path} {permissions:04o} 0 0")
        else:
            raise ValueError(f"unexpected host special file: {path}")
    rows.extend(["nod /dev/console 0600 0 0 c 5 1", "nod /dev/null 0666 0 0 c 1 3"])
    Path(destination).write_text("\n".join(rows) + "\n")


def read_archive(path):
    data = gzip.decompress(Path(path).read_bytes())
    offset = 0
    result = {}
    while offset + 110 <= len(data):
        header = data[offset:offset + 110]
        if header[:6] != b"070701":
            raise ValueError("expected CPIO newc magic")
        fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        mode, uid, gid, size, namesize = fields[1], fields[2], fields[3], fields[6], fields[11]
        offset += 110
        if namesize < 1 or offset + namesize > len(data) or data[offset + namesize - 1] != 0:
            raise ValueError("invalid archive name")
        raw_name = data[offset:offset + namesize - 1].decode()
        if "\x00" in raw_name:
            raise ValueError("embedded NUL in archive name")
        offset = (offset + namesize + 3) & ~3
        if raw_name == "TRAILER!!!":
            if size != 0 or any(data[offset:]):
                raise ValueError("unexpected content after CPIO trailer")
            return result
        if ".." in PurePosixPath(raw_name).parts:
            raise ValueError("archive path traversal")
        name = "/" + str(PurePosixPath(raw_name)).lstrip("/")
        if name in result or offset + size > len(data):
            raise ValueError("duplicate or truncated archive entry")
        result[name] = {"mode": mode, "uid": uid, "gid": gid, "data": data[offset:offset + size],
                        "major": fields[9], "minor": fields[10]}
        offset = (offset + size + 3) & ~3
    raise ValueError("CPIO trailer missing")


def verify(path, release, revision):
    entries = read_archive(path)
    for name in ("/init", "/bin/busybox", "/etc/udhcpc.script"):
        entry = entries[name]
        if not stat.S_ISREG(entry["mode"]) or not entry["mode"] & 0o111:
            raise ValueError(f"not an executable regular file: {name}")
    if not entries["/init"]["data"].startswith(b"#!/bin/sh\n"):
        raise ValueError("/init interpreter mismatch")
    if not entries["/bin/busybox"]["data"].startswith(b"\x7fELF"):
        raise ValueError("BusyBox is not ELF")
    sh = entries["/bin/sh"]
    # Linux gen_init_cpio stores the symlink target INCLUDING its terminating
    # NUL (strlen(target) + 1). Other newc writers may omit it. Accept both
    # exact encodings, never embedded NULs or alternate/escaping targets.
    sh_target = sh["data"].removesuffix(b"\x00")
    if not stat.S_ISLNK(sh["mode"]) or sh_target not in (b"busybox", b"/bin/busybox"):
        raise ValueError("/bin/sh must resolve to the packaged BusyBox")
    for name, major, minor in (("/dev/console", 5, 1), ("/dev/null", 1, 3)):
        node = entries[name]
        if not stat.S_ISCHR(node["mode"]) or (node["major"], node["minor"]) != (major, minor):
            raise ValueError(f"incorrect device node: {name}")
    for name, source in (("/etc/phase1-release", release), ("/etc/phase1-source", revision)):
        if entries[name]["data"] != Path(source).read_bytes():
            raise ValueError(f"identity mismatch: {name}")
    if any(e["uid"] != 0 or e["gid"] != 0 for e in entries.values()):
        raise ValueError("non-root archive ownership")
    print(f"AC-04: validated {len(entries)} entries, console nodes, init, shell, and identities")


def main():
    try:
        if sys.argv[1] == "manifest":
            manifest(*sys.argv[2:])
        elif sys.argv[1] == "verify":
            verify(*sys.argv[2:])
        else:
            raise ValueError("usage: initramfs.py manifest STAGE LIST | verify ARCHIVE RELEASE REVISION")
    except (OSError, ValueError, KeyError) as exc:
        print(f"AC-04: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
