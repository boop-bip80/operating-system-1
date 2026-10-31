#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

for tool in musl-gcc gcc make rsync curl tar sha256sum readelf install tee grep sed awk; do
    phase1_require "$tool"
done

: "${BUSYBOX_VERSION:?inputs.lock must define BUSYBOX_VERSION}"
: "${BUSYBOX_URL:?inputs.lock must define BUSYBOX_URL}"
: "${BUSYBOX_SHA256:?inputs.lock must define BUSYBOX_SHA256}"
[[ "$BUSYBOX_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || phase1_die "AC-04: invalid BusyBox version"
[[ "$BUSYBOX_SHA256" =~ ^[0-9a-f]{64}$ ]] || phase1_die "AC-04: invalid BusyBox SHA-256 pin"
[[ "$BUSYBOX_URL" == https://* ]] || phase1_die "AC-04: BusyBox downloads require HTTPS"

phase1_prepare_output
for directory in "$DOWNLOAD_DIR" "$BUSYBOX_SOURCE_DIR" "$BUSYBOX_BUILD_DIR" "$KERNEL_UAPI_DIR" "$OUTPUT_DIR/rootfs"; do
    [[ ! -L "$directory" ]] || phase1_die "AC-04: refusing a symlink output directory: $directory"
done
mkdir -p "$DOWNLOAD_DIR" "$BUSYBOX_BUILD_DIR" "$OUTPUT_DIR/logs" "$OUTPUT_DIR/rootfs"
exec > >(tee "$OUTPUT_DIR/logs/build-busybox.log") 2>&1

# musl-gcc intentionally does not search the host glibc header tree. BusyBox's
# networking applets need Linux UAPI headers; export them from this repository's
# kernel rather than mixing /usr/include glibc headers into a static musl build.
phase1_log "Exporting repository Linux UAPI headers for static BusyBox"
make -C "$REPO_ROOT" O="$KERNEL_BUILD_DIR" ARCH=x86_64 \
    INSTALL_HDR_PATH="$KERNEL_UAPI_DIR" headers_install
for header in linux/types.h linux/netlink.h asm/types.h; do
    phase1_assert_regular "$KERNEL_UAPI_DIR/include/$header"
done
uapi_cflags="-I$KERNEL_UAPI_DIR/include"
if ! printf '#include <linux/types.h>\n#include <linux/netlink.h>\n#include <asm/types.h>\nint main(void) { return 0; }\n' | \
    musl-gcc "$uapi_cflags" -x c -fsyntax-only -; then
    phase1_die 'AC-04: musl-gcc cannot compile against exported repository Linux UAPI headers'
fi

archive="$DOWNLOAD_DIR/busybox-${BUSYBOX_VERSION}.tar.bz2"
temporary_archive=""
cleanup_download() {
    if [[ -n "$temporary_archive" && -f "$temporary_archive" ]]; then
        rm -f -- "$temporary_archive"
    fi
}
trap cleanup_download EXIT

verify_archive() {
    local actual_sha
    actual_sha=$(sha256sum -- "$1")
    actual_sha=${actual_sha%% *}
    [[ "$actual_sha" == "$BUSYBOX_SHA256" ]] || phase1_die "AC-04: BusyBox archive checksum mismatch: $1"
}

if [[ ! -f "$archive" ]]; then
    phase1_log "Downloading pinned BusyBox $BUSYBOX_VERSION"
    temporary_archive=$(mktemp "$DOWNLOAD_DIR/.busybox-download.XXXXXXXX")
    curl --fail --location --retry 3 --connect-timeout 30 --max-time 300 \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --output "$temporary_archive" "$BUSYBOX_URL"
    verify_archive "$temporary_archive"
    mv -- "$temporary_archive" "$archive"
    temporary_archive=""
fi
# Verify cached bytes too; a download cache is not a trust boundary.
verify_archive "$archive"

source_stamp="$BUSYBOX_SOURCE_DIR/.phase1-source.sha256"
build_stamp="$BUSYBOX_BUILD_DIR/.phase1-source.sha256"
if [[ -f "$source_stamp" ]]; then
    [[ "$(< "$source_stamp")" == "$BUSYBOX_SHA256" ]] || \
        phase1_die "AC-04: BusyBox source pin changed; use a fresh Phase 1 output directory"
    [[ -f "$BUSYBOX_SOURCE_DIR/Makefile" ]] || phase1_die "AC-04: incomplete cached BusyBox source"
else
    if [[ -d "$BUSYBOX_SOURCE_DIR" && -n "$(find "$BUSYBOX_SOURCE_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
        phase1_die "AC-04: refusing to reuse an unverified BusyBox source directory"
    fi
    mkdir -p "$BUSYBOX_SOURCE_DIR"
    tar --extract --bzip2 --file "$archive" --strip-components=1 \
        --directory "$BUSYBOX_SOURCE_DIR" --no-same-owner --no-same-permissions
    [[ -f "$BUSYBOX_SOURCE_DIR/Makefile" ]] || phase1_die "AC-04: BusyBox source archive has no Makefile"
    printf '%s\n' "$BUSYBOX_SHA256" > "$source_stamp"
fi
if [[ -f "$build_stamp" && "$(< "$build_stamp")" != "$BUSYBOX_SHA256" ]]; then
    phase1_die "AC-04: BusyBox build cache uses another source pin; use a fresh Phase 1 output directory"
fi
printf '%s\n' "$BUSYBOX_SHA256" > "$build_stamp"

export SOURCE_DATE_EPOCH
export KCONFIG_NOTIMESTAMP=1
export LC_ALL=C
export TZ=UTC
make_args=(-C "$BUSYBOX_SOURCE_DIR" "O=$BUSYBOX_BUILD_DIR" ARCH=x86_64 CC=musl-gcc HOSTCC=gcc CROSS_COMPILE= "EXTRA_CFLAGS=$uapi_cflags")

phase1_log "Configuring minimal static musl BusyBox"
make "${make_args[@]}" allnoconfig
# BusyBox's legacy allnoconfig resets booleans even when given
# KCONFIG_ALLCONFIG. Merge after allnoconfig, then normalize with defconfig.
KCONFIG_CONFIG="$BUSYBOX_BUILD_DIR/.config" \
    bash "$REPO_ROOT/scripts/kconfig/merge_config.sh" -m -Q -O "$BUSYBOX_BUILD_DIR" \
    "$BUSYBOX_BUILD_DIR/.config" "$PHASE_DIR/busybox.config"
make "${make_args[@]}" "KBUILD_DEFCONFIG=$BUSYBOX_BUILD_DIR/.config" defconfig

while IFS= read -r setting; do
    case "$setting" in
        CONFIG_*=*)
            grep -Fqx -- "$setting" "$BUSYBOX_BUILD_DIR/.config" || \
                phase1_die "AC-04: BusyBox Kconfig did not retain required setting: $setting"
            ;;
    esac
done < "$PHASE_DIR/busybox.config"
install -m 0644 "$BUSYBOX_BUILD_DIR/.config" "$OUTPUT_DIR/busybox.config"

phase1_log "Building BusyBox $BUSYBOX_VERSION with musl-gcc"
make "${make_args[@]}" -j"$JOBS"
binary="$BUSYBOX_BUILD_DIR/busybox"
[[ -x "$binary" ]] || phase1_die "AC-04: BusyBox build did not produce an executable"
readelf -h "$binary" > "$OUTPUT_DIR/logs/busybox-elf-header.txt"
readelf -l "$binary" > "$OUTPUT_DIR/logs/busybox-elf-programs.txt"
readelf -d "$binary" > "$OUTPUT_DIR/logs/busybox-elf-dynamic.txt"
grep -Eq 'Class:[[:space:]]+ELF64' "$OUTPUT_DIR/logs/busybox-elf-header.txt" || \
    phase1_die "AC-04: BusyBox is not a 64-bit ELF"
grep -Eq 'Machine:[[:space:]]+Advanced Micro Devices X86-64' "$OUTPUT_DIR/logs/busybox-elf-header.txt" || \
    phase1_die "AC-04: BusyBox is not an x86-64 ELF"
if grep -Eq '^[[:space:]]*INTERP([[:space:]]|$)' "$OUTPUT_DIR/logs/busybox-elf-programs.txt" || \
    grep -Fq '(NEEDED)' "$OUTPUT_DIR/logs/busybox-elf-dynamic.txt"; then
    phase1_die "AC-04: BusyBox depends on a dynamic interpreter or shared library"
fi

"$binary" --list > "$OUTPUT_DIR/logs/busybox-applets.txt"
required_applets=(ash sh mount umount mkdir mknod cat ls uname grep sed awk cut tr test '['
    echo printf sleep ip ipcalc udhcpc poweroff reboot sync rm cp mv dd head tail wc stat
    readlink setsid cttyhack stty env true false pwd chmod touch dmesg)
for applet in "${required_applets[@]}"; do
    grep -Fqx -- "$applet" "$OUTPUT_DIR/logs/busybox-applets.txt" || \
        phase1_die "AC-04: required BusyBox applet is missing: $applet"
done
# Expand arithmetic inside the newly built BusyBox shell, not the host shell.
# shellcheck disable=SC2016
"$binary" sh -c 'test "$((2 + 3))" -eq 5 && printf "%s\n" P1:BUSYBOX_HOST_OK'

# BusyBox install touches its binary and applet symlinks only. In particular,
# INIT is disabled: never replace the project's custom rootfs/init script.
make "${make_args[@]}" "CONFIG_PREFIX=$OUTPUT_DIR/rootfs" install
[[ -x "$OUTPUT_DIR/rootfs/bin/busybox" && -x "$OUTPUT_DIR/rootfs/bin/sh" ]] || \
    phase1_die "AC-04: BusyBox install omitted its binary or /bin/sh"
phase1_log "AC-04: static BusyBox and required applets validated"
