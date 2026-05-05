"""EthOS Storage — Filesystem & Pool Routes

Routes: /api/storage/relabel, /api/storage/format/*, /api/storage/merge,
        /api/storage/partition, /api/storage/deps/*, /api/storage/analyze/*,
        /api/storage/pool/*
"""
import json
import os
import re
import sys
from flask import jsonify, request, Response, stream_with_context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path, apt_install as _apt_install
from utils import fmt_bytes, require_tools
from blueprints.admin_required import admin_required
from blueprints.storage import (
    storage_bp,
    host_run, host_run_stream, Q,
    _validate_drive_name, _validate_disk_name, _is_system_disk,
    _invalidate_drives_cache,
)

# ═══════════════════════════════════════════════════════════
#  Disk Formatting
# ═══════════════════════════════════════════════════════════

FORMAT_FS_OPTIONS = [
    {"value": "ext4",  "label": "ext4",  "desc": "Best for Linux — fast, reliable, journaling", "linux": True,  "windows": False, "mac": False},
    {"value": "btrfs", "label": "Btrfs", "desc": "Modern Linux — snapshots, compression, RAID", "linux": True,  "windows": False, "mac": False},
    {"value": "xfs",   "label": "XFS",   "desc": "High-performance Linux — large files, servers", "linux": True,  "windows": False, "mac": False},
    {"value": "ntfs",  "label": "NTFS",  "desc": "Windows — full Windows compatibility", "linux": True,  "windows": True,  "mac": True},
    {"value": "exfat", "label": "exFAT", "desc": "Universal — Windows, Mac, Linux, large files", "linux": True,  "windows": True,  "mac": True},
    {"value": "vfat",  "label": "FAT32", "desc": "Maximum compatibility — 4 GB file size limit", "linux": True,  "windows": True,  "mac": True},
    {"value": "ext3",  "label": "ext3",  "desc": "Legacy Linux — journaling, compatibility", "linux": True,  "windows": False, "mac": False},
    {"value": "f2fs",  "label": "F2FS",  "desc": "Flash-Friendly — SSD, USB drives, SD cards", "linux": True,  "windows": False, "mac": False},
]

# Mapping from fs value to mkfs command
MKFS_CMDS = {
    "ext4":  "mkfs.ext4 -F",
    "ext3":  "mkfs.ext3 -F",
    "btrfs": "mkfs.btrfs -f",
    "xfs":   "mkfs.xfs -f",
    "ntfs":  "mkfs.ntfs -f",
    "exfat": "mkfs.exfat",
    "vfat":  "mkfs.vfat -F 32",
    "f2fs":  "mkfs.f2fs -f",
}


@storage_bp.route('/relabel', methods=['POST'])
def relabel_drive():
    """Change the filesystem label of a partition.
    Expects JSON: { drive: 'sdb2', label: 'NewLabel' }
    For ext2/3/4 uses e2label, for ntfs uses ntfslabel, for fat uses fatlabel, for exfat uses exfatlabel.
    Ext can be relabeled while mounted; NTFS/FAT must be unmounted first.
    """
    data = request.json or {}
    drive = data.get('drive', '').strip()
    new_label = data.get('label', '').strip()

    if not drive or not re.match(r'^[a-zA-Z0-9]+$', drive):
        return jsonify({'error': 'Invalid drive name'}), 400
    if not new_label:
        return jsonify({'error': 'Label is required'}), 400
    # Sanitize label: no slashes, max 32 chars
    if '/' in new_label or len(new_label) > 32:
        return jsonify({'error': 'Label must be max 32 chars, no slashes'}), 400

    dev_path = f'/dev/{drive}'

    # Detect filesystem type
    r = host_run(f"lsblk -no FSTYPE {Q(dev_path)} 2>/dev/null")
    if r.returncode != 0 or not r.stdout.strip():
        return jsonify({'error': 'Cannot detect filesystem type'}), 400
    fstype = r.stdout.strip().lower()

    # Check if mounted
    r_mp = host_run(f"findmnt -n -o TARGET {Q(dev_path)} 2>/dev/null")
    is_mounted = r_mp.returncode == 0 and r_mp.stdout.strip() != ''
    mountpoint = r_mp.stdout.strip() if is_mounted else None

    # Build the label command based on filesystem
    # For filesystems that require unmount, handle it automatically
    needs_remount = False
    if fstype in ('ext2', 'ext3', 'ext4'):
        cmd = f"e2label {Q(dev_path)} {Q(new_label)}"
    elif fstype in ('ntfs', 'fuseblk'):
        if is_mounted:
            r_um = host_run(f"umount {Q(mountpoint)}")
            if r_um.returncode != 0:
                r_um = host_run(f"umount -l {Q(mountpoint)}")
                if r_um.returncode != 0:
                    return jsonify({'error': 'Failed to unmount NTFS disk'}), 500
            needs_remount = True
        cmd = f"ntfslabel --force {Q(dev_path)} {Q(new_label)}"
    elif fstype in ('vfat', 'fat32', 'fat16'):
        if is_mounted:
            r_um = host_run(f"umount {Q(mountpoint)}")
            if r_um.returncode != 0:
                return jsonify({'error': 'Failed to unmount FAT disk'}), 500
            needs_remount = True
        cmd = f"fatlabel {Q(dev_path)} {Q(new_label)}"
    elif fstype == 'exfat':
        if is_mounted:
            r_um = host_run(f"umount {Q(mountpoint)}")
            if r_um.returncode != 0:
                return jsonify({'error': 'Failed to unmount exFAT disk'}), 500
            needs_remount = True
        cmd = f"exfatlabel {Q(dev_path)} {Q(new_label)}"
    elif fstype == 'btrfs':
        cmd = f"btrfs filesystem label {Q(dev_path)} {Q(new_label)}"
    elif fstype == 'xfs':
        if is_mounted:
            r_um = host_run(f"umount {Q(mountpoint)}")
            if r_um.returncode != 0:
                return jsonify({'error': 'Failed to unmount XFS disk'}), 500
            needs_remount = True
        cmd = f"xfs_admin -L {Q(new_label)} {Q(dev_path)}"
    else:
        return jsonify({'error': f'Label change is not supported for {fstype}'}), 400

    r = host_run(cmd, timeout=15)
    if r.returncode != 0:
        # If we unmounted, try to remount before returning error
        if needs_remount and mountpoint:
            host_run(f"mount {Q(dev_path)} {Q(mountpoint)}", timeout=10)
        return jsonify({'error': f'Error: {r.stderr.strip() or r.stdout.strip()}'}), 500

    # Remount at new label path if it was auto-unmounted
    if needs_remount and mountpoint:
        # Mount to a path based on new label
        new_mp = f"/media/devmon/{new_label}"
        host_run(f"mkdir -p {Q(new_mp)}", timeout=5)
        r_rm = host_run(f"mount {Q(dev_path)} {Q(new_mp)}", timeout=15)
        if r_rm.returncode != 0:
            # Fallback: remount at old path
            host_run(f"mount {Q(dev_path)} {Q(mountpoint)}", timeout=10)
        else:
            # Clean up old empty mountpoint dir
            host_run(f"rmdir {Q(mountpoint)} 2>/dev/null", timeout=5)

    return jsonify({'success': True, 'drive': drive, 'label': new_label, 'fstype': fstype})


@storage_bp.route('/format/options')
def format_options():
    """Return available filesystem options with system recommendation."""
    # Detect current system filesystem
    r = host_run("findmnt -n -o FSTYPE / 2>/dev/null")
    system_fs = r.stdout.strip() if r.returncode == 0 else "ext4"

    # Check which mkfs tools are available
    options = []
    for fs in FORMAT_FS_OPTIONS:
        cmd = MKFS_CMDS.get(fs["value"], "").split()[0]
        if cmd:
            avail_r = host_run(f"command -v {cmd}")
            available = avail_r.returncode == 0
        else:
            available = False
        options.append({
            **fs,
            "available": available,
            "recommended": fs["value"] == system_fs,
        })

    return jsonify({
        "system_fs": system_fs,
        "options": options,
    })


@storage_bp.route('/format', methods=['POST'])
def format_drive():
    """Format a drive with a specified filesystem.
    Expects JSON: { drive: 'sda1', fstype: 'ext4', label: 'MyDisk' }
    The drive MUST be unmounted first.
    """
    data = request.json or {}
    drive_name = data.get("drive", "").strip()
    fstype = data.get("fstype", "").strip()
    label = data.get("label", "").strip()

    if not drive_name or not fstype:
        return jsonify({"error": "drive and fstype are required"}), 400

    # Validate drive name (prevent injection)
    if not re.match(r'^[a-zA-Z0-9]+$', drive_name):
        return jsonify({"error": "Invalid device name"}), 400

    # Validate fstype
    if fstype not in MKFS_CMDS:
        return jsonify({"error": f"Unsupported filesystem: {fstype}"}), 400

    dev_path = f"/dev/{drive_name}"

    # Check device exists
    r = host_run(f"lsblk -no NAME {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": f"Device {dev_path} does not exist"}), 404

    # Check device is not mounted (including child partitions)
    r = host_run(f"findmnt -n -o TARGET {dev_path} 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        return jsonify({"error": f"Device is mounted on {r.stdout.strip()}. Unmount first."}), 400

    # Also check child devices (e.g. sda has sda1 mounted)
    r = host_run(f"lsblk -no MOUNTPOINT {dev_path} 2>/dev/null")
    if r.returncode == 0:
        mounted_children = [mp.strip() for mp in r.stdout.strip().split('\n') if mp.strip()]
        if mounted_children:
            # Check for system mounts first
            for mp in mounted_children:
                if mp in ('/', '/boot', '/boot/efi', '/home'):
                    return jsonify({"error": f"Device is in use by system ({mp})!"}), 403
            # Any other mount — must unmount first
            return jsonify({"error": f"Partition is mounted on {mounted_children[0]}. Unmount first."}), 400

    # Safety: refuse to format system disk (robust check incl. LVM)
    # Extract base disk name for system check
    _base_disk = re.sub(r'p?\d+$', '', drive_name)
    if _is_system_disk(drive_name) or (_base_disk != drive_name and _is_system_disk(_base_disk)):
        return jsonify({"error": "Cannot format system disk!"}), 403

    # Build mkfs command
    mkfs_base = MKFS_CMDS[fstype]
    label_flag = ""
    if label:
        safe_label = re.sub(r'[^a-zA-Z0-9_\-.]', '_', label)[:16]
        if fstype in ("ext4", "ext3", "btrfs", "f2fs"):
            label_flag = f"-L {Q(safe_label)}"
        elif fstype == "xfs":
            label_flag = f"-L {Q(safe_label)}"
        elif fstype == "ntfs":
            label_flag = f"-L {Q(safe_label)}"
        elif fstype in ("exfat", "vfat"):
            label_flag = f"-n {Q(safe_label[:11])}"

    cmd = f"{mkfs_base} {label_flag} {dev_path} 2>&1"

    def generate():
        yield f"data: {json.dumps({'type': 'step', 'message': f'Formatting {dev_path} as {fstype}...'})}\n\n"
        if label:
            yield f"data: {json.dumps({'type': 'step', 'message': f'Label: {label}'})}\n\n"

        for line in host_run_stream(cmd):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = int(line.split(":")[1])
                if code == 0:
                    # For ext filesystems, set periodic fsck (every 30 mounts or 90 days)
                    if fstype in ('ext4', 'ext3', 'ext2'):
                        host_run(f'tune2fs -c 30 -i 90d {dev_path} 2>/dev/null', timeout=10)
                        yield f"data: {json.dumps({'type': 'step', 'message': 'Periodic fsck configured (30 mounts / 90 days)'})}\n\n"
                    yield f"data: {json.dumps({'type': 'step', 'message': 'Format completed successfully!'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': True})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'step', 'message': f'Format error (code: {code})'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Merge Partitions (repartition to single partition + format)
# ---------------------------------------------------------------------------

@storage_bp.route('/merge', methods=['POST'])
def merge_partitions():
    """Merge all partitions on a disk into one and format it.
    Expects JSON: { disk: 'sdb', fstype: 'ext4', label: 'MyDisk' }
    ALL partitions on disk MUST be unmounted.
    """
    data = request.json or {}
    disk_name = data.get("disk", "").strip()
    fstype = data.get("fstype", "").strip()
    label = data.get("label", "").strip()

    if not disk_name or not fstype:
        return jsonify({"error": "disk and fstype are required"}), 400

    # Validate disk name (only base disk, no partition numbers)
    if not _validate_disk_name(disk_name):
        return jsonify({"error": "Invalid disk name (provide base disk e.g. sdb, not a partition)"}), 400

    if fstype not in MKFS_CMDS:
        return jsonify({"error": f"Unsupported filesystem: {fstype}"}), 400

    dev_path = f"/dev/{disk_name}"

    # Check device exists and is a disk
    r = host_run(f"lsblk -ndo TYPE {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": f"Device {dev_path} does not exist"}), 404
    if r.stdout.strip() != 'disk':
        return jsonify({"error": f"{dev_path} is not a base disk"}), 400

    # Get all partitions on this disk
    r = host_run(f"lsblk -nlo NAME,MOUNTPOINT {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": "Cannot read partitions"}), 500

    parts_mounted = []
    for line in r.stdout.strip().split('\n'):
        parts = line.split(None, 1)
        if len(parts) >= 2 and parts[1].strip():
            pname = parts[0].strip()
            mp = parts[1].strip()
            if pname != disk_name:  # Skip the disk itself
                parts_mounted.append(f"{pname} → {mp}")

    if parts_mounted:
        return jsonify({"error": f"Partitions are mounted: {', '.join(parts_mounted)}. Unmount them first."}), 400

    # Safety: refuse system disk (robust check incl. LVM)
    if _is_system_disk(disk_name):
        return jsonify({"error": "Cannot merge partitions of system disk!"}), 403

    # Build label flags
    safe_label = ""
    if label:
        safe_label = re.sub(r'[^a-zA-Z0-9_\-.]', '_', label)[:16]

    def generate():
        yield f"data: {json.dumps({'type': 'step', 'message': f'Merging partitions on {dev_path}...'})}\n\n"

        # Step 1: Wipe existing partition signatures
        yield f"data: {json.dumps({'type': 'step', 'message': 'Wiping partition table...'})}\n\n"
        for line in host_run_stream(f"wipefs -a {dev_path} 2>&1"):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = int(line.split(":")[1])
                if code != 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': f'wipefs error (code: {code})'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                    return
            else:
                if line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Step 2: Create new GPT partition table with single partition
        yield f"data: {json.dumps({'type': 'step', 'message': 'Creating new GPT partition table...'})}\n\n"
        parted_cmd = f"parted -s {dev_path} mklabel gpt mkpart primary 0% 100% 2>&1"
        for line in host_run_stream(parted_cmd):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = int(line.split(":")[1])
                if code != 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': f'parted error (code: {code})'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                    return
            else:
                if line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Wait for kernel to re-read partition table
        yield f"data: {json.dumps({'type': 'step', 'message': 'Refreshing partition table...'})}\n\n"
        host_run(f"partprobe {dev_path} 2>/dev/null")
        host_run("sleep 2")

        # Determine new partition name (sdb1, nvme0n1p1, etc.)
        if 'nvme' in disk_name:
            new_part = f"{dev_path}p1"
            new_part_name = f"{disk_name}p1"
        else:
            new_part = f"{dev_path}1"
            new_part_name = f"{disk_name}1"

        # Auto-unmount in case devmon/udisks auto-mounted the new partition
        host_run(f"umount {new_part} 2>/dev/null")
        host_run("sleep 1")

        # Verify new partition exists
        r2 = host_run(f"lsblk -no NAME {new_part} 2>/dev/null")
        if r2.returncode != 0:
            yield f"data: {json.dumps({'type': 'step', 'message': f'Partition {new_part} was not created'})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'step', 'message': f'Created partition {new_part_name}'})}\n\n"

        # Step 3: Format the new partition
        mkfs_base = MKFS_CMDS[fstype]
        label_flag = ""
        if safe_label:
            if fstype in ("ext4", "ext3", "btrfs", "f2fs", "xfs"):
                label_flag = f"-L {Q(safe_label)}"
            elif fstype == "ntfs":
                label_flag = f"-L {Q(safe_label)}"
            elif fstype in ("exfat", "vfat"):
                label_flag = f"-n {Q(safe_label[:11])}"

        fmt_cmd = f"{mkfs_base} {label_flag} {new_part} 2>&1"
        yield f"data: {json.dumps({'type': 'step', 'message': f'Formatting as {fstype}...'})}\n\n"

        for line in host_run_stream(fmt_cmd):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = int(line.split(":")[1])
                if code == 0:
                    if fstype in ('ext4', 'ext3', 'ext2'):
                        host_run(f'tune2fs -c 30 -i 90d {new_part} 2>/dev/null', timeout=10)
                    yield f"data: {json.dumps({'type': 'step', 'message': 'Format completed successfully!'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': True, 'partition': new_part_name})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'step', 'message': f'Format error (code: {code})'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
            else:
                if line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Split / Partition a Disk
# ---------------------------------------------------------------------------

@storage_bp.route('/partition', methods=['POST'])
def partition_disk():
    """Repartition a disk into multiple partitions and format each.
    Expects JSON: {
        disk: 'sdb',
        partitions: [
            { size_mb: 50000, fstype: 'ext4', label: 'Dane' },
            { size_mb: 0, fstype: 'ntfs', label: 'Media' },   // 0 = remaining space
        ]
    }
    ALL existing partitions MUST be unmounted.
    """
    data = request.json or {}
    disk_name = data.get("disk", "").strip()
    partitions = data.get("partitions", [])

    if not disk_name:
        return jsonify({"error": "Disk name is required"}), 400
    if not partitions or len(partitions) < 1:
        return jsonify({"error": "Provide at least one partition"}), 400
    if len(partitions) > 8:
        return jsonify({"error": "Maximum 8 partitions"}), 400

    # Validate disk name
    if not _validate_disk_name(disk_name):
        return jsonify({"error": "Invalid disk name"}), 400

    # Validate each partition
    for i, p in enumerate(partitions):
        fs = p.get("fstype", "").strip()
        if not fs:
            return jsonify({"error": f"Partition {i+1}: no filesystem specified"}), 400
        if fs not in MKFS_CMDS:
            return jsonify({"error": f"Partition {i+1}: unsupported filesystem '{fs}'"}), 400

    # Count how many use size_mb=0 (remaining space)
    remaining_count = sum(1 for p in partitions if not p.get("size_mb"))
    if remaining_count > 1:
        return jsonify({"error": "Only one partition can use 'remaining space'"}), 400

    dev_path = f"/dev/{disk_name}"

    # Check device exists and is a disk
    r = host_run(f"lsblk -ndo TYPE {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": f"Device {dev_path} does not exist"}), 404
    if r.stdout.strip() != 'disk':
        return jsonify({"error": f"{dev_path} is not a base disk"}), 400

    # Get disk size in bytes
    r = host_run(f"lsblk -ndbo SIZE {dev_path} 2>/dev/null")
    if r.returncode != 0:
        return jsonify({"error": "Cannot read disk size"}), 500
    disk_bytes = int(r.stdout.strip())
    disk_mb = disk_bytes // (1024 * 1024)

    # Validate total size
    total_requested_mb = sum(p.get("size_mb", 0) for p in partitions)
    if total_requested_mb > disk_mb:
        return jsonify({"error": f"Total partition size ({total_requested_mb} MB) exceeds disk size ({disk_mb} MB)"}), 400

    # Check all partitions unmounted
    r = host_run(f"lsblk -nlo NAME,MOUNTPOINT {dev_path} 2>/dev/null")
    if r.returncode == 0:
        for line in r.stdout.strip().split('\n'):
            cols = line.split(None, 1)
            if len(cols) >= 2 and cols[1].strip() and cols[0].strip() != disk_name:
                return jsonify({"error": f"Partition {cols[0].strip()} is mounted on {cols[1].strip()}. Unmount first."}), 400

    # Safety: refuse system disk (robust check incl. LVM)
    if _is_system_disk(disk_name):
        return jsonify({"error": "Cannot partition system disk!"}), 403

    def _label_flag(fstype, label):
        if not label:
            return ""
        safe = re.sub(r'[^a-zA-Z0-9_\-.]', '_', label)[:16]
        if fstype in ("ext4", "ext3", "btrfs", "f2fs", "xfs", "ntfs"):
            return f"-L {Q(safe)}"
        elif fstype in ("exfat", "vfat"):
            return f"-n {Q(safe[:11])}"
        return ""

    def generate():
        yield f"data: {json.dumps({'type': 'step', 'message': f'Partitioning {dev_path} into {len(partitions)} partitions...'})}\n\n"

        # Step 1: Wipe
        yield f"data: {json.dumps({'type': 'step', 'message': 'Wiping partition table...'})}\n\n"
        for line in host_run_stream(f"wipefs -a {dev_path} 2>&1"):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                if int(line.split(":")[1]) != 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': 'wipefs error'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                    return
            elif line.strip():
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Step 2: Create GPT label
        yield f"data: {json.dumps({'type': 'step', 'message': 'Creating GPT table...'})}\n\n"
        for line in host_run_stream(f"parted -s {dev_path} mklabel gpt 2>&1"):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                if int(line.split(":")[1]) != 0:
                    yield f"data: {json.dumps({'type': 'step', 'message': 'parted mklabel error'})}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                    return
            elif line.strip():
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Step 3: Create partitions
        # Build parted mkpart commands with MB offsets
        offset_mb = 1  # Start at 1 MiB for alignment
        part_specs = []
        for i, p in enumerate(partitions):
            size_mb = p.get("size_mb", 0)
            if size_mb > 0:
                end_mb = offset_mb + size_mb
                part_specs.append((offset_mb, end_mb, p))
                offset_mb = end_mb
            else:
                # Remaining space — use 100%
                part_specs.append((offset_mb, -1, p))  # -1 means "100%"

        for idx, (start, end, p) in enumerate(part_specs):
            pnum = idx + 1
            end_str = "100%" if end == -1 else f"{end}MiB"
            parted_cmd = f"parted -s {dev_path} mkpart primary {start}MiB {end_str} 2>&1"
            yield f"data: {json.dumps({'type': 'step', 'message': f'Creating partition {pnum}/{len(partitions)}...'})}\n\n"
            for line in host_run_stream(parted_cmd):
                line = line.rstrip("\n")
                if line.startswith("__EXIT_CODE__:"):
                    if int(line.split(":")[1]) != 0:
                        yield f"data: {json.dumps({'type': 'step', 'message': f'Error creating partition {pnum}'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                        return
                elif line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

        # Step 4: Re-read partition table
        yield f"data: {json.dumps({'type': 'step', 'message': 'Refreshing partition table...'})}\n\n"
        host_run(f"partprobe {dev_path} 2>/dev/null")
        host_run("sleep 2")

        # Auto-unmount any auto-mounted partitions (devmon/udisks)
        for idx in range(len(partitions)):
            pnum = idx + 1
            if 'nvme' in disk_name:
                pdev = f"{dev_path}p{pnum}"
            else:
                pdev = f"{dev_path}{pnum}"
            host_run(f"umount {pdev} 2>/dev/null")
        host_run("sleep 1")

        # Step 5: Format each partition
        created = []
        for idx, (_, _, p) in enumerate(part_specs):
            pnum = idx + 1
            if 'nvme' in disk_name:
                pdev = f"{dev_path}p{pnum}"
                pname = f"{disk_name}p{pnum}"
            else:
                pdev = f"{dev_path}{pnum}"
                pname = f"{disk_name}{pnum}"

            fstype = p.get("fstype", "ext4")
            label = p.get("label", "").strip()
            mkfs_base = MKFS_CMDS[fstype]
            lbl = _label_flag(fstype, label)

            yield f"data: {json.dumps({'type': 'step', 'message': f'Formatting {pname} as {fstype}...'})}\n\n"

            fmt_ok = False
            for line in host_run_stream(f"{mkfs_base} {lbl} {pdev} 2>&1"):
                line = line.rstrip("\n")
                if line.startswith("__EXIT_CODE__:"):
                    code = int(line.split(":")[1])
                    if code == 0:
                        fmt_ok = True
                        yield f"data: {json.dumps({'type': 'step', 'message': f'{pname} formatted ✓'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'step', 'message': f'Format error for {pname} (code: {code})'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'success': False})}\n\n"
                        return
                elif line.strip():
                    yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

            created.append(pname)

        parts_str = ", ".join(created)
        yield f"data: {json.dumps({'type': 'step', 'message': f'Partitioning complete! Created: {parts_str}'})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'success': True, 'partitions': created})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Filesystem Dependencies
# ---------------------------------------------------------------------------

FS_DEPS = {
    "ntfs": {"label": "NTFS (ntfs-3g)", "check": "command -v ntfs-3g", "packages": "ntfs-3g"},
    "ntfs3": {"label": "NTFS3 (kernel)", "check": "modprobe -n ntfs3 2>/dev/null", "packages": "linux-modules-extra-$(uname -r)"},
    "exfat": {"label": "exFAT", "check": "command -v mount.exfat || command -v mount.exfat-fuse", "packages": "exfat-fuse exfatprogs"},
    "btrfs": {"label": "Btrfs", "check": "command -v btrfs", "packages": "btrfs-progs"},
    "xfs": {"label": "XFS", "check": "command -v xfs_repair", "packages": "xfsprogs"},
    "hfsplus": {"label": "HFS+", "check": "command -v fsck.hfsplus", "packages": "hfsplus hfsutils hfsprogs"},
    "f2fs": {"label": "F2FS", "check": "command -v fsck.f2fs", "packages": "f2fs-tools"},
}


@storage_bp.route('/deps')
def check_deps():
    results = []
    for fs, info in FS_DEPS.items():
        r = host_run(info["check"])
        results.append({
            "fs": fs,
            "label": info["label"],
            "installed": r.returncode == 0,
            "packages": info["packages"],
        })
    return jsonify(results)


@storage_bp.route('/deps/install', methods=['POST'])
def install_deps():
    data = request.json or {}
    packages_str = data.get("packages", "")
    if not packages_str:
        return jsonify({"error": "packages is required"}), 400

    # Validate package names to prevent command injection
    # Only allow alphanumeric, hyphens, dots, colons, plus signs (valid dpkg chars)
    packages = packages_str.split()
    for pkg in packages:
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.+\-:~]+$', pkg):
            return jsonify({"error": f"Invalid package name: {pkg}"}), 400
    safe_packages = ' '.join(packages)

    def generate():
        install_script = f"""
export DEBIAN_FRONTEND=noninteractive
echo '::STEP::Repairing package manager...'
dpkg --configure -a 2>/dev/null || true
echo '::STEP::Updating package lists...'
apt-get update -y 2>&1
echo '::STEP::Installing {safe_packages}...'
apt-get install -y {safe_packages} 2>&1
echo '::STEP::Installation complete!'
echo '::DONE::'
"""
        for line in host_run_stream(install_script):
            line = line.rstrip("\n")
            if line.startswith("__EXIT_CODE__:"):
                code = line.split(":")[1]
                yield f"data: {json.dumps({'type': 'exit', 'code': int(code)})}\n\n"
            elif line.startswith("::STEP::"):
                yield f"data: {json.dumps({'type': 'step', 'message': line[8:]})}\n\n"
            elif line.startswith("::DONE::"):
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'log', 'message': line})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# API – Disk Usage Analytics
# ---------------------------------------------------------------------------

def _fmt_bytes(n):
    """Human-readable bytes."""
    return fmt_bytes(n)


@storage_bp.route('/analyze')
def analyze_path():
    """Analyze disk usage of a directory. Returns top-level children with sizes.
    Query params: path (required), limit (default 50)
    Uses du -k (1K blocks) which is much faster than du -b (apparent size).
    """
    path = request.args.get('path', '/')
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
    except (ValueError, TypeError):
        limit = 50

    # Validate path is absolute
    if not path.startswith('/'):
        return jsonify({"error": "Path must be absolute"}), 400

    # du -k --max-depth=1: list direct children sizes in KB
    # Use -x (stay on same fs) for root-level paths that might have mounts under them
    # Don't use -x for specific mount points (user wants to scan that drive)
    use_x = path in ('/', '/home', '/var', '/usr', '/opt', '/tmp', '/root')
    x_flag = 'x' if use_x else ''
    cmd = f"du -k{x_flag} --max-depth=1 {Q(path)} 2>/dev/null | sort -rn | head -n {limit + 1}"
    r = host_run(cmd, timeout=90)
    if r.returncode != 0 and not r.stdout.strip():
        return jsonify({"error": "Cannot analyze path", "detail": r.stderr.strip()}), 400

    entries = []
    total_size = 0
    norm_path = path.rstrip('/')
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t', 1)
        if len(parts) != 2:
            continue
        try:
            size_kb = int(parts[0])
        except ValueError:
            continue
        entry_path = parts[1].strip()
        size_bytes = size_kb * 1024

        ep_norm = entry_path.rstrip('/')
        if ep_norm == norm_path or ep_norm == '':
            total_size = size_bytes
            continue

        name = ep_norm[len(norm_path):].lstrip('/')
        if not name or '/' in name:
            continue

        entries.append({
            'path': entry_path,
            'name': name,
            'size': size_bytes,
            'size_human': _fmt_bytes(size_bytes),
        })

    # Compute percentages
    for e in entries:
        e['percent'] = round((e['size'] / total_size * 100) if total_size > 0 else 0, 1)

    return jsonify({
        'path': path,
        'total_size': total_size,
        'total_size_human': _fmt_bytes(total_size),
        'entries': entries[:limit],
    })


@storage_bp.route('/analyze/files')
def analyze_files():
    """Find the largest files in a directory.
    Query params: path (required), limit (default 50)
    """
    path = request.args.get('path', '/')
    try:
        limit = min(int(request.args.get('limit', 50)), 200)
    except (ValueError, TypeError):
        limit = 50

    if not path.startswith('/'):
        return jsonify({"error": "Path must be absolute"}), 400

    cmd = (
        f"find {Q(path)} -xdev -maxdepth 8 -type f -printf '%s\\t%T@\\t%p\\n' 2>/dev/null | "
        f"sort -rn | head -n {limit}"
    )
    r = host_run(cmd, timeout=90)
    if r.returncode != 0 and not r.stdout.strip():
        return jsonify({"error": "Cannot scan files", "detail": r.stderr.strip()}), 400

    files = []
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('\t', 2)
        if len(parts) < 3:
            continue
        try:
            size = int(parts[0])
            mtime = float(parts[1])
        except ValueError:
            continue
        fpath = parts[2].strip()
        name = os.path.basename(fpath)
        ext = os.path.splitext(name)[1].lower()

        files.append({
            'path': fpath,
            'name': name,
            'ext': ext,
            'size': size,
            'size_human': _fmt_bytes(size),
            'modified': mtime,
        })

    return jsonify({
        'path': path,
        'files': files,
    })


# ---------------------------------------------------------------------------
# Storage Pool Wizard — endpoints
# ---------------------------------------------------------------------------

_POOLS_FILE = data_path('storage_pools.json')


def _load_pools():
    try:
        with open(_POOLS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_pools(pools):
    os.makedirs(os.path.dirname(_POOLS_FILE), exist_ok=True)
    with open(_POOLS_FILE, 'w') as f:
        json.dump(pools, f, indent=2)


@storage_bp.route('/pool/available-disks')
def pool_available_disks():
    """List disks/partitions available for pool creation.
    Excludes: system disks, mounted, RAID members, LVM PVs."""

    r = host_run(
        "lsblk -J -b -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL,MODEL,TRAN,UUID,HOTPLUG,PKNAME"
    )
    if r.returncode != 0:
        return jsonify({"error": r.stderr.strip()}), 500
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return jsonify({"error": "Failed to parse lsblk"}), 500

    # Collect RAID members
    raid_members = set()
    md_r = host_run("cat /proc/mdstat 2>/dev/null")
    if md_r.returncode == 0:
        for line in md_r.stdout.splitlines():
            for m in re.findall(r'(\w+)\[\d+\]', line):
                raid_members.add(m)

    # Collect LVM PVs
    lvm_pvs = set()
    pv_r = host_run("pvs --noheadings -o pv_name 2>/dev/null")
    if pv_r.returncode == 0:
        for line in pv_r.stdout.strip().splitlines():
            dev = line.strip().replace('/dev/', '')
            if dev:
                lvm_pvs.add(dev)

    available = []

    def _check(dev, parent=None):
        name = dev.get('name', '')
        dtype = dev.get('type', '')
        children = dev.get('children', [])

        if dtype in ('loop', 'rom') or name.startswith('loop') or name.startswith('nbd') or name.startswith('zram'):
            return

        # For whole disks with partitions — skip the disk itself,
        # only offer whole disks without partitions
        if dtype == 'disk' and children:
            for c in children:
                _check(c, parent=dev)
            return

        if dtype == 'disk' and not children:
            # Whole disk without partitions — candidate
            pass
        elif dtype == 'part':
            pass
        else:
            return

        # Skip system disks
        if _is_system_disk(name):
            return
        base = re.sub(r'p?\d+$', '', name)
        if base != name and _is_system_disk(base):
            return

        # Skip mounted
        if dev.get('mountpoint'):
            return

        # Skip RAID members
        if name in raid_members:
            return

        # Skip LVM PVs
        if name in lvm_pvs:
            return

        p = parent or dev
        tran = p.get('tran') or dev.get('tran') or ''
        model = (p.get('model') or dev.get('model') or '').strip()
        size_bytes = 0
        try:
            size_bytes = int(dev.get('size', 0))
        except (ValueError, TypeError):
            pass

        # Skip zero-size devices
        if size_bytes <= 0:
            return

        available.append({
            'name': name,
            'device': f'/dev/{name}',
            'size_bytes': size_bytes,
            'size': _fmt_bytes(size_bytes) if size_bytes else dev.get('size', '?'),
            'type': dtype,
            'model': model or None,
            'tran': tran,
            'removable': bool(p.get('hotplug') or dev.get('hotplug')),
            'fstype': dev.get('fstype'),
            'label': dev.get('label'),
        })

    for dev in data.get('blockdevices', []):
        _check(dev)

    return jsonify({'disks': available})


@storage_bp.route('/pool/create', methods=['POST'])
@admin_required
def pool_create():
    """Create a storage pool: [RAID] + format + mount + fstab + [Samba share].
    Streams progress via SSE."""
    data = request.get_json(force=True)

    disks = data.get('disks', [])
    raid_level = data.get('raid_level')  # None for single disk
    fstype = data.get('fstype', 'ext4')
    pool_name = data.get('name', '').strip()
    mount_path = data.get('mount_path', '').strip()
    samba = data.get('samba', False)
    samba_name = data.get('samba_name', '').strip()

    # --- Validation ---
    if not disks:
        return jsonify({"error": "No disks selected"}), 400
    if not pool_name or not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9 _\-]{0,63}$', pool_name):
        return jsonify({"error": "Invalid pool name"}), 400
    if fstype not in ('ext4', 'btrfs'):
        return jsonify({"error": "Unsupported filesystem (ext4 or btrfs)"}), 400
    if not mount_path or not mount_path.startswith('/') or '..' in mount_path:
        return jsonify({"error": "Invalid mount path"}), 400
    mount_path = _sanitize_mount_path(mount_path)

    for d in disks:
        if not re.match(r'^[a-zA-Z0-9]+$', d):
            return jsonify({"error": f"Invalid disk name: {d}"}), 400

    if len(disks) > 1 and not raid_level:
        return jsonify({"error": "RAID level required for multiple disks"}), 400
    if raid_level and raid_level not in ('0', '1', '5', '6', '10'):
        return jsonify({"error": f"Invalid RAID level: {raid_level}"}), 400

    min_devs = {'0': 2, '1': 2, '5': 3, '6': 4, '10': 4}
    if raid_level and len(disks) < min_devs.get(raid_level, 2):
        return jsonify({"error": f"RAID {raid_level} requires at least {min_devs[raid_level]} disks"}), 400

    # Block USB/removable disks in RAID arrays
    if raid_level:
        r_chk = host_run(
            "lsblk -J -o NAME,TRAN,HOTPLUG "
            + " ".join(f"/dev/{Q(d)}" for d in disks)
        )
        if r_chk.returncode == 0:
            try:
                chk_data = json.loads(r_chk.stdout)
                for bdev in chk_data.get("blockdevices", []):
                    if bdev.get("tran") == "usb" or bdev.get("hotplug"):
                        return jsonify({
                            "error": f"USB/removable disk /dev/{bdev['name']} cannot be used in RAID. "
                                     "Create a single-disk pool instead."
                        }), 400
            except (json.JSONDecodeError, KeyError):
                pass

    def _sse(msg_type, message, **extra):
        payload = {'type': msg_type, 'message': message}
        payload.update(extra)
        return f"data: {json.dumps(payload)}\n\n"

    def generate():
        target_dev = None

        # ── Step 1: RAID creation (if multi-disk) ──
        if len(disks) > 1 and raid_level:
            yield _sse('step', f'Creating RAID {raid_level} with {len(disks)} disks...')

            # Wipe existing signatures
            for d in disks:
                host_run(f"wipefs -a /dev/{Q(d)} 2>/dev/null", timeout=10)
                host_run(f"mdadm --zero-superblock /dev/{Q(d)} 2>/dev/null", timeout=10)

            # Find free md name
            existing = set()
            md_r = host_run("cat /proc/mdstat 2>/dev/null")
            if md_r.returncode == 0:
                for line in md_r.stdout.splitlines():
                    m = re.match(r'^(md\d+)', line)
                    if m:
                        existing.add(m.group(1))
            md_name = None
            for i in range(128):
                cand = f'md{i}'
                if cand not in existing:
                    md_name = cand
                    break
            if not md_name:
                yield _sse('error', 'No free md device numbers')
                yield _sse('done', 'Failed', success=False)
                return

            dev_args = ' '.join(f'/dev/{Q(d)}' for d in disks)
            cmd = (
                f'yes | mdadm --create /dev/{md_name} '
                f'--level={Q(raid_level)} '
                f'--raid-devices={len(disks)} '
                f'--run --force {dev_args} 2>&1'
            )
            r = host_run(cmd, timeout=120)
            if r.returncode != 0:
                yield _sse('error', f'RAID creation failed: {r.stderr.strip() or r.stdout.strip()}')
                yield _sse('done', 'Failed', success=False)
                return

            # Save mdadm config
            host_run('mdadm --detail --scan >> /etc/mdadm/mdadm.conf 2>/dev/null || true')
            host_run('update-initramfs -u 2>/dev/null || true', timeout=120)

            target_dev = f'/dev/{md_name}'
            yield _sse('step', f'RAID {raid_level} array created: {target_dev}', device=target_dev)

            # Wait for RAID to become available
            time.sleep(2)
        else:
            # Single disk
            d = disks[0]
            target_dev = f'/dev/{d}'
            yield _sse('step', f'Using disk: {target_dev}')

            # Wipe signatures on single disk too
            host_run(f"wipefs -a {Q(target_dev)} 2>/dev/null", timeout=10)

        # ── Step 2: Format ──
        yield _sse('step', f'Formatting {target_dev} as {fstype}...')

        label_safe = re.sub(r'[^a-zA-Z0-9_\-.]', '_', pool_name)[:16]
        if fstype == 'ext4':
            mkfs_cmd = f"mkfs.ext4 -F -L {Q(label_safe)} {Q(target_dev)} 2>&1"
        else:
            mkfs_cmd = f"mkfs.btrfs -f -L {Q(label_safe)} {Q(target_dev)} 2>&1"

        for line in host_run_stream(mkfs_cmd):
            line = line.rstrip('\n')
            if line.startswith('__EXIT_CODE__:'):
                code = int(line.split(':')[1])
                if code != 0:
                    yield _sse('error', f'Format failed (exit code {code})')
                    yield _sse('done', 'Failed', success=False)
                    return
            else:
                if line.strip():
                    yield _sse('log', line)

        # ext4 tuning
        if fstype == 'ext4':
            host_run(f'tune2fs -c 30 -i 90d {Q(target_dev)} 2>/dev/null', timeout=10)

        yield _sse('step', 'Format completed')

        # ── Step 3: Mount ──
        yield _sse('step', f'Mounting to {mount_path}...')

        host_run(f"mkdir -p {Q(mount_path)}")
        if fstype == 'ext4':
            mount_opts = 'defaults,nofail,noatime,commit=60'
        else:
            mount_opts = 'defaults,nofail,noatime'

        r = host_run(f"mount -t {fstype} -o {mount_opts} {Q(target_dev)} {Q(mount_path)}")
        if r.returncode != 0:
            yield _sse('error', f'Mount failed: {r.stderr.strip()}')
            yield _sse('done', 'Failed', success=False)
            return

        # Set permissions
        host_run(f"chmod 0777 {Q(mount_path)}")
        uid_r = host_run("id -u")
        gid_r = host_run("id -g")
        uid = uid_r.stdout.strip() or '1000'
        gid = gid_r.stdout.strip() or '1000'
        host_run(f"chown {uid}:{gid} {Q(mount_path)}")

        yield _sse('step', f'Mounted at {mount_path}')

        # ── Step 4: fstab ──
        yield _sse('step', 'Adding to fstab for auto-mount on boot...')

        uuid_r = host_run(f"blkid -s UUID -o value {Q(target_dev)}")
        uuid = uuid_r.stdout.strip()
        if uuid:
            _fstab_add(uuid, mount_path, fstype, mount_opts)
            yield _sse('step', 'Added to fstab')
        else:
            yield _sse('log', 'Warning: UUID not found, skipping fstab')

        # ── Step 5: Samba share ──
        if samba and samba_name:
            yield _sse('step', f'Creating Samba share "{samba_name}"...')

            smbd_check = host_run("command -v smbd")
            if smbd_check.returncode != 0:
                yield _sse('log', 'Samba not installed — skipping share creation')
            else:
                safe_samba = re.sub(r'[^a-zA-Z0-9 _\-]', '_', samba_name)[:64]
                uid_r2 = host_run("id -un")
                user = uid_r2.stdout.strip() or "nobody"
                gid_r2 = host_run("id -gn")
                group = gid_r2.stdout.strip() or "nogroup"
                if user == "root":
                    user, group = _detect_primary_user()

                subnet = _detect_lan_subnet()
                ifaces = _detect_lan_interfaces()
                share_conf = {
                    "name": safe_samba,
                    "path": mount_path,
                    "guest_ok": "no",
                    "writable": "yes",
                    "user": user,
                    "group": group,
                    "subnet": subnet,
                    "ifaces": ifaces,
                }
                _host_write_json('/tmp/_samba_params.json', share_conf)

                script = """import json, re
NL = chr(10)
params = json.loads(open('/tmp/_samba_params.json').read())
name = params['name']
path = params['path']
guest = params['guest_ok']
user = params['user']
group = params['group']
subnet = params['subnet']
ifaces = params.get('ifaces', 'eth0')
try:
    conf = open('/etc/samba/smb.conf').read()
except FileNotFoundError:
    conf = ''

GLOBAL_DEFAULTS = {
    'workgroup': 'WORKGROUP',
    'server string': 'EthOS NAS',
    'security': 'user',
    'map to guest': 'Bad User',
    'guest account': 'nobody',
    'server min protocol': 'SMB3',
    'server signing': 'mandatory',
    'smb encrypt': 'if_required',
    'dns proxy': 'no',
    'interfaces': f'127.0.0.0/8 {ifaces}',
    'bind interfaces only': 'yes',
    'hosts allow': f'127.0.0.1 {subnet}',
    'hosts deny': '0.0.0.0/0',
}
if '[global]' not in conf:
    header = '[global]' + NL
    for k, v in GLOBAL_DEFAULTS.items():
        header += f'    {k} = {v}' + NL
    conf = header + NL + conf
else:
    gstart = conf.index('[global]')
    next_bracket = conf.find(NL + '[', gstart + 8)
    gend = next_bracket + 1 if next_bracket != -1 else len(conf)
    global_block = conf[gstart:gend]
    additions = ''
    for k, v in GLOBAL_DEFAULTS.items():
        if k not in global_block.lower():
            additions += f'    {k} = {v}' + NL
    if additions:
        insert_pos = conf.index(NL, gstart) + 1
        conf = conf[:insert_pos] + additions + conf[insert_pos:]

conf = re.sub(r'(?im)^(\\s*map to guest\\s*=\\s*).*$', r'\\1Bad User', conf)
conf = re.sub(r'(?im)^(\\s*interfaces\\s*=\\s*).*$', f'\\\\1127.0.0.0/8 {ifaces}', conf)
conf = re.sub(r'(?im)^(\\s*smb encrypt\\s*=\\s*).*$', r'\\1if_required', conf)

pattern = r'\\[' + re.escape(name) + r'\\][^\\[]*'
conf = re.sub(pattern, '', conf, flags=re.IGNORECASE)
conf = conf.rstrip() + NL + NL
block = f'[{name}]' + NL
block += f'    path = {path}' + NL
block += '    browseable = yes' + NL
block += '    writable = yes' + NL
block += '    read only = no' + NL
block += f'    guest ok = {guest}' + NL
block += f'    force user = {user}' + NL
block += f'    force group = {group}' + NL
block += '    create mask = 0664' + NL
block += '    directory mask = 0775' + NL
open('/etc/samba/smb.conf', 'w').write(conf + block)
import os
os.makedirs(path, mode=0o775, exist_ok=True)
"""
                _host_write_script('/tmp/_samba_edit.py', script)
                r = host_run("python3 /tmp/_samba_edit.py")
                host_run("rm -f /tmp/_samba_edit.py /tmp/_samba_params.json 2>/dev/null")

                if r.returncode != 0:
                    yield _sse('log', f'Samba share creation failed: {r.stderr.strip()}')
                else:
                    host_run("systemctl restart smbd nmbd 2>/dev/null || systemctl restart smb nmb 2>/dev/null || true")
                    yield _sse('step', f'Samba share "{safe_samba}" created')

        # ── Save pool config ──
        pools = _load_pools()
        pool_entry = {
            'name': pool_name,
            'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'disks': disks,
            'raid_level': raid_level,
            'raid_device': target_dev if (len(disks) > 1 and raid_level) else None,
            'fstype': fstype,
            'mount_path': mount_path,
            'samba_share': samba_name if samba else None,
            'device': target_dev,
        }
        pools.append(pool_entry)
        _save_pools(pools)

        yield _sse('step', 'Pool configuration saved')
        yield _sse('done', f'Pool "{pool_name}" is ready!', success=True, pool=pool_entry)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@storage_bp.route('/pool/list')
def pool_list():
    """List saved storage pools with live usage, RAID health, and shares.

    Always includes the system data volume (/mnt/data) as the first pool,
    matching Synology's model where Volume 1 is always visible.
    """
    pools = _load_pools()

    # Gather live df data for all mountpoints
    df_map = {}
    dfr = host_run("df -B1 --output=target,size,used,avail,pcent 2>/dev/null")
    if dfr.returncode == 0:
        for line in dfr.stdout.strip().split('\n')[1:]:
            parts = line.split()
            if len(parts) >= 5:
                try:
                    df_map[parts[0]] = {
                        'total': int(parts[1]),
                        'used': int(parts[2]),
                        'free': int(parts[3]),
                        'percent': float(parts[4].replace('%', '')),
                    }
                except (ValueError, IndexError):
                    pass

    # ── System Volume (like Synology's Volume 1) ──
    # Detect where EthOS data lives: /mnt/data (production) or /opt/ethos/data fs
    sys_pool = None
    sys_mount = None
    if '/mnt/data' in df_map:
        sys_mount = '/mnt/data'
    else:
        # Dev / legacy installs: find the filesystem holding /opt/ethos/data
        data_dir = data_path()
        if os.path.isdir(data_dir):
            fmr = host_run(f"findmnt -n -o TARGET --target {Q(data_dir)} 2>/dev/null", timeout=5)
            if fmr.returncode == 0 and fmr.stdout.strip():
                sys_mount = fmr.stdout.strip()

    if sys_mount and sys_mount in df_map:
        sys_disks = []
        sys_fstype = 'ext4'
        sys_device = None
        src_r = host_run(f"findmnt -n -o SOURCE,FSTYPE {Q(sys_mount)} 2>/dev/null", timeout=5)
        if src_r.returncode == 0 and src_r.stdout.strip():
            parts = src_r.stdout.strip().split()
            src_dev = parts[0].split('[')[0]  # strip btrfs subvol suffix
            sys_device = src_dev
            if len(parts) > 1:
                sys_fstype = parts[1]
            pk_r = host_run(f"lsblk -no PKNAME {Q(src_dev)} 2>/dev/null", timeout=5)
            if pk_r.returncode == 0 and pk_r.stdout.strip():
                sys_disks = [pk_r.stdout.strip()]
            else:
                sys_disks = [src_dev.replace('/dev/', '')]
        sys_pool = {
            'name': 'Volume 1',
            'system': True,
            'created': None,
            'disks': sys_disks,
            'raid_level': None,
            'raid_device': None,
            'fstype': sys_fstype,
            'mount_path': sys_mount,
            'samba_share': None,
            'device': sys_device,
            'usage': df_map.get(sys_mount),
            'mounted': True,
            'raid_status': None,
            'shares': [],
        }

    # Gather RAID status for md devices
    raid_status = {}
    md_r = host_run("cat /proc/mdstat 2>/dev/null")
    if md_r.returncode == 0:
        for line in md_r.stdout.splitlines():
            m = re.match(r'^(md\d+)\s*:\s*active\s+(\S+)\s+(.+)', line)
            if m:
                raid_status[f'/dev/{m.group(1)}'] = {
                    'state': 'active',
                    'level': m.group(2),
                    'members_line': m.group(3),
                }

    # Gather Samba shares
    samba_shares = {}
    try:
        smb_conf = host_run("cat /etc/samba/smb.conf 2>/dev/null").stdout or ""
        current_share = None
        for line in smb_conf.splitlines():
            line_s = line.strip()
            m = re.match(r'^\[(.+)\]$', line_s)
            if m:
                name = m.group(1)
                if name.lower() != 'global':
                    current_share = name
                    samba_shares[name] = {}
                else:
                    current_share = None
            elif current_share and '=' in line_s:
                k, v = line_s.split('=', 1)
                samba_shares[current_share][k.strip().lower()] = v.strip()
    except Exception:
        pass

    # Enrich system pool with shares
    if sys_pool:
        smp = sys_pool['mount_path']
        for sname, sdata in samba_shares.items():
            sp = sdata.get('path', '')
            if sp.startswith(smp) or sp.startswith('/home'):
                sys_pool['shares'].append({
                    'name': sname,
                    'protocol': 'samba',
                    'path': sp,
                })

    # Enrich each user-created pool with live data
    for pool in pools:
        mp = pool.get('mount_path', '')
        pool['usage'] = df_map.get(mp)
        pool['mounted'] = mp in df_map

        # RAID health
        rd = pool.get('raid_device')
        if rd and rd in raid_status:
            pool['raid_status'] = raid_status[rd]['state']
        elif rd:
            pool['raid_status'] = 'inactive'
        else:
            pool['raid_status'] = None

        # Associated shares
        share_name = pool.get('samba_share')
        pool['shares'] = []
        if share_name and share_name in samba_shares:
            pool['shares'].append({
                'name': share_name,
                'protocol': 'samba',
                'path': samba_shares[share_name].get('path', mp),
            })
        # Also find any other Samba shares pointing to this mount
        for sname, sdata in samba_shares.items():
            if sdata.get('path', '').startswith(mp) and sname != share_name:
                pool['shares'].append({
                    'name': sname,
                    'protocol': 'samba',
                    'path': sdata.get('path', ''),
                })

    # System volume first, then user pools (like Synology)
    all_pools = []
    if sys_pool:
        all_pools.append(sys_pool)
    all_pools.extend(pools)

    return jsonify({'pools': all_pools})


@storage_bp.route('/pool/<pool_name>/delete', methods=['POST'])
@admin_required
def pool_delete(pool_name):
    """Delete a storage pool: unmount, remove fstab, optionally destroy RAID."""
    data = request.get_json(force=True) if request.data else {}
    wipe = data.get('wipe', False)

    pools = _load_pools()
    pool = None
    for p in pools:
        if p.get('name') == pool_name:
            pool = p
            break
    if not pool:
        return jsonify({"error": f"Pool '{pool_name}' not found"}), 404

    mp = pool.get('mount_path', '')
    device = pool.get('device', '')
    raid_dev = pool.get('raid_device')
    share_name = pool.get('samba_share')
    steps = []

    # Remove Samba share
    if share_name:
        try:
            smb_conf = host_run("cat /etc/samba/smb.conf 2>/dev/null").stdout or ""
            pattern = r'\[' + re.escape(share_name) + r'\][^\[]*'
            new_conf = re.sub(pattern, '', smb_conf, flags=re.IGNORECASE).strip() + '\n'
            _host_write_file('/tmp/_smb_del.conf', new_conf)
            host_run("cp /tmp/_smb_del.conf /etc/samba/smb.conf && rm /tmp/_smb_del.conf")
            host_run("systemctl restart smbd nmbd 2>/dev/null || true")
            steps.append(f"Removed Samba share '{share_name}'")
        except Exception as e:
            steps.append(f"Warning: Samba share removal failed: {e}")

    # Unmount
    if mp:
        host_run(f"umount -l {Q(mp)} 2>/dev/null", timeout=10)
        steps.append(f"Unmounted {mp}")

    # Remove fstab entry
    if mp:
        _fstab_remove(mp)
        # Also try UUID-based removal
        uuid_r = host_run(f"blkid -s UUID -o value {Q(device)} 2>/dev/null")
        uuid = uuid_r.stdout.strip()
        if uuid:
            host_run(f"sed -i '/UUID={uuid}/d' /etc/fstab 2>/dev/null || true")
        steps.append("Removed fstab entry")

    # Stop RAID array
    if raid_dev and wipe:
        host_run(f"mdadm --stop {Q(raid_dev)} 2>/dev/null", timeout=30)
        # Zero superblocks on member disks
        for disk in pool.get('disks', []):
            host_run(f"mdadm --zero-superblock /dev/{Q(disk)} 2>/dev/null", timeout=10)
        steps.append(f"Stopped RAID array {raid_dev}")

    # Wipe filesystem signatures
    if wipe and device:
        host_run(f"wipefs -a {Q(device)} 2>/dev/null", timeout=10)
        steps.append(f"Wiped filesystem on {device}")

    # Remove mountpoint directory
    if mp:
        host_run(f"rmdir {Q(mp)} 2>/dev/null", timeout=5)

    # Remove from saved pools
    pools = [p for p in pools if p.get('name') != pool_name]
    _save_pools(pools)
    steps.append("Pool removed from configuration")

    _invalidate_drives_cache()
    return jsonify({"ok": True, "steps": steps})


