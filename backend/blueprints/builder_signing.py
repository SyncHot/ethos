"""
EthOS — Builder Artifact Signing

Generates and manages an RSA-4096 key pair for signing build artifacts.
Each successful build produces an ethos-manifest.json that contains:
  - SHA-256 of the SquashFS image
  - dm-verity root hash
  - RSA-SHA256 signature of the payload "sha256:<sqsh_hash>:verity:<roothash>"
  - Embedded public key (PEM) for offline verification
  - Build timestamp

The manifest travels alongside the image and lets installers (and users)
verify that the artifact was produced by this EthOS instance and has not
been tampered with.
"""

import base64
import hashlib
import json
import logging
import os
import time

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run as _host_run, q, data_path

logger = logging.getLogger('builder')

_KEY_DIR         = os.path.join(data_path('signing-keys'), '')
_PRIVATE_KEY     = os.path.join(_KEY_DIR, 'builder-private.pem')
_PUBLIC_KEY      = os.path.join(_KEY_DIR, 'builder-public.pem')
_MANIFEST_NAME   = 'ethos-manifest.json'


# ─────────────────────────────────────────────────────────
#  Key management
# ─────────────────────────────────────────────────────────

def ensure_signing_key() -> bool:
    """
    Generate RSA-4096 key pair if not already present.
    Keys are stored in data/signing-keys/ (mode 0700 dir, 0600 private key).
    Returns True if keys exist/were created successfully.
    """
    os.makedirs(_KEY_DIR, mode=0o700, exist_ok=True)

    if os.path.exists(_PRIVATE_KEY) and os.path.exists(_PUBLIC_KEY):
        return True

    logger.info('builder_signing: generating RSA-4096 key pair...')
    r = _host_run(
        f'openssl genrsa -out {q(_PRIVATE_KEY)} 4096 2>/dev/null'
        f' && openssl rsa -in {q(_PRIVATE_KEY)}'
        f' -pubout -out {q(_PUBLIC_KEY)} 2>/dev/null',
        timeout=60,
    )
    if r.returncode != 0:
        logger.error('builder_signing: key generation failed: %s', r.stderr)
        return False

    try:
        os.chmod(_PRIVATE_KEY, 0o600)
        os.chmod(_KEY_DIR,     0o700)
    except Exception:
        pass

    logger.info('builder_signing: key pair created at %s', _KEY_DIR)
    return True


def get_public_key_pem() -> str:
    """Return the public key PEM string, or '' if unavailable."""
    try:
        return open(_PUBLIC_KEY).read().strip()
    except FileNotFoundError:
        return ''


# ─────────────────────────────────────────────────────────
#  Signing
# ─────────────────────────────────────────────────────────

def sign_artifact(sqsh_path: str, roothash: str, build_version: str = '') -> dict:
    """
    Sign a build artifact and return a manifest dict.

    Payload signed:  "sha256:<sqsh_sha256>:verity:<roothash>"
    Signature algo:  RSA-SHA256 (PKCS#1 v1.5)

    Returns the manifest dict on success, {} on failure.
    The caller is responsible for writing it to disk.
    """
    if not ensure_signing_key():
        logger.warning('builder_signing: no signing key — manifest will be unsigned')
        return {}

    if not os.path.isfile(sqsh_path):
        logger.error('builder_signing: sqsh file not found: %s', sqsh_path)
        return {}

    sqsh_sha256 = _sha256_file(sqsh_path)
    payload     = f'sha256:{sqsh_sha256}:verity:{roothash}'.encode()

    tmp_payload = f'/tmp/ethos-sign-payload-{os.getpid()}.bin'
    tmp_sig     = f'/tmp/ethos-sign-sig-{os.getpid()}.sig'
    try:
        with open(tmp_payload, 'wb') as f:
            f.write(payload)

        r = _host_run(
            f'openssl dgst -sha256 -sign {q(_PRIVATE_KEY)}'
            f' -out {q(tmp_sig)} {q(tmp_payload)} 2>/dev/null',
            timeout=15,
        )
        if r.returncode != 0:
            logger.error('builder_signing: openssl sign failed: %s', r.stderr)
            return {}

        with open(tmp_sig, 'rb') as f:
            sig_b64 = base64.b64encode(f.read()).decode()

        pub_pem = open(_PUBLIC_KEY).read().strip()

        manifest = {
            'version':        '1',
            'build_version':  build_version,
            'build_time':     int(time.time()),
            'sqsh_sha256':    sqsh_sha256,
            'verity_roothash': roothash,
            'signature':      sig_b64,
            'public_key':     pub_pem,
        }
        logger.info(
            'builder_signing: artifact signed — sqsh_sha256=%s roothash=%s',
            sqsh_sha256[:12] + '…', roothash[:12] + '…' if roothash else 'none',
        )
        return manifest

    finally:
        for f in (tmp_payload, tmp_sig):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass


def write_manifest(manifest: dict, out_dir: str) -> str:
    """
    Write manifest JSON to out_dir/ethos-manifest.json.
    Returns the full path on success, '' on error.
    """
    if not manifest:
        return ''
    path = os.path.join(out_dir, _MANIFEST_NAME)
    tmp  = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(manifest, f, indent=2)
        os.replace(tmp, path)
        logger.info('builder_signing: manifest written to %s', path)
        return path
    except Exception as exc:
        logger.error('builder_signing: failed to write manifest: %s', exc)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        return ''


# ─────────────────────────────────────────────────────────
#  Verification
# ─────────────────────────────────────────────────────────

def verify_artifact(sqsh_path: str, manifest_path: str) -> tuple:
    """
    Verify a build artifact against its manifest.

    Checks:
      1. SHA-256 of the .sqsh file matches manifest
      2. RSA-SHA256 signature is valid for the embedded public key

    Returns (ok: bool, error_message: str).
    """
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        return False, f'Cannot read manifest: {exc}'

    # 1. File integrity
    actual_sha = _sha256_file(sqsh_path)
    if actual_sha != manifest.get('sqsh_sha256'):
        return False, (
            f'SHA-256 mismatch: file is corrupted or tampered.\n'
            f'  Expected: {manifest.get("sqsh_sha256", "?")}\n'
            f'  Actual:   {actual_sha}'
        )

    # 2. Signature
    roothash = manifest.get('verity_roothash', '')
    payload  = f'sha256:{manifest["sqsh_sha256"]}:verity:{roothash}'.encode()
    pub_pem  = manifest.get('public_key', '')
    sig_b64  = manifest.get('signature', '')

    if not pub_pem or not sig_b64:
        # Unsigned manifest — SHA-256 matched but no signature
        return True, 'Warning: manifest has no signature (SHA-256 OK)'

    tmp_payload = f'/tmp/ethos-verify-payload-{os.getpid()}.bin'
    tmp_pubkey  = f'/tmp/ethos-verify-pub-{os.getpid()}.pem'
    tmp_sig     = f'/tmp/ethos-verify-sig-{os.getpid()}.sig'
    try:
        with open(tmp_payload, 'wb') as f:
            f.write(payload)
        with open(tmp_pubkey, 'w') as f:
            f.write(pub_pem)
        with open(tmp_sig, 'wb') as f:
            f.write(base64.b64decode(sig_b64))

        r = _host_run(
            f'openssl dgst -sha256 -verify {q(tmp_pubkey)}'
            f' -signature {q(tmp_sig)} {q(tmp_payload)} 2>/dev/null',
            timeout=10,
        )
        if r.returncode == 0:
            return True, ''
        return False, 'Signature verification failed — artifact may have been tampered with'

    except Exception as exc:
        return False, f'Verification error: {exc}'
    finally:
        for f in (tmp_payload, tmp_pubkey, tmp_sig):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass


# ─────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    """Return hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()
