"""Installation execution API routes — start, progress, reboot."""

import os
import threading
import time
import logging

from flask import Blueprint, jsonify, request
import disk_ops
import system_ops
from i18n import I18N

install_bp = Blueprint("install", __name__, url_prefix="/api/install")
log = logging.getLogger("ethos-installer")

# Global install state
_state = {
    "running": False,
    "phase": "",
    "percent": 0,
    "message": "",
    "done": False,
    "error": None,
    "new_ip": None,
    "os_disk": None,
}
_logs = []          # list of {"ts": float, "msg": str}
_lock = threading.Lock()


def _set_state(**kwargs):
    with _lock:
        _state.update(kwargs)


def _add_log(msg):
    """Append a timestamped log entry visible via /api/install/logs."""
    with _lock:
        _logs.append({"ts": time.time(), "msg": msg})


@install_bp.route("/start", methods=["POST"])
def start_install():
    """Begin system installation in background thread."""
    with _lock:
        if _state["running"]:
            return jsonify({"ok": False, "error": "Installation already running"}), 409

    data = request.get_json(silent=True) or {}

    # Required fields — strip /dev/ prefix if present (disk_ops expects bare names like 'vdb')
    os_disk = data.get("os_disk", "").strip().removeprefix("/dev/")
    data_disk = data.get("data_disk")  # None/"same"/device
    if isinstance(data_disk, str):
        data_disk = data_disk.strip().removeprefix("/dev/") or None
    username = data.get("username", "").strip()
    password = data.get("password", "")
    hostname = data.get("hostname", "ethos").strip()
    lang = data.get("lang", "pl")
    confirm = data.get("confirmation", "").strip()
    encrypt = bool(data.get("encrypt", False))
    passphrase = data.get("passphrase", "")

    # Validate confirmation token
    i18n = I18N()
    expected = i18n.confirm_token(lang)
    if confirm.upper() != expected:
        return jsonify({
            "ok": False,
            "error": f"Type '{expected}' to confirm",
        }), 400

    if not os_disk:
        return jsonify({"ok": False, "error": "No system disk selected"}), 400
    if not username:
        return jsonify({"ok": False, "error": "Username required"}), 400
    if len(password) < 4:
        return jsonify({"ok": False, "error": "Password too short (min 4)"}), 400
    if encrypt and len(passphrase) < 8:
        return jsonify({"ok": False, "error": "Encryption passphrase too short (min 8)"}), 400

    _set_state(
        running=True, phase="starting", percent=0,
        message="Starting installation...", done=False, error=None, new_ip=None,
        os_disk=os_disk,
    )
    with _lock:
        _logs.clear()
    _add_log("Installation started")

    def worker():
        mount_dir = "/mnt/ethos-target"
        sqsh_mount = "/tmp/sqsh-postinst"
        overlay_dir = "/mnt/ethos-overlay"
        bind_mounted = False
        squashfs_mode = False
        overlay_mounted = False
        try:
            def progress_cb(phase, pct, msg):
                _set_state(phase=phase, percent=pct, message=msg)
                _add_log(f"[{pct}%] {msg}")

            # Step 1: Disk install (partition + clone + GRUB)
            ok, err = disk_ops.install(os_disk, data_disk, progress_cb,
                                       encrypt=encrypt, passphrase=passphrase)
            if not ok:
                _add_log(f"ERROR: Disk install failed: {err}")
                _set_state(running=False, error=f"Disk install failed: {err}")
                return

            # Step 2: Mount target for post-config
            _add_log("[85%] Mounting target for post-install configuration...")
            _set_state(phase="postconfig", percent=85, message="Mounting target for configuration...")
            from disk_ops import _run, _part
            _, _, mrc = _run(f"mount {_part('/dev/' + os_disk, 2)} {mount_dir}", timeout=30)
            if mrc != 0:
                raise RuntimeError(f"Cannot mount root partition at {mount_dir}")
            # Mount btrfs data partition at /mnt/data so that absolute
            # symlinks created by data separation resolve correctly
            # (e.g. /opt/ethos/data → /mnt/data/ethos/data).
            _run("mkdir -p /mnt/data", timeout=5)
            same_disk = data_disk is None or data_disk == "same" or data_disk == os_disk
            if same_disk:
                data_part = _part('/dev/' + os_disk, 4)
            else:
                data_part = _part('/dev/' + data_disk, 1)

            # If encrypted, LUKS volume was closed during disk_ops cleanup — reopen
            if encrypt:
                from disk_ops import _luks_state
                keyfile = "/tmp/luks-keyfile"
                mapper = "ethos_data"
                if os.path.exists(keyfile):
                    _run(f"cryptsetup luksOpen --key-file {keyfile} {data_part} {mapper}",
                         timeout=30)
                    data_part = f"/dev/mapper/{mapper}"

            _, merr, mrc = _run(f"mount -o subvol=@data {data_part} /mnt/data", timeout=30)
            if mrc != 0:
                log.warning("Data partition mount failed (%s), creating dirs directly", merr)
                for d in ("data", "logs", "backups", "uploads"):
                    tgt = f"/mnt/data/ethos/{d}"
                    os.makedirs(tgt, exist_ok=True)
                os.makedirs("/mnt/data/homes", exist_ok=True)

            # Detect SquashFS mode — root partition has root.sqsh instead of
            # a full filesystem, so chroot into the raw partition won't work.
            # Set up a proper overlay mount (squashfs lower + data upper).
            sqsh_path = os.path.join(mount_dir, "root.sqsh")
            squashfs_mode = os.path.isfile(sqsh_path)

            if squashfs_mode:
                _add_log("[86%] Setting up SquashFS overlay for post-install...")
                os.makedirs(sqsh_mount, exist_ok=True)
                os.makedirs(overlay_dir, exist_ok=True)

                # Ensure overlay kernel module is loaded
                _run("modprobe overlay 2>/dev/null || true", timeout=10)

                # Mount squashfs as read-only lower layer
                _, serr, src = _run(
                    f"mount -t squashfs -o ro,loop {sqsh_path} {sqsh_mount}",
                    timeout=30,
                )
                if src != 0:
                    raise RuntimeError(f"Cannot mount squashfs: {serr}")

                # Set up overlay: squashfs (lower) + data partition (upper).
                # index=off is required when upperdir is on Btrfs (kernel ≥5.12
                # rejects Btrfs as overlay upper without this flag).
                # nfs_export=off avoids unrelated VFS inode requirements.
                overlay_upper = "/mnt/data/ethos/overlay/a/upper"
                overlay_work = "/mnt/data/ethos/overlay/a/work"
                os.makedirs(overlay_upper, exist_ok=True)
                os.makedirs(overlay_work, exist_ok=True)

                _, oerr, orc = _run(
                    f"mount -t overlay overlay "
                    f"-o lowerdir={sqsh_mount},upperdir={overlay_upper},"
                    f"workdir={overlay_work},index=off,nfs_export=off {overlay_dir}",
                    timeout=30,
                )
                if orc != 0:
                    raise RuntimeError(f"Cannot mount overlay: {oerr}")
                overlay_mounted = True

                # Use overlay as the effective root for chroot
                effective_root = overlay_dir

                # Bind-mount /dev, /proc, /sys for chroot
                _run(f"mount --bind /dev {effective_root}/dev")
                _run(f"mount --bind /dev/pts {effective_root}/dev/pts 2>/dev/null")
                _run(f"mount -t proc proc {effective_root}/proc 2>/dev/null")
                _run(f"mount -t sysfs sysfs {effective_root}/sys 2>/dev/null")
                bind_mounted = True

                # Bind-mount /mnt/data inside overlay so symlinks resolve
                os.makedirs(f"{effective_root}/mnt/data", exist_ok=True)
                _run(f"mount --bind /mnt/data {effective_root}/mnt/data")
            else:
                effective_root = mount_dir
                # Bind-mount /dev for chroot operations (chpasswd, ssh-keygen)
                _run(f"mount --bind /dev {mount_dir}/dev")
                _run(f"mount --bind /dev/pts {mount_dir}/dev/pts 2>/dev/null")
                _run(f"mount -t proc proc {mount_dir}/proc 2>/dev/null")
                _run(f"mount -t sysfs sysfs {mount_dir}/sys 2>/dev/null")
                bind_mounted = True

            # Step 3: Create user
            _set_state(phase="user", percent=88, message="Creating user account...")
            _add_log("[88%] Creating user account...")
            system_ops.create_user(username, password, root_dir=effective_root)

            # Step 4: Set hostname
            _set_state(phase="hostname", percent=90, message="Setting hostname...")
            system_ops.set_hostname(hostname, root_dir=effective_root)

            # Step 5: Write config
            _set_state(phase="config", percent=92, message="Writing configuration...")
            system_ops.write_install_conf(username, hostname, root_dir=effective_root)

            # Write ETHOS_USER to ethos.env
            env_path = os.path.join(effective_root, "opt/ethos/ethos.env")
            if os.path.exists(env_path):
                with open(env_path, "r") as ef:
                    env_content = ef.read()
                if "ETHOS_USER=" not in env_content:
                    with open(env_path, "a") as ef:
                        ef.write(f"ETHOS_USER={username}\n")

            # Step 5b: Mark setup complete (user already configured during install)
            system_ops.write_setup_done(username, hostname, root_dir=effective_root)

            # Step 6: Configure services
            _set_state(phase="services", percent=94, message="Configuring services...")
            _add_log("[94%] Configuring services...")
            system_ops.configure_services(root_dir=effective_root)
            _add_log("Regenerating SSH keys...")
            system_ops.regenerate_ssh_keys(root_dir=effective_root)
            _add_log("Generating TLS certificate...")
            system_ops.generate_tls_cert(hostname, root_dir=effective_root)

            # Step 7: Mark installed (or defer to firstboot for SquashFS)
            _set_state(phase="marker", percent=97, message="Finalizing...")
            if squashfs_mode:
                # SquashFS images ship venv as a symlink to /mnt/data/ethos/venv.
                # The actual venv (python packages) must be created on first boot
                # by firstboot-v2.sh.  Re-enable firstboot and skip .installed
                # so it runs on the first real boot.
                wants = os.path.join(
                    effective_root,
                    "etc/systemd/system/multi-user.target.wants",
                )
                fb_link = os.path.join(wants, "ethos-firstboot.service")
                try:
                    os.symlink(
                        "/etc/systemd/system/ethos-firstboot.service",
                        fb_link,
                    )
                    _add_log("[97%] Firstboot re-enabled for venv creation")
                except (FileExistsError, OSError) as exc:
                    _add_log(f"[97%] Firstboot symlink: {exc}")
            else:
                system_ops.mark_installed(root_dir=effective_root)

            # Get expected IP after reboot
            import subprocess as _sp
            try:
                _ip_out = _sp.check_output(
                    "ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -1",
                    shell=True, text=True, timeout=5
                ).strip()
            except Exception:
                _ip_out = ""
            new_ip = _ip_out or "—"

            # Write installer_result.json handover contract
            _add_log("[98%] Writing installer result...")
            import json as _json
            result_data = {
                "version": 2,
                "strategy": "same_disk" if same_disk else "separate_disk",
                "os_device": os_disk,
                "data_devices": [] if same_disk else [data_disk],
                "encrypt": encrypt,
                "username": username,
                "hostname": hostname,
                "lang": lang,
                "success": True,
            }
            # Write to target (data partition or root)
            result_paths = []
            data_result = "/mnt/data/ethos/data/installer_result.json"
            root_result = os.path.join(mount_dir, "opt/ethos/data/installer_result.json")
            if os.path.isdir("/mnt/data/ethos/data"):
                result_paths.append(data_result)
            if os.path.isdir(os.path.dirname(root_result)):
                result_paths.append(root_result)
            # Also overlay upper for squashfs mode
            if squashfs_mode:
                ovl_result = os.path.join(
                    mount_dir, "data-part/overlay/upper/opt/ethos/data/installer_result.json"
                ) if os.path.isdir(os.path.join(mount_dir, "data-part")) else None
                if ovl_result:
                    os.makedirs(os.path.dirname(ovl_result), exist_ok=True)
                    result_paths.append(ovl_result)
            for rp in result_paths:
                os.makedirs(os.path.dirname(rp), exist_ok=True)
                with open(rp, "w") as rf:
                    _json.dump(result_data, rf, indent=2)
            _add_log(f"[98%] Wrote installer_result.json to {len(result_paths)} location(s)")

            _set_state(
                running=False, done=True, percent=100,
                phase="done", message="Installation complete!",
                new_ip=new_ip,
            )
            log.info("Installation completed successfully. IP: %s", new_ip)

        except Exception as e:
            log.error("Install worker crashed: %s", e, exc_info=True)
            _add_log(f"FATAL: {e}")
            _set_state(running=False, error=str(e))

        finally:
            # Always unmount to prevent stale bind-mounts
            from disk_ops import _run as _drun
            if squashfs_mode and overlay_mounted:
                # Unmount overlay-specific mounts
                if bind_mounted:
                    _drun(f"umount -l {overlay_dir}/mnt/data 2>/dev/null", timeout=10)
                    for fs in ("sys", "proc", "dev/pts", "dev"):
                        _drun(f"umount -l {overlay_dir}/{fs} 2>/dev/null", timeout=10)
                _drun(f"umount {overlay_dir} 2>/dev/null", timeout=15)
                _drun(f"umount {sqsh_mount} 2>/dev/null", timeout=15)
            elif bind_mounted:
                for fs in ("sys", "proc", "dev/pts", "dev"):
                    _drun(f"umount -l {mount_dir}/{fs} 2>/dev/null", timeout=10)
            _drun(f"umount /mnt/data 2>/dev/null", timeout=15)
            _drun(f"umount -R {mount_dir} 2>/dev/null", timeout=30)
            # Close LUKS if still open
            if encrypt:
                _drun("cryptsetup close ethos_data 2>/dev/null", timeout=15)

    threading.Thread(target=worker, daemon=True, name="installer").start()
    return jsonify({"ok": True}), 202


@install_bp.route("/progress", methods=["GET"])
def progress():
    """Poll installation progress."""
    with _lock:
        return jsonify(dict(_state))


@install_bp.route("/logs", methods=["GET"])
def install_logs():
    """Return install log entries since a given index."""
    since = request.args.get("since", 0, type=int)
    with _lock:
        entries = _logs[since:]
        total = len(_logs)
    return jsonify({"logs": entries, "total": total})


@install_bp.route("/reboot", methods=["POST"])
def reboot():
    """Trigger system reboot after successful installation."""
    with _lock:
        if not _state.get("done"):
            return jsonify({"ok": False, "error": "Installation not complete"}), 400

    system_ops.reboot(delay=3)
    return jsonify({"ok": True, "message": "Rebooting in 3 seconds..."})
