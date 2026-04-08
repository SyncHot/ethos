"""
EthOS — Builder Secure Boot Pipeline

Manages Machine Owner Keys (MOK) for signing the kernel and GRUB EFI
binary so the image can boot on UEFI systems with Secure Boot enabled.

Key material lives in data/secureboot-keys/ (never committed to git):
  - MOK.key   — RSA-2048 private key (mode 0600)
  - MOK.crt   — self-signed X.509 certificate (10-year validity)
  - MOK.der   — DER-encoded cert for mokutil enrollment

Signing requires the host to have 'sbsign' installed (sbsigntool package).
If sbsign is unavailable the pipeline gracefully skips with a log warning.

End-user enrollment (once, on first physical boot):
  sudo mokutil --import /boot/efi/EFI/ethos/MOK.der
  → reboot → UEFI MOK manager UI → enroll key

After enrollment, UEFI will verify the signatures on each boot.
"""

import logging
import os
import time

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, q, data_path

logger = logging.getLogger('builder')

_KEY_DIR     = os.path.join(data_path('secureboot-keys'), '')
_MOK_KEY     = os.path.join(_KEY_DIR, 'MOK.key')
_MOK_CRT     = os.path.join(_KEY_DIR, 'MOK.crt')
_MOK_DER     = os.path.join(_KEY_DIR, 'MOK.der')


# ─────────────────────────────────────────────────────────
#  Key management
# ─────────────────────────────────────────────────────────

def ensure_mok_keys(brand_name: str = 'EthOS') -> bool:
    """
    Generate MOK key pair if not already present.
    Keys are stored in data/secureboot-keys/ (mode 0700 dir, 0600 key).
    Returns True if keys exist / were created successfully.
    """
    os.makedirs(_KEY_DIR, mode=0o700, exist_ok=True)

    if os.path.exists(_MOK_KEY) and os.path.exists(_MOK_CRT) and os.path.exists(_MOK_DER):
        logger.debug('builder_secureboot: MOK keys already present')
        return True

    logger.info('builder_secureboot: generating MOK RSA-2048 key pair...')

    subject = (
        f'/CN={brand_name} Secure Boot MOK'
        f'/O={brand_name}'
        f'/OU=EthOS Builder'
    )

    # Generate private key + self-signed cert (10 years)
    r = _host_run(
        f'openssl req -newkey rsa:2048 -nodes -keyout {q(_MOK_KEY)}'
        f' -new -x509 -sha256 -days 3650'
        f' -subj {q(subject)}'
        f' -out {q(_MOK_CRT)} 2>/dev/null',
        timeout=30,
    )
    if r.returncode != 0:
        logger.error('builder_secureboot: key generation failed: %s', r.stderr)
        return False

    # Convert to DER for mokutil enrollment
    r2 = _host_run(
        f'openssl x509 -in {q(_MOK_CRT)} -out {q(_MOK_DER)} -outform DER 2>/dev/null',
        timeout=10,
    )
    if r2.returncode != 0:
        logger.error('builder_secureboot: DER conversion failed')
        return False

    try:
        os.chmod(_MOK_KEY, 0o600)
        os.chmod(_KEY_DIR, 0o700)
    except Exception:
        pass

    logger.info('builder_secureboot: MOK keys created at %s', _KEY_DIR)
    return True


def get_mok_crt_path() -> str:
    return _MOK_CRT if os.path.exists(_MOK_CRT) else ''


def get_mok_der_path() -> str:
    return _MOK_DER if os.path.exists(_MOK_DER) else ''


# ─────────────────────────────────────────────────────────
#  Signing
# ─────────────────────────────────────────────────────────

def _sbsign_available() -> bool:
    r = _host_run('command -v sbsign 2>/dev/null', timeout=3)
    return r.returncode == 0 and bool(r.stdout.strip())


def sign_efi_binary(efi_path: str, brand_name: str = 'EthOS') -> bool:
    """
    Sign an EFI binary (kernel or GRUB EFI) in place with the MOK key.
    The original is backed up as <path>.unsigned before signing.

    Returns True on success.
    """
    if not os.path.isfile(efi_path):
        logger.warning('builder_secureboot: EFI binary not found: %s', efi_path)
        return False

    if not _sbsign_available():
        logger.warning(
            'builder_secureboot: sbsign not available — '
            'install sbsigntool to enable Secure Boot signing'
        )
        return False

    if not ensure_mok_keys(brand_name):
        return False

    backup = efi_path + '.unsigned'
    r = _host_run(f'cp {q(efi_path)} {q(backup)} 2>/dev/null', timeout=5)

    r = _host_run(
        f'sbsign --key {q(_MOK_KEY)} --cert {q(_MOK_CRT)}'
        f' --output {q(efi_path)} {q(efi_path)} 2>/dev/null',
        timeout=30,
    )
    if r.returncode != 0:
        logger.error('builder_secureboot: sbsign failed for %s: %s', efi_path, r.stderr)
        # Restore unsigned binary
        _host_run(f'mv {q(backup)} {q(efi_path)} 2>/dev/null', timeout=5)
        return False

    logger.info('builder_secureboot: signed %s', os.path.basename(efi_path))
    return True


def sign_rootfs_efi_binaries(rootfs: str, brand_name: str = 'EthOS') -> dict:
    """
    Sign all EFI binaries in the rootfs (kernel stubs + GRUB).

    Targets:
      /boot/vmlinuz-*       — unified kernel image (if EFI stub)
      /boot/efi/EFI/BOOT/BOOTX64.EFI
      /boot/efi/EFI/ethos/grubx64.efi  (if present)

    Returns dict: {path: True|False, ...}
    """
    results = {}

    # GRUB EFI binaries
    for rel in [
        'boot/efi/EFI/BOOT/BOOTX64.EFI',
        'boot/efi/EFI/ethos/grubx64.efi',
        'boot/efi/EFI/ethos/shimx64.efi',
    ]:
        full = os.path.join(rootfs, rel)
        if os.path.isfile(full):
            results[rel] = sign_efi_binary(full, brand_name)

    # Kernel vmlinuz files (Debian/Ubuntu kernels are EFI stubs)
    import glob
    for vmlinuz in sorted(glob.glob(os.path.join(rootfs, 'boot', 'vmlinuz-*'))):
        rel = vmlinuz.replace(rootfs.rstrip('/'), '').lstrip('/')
        results[rel] = sign_efi_binary(vmlinuz, brand_name)

    signed   = [k for k, v in results.items() if v]
    unsigned = [k for k, v in results.items() if not v]
    logger.info(
        'builder_secureboot: signed=%d skipped/failed=%d',
        len(signed), len(unsigned),
    )
    return results


def install_mok_der_to_esp(rootfs: str) -> bool:
    """
    Copy MOK.der into the ESP so the user can run mokutil after install.
    Destination: /boot/efi/EFI/ethos/MOK.der
    """
    if not os.path.exists(_MOK_DER):
        return False
    dest_dir = os.path.join(rootfs, 'boot', 'efi', 'EFI', 'ethos')
    try:
        os.makedirs(dest_dir, exist_ok=True)
        import shutil
        shutil.copy2(_MOK_DER, os.path.join(dest_dir, 'MOK.der'))
        logger.info('builder_secureboot: MOK.der installed to ESP')
        return True
    except Exception as exc:
        logger.warning('builder_secureboot: MOK.der install failed: %s', exc)
        return False
