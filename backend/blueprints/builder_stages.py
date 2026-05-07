"""
EthOS — Builder Stages
Background worker (_build_image_worker) and x86 wrapper script (_x86_wrapper_script).
Extracted from builder.py to keep the primary blueprint file manageable.
All module-level state is accessed via _builder() to avoid gevent import-lock issues.
"""

import sys
import time
from blueprints.builder_spec import load_spec, spec_to_shell_vars


def _builder():
    """Return the blueprints.builder module without importing it directly."""
    return sys.modules.get('blueprints.builder')


def _build_image_worker(nasos, resume=False):
    """Background worker that runs the x86 image build."""
    from blueprints.builder_resources import enter_build_slice, leave_build_slice
    enter_build_slice()
    try:
        wrapper = _x86_wrapper_script(nasos)
        if resume:
            wrapper = 'export ETHOS_RESUME=1\n' + wrapper

        start_time = time.time()
        result_info = {}

        for line in _builder()._host_run_stream(wrapper, track_pid=True):
            line = line.rstrip('\n')
            if line.startswith('__EXIT_CODE__:'):
                code = int(line.split(':')[1])
                elapsed = time.time() - start_time
                elapsed_m = int(elapsed // 60)
                elapsed_s = int(elapsed % 60)
                if code == 0:
                    img_size = _builder()._human_size(int(result_info.get('img_size', 0)))
                    msg = f'Image ready! IMG: {img_size}'
                    msg += f' (czas: {elapsed_m}min {elapsed_s}s)'
                    beacon_id = f"build-{int(start_time)}"
                    res = {
                        'success': True, 'message': msg,
                        'img': result_info.get('img_path', ''),
                        'beacon_id': beacon_id,
                    }
                    with _builder()._build_lock:
                        _builder()._build_state['beacon_id'] = beacon_id
                    _builder()._update_build(status='done', percent=100, message=msg, result=res)
                else:
                    msg = f'Image build error (code: {code}, time: {elapsed_m}min {elapsed_s}s)'
                    _builder()._update_build(status='error', message=msg, result={'success': False, 'message': msg})
            elif line.startswith('STEP:'):
                parts = line.split(':', 2)
                pct = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                msg = parts[2] if len(parts) > 2 else ''
                _builder()._update_build(percent=pct, message=msg)
            elif line.startswith('RESULT_IMG:'):
                p = line.split(':')
                result_info['img_path'] = p[1] if len(p) > 1 else ''
                result_info['img_size'] = p[2] if len(p) > 2 else '0'
            elif line.startswith('LOG:RESUME_AVAILABLE:'):
                build_dir = line[len('LOG:RESUME_AVAILABLE:'):]
                with _builder()._build_lock:
                    _builder()._build_state['resume_available'] = True
                    _builder()._build_state['build_dir'] = build_dir.strip()
                    _builder()._save_build_state()
            elif line.startswith('PREFLIGHT_RESULT:'):
                pf_res = line[len('PREFLIGHT_RESULT:'):].strip()
                with _builder()._build_lock:
                    _builder()._build_state['preflight_result'] = pf_res
                    _builder()._save_build_state()
                _builder()._update_build(log=f'Pre-flight result: {pf_res}')
            elif line.startswith('LOG:'):
                msg = line[4:]
                _builder()._update_build(log=msg)
            elif line.strip():
                _builder()._update_build(log=line)
    except Exception as e:
        msg = f'Exception: {e}'
        _builder()._update_build(status='error', message=msg, result={'success': False, 'message': msg})
    finally:
        leave_build_slice()



# ─────────────────────────────────────────────────────────
#  x86 wrapper script — debootstrap + GRUB
# ─────────────────────────────────────────────────────────

def _x86_wrapper_script(nasos: str) -> str:
    """Return bash wrapper script for building x86 image."""
    _opt_js, _opt_py = _builder()._get_optional_files()
    optional_js_list = ' '.join(_opt_js)
    optional_py_list = ' '.join(_opt_py)

    # Load declarative build spec
    spec = load_spec()
    spec_vars = spec_to_shell_vars(spec)

    return rf"""
set -e
set -o pipefail
export DEBIAN_FRONTEND=noninteractive

NASOS="{nasos}"

# ── Declarative build spec (from data/build-spec.yaml) ──
{spec_vars}

# Check dependencies
echo "STEP:2:Checking dependencies..."
for cmd in debootstrap parted mkfs.ext4 mkfs.vfat grub-install; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "STEP:3:Installing dependencies..."
        apt-get update -qq
        apt-get install -y -qq debootstrap parted dosfstools e2fsprogs \\
            grub-efi-amd64-bin grub-common grub2-common \\
            mtools xorriso isolinux debian-archive-keyring squashfs-tools zstd cryptsetup-bin 2>/dev/null || true
        break
    fi
done

# Ensure the appropriate archive keyring is present
if [ "$BASE_DISTRO" = "ubuntu" ]; then
    # Ubuntu debootstrap uses --no-check-gpg since ubuntu-keyring may be unavailable on Debian hosts
    echo "LOG:Ubuntu build — skipping keyring check (using --no-check-gpg for debootstrap)"
else
    if [ ! -f /usr/share/keyrings/debian-archive-keyring.gpg ]; then
        echo "LOG:Installing debian-archive-keyring..."
        apt-get update -qq 2>/dev/null
        apt-get install -y -qq debian-archive-keyring 2>/dev/null || true
    fi
fi

echo "STEP:5:Preparing environment..."

# Version from version.json (not overridden by spec)
VERSION=$(python3 -c "import json; print(json.load(open('$NASOS/backend/version.json'))['version'])" 2>/dev/null || echo '2.4.0')
FINAL_IMG="$NASOS/installer/images/ethos-x86.img"
WORK_DIR="/tmp/ethos-x86-build-web"

# ── Clean up stale loop devices / mounts from previous failed builds ──
# On-disk builds that fail leave loop devices attached to (deleted) img files,
# each holding ~8GB of phantom space on tmpfs. Clean them all before starting.
_cleanup_stale_loops() {{
    local _img="$WORK_DIR/ethos-x86.img"
    # Unmount any stacked mounts on WORK_DIR/root
    while mountpoint -q "$WORK_DIR/root" 2>/dev/null; do
        umount "$WORK_DIR/root" 2>/dev/null || umount -l "$WORK_DIR/root" 2>/dev/null || break
    done
    umount "$WORK_DIR/efi" 2>/dev/null || true
    # Detach all loop devices referencing our img (including deleted inodes)
    losetup -a 2>/dev/null | {{ grep "ethos-x86-build-web" || true; }} | cut -d: -f1 | while read _ld; do
        losetup -d "$_ld" 2>/dev/null || true
    done
    # If WORK_DIR was a tmpfs mount from prior build, unmount it
    if mountpoint -q "$WORK_DIR" 2>/dev/null; then
        umount "$WORK_DIR" 2>/dev/null || umount -l "$WORK_DIR" 2>/dev/null || true
    fi
    # Remove stale files
    rm -rf "$WORK_DIR" 2>/dev/null || true
}}
_cleanup_stale_loops
echo "LOG:Stale build artifacts cleaned"

# ── Performance: use tmpfs (RAM) for build if enough memory ──
# VM-aware: subtract RAM already used by running QEMU processes.
MEM_AVAIL_MB=$(awk '/MemAvailable/{{print int($2/1024)}}' /proc/meminfo 2>/dev/null || echo 0)
VM_RAM_MB=$(ps -eo rss,comm --no-headers 2>/dev/null | awk '/qemu/{{s+=$1}} END{{print int(s/1024)}}')
VM_RAM_MB=${{VM_RAM_MB:-0}}
EFFECTIVE_RAM_MB=$(( MEM_AVAIL_MB - VM_RAM_MB - 512 ))
USE_TMPFS=0
if [ "$EFFECTIVE_RAM_MB" -gt "$TMPFS_MIN_RAM_MB" ]; then
    USE_TMPFS=1
    echo "LOG:RAM: avail=${{MEM_AVAIL_MB}}MB vms=${{VM_RAM_MB}}MB effective=${{EFFECTIVE_RAM_MB}}MB — building in tmpfs"
    mkdir -p "$WORK_DIR"
    mount -t tmpfs -o size=${{IMG_SIZE_GB}}G,nr_inodes=0 tmpfs "$WORK_DIR"
else
    echo "LOG:RAM: avail=${{MEM_AVAIL_MB}}MB vms=${{VM_RAM_MB}}MB effective=${{EFFECTIVE_RAM_MB}}MB — building on disk"
    mkdir -p "$WORK_DIR"
fi
OUTPUT_IMG="$WORK_DIR/ethos-x86.img"
CKPT_DIR="$WORK_DIR/.ckpts"
BUILD_DONE=0

# ── Stage checkpoint helpers (idempotent builds) ──
_ckpt_done() {{ [ -f "$CKPT_DIR/$1" ]; }}
_ckpt_set()  {{ mkdir -p "$CKPT_DIR"; touch "$CKPT_DIR/$1"; echo "LOG:✓ Stage checkpoint: $1"; }}

RESUME_MODE="${{ETHOS_RESUME:-0}}"
if [ "$RESUME_MODE" = "1" ] && [ -d "$CKPT_DIR" ]; then
    echo "LOG:Resume mode — existing checkpoints: $(ls "$CKPT_DIR/" 2>/dev/null | tr '\n' ' ')"
fi

# ── Build cache directories (persist across builds) ──
DEBOOTSTRAP_CACHE="/var/cache/ethos-builder/debootstrap"
APT_CACHE="/var/cache/ethos-builder/apt"
mkdir -p "$DEBOOTSTRAP_CACHE" "$APT_CACHE"

# Cleanup function
cleanup() {{
    sync 2>/dev/null || true
    # Unmount in reverse order, use lazy unmount for stubborn mounts
    for m in var/cache/apt/archives boot/efi run sys proc dev/shm dev/pts dev; do
        umount "$WORK_DIR/root/$m" 2>/dev/null || \
            umount -l "$WORK_DIR/root/$m" 2>/dev/null || true
    done
    sleep 1
    # Unmount all stacked root mounts (previous builds may leave multiple)
    while mountpoint -q "$WORK_DIR/root" 2>/dev/null; do
        umount "$WORK_DIR/root" 2>/dev/null || \
            umount -l "$WORK_DIR/root" 2>/dev/null || break
    done
    umount "$WORK_DIR/efi" 2>/dev/null || true
    sleep 1
    # Detach ALL loop devices associated with our build image (not just $LOOP_DEV)
    losetup -a 2>/dev/null | {{ grep "ethos-x86-build-web" || true; }} | cut -d: -f1 | while read _ld; do
        losetup -d "$_ld" 2>/dev/null || true
    done
    if [ "$BUILD_DONE" = "1" ]; then
        # Success — clean up completely
        if [ "$USE_TMPFS" -eq 1 ] && mountpoint -q "$WORK_DIR" 2>/dev/null; then
            umount "$WORK_DIR" 2>/dev/null || \
                umount -l "$WORK_DIR" 2>/dev/null || true
        fi
        rm -rf "$WORK_DIR" 2>/dev/null || true
    else
        # On-disk failure: always clean up (resume from disk wastes /tmp space)
        # Tmpfs failure: preserve for quick in-session resume (vanishes on reboot)
        if [ "$USE_TMPFS" -eq 1 ]; then
            echo "LOG:RESUME_AVAILABLE:$WORK_DIR"
            echo "LOG:Note: Build dir is in tmpfs — checkpoints survive crash but not reboot"
        else
            echo "LOG:Build failed — cleaning on-disk artifacts to free /tmp"
            rm -rf "$WORK_DIR" 2>/dev/null || true
        fi
    fi
}}
trap cleanup EXIT

# ── Re-mount helper (used when resuming from checkpoint) ──
_remount_for_resume() {{
    echo "LOG:Remounting build artifacts for resume..."
    mkdir -p "$WORK_DIR/root" "$WORK_DIR/efi"
    LOOP_DEV=$(losetup --find --show --partscan "$OUTPUT_IMG" 2>/dev/null) || {{
        echo "LOG:ERROR: Cannot attach loop device to $OUTPUT_IMG"; exit 1;
    }}
    echo "LOG:Loop device: $LOOP_DEV"
    mount "${{LOOP_DEV}}p2" "$WORK_DIR/root" || {{ echo "LOG:ERROR: Cannot mount root partition"; exit 1; }}
    mkdir -p "$WORK_DIR/root/boot/efi"
    mount "${{LOOP_DEV}}p1" "$WORK_DIR/root/boot/efi" 2>/dev/null || true
    echo "LOG:Disk remounted for resume"
}}

# ── Step 1: Create disk image ──
if _ckpt_done "01_disk"; then
    echo "STEP:8:Resuming — disk image exists"
    _remount_for_resume
    echo "STEP:14:Obraz dysku (z checkpointa)"
else
    echo "STEP:8:Creating disk image (${{IMG_SIZE_GB}}GB)..."
    mkdir -p "$WORK_DIR"/{{root,efi}}
    rm -f "$OUTPUT_IMG" "$FINAL_IMG"
    truncate -s "${{IMG_SIZE_GB}}G" "$OUTPUT_IMG"

    LOOP_DEV=$(losetup --find --show --partscan "$OUTPUT_IMG")
    echo "LOG:Loop device: $LOOP_DEV"

    parted -s "$LOOP_DEV" mklabel gpt
    parted -s "$LOOP_DEV" mkpart ESP fat32 1MiB ${{ESP_SIZE_MB}}MiB
    parted -s "$LOOP_DEV" set 1 esp on
    parted -s "$LOOP_DEV" mkpart primary ext4 ${{ESP_SIZE_MB}}MiB $((${{ESP_SIZE_MB}} + ${{ROOT_SIZE_MB}}))MiB
    partprobe "$LOOP_DEV"
    # Wait for partition devices to appear (up to 10s, 0.5s steps)
    for _pnum in 1 2; do
        _waited=0
        while [ ! -b "${{LOOP_DEV}}p${{_pnum}}" ] && [ $_waited -lt 20 ]; do
            sleep 0.5; _waited=$((_waited + 1))
        done
        if [ ! -b "${{LOOP_DEV}}p${{_pnum}}" ]; then
            echo "LOG:ERROR: Partition ${{LOOP_DEV}}p${{_pnum}} not ready after 10s"
            exit 1
        fi
    done

    mkfs.vfat -F32 "${{LOOP_DEV}}p1"
    mkfs.ext4 -q -L "ethos-root" "${{LOOP_DEV}}p2"

    mount "${{LOOP_DEV}}p2" "$WORK_DIR/root"
    mkdir -p "$WORK_DIR/root/boot/efi"
    mount "${{LOOP_DEV}}p1" "$WORK_DIR/root/boot/efi"

    echo "STEP:14:Obraz dysku utworzony"
    _ckpt_set "01_disk"
fi

# ── Step 2: Debootstrap ──
if _ckpt_done "02_debootstrap"; then
    echo "STEP:45:${{BASE_DISTRO^}} base system present (checkpoint — skipped)"
else
    if [ "$BASE_DISTRO" = "ubuntu" ]; then
        echo "STEP:15:Debootstrap — minimal Ubuntu install (this will take a few minutes)..."
        DEBOOTSTRAP_MIRROR="http://archive.ubuntu.com/ubuntu/"
        DEBOOTSTRAP_EXTRA_OPTS="--no-check-gpg --components=main,restricted,universe"
    else
        echo "STEP:15:Debootstrap — minimal Debian install (this will take a few minutes)..."
        DEBOOTSTRAP_MIRROR="http://deb.debian.org/debian"
        DEBOOTSTRAP_EXTRA_OPTS=""
    fi
    PKG_COUNT=0
    if [ -d "$DEBOOTSTRAP_CACHE" ] && [ "$(ls -A "$DEBOOTSTRAP_CACHE" 2>/dev/null)" ]; then
        echo "LOG:Using debootstrap cache ($(du -sh "$DEBOOTSTRAP_CACHE" | cut -f1))"
    fi
    DEBS_LOG="/tmp/ethos-debootstrap-$$.log"
    debootstrap --cache-dir="$DEBOOTSTRAP_CACHE" --variant=minbase $DEBOOTSTRAP_EXTRA_OPTS --include=\\
$DEBOOTSTRAP_INCLUDE \\
        "$DEBIAN_RELEASE" "$WORK_DIR/root" "$DEBOOTSTRAP_MIRROR" 2>&1 | \\
        tee "$DEBS_LOG" | \\
        while IFS= read -r line; do
            if echo "$line" | grep -qE "^I: Retrieving"; then
                PKG_COUNT=$((PKG_COUNT + 1))
                if (( PKG_COUNT % 20 == 0 )); then
                    echo "LOG:Downloading packages... ($PKG_COUNT downloaded)"
                fi
            elif echo "$line" | grep -qE "^I: Validating"; then
                echo "LOG:$line"
            elif echo "$line" | grep -qE "^I: Extracting"; then
                PKG_COUNT=$((PKG_COUNT + 1))
                if (( PKG_COUNT % 30 == 0 )); then
                    echo "LOG:Extracting... ($PKG_COUNT)"
                fi
            elif echo "$line" | grep -qE "^I: Unpacking|^I: Configuring"; then
                echo "LOG:$line"
            elif echo "$line" | grep -qE "^I: |^W: |^E: "; then
                echo "LOG:$line"
            fi
        done
    DEBS_RC=${{PIPESTATUS[0]}}
    if [ "$DEBS_RC" -ne 0 ]; then
        echo "LOG:ERROR: debootstrap failed (exit $DEBS_RC)"
        tail -5 "$DEBS_LOG" 2>/dev/null | while IFS= read -r _l; do echo "LOG:DEBS> $_l"; done
        rm -f "$DEBS_LOG"
        echo "STEP:45:Debootstrap failed"
        exit 1
    fi
    rm -f "$DEBS_LOG"

    # Verify debootstrap succeeded
    if [ ! -d "$WORK_DIR/root/dev" ] || [ ! -d "$WORK_DIR/root/etc" ]; then
        echo "LOG:ERROR: debootstrap did not create rootfs — check logs"
        echo "STEP:45:Debootstrap failed"
        exit 1
    fi

    echo "STEP:45:${{BASE_DISTRO^}} installed. Configuring system..."
    _ckpt_set "02_debootstrap"
fi

# ── Step 3: Configure system ──
ROOT="$WORK_DIR/root"
echo "LOG:Bind mount /dev, /proc, /sys, /run..."
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount --bind /dev/shm "$ROOT/dev/shm" 2>/dev/null || true
mount -t proc proc "$ROOT/proc"
mount -t sysfs sysfs "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/run"

# Bind-mount apt cache for faster rebuilds
mkdir -p "$ROOT/var/cache/apt/archives"
mount --bind "$APT_CACHE" "$ROOT/var/cache/apt/archives"
echo "LOG:Apt cache bind-mounted ($(du -sh "$APT_CACHE" 2>/dev/null | cut -f1) cached)"

# DNS for chroot — essential for apt-get
# Host resolv.conf may be systemd-resolved stub (127.0.0.53) which won't work in chroot
if [ -f /run/systemd/resolve/resolv.conf ]; then
    cp /run/systemd/resolve/resolv.conf "$ROOT/etc/resolv.conf"
    echo "LOG:DNS: copied resolv.conf from host"
else
    echo "nameserver 8.8.8.8" > "$ROOT/etc/resolv.conf"
    echo "nameserver 1.1.1.1" >> "$ROOT/etc/resolv.conf"
    echo "LOG:DNS: using 8.8.8.8 / 1.1.1.1"
fi

# Fix any broken packages left by debootstrap (polkitd etc.)
echo "LOG:Fixing packages after debootstrap..."
chroot "$ROOT" dpkg --configure -a 2>&1 | tail -3 || true
chroot "$ROOT" bash -c 'DEBIAN_FRONTEND=noninteractive apt --fix-broken install -y' 2>&1 | tail -3 || true
echo "LOG:Packages fixed"

# Install network-manager in chroot (needs systemd bind-mounts for polkitd)
echo "LOG:Installing network-manager in chroot..."
chroot "$ROOT" apt-get update -qq 2>&1 | tail -3 || true
chroot "$ROOT" bash -c 'DEBIAN_FRONTEND=noninteractive apt-get install -y -qq network-manager dbus-user-session' 2>&1 | tail -5 || echo "LOG:network-manager install issue"

echo "LOG:Creating fstab, hostname, locale..."
ROOT_UUID=$(blkid -s UUID -o value "${{LOOP_DEV}}p2")
EFI_UUID=$(blkid -s UUID -o value "${{LOOP_DEV}}p1")

if [ -z "$ROOT_UUID" ]; then
    echo "LOG:ERROR: Failed to read root partition UUID (${{LOOP_DEV}}p2)"
    exit 1
fi
if [ -z "$EFI_UUID" ]; then
    echo "LOG:ERROR: Failed to read EFI partition UUID (${{LOOP_DEV}}p1)"
    exit 1
fi

cat > "$ROOT/etc/fstab" <<FSTAB
UUID=$ROOT_UUID  /          ext4  noatime,errors=remount-ro  0 1
UUID=$EFI_UUID   /boot/efi  vfat  umask=0077         0 1
FSTAB

echo "$DEFAULT_HOSTNAME" > "$ROOT/etc/hostname"

# NOTE: Swap is intentionally NOT created in the build image.
# The installer generates its own fstab with swap on the data partition.
# Keeping the root partition small allows dd-based fast cloning.

# ── I/O tuning for low-power NAS hardware ──
echo "LOG:Konfiguracja I/O tuning..."
cat > "$ROOT/etc/sysctl.d/90-ethos-nas.conf" <<'IOTUNE'
# EthOS NAS Tuning
vm.swappiness = 10
vm.dirty_ratio = 40
vm.dirty_background_ratio = 10
vm.vfs_cache_pressure = 50
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
vm.min_free_kbytes = 65536
vm.dirty_expire_centisecs = 1500
vm.dirty_writeback_centisecs = 1500
kernel.nmi_watchdog = 0
net.ipv4.ip_forward = 1
IOTUNE

# ── Security hardening: sysctl ──
cat > "$ROOT/etc/sysctl.d/91-ethos-security.conf" <<'SECSYSCTL'
# Kernel pointer hardening
kernel.kptr_restrict = 2
kernel.dmesg_restrict = 1
kernel.perf_event_paranoid = 3
kernel.unprivileged_bpf_disabled = 1
# Reverse-path filtering (spoofing protection)
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1
# SYN flood protection
net.ipv4.tcp_syncookies = 1
# Disable ICMP redirects
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.send_redirects = 0
# Disable source routing
net.ipv4.conf.all.accept_source_route = 0
net.ipv6.conf.all.accept_source_route = 0
SECSYSCTL

# ── Security: kernel module blacklist ──
cat > "$ROOT/etc/modprobe.d/ethos-security-blacklist.conf" <<'MODBLK'
# Disable DMA-capable bus interfaces (potential physical attack vectors)
blacklist firewire-core
blacklist thunderbolt
# Disable uncommon/legacy filesystems (attack surface reduction)
blacklist cramfs
blacklist freevxfs
blacklist jffs2
blacklist hfs
blacklist hfsplus
blacklist udf
install cramfs /bin/true
install freevxfs /bin/true
install jffs2 /bin/true
install hfs /bin/true
install hfsplus /bin/true
install udf /bin/true
# Disable uncommon network protocols
blacklist dccp
blacklist sctp
blacklist rds
blacklist tipc
install dccp /bin/true
install sctp /bin/true
install rds /bin/true
install tipc /bin/true
MODBLK

cat > "$ROOT/etc/udev/rules.d/99-ethos-power.rules" <<'UDEV_PWR'
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="1", RUN+="/sbin/hdparm -S 242 /dev/%k"
UDEV_PWR

cat > "$ROOT/etc/udev/rules.d/99-ethos-readahead.rules" <<'UDEV'
SUBSYSTEM=="block", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="1", RUN+="/sbin/blockdev --setra 4096 /dev/%k"
SUBSYSTEM=="block", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="0", RUN+="/sbin/blockdev --setra 256 /dev/%k"
SUBSYSTEM=="block", KERNEL=="nvme*", RUN+="/sbin/blockdev --setra 256 /dev/%k"
UDEV

# I/O scheduler: BFQ for HDD (better for mixed workloads), none for NVMe
cat > "$ROOT/etc/udev/rules.d/60-ethos-scheduler.rules" <<'UDEV_SCHED'
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="1", ATTR{{queue/scheduler}}="bfq"
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{{queue/rotational}}=="0", ATTR{{queue/scheduler}}="none"
ACTION=="add|change", KERNEL=="nvme[0-9]*n[0-9]*", TEST=="queue/scheduler", ATTR{{queue/scheduler}}="none"
UDEV_SCHED

# Logrotate policy for EthOS logs
cat > "$ROOT/etc/logrotate.d/ethos" <<'LOGROTATE'
/opt/ethos/logs/*.log {{
    weekly
    rotate 4
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    maxsize 50M
}}

/opt/ethos/logs/copilot_tickets/*.log {{
    monthly
    rotate 2
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    maxsize 100M
}}
LOGROTATE

# ── Persistent journald (survive reboots — critical for NAS debugging) ──
mkdir -p "$ROOT/etc/systemd/journald.conf.d"
cat > "$ROOT/etc/systemd/journald.conf.d/ethos.conf" <<'JOURNALD'
[Journal]
Storage=persistent
SystemMaxUse=100M
SystemKeepFree=200M
MaxRetentionSec=2week
Compress=yes
JOURNALD
mkdir -p "$ROOT/var/log/journal"

cat > "$ROOT/etc/hosts" <<HOSTS
127.0.0.1   localhost
127.0.1.1   $DEFAULT_HOSTNAME
::1         localhost ip6-localhost ip6-loopback
HOSTS

echo "en_US.UTF-8 UTF-8" > "$ROOT/etc/locale.gen"
echo "pl_PL.UTF-8 UTF-8" >> "$ROOT/etc/locale.gen"
chroot "$ROOT" locale-gen >/dev/null 2>&1
echo 'LANG=en_US.UTF-8' > "$ROOT/etc/default/locale"
ln -sf /usr/share/zoneinfo/Europe/Warsaw "$ROOT/etc/localtime"

if [ "$BASE_DISTRO" = "ubuntu" ]; then
cat > "$ROOT/etc/apt/sources.list" <<APT
deb http://archive.ubuntu.com/ubuntu $DEBIAN_RELEASE main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu $DEBIAN_RELEASE-updates main restricted universe multiverse
deb http://security.ubuntu.com/ubuntu $DEBIAN_RELEASE-security main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu $DEBIAN_RELEASE-backports main restricted universe multiverse
APT
else
cat > "$ROOT/etc/apt/sources.list" <<APT
deb http://deb.debian.org/debian $DEBIAN_RELEASE main contrib non-free non-free-firmware
deb http://deb.debian.org/debian $DEBIAN_RELEASE-updates main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security $DEBIAN_RELEASE-security main contrib non-free non-free-firmware
deb http://deb.debian.org/debian $DEBIAN_RELEASE-backports main contrib non-free non-free-firmware
APT
fi

echo "LOG:Creating user $DEFAULT_USER..."
PASS_HASH=$(openssl passwd -6 "$USER_PASS")
chroot "$ROOT" useradd -m -s /bin/bash -G sudo -p "$PASS_HASH" "$DEFAULT_USER"
ALLOWED_CMDS="/opt/ethos/tools/ethos-system-helper.sh, /opt/ethos/tools/ethos-power-*, /usr/bin/systemctl restart ethos, /usr/sbin/smartctl, /usr/bin/docker"
echo "${{DEFAULT_USER}} ALL=(ALL) NOPASSWD: ${{ALLOWED_CMDS}}" > "$ROOT/etc/sudoers.d/010_ethos"
chmod 440 "$ROOT/etc/sudoers.d/010_ethos"
chroot "$ROOT" groupadd -f ethos-admin
chroot "$ROOT" groupadd -f ethos-user
chroot "$ROOT" usermod -aG ethos-admin,ethos-user "$DEFAULT_USER"
chroot "$ROOT" systemctl enable ssh || true
chroot "$ROOT" systemctl enable smartmontools || true
chroot "$ROOT" systemctl enable nut-server || true
chroot "$ROOT" systemctl enable NetworkManager || true

# ── SMART Monitoring Configuration ──
echo "LOG:Konfiguracja SMART Monitoring..."
cat > "$ROOT/etc/smartd.conf" <<'EOF'
DEVICESCAN -a -o on -S on -n standby,q -s (S/../../7/02|L/../01/./03) -W 4,50,55 -R 199 -m root -M exec /etc/smartmontools/run.d/ethos-notify
EOF

mkdir -p "$ROOT/etc/smartmontools/run.d"
cat > "$ROOT/etc/smartmontools/run.d/ethos-notify" <<'EOF'
#!/bin/bash
# EthOS S.M.A.R.T. Alert Hook
# Triggered by smartd on disk issues

API_URL="http://localhost:9000/api"

# Log to EventLog
if [ -n "$SMARTD_MESSAGE" ]; then
    curl -s -X POST "$API_URL/eventlog" \
      -H "Content-Type: application/json" \
      -d "{{
        \"category\": \"storage\",
        \"level\": \"warning\",
        \"message\": \"SMART Alert: $SMARTD_DEVICE\",
        \"detail\": {{
            \"device\": \"$SMARTD_DEVICE\",
            \"message\": \"$SMARTD_MESSAGE\",
            \"failtype\": \"$SMARTD_FAILTYPE\",
            \"full_message\": \"$SMARTD_FULLMESSAGE\"
        }}
      }}"
fi

# Trigger backup on critical attributes
# Reallocated, Pending, Uncorrectable, or failure
DO_BACKUP=0

case "$SMARTD_MESSAGE" in
    *Reallocated_Sector_Ct*|*Current_Pending_Sector*|*Offline_Uncorrectable*)
        DO_BACKUP=1
        ;;
    *UDMA_CRC_Error_Count*)
        # Just log, don't trigger panic backup for cable errors
        ;;
esac

if [ -n "$SMARTD_FAILTYPE" ] && [ "$SMARTD_FAILTYPE" != "EmailTest" ]; then
    DO_BACKUP=1
fi

if [ "$DO_BACKUP" -eq 1 ]; then
    curl -s -X POST "$API_URL/backup/trigger-smart" \
      -H "Content-Type: application/json" \
      -d "{{}}"
fi
EOF
chmod +x "$ROOT/etc/smartmontools/run.d/ethos-notify"

chroot "$ROOT" systemctl disable networking 2>/dev/null || true
chroot "$ROOT" systemctl enable avahi-daemon 2>/dev/null || true
chroot "$ROOT" systemctl enable serial-getty@ttyS0.service 2>/dev/null || true

# ── Fail2Ban Configuration ──
echo "LOG:Konfiguracja Fail2Ban (SSH, Samba, Web)..."
cat > "$ROOT/etc/fail2ban/jail.local" <<'F2B'
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5
backend = systemd
ignoreip = 127.0.0.1/8 ::1 192.168.0.0/16 10.0.0.0/8
# Persistent database
dbfile = /opt/ethos/data/fail2ban.sqlite3
dbpurgeage = 86400
action = %(action_)s
         ethos-eventlog

[sshd]
enabled = true

[samba]
enabled = true
port = 139,445
filter = samba
logpath = /var/log/samba/log.*
backend = auto

[ethos-web]
enabled = true
port = 9000
filter = ethos-web
logpath = /opt/ethos/logs/access.log
backend = auto
F2B

mkdir -p "$ROOT/etc/fail2ban/action.d"
cat > "$ROOT/etc/fail2ban/action.d/ethos-eventlog.conf" <<'ACT'
[Definition]
actionban = /opt/ethos/tools/fail2ban_eventlog.py <name> <ip> <failures>
ACT

mkdir -p "$ROOT/etc/fail2ban/filter.d"
cat > "$ROOT/etc/fail2ban/filter.d/ethos-web.conf" <<'WEB'
[Definition]
failregex = ^<HOST> - - \[.*\] ".*" (401|403) .*$
ignoreregex =
WEB

chroot "$ROOT" systemctl enable fail2ban || true

# ── UFW Firewall ──
echo "LOG:Configuring UFW firewall..."
LAN="192.168.0.0/16"
chroot "$ROOT" bash -c 'command -v ufw &>/dev/null || apt-get install -y -qq ufw' 2>&1 | tail -3
chroot "$ROOT" ufw default deny incoming 2>/dev/null || true
chroot "$ROOT" ufw default allow outgoing 2>/dev/null || true
chroot "$ROOT" ufw allow from $LAN to any port 22 proto tcp comment 'SSH' 2>/dev/null || true
chroot "$ROOT" ufw allow from $LAN to any port 9000 proto tcp comment 'EthOS Web UI' 2>/dev/null || true
chroot "$ROOT" ufw allow from $LAN to any port 80,443 proto tcp comment 'HTTP / HTTPS' 2>/dev/null || true
# NOTE: Do NOT enable UFW here — during installer mode (USB boot) there is
# no firewall needed (open hotspot). UFW is enabled by the installer when
# it writes the system to the target disk (system_ops.configure_services).
# Enabling UFW in chroot can also produce broken iptables state.
echo "LOG:UFW rules configured (will be enabled after installation)"

# ── SSH Hardening ──
echo "LOG:SSH hardening..."
mkdir -p "$ROOT/etc/ssh/sshd_config.d"
cat > "$ROOT/etc/ssh/sshd_config.d/ethos-hardening.conf" <<'SSHH'
# EthOS SSH Hardening
PermitRootLogin no
PasswordAuthentication yes
MaxAuthTries 3
LoginGraceTime 30
X11Forwarding no
PermitEmptyPasswords no
SSHH

# Gate SSH login until the default password is changed via the Web UI.
# ForceCommand runs check_password_changed.sh which blocks or exec's the shell.
# During installer mode (.installed absent) the script allows access for debugging.
cat >> "$ROOT/etc/ssh/sshd_config" <<'SSHGATE'

# EthOS: block SSH until default password changed via Web UI
Match User *
    ForceCommand /opt/ethos/tools/check_password_changed.sh
SSHGATE

# ── SSH host key regeneration drop-in ──
# Host keys are wiped at dist-sec time so each deployed system gets unique keys.
# This drop-in ensures sshd generates any missing keys before starting —
# without it the ssh.service ControlProcess (sshd -t) can fail on first boot.
mkdir -p "$ROOT/etc/systemd/system/ssh.service.d"
cat > "$ROOT/etc/systemd/system/ssh.service.d/ethos-keygen.conf" <<'SSHKEYGEN'
[Service]
ExecStartPre=/usr/bin/ssh-keygen -A 2>/dev/null
SSHKEYGEN

# ── Force password change on first boot ──
rm -f "$ROOT/opt/ethos/.password_changed"

# ── USB automount (devmon/udevil) ──
echo "LOG:Konfiguracja devmon USB automount..."
chroot "$ROOT" bash -c 'id devmon &>/dev/null || useradd -r -s /usr/sbin/nologin -d /media/devmon devmon' 2>/dev/null || true
mkdir -p "$ROOT/media/devmon"
chroot "$ROOT" chown devmon:root /media/devmon
chroot "$ROOT" chmod 755 /media/devmon
cat > "$ROOT/etc/systemd/system/devmon@.service" <<'DEVMONSVC'
[Unit]
Description=devmon USB automounter for %i
After=local-fs.target

[Service]
Type=simple
User=%i
ExecStart=/usr/bin/devmon --no-gui --exec-on-drive "chmod o+x /media/%i"
ExecStartPost=/bin/chmod o+x /media/%i
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
DEVMONSVC
ln -sf /etc/systemd/system/devmon@.service "$ROOT/etc/systemd/system/multi-user.target.wants/devmon@devmon.service"
echo "LOG:devmon USB automount OK"

# NetworkManager config — manage ALL devices (Ubuntu server defaults to WiFi-only)
mkdir -p "$ROOT/etc/NetworkManager/conf.d"
cat > "$ROOT/etc/NetworkManager/conf.d/00-ethos.conf" <<'NMCFG'
[main]
dns=dnsmasq

[keyfile]
# Override Ubuntu's 10-globally-managed-devices.conf which only manages WiFi.
# EthOS needs NM to manage ethernet too (no netplan/networkd).
unmanaged-devices=none

[device]
wifi.scan-rand-mac-address=no
NMCFG
# Lock root account — random password + lock
ROOT_PASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 24)
chroot "$ROOT" bash -c "echo \"root:${{ROOT_PASS}}\" | chpasswd"
chroot "$ROOT" passwd -l root

# ── Branding: /etc/os-release ──
cat > "$ROOT/etc/os-release" <<OSREL
PRETTY_NAME="$BRAND_NAME v${{VERSION}}"
NAME="$BRAND_NAME"
VERSION_ID="${{VERSION}}"
VERSION="${{VERSION}}"
ID=ethos
ID_LIKE=$BASE_DISTRO
HOME_URL="https://ethos.local"
OSREL

cat > "$ROOT/etc/issue" <<ISSUE
$BRAND_NAME \\n \\l

ISSUE
echo "$BRAND_NAME" > "$ROOT/etc/issue.net"

# ── Full Debian branding purge ──
# lsb-release
cat > "$ROOT/etc/lsb-release" <<LSBREL
DISTRIB_ID=EthOS
DISTRIB_RELEASE=${{VERSION}}
DISTRIB_CODENAME=ethos
DISTRIB_DESCRIPTION="$BRAND_NAME v${{VERSION}}"
LSBREL
# /usr/lib/os-release (canonical path, symlinked by many tools)
if [ -d "$ROOT/usr/lib" ]; then
    cp "$ROOT/etc/os-release" "$ROOT/usr/lib/os-release" 2>/dev/null || true
fi
# Neutralise /etc/debian_version (content doesn't matter for EthOS)
echo "ethos/${{VERSION}}" > "$ROOT/etc/debian_version" 2>/dev/null || true
# Replace dpkg origin so dpkg --version shows EthOS
if [ -d "$ROOT/etc/dpkg/origins" ]; then
    cat > "$ROOT/etc/dpkg/origins/ethos" <<DPKGORIG
Vendor: EthOS
Vendor-URL: https://ethos.local
Bugs: https://ethos.local/bugs
Parent: Debian
DPKGORIG
    ln -sf ethos "$ROOT/etc/dpkg/origins/default" 2>/dev/null || true
fi
# Remove Debian motd snippets that would reveal base distro
rm -f "$ROOT/etc/update-motd.d/10-uname" 2>/dev/null || true
cat > "$ROOT/etc/motd" <<MOTD

  Welcome to $BRAND_NAME v${{VERSION}}
  https://ethos.local

MOTD

# ── GRUB defaults (so update-grub keeps EthOS name) ──
cat > "$ROOT/etc/default/grub" <<GRUBDEF
GRUB_DEFAULT=0
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="EthOS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=3 rd.systemd.show_status=auto vt.global_cursor_default=0 splash net.ifnames=0 biosdevname=0"
GRUB_CMDLINE_LINUX=""
GRUBDEF

echo "STEP:52:System configured"

# ── Step 4: GRUB ──
echo "STEP:53:Installing GRUB (UEFI)..."

echo "LOG:apt-get update in chroot..."
chroot "$ROOT" apt-get update -qq 2>&1 | tail -3 || true
echo "LOG:Installing GRUB packages..."
if [ "$BASE_DISTRO" = "ubuntu" ]; then
    DEBIAN_FRONTEND=noninteractive chroot "$ROOT" apt-get install -y -qq grub-efi-amd64 grub-efi-amd64-bin grub-common grub2-common 2>&1 | tail -5 || true
else
    DEBIAN_FRONTEND=noninteractive chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports grub-efi-amd64 grub-efi-amd64-bin grub-common grub2-common 2>&1 | tail -5 || true
fi
DEBIAN_FRONTEND=noninteractive chroot "$ROOT" apt-get install -y -qq efibootmgr 2>&1 | tail -5 || true

mkdir -p "$ROOT/boot/efi/EFI/BOOT"
echo "LOG:GRUB UEFI install..."
chroot "$ROOT" grub-install --target=x86_64-efi --efi-directory=/boot/efi \\
    --boot-directory=/boot --removable --no-nvram 2>&1 | tail -5
GRUB_RC=${{PIPESTATUS[0]}}
GRUB_EFI_BIN="$ROOT/boot/efi/EFI/BOOT/BOOTX64.EFI"
if [ "$GRUB_RC" -ne 0 ] || [ ! -f "$GRUB_EFI_BIN" ]; then
    echo "LOG:ERROR: grub-install failed (rc=$GRUB_RC) or BOOTX64.EFI missing"
    ls -la "$ROOT/boot/efi/EFI/" 2>/dev/null | while IFS= read -r _l; do echo "LOG:EFI> $_l"; done
    echo "STEP:0:ERROR: UEFI grub-install failed!"
    exit 1
fi

KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||") || true
INITRD=$(ls "$ROOT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||") || true

# If kernel not found at this stage (e.g. initrd not yet generated), use symlinks as fallback
if [ -z "$KERN" ] && [ -L "$ROOT/boot/vmlinuz" ]; then
    KERN="/boot/vmlinuz"
fi
if [ -z "$INITRD" ] && [ -L "$ROOT/boot/initrd.img" ]; then
    INITRD="/boot/initrd.img"
fi

mkdir -p "$ROOT/boot/grub"
cat > "$ROOT/boot/grub/grub.cfg" <<GRUBCFG
set timeout=3
set default=0
insmod part_gpt
insmod ext2
insmod gzio
insmod search_fs_uuid
menuentry "EthOS v${{VERSION}}" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro quiet loglevel=3 rd.systemd.show_status=auto vt.global_cursor_default=0 splash net.ifnames=0 biosdevname=0 fsck.repair=preen console=tty0 console=ttyS0,115200n8
    initrd ${{INITRD}}
}}
menuentry "EthOS v${{VERSION}} (recovery)" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro single nomodeset fsck.repair=preen console=tty0 console=ttyS0,115200n8
    initrd ${{INITRD}}
}}
GRUBCFG

cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/EFI/BOOT/grub.cfg"

# GRUB's embedded prefix is /boot/grub — create that path on the ESP too,
# so insmod ext2 and grub.cfg are found regardless of which device GRUB sees as root
mkdir -p "$ROOT/boot/efi/boot/grub/x86_64-efi"
cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/boot/grub/grub.cfg"
for _mod in ext2 part_gpt part_msdos gzio linux normal search search_fs_uuid \
            search_label configfile all_video boot fat efi_gop video video_fb; do
    cp "$ROOT/boot/grub/x86_64-efi/${{_mod}}.mod" \
       "$ROOT/boot/efi/boot/grub/x86_64-efi/" 2>/dev/null || true
done

# Copy kernel + initrd to ESP recovery directory
mkdir -p "$ROOT/boot/efi/EFI/recovery"
cp "$ROOT/boot/${{KERN##*/}}" "$ROOT/boot/efi/EFI/recovery/vmlinuz" 2>/dev/null || true
cp "$ROOT/boot/${{INITRD##*/}}" "$ROOT/boot/efi/EFI/recovery/initrd.img" 2>/dev/null || true
echo "LOG:Recovery kernel copied to ESP"

echo "STEP:60:GRUB installed"

# From here on, individual failures should not abort the whole build
set +e

# ── Step 5: Install dependencies (native) ──
echo "STEP:61:Installing dependencies..."
echo "LOG:apt-get update in chroot..."
chroot "$ROOT" apt-get update -qq 2>&1 | tail -3 || echo "LOG:apt-get update failed but continuing"

echo "LOG:Installing minimal packages..."
chroot "$ROOT" apt-get install -y -qq \
    python3 python3-pip python3-venv \
    avahi-daemon \
    wpasupplicant dnsmasq rfkill \
    cloud-guest-utils \
    udevil udisks2 \
    zstd cron systemd-timesyncd \
    gnupg age \
    initramfs-tools \
    2>&1 | tail -10 || echo "LOG:Some packages skipped"

echo "LOG:Installing Plymouth for boot splash..."
chroot "$ROOT" apt-get install -y -qq plymouth plymouth-themes 2>&1 | tail -5 || echo "LOG:Plymouth skipped"
# Set Plymouth theme without -R (initramfs is rebuilt later in the script)
chroot "$ROOT" plymouth-set-default-theme spinner 2>/dev/null || echo "LOG:Plymouth theme alternatives skipped"
mkdir -p "$ROOT/etc/plymouth"
cat > "$ROOT/etc/plymouth/plymouthd.conf" <<PLYCFG
[Daemon]
Theme=spinner
ShowDelay=0
PLYCFG
echo "LOG:Plymouth theme set to spinner"

echo "LOG:Installing firmware..."
if [ "$BASE_DISTRO" = "ubuntu" ]; then
    # Selective firmware: WiFi + NIC essentials only (~50MB vs ~800MB for full linux-firmware)
    chroot "$ROOT" apt-get install -y -qq \
        linux-firmware \
        2>&1 | tail -5 || echo "LOG:Some firmware skipped"
    # NOTE: linux-firmware is needed for now (Ubuntu bundles all firmware in one package).
    # When Ubuntu provides granular firmware packages, switch to selective.
else
    chroot "$ROOT" apt-get install -y -qq \
        firmware-atheros firmware-realtek firmware-brcm80211 \
        firmware-misc-nonfree firmware-linux-nonfree firmware-intel-sound \
        2>&1 | tail -10 || echo "LOG:Some firmware skipped"
fi

# All other packages (storage tools, sensors, printer, archives, etc.)
# are installed lazily by EthOS (ensure_dep) when user enables features.
# Builder tools are pre-installed so image creation works out of the box.
echo "LOG:Installing builder tools..."
chroot "$ROOT" apt-get install -y -qq \
    debootstrap squashfs-tools xorriso isolinux \
    parted dosfstools e2fsprogs btrfs-progs mtools \
    2>&1 | tail -5 || echo "LOG:Some builder tools skipped"

# Install spec-defined extra packages (from build-spec.yaml packages.apt_extra)
if [ -n "$APT_EXTRA_PKGS" ]; then
    echo "LOG:Installing spec apt_extra packages: $APT_EXTRA_PKGS"
    chroot "$ROOT" apt-get install -y -qq $APT_EXTRA_PKGS \
        2>&1 | tail -10 || echo "LOG:Some apt_extra packages skipped"
fi

echo "STEP:73:Installing kernel and firmware updates..."

# First clean apt cache to free space before big installs
chroot "$ROOT" apt-get clean 2>/dev/null || true
echo "LOG:Disk usage before kernel/firmware update:"
df -h "$ROOT" 2>/dev/null | tail -1 || true

if [ "$BASE_DISTRO" = "ubuntu" ]; then
    echo "LOG:Ubuntu: installing HWE kernel (linux-image-generic-hwe-24.04) for latest hardware support..."
    chroot "$ROOT" apt-get install -y -qq linux-image-generic-hwe-24.04 linux-headers-generic-hwe-24.04 linux-firmware 2>&1 | tail -5 || echo "LOG:HWE kernel install skipped"
else
    echo "LOG:Installing linux-image-amd64 from backports..."
    chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports linux-image-amd64 2>&1 | tail -5 || echo "LOG:Backports kernel skipped"
fi

# Remove non-HWE generic kernel if HWE was installed (to save ~200MB and avoid dual initramfs)
if [ "$BASE_DISTRO" = "ubuntu" ]; then
    NON_HWE=$(chroot "$ROOT" dpkg -l 'linux-image-[0-9]*' 2>/dev/null | awk '/^ii/{{print $2}}' | grep -v hwe | head -1 || true)
    if [[ -n "$NON_HWE" ]]; then
        echo "LOG:Removing non-HWE kernel package $NON_HWE..."
        chroot "$ROOT" apt-get remove -y --purge "$NON_HWE" linux-image-generic linux-headers-generic 2>&1 | tail -3 || true
        echo "LOG:Non-HWE kernel removed"
    fi
fi
# Remove OLD kernel to save ~200MB and avoid initramfs for 2 kernels
OLD_KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | head -1 | sed 's|.*/vmlinuz-||')
NEW_KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | sed 's|.*/vmlinuz-||')
if [[ -n "$OLD_KERN" && -n "$NEW_KERN" && "$OLD_KERN" != "$NEW_KERN" ]]; then
    echo "LOG:Removing old kernel $OLD_KERN (keeping $NEW_KERN)"
    chroot "$ROOT" apt-get remove -y --purge "linux-image-$OLD_KERN" 2>&1 | tail -3 || true
    rm -f "$ROOT/boot/vmlinuz-$OLD_KERN" "$ROOT/boot/initrd.img-$OLD_KERN" "$ROOT/boot/System.map-$OLD_KERN" "$ROOT/boot/config-$OLD_KERN" 2>/dev/null
    rm -rf "$ROOT/lib/modules/$OLD_KERN" 2>/dev/null
    echo "LOG:Old kernel removed"
fi

# Refresh grub.cfg + BOOTX64.EFI now that the latest kernel is active
KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||")
INITRD=$(ls "$ROOT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||")
# Fallback: initrd may not exist yet (generated later by update-initramfs)
if [[ -z "$INITRD" && -L "$ROOT/boot/initrd.img" ]]; then
    INITRD="/boot/initrd.img"
fi
echo "LOG:Refreshing GRUB config for kernel: $KERN initrd: $INITRD"
cat > "$ROOT/boot/grub/grub.cfg" <<GRUBCFG
set timeout=3
set default=0
insmod part_gpt
insmod ext2
insmod gzio
insmod search_fs_uuid
menuentry "EthOS v${{VERSION}}" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro quiet loglevel=3 rd.systemd.show_status=auto vt.global_cursor_default=0 splash net.ifnames=0 biosdevname=0 fsck.repair=preen console=tty0 console=ttyS0,115200n8
    initrd ${{INITRD}}
}}
menuentry "EthOS v${{VERSION}} (recovery)" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro single nomodeset fsck.repair=preen console=tty0 console=ttyS0,115200n8
    initrd ${{INITRD}}
}}
GRUBCFG
cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/EFI/BOOT/grub.cfg"
# Update recovery kernel on ESP
cp "$ROOT/boot/${{KERN##*/}}" "$ROOT/boot/efi/EFI/recovery/vmlinuz" 2>/dev/null || true
cp "$ROOT/boot/${{INITRD##*/}}" "$ROOT/boot/efi/EFI/recovery/initrd.img" 2>/dev/null || true
# Re-run grub-install to refresh BOOTX64.EFI modules
chroot "$ROOT" grub-install --target=x86_64-efi --efi-directory=/boot/efi \
    --boot-directory=/boot --removable --no-nvram 2>/dev/null || echo "LOG:grub-install refresh skipped"
# grub-install overwrites ESP grub.cfg — re-copy everything to ESP
cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/EFI/BOOT/grub.cfg"
cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/boot/grub/grub.cfg"
for _mod in ext2 part_gpt part_msdos gzio linux normal search search_fs_uuid \
            search_label configfile all_video boot fat efi_gop video video_fb; do
    cp "$ROOT/boot/grub/x86_64-efi/${{_mod}}.mod" \
       "$ROOT/boot/efi/boot/grub/x86_64-efi/" 2>/dev/null || true
done
echo "LOG:GRUB refreshed for latest kernel"

if [ "$BASE_DISTRO" = "ubuntu" ]; then
    echo "LOG:Ubuntu: installing extra firmware for newer hardware (Intel/AMD iGPU, WiFi, audio)..."
    chroot "$ROOT" apt-get install -y -qq linux-firmware firmware-sof-signed 2>&1 | tail -5 || echo "LOG:Firmware install skipped"
else
    echo "LOG:Installing firmware-iwlwifi from backports..."
    chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-iwlwifi 2>&1 | tail -5 || echo "LOG:Backports iwlwifi skipped"
    echo "LOG:Installing firmware-realtek from backports..."
    chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-realtek 2>&1 | tail -5 || echo "LOG:Backports realtek skipped"
    echo "LOG:Installing firmware-misc-nonfree from backports..."
    chroot "$ROOT" apt-get install -y -qq -t ${{DEBIAN_RELEASE}}-backports firmware-misc-nonfree 2>&1 | tail -5 || echo "LOG:Backports misc skipped"
fi

# Disable standalone dnsmasq (NM uses its own for AP mode)
chroot "$ROOT" systemctl disable dnsmasq 2>/dev/null || true
chroot "$ROOT" systemctl mask dnsmasq 2>/dev/null || true

# Install rfkill (needed for WiFi unblock)
chroot "$ROOT" apt-get install -y -qq rfkill 2>/dev/null || true

# Clean apt cache before initramfs rebuild to maximize free space
chroot "$ROOT" apt-get clean 2>/dev/null || true
rm -rf "$ROOT/var/lib/apt/lists/"* 2>/dev/null || true
echo "LOG:Disk usage before initramfs:"
df -h "$ROOT" 2>/dev/null | tail -1 || true

# ── SquashFS + OverlayFS initramfs hooks ──
# These hooks allow the installed system to boot from a read-only squashfs
# with a persistent overlay on the ext4 root partition.
echo "LOG:Adding SquashFS overlay boot support to initramfs..."
mkdir -p "$ROOT/etc/initramfs-tools/hooks"
mkdir -p "$ROOT/etc/initramfs-tools/scripts/local-bottom"

cat > "$ROOT/etc/initramfs-tools/hooks/ethos-overlay" <<'HOOKEOF'
#!/bin/sh
PREREQ=""
prereqs() {{ echo "$PREREQ"; }}
case "$1" in prereqs) prereqs; exit 0 ;; esac
. /usr/share/initramfs-tools/hook-functions
manual_add_modules squashfs
manual_add_modules overlay
manual_add_modules loop
manual_add_modules dm_verity
manual_add_modules dm_mod
manual_add_modules btrfs
# NVMe/AHCI/virtio storage — must be in initrd so the kernel can
# find the root partition on any storage controller, even when
# building in a VM that has no NVMe hardware itself.
manual_add_modules nvme nvme_core nvme_fabrics
manual_add_modules ahci libahci
manual_add_modules virtio_blk virtio_pci
copy_exec /sbin/losetup /sbin
copy_exec /sbin/blkid /sbin
# dm-verity support (optional — only if veritysetup is installed)
if [ -x /sbin/veritysetup ]; then
    copy_exec /sbin/veritysetup /sbin
fi
HOOKEOF
chmod +x "$ROOT/etc/initramfs-tools/hooks/ethos-overlay"

cat > "$ROOT/etc/initramfs-tools/scripts/local-bottom/ethos-overlay" <<'OVERLAYEOF'
#!/bin/sh
# EthOS SquashFS + OverlayFS boot script
# Lower layer: read-only squashfs on Root-A/B partition
# Upper layer: writable overlay — prefers EthOS-Data btrfs partition (like Synology),
#              falls back to Root-A/B ext4 partition if data disk unavailable.
# Activated by kernel cmdline: ethos.rootfs=squashfs
PREREQ=""
prereqs() {{ echo "$PREREQ"; }}
case "$1" in prereqs) prereqs; exit 0 ;; esac
grep -q "ethos.rootfs=squashfs" /proc/cmdline || exit 0
[ -f "${{rootmnt}}/root.sqsh" ] || exit 0
echo "ethos-overlay: starting SquashFS overlay setup"
modprobe -q squashfs 2>/dev/null || true
modprobe -q overlay 2>/dev/null || true
modprobe -q loop 2>/dev/null || true
modprobe -q btrfs 2>/dev/null || true
mkdir -p /run/ethos-rootfs
mount --move "${{rootmnt}}" /run/ethos-rootfs

# Determine boot slot (a or b) from cmdline
SLOT="a"
for arg in $(cat /proc/cmdline); do
    case "$arg" in ethos.slot=*) SLOT="${{arg#ethos.slot=}}" ;; esac
done

# ── Try to use EthOS-Data btrfs partition for overlay (Synology-style) ──
DATA_UPPER=""
DATA_DEV=$(blkid -L EthOS-Data 2>/dev/null)
if [ -n "$DATA_DEV" ]; then
    DATA_MNT=/run/ethos-data
    mkdir -p "$DATA_MNT"
    if mount -t btrfs -o subvol=@data,noatime "$DATA_DEV" "$DATA_MNT" 2>/dev/null; then
        UPPER="$DATA_MNT/ethos/overlay/$SLOT/upper"
        WORK="$DATA_MNT/ethos/overlay/$SLOT/work"
        mkdir -p "$UPPER" "$WORK"
        DATA_UPPER=1
        echo "ethos-overlay: using EthOS-Data btrfs for overlay upper=$UPPER"
    fi
fi

# ── Fallback: use overlay dirs on Root-A/B partition ──
if [ -z "$DATA_UPPER" ]; then
    UPPER=/run/ethos-rootfs/overlay/upper
    WORK=/run/ethos-rootfs/overlay/work
    mkdir -p "$UPPER" "$WORK"
    echo "ethos-overlay: using Root partition for overlay upper=$UPPER"
fi

# dm-verity integrity check (optional — runs if roothash and verity data exist)
VERITY_OK=0
ROOTHASH_FILE="/run/ethos-rootfs/root.sqsh.roothash"
VERITY_FILE="/run/ethos-rootfs/root.sqsh.verity"
SQSH_FILE="/run/ethos-rootfs/root.sqsh"
if [ -f "$ROOTHASH_FILE" ] && [ -f "$VERITY_FILE" ] && command -v veritysetup >/dev/null 2>&1; then
    modprobe -q dm_verity 2>/dev/null || true
    modprobe -q dm_mod 2>/dev/null || true
    ROOTHASH=$(cat "$ROOTHASH_FILE")
    LOOP_DEV=$(losetup --find --show "$SQSH_FILE")
    HASH_DEV=$(losetup --find --show "$VERITY_FILE")
    if veritysetup open --hash-offset=0 "$LOOP_DEV" ethos-verity "$HASH_DEV" "$ROOTHASH" 2>/dev/null; then
        mkdir -p /run/ethos-sqsh
        if mount -t squashfs -o ro /dev/mapper/ethos-verity /run/ethos-sqsh 2>/dev/null; then
            VERITY_OK=1
        else
            veritysetup close ethos-verity 2>/dev/null
        fi
    fi
    if [ "$VERITY_OK" = "0" ]; then
        losetup -d "$LOOP_DEV" 2>/dev/null
        losetup -d "$HASH_DEV" 2>/dev/null
        echo "ethos-overlay: dm-verity verification FAILED — falling back to unverified mount"
    fi
fi

# Standard mount (no verity or verity unavailable)
if [ "$VERITY_OK" = "0" ]; then
    mkdir -p /run/ethos-sqsh
    echo "ethos-overlay: mounting squashfs from $SQSH_FILE"
    if ! mount -t squashfs -o ro,loop "$SQSH_FILE" /run/ethos-sqsh; then
        echo "ethos-overlay: FAILED to mount squashfs — falling back to raw root"
        mount --move /run/ethos-rootfs "${{rootmnt}}"
        exit 0
    fi
    echo "ethos-overlay: squashfs mounted OK"
fi

echo "ethos-overlay: mounting overlay lowerdir=/run/ethos-sqsh upperdir=$UPPER workdir=$WORK"
if ! mount -t overlay overlay \
    -o "lowerdir=/run/ethos-sqsh,upperdir=$UPPER,workdir=$WORK,index=off,nfs_export=off" \
    "${{rootmnt}}"; then
    echo "ethos-overlay: FAILED to mount overlay — falling back to raw root"
    umount /run/ethos-sqsh 2>/dev/null
    [ "$VERITY_OK" = "1" ] && veritysetup close ethos-verity 2>/dev/null
    [ -n "$DATA_UPPER" ] && umount /run/ethos-data 2>/dev/null
    mount --move /run/ethos-rootfs "${{rootmnt}}"
    exit 0
fi
mkdir -p "${{rootmnt}}/.squashfs" "${{rootmnt}}/.rootfs"
mount --move /run/ethos-rootfs "${{rootmnt}}/.rootfs"
mount --move /run/ethos-sqsh "${{rootmnt}}/.squashfs"
# Ensure essential mount-point dirs exist (they may be absent from squashfs)
for _d in dev proc sys run tmp media mnt; do
    mkdir -p "${{rootmnt}}/$_d"
done
echo "ethos-overlay: overlay boot setup COMPLETE"
# Expose data partition mount inside the new root for runtime use
if [ -n "$DATA_UPPER" ]; then
    mkdir -p "${{rootmnt}}/run/ethos-data"
    mount --move /run/ethos-data "${{rootmnt}}/run/ethos-data"
fi
OVERLAYEOF
chmod +x "$ROOT/etc/initramfs-tools/scripts/local-bottom/ethos-overlay"

# ── Emergency boot diagnostics: dump dmesg to ESP ──
cat > "$ROOT/etc/initramfs-tools/scripts/local-bottom/ethos-bootlog" <<'BOOTLOGEOF'
#!/bin/sh
# Emergency dmesg dump to ESP — accessible even if root fails to mount.
# Saved to FAT32 EFI partition which is always mountable from rescue USB.
PREREQ=""
prereqs() {{ echo "$PREREQ"; }}
case "$1" in prereqs) prereqs; exit 0 ;; esac
ESP=""
for p in /boot/efi /efi; do
    if mountpoint -q "${{rootmnt}}$p" 2>/dev/null; then ESP="${{rootmnt}}$p"; break; fi
done
if [ -z "$ESP" ]; then
    ESP_DEV=$(blkid -t TYPE=vfat -o device 2>/dev/null | sed -n '1p')
    if [ -n "$ESP_DEV" ]; then
        mkdir -p /run/ethos-esp
        mount -t vfat "$ESP_DEV" /run/ethos-esp 2>/dev/null && ESP="/run/ethos-esp"
    fi
fi
if [ -n "$ESP" ]; then
    mkdir -p "$ESP/EFI/ethos/logs"
    dmesg > "$ESP/EFI/ethos/logs/last-dmesg.log" 2>/dev/null || true
fi
BOOTLOGEOF
chmod +x "$ROOT/etc/initramfs-tools/scripts/local-bottom/ethos-bootlog"

# Rebuild initramfs with firmware + overlay hooks (only for the new kernel)
echo "LOG:Przebudowa initramfs..."
if ! chroot "$ROOT" which update-initramfs &>/dev/null; then
    echo "LOG:WARNING: update-initramfs not found — installing initramfs-tools"
    chroot "$ROOT" apt-get install -y -qq initramfs-tools 2>&1 | tail -3
fi
if [[ -n "$NEW_KERN" ]]; then
    chroot "$ROOT" update-initramfs -u -k "$NEW_KERN" 2>&1 | tail -5 || echo "LOG:initramfs update failed"
else
    chroot "$ROOT" update-initramfs -u -k all 2>/dev/null || echo "LOG:initramfs update failed"
fi

# ── Final GRUB refresh — initrd now exists after update-initramfs ──
KERN=$(ls "$ROOT/boot/vmlinuz-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||") || true
INITRD=$(ls "$ROOT/boot/initrd.img-"* 2>/dev/null | sort -V | tail -1 | sed "s|$ROOT||") || true
if [[ -z "$INITRD" && -L "$ROOT/boot/initrd.img" ]]; then
    INITRD="/boot/initrd.img"
fi
if [[ -n "$KERN" && -n "$INITRD" ]]; then
    echo "LOG:Final GRUB refresh: kernel=$KERN initrd=$INITRD"
    cat > "$ROOT/boot/grub/grub.cfg" <<GRUBCFG
set timeout=3
set default=0
insmod part_gpt
insmod ext2
insmod gzio
insmod search_fs_uuid
menuentry "EthOS v${{VERSION}}" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro quiet loglevel=3 rd.systemd.show_status=auto vt.global_cursor_default=0 splash net.ifnames=0 biosdevname=0 fsck.repair=preen console=tty0 console=ttyS0,115200n8
    initrd ${{INITRD}}
}}
menuentry "EthOS v${{VERSION}} (recovery)" {{
    search --no-floppy --fs-uuid --set=root ${{ROOT_UUID}}
    linux ${{KERN}} root=UUID=${{ROOT_UUID}} ro single nomodeset fsck.repair=preen console=tty0 console=ttyS0,115200n8
    initrd ${{INITRD}}
}}
GRUBCFG
    cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/EFI/BOOT/grub.cfg"
    cp "$ROOT/boot/grub/grub.cfg" "$ROOT/boot/efi/boot/grub/grub.cfg"
    cp "$ROOT/boot/${{INITRD##*/}}" "$ROOT/boot/efi/EFI/recovery/initrd.img" 2>/dev/null || true
    echo "LOG:GRUB config updated with initrd"
else
    echo "LOG:WARNING: kernel=$KERN initrd=$INITRD — grub.cfg may be incomplete!"
fi

echo "STEP:75:Dependencies installed"
_ckpt_set "05_apt_deps"

# ── SBOM: generate Software Bill of Materials ──
echo "LOG:Generating SBOM (SPDX-2.3)..."
python3 - "$ROOT" "$VERSION" "$BRAND_NAME" "$WORK_DIR" <<'SBOMPY'
import sys, os
sys.path.insert(0, '/opt/ethos/backend/blueprints')
try:
    from builder_sbom import generate_sbom, write_sbom
    rootfs, version, brand, outdir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    sbom = generate_sbom(rootfs, version, brand)
    write_sbom(sbom, outdir)
    print(f"LOG:SBOM: {{len(sbom.get('packages', []))}} packages documented", flush=True)
except Exception as e:
    print(f"LOG:SBOM generation skipped: {{e}}", flush=True)
SBOMPY

# ── Step 6: Inject EthOS (full package) ──
echo "STEP:76:Injecting EthOS..."

ETHOS_DIR="$ROOT/opt/ethos"
mkdir -p "$ETHOS_DIR"/{{data,backups,logs,uploads,cups-config}}

# ── Copy entire backend/ ──
echo "LOG:Copying backend..."
cp -r "$NASOS/backend" "$ETHOS_DIR/"
rm -rf "$ETHOS_DIR/backend/__pycache__" "$ETHOS_DIR/backend/blueprints/__pycache__"
rm -f "$ETHOS_DIR/backend/blueprints/"*.bak 2>/dev/null || true

# ── License & compliance ──
for f in LICENSE NOTICE; do
  [ -f "$NASOS/$f" ] && cp "$NASOS/$f" "$ETHOS_DIR/"
done

# ── Copy entire frontend/ ──
echo "LOG:Copying frontend..."
cp -r "$NASOS/frontend" "$ETHOS_DIR/"
# Remove optional app JS files — they are installed via Package Center
OPTIONAL_JS="{optional_js_list}"
for fname in $OPTIONAL_JS; do
  rm -f "$ETHOS_DIR/frontend/js/apps/$fname"
  rm -f "$ETHOS_DIR/frontend_dist/js/apps/$fname" 2>/dev/null || true
done
echo "LOG:Optional app JS removed from base image ($(echo $OPTIONAL_JS | wc -w) files)"
# Remove optional blueprint .py files — installed via Package Center
OPTIONAL_PY="{optional_py_list}"
for fname in $OPTIONAL_PY; do
  rm -f "$ETHOS_DIR/backend/blueprints/$fname"
done

# ── Copy tools ──
echo "LOG:Copying tools..."
mkdir -p "$ETHOS_DIR/tools"
cp "$NASOS/tools/ethos-power-config.sh" "$ETHOS_DIR/tools/"
cp "$NASOS/tools/ethos-system-helper.sh" "$ETHOS_DIR/tools/"
cp "$NASOS/tools/ethos-power.service" "$ETHOS_DIR/tools/"
cp "$NASOS/tools/ethos-power-blacklist.conf" "$ETHOS_DIR/tools/"
# Security scripts — SSH password gate + fail2ban event logger
cp "$NASOS/tools/check_password_changed.sh" "$ETHOS_DIR/tools/" 2>/dev/null || echo "WARN:check_password_changed.sh not found"
cp "$NASOS/tools/fail2ban_eventlog.py"      "$ETHOS_DIR/tools/" 2>/dev/null || echo "WARN:fail2ban_eventlog.py not found"
chmod +x "$ETHOS_DIR/tools/ethos-power-config.sh"
chmod +x "$ETHOS_DIR/tools/ethos-system-helper.sh"
chmod +x "$ETHOS_DIR/tools/check_password_changed.sh" 2>/dev/null || true
chmod +x "$ETHOS_DIR/tools/fail2ban_eventlog.py" 2>/dev/null || true

# ── CUPS config ──
if [[ -d "$NASOS/cups-config" ]]; then
    cp -r "$NASOS/cups-config/"* "$ETHOS_DIR/cups-config/" 2>/dev/null || true
fi

# ── Installer scripts (for future updates) ──
mkdir -p "$ETHOS_DIR/installer/images"
cp "$NASOS/installer/"*.sh         "$ETHOS_DIR/installer/"     2>/dev/null || true
cp "$NASOS/installer/images/"*.sh     "$ETHOS_DIR/installer/images/" 2>/dev/null || true

# ── Flask-based preboot installer ──
echo "LOG:Copying Flask preboot installer..."
if [[ -d "$NASOS/installer/preboot" ]]; then
    cp -r "$NASOS/installer/preboot" "$ETHOS_DIR/installer/preboot"
    find "$ETHOS_DIR/installer/preboot" -type d -name "__pycache__" -exec rm -rf {{}} + 2>/dev/null || true
    echo "LOG:Flask preboot installer copied — $(du -sh "$ETHOS_DIR/installer/preboot" | awk '{{print $1}}')"
else
    echo "LOG:ERROR — Flask preboot installer not found at $NASOS/installer/preboot"
    exit 1
fi

# ── Clean cache from copied code ──
find "$ETHOS_DIR" -type d -name "__pycache__" -exec rm -rf {{}} + 2>/dev/null || true
find "$ETHOS_DIR" -name "*.pyc" -delete 2>/dev/null || true
rm -rf "$ETHOS_DIR/tests" "$ETHOS_DIR/logs" "$ETHOS_DIR/backups" 2>/dev/null || true

echo "LOG:Files copied — $(du -sh "$ETHOS_DIR" | awk '{{print $1}}')"

# ── Python venv + environment file ──
echo "LOG:Creating Python venv..."
chroot "$ROOT" python3 -m venv /opt/ethos/venv 2>&1 | tail -3 || echo "LOG:venv creation issue"
echo "LOG:pip install requirements..."
chroot "$ROOT" /opt/ethos/venv/bin/pip install --no-cache-dir -r /opt/ethos/backend/requirements.txt 2>&1 | tail -15 || echo "LOG:pip install issue"
# Verify critical imports work
chroot "$ROOT" /opt/ethos/venv/bin/python -c "import flask; import psutil; import gevent; import pyudev; print('OK: all imports')" 2>&1 || echo "LOG:WARNING: Some Python modules missing!"

# Cache pip wheels so firstboot can install offline (no internet required)
echo "LOG:Caching pip wheels for offline firstboot..."
PIP_CACHE="$ETHOS_DIR/.pip-cache"
mkdir -p "$PIP_CACHE"
chroot "$ROOT" /opt/ethos/venv/bin/pip download -d /opt/ethos/.pip-cache -r /opt/ethos/backend/requirements.txt 2>&1 | tail -5 || echo "LOG:WARNING: pip wheel cache failed"
# Pre-build wheel for GPUtil (ships only as sdist — build it now so offline firstboot can install it)
# setuptools is needed for the build; install it temporarily then remove.
echo "LOG:Pre-building wheels for sdist-only packages..."
chroot "$ROOT" /opt/ethos/venv/bin/pip install --no-cache-dir setuptools 2>&1 | tail -2 || true
chroot "$ROOT" /opt/ethos/venv/bin/pip wheel --no-deps --no-build-isolation \
    -w /opt/ethos/.pip-cache GPUtil==1.4.0 2>&1 | tail -3 || echo "LOG:WARNING: GPUtil wheel build failed (non-fatal)"
WHEEL_COUNT=$(ls "$PIP_CACHE"/*.whl 2>/dev/null | wc -l)
echo "LOG:Cached $WHEEL_COUNT wheel files ($(du -sh "$PIP_CACHE" 2>/dev/null | awk '{{print $1}}'))"

cat > "$ETHOS_DIR/ethos.env" <<ENVFILE
NAS_NAME=EthOS
PORT=$NAS_PORT
ETHOS_ROOT=/opt/ethos
BACKUP_DIR=/opt/ethos/backups
ETHOS_BUILD_ID=build-$(date +%s)
ENVFILE
# Inject build host for QA beacon (only if ETHOS_QA_BUILD_HOST is set in the host env)
if [[ -n "${{ETHOS_QA_BUILD_HOST:-}}" ]]; then
    echo "ETHOS_BUILD_HOST=${{ETHOS_QA_BUILD_HOST}}" >> "$ETHOS_DIR/ethos.env"
fi
chmod 640 "$ETHOS_DIR/ethos.env"

cat > "$ETHOS_DIR/start.sh" <<'MGMT_STARTSH'
#!/bin/bash
sudo systemctl start ethos
echo "EthOS uruchomiony"
MGMT_STARTSH

cat > "$ETHOS_DIR/stop.sh" <<'MGMT_STOPSH'
#!/bin/bash
sudo systemctl stop ethos
echo "EthOS zatrzymany"
MGMT_STOPSH

cat > "$ETHOS_DIR/rebuild.sh" <<'MGMT_REBSH'
#!/bin/bash
cd "$(dirname "$0")"
./venv/bin/pip install --quiet --no-cache-dir -r backend/requirements.txt
sudo systemctl restart ethos
echo "EthOS przebudowany i uruchomiony"
MGMT_REBSH

chmod +x "$ETHOS_DIR"/{{start,stop,rebuild}}.sh

# ── install.conf ──
cat > "$ETHOS_DIR/install.conf" <<INSTCFG
ETHOS_USER="$DEFAULT_USER"
ETHOS_HOSTNAME="$DEFAULT_HOSTNAME"
ETHOS_NAS_NAME="$BRAND_NAME"
ETHOS_BRAND_NAME="$BRAND_NAME"
ETHOS_PORT=$NAS_PORT
ETHOS_SETUP_WIZARD=yes
INSTCFG

# ── Installer-mode marker: USB is an installer, not a live OS ──
touch "$ETHOS_DIR/.installer-mode"
echo "LOG:Installer-mode marker created"

# ── WiFi AP script ──
echo "LOG:Copying ethos-ap.sh..."
cp "$NASOS/installer/images/ethos-ap.sh" "$ROOT/usr/local/bin/ethos-ap"
chmod +x "$ROOT/usr/local/bin/ethos-ap"
if [[ ! -f "$ROOT/usr/local/bin/ethos-ap" ]]; then
    echo "LOG:ERROR — ethos-ap not copied!"
    ls -la "$NASOS/installer/images/ethos-ap.sh" 2>&1 || true
    exit 1
fi
echo "LOG:ethos-ap.sh OK"

# ── Firstboot script (copy from source — simplified v2) ──
echo "LOG:Copying firstboot-v2.sh..."
if [[ -f "$NASOS/installer/images/firstboot-v2.sh" ]]; then
    cp "$NASOS/installer/images/firstboot-v2.sh" "$ROOT/opt/ethos-firstboot.sh"
elif [[ -f "$NASOS/installer/images/firstboot.sh" ]]; then
    echo "LOG:WARNING — firstboot-v2.sh not found, falling back to firstboot.sh"
    cp "$NASOS/installer/images/firstboot.sh" "$ROOT/opt/ethos-firstboot.sh"
else
    echo "LOG:ERROR — no firstboot script found!"
    exit 1
fi
chmod +x "$ROOT/opt/ethos-firstboot.sh"
if [[ ! -f "$ROOT/opt/ethos-firstboot.sh" ]]; then
    echo "LOG:ERROR — firstboot.sh not copied!"
    exit 1
fi
echo "LOG:firstboot.sh OK"

# ── Diagnostic script ──
echo "LOG:Copying ethos-diag.sh..."
if [[ -f "$NASOS/installer/images/ethos-diag.sh" ]]; then
    cp "$NASOS/installer/images/ethos-diag.sh" "$ROOT/usr/local/bin/ethos-diag"
    chmod +x "$ROOT/usr/local/bin/ethos-diag"
    echo "LOG:ethos-diag OK"
else
    echo "LOG:WARNING — ethos-diag.sh not found (skipping)"
fi

# ── Firstboot systemd service ──
cat > "$ROOT/etc/systemd/system/ethos-firstboot.service" <<SVCUNIT
[Unit]
Description=EthOS First Boot Installer
After=network.target ethos-preboot.service
Wants=network.target
ConditionPathExists=/opt/ethos-firstboot.sh
ConditionPathExists=!/opt/ethos/.installed
ConditionPathExists=!/opt/ethos/.installer-mode
[Service]
Type=oneshot
ExecStart=/bin/bash /opt/ethos-firstboot.sh
StandardOutput=journal+console
StandardError=journal+console
TimeoutStartSec=1800
[Install]
WantedBy=multi-user.target
SVCUNIT
ln -sf /etc/systemd/system/ethos-firstboot.service "$ROOT/etc/systemd/system/multi-user.target.wants/ethos-firstboot.service"

# WiFi AP service
cat > "$ROOT/etc/systemd/system/ethos-ap.service" <<'APSVC'
[Unit]
Description=EthOS WiFi Hotspot (auto if no network)
After=NetworkManager.service
Wants=NetworkManager.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/ethos-ap auto
ExecStop=/usr/local/bin/ethos-ap stop
[Install]
WantedBy=multi-user.target
APSVC
ln -sf /etc/systemd/system/ethos-ap.service "$ROOT/etc/systemd/system/multi-user.target.wants/ethos-ap.service"

# Pre-boot setup server (Flask-based installer with i18n + offline fonts)
echo "LOG:Verifying Flask preboot installer in image..."
if [[ ! -f "$ROOT/opt/ethos/installer/preboot/app.py" ]]; then
    echo "LOG:CRITICAL ERROR — Flask preboot app.py does not exist in image!"
    exit 1
fi
echo "LOG:Flask preboot installer OK"

cat > "$ROOT/etc/systemd/system/ethos-preboot.service" <<'PREBOOT'
[Unit]
Description=EthOS Installer (pre-boot setup)
After=network.target NetworkManager.service
Wants=NetworkManager.service
Before=ethos-firstboot.service
Conflicts=ethos.service
ConditionPathExists=/opt/ethos/installer/preboot/app.py
ConditionPathExists=!/opt/ethos/.installed
StartLimitIntervalSec=60
StartLimitBurst=5
[Service]
Type=simple
WorkingDirectory=/opt/ethos/installer/preboot
ExecStart=/opt/ethos/venv/bin/python /opt/ethos/installer/preboot/app.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=5
Environment=PYTHONUNBUFFERED=1
[Install]
WantedBy=multi-user.target
PREBOOT
ln -sf /etc/systemd/system/ethos-preboot.service "$ROOT/etc/systemd/system/multi-user.target.wants/ethos-preboot.service"

# Set multi-user as default (headless — no kiosk, access via hotspot + browser)
mkdir -p "$ROOT/etc/systemd/system/multi-user.target.wants"
chroot "$ROOT" systemctl set-default multi-user.target 2>/dev/null || true

# ── ethos.service (pre-create — firstboot.sh enables + starts it after stopping preboot) ──
# After=ethos-firstboot.service: prevents race where ethos starts before firstboot
# creates the venv (which would cause repeated failures + systemd start-rate lock).
# firstboot uses --no-block so it exits immediately after queuing the start —
# no deadlock.
cat > "$ROOT/etc/systemd/system/ethos.service" <<SVCETHOS
[Unit]
Description=EthOS NAS
After=network.target local-fs.target ethos-firstboot.service
Wants=network.target
Conflicts=ethos-preboot.service
RequiresMountsFor=/mnt/data

[Service]
Type=notify
NotifyAccess=all
WorkingDirectory=/opt/ethos
EnvironmentFile=/opt/ethos/ethos.env
ExecStartPre=/bin/bash -c 'for d in data logs backups uploads venv; do p="/opt/ethos/\$d"; [ -L "\$p" ] && mkdir -p "\$(readlink "\$p")" || mkdir -p "\$p"; done'
Environment=PYTHONPATH=/opt/ethos/backend
ExecStart=/opt/ethos/venv/bin/python /opt/ethos/backend/app.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SVCETHOS
# NOTE: Do NOT enable here — firstboot.sh enables after stopping preboot (port 9000 conflict)

# ── Auto-login on tty1 as nasadmin (NOT root) during first boot ──
# After setup wizard completes, setup_complete() replaces this with
# ethos-console@tty1 (read-only Synology-style info screen).
mkdir -p "$ROOT/etc/systemd/system/getty@tty1.service.d"
cat > "$ROOT/etc/systemd/system/getty@tty1.service.d/override.conf" <<AUTOLOGIN
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $DEFAULT_USER --noclear %I \$TERM
AUTOLOGIN

# ── .bash_profile for nasadmin — show setup info on console ──
cat > "$ROOT/home/$DEFAULT_USER/.bash_profile" <<'USERPROFILE'
#!/bin/bash
# EthOS first-boot console banner
if [ ! -f /opt/ethos/.installed ]; then
    clear
    echo ""
    echo "  ======================================================="
    echo "              EthOS — Pierwszy start"
    echo "  ======================================================="
    echo ""
    echo "  System sie konfiguruje..."
    echo ""
    IP=$(hostname -I 2>/dev/null | awk '{{print $1}}')
    if [ -n "$IP" ]; then
    echo "  =>  http://${{IP}}:9000"
    else
    echo "  No network — connect to WiFi hotspot:"
    echo "    SSID:  ethos  (bez hasla)"
    echo "    Adres: http://192.168.42.1:9000"
    echo ""
    echo "  Lub podlacz kabel Ethernet."
    fi
    echo ""
    echo "  Kreator pomoze Ci ustawic:"
    echo "    - Polaczenie z siecia WiFi"
    echo "    - Konto administratora"
    echo "    - Dysk danych"
    echo ""
    echo "  ======================================================="
    echo ""
    while [ -z "$IP" ]; do
        sleep 15
        IP=$(hostname -I 2>/dev/null | awk '{{print $1}}')
        if [ -n "$IP" ]; then
            echo "  Siec dostepna: http://${{IP}}:9000"
            echo ""
        fi
    done
fi
USERPROFILE
chown $(chroot "$ROOT" id -u $DEFAULT_USER):$(chroot "$ROOT" id -g $DEFAULT_USER) "$ROOT/home/$DEFAULT_USER/.bash_profile"

# ── Pre-install console info screen service (activated after setup) ──
cp "$NASOS/tools/ethos-console.sh"       "$ROOT/opt/ethos/tools/ethos-console.sh"
cp "$NASOS/tools/ethos-console@.service" "$ROOT/etc/systemd/system/ethos-console@.service"
chmod 755 "$ROOT/opt/ethos/tools/ethos-console.sh"

echo "STEP:85:EthOS injected"
_ckpt_set "06_inject_ethos"

# ── Step 7: Cleanup & finalize ──
echo "STEP:86:Finalizing..."
# Unmount apt cache BEFORE cleaning (it's bind-mounted to host cache)
umount "$ROOT/var/cache/apt/archives" 2>/dev/null || true
chroot "$ROOT" apt-get clean 2>/dev/null || true
rm -rf "$ROOT/var/lib/apt/lists/"* 2>/dev/null || true
rm -rf "$ROOT/var/cache/apt/"*.bin 2>/dev/null || true
rm -rf "$ROOT/tmp/"* 2>/dev/null || true
rm -rf "$ROOT/var/tmp/"* 2>/dev/null || true
rm -rf "$ROOT/var/log/"*.gz "$ROOT/var/log/"*.1 2>/dev/null || true
# Clear pip cache that may have leaked
rm -rf "$ROOT/root/.cache" 2>/dev/null || true
# Only search /opt and /usr — skip mounted /proc, /sys, /dev
find "$ROOT/opt" "$ROOT/usr" -type d -name "__pycache__" -exec rm -rf {{}} + 2>/dev/null || true
# Remove .pyc files (regenerated on import)
find "$ROOT/opt" -name '*.pyc' -delete 2>/dev/null || true
truncate -s 0 "$ROOT/etc/machine-id" 2>/dev/null || true
rm -f "$ROOT/var/lib/dbus/machine-id"
# [DIST-SEC] Remove SSH host keys so they regenerate on first boot
rm -f "$ROOT/etc/ssh/ssh_host_"*
# Log final image usage
echo "LOG:Wykorzystanie dysku w obrazie:"
du -sh "$ROOT"/* 2>/dev/null | sort -rh | head -10 || true
df -h "$ROOT" 2>/dev/null || true

# ── Step 7a: Create SquashFS immutable root image ──
# SquashFS = golden image of the installed system (NOT the installer USB state).
# Data dirs are symlinked to /mnt/data/ethos/ for persistence across updates.
if command -v mksquashfs >/dev/null 2>&1; then
    echo "STEP:87:Creating SquashFS immutable root image..."

    ETHOS_DIR_SQ="$ROOT/opt/ethos"
    # Back up venv before symlinking (needed for installer on the raw image)
    VENV_BACKUP="$WORK_DIR/venv-backup"
    if [ -d "$ETHOS_DIR_SQ/venv/bin" ]; then
        cp -a "$ETHOS_DIR_SQ/venv" "$VENV_BACKUP"
        echo "LOG:venv backed up for installer restore"
    fi
    # Prepare clean installed-system state (squashfs should NOT contain installer artifacts)
    for d in data logs backups uploads venv; do
        rm -rf "$ETHOS_DIR_SQ/$d"
        ln -s "/mnt/data/ethos/$d" "$ETHOS_DIR_SQ/$d"
    done
    rm -f "$ETHOS_DIR_SQ/.installer-mode" "$ETHOS_DIR_SQ/.installed"
    mkdir -p "$ROOT/mnt/data"
    mkdir -p "$ROOT/mnt/snapshots"

    # Unmount bind-mounted pseudo-filesystems BEFORE cleaning/squashing.
    # These were bind-mounted for chroot operations (apt, grub-install, etc.)
    # and if left mounted, mksquashfs would include host /proc, /sys, /dev content.
    for m in boot/efi run sys proc dev/shm dev/pts dev; do
        umount "$ROOT/$m" 2>/dev/null || \
            umount -l "$ROOT/$m" 2>/dev/null || true
    done
    sleep 1

    # Clean virtual-fs directories: keep empty mount-point dirs in squashfs
    # so the initramfs overlay has /dev, /proc, /sys, /run, /tmp available.
    for vfs in dev proc sys run tmp media; do
        rm -rf "$ROOT/$vfs"
        mkdir -p "$ROOT/$vfs"
    done

    SQSH_OUT="$WORK_DIR/ethos-root.sqsh"
    mksquashfs "$ROOT" "$SQSH_OUT" \
        -comp zstd -Xcompression-level $SQSH_COMPRESSION_LEVEL \
        -noappend -no-progress \
        -e "$ROOT/lost+found" \
        -e "$ROOT/swapfile" \
        -e "$ROOT/var/swap" \
        -e "$ROOT/opt/ethos/installer/images/ethos-x86.img" \
        -e "$ROOT/opt/ethos/installer/images/ethos-root.sqsh" \
        -e "$ROOT/opt/ethos/installer/images/ethos-root.sqsh.verity" \
        -e "$ROOT/opt/ethos/installer/images/ethos-root.sqsh.roothash" \
        -e "$ROOT/opt/ethos/installer/images/ethos-manifest.json" \
        2>&1 | tail -10

    SQSH_SIZE=$(stat -c%s "$SQSH_OUT" 2>/dev/null || echo 0)
    echo "LOG:SquashFS image: $((SQSH_SIZE / 1048576))MB"

    # ── SquashFS sanity checks ──
    # Mount the freshly-created squashfs and verify critical files are present.
    # These checks catch structural issues that would cause firstboot or boot
    # failures on the installed system (e.g. missing initramfs scripts, missing
    # firstboot, broken symlinks).
    echo "LOG:Running SquashFS sanity checks..."
    SQSH_CHECK="/tmp/sqsh-sanity-$$"
    mkdir -p "$SQSH_CHECK"
    SANITY_FAIL=0
    if mount -t squashfs -o ro,loop "$SQSH_OUT" "$SQSH_CHECK" 2>/dev/null; then
        # 1. Firstboot script must exist
        if [ ! -f "$SQSH_CHECK/opt/ethos-firstboot.sh" ]; then
            echo "LOG:SANITY FAIL: /opt/ethos-firstboot.sh missing from squashfs"
            SANITY_FAIL=1
        fi
        # 2. Requirements file must exist (firstboot needs it to install venv)
        if [ ! -f "$SQSH_CHECK/opt/ethos/backend/requirements.txt" ]; then
            echo "LOG:SANITY FAIL: requirements.txt missing from squashfs"
            SANITY_FAIL=1
        else
            # Verify critical packages are listed
            for _pkg in flask gevent psutil; do
                if ! grep -qi "$_pkg" "$SQSH_CHECK/opt/ethos/backend/requirements.txt"; then
                    echo "LOG:SANITY FAIL: $_pkg not in requirements.txt"
                    SANITY_FAIL=1
                fi
            done
        fi
        # 3. Initramfs overlay script must exist
        if [ ! -f "$SQSH_CHECK/etc/initramfs-tools/scripts/local-bottom/ethos-overlay" ]; then
            echo "LOG:SANITY FAIL: ethos-overlay initramfs script missing"
            SANITY_FAIL=1
        fi
        # 4. Systemd services must exist
        for _svc in ethos.service ethos-firstboot.service; do
            if [ ! -f "$SQSH_CHECK/etc/systemd/system/$_svc" ]; then
                echo "LOG:SANITY FAIL: $_svc missing from squashfs"
                SANITY_FAIL=1
            fi
        done
        # 5. Firstboot must be enabled (symlink in multi-user.target.wants)
        if [ ! -L "$SQSH_CHECK/etc/systemd/system/multi-user.target.wants/ethos-firstboot.service" ]; then
            echo "LOG:SANITY FAIL: ethos-firstboot.service not enabled"
            SANITY_FAIL=1
        fi
        # 6. Data dirs must be symlinks (not real dirs) in the squashfs
        for _d in data logs backups uploads venv; do
            _p="$SQSH_CHECK/opt/ethos/$_d"
            if [ -d "$_p" ] && [ ! -L "$_p" ]; then
                echo "LOG:SANITY FAIL: /opt/ethos/$_d is a directory, expected symlink to /mnt/data"
                SANITY_FAIL=1
            fi
        done
        # 7. app.py must exist
        if [ ! -f "$SQSH_CHECK/opt/ethos/backend/app.py" ]; then
            echo "LOG:SANITY FAIL: backend/app.py missing from squashfs"
            SANITY_FAIL=1
        fi
        # 8. Initramfs hook must exist (adds squashfs/overlay modules to initrd)
        if [ ! -f "$SQSH_CHECK/etc/initramfs-tools/hooks/ethos-overlay" ]; then
            echo "LOG:SANITY FAIL: ethos-overlay initramfs hook missing"
            SANITY_FAIL=1
        fi
        umount "$SQSH_CHECK" 2>/dev/null || true
    else
        echo "LOG:SANITY FAIL: cannot mount squashfs for verification"
        SANITY_FAIL=1
    fi
    rmdir "$SQSH_CHECK" 2>/dev/null || true
    if [ "$SANITY_FAIL" = "0" ]; then
        echo "LOG:SquashFS sanity checks PASSED (8/8)"
    else
        echo "LOG:WARNING: SquashFS sanity checks FAILED — image may not boot correctly"
    fi

    # Generate dm-verity hash tree for integrity verification
    if command -v veritysetup >/dev/null 2>&1; then
        echo "LOG:Generating dm-verity hash tree..."
        VERITY_OUT="$WORK_DIR/ethos-root.sqsh.verity"
        ROOTHASH_OUT="$WORK_DIR/ethos-root.sqsh.roothash"
        veritysetup format "$SQSH_OUT" "$VERITY_OUT" 2>/dev/null | tee /tmp/verity-format.txt
        ROOTHASH=$(grep "Root hash:" /tmp/verity-format.txt | awk '{{print $NF}}')
        if [ -n "$ROOTHASH" ]; then
            echo "$ROOTHASH" > "$ROOTHASH_OUT"
            VERITY_SIZE=$(stat -c%s "$VERITY_OUT" 2>/dev/null || echo 0)
            echo "LOG:dm-verity: root hash=$ROOTHASH, hash tree=$((VERITY_SIZE / 1024))KB"
            # Sign artifact — produce ethos-manifest.json alongside .sqsh and .verity
            echo "LOG:Signing artifact (RSA-SHA256)..."
            python3 -c "
import sys
sys.path.insert(0, '$NASOS/backend')
from blueprints.builder_signing import sign_artifact, write_manifest
import json
try:
    ver = json.load(open('$NASOS/backend/version.json')).get('version','?')
except Exception:
    ver = '?'
m = sign_artifact('$SQSH_OUT', '$ROOTHASH', build_version=ver)
p = write_manifest(m, '$WORK_DIR')
print('LOG:Manifest written: ' + p if p else 'LOG:WARNING: manifest signing failed')
" 2>&1 | while IFS= read -r _l; do echo "LOG:$_l"; done || echo "LOG:WARNING: signing step failed (non-fatal)"
        else
            echo "LOG:WARNING: dm-verity format failed — skipping"
            rm -f "$VERITY_OUT" "$ROOTHASH_OUT"
        fi
        rm -f /tmp/verity-format.txt
    else
        echo "LOG:veritysetup not found — dm-verity disabled (install cryptsetup-bin for verified boot)"
    fi

    # Restore USB/installer state (so the USB can still boot the installer)
    for d in data logs backups uploads; do
        rm -f "$ETHOS_DIR_SQ/$d"
        mkdir -p "$ETHOS_DIR_SQ/$d"
    done
    # Restore venv from backup (installer needs working Python + Flask)
    rm -f "$ETHOS_DIR_SQ/venv"
    if [ -d "$VENV_BACKUP/bin" ]; then
        mv "$VENV_BACKUP" "$ETHOS_DIR_SQ/venv"
        echo "LOG:venv restored for installer"
    else
        mkdir -p "$ETHOS_DIR_SQ/venv"
        echo "LOG:WARNING: venv backup missing — installer may not work"
    fi
    touch "$ETHOS_DIR_SQ/.installer-mode"
else
    echo "LOG:WARNING: mksquashfs not found — SquashFS image will not be created"
fi

# ── Inject pre-flight health-check service (into installer rootfs AFTER squashfs creation) ──
# This service will NOT be in the installed system (squashfs is already baked).
# It runs once in the QEMU test, reports health, then self-destructs.
if [ "$PREFLIGHT_ENABLED" = "1" ] && mountpoint -q "$ROOT" 2>/dev/null; then
    echo "LOG:Injecting pre-flight health-check service into rootfs..."
    mkdir -p "$ROOT/usr/local/sbin"
    cat > "$ROOT/usr/local/sbin/ethos-preflight.sh" <<'PFSCRIPT'
#!/bin/bash
# EthOS pre-flight check — runs once in test VM, reports via serial console
exec 1>/dev/ttyS0 2>&1
echo "PREFLIGHT:START"
echo "PREFLIGHT:kernel=$(uname -r)"
SYSTEMD_STATE=$(systemctl is-system-running --wait --timeout=30 2>/dev/null || echo unknown)
echo "PREFLIGHT:SYSTEMD:$SYSTEMD_STATE"
# Check whichever EthOS service is expected to be active
# On installer images ethos-preboot.service runs; on installed systems ethos.service runs.
if systemctl is-active ethos.service >/dev/null 2>&1; then
    echo "PREFLIGHT:ETHOS:OK"
elif systemctl is-active ethos-preboot.service >/dev/null 2>&1; then
    echo "PREFLIGHT:ETHOS:PREBOOT_OK"
else
    echo "PREFLIGHT:ETHOS:FAIL"
fi
# Branding validation
if grep -q "ID=ethos" /etc/os-release 2>/dev/null; then
    echo "PREFLIGHT:BRANDING:OK"
else
    echo "PREFLIGHT:BRANDING:FAIL"
fi
# Hardening validation
if [ -f /etc/sysctl.d/91-ethos-security.conf ]; then
    echo "PREFLIGHT:HARDENING:OK"
else
    echo "PREFLIGHT:HARDENING:FAIL"
fi
# Flask available (check venv python, not system python)
if /opt/ethos/venv/bin/python -c "import flask" 2>/dev/null; then
    echo "PREFLIGHT:FLASK:OK"
else
    echo "PREFLIGHT:FLASK:FAIL"
fi
# Critical dependencies (gevent, psutil — most common firstboot pip failures)
if /opt/ethos/venv/bin/python -c "import gevent; import psutil; import flask_socketio" 2>/dev/null; then
    echo "PREFLIGHT:DEPS:OK"
else
    echo "PREFLIGHT:DEPS:FAIL"
fi
echo "PREFLIGHT:DONE"
systemctl disable ethos-preflight.service 2>/dev/null || true
rm -f /usr/local/sbin/ethos-preflight.sh /etc/systemd/system/ethos-preflight.service
systemctl daemon-reload 2>/dev/null || true
# Only power off in QA/beacon mode — when ETHOS_BUILD_HOST is set in ethos.env
# (injected by builder when ETHOS_QA_BUILD_HOST env var is set on the build host).
# Without it the image runs normally so the user can access the web UI.
if grep -q "^ETHOS_BUILD_HOST=" /opt/ethos/ethos.env 2>/dev/null; then
    shutdown -h now
fi
exit 0
PFSCRIPT
    chmod +x "$ROOT/usr/local/sbin/ethos-preflight.sh"
    cat > "$ROOT/etc/systemd/system/ethos-preflight.service" <<'PFSVC'
[Unit]
Description=EthOS Pre-flight Health Check
After=network.target ethos-preboot.service ethos.service
ConditionPathExists=/usr/local/sbin/ethos-preflight.sh

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ethos-preflight.sh
TimeoutStartSec=120
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
PFSVC
    chroot "$ROOT" systemctl enable ethos-preflight.service 2>/dev/null || true
    echo "LOG:Pre-flight service injected into rootfs"
fi

# ── Secure Boot MOK signing ──
echo "LOG:Attempting Secure Boot MOK signing..."
python3 - "$ROOT" "$BRAND_NAME" <<'SBSIGNPY'
import sys, os
sys.path.insert(0, '/opt/ethos/backend/blueprints')
try:
    from builder_secureboot import ensure_mok_keys, sign_rootfs_efi_binaries, install_mok_der_to_esp
    rootfs, brand = sys.argv[1], sys.argv[2]
    if ensure_mok_keys(brand):
        results = sign_rootfs_efi_binaries(rootfs, brand)
        signed = [k for k, v in results.items() if v]
        install_mok_der_to_esp(rootfs)
        if signed:
            print(f"LOG:Secure Boot: signed {{len(signed)}} EFI binaries", flush=True)
            print(f"LOG:Secure Boot: after install run: sudo mokutil --import /boot/efi/EFI/ethos/MOK.der", flush=True)
        else:
            print("LOG:Secure Boot: no EFI binaries signed (sbsign not installed or no targets found)", flush=True)
    else:
        print("LOG:Secure Boot: MOK key generation failed — skipping", flush=True)
except Exception as e:
    print(f"LOG:Secure Boot signing skipped: {{e}}", flush=True)
SBSIGNPY

sync

for m in boot/efi run sys proc dev/shm dev/pts dev; do
    umount "$ROOT/$m" 2>/dev/null || \
        umount -l "$ROOT/$m" 2>/dev/null || true
done
sleep 1
umount "$ROOT" 2>/dev/null || \
    umount -l "$ROOT" 2>/dev/null || true

# ── Step 7b: Inject install image(s) into filesystem ──
ROOT_PART="${{LOOP_DEV}}p2"

if [ -f "$WORK_DIR/ethos-root.sqsh" ]; then
    # SquashFS available — inject as primary install method
    echo "STEP:88:Injecting SquashFS image..."
    mount "$ROOT_PART" "$WORK_DIR/root"
    mkdir -p "$WORK_DIR/root/opt/ethos/installer/images"
    cp "$WORK_DIR/ethos-root.sqsh" "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.sqsh"
    SQSH_FINAL=$(stat -c%s "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.sqsh" 2>/dev/null || echo 0)
    echo "LOG:SquashFS image injected: $((SQSH_FINAL / 1048576))MB"
    rm -f "$WORK_DIR/ethos-root.sqsh"

    # Inject dm-verity data alongside squashfs
    if [ -f "$WORK_DIR/ethos-root.sqsh.verity" ] && [ -f "$WORK_DIR/ethos-root.sqsh.roothash" ]; then
        cp "$WORK_DIR/ethos-root.sqsh.verity" "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.sqsh.verity"
        cp "$WORK_DIR/ethos-root.sqsh.roothash" "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.sqsh.roothash"
        # Also copy to boot/efi for the installed system
        mkdir -p "$WORK_DIR/root/boot/efi/EFI/ethos"
        cp "$WORK_DIR/ethos-root.sqsh.roothash" "$WORK_DIR/root/boot/efi/EFI/ethos/roothash"
        echo "LOG:dm-verity data injected"
        rm -f "$WORK_DIR/ethos-root.sqsh.verity" "$WORK_DIR/ethos-root.sqsh.roothash"
        # Inject manifest (signing artifact)
        if [ -f "$WORK_DIR/ethos-manifest.json" ]; then
            cp "$WORK_DIR/ethos-manifest.json" "$WORK_DIR/root/opt/ethos/installer/images/ethos-manifest.json"
            echo "LOG:Manifest injected into image"
            rm -f "$WORK_DIR/ethos-manifest.json"
        fi
        # Inject SBOM alongside squashfs
        if [ -f "$WORK_DIR/ethos-sbom.json" ]; then
            cp "$WORK_DIR/ethos-sbom.json" "$WORK_DIR/root/opt/ethos/installer/images/ethos-sbom.json"
            echo "LOG:SBOM injected into image"
            rm -f "$WORK_DIR/ethos-sbom.json"
        fi
    fi
    sync
    umount "$WORK_DIR/root" 2>/dev/null || \
        umount -l "$WORK_DIR/root" 2>/dev/null || true
else
    # Fallback: create dd+zstd compressed root image for non-squashfs install
    echo "STEP:88:Creating compressed root image (fallback)..."
    COMPRESSED_IMG="$WORK_DIR/ethos-root.img.zst"

    echo "LOG:Running e2fsck on root partition..."
    e2fsck -f -y "$ROOT_PART" 2>&1 | tail -5 || true

    echo "LOG:Shrinking root filesystem to minimum size..."
    resize2fs -M "$ROOT_PART" 2>&1 | tail -5
    BLOCK_COUNT=$(dumpe2fs -h "$ROOT_PART" 2>/dev/null | awk '/Block count:/ {{print $3}}')
    BLOCK_SIZE=$(dumpe2fs -h "$ROOT_PART" 2>/dev/null | awk '/Block size:/ {{print $3}}')
    if [ -n "$BLOCK_COUNT" ] && [ -n "$BLOCK_SIZE" ]; then
        USED_BYTES=$((BLOCK_COUNT * BLOCK_SIZE))
        SAFE_BYTES=$(( (USED_BYTES * 105 / 100 + 4194303) / 4194304 * 4194304 ))
        DD_COUNT=$((SAFE_BYTES / 4194304))
        echo "LOG:Root partition minimized: ${{BLOCK_COUNT}} blocks x ${{BLOCK_SIZE}}B = $((USED_BYTES / 1048576))MB, dd count=$DD_COUNT"
        DECOMPRESSED_BYTES=$((DD_COUNT * 4194304))
        set -o pipefail
        dd if="$ROOT_PART" bs=4M count=$DD_COUNT status=none | zstd -3 -T0 -o "$COMPRESSED_IMG" 2>&1
        DD_RC=$?
        set +o pipefail
        if [ $DD_RC -ne 0 ]; then
            echo "LOG:WARNING: dd+zstd pipeline failed with code $DD_RC"
        fi
        if [ -f "$COMPRESSED_IMG" ]; then
            COMP_SIZE=$(stat -c%s "$COMPRESSED_IMG" 2>/dev/null || echo 0)
            echo "LOG:Compressed root image: $((COMP_SIZE / 1048576))MB (decompressed: $((DECOMPRESSED_BYTES / 1048576))MB)"
            # Validate the compressed image
            if ! zstd -t "$COMPRESSED_IMG" 2>/dev/null; then
                echo "LOG:WARNING: Compressed image failed integrity check — removing"
                rm -f "$COMPRESSED_IMG"
            fi
        fi
    fi

    echo "LOG:Expanding root filesystem back..."
    resize2fs "$ROOT_PART" 2>&1 | tail -3

    mount "$ROOT_PART" "$WORK_DIR/root"
    if [ -f "$COMPRESSED_IMG" ]; then
        mkdir -p "$WORK_DIR/root/opt/ethos/installer/images"
        cp "$COMPRESSED_IMG" "$WORK_DIR/root/opt/ethos/installer/images/ethos-root.img.zst"
        echo "LOG:Compressed root image injected into filesystem"
        rm -f "$COMPRESSED_IMG"
    fi
    sync
    umount "$WORK_DIR/root" 2>/dev/null || \
        umount -l "$WORK_DIR/root" 2>/dev/null || true
fi

echo "STEP:90:Finalizacja obrazu IMG..."

losetup -d "$LOOP_DEV" 2>/dev/null || true
LOOP_DEV=""

# Move image from tmpfs to persistent storage
mkdir -p "$(dirname "$FINAL_IMG")"
if [ "$USE_TMPFS" -eq 1 ] && [ -f "$OUTPUT_IMG" ]; then
    echo "LOG:Copying image from RAM to disk ($FINAL_IMG)..."
    cp "$OUTPUT_IMG" "$FINAL_IMG"
    rm -f "$OUTPUT_IMG"
    OUTPUT_IMG="$FINAL_IMG"
    echo "LOG:Obraz przeniesiony na dysk"
elif [ "$OUTPUT_IMG" != "$FINAL_IMG" ]; then
    mv "$OUTPUT_IMG" "$FINAL_IMG" 2>/dev/null || cp "$OUTPUT_IMG" "$FINAL_IMG"
    OUTPUT_IMG="$FINAL_IMG"
fi

# Results
IMG_SIZE=$(stat -c%s "$OUTPUT_IMG" 2>/dev/null || echo 0)

# ── Step 8: Pre-flight VM validation ──
if [ "$PREFLIGHT_ENABLED" = "1" ]; then
    echo "STEP:92:Pre-flight VM test — booting image in QEMU..."
    OVMF_FW=""
    for _p in /usr/share/OVMF/OVMF.fd /usr/share/ovmf/OVMF.fd /usr/share/qemu/OVMF.fd \\
              /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd; do
        if [ -f "$_p" ]; then OVMF_FW="$_p"; break; fi
    done
    if [ -z "$OVMF_FW" ]; then
        echo "LOG:Pre-flight: OVMF not found — install 'ovmf' package to enable VM test"
        echo "PREFLIGHT_RESULT:skipped"
    elif ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
        echo "LOG:Pre-flight: qemu-system-x86_64 not found — install 'qemu-system-x86' package"
        echo "PREFLIGHT_RESULT:skipped"
    else
        PFLOG="/tmp/ethos-preflight-$$.serial"
        KVM_OPTS=""
        if [ -e /dev/kvm ]; then
            KVM_OPTS="-enable-kvm -cpu host"
            echo "LOG:Pre-flight: KVM available — hardware acceleration enabled"
        else
            echo "LOG:Pre-flight: KVM not available — using software emulation (may be slow)"
        fi
        echo "LOG:Pre-flight: Starting QEMU (timeout: ${{PREFLIGHT_TIMEOUT}}s)..."
        qemu-system-x86_64 \\
            -bios "$OVMF_FW" \\
            -drive file="$OUTPUT_IMG",format=raw,if=virtio,readonly=on \\
            -m 1024 \\
            -smp 2 \\
            $KVM_OPTS \\
            -nographic \\
            -serial file:"$PFLOG" \\
            -no-reboot \\
            -display none \\
            2>/dev/null &
        QEMU_PID=$!
        PFLIGHT_OK=0
        PFLIGHT_FAIL=0
        _elapsed=0
        while [ $_elapsed -lt "${{PREFLIGHT_TIMEOUT}}" ]; do
            sleep 2
            _elapsed=$((_elapsed + 2))
            if [ -f "$PFLOG" ]; then
                if grep -q "^PREFLIGHT:DONE" "$PFLOG" 2>/dev/null; then
                    PFLIGHT_OK=1
                    break
                fi
                if grep -q "Kernel panic" "$PFLOG" 2>/dev/null; then
                    PFLIGHT_FAIL=1
                    break
                fi
            fi
        done
        kill $QEMU_PID 2>/dev/null
        wait $QEMU_PID 2>/dev/null || true
        if [ "$PFLIGHT_OK" = "1" ]; then
            echo "LOG:Pre-flight: PASSED — image booted and services verified"
            if [ -f "$PFLOG" ]; then
                {{ grep "^PREFLIGHT:" "$PFLOG" 2>/dev/null || true; }} | while IFS= read -r _line; do
                    echo "LOG:VM> $_line"
                done
            fi
            echo "PREFLIGHT_RESULT:ok"
        elif [ "$PFLIGHT_FAIL" = "1" ]; then
            echo "LOG:Pre-flight: FAILED — kernel panic detected"
            echo "PREFLIGHT_RESULT:fail"
        else
            echo "LOG:Pre-flight: TIMEOUT — VM did not report within ${{PREFLIGHT_TIMEOUT}}s"
            echo "PREFLIGHT_RESULT:timeout"
        fi
        rm -f "$PFLOG"
    fi
else
    echo "LOG:Pre-flight VM test disabled"
    echo "PREFLIGHT_RESULT:disabled"
fi

BUILD_DONE=1
echo "STEP:100:Obraz gotowy!"
echo "RESULT_IMG:$OUTPUT_IMG:$IMG_SIZE"
"""


