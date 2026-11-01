#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# A native seat0 login session, not a nested Wayland/X11 or headless window.
set -Eeuo pipefail
umask 077
reason=SESSION
sway_pid=
sway_tail_pid=
events_tail_pid=
shell_pid=
failed() { trap - ERR; printf 'P3:FAIL:%s\n' "$reason"; exit 1; }
# shellcheck disable=SC2317,SC2329 # Invoked by EXIT trap, including TERM handling.
cleanup() {
    trap - EXIT
    for pid in "$shell_pid" "$sway_pid" "$sway_tail_pid" "$events_tail_pid"; do
        if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
    done
}
trap failed ERR
trap cleanup EXIT
trap 'exit 0' TERM INT
[[ "$(id -u):$(id -g)" == 1000:1000 ]]
[[ -f /etc/phase3-source ]]
[[ "${XDG_RUNTIME_DIR:-}" == /run/user/1000 ]]
[[ -d "$XDG_RUNTIME_DIR" && ! -L "$XDG_RUNTIME_DIR" ]]
[[ "$(stat -c '%u:%a' "$XDG_RUNTIME_DIR")" == 1000:700 ]]
[[ "${XDG_SESSION_TYPE:-}" == wayland && -n "${XDG_SESSION_ID:-}" ]]
[[ "$(loginctl show-session "$XDG_SESSION_ID" -p Active --value)" == yes ]]
[[ "$(loginctl show-session "$XDG_SESSION_ID" -p Seat --value)" == seat0 ]]
[[ "$(loginctl show-session "$XDG_SESSION_ID" -p Type --value)" == wayland ]]
export XDG_CURRENT_DESKTOP=sway XDG_SESSION_DESKTOP=phase3 DESKTOP_SESSION=phase3
export LIBSEAT_BACKEND=logind WLR_RENDERER=pixman WLR_BACKENDS=libinput,drm
export GDK_BACKEND=wayland
# Never fall back to an untested renderer or an alternate display backend.
[[ "$WLR_RENDERER" == pixman && "$WLR_BACKENDS" == libinput,drm ]]
printf 'P3:DESKTOP_USER:1000\n'
state="$HOME/.local/state/phase3"
mkdir -p -- "$state"
[[ -d "$state" && ! -L "$state" ]]
events="$state/events.log"
[[ ! -L "$events" ]]
: > "$events"
tail --lines=+1 --follow=name --retry --sleep-interval=0.1 "$events" &
events_tail_pid=$!
sway_log="$state/sway.log"
[[ ! -L "$sway_log" ]]
: > "$sway_log"
reason=SWAY_START
/usr/bin/sway --verbose --config /etc/phase3/sway.conf > "$sway_log" 2>&1 &
sway_pid=$!
tail --lines=+1 --follow=name --retry --sleep-interval=0.1 "$sway_log" &
sway_tail_pid=$!
reason=SWAY_READY
# Sway selects a wayland-N display dynamically (wayland-1 in Sway 1.10). Read its *freshly
# truncated* verbose log, never assume an output name or take a stale socket.
# Upstream Sway names its IPC socket from its real UID and process PID.
sway_socket="$XDG_RUNTIME_DIR/sway-ipc.1000.$sway_pid.sock"
display_name=
for ((attempt=0;attempt<160;attempt++)); do
    kill -0 "$sway_pid"
    display_name=$(sed -nE "s/.*Running compositor on wayland display '([^']+)'.*/\1/p" "$sway_log" | tail -n 1)
    if [[ "$display_name" =~ ^wayland-[0-9]+$ ]]; then
        display_socket="$XDG_RUNTIME_DIR/$display_name"
        if [[ -S "$sway_socket" && ! -L "$sway_socket" && \
              -S "$display_socket" && ! -L "$display_socket" ]] && \
           [[ "$(stat -c %u "$sway_socket")" == 1000 && \
              "$(stat -c %u "$display_socket")" == 1000 ]] && \
           SWAYSOCK="$sway_socket" timeout 3 /usr/bin/swaymsg -t get_tree -r >/dev/null 2>&1 && \
           WAYLAND_DISPLAY="$display_name" timeout 3 /usr/bin/wayland-info >/dev/null 2>&1; then
            break
        fi
    fi
    sleep 0.25
done
kill -0 "$sway_pid"
[[ "$display_name" =~ ^wayland-[0-9]+$ ]]
display_socket="$XDG_RUNTIME_DIR/$display_name"
[[ -S "$sway_socket" && ! -L "$sway_socket" ]]
[[ -S "$display_socket" && ! -L "$display_socket" ]]
[[ "$(stat -c %u "$sway_socket")" == 1000 && \
   "$(stat -c %u "$display_socket")" == 1000 ]]
SWAYSOCK="$sway_socket" timeout 3 /usr/bin/swaymsg -t get_tree -r >/dev/null
WAYLAND_DISPLAY="$display_name" timeout 3 /usr/bin/wayland-info >/dev/null
[[ "$(awk '/^Uid:/ { print $2 ":" $3 ":" $4 ":" $5 }' "/proc/$sway_pid/status")" == 1000:1000:1000:1000 ]]
printf 'P3:SWAY_READY\n'
# Launch only after the real DRM compositor IPC and Wayland display are usable.
# Unlike a sway.conf exec command this child is supervised; shell disappearance
# is a hard failure, and a compositor reload cannot spawn duplicate chrome.
export SWAYSOCK="$sway_socket" WAYLAND_DISPLAY="$display_name"
reason=SHELL_START
/usr/bin/python3 /usr/lib/phase3/shell.py &
shell_pid=$!
kill -0 "$shell_pid"
reason=SESSION_EXIT
# Both processes must remain alive; a silently missing Dock fails acceptance.
# The actual GtkLayerShell readiness markers come from the shell itself.
wait -n "$sway_pid" "$shell_pid" || true
if ! kill -0 "$shell_pid" 2>/dev/null; then
    reason=SHELL_EXIT
else
    reason=SWAY_EXIT
fi
failed
