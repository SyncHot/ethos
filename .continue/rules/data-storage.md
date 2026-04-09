---
name: App Data Storage Rules
globs: "backend/**/*.py"
---

# App Data Storage Rules

EthOS runs on a **4 GB SquashFS root partition** (A/B slots). The root fills instantly if apps store large data there. All user data, databases, and large files MUST go to the data partition (`/mnt/data`).

## What goes where

| Stored on root ✅ | Stored on data partition ✅ |
|---|---|
| apt binaries (`/usr`, `/lib`) | Virus/ML/AI model databases |
| System configs (`/etc/*`) | App index/cache databases |
| pip packages (`venv/`) | Docker images & containers |
| Small state configs | VM disk images, downloaded media |

## Pattern: get_data_disk() with fallback

```python
from host import get_data_disk, data_path, q
import os

def _my_app_data_dir():
    """Return data dir — prefers /mnt/data, falls back to data_path."""
    dd = get_data_disk()          # returns '/mnt/data' or ''
    if dd:
        p = os.path.join(dd, 'myapp')
        os.makedirs(p, exist_ok=True)
        return p
    return data_path('myapp')     # fallback: /opt/ethos/data/myapp
```

- `data_path()` is also safe — on separate-disk installs `data/` is a symlink to `/mnt/data/ethos/data/`.
- Use `get_data_disk()` directly only when you need to escape the ethos subdirectory (e.g., Docker needs `/mnt/data/docker`, not `/mnt/data/ethos/data/docker`).

## Pattern: redirect third-party app data after apt install

When an apt package stores large data in `/var/lib/<pkg>`, redirect it:

```python
def _bg_install():
    r = apt_install('somepkg', timeout=300)
    if r.returncode != 0:
        return
    dd = get_data_disk()
    if dd:
        data_dir = os.path.join(dd, 'somepkg')
        os.makedirs(data_dir, exist_ok=True)
        host_run(f'chown -R somepkg:somepkg {q(data_dir)}', timeout=10)
        # Patch app config to use new dir...
```

## Checklist for every new app

1. Does the app install apt packages with data dirs in `/var/lib/<pkg>`? → Redirect to `get_data_disk()`.
2. Does the app download large files (models, media, ISOs)? → Use `data_path('myapp/models')` or `get_data_disk()`.
3. Does the app run containerized services? → Set data-root to data partition.
4. Does the app write a database/index? → Store in `data_path('myapp/myapp.db')`.
5. Does the app have a configurable data directory? → Patch to `get_data_disk()` path at install time.
