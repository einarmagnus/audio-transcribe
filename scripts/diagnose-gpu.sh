#!/usr/bin/env bash
# Collect diagnostics for the "whisper-cli + Vulkan froze the screen" incident.
#
# Safe to run: it only reads /sys, /proc, logs and config. It does NOT start
# whisper-cli and does NOT submit any GPU work.
#
# Usage:
#   bash scripts/diagnose-gpu.sh              # user-level info
#   sudo bash scripts/diagnose-gpu.sh         # also kernel logs, debugfs, dumps
#
# Writes a report to the repo root: gpu-diagnosis-<timestamp>.txt
# and (when readable) full logs next to it in gpu-diagnosis-<timestamp>.logs/

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
USER_HOME="$(getent passwd "${SUDO_USER:-$(id -un)}" 2>/dev/null | cut -d: -f6)"
USER_HOME="${USER_HOME:-$HOME}"
OUT="${1:-$REPO/gpu-diagnosis-$STAMP.txt}"
LOG_DIR="$REPO/gpu-diagnosis-$STAMP.logs"
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="$REPO/.tmp/gpu-diagnosis-$STAMP.logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true

# Tee everything to the report as well as the terminal. `tee` is a child of
# this script, so without waiting for it the script returns (and the report
# looks complete) while tee still holds the last few KB in its stdio buffer.
# Close our ends of the pipe so tee sees EOF, then wait for it to drain.
exec > >(tee "$OUT") 2>&1
TEE_PID=$!
trap 'exec 1>&- 2>&-; wait "$TEE_PID" 2>/dev/null' EXIT

section() { printf '\n===== %s =====\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
show() { # show <label> <command...>
    printf -- '--- %s\n' "$1"
    shift
    "$@" 2>&1 | head -200 || true
}

section "Report"
echo "written:  $OUT"
echo "logs dir: $LOG_DIR"
echo "date:     $(date -Is)"
echo "user:     $(id -un) (uid=$(id -u))"
[ "$(id -u)" -eq 0 ] && echo "running as root: yes" || echo "running as root: no (rerun with sudo for kernel logs/debugfs)"

section "System"
uname -a
have nixos-version && nixos-version
echo "--- kernel command line"
cat /proc/cmdline 2>/dev/null
echo "--- os-release"
cat /etc/os-release 2>/dev/null

section "amdgpu module parameters"
if [ -d /sys/module/amdgpu/parameters ]; then
    for p in /sys/module/amdgpu/parameters/*; do
        printf '%s=%s\n' "$(basename "$p")" "$(cat "$p" 2>/dev/null)"
    done
else
    echo "(amdgpu module not loaded)"
fi

section "GPU / DRM devices"
for card in /sys/class/drm/card[0-9]*; do
    [ -e "$card/device/vendor" ] || continue
    echo "--- $card"
    for attr in vendor device subsystem_vendor subsystem_device; do
        [ -r "$card/device/$attr" ] && printf '%s: %s\n' "$attr" "$(cat "$card/device/$attr")"
    done
    [ -L "$card/device/driver" ] && echo "driver: $(basename "$(readlink -f "$card/device/driver")")"
    for attr in mem_info_vram_total mem_info_vram_used mem_info_gtt_total mem_info_gtt_used; do
        [ -r "$card/device/$attr" ] && printf '%s: %s\n' "$attr" "$(cat "$card/device/$attr")"
    done
done
if have lspci; then
    echo "--- lspci"
    lspci -nnk 2>/dev/null | grep -iA3 -E 'vga|3d|display' || true
else
    echo "--- lspci not installed; sysfs PCI class 0x03 devices:"
    for dev in /sys/bus/pci/devices/*; do
        cls="$(cat "$dev/class" 2>/dev/null)"
        case "$cls" in
            0x03*) echo "$dev class=$cls vendor=$(cat "$dev/vendor" 2>/dev/null) device=$(cat "$dev/device" 2>/dev/null)";;
        esac
    done
fi

section "Temperatures / clocks / power"
for hw in /sys/class/drm/card*/device/hwmon/hwmon*; do
    [ -d "$hw" ] || continue
    echo "--- $hw ($(cat "$hw/name" 2>/dev/null))"
    for f in "$hw"/temp*_input "$hw"/freq*_input "$hw"/power*_average; do
        [ -r "$f" ] && printf '%s=%s\n' "$(basename "$f")" "$(cat "$f" 2>/dev/null)"
    done
done

section "Mesa / Vulkan ICD"
echo "--- /run/opengl-driver"
readlink -f /run/opengl-driver 2>/dev/null || echo "(missing)"
echo "--- radv library"
readlink -f /run/opengl-driver/lib/libvulkan_radeon.so 2>/dev/null || echo "(missing)"
for f in /run/opengl-driver/share/vulkan/icd.d/*.json /etc/vulkan/icd.d/*.json; do
    [ -f "$f" ] || continue
    echo "--- $f"
    cat "$f"
done
echo "--- relevant environment"
env | grep -iE '^(VK_|VULKAN|MESA|LIBGL|AMD_|RADV_)' || echo "(none)"
if have vulkaninfo; then
    echo "--- vulkaninfo --summary"
    timeout 30 vulkaninfo --summary 2>&1 | head -80 || true
else
    echo "(vulkaninfo not installed; skipping — safe to ignore)"
fi
echo "note: whisper-cli defaults to -dev 0; on hybrid laptops that is often the"
echo "      integrated display GPU. This app auto-selects the first discrete GPU"
echo "      unless --device N / TRANSCRIBE_DEVICE is given."

journal_section() { # journal_section <label> <journalctl args...>
    local label="$1"
    shift
    echo "--- $label (filtered)"
    journalctl "$@" --no-pager 2>&1 \
        | grep -iE 'amdgpu|drm|gpu|vulkan|radeon|ring |gfx|reset|VM_L2|oom|out of memory|killed process|panic|BUG|segfault|thermal|throttl|iommu' \
        | tail -200 || true
    echo "--- $label (last 40 lines, unfiltered)"
    journalctl "$@" --no-pager 2>&1 | tail -40 || true
}

section "Boots"
if have journalctl; then
    journalctl --list-boots --no-pager 2>&1 | tail -10
else
    echo "(journalctl not installed)"
fi

section "Kernel log — previous boot (the freeze)"
if have journalctl; then
    journalctl -k -b -1 --no-pager >"$LOG_DIR/kernel-boot-minus1.log" 2>"$LOG_DIR/kernel-boot-minus1.err" || true
    if [ -s "$LOG_DIR/kernel-boot-minus1.log" ]; then
        echo "(full log saved to $LOG_DIR/kernel-boot-minus1.log)"
        journal_section "previous boot kernel" -k -b -1
    else
        echo "(unavailable — journal not readable as this user; rerun with sudo)"
        cat "$LOG_DIR/kernel-boot-minus1.err" 2>/dev/null | head -5
    fi
    echo
    echo "--- previous boot warnings/errors"
    journalctl -b -1 -p warning --no-pager 2>&1 | tail -120 || true
else
    echo "(journalctl not installed)"
fi

section "GPU-hang signatures across ALL recorded boots"
if have journalctl; then
    journalctl -k --no-pager >"$LOG_DIR/kernel-all-boots.log" 2>/dev/null || true
    if [ -s "$LOG_DIR/kernel-all-boots.log" ]; then
        echo "(full kernel log saved to $LOG_DIR/kernel-all-boots.log)"
        grep -iE 'ring gfx|GPU reset|gfxoff|VM_L2_PROTECTION_FAULT|amdgpu.*(timeout|failed|error)|amdgpu: GPU recovery' \
            "$LOG_DIR/kernel-all-boots.log" | tail -300 || true
    else
        echo "(unavailable — rerun with sudo)"
    fi
else
    echo "(journalctl not installed)"
fi

section "Kernel log — current boot"
if have journalctl; then
    journal_section "current boot kernel" -k -b 0
    have dmesg && { echo "--- dmesg (last 60)"; dmesg -T 2>&1 | tail -60 || true; }
else
    echo "(journalctl not installed)"
fi

section "GFXOFF state (needs root; bounded by timeout)"
# Reading amdgpu_gfxoff can block when a GPU is wedged — the first version of
# this script hung here. Always bound it.
if [ -d /sys/kernel/debug/dri ]; then
    found=0
    for f in /sys/kernel/debug/dri/*/amdgpu_gfxoff; do
        [ -e "$f" ] || continue
        found=1
        val="$(timeout 3 cat "$f" 2>/dev/null | tr -d '\0' | head -c 64)"
        printf '%s=%s\n' "$f" "${val:-<unreadable/timeout>}"
    done
    [ "$found" -eq 0 ] && echo "(no amdgpu_gfxoff nodes visible; rerun with sudo after: mount -t debugfs none /sys/kernel/debug)"
else
    echo "(debugfs not mounted or not readable)"
fi

section "Failed systemd units"
if have systemctl; then
    systemctl --failed --no-pager 2>&1 | head -40 || true
fi

section "Coredumps / RADV dumps / devcoredump"
if have coredumpctl; then
    coredumpctl list --no-pager --since '-3 days' 2>&1 | tail -30 || true
else
    echo "(coredumpctl not installed)"
fi
ls -la "$USER_HOME"/radv_dumps* 2>/dev/null || true
for f in /sys/class/drm/card*/device/devcoredump/data; do
    [ -e "$f" ] && printf '%s: size=%s bytes\n' "$f" "$(stat -c %s "$f" 2>/dev/null)"
done

section "App / repo state"
echo "--- repo"
ls -la "$REPO" 2>/dev/null | head -40
echo "--- .tmp (test audio / transcripts)"
ls -la "$REPO/.tmp" 2>/dev/null || true
echo "--- recently modified .txt files (possible sidecars)"
find "$REPO" -maxdepth 3 -name '*.txt' -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' 2>/dev/null | sort | tail -20
echo "--- flake.lock nixpkgs revision"
grep -A4 '"nixpkgs"' "$REPO/flake.lock" 2>/dev/null | head -10

section "Please fill in (free text)"
cat <<'EOF'
- Exact time the screen went black:
- Audio file being transcribed (path, duration, format):
- Did the terminal show "transcribe: starting whisper-cli ..." before the freeze?
- Did any transcript segments appear before the freeze?
- Was a .txt sidecar created or updated?
- First run or after several minutes? (model load vs. encode phase)
- Anything else running (game, browser, VM, other GPU app)?
- Did Ctrl+Alt+F3 / SysRq work, or only a hard reset?
EOF

section "Done"
echo "Report: $OUT"
echo "Full logs: $LOG_DIR"
if [ -n "${SUDO_UID:-}" ] && [ -n "${SUDO_GID:-}" ]; then
    chown "$SUDO_UID:$SUDO_GID" "$OUT" 2>/dev/null || true
    chown -R "$SUDO_UID:$SUDO_GID" "$LOG_DIR" 2>/dev/null || true
fi
