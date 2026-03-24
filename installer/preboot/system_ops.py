"""
System operations — user creation, hostname, service config.
"""

import logging
import os
import subprocess

log = logging.getLogger("ethos-installer")


def _run(cmd, timeout=30):
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def create_user(username, password, root_dir="/"):
    """
    Create system user with sudo access.
    root_dir: "/" for live system, or "/mnt/ethos-target" for chroot.
    """
    log.info("Creating user: %s (root=%s)", username, root_dir)

    if root_dir == "/":
        # Live system
        _, _, rc = _run(f"id {_shq(username)} 2>/dev/null")
        if rc == 0:
            log.info("User %s already exists, updating password", username)
            _run(f"echo {_shq(username + ':' + password)} | chpasswd")
            return True

        groups = "sudo"
        _, err, rc = _run(
            f"useradd -m -s /bin/bash -G {groups} {_shq(username)} 2>&1"
        )
        if rc != 0:
            log.error("useradd failed: %s", err)
            return False

        _, err, rc = _run(
            f"echo {_shq(username + ':' + password)} | chpasswd"
        )
        if rc != 0:
            log.error("chpasswd failed: %s", err)
            return False
    else:
        # Chroot — write directly
        _run(
            f"chroot {root_dir} useradd -m -s /bin/bash -G sudo "
            f"{_shq(username)} 2>&1"
        )
        _run(
            f"chroot {root_dir} bash -c "
            f"\"echo {_shq(username + ':' + password)} | chpasswd\" 2>&1"
        )

    log.info("User %s created", username)
    return True


def set_hostname(hostname, root_dir="/"):
    """Set system hostname."""
    log.info("Setting hostname: %s", hostname)

    hostname_clean = hostname.strip().lower().replace(" ", "-")

    if root_dir == "/":
        _run(f"hostnamectl set-hostname {_shq(hostname_clean)}")
    else:
        path = f"{root_dir}/etc/hostname"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(hostname_clean + "\n")

        hosts_path = f"{root_dir}/etc/hosts"
        if os.path.exists(hosts_path):
            with open(hosts_path, "r") as f:
                content = f.read()
            if hostname_clean not in content:
                with open(hosts_path, "a") as f:
                    f.write(f"127.0.1.1  {hostname_clean}\n")


def write_install_conf(username, hostname, port=9000, root_dir="/"):
    """Write /opt/ethos/install.conf on the target system."""
    conf_path = os.path.join(root_dir, "opt/ethos/install.conf")
    os.makedirs(os.path.dirname(conf_path), exist_ok=True)

    content = (
        f'ETHOS_USER="{username}"\n'
        f'ETHOS_HOSTNAME="{hostname}"\n'
        f'ETHOS_NAS_NAME="EthOS"\n'
        f'ETHOS_PORT={port}\n'
        f'ETHOS_SETUP_WIZARD=no\n'
        f'ETHOS_BRAND_NAME="EthOS"\n'
    )
    with open(conf_path, "w") as f:
        f.write(content)
    log.info("Wrote install.conf: %s", conf_path)


def mark_installed(root_dir="/"):
    """Create .installed marker so preboot doesn't run again."""
    marker = os.path.join(root_dir, "opt/ethos/.installed")
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w") as f:
        f.write("installed\n")
    log.info("Created marker: %s", marker)


def write_setup_done(username, hostname, root_dir="/"):
    """Create setup_done so the web UI skips the setup wizard.

    Called after the installer has already collected credentials and
    configured the system — no wizard step needed on first boot.
    """
    import json
    import time

    data_dir = os.path.join(root_dir, "opt/ethos/data")
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "setup_done")
    with open(path, "w") as f:
        json.dump({
            "timestamp": int(time.time()),
            "hostname": hostname,
            "username": username,
            "nas_name": "EthOS",
        }, f)
    log.info("Created setup_done: %s", path)


def configure_services(root_dir="/"):
    """Enable ethos.service and disable preboot on target."""
    if root_dir == "/":
        _run("systemctl enable ethos.service 2>/dev/null")
        _run("systemctl disable ethos-preboot.service 2>/dev/null")
        _run("systemctl disable ethos-firstboot.service 2>/dev/null")
    else:
        _run(f"chroot {root_dir} systemctl enable ethos.service 2>/dev/null")
        _run(
            f"chroot {root_dir} systemctl disable ethos-preboot.service 2>/dev/null"
        )
        _run(
            f"chroot {root_dir} systemctl disable ethos-firstboot.service 2>/dev/null"
        )
    log.info("Services configured (root=%s)", root_dir)


def regenerate_ssh_keys(root_dir="/"):
    """Regenerate SSH host keys for security."""
    ssh_dir = os.path.join(root_dir, "etc/ssh")
    if os.path.isdir(ssh_dir):
        _run(f"rm -f {ssh_dir}/ssh_host_*")
        if root_dir == "/":
            _run("ssh-keygen -A 2>/dev/null")
        else:
            _run(f"chroot {root_dir} ssh-keygen -A 2>/dev/null")
        log.info("SSH keys regenerated")


def reboot(delay=3):
    """Schedule system reboot."""
    log.info("Rebooting in %d seconds...", delay)
    _run(f"(sleep {delay} && reboot) &")
