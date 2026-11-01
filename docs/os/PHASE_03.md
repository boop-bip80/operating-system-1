<!-- SPDX-License-Identifier: GPL-2.0 -->
# Phase 3 — Original Wayland desktop shell (P3.1 vertical slice)

**Product direction:** a cohesive, original macOS-inspired Linux desktop experience built on this repository's kernel. This is not macOS, is not affiliated with Apple, and does not promise compatibility with macOS applications.

**Status: IMPLEMENTATION IN PROGRESS; NOT QUALIFIED.** The first delivery is **P3.1**, a deliberately bounded, usable original desktop-shell vertical slice for the existing x86-64/QEMU target. No checkbox below is verified merely because an implementation file or this specification is committed. Every source revision needs its own complete acceptance evidence.

**Qualified predecessor:** Phase 2 reference commit [`af6f0137132deb8b6f0d2d7e38584974478eff6a`](https://github.com/boop-bip80/operating-system-1/commit/af6f0137132deb8b6f0d2d7e38584974478eff6a) passed two independent full Phase 2 workflow attempts ([attempt 1](https://github.com/boop-bip80/operating-system-1/actions/runs/41250906242/attempts/1), [attempt 2](https://github.com/boop-bip80/operating-system-1/actions/runs/41250906242/attempts/2)) and its [same-SHA Phase 1 regression](https://github.com/boop-bip80/operating-system-1/actions/runs/41250906247). This qualifies **that exact source revision** only. The Phase 3 implementation commit will have a different SHA and must pass new Phase 1, Phase 2, and Phase 3 qualification on that same SHA before P3.1 is DONE.

## 1. Objective and scope

Replace the stock **user-facing** Weston desktop shell with an original top bar, application launcher, and functional Dock, on a separately built Phase 3 image. Launch and manage real Wayland applications using native input and window-manager state, and prove the result using QEMU's emulated framebuffer, input injection, guest acknowledgments, and persisted data.

The QEMU target remains: x86-64 `q35`, two vCPUs, 2 GiB RAM, TCG (no KVM requirement), OVMF/GRUB boot from a standalone GPT disk, this repository's kernel, a signed-snapshot Debian trixie root filesystem, systemd/logind, virtio DRM/KMS GPU at 1024×768, virtio keyboard/tablet, and persistent ext4 storage. The compositor must run as unprivileged UID/GID 1000 on the real local seat. Any kernel configuration changes needed by the selected compositor are **new explicit test obligations**.

**P3.1 in scope**
- An original, Wayland-native top bar showing the system identity, an accurate clock, and an application-launcher entry point.
- An original Dock with visibly active-app indications, starter launch entries, and live running-window state. Pin/favorite editing is deferred.
- An application launcher populated from the guest's installed `.desktop` files; selecting Terminal must launch a real `foot` window.
- Real keyboard and pointer input, desktop navigation, focus and active-window switching.
- Dock window actions: focus, close, and hide/restore. Under Sway, hiding is implemented honestly via scratchpad behavior; do not mislabel it as generic desktop-window minimization.
- A user-toggled **Compact Dock** preference saved in the ordinary user's home and restored on a second boot.
- Deterministic, source-bound, two-boot graphical acceptance with a failure/negative control.
- A separate, verifiable bootable disk and CI artifact for the Phase 3 variant, without weakening or silently replacing Phase 1/2 acceptance.

**Explicitly deferred:** full Finder/file manager replacement, settings application suite, global macOS menus, smooth animation/polish, accessibility certification, notifications, **automatic shell/compositor crash recovery**, multimedia/audio, GPU acceleration, USB/Wi-Fi, touch, installation/live ISO, secure boot, encryption, suspend, signed updates, bare-metal hardware, production account policy, app-store delivery, and macOS application compatibility. These require their own future phase contracts and evidence; a screenshot is not proof of any of them.

## 2. Integration architecture and non-negotiable boundaries

### 2.1 Compositor and shell

**Selected implementation contract for P3.1:**
- Run **Sway** (wlroots) as the Wayland compositor/window manager, with the **real DRM backend** and **Pixman software rendering**, using the guest's virtio GPU and input devices. The intended explicit environment is `WLR_BACKENDS=libinput,drm` and `WLR_RENDERER=pixman`; verify the active backend/renderer at runtime, not merely the environment settings. The P3.1 compositor is separate from the existing Phase 2 Weston reference; Weston/its Phase 2 image remain intact and independently testable. **Why this architectural split:** Phase 2's stock Weston desktop-shell does not provide the selected wlroots `wlr-layer-shell` surface/zone and Sway JSON IPC contract needed for this independently implemented panel, Dock and app lifecycle.
- Implement the original shell as Python 3/GTK 3/PyGObject clients using **GtkLayerShell** (the supported `wlr-layer-shell` path). The top bar and Dock request compositor exclusive zones; the app launcher is a proper interactive layer-shell surface. **P3-07 needs independent Sway IPC proof that these reservations actually changed the live usable work area** after both GTK layer surfaces mapped. Do **not** assume Weston desktop-shell implements this protocol.
- Source-owned shell files start in `distro/phase3/shell/shell.py` and `distro/phase3/shell/style.css`; the image installs them at `/usr/lib/phase3/shell.py` and `/usr/share/phase3/style.css`. The style and icons must be original or explicitly redistributable.
- Query Sway's JSON IPC (e.g. `swaymsg -t get_tree -r`) for live window identity and active focus, not mock lists or guessed processes. Use compositor-provided container IDs for focus/close/scratchpad operations, never user-supplied titles interpolated into shell commands.
- Launch only installed/verified application entries via desktop-entry-aware APIs (e.g. `Gio.DesktopAppInfo.launch`), with no shell-string concatenation from `Exec` values. The first verified launcher target is `foot` Terminal; additional apps may be added only when their actual package, licensing, and behavior are tested.
- Document exactly how layer-shell surfaces, Sway IPC socket ownership, systemd/PAM/logind session, compositor process lifetime, and child-app lifetimes are coordinated. A visible drawing with no responsive compositor is not a pass.

This architecture is **chosen, not yet proved to run in this QEMU image**. In particular, Sway/wlroots compatibility with this kernel's virtio DRM device, Pixman renderer, logind seat, and the pinned Debian snapshot must be demonstrated in the guest. A missing package/protocol/backend must fail the milestone; do not silently switch to headless, X11, nested Wayland, privileged rendering, or software screenshots.

### 2.2 Build isolation, pins, and safety

- Keep original Phase 2 sources, scripts, rootfs overlay, `.github/workflows/phase2-desktop.yml`, and accepted reference behavior working. Place Phase 3-owned orchestration, shell, tests, and manifest under `distro/phase3/` and generated output under excluded `.build/phase3/`.
- Reuse the repository-built kernel and Phase 2's signed Debian trixie snapshot date `20261001T000000Z` only through an explicitly documented and verified dependency/staging boundary. Derive a **separate** P3 rootfs/image; never modify or test an already-qualified Phase 2 disk as though it were unchanged.
- The initial runtime packages to verify against the pinned signed snapshot include `sway`, `foot`, `python3-gi`, `gir1.2-gtk-3.0`, and `gir1.2-gtklayershell-0.1`, plus required library/runtime dependencies. Record actual package and source-package versions, checksums, Debian InRelease signature validation, and third-party licenses. Package availability and exact name/version resolution are implementation-time gates, not presumed success.
- Root privileges are allowed only for the documented isolated distribution staging/image assembly. No host mounts/loop devices/physical disks, no broad recursive cleanup, no host display/session sharing, no disabling package signature verification, and no passing secrets through artifacts.
- Maintain a locked demo user's password, no `sudo` privilege, no privileged Sway/shell or network-facing control socket. Keep QMP local and private. Record the exact source SHA, dirty state, effective kernel config, toolchain, host and guest package inventories, build-command provenance, and output hashes.
- Give Phase 3 an explicit `inputs.lock`/equivalent and fail closed if repository source, Debian signature, package identity, kernel release, image/manifest, or declared dependency differs. Do not describe changing Ubuntu runner packages as bit-for-bit reproducible.

## 3. Implementation work packages, in dependency order

### P3.1-A — Establish a separate build/verification lane
1. Record the Phase 2 baseline, dependency/artifact SHA hashes, runner and source revision.
2. Add `distro/phase3/Makefile` targets for `check`, `kernel` (or verified reuse), `userspace`, `image`, `manifest`, `test`, `verify`, `artifact`, and `run`, documenting exact privileges and safe outputs.
3. Stage a fresh Phase 3 rootfs and standalone disk without mutating the Phase 2 image. Prove Phase 2's unchanged build/test contract in regression CI.

### P3.1-B — Prove Sway/DRM/Pixman before styling
1. Install the signed pinned compositor/runtime packages and configure a PAM-backed local `desktop` session (UID/GID 1000, active `seat0`/tty1).
2. Start Sway against actual virtio DRM/KMS with Pixman; validate the compositor/backend/renderer and Wayland socket from fresh logs and guest process identities.
3. Prove a native Wayland client draws visible pixels to the emulated GPU, receives QMP-injected keyboard/pointer input, and survives a full desktop session.
4. Add a no-GPU negative boot that fails at the expected guest stage without a headless/nested fallback.

### P3.1-C — Implement original shell and desktop-entry launcher
1. Add testable shell models separating desktop-entry indexing, Sway JSON IPC, Dock/preferences state, and GTK presentation. Unit-test protocol parsing, malformed events, unavailable apps, quoting, and stale window IDs.
2. Draw top bar, Dock, and launcher as native layer-shell clients with proper exclusive zones and focus behavior. Require distinguishable visible surfaces and accessible labeling at the tested resolution. Record and validate fresh `swaymsg -r -t get_outputs` and `swaymsg -r -t get_workspaces` JSON after both bar and Dock map; a compositor-applied workspace rectangle—not merely client `set_exclusive_zone` calls—must prove P3-07.
3. After the launcher actually maps, emit a bounded, deterministic `P3:APP_INDEX` **sample** of canonical eligible desktop-entry IDs observed through guest `Gio.AppInfo`/`Gio.DesktopAppInfo`, with invalid/unavailable entries skipped rather than invented. The source bounds the sample to at most 60 eligible IDs and 1,600 cumulative ASCII ID characters; a distinct displayed-ID subset identifies what the UI actually renders, so this is not a claim to exhaustively enumerate every installed desktop entry. Correlate the record to the visible installed-app list and screenshot, then select Terminal from the original launcher via an actual pointer click. Launch a **new real foot client**, assert its appearance and mapped/focused container in Sway's live tree, and accept real keyboard input.
4. Click a running-app Dock entry to focus its real window. Exercise hide-to-scratchpad, restore, and close; verify independent compositor and process observations rather than trusting button logs.
5. Toggle Compact Dock through the launcher as the unprivileged desktop user, atomically persist the setting in `~/.config/phase3/preferences.json`, reboot the same persistent overlay, and prove the Compact Dock setting and visible state are restored without an init-time rewrite.

### P3.1-D — Acceptance harness and anti-false-positive controls
1. Extend the established QEMU/QMP method in a Phase 3-specific harness. Use a fresh host-generated challenge **after first observed graphical readiness**. Deliver it exclusively by real emulated input, never fw_cfg/kernel parameters/serial/guest-prewritten files.
2. Capture source-bound serial logs, Sway/compositor logs, bounded IPC tree snapshots, QMP JSONL, guest process/UID evidence, and **13 exact GPU PPM+PNG stages per positive boot**: `ready`, `launcher`, `preferences`, `foot`, `saved`, `second`, `focused`, `menu_minimize`, `hidden`, `restored`, `menu_close`, `closed`, `desktop`.
3. Require the screenshots to show nonblank, expected **distinct** stages; correlate screenshot times with actual QMP command acknowledgments, mapped GTK allocations for launcher Terminal/Compact Dock pointer targets, framebuffer-calibrated GTK Dock popup row coordinates, and guest/Sway state. A generated bitmap, guessed fixed pointer target, host desktop screenshot, mocked IPC response, echoed input, or success marker alone cannot pass.
4. Boot the persistent disk a second time with a new firmware-variables copy and a fresh host challenge. Verify both stored preference and prior user data after reboot.
5. Exercise the no-GPU control and at least one shell/compositor-start failure control with guest-stage evidence; rejected controls must not be counted as a healthy desktop. This tests fail-closed behavior, **not** automatic recovery/restart, which is deferred.
6. Bound every boot (initial target: 420 seconds for positive cases, 300 seconds for negative controls), assert clean systemd/guest shutdown, fail for kernel panic/oops or unexpected service failure, and retain diagnostics even when tests fail.

### P3.1-E — CI, provenance, and handover
1. Add `.github/workflows/phase3-desktop.yml` with SHA-pinned actions, read-only permissions, exact event-SHA checkout, a fresh Ubuntu 24.04 x86-64 runner, QEMU TCG, no acceptance cache, bounded build/test time, and artifacts on failure.
2. First use `workflow_dispatch` for integration if needed; before declaring qualification, enable normal push/PR coverage on `rcu-free-v3` and require **two full successful Phase 3 runs or attempts on the same SHA**, both including end-to-end boots, evidence verification, and artifact upload.
3. Require successful complete Phase 1 **and** Phase 2 regression workflows for **that same Phase 3 source SHA**; historical reference successes are not substitutes.
4. Publish a curated `phase3-{sha}-{run_id}-attempt-{attempt}` bundle with disk, firmware as needed, `manifest.json`, `SHA256SUMS`, source/dependency/package/license records, test results bound to the manifest hash, QEMU/IPC/serial logs and framebuffer PNG/PPM captures. Retain for at least the existing 14-day workflow period, and preserve release evidence durably for longer reviews.
5. Update README and this document with exact scoped results, commit SHA, all workflow attempts/artifact identities, and the next unimplemented work. Never mark P3.1 DONE from a green host unit-test job, boot screenshot, incomplete `not_validated` artifact, or another SHA's tests.

## 4. Mandatory P3.1 acceptance register

All verdicts below are **PENDING** until proven on the exact source SHA being reviewed. A passing result must cite the concrete artifact file and/or external workflow URL.

| ID | Mandatory assertion | Minimum concrete evidence | Initial verdict |
| --- | --- | --- | --- |
| P3-01 | Clean exact source, toolchain and dependencies recorded | Source SHA, runner manifest, `inputs.lock`, signed package metadata, guest/host versions | PENDING |
| P3-02 | Phase 1 and Phase 2 regressions pass on the P3 SHA | Complete successful same-SHA workflow runs/artifacts and validators | PENDING |
| P3-03 | A separate disk boots OVMF/GRUB into this repository kernel | GPT/ESP inventory, kernel hashes, QEMU command without `-kernel`/`-initrd`, serial identity | PENDING |
| P3-04 | Rootfs additions come from signed pinned package inputs | Signed InRelease records, source/package inventories, install/build logs and licenses | PENDING |
| P3-05 | Sway and shell run in a valid local non-root seat session | PAM/logind seat and UID 1000 assertions; compositor/child process identities | PENDING |
| P3-06 | Real virtio DRM/KMS with Pixman, not nested/headless/X11 | Fresh Sway renderer/backend logs, DRM device identity, guest Wayland client and QEMU scanout | PENDING |
| P3-07 | Original top bar and Dock reserve real compositor usable area | Both mapped GTK layer surfaces, fresh `get_outputs` full output and `get_workspaces` usable rectangle with bounded 0–4 horizontal/top and 0–12 bottom extra insets, `P3:IPC_LAYOUT` before `P3:LAYER_SHELL_READY`, QMP framebuffer | PENDING |
| P3-08 | Launcher lists real installed desktop entries safely | Bounded canonical `P3:APP_INDEX` eligible-ID sample and displayed-ID subset derived from guest Gio after launcher maps, negative malformed/unsafe-entry tests, matching launcher screenshot and QMP click | PENDING |
| P3-09 | QMP pointer launches a real Terminal app | Source-emitted mapped GTK row allocation, actual QMP pointer transcript, foot PID and new mapped Sway container | PENDING |
| P3-10 | Real keyboard text reaches the focused Terminal | Post-readiness random challenge, native app/guest acknowledgment, input transcript | PENDING |
| P3-11 | Dock focus switching targets actual live windows | Before/after Sway `get_tree` focus state, QMP pointer and framebuffer changes | PENDING |
| P3-12 | Dock hide/scratchpad and restore work; close really closes | Actual Sway IPC actions and tree/process/frame evidence for each transition | PENDING |
| P3-13 | User-toggled Compact Dock persists through second boot | Mapped GTK checkbox/QMP click, owned persisted file, round-2 disk read and smaller Dock icon proven by first-vs-second boot QMP PPM pixel ROI | PENDING |
| P3-14 | Guest remains bounded and shuts down cleanly | Distinct fresh positive boots, total timeouts, QEMU exit and kernel powerdown proof | PENDING |
| P3-15 | GPU and shell-start negative controls reject false desktop success | Guest-stage error markers, complete logs, no positive shell screenshot/markers | PENDING |
| P3-16 | Graphical/IPC evidence is real, correlated, and tamper-detectable | All 13 PPM+PNG stage hashes per positive boot, pixel-calibrated Dock menu targets, QMP/IPC logs, manifest-bound standalone verifier | PENDING |
| P3-17 | Separate Phase 3 artifact is reproducible and reviewable | Image/firmware checksums, installation/launch instructions, license notices | PENDING |
| P3-18 | Two independent complete Phase 3 attempts qualify one SHA | Two fresh-runner green attempts, distinct manifests/artifacts, full end-to-end verdicts | PENDING |

**P3.1 DONE = P3-01 through P3-18 verified for one exact source SHA, with two independent clean Phase 3 attempts, same-SHA Phase 1 and Phase 2 regressions, and reviewed artifacts.** A new commit invalidates the former same-SHA claim until new verification. The root README should report the implementation status and the latest independently qualified revision separately.

## 5. Reviewer procedure and failure contract

From a clean Linux x86-64 checkout of the target SHA, the implemented `distro/phase3/Makefile` and [Phase 3 workflow](https://github.com/boop-bip80/operating-system-1/actions/workflows/phase3-desktop.yml) provide the following executable targets. They require the documented signed Debian inputs, build/QEMU prerequisites and root privileges for userspace/image staging. Their existence, or a successful host-only `check`, **does not qualify the GUI**; `test` must complete the real QEMU scenarios and `verify` must accept their evidence.

```sh
make -C distro/phase3 check
make -C distro/phase3 kernel
sudo make -C distro/phase3 userspace
sudo make -C distro/phase3 image
make -C distro/phase3 manifest
make -C distro/phase3 test
make -C distro/phase3 verify
make -C distro/phase3 artifact
```

**How to interpret native-rendering and IPC proof:** The implementation checks the supervised Sway PID's actual `/proc` environment for `WLR_RENDERER=pixman` and `WLR_BACKENDS=libinput,drm`, plus a fresh wlroots log line matching `[render/pixman/renderer.c:<line>] Creating pixman renderer`; only then does the supervisor emit `P3:PIXMAN_RENDERER_CREATED`. The [pinned Debian wlroots 0.18.2-3 source](https://sources.debian.org/src/wlroots/0.18.2-3/render/wlr_renderer.c/#L565) selects only `wlr_pixman_renderer_create()` for this explicit renderer option, with no GLES/Vulkan fallback. This is **direct, in-guest renderer-initialization log evidence plus source-backed interpretation**, not an independent renderer API query or a standalone proof of visible pixels: P3-06 additionally requires a live non-root Sway PID holding `/dev/dri/card0`, connected output/modeset logs, and independently changing QMP GPU scanout. `P3:IPC_TREE` lines are bounded, ordered JSON **projections of live Sway `get_tree` responses**, carrying container IDs, app IDs, PIDs, focus and scratchpad/visibility state—not unabridged compositor trees. The verifier must correlate these serial snapshots with real QMP input and hash-bound framebuffer files; shell-generated action markers alone cannot pass P3-11/12/16. After a Dock Close, independently require `P3:WINDOW_CLOSED:A` and a matching bound `P3:PROCESS_EXITED:A:<pid>`, then a fresh Sway `get_tree` snapshot showing the surviving B window focused and `P3:WINDOW_ACTIVATED:B`; the shell's successful `focus` command request is never proof without this later compositor state.

**How to interpret low-contrast terminal scanout (P3-09/11):** In a [real, unsuccessful Phase 3 diagnostic attempt](https://github.com/boop-bip80/operating-system-1/actions/runs/41271420047) on source `80916088438aac5d0b228bc443b4a5448695092f`, QEMU rendered a focused `foot` window acknowledged by live Sway IPC, yet the first-window framebuffer oracle rejected it: the wallpaper RGB `(21,36,55)` and terminal interior `(36,36,36)` differ by only 19 in their largest channel, below the former generic `>25` pixel threshold. This **does not qualify P3.1**. For the specifically bounded **first terminal appearance and final desktop return** transitions, require at least **7%** changed samples in the source-defined central GPU screen box using a per-transition maximum-channel delta `>18`. **Only for first terminal appearance**, additionally require at least **0.5%** changed samples with delta `>25` to corroborate higher-contrast window detail. The final desktop-return transition is corroborated by the independent live Sway last-window exit, not an unmeasured second high-contrast quota. Keep the stronger existing stage-specific ROI, popup and preference thresholds unchanged. Replay both tests from hash-bound QMP scanouts in the independent manifest verifier, and require the corresponding real Sway IPC/process and QMP input evidence; changing the threshold cannot turn a host-only/window-marker assertion into a graphical pass.

**How to interpret applied layer-shell layout (P3-07):** A fresh successful `swaymsg -r -t get_outputs` reply establishes the **full** DRM output rectangle, active connector, scale, transform and mode; its `rect` alone says nothing about reserved zones. A separate fresh `swaymsg -r -t get_workspaces` reply establishes the compositor's **usable** visible workspace rectangle. On the prescribed single `Virtual-1` output at 1024×768, scale `1.0`, normal transform, the source-defined top bar anchors top/left/right and **requests a 38-pixel exclusive zone**; the Dock anchors bottom and **requests a 68-pixel exclusive zone**, spanning its 60-pixel surface and 8-pixel margin. These are client requests, **not assertions of pixel-exact final compositor reservation**. A real [diagnostic QEMU run](https://github.com/boop-bip80/operating-system-1/actions/runs/41270106822) on source `4000c331eaa44921500cf897a17b070cfe06b8b8` observed the **compositor-applied** visible workspace `{x:4,y:42,width:1016,height:646}` inside full output `{x:0,y:0,width:1024,height:768}`; this is concrete evidence that Sway's configured `gaps inner 4` and layer-shell placement can inset the workspace beyond nominal exclusive zones, **not a P3.1 qualification pass**. The bounded **P3-07 acceptance** for this fixed single-output QEMU profile is: real positive-size visible workspace fully inside the 1024×768 output; symmetric left/right inset in **`0..4`** logical pixels each (`left = x`, `right = 1024 - (x + width)`, and `left == right`); `top_extra = y - 38` in **`0..4`**; and `bottom_extra = 768 - (y + height) - 68` in **`0..12`**. This preserves proof of at least 38 top-bar and 68 Dock pixels while accounting for bounded compositor gap/margin behavior, without falsely demanding full-width `x=0,width=1024`. Verify a single active `Virtual-1` output and one **visible** workspace whose `output` matches that connector; reject missing, stale, malformed, mismatched, unscaled or out-of-bounds geometry. Only after both GTK layer surfaces map **and** these two live IPC responses validate may the shell emit `P3:IPC_LAYOUT` with bounded normalized output/workspace geometry, **before** `P3:LAYER_SHELL_READY`. The marker remains corroborating evidence subject to screenshot and verifier correlation—not proof by itself. See [Sway's documented IPC rectangle schemas](https://github.com/swaywm/sway/blob/master/sway/sway-ipc.7.scd), [Sway's `arrange_layers` usable-area update](https://github.com/swaywm/sway/blob/master/sway/desktop/layer_shell.c#L1271-L1338), and [wlroots layer-shell exclusivity and margin semantics](https://github.com/swaywm/wlroots/blob/master/protocol/wlr-layer-shell-unstable-v1.xml#L1209-L1278).

**How to interpret the installed-app launcher index (P3-08):** The guest shell must derive `P3:APP_INDEX` from a **bounded sample** of the actual `Gio.AppInfo`/`Gio.DesktopAppInfo` enumeration used to render the launcher, **after the launcher is mapped**, not from a hard-coded fixture, package manifest, host enumeration, or shell-generated success assertion. Keep normalized eligible desktop-entry `get_id()` strings canonical, sorted and unique, with source-defined maxima of 60 eligible IDs and 1,600 cumulative ASCII ID characters; also record the distinct **displayed-ID subset** that actually rendered. Skip malformed/unavailable entries, verify the emitted record's byte bound, and correlate displayed IDs with independently observed launcher pixels and the mapped Terminal row's QMP click. The sample is **not** an exhaustive installed-application inventory, which is separately covered by signed package provenance. Malformed/unsafe desktop-entry host regressions remain mandatory; the marker alone cannot establish safe launch semantics or visible UI.

1. Confirm source, dependency, kernel and package provenance; reject a dirty tree or an unavailable pinned dependency.
2. Select two full successful [Phase 3 workflow](https://github.com/boop-bip80/operating-system-1/actions) runs/attempts for the exact SHA; open each complete artifact and check `SHA256SUMS`, manifest identity and `test-results.json` `pass`. Do not count a `not_validated` diagnostic bundle.
3. Review actual GPU screenshots, QMP keyboard/mouse transcript, Sway IPC state, guest process identities, first/second boot persistence, **both independent no-GPU and shell-start negative controls**, and clean shutdown.
4. Independently confirm same-SHA [Phase 1](https://github.com/boop-bip80/operating-system-1/actions/workflows/phase1-boot.yml) and [Phase 2](https://github.com/boop-bip80/operating-system-1/actions/workflows/phase2-desktop.yml) success.
5. Record immutable links, artifact IDs, checksums, reviewed acceptance rows and any exclusions in the release or review record. Keep lasting evidence beyond automatic artifact expiry.

Failure must be nonzero for missing/unverifiable inputs, inability to boot the repository kernel, wrong kernel/source identity, failed seat ownership, privileged or wrong compositor, missing real DRM/Pixman/layer-shell support, unresponsive input, fake/stale IPC, failure to launch/focus/hide/restore/close the real app, missing/changed screenshot, wrong nonce, lost persisted state, timeout, panic/oops, or unclean shutdown. Preserve diagnostics without turning the failure into a claimed pass.

## 6. After P3.1

Once accepted, choose a separate, explicitly specified milestone for richer desktop UX: native settings and file manager, window decorations and multi-window workflows, design consistency, keyboard shortcuts/search, accessibility, notifications, app install/update, and hardware qualification. Do not expand P3.1's acceptance boundary retroactively to imply those are solved.

See [Phase 1](PHASE_01.md), [Phase 2](PHASE_02.md), [build provenance](BUILD_PROVENANCE.md), and [boot troubleshooting](BOOT_TROUBLESHOOTING.md).
