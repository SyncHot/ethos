"""
EthOS — Hardware Discovery API
Full hardware inventory: CPU, RAM, disks, NICs, GPU, PCIe, USB, system DMI.

Endpoints:
  GET  /api/hardware/profile     — full hardware inventory (cached)
  POST /api/hardware/refresh     — force refresh hardware profile
  GET  /api/hardware/recommendations — auto-suggest config based on hardware
"""

import os
import sys
import json
import re
import time
from flask import Blueprint, jsonify

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, data_path

hardware_bp = Blueprint('hardware', __name__, url_prefix='/api/hardware')

_CACHE_FILE = data_path('hardware_profile.json')
_CACHE_TTL = 3600  # 1 hour
_profile_cache = {'data': None, 'ts': 0}


def _run(cmd, timeout=10):
    r = host_run(cmd, timeout=timeout)
    return r.stdout.strip() if r.returncode == 0 else ''


def _parse_cpu():
    """Parse CPU info from lscpu."""
    out = _run('lscpu 2>/dev/null')
    info = {}
    for line in out.split('\n'):
        if ':' in line:
            k, v = line.split(':', 1)
            info[k.strip()] = v.strip()

    cores = int(info.get('CPU(s)', '0') or '0')
    model = info.get('Model name', 'Unknown')
    arch = info.get('Architecture', '')

    # Frequency
    freq_max = info.get('CPU max MHz', '')
    freq_min = info.get('CPU min MHz', '')

    # Cache
    caches = {}
    for key in ['L1d cache', 'L1i cache', 'L2 cache', 'L3 cache']:
        if key in info:
            caches[key.replace(' cache', '')] = info[key]

    # Flags (virtualization, AES, etc.)
    flags_raw = _run("grep -m1 '^flags' /proc/cpuinfo 2>/dev/null")
    flags = flags_raw.split(':')[1].strip().split() if ':' in flags_raw else []
    notable = [f for f in flags if f in ('vmx', 'svm', 'aes', 'avx', 'avx2', 'avx512f', 'sse4_2', 'rdrand')]

    return {
        'model': model,
        'architecture': arch,
        'cores': cores,
        'threads_per_core': int(info.get('Thread(s) per core', '1') or '1'),
        'sockets': int(info.get('Socket(s)', '1') or '1'),
        'freq_max_mhz': float(freq_max) if freq_max else None,
        'freq_min_mhz': float(freq_min) if freq_min else None,
        'caches': caches,
        'notable_flags': notable,
        'virtualization': info.get('Virtualization', ''),
    }


def _parse_memory():
    """Parse RAM info from /proc/meminfo + dmidecode."""
    mem = {}
    out = _run('cat /proc/meminfo 2>/dev/null')
    for line in out.split('\n'):
        if ':' in line:
            k, v = line.split(':', 1)
            mem[k.strip()] = v.strip()

    total_kb = int(mem.get('MemTotal', '0').replace('kB', '').strip() or '0')

    # DIMM slots from dmidecode
    dimms = []
    dmi_out = _run('dmidecode -t memory 2>/dev/null')
    if dmi_out:
        current = {}
        for line in dmi_out.split('\n'):
            line = line.strip()
            if line.startswith('Memory Device'):
                if current:
                    dimms.append(current)
                current = {}
            elif ':' in line and current is not None:
                k, v = line.split(':', 1)
                k, v = k.strip(), v.strip()
                if k in ('Size', 'Type', 'Speed', 'Manufacturer', 'Locator', 'Form Factor'):
                    current[k.lower().replace(' ', '_')] = v
        if current:
            dimms.append(current)

    # Filter out empty slots
    populated = [d for d in dimms if d.get('size') and d['size'] not in ('No Module Installed', 'Not Installed', '0')]

    return {
        'total_bytes': total_kb * 1024,
        'total_human': f'{total_kb / 1048576:.1f} GB',
        'dimms': populated,
        'slots_total': len(dimms),
        'slots_used': len(populated),
    }


def _parse_disks():
    """Parse disk info from lsblk."""
    out = _run('lsblk -Jbno NAME,SIZE,TYPE,MODEL,SERIAL,ROTA,TRAN,VENDOR,REV,SUBSYSTEMS 2>/dev/null')
    disks = []
    try:
        data = json.loads(out)
        for dev in data.get('blockdevices', []):
            if dev.get('type') != 'disk':
                continue
            size = int(dev.get('size', 0) or 0)
            disks.append({
                'name': dev.get('name', ''),
                'model': (dev.get('model') or '').strip(),
                'serial': (dev.get('serial') or '').strip(),
                'size_bytes': size,
                'size_human': f'{size / (1024**3):.1f} GB' if size else '0',
                'rotational': bool(int(dev.get('rota', '1') or '1')),
                'transport': (dev.get('tran') or '').strip(),
                'type': 'HDD' if int(dev.get('rota', '1') or '1') else ('NVMe' if (dev.get('tran') or '') == 'nvme' else 'SSD'),
            })
    except (json.JSONDecodeError, ValueError):
        pass
    return disks


def _parse_nics():
    """Parse NIC info from sysfs + ethtool."""
    nics = []
    out = _run("ls /sys/class/net/ 2>/dev/null")
    for name in out.split():
        if name in ('lo',) or name.startswith(('docker', 'br-', 'veth', 'virbr')):
            continue
        nic = {'name': name}

        # Type
        wireless = os.path.isdir(f'/sys/class/net/{name}/wireless')
        nic['type'] = 'wifi' if wireless else 'ethernet'

        # MAC
        mac = _run(f"cat /sys/class/net/{name}/address 2>/dev/null")
        nic['mac'] = mac

        # Speed
        speed = _run(f"cat /sys/class/net/{name}/speed 2>/dev/null")
        nic['speed_mbps'] = int(speed) if speed and speed.lstrip('-').isdigit() and int(speed) > 0 else None

        # Driver
        driver_link = _run(f"readlink /sys/class/net/{name}/device/driver 2>/dev/null")
        nic['driver'] = os.path.basename(driver_link) if driver_link else ''

        # WoL capability
        ethtool = _run(f"ethtool {name} 2>/dev/null | grep -i 'wake-on'")
        wol_supports = ''
        for line in ethtool.split('\n'):
            if 'Supports Wake-on' in line:
                wol_supports = line.split(':')[1].strip() if ':' in line else ''
        nic['wol_capable'] = 'g' in wol_supports

        # Link features
        features = _run(f"ethtool -k {name} 2>/dev/null | head -20")
        offload = {}
        for line in features.split('\n'):
            if ':' in line:
                k, v = line.split(':', 1)
                k, v = k.strip(), v.strip()
                if k in ('tcp-segmentation-offload', 'generic-receive-offload', 'rx-checksumming', 'tx-checksumming'):
                    offload[k] = v.startswith('on')
        nic['offload'] = offload

        nics.append(nic)
    return nics


def _parse_gpus():
    """Parse GPU info from lspci."""
    gpus = []
    out = _run("lspci -nn 2>/dev/null | grep -iE 'VGA|3D|Display'")
    for line in out.split('\n'):
        if not line.strip():
            continue
        # Format: "00:02.0 VGA compatible controller [0300]: Intel Corporation ..."
        m = re.match(r'(\S+)\s+(.+?):\s+(.+?)(?:\s+\[([0-9a-f:]+)\])?$', line, re.I)
        if m:
            gpus.append({
                'slot': m.group(1),
                'class': m.group(2).strip(),
                'device': m.group(3).strip(),
                'id': m.group(4) or '',
            })
    return gpus


def _parse_usb():
    """Parse USB devices from lsusb."""
    devices = []
    out = _run("lsusb 2>/dev/null")
    for line in out.split('\n'):
        if not line.strip():
            continue
        # Format: "Bus 001 Device 002: ID 8087:0024 Intel Corp. ..."
        m = re.match(r'Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+(\S+)\s+(.*)', line)
        if m:
            devices.append({
                'bus': m.group(1),
                'device': m.group(2),
                'id': m.group(3),
                'name': m.group(4).strip(),
            })
    return devices


def _parse_system():
    """Parse system DMI info from dmidecode."""
    info = {}
    out = _run("dmidecode -t system 2>/dev/null")
    for line in out.split('\n'):
        line = line.strip()
        if ':' in line:
            k, v = line.split(':', 1)
            k, v = k.strip(), v.strip()
            if k in ('Manufacturer', 'Product Name', 'Version', 'Serial Number', 'UUID', 'Family'):
                info[k.lower().replace(' ', '_')] = v

    # BIOS info
    bios_out = _run("dmidecode -t bios 2>/dev/null")
    bios = {}
    for line in bios_out.split('\n'):
        line = line.strip()
        if ':' in line:
            k, v = line.split(':', 1)
            k, v = k.strip(), v.strip()
            if k in ('Vendor', 'Version', 'Release Date', 'BIOS Revision'):
                bios[k.lower().replace(' ', '_')] = v
    info['bios'] = bios

    # Baseboard
    bb_out = _run("dmidecode -t baseboard 2>/dev/null")
    board = {}
    for line in bb_out.split('\n'):
        line = line.strip()
        if ':' in line:
            k, v = line.split(':', 1)
            k, v = k.strip(), v.strip()
            if k in ('Manufacturer', 'Product Name', 'Version', 'Serial Number'):
                board[k.lower().replace(' ', '_')] = v
    info['baseboard'] = board

    return info


def _build_profile():
    """Build complete hardware profile."""
    return {
        'timestamp': time.time(),
        'system': _parse_system(),
        'cpu': _parse_cpu(),
        'memory': _parse_memory(),
        'disks': _parse_disks(),
        'network': _parse_nics(),
        'gpus': _parse_gpus(),
        'usb': _parse_usb(),
    }


def _get_profile(force=False):
    """Get hardware profile (cached)."""
    now = time.time()
    if not force and _profile_cache['data'] and (now - _profile_cache['ts']) < _CACHE_TTL:
        return _profile_cache['data']

    # Try disk cache
    if not force and os.path.isfile(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, 'r') as f:
                data = json.load(f)
            if (now - data.get('timestamp', 0)) < _CACHE_TTL:
                _profile_cache['data'] = data
                _profile_cache['ts'] = now
                return data
        except (OSError, json.JSONDecodeError):
            pass

    # Build fresh
    data = _build_profile()
    _profile_cache['data'] = data
    _profile_cache['ts'] = now

    # Save to disk
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass

    return data


@hardware_bp.route('/profile')
def get_hardware_profile():
    """Full hardware inventory."""
    return jsonify(_get_profile())


@hardware_bp.route('/refresh', methods=['POST'])
def refresh_profile():
    """Force refresh hardware profile."""
    data = _get_profile(force=True)
    return jsonify({'ok': True, 'profile': data})


@hardware_bp.route('/recommendations')
def get_recommendations():
    """Auto-suggest configuration based on hardware."""
    profile = _get_profile()
    recs = []

    cpu = profile.get('cpu', {})
    mem = profile.get('memory', {})
    disks = profile.get('disks', [])
    nics = profile.get('network', [])

    total_ram_gb = mem.get('total_bytes', 0) / (1024**3)

    # RAM recommendations
    if total_ram_gb < 2:
        recs.append({'category': 'memory', 'level': 'warning', 'message': 'Less than 2 GB RAM — Docker and VMs will be limited'})
    elif total_ram_gb >= 16:
        recs.append({'category': 'memory', 'level': 'info', 'message': f'{total_ram_gb:.0f} GB RAM — excellent for Docker containers and VMs'})

    # Disk recommendations
    hdds = [d for d in disks if d.get('rotational')]
    ssds = [d for d in disks if not d.get('rotational')]

    if ssds and hdds:
        recs.append({'category': 'storage', 'level': 'info', 'message': 'SSD + HDD combo detected — consider SSD caching for HDD pools'})
    if len(hdds) >= 2:
        recs.append({'category': 'storage', 'level': 'info', 'message': f'{len(hdds)} HDDs detected — RAID1/5 recommended for redundancy'})
    if not ssds:
        recs.append({'category': 'storage', 'level': 'info', 'message': 'No SSD detected — adding an SSD for system/cache would improve performance'})

    # CPU recommendations
    if 'vmx' in cpu.get('notable_flags', []) or 'svm' in cpu.get('notable_flags', []):
        recs.append({'category': 'cpu', 'level': 'info', 'message': 'Hardware virtualization supported — VMs available'})
    else:
        recs.append({'category': 'cpu', 'level': 'warning', 'message': 'No hardware virtualization — VMs will not work'})

    if 'aes' in cpu.get('notable_flags', []):
        recs.append({'category': 'cpu', 'level': 'info', 'message': 'AES-NI supported — LUKS encryption will be hardware-accelerated'})

    # Network recommendations
    eth_nics = [n for n in nics if n.get('type') == 'ethernet']
    if len(eth_nics) >= 2:
        recs.append({'category': 'network', 'level': 'info', 'message': f'{len(eth_nics)} Ethernet ports — link aggregation available for higher throughput'})

    fast_nics = [n for n in eth_nics if (n.get('speed_mbps') or 0) >= 2500]
    if fast_nics:
        recs.append({'category': 'network', 'level': 'info', 'message': f'2.5G+ NIC detected ({fast_nics[0]["name"]}) — enable jumbo frames for best performance'})

    return jsonify({'recommendations': recs})
