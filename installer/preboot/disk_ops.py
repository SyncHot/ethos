"""
Disk operations — discovery, partitioning, cloning, GRUB.
"""

import json
import logging
import os
import shutil
import subprocess
import shlex
import time
from datetime import datetime

log = logging.getLogger("ethos-installer")


def _part(dev, n):
    """Return partition device path: /dev/sda1 or /dev/nvme0n1p1."""
    # NVMe and mmcblk devices use 'p' separator before partition number
    if "nvme" in dev or "mmcblk" in dev:
        return f"{dev}p{n}"
    return f"{dev}{n}"


def _run(cmd, timeout=30):
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout.strip(), stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            proc.wait()
        log.warning("Command timed out after %ds: %s", timeout, cmd[:120])
        return "", "timeout", 1
    except Exception as e:
        if proc is not None:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            proc.wait()
        return "", str(e), 1


def _get_boot_device():
    """Find the device we booted from (root filesystem)."""
    out, _, _ = _run("findmnt -n -o SOURCE / 2>/dev/null")
    if not out:
        return None
    # /dev/sda2 → sda
    import re
    m = re.match(r"/dev/(\w+?)p?\d*$", out)
    if m:
        return m.group(1)
    m = re.match(r"/dev/(sd[a-z]+|nvme\d+n\d+|mmcblk\d+)", out)
    if m:
        return m.group(1)
    return None


def _get_persistent_id(dev_name):
    """Map dev name (sda) → persistent /dev/disk/by-id/ name."""
    by_id = "/dev/disk/by-id"
    if not os.path.isdir(by_id):
        return dev_name
    for entry in sorted(os.listdir(by_id)):
        if "-part" in entry:
            continue
        try:
            target = os.path.realpath(os.path.join(by_id, entry))
            if target == f"/dev/{dev_name}":
                return entry
        except OSError:
            continue
    return dev_name


def _smart_status(dev_path):
    """Check SMART health for a device."""
    out, _, rc = _run(f"smartctl -H {shlex.quote(dev_path)} 2>/dev/null", timeout=10)
    if rc == 0 and "PASSED" in out:
        return "ok"
    if "FAILED" in out:
        return "failed"
    return "unknown"


def _smart_temp(dev_path):
    """Get drive temperature in °C."""
    out, _, _ = _run(
        f"smartctl -A {shlex.quote(dev_path)} 2>/dev/null | "
        f"grep -i 'temperature' | head -1",
        timeout=10,
    )
    if out:
        import re
        m = re.search(r"(\d+)\s*$", out)
        if m:
            return int(m.group(1))
    return None


def discover():
    """
    Discover block devices suitable for installation.
    Returns list of disk dicts + boot_device info.
    """
    boot_dev = _get_boot_device()
    log.info("Boot device: %s", boot_dev)

    out, _, rc = _run(
        "lsblk -J -b -o NAME,SIZE,TYPE,MODEL,SERIAL,TRAN,ROTA,HOTPLUG,MOUNTPOINT "
        "2>/dev/null"
    )
    if rc != 0 or not out:
        return {"disks": [], "boot_device": boot_dev, "error": "lsblk failed"}

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"disks": [], "boot_device": boot_dev, "error": "lsblk parse error"}

    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        name = dev["name"]
        size_bytes = int(dev.get("size", 0) or 0)
        if size_bytes < 4 * 1024**3:  # Skip < 4GB
            continue

        # Check if any partition is mounted as root
        is_boot = name == boot_dev
        children = dev.get("children", [])
        mountpoints = []
        for child in children:
            mp = child.get("mountpoint")
            if mp:
                mountpoints.append(mp)

        dev_path = f"/dev/{name}"
        disk = {
            "name": name,
            "path": dev_path,
            "persistent_id": _get_persistent_id(name),
            "size_bytes": size_bytes,
            "size_gb": round(size_bytes / (1024**3), 1),
            "size_human": _human_size(size_bytes),
            "model": (dev.get("model") or "").strip() or "Unknown",
            "serial": (dev.get("serial") or "").strip(),
            "transport": dev.get("tran") or "",
            "rotational": bool(dev.get("rota")),
            "removable": bool(dev.get("hotplug")),
            "is_boot": is_boot,
            "mountpoints": mountpoints,
            "smart_status": _smart_status(dev_path),
            "smart_temp": _smart_temp(dev_path),
            "partitions": len(children),
        }
        disks.append(disk)

    disks.sort(key=lambda d: (d["is_boot"], d["removable"], -d["size_bytes"]))
    return {"disks": disks, "boot_device": boot_dev}


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def validate(os_disk, data_disk, boot_device):
    """
    Validate disk selection.
    os_disk: device name (e.g. 'sda')
    data_disk: device name or None (same as os_disk) or 'same'
    boot_device: current boot device name
    Returns (ok, errors, warnings).
    """
    errors = []
    warnings = []

    if not os_disk:
        errors.append("No system disk selected")
        return False, errors, warnings

    if os_disk == boot_device:
        errors.append("Cannot install on the boot device (USB)")
        return False, errors, warnings

    same_disk = data_disk is None or data_disk == "same" or data_disk == os_disk
    if not same_disk and data_disk == boot_device:
        errors.append("Cannot use boot device as data disk")
        return False, errors, warnings

    smart = _smart_status(f"/dev/{os_disk}")
    if smart == "failed":
        warnings.append(f"SMART failure detected on /dev/{os_disk}")

    if not same_disk:
        smart_d = _smart_status(f"/dev/{data_disk}")
        if smart_d == "failed":
            warnings.append(f"SMART failure detected on /dev/{data_disk}")

    return True, errors, warnings


def install(os_disk, data_disk, progress_cb=None):
    """
    Full installation: partition, clone, install GRUB.
    os_disk: device name ('sda')
    data_disk: device name or None (same disk)
    progress_cb: callable(phase, percent, message)
    """
    same_disk = data_disk is None or data_disk == "same" or data_disk == os_disk
    os_dev = f"/dev/{os_disk}"
    data_dev = f"/dev/{data_disk}" if not same_disk else None

    def _p(phase, pct, msg):
        log.info("Install [%d%%] %s: %s", pct, phase, msg)
        if progress_cb:
            progress_cb(phase, pct, msg)

    try:
        # Phase 1: Wipe + partition OS disk
        _p("partitioning", 5, f"Wiping {os_dev}...")
        _wipe_disk(os_dev)

        _p("partitioning", 10, f"Creating GPT on {os_dev}...")
        _create_gpt(os_dev, same_disk)

        # Phase 2: Format
        _p("partitioning", 20, "Creating filesystems...")
        _format_partitions(os_dev, same_disk)

        # Phase 3: Clone root
        _p("cloning", 30, "Mounting target...")
        mount_dir = "/mnt/ethos-target"
        os.makedirs(mount_dir, exist_ok=True)

        squashfs_img = "/opt/ethos/installer/images/ethos-root.sqsh"
        compressed_img = "/opt/ethos/installer/images/ethos-root.img.zst"
        squashfs_mode = False

        if os.path.isfile(squashfs_img):
            # SquashFS immutable root install (preferred)
            squashfs_mode = True
            _p("cloning", 35, "Installing SquashFS immutable root...")
            _squashfs_install(squashfs_img, _part(os_dev, 2), mount_dir, _p)
            os.makedirs(f"{mount_dir}/boot/efi", exist_ok=True)
            _run(f"mount {_part(os_dev, 1)} {mount_dir}/boot/efi", timeout=15)
        elif os.path.isfile(compressed_img):
            # Fast dd path — write compressed block image directly to partition
            _p("cloning", 35, "Writing system image (fast block copy)...")
            try:
                _dd_clone(compressed_img, _part(os_dev, 2), _p)
                _p("cloning", 65, "Mounting target filesystem...")
                _run(f"mount {_part(os_dev, 2)} {mount_dir}", timeout=30)
            except RuntimeError as e:
                log.warning("dd clone failed: %s — falling back to rsync", e)
                # Re-format partition (dd may have left it corrupt)
                _run(f"mkfs.ext4 -F -L EthOS-Root-A {_part(os_dev, 2)}", timeout=120)
                _run(f"mount {_part(os_dev, 2)} {mount_dir}", timeout=30)
                _p("cloning", 35, "Copying system files (this may take a while)...")
                _clone_root(mount_dir, _p)
            os.makedirs(f"{mount_dir}/boot/efi", exist_ok=True)
            _run(f"mount {_part(os_dev, 1)} {mount_dir}/boot/efi", timeout=15)
        else:
            # Legacy rsync path — file-by-file copy from running USB
            log.info("No compressed image found, falling back to rsync")
            _run(f"mount {_part(os_dev, 2)} {mount_dir}", timeout=30)
            os.makedirs(f"{mount_dir}/boot/efi", exist_ok=True)
            _run(f"mount {_part(os_dev, 1)} {mount_dir}/boot/efi", timeout=15)
            _p("cloning", 35, "Copying system files (this may take a while)...")
            _clone_root(mount_dir, _p)

        if not squashfs_mode:
            # Traditional install: fixup + data separation on ext4 root
            _p("configuring", 73, "Configuring installed system...")
            _fixup_installed_system(mount_dir)
            if same_disk:
                _p("configuring", 74, "Setting up data partition symlinks...")
                _setup_data_separation(mount_dir, _part(os_dev, 4))
        else:
            # SquashFS: data dirs are already symlinks in squashfs image —
            # just create target directories on the data partition
            if same_disk:
                _p("configuring", 74, "Preparing data partition...")
                _prepare_data_dirs(_part(os_dev, 4))

        # Phase 4: GRUB
        _p("bootloader", 75, "Installing GRUB bootloader...")
        _install_grub(os_dev, mount_dir, _p, squashfs_mode=squashfs_mode)

        # Phase 5: fstab
        if squashfs_mode:
            _p("configuring", 80, "Writing fstab to overlay...")
            _write_overlay_fstab(os_dev, mount_dir, same_disk)
        else:
            _p("bootloader", 80, "Configuring fstab...")
            _generate_fstab(os_dev, mount_dir, same_disk)

        # Phase 6: Data disk (if separate)
        if not same_disk and data_dev:
            _p("data", 81, f"Partitioning data disk {data_dev}...")
            _wipe_disk(data_dev)
            _run(f"parted -s {data_dev} mklabel gpt", timeout=30)
            _run(f"parted -s {data_dev} mkpart primary btrfs 1MiB 100%", timeout=30)
            _run("partprobe 2>/dev/null && sleep 2", timeout=10)
            _p("data", 82, "Formatting data disk (Btrfs)...")
            _run(f"mkfs.btrfs -f -L EthOS-Data {_part(data_dev, 1)}", timeout=120)
            _create_btrfs_subvolumes(_part(data_dev, 1))
            if squashfs_mode:
                _prepare_data_dirs(_part(data_dev, 1))
                _write_overlay_fstab(os_dev, mount_dir, same_disk, data_dev=data_dev)
            else:
                _p("configuring", 82, "Setting up data partition symlinks...")
                _setup_data_separation(mount_dir, _part(data_dev, 1))
                _generate_fstab(os_dev, mount_dir, same_disk, data_dev=data_dev)

        # Phase 7: Cleanup
        _p("finalizing", 83, "Unmounting...")
        _run(f"umount -R {mount_dir} 2>/dev/null", timeout=30)

        _p("done", 84, "Disk operations complete")
        return True, ""

    except Exception as e:
        log.error("Installation failed: %s", e, exc_info=True)
        _run(f"umount -R /mnt/ethos-target 2>/dev/null")
        return False, str(e)


def _release_disk(dev):
    """Unmount all partitions, disable swap, and release kernel holds on a disk."""
    import glob as _glob
    # Find all partitions for this device (e.g. /dev/sdb1, /dev/sdb2, ...)
    base = os.path.basename(dev)
    parts = sorted(_glob.glob(f"{dev}[0-9]*") + _glob.glob(f"{dev}p[0-9]*"))
    for part in parts:
        _run(f"umount -f {part} 2>/dev/null", timeout=15)
        _run(f"swapoff {part} 2>/dev/null", timeout=10)
    # Also unmount the whole device in case it's mounted directly
    _run(f"umount -f {dev} 2>/dev/null", timeout=15)
    _run(f"swapoff {dev} 2>/dev/null", timeout=10)
    # Remove any device-mapper mappings (LUKS, LVM) that reference this disk
    _run(f"dmsetup remove_all 2>/dev/null", timeout=15)
    # Tell kernel to drop partition info
    _run(f"blockdev --rereadpt {dev} 2>/dev/null", timeout=10)
    _run("partprobe 2>/dev/null && sleep 1", timeout=10)


def _wipe_disk(dev):
    _release_disk(dev)
    _run(f"wipefs -a {dev} 2>/dev/null", timeout=30)
    _run(f"dd if=/dev/zero of={dev} bs=1M count=10 2>/dev/null", timeout=30)
    _run("partprobe 2>/dev/null && sleep 2", timeout=10)


def _create_gpt(dev, include_data_part):
    """Create GPT with A/B root scheme: ESP (512M) + Root-A (4G) + Root-B (4G) [+ Data].

    Partition layout:
      p1 = ESP    (FAT32, 512MB)   → /boot/efi
      p2 = Root-A (ext4,  4GB)     → / (active after install)
      p3 = Root-B (ext4,  4GB)     → / (used by OTA updates, empty initially)
      p4 = Data   (btrfs, rest)    → /mnt/data (only if same-disk mode)
    """
    cmds = [
        f"parted -s {dev} mklabel gpt",
        f"parted -s {dev} mkpart ESP fat32 1MiB 513MiB",
        f"parted -s {dev} set 1 esp on",
        f"parted -s {dev} mkpart primary ext4 513MiB 4609MiB",   # Root-A: 4096MB
        f"parted -s {dev} mkpart primary ext4 4609MiB 8705MiB",  # Root-B: 4096MB
    ]
    if include_data_part:
        cmds.append(f"parted -s {dev} mkpart primary ext4 8705MiB 100%")  # Data: rest

    for cmd in cmds:
        out, err, rc = _run(cmd, timeout=30)
        if rc != 0:
            raise RuntimeError(f"Partition failed: {cmd} → {err}")

    _run("partprobe 2>/dev/null && sleep 2", timeout=10)


def _format_partitions(dev, same_disk):
    """Format A/B partitions: Root-A + Root-B (empty), optionally Data."""
    _, err, rc = _run(f"mkfs.vfat -F32 -n EFI {_part(dev, 1)}", timeout=60)
    if rc != 0:
        raise RuntimeError(f"mkfs.vfat failed: {err}")

    _, err, rc = _run(f"mkfs.ext4 -F -L EthOS-Root-A {_part(dev, 2)}", timeout=120)
    if rc != 0:
        raise RuntimeError(f"mkfs.ext4 Root-A failed: {err}")

    _, err, rc = _run(f"mkfs.ext4 -F -L EthOS-Root-B {_part(dev, 3)}", timeout=120)
    if rc != 0:
        raise RuntimeError(f"mkfs.ext4 Root-B failed: {err}")

    if same_disk:
        _, err, rc = _run(f"mkfs.btrfs -f -L EthOS-Data {_part(dev, 4)}", timeout=120)
        if rc != 0:
            raise RuntimeError(f"mkfs.btrfs data failed: {err}")
        _create_btrfs_subvolumes(_part(dev, 4))


def _create_btrfs_subvolumes(data_part):
    """Create @data and @snapshots subvolumes on a btrfs partition."""
    tmp_mount = "/tmp/btrfs-setup"
    os.makedirs(tmp_mount, exist_ok=True)
    try:
        _run(f"mount {data_part} {tmp_mount}", timeout=30)
        _run(f"btrfs subvolume create {tmp_mount}/@data", timeout=30)
        _run(f"btrfs subvolume create {tmp_mount}/@snapshots", timeout=30)
        log.info("Created btrfs subvolumes: @data, @snapshots")
    finally:
        _run(f"umount {tmp_mount} 2>/dev/null", timeout=15)


def _clone_root(mount_dir, progress_cb):
    """Clone current root to mount_dir using rsync."""
    excludes = (
        "--exclude=/proc --exclude=/sys --exclude=/dev "
        "--exclude=/run --exclude=/tmp --exclude=/mnt "
        "--exclude=/media --exclude=/lost+found "
        "--exclude=/opt/ethos/installer/images/ethos-x86.img "
        "--exclude=/opt/ethos/installer/images/ethos-root.img.zst "
        "--exclude=/opt/ethos/installer/images/ethos-root.sqsh "
        "--exclude=/swapfile --exclude=/var/swap "
        "--exclude=/opt/ethos/data/visual_qa "
        "--exclude=/opt/ethos/logs/copilot_tickets "
    )
    cmd = f"rsync -aAXH {excludes} / {mount_dir}/ 2>&1"
    log.info("Cloning: %s", cmd)

    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )

    lines = 0
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        lines += 1
        if lines % 5000 == 0:
            pct = min(70, 35 + (lines // 5000) * 5)
            progress_cb("cloning", pct, f"Copied {lines} files...")

    rc = proc.wait()
    if rc not in (0, 24):  # 24 = vanished files (ok during live copy)
        raise RuntimeError(f"rsync failed with code {rc}")

    # Create required mount points
    for d in ("proc", "sys", "dev", "run", "tmp", "mnt", "media"):
        os.makedirs(f"{mount_dir}/{d}", exist_ok=True)


def _dd_clone(compressed_img, target_part, progress_cb):
    """Write compressed root image to target partition using dd+zstd (block-level)."""
    log.info("Fast clone: %s → %s", compressed_img, target_part)

    # Pre-flight checks
    if not os.path.isfile(compressed_img):
        raise RuntimeError(f"Compressed image not found: {compressed_img}")

    img_size = os.path.getsize(compressed_img)
    if img_size < 1024:
        raise RuntimeError(f"Compressed image too small ({img_size} bytes) — likely corrupt")
    log.info("Compressed image size: %d MB", img_size // (1024 * 1024))

    # Check zstdcat is available
    out, err, rc = _run("which zstdcat", timeout=5)
    if rc != 0:
        # Try zstd -d as fallback
        out2, _, rc2 = _run("which zstd", timeout=5)
        if rc2 != 0:
            raise RuntimeError("Neither zstdcat nor zstd found — cannot decompress image")
        decompress_cmd = f"zstd -dc {shlex.quote(compressed_img)}"
        log.info("zstdcat not found, using zstd -dc")
    else:
        decompress_cmd = f"zstdcat {shlex.quote(compressed_img)}"

    # Validate compressed image integrity
    log.info("Validating compressed image...")
    _, verr, vrc = _run(f"zstd -t {shlex.quote(compressed_img)}", timeout=120)
    if vrc != 0:
        raise RuntimeError(f"Compressed image failed integrity check: {verr}")

    # Check target partition exists
    if not os.path.exists(target_part):
        _run("partprobe 2>/dev/null && sleep 2", timeout=10)
        if not os.path.exists(target_part):
            raise RuntimeError(f"Target partition {target_part} does not exist")

    # Decompress and write block-by-block, with pipefail to catch zstd errors
    cmd = f"bash -o pipefail -c '{decompress_cmd} | dd of={target_part} bs=4M conv=fsync status=progress 2>&1'"
    log.info("Running: %s", cmd)
    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )

    last_line = ""
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        line = line.strip()
        if line:
            last_line = line
            log.info("dd: %s", line[:200])

    rc = proc.wait()
    if rc != 0:
        # Collect diagnostics
        diag_parts = [f"dd clone failed with code {rc}"]
        if last_line:
            diag_parts.append(f"last output: {last_line[:200]}")
        # Check disk space
        out, _, _ = _run(f"blockdev --getsize64 {target_part}", timeout=5)
        if out.strip():
            diag_parts.append(f"partition size: {int(out.strip()) // (1024*1024)} MB")
        diag_parts.append(f"image size: {img_size // (1024*1024)} MB")
        raise RuntimeError(" | ".join(diag_parts))

    progress_cb("cloning", 55, "Verifying filesystem...")
    out, err, rc = _run(f"e2fsck -f -y {target_part}", timeout=120)
    if rc not in (0, 1):  # 1 = corrected errors (ok)
        log.warning("e2fsck returned %d: %s", rc, err)

    progress_cb("cloning", 60, "Expanding filesystem to full partition...")
    out, err, rc = _run(f"resize2fs {target_part}", timeout=120)
    if rc != 0:
        raise RuntimeError(f"resize2fs failed: {err}")

    # Set unique label and UUID for this partition
    _run(f"tune2fs -L EthOS-Root-A {target_part}", timeout=15)
    _run(f"tune2fs -U random {target_part}", timeout=15)
    log.info("dd clone complete — filesystem expanded and UUID randomized")


def _squashfs_install(sqsh_img, target_part, mount_dir, progress_cb):
    """Install system using SquashFS immutable root.

    Copies root.sqsh to the ext4 root partition, extracts kernel + initrd
    for GRUB, and creates the overlay directory structure.
    """
    import shutil
    import glob as _glob

    log.info("SquashFS install: %s → %s", sqsh_img, target_part)

    # Mount the target ext4 partition
    os.makedirs(mount_dir, exist_ok=True)
    _run(f"mount {target_part} {mount_dir}", timeout=30)

    # Copy squashfs image to root of ext4 partition
    progress_cb("cloning", 40, "Copying SquashFS image...")
    dst_sqsh = os.path.join(mount_dir, "root.sqsh")
    shutil.copy2(sqsh_img, dst_sqsh)
    sqsh_mb = os.path.getsize(sqsh_img) // (1024 * 1024)
    log.info("Copied root.sqsh (%d MB)", sqsh_mb)

    # Copy dm-verity data if available
    verity_src = sqsh_img + ".verity"
    roothash_src = sqsh_img + ".roothash"
    if os.path.isfile(verity_src) and os.path.isfile(roothash_src):
        shutil.copy2(verity_src, os.path.join(mount_dir, "root.sqsh.verity"))
        shutil.copy2(roothash_src, os.path.join(mount_dir, "root.sqsh.roothash"))
        log.info("dm-verity data copied alongside root.sqsh")
    else:
        log.info("No dm-verity data found — unverified boot")

    # Mount squashfs to extract kernel + initrd (GRUB needs them on ext4)
    progress_cb("cloning", 50, "Extracting kernel and initrd...")
    sqsh_mount = "/tmp/sqsh-extract"
    os.makedirs(sqsh_mount, exist_ok=True)
    out, err, rc = _run(
        f"mount -t squashfs -o ro,loop {dst_sqsh} {sqsh_mount}", timeout=30
    )
    if rc != 0:
        raise RuntimeError(f"Cannot mount squashfs: {err}")

    try:
        boot_dir = os.path.join(mount_dir, "boot")
        os.makedirs(boot_dir, exist_ok=True)

        for pattern in ("vmlinuz-*", "initrd.img-*"):
            files = sorted(_glob.glob(os.path.join(sqsh_mount, "boot", pattern)))
            if files:
                src = files[-1]
                dst = os.path.join(boot_dir, os.path.basename(src))
                shutil.copy2(src, dst)
                log.info("Extracted %s", os.path.basename(src))

        # Copy GRUB modules from squashfs (needed for grub-install)
        sqsh_grub = os.path.join(sqsh_mount, "usr/lib/grub")
        dst_grub = os.path.join(mount_dir, "usr/lib/grub")
        if os.path.isdir(sqsh_grub):
            os.makedirs(os.path.dirname(dst_grub), exist_ok=True)
            shutil.copytree(sqsh_grub, dst_grub, dirs_exist_ok=True)
    finally:
        _run(f"umount {sqsh_mount} 2>/dev/null", timeout=15)

    # Create overlay directory structure
    progress_cb("cloning", 55, "Creating overlay structure...")
    overlay_dir = os.path.join(mount_dir, "overlay")
    os.makedirs(os.path.join(overlay_dir, "upper"), exist_ok=True)
    os.makedirs(os.path.join(overlay_dir, "work"), exist_ok=True)

    progress_cb("cloning", 60, "SquashFS image installed")
    log.info("SquashFS install complete: root.sqsh + kernel/initrd + overlay dirs")


def _prepare_data_dirs(data_part):
    """Create directory structure on data partition for SquashFS mode.

    The squashfs image has symlinks: /opt/ethos/{data,logs,...} → /mnt/data/ethos/{dir}.
    This function creates those target directories on the btrfs data partition.
    """
    tmp_mount = "/tmp/data-prep"
    os.makedirs(tmp_mount, exist_ok=True)
    try:
        out, err, rc = _run(f"mount -o subvol=@data {data_part} {tmp_mount}", timeout=30)
        if rc != 0:
            log.warning("Could not mount data partition for prep: %s", err)
            return
        for dirname in ("data", "logs", "backups", "uploads"):
            os.makedirs(os.path.join(tmp_mount, "ethos", dirname), exist_ok=True)
        log.info("Created data partition directories for squashfs mode")
    finally:
        _run(f"umount {tmp_mount} 2>/dev/null", timeout=15)


def _setup_data_separation(mount_dir, data_part):
    """Move persistent dirs from root to btrfs data partition via symlinks.

    Creates /mnt/data/ethos/{data,logs,backups,uploads} on the data partition,
    moves any existing content from root, and replaces with symlinks.
    This ensures all persistent data survives root A/B updates and factory resets.
    """
    import shutil

    ethos_root = os.path.join(mount_dir, "opt/ethos")
    data_mount = "/tmp/data-setup"
    os.makedirs(data_mount, exist_ok=True)

    try:
        # Mount the @data subvolume
        out, err, rc = _run(f"mount -o subvol=@data {data_part} {data_mount}", timeout=30)
        if rc != 0:
            log.warning("Could not mount data partition for separation: %s", err)
            return

        # Create persistent directory structure on data partition
        ethos_data_root = os.path.join(data_mount, "ethos")
        for dirname in ("data", "logs", "backups", "uploads"):
            target_dir = os.path.join(ethos_data_root, dirname)
            os.makedirs(target_dir, exist_ok=True)

            src_dir = os.path.join(ethos_root, dirname)
            if os.path.isdir(src_dir) and not os.path.islink(src_dir):
                # Move existing content to data partition
                for item in os.listdir(src_dir):
                    s = os.path.join(src_dir, item)
                    d = os.path.join(target_dir, item)
                    if os.path.isdir(s):
                        if os.path.exists(d):
                            shutil.rmtree(d)
                        shutil.copytree(s, d, symlinks=True)
                    else:
                        shutil.copy2(s, d)
                shutil.rmtree(src_dir)
            elif os.path.islink(src_dir):
                os.remove(src_dir)
            elif os.path.exists(src_dir):
                os.remove(src_dir)

            # Create symlink: /opt/ethos/{dir} → /mnt/data/ethos/{dir}
            os.symlink(f"/mnt/data/ethos/{dirname}", src_dir)
            log.info("Symlinked %s → /mnt/data/ethos/%s", dirname, dirname)

        # Create /mnt/data and /mnt/snapshots mount points in target
        os.makedirs(os.path.join(mount_dir, "mnt/data"), exist_ok=True)
        os.makedirs(os.path.join(mount_dir, "mnt/snapshots"), exist_ok=True)

    finally:
        _run(f"umount {data_mount} 2>/dev/null", timeout=15)

    log.info("Data separation complete — persistent dirs on btrfs data partition")


def _fixup_installed_system(mount_dir):
    """Adjust the cloned system for installed-mode operation.

    The source (installer) image carries artifacts that must be removed
    or patched before the target can boot as a normal EthOS instance:
    - .installer-mode flag (would start the preboot installer instead)
    - .installed flag (must be absent so firstboot.sh can run)
    - ethos.service may have wrong port or Type from the builder
    - ethos-preboot.service should be disabled
    - ethos-firstboot.service should be enabled
    """
    ethos_root = os.path.join(mount_dir, "opt/ethos")

    # Remove installer-mode flag so the system boots into normal EthOS
    installer_flag = os.path.join(ethos_root, ".installer-mode")
    if os.path.exists(installer_flag):
        os.remove(installer_flag)
        log.info("Removed .installer-mode flag")

    # Remove .installed so firstboot.sh can run on first boot.
    # firstboot handles: user creation, hostname, setup_done, etc.
    installed_flag = os.path.join(ethos_root, ".installed")
    if os.path.exists(installed_flag):
        os.remove(installed_flag)
        log.info("Removed .installed flag (firstboot will recreate it)")

    # Read target port from ethos.env (default 9000)
    port = "9000"
    env_file = os.path.join(ethos_root, "ethos.env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                if line.startswith("PORT="):
                    port = line.strip().split("=", 1)[1]

    # Write a correct ethos.service for the installed system
    svc_path = os.path.join(mount_dir, "etc/systemd/system/ethos.service")
    svc_content = f"""[Unit]
Description=EthOS NAS
After=network.target ethos-firstboot.service local-fs.target
Wants=network.target
RequiresMountsFor=/mnt/data

[Service]
Type=notify
NotifyAccess=all
WorkingDirectory=/opt/ethos
EnvironmentFile=/opt/ethos/ethos.env
ExecStartPre=/bin/bash -c 'for d in data logs backups uploads; do p="/opt/ethos/$d"; [ -L "$p" ] && mkdir -p "$(readlink "$p")" || mkdir -p "$p"; done'
Environment=PYTHONPATH=/opt/ethos/backend
ExecStart=/opt/ethos/venv/bin/python /opt/ethos/backend/app.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""
    with open(svc_path, "w") as f:
        f.write(svc_content)
    log.info("Wrote ethos.service (port=%s, Type=simple)", port)

    wants_dir = os.path.join(mount_dir, "etc/systemd/system/multi-user.target.wants")
    os.makedirs(wants_dir, exist_ok=True)

    # Disable the preboot installer service on the target
    preboot_link = os.path.join(wants_dir, "ethos-preboot.service")
    if os.path.exists(preboot_link):
        os.remove(preboot_link)
        log.info("Disabled ethos-preboot.service")

    # Enable ethos-firstboot.service so it runs on first boot
    fb_link = os.path.join(wants_dir, "ethos-firstboot.service")
    if not os.path.exists(fb_link):
        try:
            os.symlink(
                "/etc/systemd/system/ethos-firstboot.service", fb_link
            )
            log.info("Enabled ethos-firstboot.service")
        except OSError:
            pass

    # Enable the main ethos service
    ethos_link = os.path.join(wants_dir, "ethos.service")
    if not os.path.exists(ethos_link):
        try:
            os.symlink("/etc/systemd/system/ethos.service", ethos_link)
            log.info("Enabled ethos.service")
        except OSError:
            pass

    # Copy updated firstboot-v2 wrapper to target
    src_fb = os.path.join(ethos_root, "installer/images/firstboot-v2.sh")
    dst_fb = os.path.join(mount_dir, "opt/ethos-firstboot.sh")
    if os.path.exists(src_fb):
        import shutil
        shutil.copy2(src_fb, dst_fb)
        os.chmod(dst_fb, 0o755)
        log.info("Deployed updated firstboot-v2.sh")

    # Create setup_done if wizard is disabled (belt-and-suspenders with firstboot)
    conf_path = os.path.join(ethos_root, "install.conf")
    setup_wizard = "yes"
    hostname = "ethos"
    username = "nasadmin"
    nas_name = "EthOS"
    if os.path.exists(conf_path):
        with open(conf_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("ETHOS_SETUP_WIZARD="):
                    setup_wizard = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("ETHOS_HOSTNAME="):
                    hostname = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("ETHOS_USER="):
                    username = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("ETHOS_NAS_NAME="):
                    nas_name = line.split("=", 1)[1].strip().strip('"')

    if setup_wizard != "yes":
        import json, time
        data_dir = os.path.join(ethos_root, "data")
        os.makedirs(data_dir, exist_ok=True)
        setup_done = os.path.join(data_dir, "setup_done")
        with open(setup_done, "w") as f:
            json.dump({
                "timestamp": int(time.time()),
                "hostname": hostname,
                "username": username,
                "nas_name": nas_name,
            }, f)
        # Password was set during install — skip force-change gate
        pw_marker = os.path.join(ethos_root, ".password_changed")
        with open(pw_marker, "w") as f:
            f.write("installer\n")
        log.info("Created setup_done + .password_changed (wizard=%s)", setup_wizard)


def _install_grub(dev, mount_dir, progress_cb=None, squashfs_mode=False):
    """Install UEFI GRUB bootloader with sub-step progress reporting."""
    import platform
    arch = platform.machine()

    def _p(pct, msg):
        log.info("GRUB [%d%%] %s", pct, msg)
        if progress_cb:
            progress_cb("bootloader", pct, msg)

    if arch == "x86_64":
        # Bind-mount required filesystems for chroot
        _p(75, "Mounting filesystems for chroot...")
        for fs in ("dev", "proc", "sys"):
            _, err, rc = _run(f"mount --bind /{fs} {mount_dir}/{fs}", timeout=10)
            if rc != 0:
                log.warning("mount --bind /%s failed: %s", fs, err)
        _run(f"mount --bind /dev/pts {mount_dir}/dev/pts", timeout=10)

        # Install GRUB UEFI
        _p(76, "Installing GRUB UEFI (x86_64-efi)...")
        _, err, rc = _run(
            f"chroot {mount_dir} grub-install --target=x86_64-efi "
            f"--efi-directory=/boot/efi --boot-directory=/boot "
            f"--removable --no-nvram {dev} 2>&1",
            timeout=120,
        )
        if rc != 0:
            log.error("GRUB UEFI failed (rc=%d): %s", rc, err)
            _p(77, f"GRUB UEFI failed (rc={rc}): {err}")
        else:
            _p(77, "GRUB UEFI installed successfully")

        _p(78, "Generating GRUB configuration (update-grub)...")
        _, uerr, urc = _run(f"chroot {mount_dir} update-grub 2>&1", timeout=120)
        if urc != 0:
            log.warning("update-grub failed (rc=%d): %s", urc, uerr)
            _p(78, f"update-grub warning (rc={urc})")

        # Unbind
        _p(79, "Unmounting chroot filesystems...")
        for fs in ("dev/pts", "sys", "proc", "dev"):
            _run(f"umount {mount_dir}/{fs} 2>/dev/null")

        # Build a standalone BOOTX64.EFI that finds the root by UUID.
        _p(79, "Building standalone EFI bootloader...")
        _write_esp_grub(dev, mount_dir, progress_cb, squashfs_mode=squashfs_mode)

    else:
        log.info("Non-x86 arch (%s): skipping GRUB (assuming U-Boot/other)", arch)


def _write_esp_grub(dev, mount_dir, progress_cb=None, squashfs_mode=False):
    """Create a standalone BOOTX64.EFI + ESP grub.cfg with A/B boot counter.

    Partition layout (target disk):
      p2 = Root-A (active after install)
      p3 = Root-B (empty, used by OTA updates)

    GRUB uses grubenv to track:
      boot_slot    = a | b   (which root to boot)
      boot_counter = 0..3    (incremented on failed boot)
      boot_success = 0 | 1   (set to 1 by EthOS app on successful start)

    Boot counter logic:
      If boot_success != 1 → increment boot_counter
      If boot_counter >= 3 → flip boot_slot (automatic rollback)
      Always set boot_success = 0 before booting (app must set it to 1)
    """
    import glob as _glob
    import tempfile

    def _p(pct, msg):
        log.info("ESP GRUB [%d%%] %s", pct, msg)
        if progress_cb:
            progress_cb("bootloader", pct, msg)

    # Get UUIDs for both root slots and ESP
    root_a_part = _part(dev, 2)
    root_b_part = _part(dev, 3)
    root_a_uuid, _, rc = _run(f"blkid -s UUID -o value {root_a_part}")
    root_b_uuid, _, _ = _run(f"blkid -s UUID -o value {root_b_part}")
    esp_uuid, _, _ = _run(f"blkid -s UUID -o value {_part(dev, 1)}")
    if rc != 0 or not root_a_uuid:
        log.warning("Cannot determine Root-A UUID for %s, skipping ESP GRUB", root_a_part)
        return
    if not root_b_uuid:
        log.warning("Cannot determine Root-B UUID for %s", root_b_part)

    # Find kernel + initrd on the target (Root-A)
    boot_dir = os.path.join(mount_dir, "boot")
    kernels = sorted(_glob.glob(os.path.join(boot_dir, "vmlinuz-*")))
    initrds = sorted(_glob.glob(os.path.join(boot_dir, "initrd.img-*")))
    if not kernels or not initrds:
        log.warning("No kernel/initrd found in %s, skipping ESP GRUB", boot_dir)
        return

    kern_name = os.path.basename(kernels[-1])
    initrd_name = os.path.basename(initrds[-1])
    kver = kern_name.replace("vmlinuz-", "")

    # Read the EthOS version
    version = "EthOS"
    ver_file = os.path.join(mount_dir, "opt/ethos/backend/version.json")
    if os.path.exists(ver_file):
        try:
            with open(ver_file) as f:
                vdata = json.load(f)
            version = f"EthOS v{vdata.get('version', '?')}"
        except Exception:
            pass

    # ── 1) Write ESP grub.cfg with A/B boot counter logic ──
    esp_grub_dir = os.path.join(mount_dir, "boot/efi/EFI/BOOT")
    os.makedirs(esp_grub_dir, exist_ok=True)
    # Also create /boot/grub/ on ESP for grubenv
    boot_grub_dir = os.path.join(mount_dir, "boot/efi/boot/grub")
    os.makedirs(boot_grub_dir, exist_ok=True)

    cmdline = "ro quiet console=tty0 console=ttyS0,115200 net.ifnames=0 biosdevname=0 fsck.repair=preen"
    if squashfs_mode:
        cmdline += " ethos.rootfs=squashfs"

    esp_grub_cfg = os.path.join(esp_grub_dir, "grub.cfg")
    with open(esp_grub_cfg, "w") as f:
        f.write(f"""\
# EthOS A/B boot configuration with automatic failover
set timeout=3
set default=0
insmod part_gpt
insmod ext2
insmod fat
insmod gzio
insmod loadenv

# Load persistent boot state from grubenv
if [ -s $prefix/grubenv ]; then
    load_env
fi

# Defaults for fresh install
if [ -z "$boot_slot" ]; then
    set boot_slot=a
fi
if [ -z "$boot_counter" ]; then
    set boot_counter=0
fi
if [ -z "$boot_success" ]; then
    set boot_success=1
fi

# Boot counter logic: if previous boot didn't mark success, count failure
if [ "$boot_success" != "1" ]; then
    # Increment counter (GRUB math)
    if [ "$boot_counter" = "0" ]; then set boot_counter=1;
    elif [ "$boot_counter" = "1" ]; then set boot_counter=2;
    elif [ "$boot_counter" = "2" ]; then set boot_counter=3;
    else set boot_counter=3;
    fi

    # After 3 failed boots, flip to other slot (automatic rollback)
    if [ "$boot_counter" = "3" ]; then
        if [ "$boot_slot" = "a" ]; then
            set boot_slot=b
        else
            set boot_slot=a
        fi
        set boot_counter=0
    fi
fi

# Clear boot_success — EthOS app must set it to 1 on successful start
set boot_success=0
save_env boot_slot boot_counter boot_success

# Boot the selected slot
if [ "$boot_slot" = "b" ]; then
    search --no-floppy --fs-uuid --set=root {root_b_uuid}
    linux /boot/{kern_name} root=UUID={root_b_uuid} {cmdline} ethos.slot=b
    initrd /boot/{initrd_name}
else
    search --no-floppy --fs-uuid --set=root {root_a_uuid}
    linux /boot/{kern_name} root=UUID={root_a_uuid} {cmdline} ethos.slot=a
    initrd /boot/{initrd_name}
fi

# Manual boot entries (accessible via GRUB menu / Esc)
menuentry "{version} — Slot A" {{
    search --no-floppy --fs-uuid --set=root {root_a_uuid}
    linux /boot/{kern_name} root=UUID={root_a_uuid} {cmdline} ethos.slot=a
    initrd /boot/{initrd_name}
}}
menuentry "{version} — Slot B" {{
    search --no-floppy --fs-uuid --set=root {root_b_uuid}
    linux /boot/{kern_name} root=UUID={root_b_uuid} {cmdline} ethos.slot=b
    initrd /boot/{initrd_name}
}}
menuentry "{version} (recovery)" {{
    search --no-floppy --fs-uuid --set=root {root_a_uuid}
    linux /boot/{kern_name} root=UUID={root_a_uuid} ro single nomodeset fsck.repair=preen ethos.slot=a
    initrd /boot/{initrd_name}
}}
menuentry "EthOS Recovery Shell (ESP)" {{
    search --no-floppy --fs-uuid --set=esp {esp_uuid}
    linux ($esp)/EFI/recovery/vmlinuz ro init=/bin/bash nomodeset
    initrd ($esp)/EFI/recovery/initrd.img
}}
""")
    log.info("Wrote A/B ESP grub.cfg: kernel=%s root_a=%s root_b=%s", kver, root_a_uuid, root_b_uuid)

    # ── 2) Initialize grubenv with default boot state ──
    grubenv_path = os.path.join(boot_grub_dir, "grubenv")
    # grub-editenv creates a 1024-byte grubenv file
    _run(f"grub-editenv {grubenv_path} create", timeout=10)
    _run(f"grub-editenv {grubenv_path} set boot_slot=a", timeout=10)
    _run(f"grub-editenv {grubenv_path} set boot_counter=0", timeout=10)
    _run(f"grub-editenv {grubenv_path} set boot_success=1", timeout=10)
    log.info("Initialized grubenv: boot_slot=a, boot_counter=0, boot_success=1")

    # Also write grubenv to /boot/grub/ on root partition (GRUB may look there)
    root_grub_dir = os.path.join(mount_dir, "boot/grub")
    os.makedirs(root_grub_dir, exist_ok=True)
    root_grubenv = os.path.join(root_grub_dir, "grubenv")
    _run(f"grub-editenv {root_grubenv} create", timeout=10)
    _run(f"grub-editenv {root_grubenv} set boot_slot=a", timeout=10)
    _run(f"grub-editenv {root_grubenv} set boot_counter=0", timeout=10)
    _run(f"grub-editenv {root_grubenv} set boot_success=1", timeout=10)

    # ── 3) Write A/B slot metadata for the updater ──
    slot_meta_dir = os.path.join(mount_dir, "opt/ethos/data")
    os.makedirs(slot_meta_dir, exist_ok=True)
    slot_meta = os.path.join(slot_meta_dir, "ab_slots.json")
    import json as _json
    with open(slot_meta, "w") as f:
        _json.dump({
            "slot_a": {"partition": root_a_part, "uuid": root_a_uuid, "label": "EthOS-Root-A"},
            "slot_b": {"partition": root_b_part, "uuid": root_b_uuid, "label": "EthOS-Root-B"},
            "active": "a",
            "grubenv_esp": "/boot/efi/boot/grub/grubenv",
            "grubenv_root": "/boot/grub/grubenv",
        }, f, indent=2)
    log.info("Wrote A/B slot metadata to %s", slot_meta)

    # ── 4) Build standalone BOOTX64.EFI ──
    _p(79, "Building standalone BOOTX64.EFI...")
    grub_mod_dir = "/usr/lib/grub/x86_64-efi"
    if not os.path.isdir(grub_mod_dir):
        grub_mod_dir = os.path.join(mount_dir, "usr/lib/grub/x86_64-efi")
    if not os.path.isdir(grub_mod_dir):
        log.warning("GRUB x86_64-efi modules not found, skipping standalone EFI rebuild")
        return

    # Early config: find ESP by its UUID, then load main grub.cfg
    # esp_uuid already computed above
    with tempfile.NamedTemporaryFile(mode='w', suffix='.cfg', delete=False) as tmp:
        tmp.write(f"search --no-floppy --fs-uuid --set=root {esp_uuid}\n")
        tmp.write("set prefix=($root)/boot/grub\n")
        tmp.write("configfile ($root)/EFI/BOOT/grub.cfg\n")
        early_cfg = tmp.name

    efi_out = os.path.join(esp_grub_dir, "BOOTX64.EFI")
    _, err, rc = _run(
        f"grub-mkstandalone --format=x86_64-efi "
        f"--output={efi_out} --locales='' --fonts='' "
        f"--modules='part_gpt ext2 fat search search_fs_uuid normal "
        f"linux boot configfile gzio loadenv' "
        f"'boot/grub/grub.cfg={early_cfg}'",
        timeout=120,
    )
    os.unlink(early_cfg)

    if rc != 0:
        log.warning("grub-mkstandalone failed: %s", err)
    else:
        log.info("Built standalone BOOTX64.EFI (A/B aware, ESP UUID %s)", esp_uuid)

    # ── 5) Copy kernel + initrd to ESP for recovery ──
    _setup_recovery_on_esp(mount_dir, esp_grub_dir, kern_name, initrd_name)

    # ── 6) Copy dm-verity roothash to ESP ──
    roothash_src = os.path.join(mount_dir, "root.sqsh.roothash")
    if os.path.isfile(roothash_src):
        esp_base = os.path.dirname(os.path.dirname(esp_grub_dir))
        ethos_dir = os.path.join(esp_base, "EFI", "ethos")
        os.makedirs(ethos_dir, exist_ok=True)
        shutil.copy2(roothash_src, os.path.join(ethos_dir, "roothash"))
        log.info("dm-verity roothash copied to ESP")


def _setup_recovery_on_esp(mount_dir, esp_grub_dir, kern_name, initrd_name):
    """Copy kernel + initrd to ESP recovery directory.

    This provides a last-resort recovery boot when both A/B root slots are
    damaged. GRUB's 'EthOS Recovery Shell' entry boots this kernel with
    'init=/bin/bash' for manual repair.
    """
    esp_base = os.path.dirname(os.path.dirname(esp_grub_dir))  # …/boot/efi
    recovery_dir = os.path.join(esp_base, "EFI", "recovery")
    os.makedirs(recovery_dir, exist_ok=True)

    boot_dir = os.path.join(mount_dir, "boot")
    kern_src = os.path.join(boot_dir, kern_name)
    initrd_src = os.path.join(boot_dir, initrd_name)

    if os.path.isfile(kern_src):
        shutil.copy2(kern_src, os.path.join(recovery_dir, "vmlinuz"))
        log.info("Recovery kernel copied to ESP: %s", kern_name)
    else:
        log.warning("Recovery: kernel %s not found", kern_src)
        return

    if os.path.isfile(initrd_src):
        shutil.copy2(initrd_src, os.path.join(recovery_dir, "initrd.img"))
        log.info("Recovery initrd copied to ESP: %s", initrd_name)

    # Write a small recovery info file
    import json as _json
    with open(os.path.join(recovery_dir, "recovery.json"), "w") as f:
        _json.dump({
            "kernel": kern_name,
            "initrd": initrd_name,
            "created": datetime.now().isoformat() if 'datetime' in dir() else "unknown",
        }, f, indent=2)

    log.info("Recovery system installed on ESP at EFI/recovery/")


def _generate_fstab(dev, mount_dir, same_disk, data_dev=None):
    """Write /etc/fstab for the new system (A/B layout: root on p2)."""
    root_uuid, _, _ = _run(f"blkid -s UUID -o value {_part(dev, 2)}")
    esp_uuid, _, _ = _run(f"blkid -s UUID -o value {_part(dev, 1)}")

    lines = [
        "# EthOS fstab — generated by installer (A/B partition scheme)",
        f"UUID={root_uuid}  /          ext4   defaults,noatime  0  1",
        f"UUID={esp_uuid}   /boot/efi  vfat   defaults          0  2",
    ]

    # Data partition — same disk (p4) or separate disk (p1)
    data_part = None
    if same_disk:
        data_part = _part(dev, 4)
    elif data_dev:
        data_part = _part(data_dev, 1)

    if data_part:
        data_uuid, _, _ = _run(f"blkid -s UUID -o value {data_part}")
        if data_uuid:
            lines.append(
                f"UUID={data_uuid}  /mnt/data  btrfs  subvol=@data,defaults,noatime,compress=zstd:3,space_cache=v2  0  0"
            )
            lines.append(
                f"UUID={data_uuid}  /mnt/snapshots  btrfs  subvol=@snapshots,defaults,noatime,compress=zstd:3  0  0"
            )

    fstab = "\n".join(lines) + "\n"
    fstab_path = f"{mount_dir}/etc/fstab"
    os.makedirs(os.path.dirname(fstab_path), exist_ok=True)
    with open(fstab_path, "w") as f:
        f.write(fstab)
    log.info("Wrote fstab: %s", fstab_path)


def _write_overlay_fstab(dev, mount_dir, same_disk, data_dev=None):
    """Write fstab to overlay upper dir (for SquashFS mode).

    In squashfs mode, the root is overlayfs (managed by initramfs).
    Only ESP and data partition entries are needed in fstab.
    The fstab goes into the overlay upper so it overrides the squashfs version.
    """
    esp_uuid, _, _ = _run(f"blkid -s UUID -o value {_part(dev, 1)}")

    lines = [
        "# EthOS fstab — SquashFS immutable root mode",
        "# Root: overlayfs (squashfs lower + ext4 overlay upper, managed by initramfs)",
        f"UUID={esp_uuid}   /boot/efi  vfat   defaults  0  2",
    ]

    data_part = None
    if same_disk:
        data_part = _part(dev, 4)
    elif data_dev:
        data_part = _part(data_dev, 1)

    if data_part:
        data_uuid, _, _ = _run(f"blkid -s UUID -o value {data_part}")
        if data_uuid:
            lines.append(
                f"UUID={data_uuid}  /mnt/data  btrfs  subvol=@data,defaults,noatime,compress=zstd:3,space_cache=v2  0  0"
            )
            lines.append(
                f"UUID={data_uuid}  /mnt/snapshots  btrfs  subvol=@snapshots,defaults,noatime,compress=zstd:3  0  0"
            )

    fstab = "\n".join(lines) + "\n"

    # Write to overlay upper directory (overrides squashfs fstab on boot)
    overlay_etc = os.path.join(mount_dir, "overlay/upper/etc")
    os.makedirs(overlay_etc, exist_ok=True)
    fstab_path = os.path.join(overlay_etc, "fstab")
    with open(fstab_path, "w") as f:
        f.write(fstab)
    log.info("Wrote overlay fstab: %s", fstab_path)
