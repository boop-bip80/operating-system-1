#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# This helper is executed by weston-terminal inside its newly allocated PTY.
set -euo pipefail
[[ -f /etc/phase2-source && "$(id -u)" == 1000 && "$(id -g)" == 1000 ]]
[[ -t 0 && -t 1 ]]
# Opening /dev/tty also proves that this is a controlling terminal, not just
# unrelated tty descriptors passed to a background process.
: < /dev/tty
[[ "${XDG_RUNTIME_DIR:-}" == /run/user/1000 ]]
[[ "${XDG_SESSION_ID:-}" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]
receipt=${PHASE2_TERMINAL_RECEIPT:-}
[[ "$receipt" =~ ^/run/user/1000/phase2-terminal\.[A-Za-z0-9]{8}/ready$ ]]
receipt_dir=${receipt%/ready}
[[ -d "$receipt_dir" && ! -L "$receipt_dir" ]]
[[ "$(realpath -- "$receipt_dir")" == "$receipt_dir" ]]
[[ "$(stat -c '%u:%a' -- "$receipt_dir")" == 1000:700 ]]
[[ ! -e "$receipt" && ! -L "$receipt" ]]
[[ "$(readlink -f -- "/proc/$PPID/exe")" == "$(readlink -f -- /usr/bin/weston-terminal)" ]]
terminal_tty=$(readlink -- "/proc/$$/fd/0")
[[ "$terminal_tty" =~ ^/dev/pts/[0-9]+$ && -c "$terminal_tty" ]]
[[ "$(readlink -- "/proc/$$/fd/1")" == "$terminal_tty" ]]
umask 077
temporary=$(mktemp -- "$receipt_dir/.ready.XXXXXXXX")
printf '%s\t%s\t%s\t%s\n' "$XDG_SESSION_ID" "$$" "$PPID" "$terminal_tty" > "$temporary"
mv -T -- "$temporary" "$receipt"
# The session wrapper checks this exact PID's /proc cmdline after the exec,
# so publishing the receipt alone cannot establish an interactive shell.
unset PHASE2_TERMINAL_RECEIPT
exec /bin/bash -i
