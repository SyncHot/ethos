"""
System operations — user creation, hostname, service config.
"""

import logging
import os
import subprocess

log = logging.getLogger("ethos-installer")


def _run(cmd, timeout=30):
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout.strip(), stderr.strip(), proc.returncode
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            proc.wait()
        log.warning("Command timed out after %ds: %s", timeout, cmd[:120])
        return "", "timeout", 1
    except Exception as e:
        if proc is not None:
            try:
                os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            proc.wait()
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

        # Ensure groups exist
        for g in ("ethos-admin", "ethos-user", "ethos-family"):
            _run(f"getent group {g} >/dev/null 2>&1 || groupadd {g}")

        groups = "sudo,ethos-admin,ethos-user"
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
        # Chroot — /dev must be bind-mounted by caller for chpasswd
        for g in ("ethos-admin", "ethos-user", "ethos-family"):
            _run(f"chroot {root_dir} bash -c 'getent group {g} >/dev/null 2>&1 || groupadd {g}'")

        _run(
            f"chroot {root_dir} useradd -m -s /bin/bash -G sudo,ethos-admin,ethos-user "
            f"{_shq(username)} 2>&1"
        )
        out, err, rc = _run(
            f"chroot {root_dir} bash -c "
            f"\"echo {_shq(username + ':' + password)} | chpasswd\" 2>&1"
        )
        if rc != 0:
            log.warning("chpasswd returned code %d: %s — falling back to usermod", rc, err)
            import crypt
            salt = crypt.mksalt(crypt.METHOD_SHA512)
            hashed = crypt.crypt(password, salt)
            _run(f"chroot {root_dir} usermod -p {_shq(hashed)} {_shq(username)}")

    # Grant full passwordless sudo to the installed user
    sudoers_path = os.path.join(root_dir, f"etc/sudoers.d/010_{username}") if root_dir != "/" else f"/etc/sudoers.d/010_{username}"
    os.makedirs(os.path.dirname(sudoers_path), exist_ok=True)
    with open(sudoers_path, "w") as f:
        f.write(f"{username} ALL=(ALL) NOPASSWD:ALL\n")
    os.chmod(sudoers_path, 0o440)
    log.info("Created sudoers file: %s", sudoers_path)

    # Remove default builder user if a different username was chosen
    default_user = "nasadmin"
    if username != default_user:
        if root_dir == "/":
            _run(f"pkill -u {_shq(default_user)} 2>/dev/null")
            _run(f"userdel -r {_shq(default_user)} 2>/dev/null")
        else:
            _run(f"chroot {root_dir} userdel -r {_shq(default_user)} 2>/dev/null")
        log.info("Removed default user %s", default_user)

        # Clean up old nasadmin sudoers file
        old_sudoers = os.path.join(root_dir, "etc/sudoers.d/010_ethos") if root_dir != "/" else "/etc/sudoers.d/010_ethos"
        if os.path.exists(old_sudoers):
            os.remove(old_sudoers)
            log.info("Removed old sudoers: %s", old_sudoers)

    # Update getty autologin override to use the installed user (builder defaults to nasadmin)
    getty_dir = os.path.join(root_dir, "etc/systemd/system/getty@tty1.service.d") if root_dir != "/" else "/etc/systemd/system/getty@tty1.service.d"
    getty_override = os.path.join(getty_dir, "override.conf")
    if os.path.isdir(getty_dir):
        with open(getty_override, "w") as f:
            f.write("[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin "
                    f"{username} --noclear %I $TERM\n")
        log.info("Updated getty autologin to user: %s", username)

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
    """Create setup_done and .password_changed so the web UI skips the
    setup wizard and password-change gate.

    Called after the installer has already collected credentials and
    configured the system — no wizard step needed on first boot.
    """
    import json
    import time

    ethos_root = os.path.join(root_dir, "opt/ethos")

    data_dir = os.path.join(ethos_root, "data")
    # After data separation, data_dir is a symlink → /mnt/data/ethos/data.
    # If /mnt/data is mounted (normal case), makedirs follows the symlink.
    # If dangling (mount failed), we fall back to creating a real directory
    # so setup_done is written somewhere the system can find it on boot.
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        if os.path.islink(data_dir):
            log.warning("data symlink dangling — removing and creating real dir")
            os.remove(data_dir)
            os.makedirs(data_dir, exist_ok=True)
        else:
            raise
    path = os.path.join(data_dir, "setup_done")
    with open(path, "w") as f:
        json.dump({
            "timestamp": int(time.time()),
            "hostname": hostname,
            "username": username,
            "nas_name": "EthOS",
        }, f)
    log.info("Created setup_done: %s", path)

    # User already chose a password during install — skip the force-change gate
    pw_marker = os.path.join(ethos_root, ".password_changed")
    with open(pw_marker, "w") as f:
        f.write("installer\n")
    log.info("Created .password_changed: %s", pw_marker)


def configure_services(root_dir="/"):
    """Enable ethos.service, disable preboot, and activate UFW on target."""
    if root_dir == "/":
        _run("systemctl enable ethos.service 2>/dev/null")
        _run("systemctl disable ethos-preboot.service 2>/dev/null")
        _run("systemctl disable ethos-firstboot.service 2>/dev/null")
        _run("bash -c 'echo y | ufw enable' 2>/dev/null")
        _run("systemctl enable ufw 2>/dev/null")
    else:
        # In chroot: use symlinks directly — chroot systemctl/ufw can hang
        wants = os.path.join(root_dir, "etc/systemd/system/multi-user.target.wants")
        os.makedirs(wants, exist_ok=True)

        # Enable ethos.service
        link = os.path.join(wants, "ethos.service")
        if not os.path.exists(link):
            try:
                os.symlink("/etc/systemd/system/ethos.service", link)
            except OSError:
                pass

        # Disable preboot + firstboot
        for svc in ("ethos-preboot.service", "ethos-firstboot.service"):
            svc_link = os.path.join(wants, svc)
            if os.path.exists(svc_link):
                os.remove(svc_link)

        # Enable UFW via symlink (rules pre-configured by builder, activated on first real boot)
        ufw_link = os.path.join(wants, "ufw.service")
        if not os.path.exists(ufw_link):
            ufw_svc = "/lib/systemd/system/ufw.service"
            ufw_svc_alt = os.path.join(root_dir, "lib/systemd/system/ufw.service")
            target = ufw_svc if os.path.exists(ufw_svc_alt) else ufw_svc
            try:
                os.symlink(target, ufw_link)
            except OSError:
                pass

    log.info("Services configured (root=%s)", root_dir)


def regenerate_ssh_keys(root_dir="/"):
    """Regenerate SSH host keys for security."""
    ssh_dir = os.path.join(root_dir, "etc/ssh")
    if os.path.isdir(ssh_dir):
        _run(f"rm -f {ssh_dir}/ssh_host_*")
        if root_dir == "/":
            _run("ssh-keygen -A 2>/dev/null")
        else:
            # Generate keys directly into target — avoids chroot hangs
            for ktype in ("rsa", "ecdsa", "ed25519"):
                kfile = os.path.join(ssh_dir, f"ssh_host_{ktype}_key")
                _run(f'ssh-keygen -t {ktype} -f {kfile} -N "" -q', timeout=30)
        log.info("SSH keys regenerated")


def generate_tls_cert(hostname, root_dir="/"):
    """Generate self-signed TLS certificate for HTTPS.

    Mirrors the logic from firstboot-v2.sh so that certificates are created
    during the standard installer path (where firstboot is disabled).

    After data separation, {root_dir}/opt/ethos/data is an absolute symlink
    to /mnt/data/ethos/data.  When called from routes_install.py, /mnt/data
    is mounted on the HOST — Python follows the absolute symlink transparently.
    """
    ethos_dir = os.path.join(root_dir, "opt/ethos")
    ssl_dir = os.path.join(ethos_dir, "data/ssl")
    ssl_key = os.path.join(ssl_dir, "ethos.key")
    ssl_crt = os.path.join(ssl_dir, "ethos.crt")

    if os.path.exists(ssl_crt):
        log.info("TLS certificate already exists, skipping")
        return

    # data/ may be a dangling symlink if /mnt/data isn't mounted.
    # os.makedirs follows symlinks — if the target resolves, it works;
    # if dangling, we get OSError and skip gracefully.
    try:
        os.makedirs(ssl_dir, exist_ok=True)
    except OSError as e:
        log.warning("Cannot create SSL dir %s (data partition not mounted?): %s", ssl_dir, e)
        return

    fqdn = f"{hostname}.local" if hostname else "ethos.local"
    _, _, rc = _run(
        f'openssl req -x509 -newkey rsa:4096 -nodes '
        f'-keyout {ssl_key} -out {ssl_crt} '
        f'-days 3650 '
        f'-subj "/CN={fqdn}/O=EthOS/OU=Auto-generated" '
        f'-addext "subjectAltName=DNS:{fqdn},DNS:ethos.local,DNS:localhost" '
        f'2>/dev/null',
        timeout=60,
    )

    if rc == 0 and os.path.exists(ssl_crt):
        os.chmod(ssl_key, 0o600)
        os.chmod(ssl_crt, 0o644)
        # Register paths in ethos.env
        env_path = os.path.join(ethos_dir, "ethos.env")
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                env_content = f.read()
            if "SSL_CERT=" not in env_content:
                with open(env_path, "a") as f:
                    f.write(f"SSL_CERT=/opt/ethos/data/ssl/ethos.crt\n")
                    f.write(f"SSL_KEY=/opt/ethos/data/ssl/ethos.key\n")
        log.info("TLS certificate generated: %s", ssl_crt)
    else:
        log.warning("TLS certificate generation failed (rc=%s)", rc)


def reboot(delay=3):
    """Schedule system reboot."""
    log.info("Rebooting in %d seconds...", delay)
    _run(f"(sleep {delay} && reboot) &")
