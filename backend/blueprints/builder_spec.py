"""
EthOS — Declarative Builder Specification

Reads a YAML build spec that defines image configuration.
Enables reproducible builds and custom image variants.

Spec file location: data/build-spec.yaml (user-editable)
Default spec is generated from current hardcoded values.
"""

import os
import copy
import logging

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path


DEFAULT_SPEC = {
    'base': {
        'distro': 'ubuntu',
        'arch': 'amd64',
        'release': 'noble',
        'mirror': 'http://archive.ubuntu.com/ubuntu',
        'img_size_gb': 8,
        'variant': 'minbase',
    },
    'identity': {
        'hostname': 'ethos',
        'brand_name': 'EthOS',
        'default_user': 'nasadmin',
        'default_password': 'ethos',
        'nas_port': 9000,
    },
    'partitions': {
        'esp_mb': 256,
        'root_mb': 4096,
        'root_type': 'ext4',
        'data_type': 'btrfs',
        'squashfs': True,
        'verity': True,
    },
    'packages': {
        'debootstrap': [
            'systemd', 'systemd-sysv', 'dbus',
            'linux-image-generic',
            'efibootmgr',
            'sudo', 'openssh-server', 'curl', 'ca-certificates', 'gnupg',
            'lsb-release', 'fail2ban',
            'iproute2', 'iputils-ping', 'wireguard-tools', 'qrencode',
            'bash', 'locales', 'console-setup',
            'python3', 'python3-minimal',
            'dosfstools', 'e2fsprogs', 'btrfs-progs', 'parted', 'util-linux',
            'squashfs-tools', 'zstd',
            'rsync', 'smartmontools', 'ethtool', 'hdparm', 'cpufrequtils',
            'cryptsetup',
            'usbutils', 'pciutils', 'lm-sensors', 'nut',
            'avahi-daemon', 'libnss-mdns',
            'kmod', 'udev',
        ],
        'apt_extra': [
            'python3-pip', 'python3-venv', 'python3-dev', 'gcc',
            'ufw', 'samba', 'nfs-kernel-server',
            'net-tools', 'wget', 'dnsutils',
        ],
        'pip': [
            'flask==3.1.0', 'gevent==24.11.1', 'gunicorn',
            'flask-socketio', 'psutil', 'netifaces',
        ],
    },
    'services': {
        'enable': [
            'systemd-networkd', 'systemd-resolved', 'avahi-daemon',
            'ssh', 'fail2ban', 'ethos',
        ],
        'disable': [
            'apt-daily.timer', 'apt-daily-upgrade.timer',
            'man-db.timer',
        ],
    },
    'apps': {
        'core': True,
        'optional_include': [],
        'optional_exclude': [],
    },
    'security': {
        'ssh_password_auth': True,
        'ufw_default_deny': True,
        'ufw_allow_ports': [9000, 22],
        'fail2ban': True,
    },
    'build': {
        'use_tmpfs': True,
        'tmpfs_min_ram_mb': 10000,
        'cache_debootstrap': True,
        'cache_apt': True,
        'compression': 'zstd',
        'compression_level': 3,
    },
    'preflight': {
        'enabled': False,
        'timeout_seconds': 180,
    },
}


def get_spec_path():
    """Return path to the build spec file."""
    return data_path('build-spec.yaml')


def load_spec():
    """Load build spec from YAML, falling back to defaults."""
    spec = copy.deepcopy(DEFAULT_SPEC)

    path = get_spec_path()
    if not os.path.isfile(path):
        return spec

    if yaml is None:
        logger.warning("PyYAML not installed — using default build spec")
        return spec

    try:
        with open(path) as f:
            user_spec = yaml.safe_load(f) or {}

        for section, values in user_spec.items():
            if section in spec and isinstance(spec[section], dict) and isinstance(values, dict):
                spec[section].update(values)
            else:
                spec[section] = values

        logger.info("Loaded build spec from %s", path)
    except Exception as e:
        logger.warning("Error loading build spec %s: %s — using defaults", path, e)

    return spec


def save_spec(spec):
    """Save build spec to YAML."""
    if yaml is None:
        raise RuntimeError("PyYAML not installed — cannot save build spec")

    path = get_spec_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        yaml.dump(spec, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    logger.info("Saved build spec to %s", path)


def generate_default_spec():
    """Generate the default spec file if it doesn't exist."""
    path = get_spec_path()
    if os.path.isfile(path):
        return path

    if yaml is None:
        logger.warning("PyYAML not installed — cannot generate default spec")
        return None

    save_spec(DEFAULT_SPEC)
    return path


def spec_to_shell_vars(spec):
    """Convert spec to shell variable assignments for the build script."""
    base = spec.get('base', {})
    identity = spec.get('identity', {})
    partitions = spec.get('partitions', {})
    build_cfg = spec.get('build', {})

    lines = [
        f'BASE_DISTRO="{base.get("distro", "ubuntu")}"',
        f'DEBIAN_RELEASE="{base.get("release", "noble")}"',
        f'IMG_SIZE_GB={base.get("img_size_gb", 8)}',
        f'DEFAULT_USER="{identity.get("default_user", "nasadmin")}"',
        f'DEFAULT_HOSTNAME="{identity.get("hostname", "ethos")}"',
        f'USER_PASS="{identity.get("default_password", "ethos")}"',
        f'NAS_PORT="{identity.get("nas_port", 9000)}"',
        f'BRAND_NAME="{identity.get("brand_name", "EthOS")}"',
        f'ESP_SIZE_MB={partitions.get("esp_mb", 256)}',
        f'ROOT_SIZE_MB={partitions.get("root_mb", 4096)}',
        f'TMPFS_MIN_RAM_MB={build_cfg.get("tmpfs_min_ram_mb", 10000)}',
        f'SQSH_COMPRESSION_LEVEL={build_cfg.get("compression_level", 3)}',
    ]

    pflight = spec.get('preflight', {})
    lines.append(f'PREFLIGHT_ENABLED={1 if pflight.get("enabled", True) else 0}')
    lines.append(f'PREFLIGHT_TIMEOUT={pflight.get("timeout_seconds", 180)}')

    pkgs = spec.get('packages', {}).get('debootstrap', DEFAULT_SPEC['packages']['debootstrap'])
    lines.append(f'DEBOOTSTRAP_INCLUDE="{",".join(pkgs)}"')

    apt_extra = spec.get('packages', {}).get('apt_extra', DEFAULT_SPEC['packages']['apt_extra'])
    lines.append(f'APT_EXTRA_PKGS="{" ".join(apt_extra)}"')

    return '\n'.join(lines)
