"""
EthOS — Host Abstraction Layer
Centralizes all host command execution and path mapping.

Usage:
    from host import host_run, host_run_stream, host_path, app_path, data_path, q, NATIVE_MODE
"""

import json
import os
import subprocess
import threading
import time

# ── Always native mode ──
NATIVE_MODE = True

# ── Base paths ──
_env_root = os.environ.get('ETHOS_ROOT', '').strip()
if _env_root:
    ETHOS_ROOT = _env_root
else:
    # In development/runtime launched from source, /opt/ethos may not exist.
    # Fall back to repository root (../ from backend/host.py).
    _default_root = '/opt/ethos'
    if os.path.isdir(_default_root):
        ETHOS_ROOT = _default_root
    else:
        ETHOS_ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), '..'))
DATA_DIR = os.path.join(ETHOS_ROOT, 'data')
LOG_DIR = os.path.join(ETHOS_ROOT, 'logs')

# Ensure core runtime dirs exist regardless of launch mode.
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


def q(s):
    """Shell-quote a string for safe use in bash -c."""
    return "'" + s.replace("'", "'\\''") + "'"


def host_run(cmd, timeout=30, cwd=None):
    """Run a shell command on the host.

    Returns subprocess.CompletedProcess with .stdout, .stderr, .returncode
    """
    if cwd:
        cmd = f"cd {q(cwd)} && {cmd}"
    full_cmd = f"bash -c {q(cmd)}"

    return subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=timeout)


class _StreamWithPid:
    """Wrapper around a generator that exposes a .pid attribute."""
    def __init__(self, gen, pid):
        self._gen = gen
        self.pid = pid
    def __iter__(self):
        return self._gen
    def __next__(self):
        return next(self._gen)


def host_run_stream(cmd, cwd=None):
    """Run a host command and yield stdout+stderr lines in real time.

    Yields lines ending with \\n.
    Last line is always __EXIT_CODE__:<code>\\n
    The returned iterator has a .pid attribute with the subprocess PID.
    """
    if cwd:
        cmd = f"cd {q(cwd)} && {cmd}"

    full_cmd = f"bash -c {q(cmd)}"

    proc = subprocess.Popen(
        full_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )

    def _gen():
        for line in iter(proc.stdout.readline, ""):
            yield line
        proc.wait()
        yield f"__EXIT_CODE__:{proc.returncode}\n"

    return _StreamWithPid(_gen(), proc.pid)


def host_run_stream_raw(cmd, use_gevent=None):
    """Like host_run_stream but reads raw bytes, splitting on \\r and \\n.
    Used for dd progress which outputs \\r-delimited lines.
    When called from a threading.Thread, set use_gevent=False (or it auto-detects).
    Returns _StreamWithPid so caller can access .pid.
    """
    if use_gevent is None:
        # Auto-detect: if current thread is MainThread, gevent is safe
        use_gevent = threading.current_thread() is threading.main_thread()

    if use_gevent:
        import gevent.os
        _read = gevent.os.read
    else:
        _read = os.read

    full_cmd = f"bash -c {q(cmd)}"

    proc = subprocess.Popen(full_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def _gen():
        fd = proc.stdout.fileno()
        buf = b''
        while True:
            try:
                chunk = _read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b'\r' in buf or b'\n' in buf:
                idx_r = buf.find(b'\r')
                idx_n = buf.find(b'\n')
                if idx_r >= 0 and (idx_n < 0 or idx_r < idx_n):
                    line = buf[:idx_r].decode('utf-8', errors='replace')
                    buf = buf[idx_r + 1:]
                else:
                    line = buf[:idx_n].decode('utf-8', errors='replace')
                    buf = buf[idx_n + 1:]
                if line.strip():
                    yield line + '\n'
        if buf:
            line = buf.decode('utf-8', errors='replace').strip()
            if line:
                yield line + '\n'
        proc.wait()
        yield f"__EXIT_CODE__:{proc.returncode}\n"

    return _StreamWithPid(_gen(), proc.pid)


def host_path(path):
    """Returns path as-is (native mode)."""
    return path


def container_path(path):
    """Returns path as-is (native mode)."""
    return path


def app_path(rel=''):
    """Get absolute path within the app installation dir.
    app_path('backend/version.json') → /opt/ethos/backend/version.json
    """
    if rel:
        return os.path.join(ETHOS_ROOT, rel)
    return ETHOS_ROOT


def data_path(rel=''):
    """Get absolute path within the data dir.
    data_path('builder_state.json') → /opt/ethos/data/builder_state.json
    """
    if rel:
        return os.path.join(DATA_DIR, rel)
    return DATA_DIR


def user_data_path(filename, username):
    """Get per-user data file path.
    user_data_path('favorites.json', 'marcin') → /opt/ethos/data/favorites_marcin.json
    Inserts '_<username>' before the file extension.
    """
    base, ext = os.path.splitext(filename)
    return os.path.join(DATA_DIR, f'{base}_{username}{ext}')


def log_path(rel=''):
    """Get absolute path within the log dir."""
    if rel:
        return os.path.join(LOG_DIR, rel)
    return LOG_DIR


def browse_roots():
    """Return list of base paths for file browsing / searching."""
    return ['/home', '/media', '/run/media', '/mnt']


# ── Data disk helpers ────────────────────────────────────────

_SETUP_DONE = os.path.join(DATA_DIR, 'setup_done')

# Default user folder names — per language
# Keys: Documents, Downloads, Photos, Videos (canonical English IDs)
_FOLDER_NAMES_I18N = {
    'pl': {'Documents': 'Dokumenty', 'Downloads': 'Pobrane', 'Photos': 'Zdjęcia',  'Videos': 'Filmy'},
    'en': {'Documents': 'Documents', 'Downloads': 'Downloads', 'Photos': 'Photos',  'Videos': 'Videos'},
    'de': {'Documents': 'Dokumente', 'Downloads': 'Downloads', 'Photos': 'Fotos',   'Videos': 'Videos'},
    'fr': {'Documents': 'Documents', 'Downloads': 'Téléchargements', 'Photos': 'Photos', 'Videos': 'Vidéos'},
    'es': {'Documents': 'Documentos', 'Downloads': 'Descargas', 'Photos': 'Fotos',  'Videos': 'Vídeos'},
}


def _read_language():
    """Read LANGUAGE from ethos.env (no Flask dependency)."""
    env_path = os.path.join(ETHOS_ROOT, 'ethos.env')
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('LANGUAGE='):
                    return line.split('=', 1)[1].strip() or 'pl'
    except Exception:
        pass
    return 'pl'


def get_default_folders(lang=None):
    """Return list of default folder names for the given (or current) language.

    >>> get_default_folders('en')
    ['Documents', 'Downloads', 'Photos', 'Videos']
    """
    if lang is None:
        lang = _read_language()
    mapping = _FOLDER_NAMES_I18N.get(lang, _FOLDER_NAMES_I18N['pl'])
    return list(mapping.values())


def get_photo_folders(lang=None):
    """Return (photos_folder, videos_folder) names for the given language."""
    if lang is None:
        lang = _read_language()
    mapping = _FOLDER_NAMES_I18N.get(lang, _FOLDER_NAMES_I18N['pl'])
    return mapping['Photos'], mapping['Videos']


def get_all_photo_folder_variants():
    """Return set of all known Photos/Videos folder names across all languages.

    Used by gallery to auto-detect any localized variant.
    """
    photos = set()
    videos = set()
    for m in _FOLDER_NAMES_I18N.values():
        photos.add(m['Photos'])
        videos.add(m['Videos'])
    return photos, videos


# Legacy compat alias
USER_DEFAULT_FOLDERS = get_default_folders()


def get_data_disk():
    """Return the data disk mountpoint (e.g. '/mnt/data') or '' if none.

    Reads from ``DATA_DISK`` env var first, then falls back to the
    ``setup_done`` config file.
    """
    dd = os.environ.get('DATA_DISK', '').strip()
    if dd and dd != '/' and os.path.isdir(dd):
        return dd
    try:
        with open(_SETUP_DONE) as f:
            info = json.load(f)
        dd = info.get('data_disk', '').strip()
        if dd and dd != '/' and os.path.isdir(dd):
            return dd
    except Exception:
        pass
    return ''


def get_user_home(username):
    """Return a user's home directory path.

    When a data disk is configured, always prefer ``{data_disk}/home/{user}``
    (creating it if needed).  Falls back to ``getent passwd`` or
    ``/home/{username}``.
    """
    import shlex as _shlex

    # If a data disk is configured, that's the canonical home location
    dd = get_data_disk()
    if dd:
        dd_home = os.path.join(dd, 'home', username)
        # Create the directory if it doesn't exist yet
        try:
            os.makedirs(dd_home, mode=0o750, exist_ok=True)
        except OSError:
            pass
        if os.path.isdir(dd_home):
            return dd_home

    # No data disk — use system home from getent passwd
    r = host_run(f"getent passwd {_shlex.quote(username)} 2>/dev/null", timeout=5)
    if r.returncode == 0 and r.stdout.strip():
        parts = r.stdout.strip().split(':')
        if len(parts) >= 6 and parts[5] and os.path.isdir(parts[5]):
            return parts[5]
    return f'/home/{username}'


def ensure_user_home_structure(username, lang=None):
    """Create default folder skeleton inside the user's home directory.

    Creates localized folders (e.g. ~/Documents, ~/Dokumenty) based on
    the system language setting, plus ~/.ethos config directory.
    Silently skips anything that already exists.
    """
    home = get_user_home(username)
    if not os.path.isdir(home):
        return
    # Marker file to record created folders
    marker_file = os.path.join(home, '.ethos_created_folders')
    created_folders = []
    if os.path.isfile(marker_file):
        try:
            with open(marker_file, 'r') as f:
                created_folders = [line.strip() for line in f if line.strip()]
        except Exception:
            pass
    else:
        # First run: create folders and record them
        for folder in get_default_folders(lang):
            p = os.path.join(home, folder)
            try:
                os.makedirs(p, mode=0o755, exist_ok=True)
                created_folders.append(folder)
            except OSError:
                pass
        try:
            with open(marker_file, 'w') as f:
                for folder in created_folders:
                    f.write(folder + '\n')
        except Exception:
            pass
    # Migrate legacy .gabbyos → .ethos (one-time, for existing installs)
    old_dir = os.path.join(home, '.gabbyos')
    ethos_dir = os.path.join(home, '.ethos')
    if os.path.isdir(old_dir) and not os.path.isdir(ethos_dir):
        try:
            os.rename(old_dir, ethos_dir)
        except OSError:
            pass
    # .ethos dir for per-user app configs
    try:
        os.makedirs(ethos_dir, mode=0o700, exist_ok=True)
    except OSError:
        pass
    # Fix ownership — make sure everything belongs to the user
    import shlex as _shlex
    host_run(f"chown -R {_shlex.quote(username)}:{_shlex.quote(username)} {_shlex.quote(home)}", timeout=15)


def service_restart():
    """Restart EthOS service."""
    host_run('systemctl restart ethos', timeout=10)


def nsenter_args():
    """Return nsenter args list for os.execvp (terminal PTY).
    Returns empty list (native mode, no nsenter needed).
    """
    return []


# ── Dependency management ──

# Maps a binary name to the apt package that provides it
_DEP_PACKAGES = {
    # ── Printer / Document apps ──
    'lpadmin':    'cups',
    'lpstat':     'cups-client',
    'lpinfo':     'cups',
    'cupsenable': 'cups',
    'convert':    'imagemagick',
    'enscript':   'enscript',
    'pdftops':    'poppler-utils',
    'gs':         'ghostscript',
    'libreoffice': 'libreoffice-writer',
    # ── Samba / NFS / sharing ──
    'smbclient':  'smbclient',
    'smbpasswd':  'samba-common-bin',
    'smbd':       'samba',
    'exportfs':   'nfs-kernel-server',
    # ── Storage / disks ──
    'lsblk':      'util-linux',
    'parted':     'parted',
    'udisksctl':  'udisks2',
    'devmon':     'udevil',
    'smartctl':   'smartmontools',
    'hdparm':     'hdparm',
    'ntfs-3g':    'ntfs-3g',
    'mkfs.exfat': 'exfatprogs',
    'badblocks':  'e2fsprogs',
    # ── Hardware monitoring ──
    'sensors':    'lm-sensors',
    'lsusb':      'usbutils',
    'lspci':      'pciutils',
    # ── Network / WiFi ──
    'wpa_supplicant': 'wpasupplicant',
    'dnsmasq':    'dnsmasq',
    'rfkill':     'rfkill',
    'iwconfig':   'wireless-tools',
    'iw':         'iw',
    'avahi-daemon': 'avahi-daemon',
    # ── Archive / downloads ──
    'rsync':      'rsync',
    '7z':         'p7zip-full',
    'unrar':      'unrar-free',
    'zstd':       'zstd',
    # ── Multimedia / services ──
    'minidlnad':  'minidlna',
    'lighttpd':   'lighttpd',
    'vsftpd':     'vsftpd',
    'ffmpeg':     'ffmpeg',
    'ffprobe':    'ffmpeg',
    # ── Virtualisation ──
    'qemu-system-x86_64': 'qemu-system-x86 qemu-utils ovmf',
    'qemu-system-aarch64': 'qemu-system-arm qemu-efi-aarch64',
    # ── Web / SSL ──
    'nginx':      'nginx',
    'certbot':    'certbot',
    'crontab':    'cron',
}

# Packages that need a custom install script instead of apt
_DEP_CUSTOM_INSTALL = {
    'docker': 'curl -fsSL https://get.docker.com | sh && systemctl enable docker && systemctl start docker',
}


def check_dep(binary):
    """Check if a binary is available on the system."""
    import shutil
    return shutil.which(binary) is not None


def _apt_heal():
    """Auto-repair interrupted dpkg/apt state before installing packages.
    Fixes the 'dpkg was interrupted, run dpkg --configure -a' problem
    and removes stale apt/dpkg lock files that remain after crashes."""
    # Fix interrupted dpkg
    host_run('dpkg --configure -a 2>/dev/null', timeout=60)
    # Remove stale locks (only if no other apt/dpkg is running)
    host_run(
        'if ! fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then '
        '  rm -f /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock '
        '  /var/cache/apt/archives/lock /var/lib/apt/lists/lock 2>/dev/null; '
        'fi',
        timeout=10)


def _apt_exec(cmd, timeout=120, retries=1):
    """Run apt/dpkg command with lock and retries.

    Serializes operations with flock to avoid concurrent apt/dpkg races.
    """
    lock_file = '/tmp/ethos-apt.lock'
    wrapped = f"flock -w 180 {q(lock_file)} bash -lc {q(cmd)}"
    last = None
    for attempt in range(max(1, retries) + 1):
        _apt_heal()
        try:
            last = host_run(wrapped, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            last = subprocess.CompletedProcess(
                args=wrapped,
                returncode=124,
                stdout=(exc.stdout or ''),
                stderr=(exc.stderr or f'apt timeout after {timeout}s'),
            )
        except Exception as exc:
            last = subprocess.CompletedProcess(
                args=wrapped,
                returncode=1,
                stdout='',
                stderr=str(exc),
            )
        if last.returncode == 0:
            return last
        # Backoff helps when apt cache update/lock contention is transient.
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    return last


def apt_install(packages, timeout=120):
    """Install packages via apt with automatic self-healing.

    Automatically runs dpkg --configure -a and clears stale locks
    before installing. This prevents the common 'dpkg was interrupted'
    error on remote/unattended systems.

    Args:
        packages: space-separated package names (str) or list of names
        timeout: max seconds for the install command

    Returns:
        subprocess.CompletedProcess
    """
    if isinstance(packages, (list, tuple)):
        packages = ' '.join(packages)

    return _apt_exec(
        f'DEBIAN_FRONTEND=noninteractive apt-get update -y -qq 2>/dev/null; '
        f'DEBIAN_FRONTEND=noninteractive apt-get install -y {packages}',
        timeout=timeout,
        retries=1,
    )


def apt_remove(packages, timeout=180, purge=True, autoremove=True):
    """Remove packages via apt with lock/retry.

    Returns subprocess.CompletedProcess from the remove command.
    """
    if isinstance(packages, (list, tuple)):
        packages = ' '.join(packages)
    remove_cmd = 'purge' if purge else 'remove'
    r = _apt_exec(
        f'DEBIAN_FRONTEND=noninteractive apt-get {remove_cmd} -y {packages}',
        timeout=timeout,
        retries=1,
    )
    if autoremove:
        _apt_exec(
            'DEBIAN_FRONTEND=noninteractive apt-get autoremove -y',
            timeout=timeout,
            retries=1,
        )
    return r


def ensure_dep(binary, install=True):
    """Check if binary exists; optionally install its package. Returns (available, message)."""
    if check_dep(binary):
        return True, None
    if not install:
        pkg = _DEP_CUSTOM_INSTALL.get(binary) and binary or _DEP_PACKAGES.get(binary, binary)
        return False, f'Brak pakietu {pkg}. Zainstaluj aby korzystać z tej funkcji.'
    # Custom install (e.g. Docker via get.docker.com)
    if binary in _DEP_CUSTOM_INSTALL:
        r = host_run(_DEP_CUSTOM_INSTALL[binary], timeout=300)
        if r.returncode == 0 and check_dep(binary):
            return True, f'{binary} zainstalowany'
        return False, f'Instalacja {binary} nie powiodła się: {r.stderr[-300:]}'
    # Standard apt install
    pkg = _DEP_PACKAGES.get(binary)
    if not pkg:
        return False, f'Nie znaleziono pakietu dla: {binary}'
    r = apt_install(pkg, timeout=300)
    if r.returncode == 0 and check_dep(binary):
        return True, f'{pkg} zainstalowany'
    return False, f'Instalacja {pkg} nie powiodła się: {r.stderr[-200:]}'


_DEP_OWNERS_FILE = data_path('dep_owners.json')


def _load_dep_owners():
    try:
        with open(_DEP_OWNERS_FILE, 'r') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_dep_owners(state):
    os.makedirs(os.path.dirname(_DEP_OWNERS_FILE), exist_ok=True)
    tmp = _DEP_OWNERS_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _DEP_OWNERS_FILE)


def dep_package(binary):
    """Return apt package name(s) for a dependency binary."""
    return _DEP_PACKAGES.get(binary, binary)


def claim_dep(binary, owner):
    """Ensure dependency exists and register owner for shared cleanup.

    owner: stable package/app id that uses this dependency.
    """
    was_present = check_dep(binary)
    ok, msg = ensure_dep(binary, install=True)
    if not ok:
        return False, msg

    state = _load_dep_owners()
    entry = state.get(binary, {
        'owners': [],
        'managed': False,
        'package': dep_package(binary),
    })
    owners = entry.get('owners', [])
    if owner and owner not in owners:
        owners.append(owner)
    entry['owners'] = owners
    # Mark as managed only if EthOS actually had to install it.
    if not was_present:
        entry['managed'] = True
    state[binary] = entry
    _save_dep_owners(state)
    return True, msg


def release_dep(binary, owner):
    """Unregister owner and remove dependency when no owners remain.

    Dependency is removed only when it is EthOS-managed and no app still owns it.
    """
    state = _load_dep_owners()
    entry = state.get(binary)
    if not entry:
        return True, None

    owners = [o for o in entry.get('owners', []) if o != owner]
    entry['owners'] = owners

    if owners:
        state[binary] = entry
        _save_dep_owners(state)
        return True, None

    # Last owner removed. Purge only deps that EthOS installed.
    if entry.get('managed'):
        pkg = entry.get('package') or dep_package(binary)
        r = apt_remove(pkg, timeout=240, purge=True, autoremove=True)
        if r.returncode != 0:
            state[binary] = entry
            _save_dep_owners(state)
            err = (r.stderr or r.stdout or '').strip()
            return False, f'Nie udało się odinstalować {pkg}: {err[-200:]}'

    state.pop(binary, None)
    _save_dep_owners(state)
    return True, None
