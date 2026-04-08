"""
EthOS — Builder Software Bill of Materials (SBOM)

Generates an SPDX-Lite compatible JSON document listing all installed
packages inside a chroot rootfs. The SBOM is injected into the built
image (installer/images/ethos-sbom.json) and also produced as a
stand-alone artifact next to the .img file.

SPDX fields produced per package:
  SPDXID, name, versionInfo, supplier, originator,
  filesAnalyzed (false), licenseConcluded, licenseDeclared,
  copyrightText, downloadLocation

References:
  https://spdx.github.io/spdx-spec/v2.3/
  https://ntia.gov/SBOM  (minimum required elements)
"""

import json
import logging
import os
import subprocess
import time

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, q

logger = logging.getLogger('builder')

_SBOM_FILENAME = 'ethos-sbom.json'

# dpkg-query format: name TAB version TAB arch TAB source TAB status
_DPKG_QUERY_FMT = '${Package}\t${Version}\t${Architecture}\t${Source}\t${Status}\n'

# Map common Debian license short names to SPDX identifiers
_LICENSE_MAP = {
    'apache-2': 'Apache-2.0',
    'apache2':  'Apache-2.0',
    'gpl-2':    'GPL-2.0-only',
    'gpl-2+':   'GPL-2.0-or-later',
    'gpl-3':    'GPL-3.0-only',
    'gpl-3+':   'GPL-3.0-or-later',
    'lgpl-2':   'LGPL-2.0-only',
    'lgpl-2+':  'LGPL-2.0-or-later',
    'lgpl-2.1': 'LGPL-2.1-only',
    'lgpl-3':   'LGPL-3.0-only',
    'mit':      'MIT',
    'bsd-2':    'BSD-2-Clause',
    'bsd-3':    'BSD-3-Clause',
    'isc':      'ISC',
    'mpl-2':    'MPL-2.0',
    'cc0':      'CC0-1.0',
    'public-domain': 'LicenseRef-Public-Domain',
}


# ─────────────────────────────────────────────────────────
#  Core generation
# ─────────────────────────────────────────────────────────

def generate_sbom(rootfs_path: str, build_version: str = '',
                  brand_name: str = 'EthOS') -> dict:
    """
    Generate SBOM for the packages installed in rootfs_path.

    Returns the full SBOM document as a dict.
    Returns {} on unrecoverable error.
    """
    rootfs_path = rootfs_path.rstrip('/')
    if not os.path.isdir(rootfs_path):
        logger.error('builder_sbom: rootfs not found: %s', rootfs_path)
        return {}

    logger.info('builder_sbom: scanning packages in %s', rootfs_path)

    packages = _scan_packages(rootfs_path)
    if not packages:
        logger.warning('builder_sbom: no packages found — dpkg-query may have failed')
        return {}

    logger.info('builder_sbom: found %d packages', len(packages))

    # Enrich with license info from copyright files
    packages = _enrich_licenses(rootfs_path, packages)

    now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    doc_ns   = f'https://ethos.local/sbom/{brand_name.lower()}-{build_version}-{int(time.time())}'

    sbom = {
        'SPDXID':          'SPDXRef-DOCUMENT',
        'spdxVersion':     'SPDX-2.3',
        'dataLicense':     'CC0-1.0',
        'name':            f'{brand_name} {build_version} SBOM',
        'documentNamespace': doc_ns,
        'creationInfo': {
            'created':     now_iso,
            'creators':    [f'Tool: EthOS Builder {build_version}'],
            'licenseListVersion': '3.21',
        },
        'packages': [_pkg_to_spdx(p) for p in packages],
        'relationships': [
            {
                'spdxElementId':      'SPDXRef-DOCUMENT',
                'relationshipType':   'DESCRIBES',
                'relatedSpdxElement': f'SPDXRef-{_spdx_id(p["name"])}',
            }
            for p in packages
        ],
        '_meta': {
            'total_packages': len(packages),
            'build_version':  build_version,
            'rootfs':         rootfs_path,
            'generated_at':   now_iso,
        },
    }
    return sbom


def write_sbom(sbom: dict, out_dir: str) -> str:
    """
    Write SBOM JSON to out_dir/ethos-sbom.json atomically.
    Returns the path on success, '' on error.
    """
    if not sbom:
        return ''
    path = os.path.join(out_dir, _SBOM_FILENAME)
    tmp  = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(sbom, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        size_kb = os.path.getsize(path) // 1024
        pkg_count = sbom.get('_meta', {}).get('total_packages', len(sbom.get('packages', [])))
        logger.info('builder_sbom: SBOM written to %s (%d KB, %d packages)',
                    path, size_kb, pkg_count if isinstance(pkg_count, int) else 0)
        return path
    except Exception as exc:
        logger.error('builder_sbom: write failed: %s', exc)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        return ''


# ─────────────────────────────────────────────────────────
#  Package scanning
# ─────────────────────────────────────────────────────────

def _scan_packages(rootfs: str) -> list:
    """
    Run dpkg-query inside the rootfs and parse results.
    Returns list of dicts: name, version, arch, source, status.
    """
    dpkg_bin = os.path.join(rootfs, 'usr', 'bin', 'dpkg-query')
    if not os.path.exists(dpkg_bin):
        dpkg_bin = 'dpkg-query'   # host fallback (won't give rootfs packages)

    # Use chroot to get accurate package list
    cmd = (
        f'chroot {q(rootfs)} dpkg-query '
        f'--showformat="{_DPKG_QUERY_FMT}" --show 2>/dev/null'
    )
    r = _host_run(cmd, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        logger.warning('builder_sbom: dpkg-query failed (rc=%d), trying host dpkg', r.returncode)
        return []

    packages = []
    for line in r.stdout.splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        parts = line.split('\t')
        if len(parts) < 5:
            continue
        name, version, arch, source, status = parts[0], parts[1], parts[2], parts[3], parts[4]
        # Only include installed packages
        if 'installed' not in status:
            continue
        packages.append({
            'name':    name,
            'version': version,
            'arch':    arch,
            'source':  source or name,
            'status':  status,
            'license': 'NOASSERTION',   # enriched below
        })
    return packages


def _enrich_licenses(rootfs: str, packages: list) -> list:
    """
    Read /usr/share/doc/<pkg>/copyright to extract license information.
    Updates each package dict's 'license' field in place.
    """
    doc_base = os.path.join(rootfs, 'usr', 'share', 'doc')
    if not os.path.isdir(doc_base):
        return packages

    for pkg in packages:
        copyright_file = os.path.join(doc_base, pkg['name'], 'copyright')
        if not os.path.isfile(copyright_file):
            continue
        try:
            # Read only first 4KB — license declaration is always near top
            with open(copyright_file, 'r', errors='replace') as f:
                text = f.read(4096).lower()
            pkg['license'] = _extract_license(text)
        except Exception:
            pass
    return packages


def _extract_license(text: str) -> str:
    """
    Heuristically extract an SPDX license ID from copyright file text.
    Returns SPDX ID string or 'NOASSERTION'.
    """
    # DEP-5 machine-readable field
    for line in text.splitlines():
        if line.startswith('license:'):
            raw = line.split(':', 1)[1].strip().lower()
            spdx = _LICENSE_MAP.get(raw)
            if spdx:
                return spdx
            # Try partial match
            for key, val in _LICENSE_MAP.items():
                if key in raw:
                    return val
            if raw:
                return f'LicenseRef-{raw[:40].replace(" ", "-")}'

    # Keyword scan fallback
    keywords = [
        ('apache-2.0', 'Apache-2.0'),
        ('apache license, version 2', 'Apache-2.0'),
        ('gnu general public license.*version 2', 'GPL-2.0-or-later'),
        ('gnu general public license.*version 3', 'GPL-3.0-or-later'),
        ('mit license', 'MIT'),
        ('permission is hereby granted', 'MIT'),
        ('bsd 2-clause', 'BSD-2-Clause'),
        ('bsd 3-clause', 'BSD-3-Clause'),
        ('isc license', 'ISC'),
        ('mozilla public license', 'MPL-2.0'),
        ('public domain', 'LicenseRef-Public-Domain'),
        ('lgpl', 'LGPL-2.1-or-later'),
    ]
    import re
    for pattern, spdx in keywords:
        if re.search(pattern, text):
            return spdx
    return 'NOASSERTION'


# ─────────────────────────────────────────────────────────
#  SPDX formatting helpers
# ─────────────────────────────────────────────────────────

def _spdx_id(name: str) -> str:
    """Sanitize package name to valid SPDX element ID."""
    import re
    return re.sub(r'[^a-zA-Z0-9.\-]', '-', name)


def _pkg_to_spdx(pkg: dict) -> dict:
    """Convert internal package dict to SPDX Package element."""
    return {
        'SPDXID':            f'SPDXRef-{_spdx_id(pkg["name"])}',
        'name':              pkg['name'],
        'versionInfo':       pkg['version'],
        'downloadLocation':  'https://deb.debian.org/debian',
        'filesAnalyzed':     False,
        'supplier':          f'Organization: Debian',
        'originator':        f'Organization: Debian',
        'externalRefs': [
            {
                'referenceCategory': 'PACKAGE-MANAGER',
                'referenceType':     'purl',
                'referenceLocator':  (
                    f'pkg:deb/debian/{pkg["name"]}@{pkg["version"]}'
                    f'?arch={pkg["arch"]}'
                ),
            }
        ],
        'licenseConcluded':  pkg.get('license', 'NOASSERTION'),
        'licenseDeclared':   pkg.get('license', 'NOASSERTION'),
        'copyrightText':     'NOASSERTION',
    }
