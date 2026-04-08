"""
EthOS — Builder Resource Manager

Manages CPU/IO quotas during image builds so that:
  • Running VMs are throttled to 25% CPU to avoid starving the build
  • tmpfs size calculation accounts for RAM already consumed by VMs

Uses systemd cgroup v2 via `systemctl set-property`.
Falls back gracefully if cgroups or systemd are unavailable.
"""

import os
import logging

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, q

logger = logging.getLogger('builder')

# QEMU processes land in machine.slice when started with -name flag
_VM_SLICE = 'machine.slice'

# VM limits during build vs normal operation
_VM_CPU_QUOTA_BUILD  = '25%'
_VM_IO_WEIGHT_BUILD  = '25'

# OS reserve: keep this much RAM free for kernel + other processes
_OS_RESERVE_MB = 512


def _read_meminfo(key: str) -> int:
    """Read a /proc/meminfo value, return MB (0 on error)."""
    try:
        for line in open('/proc/meminfo'):
            if line.startswith(key + ':'):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def get_vm_memory_usage_mb() -> int:
    """
    Return total RAM (MB) currently used by all QEMU processes.

    Tries cgroup memory.current first (accurate), then falls back to
    summing RSS from /proc for all qemu-* processes.
    """
    for cg_path in [
        f'/sys/fs/cgroup/{_VM_SLICE}/memory.current',
        '/sys/fs/cgroup/machine.slice/memory.current',
    ]:
        try:
            val = int(open(cg_path).read().strip())
            return val // (1024 * 1024)
        except (FileNotFoundError, ValueError):
            pass

    r = _host_run(
        "ps -eo rss,comm --no-headers 2>/dev/null | "
        "awk '/qemu/{s+=$1} END{print int(s/1024)}'",
        timeout=5,
    )
    try:
        return max(0, int(r.stdout.strip() or '0'))
    except ValueError:
        return 0


def calculate_build_tmpfs_mb(img_size_gb: int) -> tuple:
    """
    Decide whether to use tmpfs for the build work directory.

    Returns (use_tmpfs: bool, size_mb: int).

    Effective available = MemAvailable - vm_ram_in_use - OS_reserve.
    tmpfs is only used if effective_avail >= space_needed_for_image.
    """
    mem_avail_mb = _read_meminfo('MemAvailable')
    vm_usage_mb  = get_vm_memory_usage_mb()
    needed_mb    = img_size_gb * 1024

    effective_avail = mem_avail_mb - vm_usage_mb - _OS_RESERVE_MB

    logger.debug(
        'tmpfs check: avail=%dMB vms=%dMB reserve=%dMB effective=%dMB needed=%dMB',
        mem_avail_mb, vm_usage_mb, _OS_RESERVE_MB, effective_avail, needed_mb,
    )

    if effective_avail >= needed_mb:
        return True, needed_mb
    return False, 0


def _cgroups_v2_available() -> bool:
    """Return True if cgroup v2 unified hierarchy is mounted."""
    return os.path.isfile('/sys/fs/cgroup/cgroup.controllers')


def _set_slice_property(slice_name: str, *props: str) -> bool:
    """Apply runtime properties to a systemd slice. Returns True on success."""
    if not props:
        return True
    prop_args = ' '.join(f'--property={q(p)}' for p in props)
    r = _host_run(
        f'systemctl set-property {q(slice_name)} {prop_args} 2>/dev/null',
        timeout=5,
    )
    return r.returncode == 0


def enter_build_slice() -> bool:
    """
    Throttle VMs so the build has CPU/IO headroom.
    Applies CPUQuota=25% + IOWeight=25 to machine.slice.
    Returns True if throttling was successfully applied.
    """
    if not _cgroups_v2_available():
        logger.info('builder_resources: cgroup v2 unavailable — skipping VM throttle')
        return False

    ok = _set_slice_property(
        _VM_SLICE,
        f'CPUQuota={_VM_CPU_QUOTA_BUILD}',
        f'IOWeight={_VM_IO_WEIGHT_BUILD}',
    )
    if ok:
        logger.info(
            'builder_resources: VMs throttled — CPUQuota=%s IOWeight=%s',
            _VM_CPU_QUOTA_BUILD, _VM_IO_WEIGHT_BUILD,
        )
    else:
        logger.warning('builder_resources: failed to throttle machine.slice')
    return ok


def leave_build_slice():
    """
    Restore normal VM resource limits after build.
    Safe to call from finally — never raises.
    """
    try:
        if not _cgroups_v2_available():
            return
        _set_slice_property(_VM_SLICE, 'CPUQuota=', 'IOWeight=')
        logger.info('builder_resources: VM resource limits restored')
    except Exception as exc:
        logger.warning('builder_resources: restore failed: %s', exc)
