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

    # Required fields
    os_disk = data.get("os_disk", "").strip()
    data_disk = data.get("data_disk")  # None/"same"/device
    username = data.get("username", "").strip()
    password = data.get("password", "")
    hostname = data.get("hostname", "ethos").strip()
    lang = data.get("lang", "pl")
    confirm = data.get("confirmation", "").strip()

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
        bind_mounted = False
        try:
            def progress_cb(phase, pct, msg):
                _set_state(phase=phase, percent=pct, message=msg)
                _add_log(f"[{pct}%] {msg}")

            # Step 1: Disk install (partition + clone + GRUB)
            ok, err = disk_ops.install(os_disk, data_disk, progress_cb)
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
            _, merr, mrc = _run(f"mount -o subvol=@data {data_part} /mnt/data", timeout=30)
            if mrc != 0:
                log.warning("Data partition mount failed (%s), creating dirs directly", merr)
                # Broken symlinks from data separation would cause EEXIST
                # in later makedirs calls.  Resolve by creating the target dirs.
                for d in ("data", "logs", "backups", "uploads"):
                    tgt = f"/mnt/data/ethos/{d}"
                    os.makedirs(tgt, exist_ok=True)
                os.makedirs("/mnt/data/homes", exist_ok=True)
            # Bind-mount /dev for chroot operations (chpasswd, ssh-keygen, systemctl)
            _run(f"mount --bind /dev {mount_dir}/dev")
            _run(f"mount --bind /dev/pts {mount_dir}/dev/pts 2>/dev/null")
            _run(f"mount -t proc proc {mount_dir}/proc 2>/dev/null")
            _run(f"mount -t sysfs sysfs {mount_dir}/sys 2>/dev/null")
            bind_mounted = True

            # Step 3: Create user
            _set_state(phase="user", percent=88, message="Creating user account...")
            _add_log("[88%] Creating user account...")
            system_ops.create_user(username, password, root_dir=mount_dir)

            # Step 4: Set hostname
            _set_state(phase="hostname", percent=90, message="Setting hostname...")
            system_ops.set_hostname(hostname, root_dir=mount_dir)

            # Step 5: Write config
            _set_state(phase="config", percent=92, message="Writing configuration...")
            system_ops.write_install_conf(username, hostname, root_dir=mount_dir)

            # Write ETHOS_USER to ethos.env
            env_path = os.path.join(mount_dir, "opt/ethos/ethos.env")
            if os.path.exists(env_path):
                with open(env_path, "r") as ef:
                    env_content = ef.read()
                if "ETHOS_USER=" not in env_content:
                    with open(env_path, "a") as ef:
                        ef.write(f"ETHOS_USER={username}\n")

            # Step 5b: Mark setup complete (user already configured during install)
            system_ops.write_setup_done(username, hostname, root_dir=mount_dir)

            # Step 6: Configure services
            _set_state(phase="services", percent=94, message="Configuring services...")
            _add_log("[94%] Configuring services...")
            system_ops.configure_services(root_dir=mount_dir)
            _add_log("Regenerating SSH keys...")
            system_ops.regenerate_ssh_keys(root_dir=mount_dir)
            _add_log("Generating TLS certificate...")
            system_ops.generate_tls_cert(hostname, root_dir=mount_dir)

            # Step 7: Mark installed
            _set_state(phase="marker", percent=97, message="Finalizing...")
            system_ops.mark_installed(root_dir=mount_dir)

            # Get expected IP after reboot
            import wifi_ops
            net = wifi_ops.status()
            new_ip = (
                net.get("ethernet_ip")
                or net.get("wifi_ip")
                or "192.168.42.1"
            )

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
            if bind_mounted:
                for fs in ("sys", "proc", "dev/pts", "dev"):
                    _drun(f"umount -l {mount_dir}/{fs} 2>/dev/null", timeout=10)
            _drun(f"umount /mnt/data 2>/dev/null", timeout=15)
            _drun(f"umount -R {mount_dir} 2>/dev/null", timeout=30)

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
