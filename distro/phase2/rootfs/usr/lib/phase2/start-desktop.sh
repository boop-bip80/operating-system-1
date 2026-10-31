#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -Eeuo pipefail
[[ -f /etc/phase2-source && "$(id -u)" == 1000 && "$(id -g)" == 1000 ]] || exit 1
reason=SESSION
weston_pid=
terminal_pid=
terminal_shell_pid=
log_pid=
# shellcheck disable=SC2329 # Called by the EXIT trap, including signal exits.
cleanup() {
    trap - EXIT
    for child in "$terminal_pid" "$weston_pid" "$log_pid"; do
        if [[ -n "$child" ]]; then kill "$child" 2>/dev/null || true; fi
    done
}
failed() { printf 'P2:FAIL:%s\n' "$reason"; exit 1; }
trap failed ERR
trap cleanup EXIT
trap 'exit 0' TERM INT
[[ "${XDG_RUNTIME_DIR:-}" == /run/user/1000 ]]
[[ -d "$XDG_RUNTIME_DIR" && ! -L "$XDG_RUNTIME_DIR" ]]
[[ "$(stat -c '%u:%a' "$XDG_RUNTIME_DIR")" == 1000:700 ]]
[[ "${XDG_SESSION_TYPE:-}" == wayland && "${XDG_SESSION_ID:-}" != '' ]]
[[ "$(loginctl show-session "$XDG_SESSION_ID" -p Active --value)" == yes ]]
[[ "$(loginctl show-session "$XDG_SESSION_ID" -p Seat --value)" == seat0 ]]
[[ "$(loginctl show-session "$XDG_SESSION_ID" -p Type --value)" == wayland ]]
printf 'P2:DESKTOP_USER:1000\n'
export WAYLAND_DISPLAY=wayland-0 GDK_BACKEND=wayland LIBSEAT_BACKEND=logind
weston_log="$HOME/.local/state/phase2/weston.log"
: > "$weston_log"
reason=WESTON
weston --backend=drm --renderer=pixman --socket=wayland-0 --idle-time=0 \
    --config=/etc/xdg/weston/weston.ini --log="$weston_log" &
weston_pid=$!
# Preserve renderer/DRM/logind diagnostics in the host serial artifact.
tail --lines=+1 --follow=name --retry --sleep-interval=0.1 "$weston_log" &
log_pid=$!
for ((attempt=0; attempt<120; attempt++)); do
    kill -0 "$weston_pid"
    if [[ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]] && \
       timeout 3 wayland-info > "$HOME/.local/state/phase2/wayland-info.log" 2>&1; then
        break
    fi
    sleep 0.25
done
[[ -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" ]]
timeout 5 wayland-info > "$HOME/.local/state/phase2/wayland-info.log" 2>&1
[[ "$(awk '/^Uid:/ { print $2 ":" $3 ":" $4 ":" $5 }' "/proc/$weston_pid/status")" == 1000:1000:1000:1000 ]]
printf 'P2:WESTON_READY\n'
reason=TERMINAL
terminal_directory=$(mktemp -d -- "$XDG_RUNTIME_DIR/phase2-terminal.XXXXXXXX")
terminal_receipt="$terminal_directory/ready"
PHASE2_TERMINAL_RECEIPT="$terminal_receipt" \
    weston-terminal --shell=/usr/lib/phase2/terminal-shell.sh &
terminal_pid=$!
terminal_ready=false
for ((attempt=0; attempt<120; attempt++)); do
    kill -0 "$weston_pid"
    kill -0 "$terminal_pid"
    if terminal_shell_pid=$(python3 /usr/lib/phase2/terminal_state.py \
        "$terminal_receipt" "$terminal_pid" "$XDG_SESSION_ID"); then
        terminal_ready=true
        break
    else
        # A missing receipt or the brief interval before exec is pending;
        # malformed, stale or mismatched process evidence is a hard failure.
        [[ "$?" == 1 ]]
    fi
    sleep 0.25
done
[[ "$terminal_ready" == true ]]
printf 'P2:TERMINAL_READY\n'
if [[ -f /run/phase2/test-mode ]]; then
    reason=INPUT_PROBE
    python3 /usr/lib/phase2/input-probe.py
fi
reason=TERMINAL_EXIT
kill -0 "$terminal_pid"
kill -0 "$terminal_shell_pid"
reason=WESTON_EXIT
wait "$weston_pid"
# Normal system shutdown stops the service with SIGTERM; an otherwise clean
# compositor exit during a running session is still a failure.
failed
