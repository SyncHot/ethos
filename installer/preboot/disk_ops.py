"""
Disk operations — discovery, partitioning, cloning, GRUB.
"""

import json
import logging
import os
import subprocess
import shlex
import time

log = logging.getLogger("ethos-installer")


def _run(cmd, timeout=30):
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
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
        _run(f"mount {os_dev}2 {mount_dir}", timeout=30)
        os.makedirs(f"{mount_dir}/boot/efi", exist_ok=True)
        _run(f"mount {os_dev}1 {mount_dir}/boot/efi", timeout=15)

        _p("cloning", 35, "Copying system files (this may take a while)...")
        _clone_root(mount_dir, _p)

        # Phase 4: GRUB
        _p("bootloader", 75, "Installing GRUB bootloader...")
        _install_grub(os_dev, mount_dir)

        # Phase 5: fstab
        _p("bootloader", 80, "Configuring fstab...")
        _generate_fstab(os_dev, mount_dir, same_disk)

        # Phase 6: Data disk (if separate)
        if not same_disk and data_dev:
            _p("data", 85, f"Partitioning data disk {data_dev}...")
            _wipe_disk(data_dev)
            _run(f"parted -s {data_dev} mklabel gpt", timeout=30)
            _run(f"parted -s {data_dev} mkpart primary ext4 1MiB 100%", timeout=30)
            _run("partprobe 2>/dev/null && sleep 2", timeout=10)
            _p("data", 90, "Formatting data disk...")
            _run(f"mkfs.ext4 -F -L EthOS-Data {data_dev}1", timeout=120)

        # Phase 7: Cleanup
        _p("finalizing", 95, "Unmounting...")
        _run(f"umount -R {mount_dir} 2>/dev/null", timeout=30)

        _p("done", 100, "Installation complete!")
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
    """Create GPT with: ESP (512M) + root (rest or split with data)."""
    cmds = [
        f"parted -s {dev} mklabel gpt",
        f"parted -s {dev} mkpart ESP fat32 1MiB 513MiB",
        f"parted -s {dev} set 1 esp on",
    ]
    if include_data_part:
        # Same disk: root gets 30% or 30GB (whichever is smaller), rest for data
        cmds.append(f"parted -s {dev} mkpart primary ext4 513MiB 50%")
        cmds.append(f"parted -s {dev} mkpart primary ext4 50% 100%")
    else:
        cmds.append(f"parted -s {dev} mkpart primary ext4 513MiB 100%")

    for cmd in cmds:
        out, err, rc = _run(cmd, timeout=30)
        if rc != 0:
            raise RuntimeError(f"Partition failed: {cmd} → {err}")

    _run("partprobe 2>/dev/null && sleep 2", timeout=10)


def _format_partitions(dev, same_disk):
    """Format created partitions."""
    _, err, rc = _run(f"mkfs.vfat -F32 -n EFI {dev}1", timeout=60)
    if rc != 0:
        raise RuntimeError(f"mkfs.vfat failed: {err}")

    _, err, rc = _run(f"mkfs.ext4 -F -L EthOS-Root {dev}2", timeout=120)
    if rc != 0:
        raise RuntimeError(f"mkfs.ext4 root failed: {err}")

    if same_disk:
        _, err, rc = _run(f"mkfs.ext4 -F -L EthOS-Data {dev}3", timeout=120)
        if rc != 0:
            raise RuntimeError(f"mkfs.ext4 data failed: {err}")


def _clone_root(mount_dir, progress_cb):
    """Clone current root to mount_dir using rsync."""
    excludes = (
        "--exclude=/proc --exclude=/sys --exclude=/dev "
        "--exclude=/run --exclude=/tmp --exclude=/mnt "
        "--exclude=/media --exclude=/lost+found "
        "--exclude=/opt/ethos/installer/images/ethos-x86.img "
        "--exclude=/swapfile --exclude=/var/swap "
        "--exclude=/opt/ethos/data/visual_qa "
        "--exclude=/opt/ethos/logs/copilot_tickets "
    )
    cmd = f"rsync -aAXH {excludes} / {mount_dir}/ 2>&1"
    log.info("Cloning: %s", cmd)

    proc = subprocess.Popen(
        cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
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


def _install_grub(dev, mount_dir):
    """Install GRUB bootloader."""
    import platform
    arch = platform.machine()

    if arch == "x86_64":
        # Bind-mount required filesystems for chroot
        for fs in ("dev", "proc", "sys"):
            _run(f"mount --bind /{fs} {mount_dir}/{fs}", timeout=10)
        _run(f"mount --bind /dev/pts {mount_dir}/dev/pts", timeout=10)

        # Install GRUB EFI
        _, err, rc = _run(
            f"chroot {mount_dir} grub-install --target=x86_64-efi "
            f"--efi-directory=/boot/efi --bootloader-id=EthOS "
            f"--recheck {dev} 2>&1",
            timeout=60,
        )
        if rc != 0:
            # Try legacy BIOS fallback
            log.warning("GRUB EFI failed (%s), trying BIOS...", err)
            _run(
                f"chroot {mount_dir} grub-install --target=i386-pc {dev} 2>&1",
                timeout=60,
            )

        _run(f"chroot {mount_dir} update-grub 2>&1", timeout=60)

        # Unbind
        for fs in ("dev/pts", "sys", "proc", "dev"):
            _run(f"umount {mount_dir}/{fs} 2>/dev/null")

    else:
        log.info("Non-x86 arch (%s): skipping GRUB (assuming U-Boot/other)", arch)


def _generate_fstab(dev, mount_dir, same_disk):
    """Write /etc/fstab for the new system."""
    root_uuid, _, _ = _run(f"blkid -s UUID -o value {dev}2")
    esp_uuid, _, _ = _run(f"blkid -s UUID -o value {dev}1")

    lines = [
        "# EthOS fstab — generated by installer",
        f"UUID={root_uuid}  /          ext4  defaults,noatime  0  1",
        f"UUID={esp_uuid}   /boot/efi  vfat  defaults          0  2",
    ]
    if same_disk:
        data_uuid, _, _ = _run(f"blkid -s UUID -o value {dev}3")
        if data_uuid:
            lines.append(
                f"UUID={data_uuid}  /mnt/data  ext4  defaults,noatime  0  2"
            )

    fstab = "\n".join(lines) + "\n"
    fstab_path = f"{mount_dir}/etc/fstab"
    os.makedirs(os.path.dirname(fstab_path), exist_ok=True)
    with open(fstab_path, "w") as f:
        f.write(fstab)
    log.info("Wrote fstab: %s", fstab_path)
