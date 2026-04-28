"""
EthOS — System Monitor
Collects CPU, RAM, GPU, disk, network, process, USB, Docker metrics.
Migrated from standalone resources app.
"""

import psutil
import platform
import os
import time
import subprocess
import re
import json
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run_imported
from utils import docker_available as _docker_available

try:
    import GPUtil
    HAS_GPU = True
except Exception:
    HAS_GPU = False

try:
    import cpuinfo
    HAS_CPUINFO = True
except Exception:
    HAS_CPUINFO = False

_prev_net_counters = {}
_prev_net_time = None
_prev_disk_counters = {}
_prev_disk_time = None
_last_alert_ts = {}

# Cache CPU name — cpuinfo.get_cpu_info() spawns a heavy subprocess
# and the CPU brand never changes at runtime
_cached_cpu_name = None


def _get_cpu_name():
    """Return CPU brand string, cached after first call."""
    global _cached_cpu_name
    if _cached_cpu_name is not None:
        return _cached_cpu_name
    name = platform.processor() or "Unknown"
    if HAS_CPUINFO:
        try:
            info = cpuinfo.get_cpu_info()
            name = info.get('brand_raw', name)
        except Exception:
            pass
    _cached_cpu_name = name
    return name


# Kick-start psutil cpu_percent tracking (first call always returns 0.0)
psutil.cpu_percent(interval=None, percpu=True)

def get_cpu_info():
    # interval=None: non-blocking, returns delta since last call (~0ms instead of 500ms)
    cpu_percent = psutil.cpu_percent(interval=None, percpu=True)
    cpu_freq = psutil.cpu_freq()
    cpu_count_logical = psutil.cpu_count(logical=True)
    cpu_count_physical = psutil.cpu_count(logical=False)

    temp = None
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            for name in ['coretemp', 'cpu_thermal', 'k10temp', 'zenpower']:
                if name in temps:
                    temp = max(t.current for t in temps[name])
                    break
            if temp is None:
                first_key = list(temps.keys())[0]
                temp = max(t.current for t in temps[first_key])
    except Exception:
        pass

    return {
        'name': _get_cpu_name(),
        'usage_percent': sum(cpu_percent) / len(cpu_percent) if cpu_percent else 0,
        'per_core': cpu_percent,
        'core_count': cpu_count_logical,
        'physical_cores': cpu_count_physical,
        'frequency_current': round(cpu_freq.current, 2) if cpu_freq else 0,
        'frequency_max': round(cpu_freq.max, 2) if cpu_freq else 0,
        'temperature': temp,
        'load_avg': list(os.getloadavg()) if hasattr(os, 'getloadavg') else []
    }


def get_ram_info():
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        'total': mem.total,
        'used': mem.used,
        'available': mem.available,
        'percent': mem.percent,
        'cached': getattr(mem, 'cached', 0),
        'buffers': getattr(mem, 'buffers', 0),
        'swap_total': swap.total,
        'swap_used': swap.used,
        'swap_free': swap.free,
        'swap_percent': swap.percent
    }


import shlex

def _host_cmd(cmd, timeout=10):
    """Run on host via host abstraction layer."""
    sanitized_cmd = shlex.quote(cmd)
    return _host_run_imported(sanitized_cmd, timeout=timeout)


def get_smart_info():
    """Get SMART health data for all drives using smartctl."""
    disks = []
    try:
        # Use -j for JSON output if available
        r = _host_cmd("sudo smartctl --scan -j", timeout=10)

        if r.returncode != 0 or not r.stdout.strip():
             return []

        scan_data = json.loads(r.stdout)
        devices = scan_data.get('devices', [])

        for dev in devices:
            name = dev.get('name')
            type_arg = dev.get('type') # e.g. 'sat' or 'nvme'

            cmd = f"sudo smartctl -a -j {name}"
            if type_arg:
                cmd += f" -d {type_arg}"

            out = _host_cmd(cmd, timeout=5)
            # Accept exit code 0-7 (smartctl bitmask), but we need stdout
            if not out.stdout:
                continue

            try:
                data = json.loads(out.stdout)

                # Extract key metrics
                smart_status = data.get('smart_status', {}).get('passed')
                health = 'PASS' if smart_status else 'FAIL'

                # Attributes
                attrs = data.get('ata_smart_attributes', {}).get('table', [])
                nvme_attrs = data.get('nvme_smart_health_information_log', {})

                temp = 0
                reallocated = 0
                pending = 0
                crc_errors = 0
                power_on_hours = 0

                # SATA/ATA
                for attr in attrs:
                    id_ = attr.get('id')
                    raw = attr.get('raw', {}).get('value', 0)

                    if id_ == 5: # Reallocated_Sector_Ct
                        reallocated = raw
                    elif id_ == 197: # Current_Pending_Sector
                        pending = raw
                    elif id_ == 199: # UDMA_CRC_Error_Count
                        crc_errors = raw
                    elif id_ == 9: # Power_On_Hours
                        power_on_hours = raw

                # Temperature (try generic then attrs)
                if 'temperature' in data:
                    temp = data['temperature'].get('current', 0)

                # NVMe specific
                if nvme_attrs:
                    temp = nvme_attrs.get('temperature', temp)
                    power_on_hours = nvme_attrs.get('power_on_hours', power_on_hours)

                # Life remaining
                remaining_life = -1 # Unknown
                # NVMe
                if nvme_attrs:
                     used = nvme_attrs.get('percentage_used', 0)
                     remaining_life = max(0, 100 - used)
                else:
                    # SATA SSDs (various attributes)
                    for attr in attrs:
                        id_ = attr.get('id')
                        # 231: SSD_Life_Left, 233: Media_Wearout_Indicator, 177: Wear_Leveling_Count
                        if id_ in [231, 233, 177]:
                             val = attr.get('value', -1)
                             if val != -1:
                                 remaining_life = val
                                 break

                disks.append({
                    'device': name,
                    'type': type_arg,
                    'model': data.get('model_name', 'Unknown'),
                    'serial': data.get('serial_number', ''),
                    'health': health,
                    'temperature': temp,
                    'reallocated_sectors': reallocated,
                    'pending_sectors': pending,
                    'udma_crc_errors': crc_errors,
                    'power_on_hours': power_on_hours,
                    'remaining_life': remaining_life,
                    'smart_status_passed': smart_status
                })

            except json.JSONDecodeError:
                pass

    except Exception as e:
        print(f"SMART check error: {e}")

    return disks


def get_gpu_info():
    gpus = []
    if HAS_GPU:
        try:
            gpu_list = GPUtil.getGPUs()
            for gpu in gpu_list:
                gpus.append({
                    'id': gpu.id,
                    'name': gpu.name,
                    'load': round(gpu.load * 100, 1),
                    'memory_used': round(gpu.memoryUsed, 1),
                    'memory_total': round(gpu.memoryTotal, 1),
                    'memory_percent': round((gpu.memoryUsed / gpu.memoryTotal) * 100, 1) if gpu.memoryTotal > 0 else 0,
                    'temperature': gpu.temperature,
                    'driver': gpu.driver,
                    'uuid': gpu.uuid
                })
        except Exception:
            pass
    return gpus


def _detect_distro():
    """Detect if running Debian or Ubuntu (affects package names)."""
    try:
        r = _host_run_imported("cat /etc/os-release 2>/dev/null", timeout=5)
        txt = (r.stdout or '').lower()
        if 'ubuntu' in txt:
            return 'ubuntu'
    except Exception:
        pass
    return 'debian'


def _nvidia_packages():
    """Return correct NVIDIA driver packages for this distro."""
    distro = _detect_distro()
    if distro == 'ubuntu':
        return ['nvidia-driver-550', 'nvidia-utils-550']
    # Debian uses unversioned metapackage
    return ['nvidia-driver', 'nvidia-smi']


def _check_vendor_driver(vendor):
    """Check if GPU drivers for a given vendor are actually working on the host."""
    try:
        if vendor == 'nvidia':
            r = _host_run_imported("nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null", timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return True
            # Fallback: check if the nvidia kernel module is loaded
            r2 = _host_run_imported("lsmod 2>/dev/null | grep -q '^nvidia '", timeout=5)
            return r2.returncode == 0
        elif vendor == 'amd':
            # Check for amdgpu kernel module
            r = _host_run_imported("lsmod 2>/dev/null | grep -q '^amdgpu '", timeout=5)
            if r.returncode == 0:
                return True
            # Also check if the mesa packages are installed
            r2 = _host_run_imported("dpkg -s mesa-vulkan-drivers 2>/dev/null | grep -q '^Status:.*installed'", timeout=5)
            return r2.returncode == 0
        elif vendor == 'intel':
            # Check for i915 kernel module
            r = _host_run_imported("lsmod 2>/dev/null | grep -q '^i915 '", timeout=5)
            if r.returncode == 0:
                return True
            r2 = _host_run_imported("dpkg -s intel-media-va-driver 2>/dev/null | grep -q '^Status:.*installed'", timeout=5)
            return r2.returncode == 0
    except Exception:
        pass
    return False


def _check_vendor_packages(vendor):
    """Check if vendor driver packages are installed (even if module not loaded yet)."""
    try:
        if vendor == 'nvidia':
            r = _host_run_imported("dpkg -s nvidia-driver 2>/dev/null | grep -q '^Status:.*installed'", timeout=5)
            return r.returncode == 0
        elif vendor == 'amd':
            r = _host_run_imported("dpkg -s mesa-vulkan-drivers 2>/dev/null | grep -q '^Status:.*installed'", timeout=5)
            return r.returncode == 0
        elif vendor == 'intel':
            r = _host_run_imported("dpkg -s intel-media-va-driver 2>/dev/null | grep -q '^Status:.*installed'", timeout=5)
            return r.returncode == 0
    except Exception:
        pass
    return False


def detect_gpu_hardware():
    """Detect GPU hardware via lspci (works even without drivers installed).
    Returns dict with vendor, model, driver_installed, recommended_packages."""
    result = {'cards': [], 'driver_installed': False}
    try:
        r = _host_run_imported("lspci -nn 2>/dev/null", timeout=10)
        if r.returncode != 0:
            return result
        for line in r.stdout.splitlines():
            low = line.lower()
            if not any(k in low for k in ('vga', '3d controller', 'display controller')):
                continue
            # Parse name: after "]: " and strip trailing PCI ID "[xxxx:xxxx]"
            name_part = line.split(']: ')[-1] if ']: ' in line else line.split(':')[-1]
            import re as _re
            name_part = _re.sub(r'\s*\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\]\s*$', '', name_part).strip()
            card = {
                'raw': line.strip(),
                'vendor': 'unknown',
                'name': name_part,
                'packages': [],
                'install_cmd': '',
                'driver_installed': False,
                'packages_installed': False,
                'reboot_required': False,
            }
            if 'nvidia' in low:
                card['vendor'] = 'nvidia'
                pkgs = _nvidia_packages()
                card['packages'] = pkgs
                card['install_cmd'] = (
                    'apt-get update -qq && '
                    'DEBIAN_FRONTEND=noninteractive apt-get install -y ' + ' '.join(pkgs)
                )
            elif 'amd' in low or 'radeon' in low or 'advanced micro' in low:
                card['vendor'] = 'amd'
                card['packages'] = ['mesa-vulkan-drivers', 'mesa-va-drivers', 'firmware-amd-graphics']
                card['install_cmd'] = (
                    'apt-get update -qq && '
                    'DEBIAN_FRONTEND=noninteractive apt-get install -y '
                    'mesa-vulkan-drivers mesa-va-drivers firmware-amd-graphics'
                )
            elif 'intel' in low:
                card['vendor'] = 'intel'
                card['packages'] = ['intel-media-va-driver', 'mesa-vulkan-drivers']
                card['install_cmd'] = (
                    'apt-get update -qq && '
                    'DEBIAN_FRONTEND=noninteractive apt-get install -y '
                    'intel-media-va-driver mesa-vulkan-drivers'
                )
            # Check if driver is actually installed on host for this vendor
            if card['vendor'] != 'unknown':
                card['driver_installed'] = _check_vendor_driver(card['vendor'])
                card['packages_installed'] = _check_vendor_packages(card['vendor'])
                # Common case: package install completed, module not active until reboot.
                if card['packages_installed'] and not card['driver_installed']:
                    card['reboot_required'] = True
            result['cards'].append(card)
        # Global flag: True if any card has driver installed
        result['driver_installed'] = any(c.get('driver_installed') for c in result['cards'])
    except Exception:
        pass
    return result


def get_disk_info():
    global _prev_disk_counters, _prev_disk_time

    current_time = time.time()
    disks = []

    # Get real host partitions via nsenter + df
    r = _host_cmd("df -T -B1 2>/dev/null | tail -n +2")
    if r.returncode != 0 or not r.stdout.strip():
        # Fallback to container view
        return _get_disk_info_fallback(current_time)

    seen_devices = set()
    for line in r.stdout.strip().split('\n'):
        parts = line.split()
        if len(parts) < 7:
            continue
        device = parts[0]
        fstype = parts[1]
        mountpoint = ' '.join(parts[6:])

        # Skip pseudo/virtual filesystems
        if fstype in ('tmpfs', 'devtmpfs', 'squashfs', 'overlay', 'proc', 'sysfs',
                       'devpts', 'securityfs', 'cgroup2', 'pstore', 'efivarfs',
                       'bpf', 'tracefs', 'hugetlbfs', 'mqueue', 'debugfs',
                       'fusectl', 'configfs', 'ramfs', 'nsfs', 'fuse.snapfuse'):
            continue
        # Skip snap mounts
        if mountpoint.startswith('/snap'):
            continue
        # Deduplicate same device (keep first = root mount)
        if device in seen_devices:
            continue
        seen_devices.add(device)

        try:
            total = int(parts[2])
            used = int(parts[3])
            free = int(parts[4])
            percent = float(parts[5].replace('%', ''))
        except (ValueError, IndexError):
            continue

        if total == 0:
            continue

        # Determine label from lsblk
        label = ''
        model = ''

        disks.append({
            'device': device,
            'mountpoint': mountpoint,
            'fstype': fstype,
            'total': total,
            'used': used,
            'free': free,
            'percent': round(percent, 1),
            'read_bytes': 0,
            'write_bytes': 0,
            'read_speed': 0,
            'write_speed': 0,
        })

    # Enrich with lsblk model/label info
    lr = _host_cmd("lsblk -J -o NAME,MODEL,LABEL,TRAN,HOTPLUG,TYPE 2>/dev/null")
    lsblk_map = {}
    if lr.returncode == 0:
        try:
            ldata = json.loads(lr.stdout)
            def _walk(devs, parent=None):
                for d in devs:
                    name = d.get('name', '')
                    # Inherit model/tran from closest ancestor that has one
                    model = (d.get('model') or '').strip() or (parent or {}).get('_inherited_model', '')
                    tran = d.get('tran') or (parent or {}).get('_inherited_tran', '')
                    lsblk_map[name] = {
                        'model': model,
                        'label': d.get('label') or '',
                        'tran': tran,
                        'hotplug': d.get('hotplug'),
                        'type': d.get('type', ''),
                        '_inherited_model': model,
                        '_inherited_tran': tran,
                    }
                    _walk(d.get('children', []), parent=lsblk_map[name])
            _walk(ldata.get('blockdevices', []))
        except (json.JSONDecodeError, KeyError):
            pass

    for disk in disks:
        dev_name = disk['device'].split('/')[-1].replace('mapper/', '')
        # Try exact match or partial match for LVM names
        info = lsblk_map.get(dev_name)
        if not info:
            for k, v in lsblk_map.items():
                if k in dev_name or dev_name in k:
                    info = v
                    break
        if info:
            disk['model'] = info.get('model', '')
            disk['label'] = info.get('label', '')
            tran = info.get('tran', '')
            hotplug = info.get('hotplug')
            is_usb = tran == 'usb' or hotplug == '1' or hotplug is True
            disk['is_usb'] = is_usb
        else:
            disk['model'] = ''
            disk['label'] = ''
            disk['is_usb'] = False

    # Try to get I/O stats
    try:
        disk_io = psutil.disk_io_counters(perdisk=True)
        if disk_io:
            for disk in disks:
                dev_name = disk['device'].split('/')[-1]
                # Try various key patterns for LVM
                for io_key in [dev_name, dev_name.replace('-', '--'), dev_name.replace('mapper/', '')]:
                    if io_key in disk_io:
                        io = disk_io[io_key]
                        disk['read_bytes'] = io.read_bytes
                        disk['write_bytes'] = io.write_bytes
                        if _prev_disk_counters and io_key in _prev_disk_counters and _prev_disk_time:
                            dt = current_time - _prev_disk_time
                            if dt > 0:
                                disk['read_speed'] = max(0, (io.read_bytes - _prev_disk_counters[io_key]['read']) / dt)
                                disk['write_speed'] = max(0, (io.write_bytes - _prev_disk_counters[io_key]['write']) / dt)
                        break

            _prev_disk_counters = {}
            for dev_name, io in disk_io.items():
                _prev_disk_counters[dev_name] = {'read': io.read_bytes, 'write': io.write_bytes}
            _prev_disk_time = current_time
    except Exception:
        pass

    return disks


def _get_disk_info_fallback(current_time):
    """Fallback: use psutil inside container (less accurate)."""
    global _prev_disk_counters, _prev_disk_time
    partitions = psutil.disk_partitions(all=False)
    disks = []
    disk_io = psutil.disk_io_counters(perdisk=True)
    seen = set()
    for part in partitions:
        if part.device in seen:
            continue
        seen.add(part.device)
        if part.fstype in ('tmpfs', 'devtmpfs', 'squashfs', 'overlay'):
            continue
        if part.mountpoint.startswith('/snap'):
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                'device': part.device,
                'mountpoint': part.mountpoint,
                'fstype': part.fstype,
                'total': usage.total,
                'used': usage.used,
                'free': usage.free,
                'percent': usage.percent,
                'read_bytes': 0, 'write_bytes': 0,
                'read_speed': 0, 'write_speed': 0,
                'model': '', 'label': '', 'is_usb': False,
            })
        except (PermissionError, OSError):
            continue
    _prev_disk_time = current_time
    return disks


def get_network_info():
    global _prev_net_counters, _prev_net_time

    net_io = psutil.net_io_counters(pernic=True)
    net_addrs = psutil.net_if_addrs()
    net_stats = psutil.net_if_stats()
    current_time = time.time()

    interfaces = []
    for name, counters in net_io.items():
        if name == 'lo':
            continue
        name_lower = name.lower()
        if any(name_lower.startswith(p) for p in ['docker', 'br-', 'veth', 'virbr', 'flannel', 'cni', 'cali', 'weave']):
            continue

        addrs = []
        if name in net_addrs:
            for addr in net_addrs[name]:
                if addr.family.name == 'AF_INET':
                    addrs.append({'ipv4': addr.address})
                elif addr.family.name == 'AF_INET6':
                    addrs.append({'ipv6': addr.address})

        is_up = False
        speed = 0
        if name in net_stats:
            is_up = net_stats[name].isup
            speed = net_stats[name].speed

        iface_type = 'ethernet'
        if any(w in name_lower for w in ['wlan', 'wlp', 'wifi', 'wi-fi', 'wireless']):
            iface_type = 'wifi'
        elif any(w in name_lower for w in ['tun', 'vpn', 'wg']):
            iface_type = 'vpn'

        speed_sent = 0
        speed_recv = 0
        if _prev_net_counters and name in _prev_net_counters and _prev_net_time:
            dt = current_time - _prev_net_time
            if dt > 0:
                speed_sent = (counters.bytes_sent - _prev_net_counters[name]['sent']) / dt
                speed_recv = (counters.bytes_recv - _prev_net_counters[name]['recv']) / dt

        interfaces.append({
            'name': name,
            'type': iface_type,
            'is_up': is_up,
            'speed_mbps': speed,
            'bytes_sent': counters.bytes_sent,
            'bytes_recv': counters.bytes_recv,
            'packets_sent': counters.packets_sent,
            'packets_recv': counters.packets_recv,
            'errin': counters.errin,
            'errout': counters.errout,
            'dropin': counters.dropin,
            'dropout': counters.dropout,
            'speed_sent': max(0, speed_sent),
            'speed_recv': max(0, speed_recv),
            'addresses': addrs
        })

    _prev_net_counters = {}
    for name, counters in net_io.items():
        _prev_net_counters[name] = {'sent': counters.bytes_sent, 'recv': counters.bytes_recv}
    _prev_net_time = current_time

    return interfaces


def get_processes(sort_by='cpu', limit=30):
    procs = []
    for proc in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info', 'status', 'username', 'create_time', 'cmdline']):
        try:
            info = proc.info
            procs.append({
                'pid': info['pid'],
                'name': info['name'] or 'Unknown',
                'cpu_percent': info['cpu_percent'] or 0,
                'memory_percent': round(info['memory_percent'] or 0, 2),
                'memory_bytes': info['memory_info'].rss if info['memory_info'] else 0,
                'status': info['status'],
                'username': info['username'] or 'N/A',
                'cmdline': ' '.join(info['cmdline'][:5]) if info['cmdline'] else '',
                'create_time': info['create_time']
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if sort_by == 'cpu':
        procs.sort(key=lambda x: x['cpu_percent'], reverse=True)
    elif sort_by == 'memory':
        procs.sort(key=lambda x: x['memory_bytes'], reverse=True)
    elif sort_by == 'name':
        procs.sort(key=lambda x: x['name'].lower())

    return procs[:limit]


def kill_process(pid, signal='TERM'):
    try:
        proc = psutil.Process(pid)
        if signal == 'KILL':
            proc.kill()
        else:
            proc.terminate()
        return {'success': True, 'message': f'Process {pid} ({proc.name()}) terminated'}
    except psutil.NoSuchProcess:
        return {'success': False, 'message': f'Process {pid} not found'}
    except psutil.AccessDenied:
        return {'success': False, 'message': f'Access denied for process {pid}'}
    except Exception as e:
        return {'success': False, 'message': str(e)}


# USB device class descriptions
_USB_CLASS_NAMES = {
    '00': 'Composite device',
    '01': 'Audio',
    '02': 'Communication (CDC)',
    '03': 'HID (keyboard/mouse)',
    '05': 'Physical device',
    '06': 'Image (camera/scanner)',
    '07': 'Printer',
    '08': 'Mass storage (disk)',
    '09': 'USB Hub',
    '0a': 'CDC Data',
    '0b': 'Smart Card',
    '0d': 'Security',
    '0e': 'Video camera',
    '0f': 'Personal health',
    '10': 'Audio/Video',
    'dc': 'Diagnostics',
    'e0': 'Wireless (WiFi/BT)',
    'ef': 'Miscellaneous',
    'fe': 'Application specific',
    'ff': 'Vendor specific',
}


def get_usb_devices():
    devices = []
    try:
        result = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                match = re.match(r'Bus (\d+) Device (\d+): ID ([0-9a-f]+):([0-9a-f]+)\s*(.*)', line, re.IGNORECASE)
                if match:
                    devices.append({
                        'bus': match.group(1),
                        'device': match.group(2),
                        'vendor_id': match.group(3),
                        'product_id': match.group(4),
                        'product': match.group(5).strip() or 'Unknown device',
                        'manufacturer': '',
                        'serial': '',
                        'device_class': '',
                        'device_class_name': '',
                        'speed': '',
                        'power': '',
                    })
    except Exception:
        pass

    try:
        usb_path = '/sys/bus/usb/devices'
        if os.path.exists(usb_path):
            for dev_dir in os.listdir(usb_path):
                dev_full = os.path.join(usb_path, dev_dir)
                if os.path.isdir(dev_full):
                    try:
                        vendor_id = _read_sys_file(os.path.join(dev_full, 'idVendor'))
                        product_id = _read_sys_file(os.path.join(dev_full, 'idProduct'))
                        if vendor_id and product_id:
                            for d in devices:
                                if d['vendor_id'] == vendor_id and d['product_id'] == product_id:
                                    d['manufacturer'] = _read_sys_file(os.path.join(dev_full, 'manufacturer')) or d['manufacturer']
                                    d['serial'] = _read_sys_file(os.path.join(dev_full, 'serial')) or d['serial']
                                    cls = _read_sys_file(os.path.join(dev_full, 'bDeviceClass')) or d['device_class']
                                    d['device_class'] = cls
                                    d['device_class_name'] = _USB_CLASS_NAMES.get(cls.lower(), '')
                                    d['speed'] = _read_sys_file(os.path.join(dev_full, 'speed')) or ''
                                    power = _read_sys_file(os.path.join(dev_full, 'bMaxPower')) or ''
                                    d['power'] = power
                                    break
                    except Exception:
                        continue
    except Exception:
        pass

    # Filter out root hub controllers (device_class 09) for cleaner display
    devices = [d for d in devices if d.get('device_class') != '09']

    return devices


def _read_sys_file(path):
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                return f.read().strip()
    except Exception:
        pass
    return ''


def get_system_info():
    boot_time = psutil.boot_time()
    uptime = time.time() - boot_time
    return {
        'hostname': platform.node(),
        'os': f"{platform.system()} {platform.release()}",
        'architecture': platform.machine(),
        'uptime': uptime,
        'boot_time': boot_time
    }


def _parse_docker_size(s):
    s = s.strip()
    if not s or s == '0B':
        return 0
    units = {'B': 1, 'KB': 1000, 'MB': 1000**2, 'GB': 1000**3, 'TB': 1000**4,
             'KIB': 1024, 'MIB': 1024**2, 'GIB': 1024**3, 'TIB': 1024**4}
    match = re.match(r'([\d.]+)\s*([A-Za-z]+)', s)
    if match:
        val = float(match.group(1))
        unit = match.group(2).upper()
        return int(val * units.get(unit, 1))
    return 0


def get_docker_containers():
    containers = []
    if not _docker_available():
        return containers
    SEP = '|||'
    try:
        fmt_ps = SEP.join(['{{.ID}}', '{{.Names}}', '{{.Image}}', '{{.State}}', '{{.Status}}'])
        ps_result = subprocess.run(
            ['docker', 'ps', '-a', '--format', fmt_ps],
            capture_output=True, text=True, timeout=10
        )
        if ps_result.returncode != 0:
            return containers

        container_map = {}
        for line in ps_result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split(SEP)
            if len(parts) >= 5:
                container_map[parts[1]] = {
                    'id': parts[0][:12],
                    'name': parts[1],
                    'image': parts[2],
                    'status': parts[3],
                    'status_text': parts[4],
                    'cpu_percent': 0,
                    'memory_usage': 0,
                    'memory_limit': 0,
                    'memory_percent': 0,
                    'net_input': 0,
                    'net_output': 0,
                    'block_read': 0,
                    'block_write': 0,
                    'pids': 0
                }

        fmt_stats = SEP.join(['{{.Name}}', '{{.CPUPerc}}', '{{.MemUsage}}', '{{.MemPerc}}', '{{.NetIO}}', '{{.BlockIO}}', '{{.PIDs}}'])
        stats_result = subprocess.run(
            ['docker', 'stats', '--no-stream', '--format', fmt_stats],
            capture_output=True, text=True, timeout=30
        )
        if stats_result.returncode == 0:
            for line in stats_result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split(SEP)
                if len(parts) >= 7:
                    name = parts[0]
                    if name in container_map:
                        c = container_map[name]
                        c['cpu_percent'] = float(parts[1].replace('%', '').strip() or '0')
                        mem_parts = parts[2].split('/')
                        if len(mem_parts) == 2:
                            c['memory_usage'] = _parse_docker_size(mem_parts[0].strip())
                            c['memory_limit'] = _parse_docker_size(mem_parts[1].strip())
                        c['memory_percent'] = float(parts[3].replace('%', '').strip() or '0')
                        net_parts = parts[4].split('/')
                        if len(net_parts) == 2:
                            c['net_input'] = _parse_docker_size(net_parts[0].strip())
                            c['net_output'] = _parse_docker_size(net_parts[1].strip())
                        bio_parts = parts[5].split('/')
                        if len(bio_parts) == 2:
                            c['block_read'] = _parse_docker_size(bio_parts[0].strip())
                            c['block_write'] = _parse_docker_size(bio_parts[1].strip())
                        c['pids'] = int(parts[6].strip() or '0')

                        # Check for high usage alerts (throttled)
                        _check_resource_alert(c)

        containers = list(container_map.values())
        containers.sort(key=lambda x: x['cpu_percent'], reverse=True)

    except FileNotFoundError:
        pass  # Docker not installed — normal on native images
    except Exception as e:
        print(f'Docker error: {e}')

    return containers



def _check_resource_alert(container):
    """Log warning if container usage is critically high (throttled)."""
    name = container['name']
    mem_pct = container.get('memory_percent', 0)
    cpu_pct = container.get('cpu_percent', 0)

    msg = None
    if mem_pct > 90:
        msg = f'High memory usage by container {name}: {mem_pct:.1f}%'

    if msg:
        now = time.time()
        last = _last_alert_ts.get(name, 0)
        if (now - last > 300):
            try:
                logging.getLogger('monitor').warning(f'{msg} Details: {container}')
                _last_alert_ts[name] = now
            except Exception:
                pass
            _last_alert_ts[name] = now


def docker_action(container_id, action):
    allowed = ['start', 'stop', 'restart', 'pause', 'unpause', 'kill', 'remove']
    if action not in allowed:
        return {'success': False, 'message': f'Unknown action: {action}'}
    cmd = 'rm' if action == 'remove' else action
    args = ['docker', cmd]
    if action == 'remove':
        args.append('-f')
    args.append(container_id)
    try:
        result = subprocess.run(
            args,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return {'success': True, 'message': f'Container {container_id} {action}ed successfully'}
        else:
            return {'success': False, 'message': result.stderr.strip() or f'Failed to {action} container'}
    except Exception as e:
        return {'success': False, 'message': str(e)}
