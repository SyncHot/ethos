#!/usr/bin/env python3
"""
EthOS Pre-Boot Server
Lightweight HTTP server on :9000 before EthOS is installed.

2-phase boot flow:
- Phase 1 (no network): Shows WiFi setup UI. After WiFi configured → reboot.
- Phase 2 (network available): Shows installation progress while firstboot installs.

Auto-exits when the real EthOS takes over port 9000.
"""

import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time

PORT = 9000
INSTALLED_MARKER = "/opt/ethos/.installed"
INSTALLER_MODE_MARKER = "/opt/ethos/.installer-mode"
LOG_FILE = "/var/log/ethos-firstboot.log"

# Detect mode: prepackaged has venv already built by the image builder
IS_PREPACKAGED = os.path.isfile("/opt/ethos/venv/bin/python")
# Installer-only USB: shows disk wizard instead of firstboot progress
IS_INSTALLER_MODE = os.path.isfile(INSTALLER_MODE_MARKER)


def has_network():
    """Check if system has internet connectivity."""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "3", "8.8.8.8"],
            capture_output=True, timeout=8
        )
        if r.returncode == 0:
            return True
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "3", "1.1.1.1"],
            capture_output=True, timeout=8
        )
        return r.returncode == 0
    except Exception:
        return False


def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", 1
    except Exception:
        return "", 1


def shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


# ── WiFi helpers ──

def wifi_scan():
    run("nmcli device wifi rescan 2>/dev/null", timeout=10)
    time.sleep(2)
    out, rc = run("nmcli -t -f SSID,SIGNAL,SECURITY,IN-USE device wifi list 2>/dev/null")
    seen = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        ssid = parts[0].strip()
        if not ssid:
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security = parts[2] if len(parts) > 2 else ""
        connected = "*" in parts[3] if len(parts) > 3 else False
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = {"ssid": ssid, "signal": signal, "security": security, "connected": connected}
    return sorted(seen.values(), key=lambda x: -x["signal"])


def wifi_connect(ssid, password):
    """Connect to WiFi. Returns (ok, message, new_ip)."""
    cmd = f"nmcli dev wifi connect {shq(ssid)} password {shq(password)} 2>&1"
    out, rc = run(cmd, timeout=30)
    if rc != 0:
        run(f"nmcli connection delete {shq(ssid)} 2>/dev/null")
        out, rc = run(cmd, timeout=30)
    if rc != 0:
        return False, out, ""
    new_ip = ""
    for _ in range(8):
        time.sleep(2)
        ip_out, _ = run("hostname -I 2>/dev/null")
        for part in ip_out.split():
            if part and not part.startswith("192.168.42.") and not part.startswith("169.254."):
                new_ip = part
                break
        if new_ip:
            break
    return True, "OK", new_ip


def wifi_status():
    out, _ = run("nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null")
    devices = []
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 4:
            devices.append({
                "device": parts[0], "type": parts[1],
                "state": parts[2], "connection": parts[3] if parts[3] != "--" else "",
            })
    _, rc = run("ping -c 1 -W 3 8.8.8.8 2>/dev/null", timeout=8)
    ip_out, _ = run("hostname -I 2>/dev/null")
    return {
        "devices": devices,
        "has_internet": rc == 0,
        "ip": ip_out.split()[0] if ip_out else "",
    }


def install_progress():
    """Parse firstboot log for progress estimate."""
    is_installed = os.path.isfile(INSTALLED_MARKER)
    progress = 0
    last_line = ""
    if os.path.isfile(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f:
                lines = f.readlines()
            if lines:
                last_line = lines[-1].strip()
            content = "".join(lines)
            if "First boot complete" in content or is_installed:
                progress = 100
            elif "EthOS uruchomiony" in content:
                progress = 95
            elif "Czekam na uruchomienie" in content:
                progress = 85
            elif "ethos.service" in content or "systemctl" in content:
                progress = 80
            elif "venv" in content or "pip install" in content:
                progress = 70
            elif "Przygotowuję EthOS" in content:
                progress = 60
            elif "pomijam instalację" in content or "Tryb prepackaged" in content:
                progress = 55
            elif "grupy" in content or "Konfiguruję" in content:
                progress = 50
            elif "Instaluję" in content:
                progress = 30
            elif "Sprawdzam zależności" in content:
                progress = 15
            elif "Sieć dostępna" in content:
                progress = 10
            elif "Sprawdzam sieć" in content:
                progress = 5
            else:
                progress = 2
        except Exception:
            pass
    ethos_ready = False
    if is_installed:
        _, rc = run("curl -sf http://localhost:9000/api/system/info 2>/dev/null", timeout=5)
        ethos_ready = rc == 0
    return {
        "installed": is_installed,
        "progress": progress,
        "last_line": last_line,
        "ready": ethos_ready,
    }


# ── Disk Installer (installer-mode USB) ──

_MIN_SYSTEM_DISK_GB = 16
_CONFIRMATION_TOKEN = "INSTALUJ"

_install_state = {
    "status": "idle",      # idle | running | done | error
    "phase": "",           # partitioning | copying | grub | data_disk | complete
    "percent": 0,
    "message": "",
    "error": "",
    "os_disk": "",
    "data_disk": "",
}


def _set_install(phase, percent, message):
    _install_state["phase"] = phase
    _install_state["percent"] = percent
    _install_state["message"] = message
    print(f"[installer] [{percent}%] {message}")


def has_wifi_device():
    """Check if system has a WiFi adapter."""
    out, _ = run("nmcli -t -f TYPE device status 2>/dev/null")
    return "wifi" in out


def _sp_run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _get_smart_status(dev_path):
    try:
        r = _sp_run(["smartctl", "-H", dev_path], timeout=10)
        if "PASSED" in r.stdout:
            return "ok"
        elif "FAILED" in r.stdout:
            return "fail"
        return "unknown"
    except Exception:
        return "unknown"


def _get_smart_temp(dev_path):
    try:
        r = _sp_run(["smartctl", "-A", dev_path], timeout=10)
        for line in r.stdout.splitlines():
            if "Temperature" in line and ("Celsius" in line or "Airflow" in line):
                for p in reversed(line.split()):
                    try:
                        t = int(p)
                        if 0 < t < 120:
                            return t
                    except ValueError:
                        continue
            m = re.search(r"Temperature:\s+(\d+)\s*Celsius", line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def discover_disks():
    """Find all block devices suitable for OS/data installation."""
    try:
        r = _sp_run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,TRAN,RO,RM"],
            timeout=10)
        data = json.loads(r.stdout) if r.returncode == 0 else {}
    except Exception:
        data = {}

    block_devs = data.get("blockdevices", [])

    # Find the disk that has / mounted (the USB boot source)
    boot_disk = ""
    try:
        r = _sp_run(["findmnt", "-no", "SOURCE", "/"], timeout=5)
        root_src = r.stdout.strip()
        boot_disk = re.sub(r"p?\d+$", "", root_src.replace("/dev/", ""))
    except Exception:
        pass

    devices = []
    for dev in block_devs:
        if dev.get("type") != "disk":
            continue
        if dev.get("ro"):
            continue
        name = dev.get("name", "")
        if name.startswith(("loop", "sr", "fd", "zram")):
            continue
        size = dev.get("size") or 0
        if size < 500_000_000:
            continue
        if name == boot_disk:
            continue   # Skip the USB we booted from

        tran = (dev.get("tran") or "").lower()
        removable = bool(dev.get("rm"))
        size_gb = round(size / 1e9, 1)
        model = (dev.get("model") or "").strip() or name

        # Connection type label
        if tran == "usb" or removable:
            conn = "USB"
        elif tran == "nvme":
            conn = "NVMe"
        elif tran in ("sata", "ata"):
            conn = "SATA"
        else:
            conn = tran.upper() if tran else "Internal"

        dev_path = f"/dev/{name}"
        smart = _get_smart_status(dev_path)
        temp = _get_smart_temp(dev_path)

        # Check existing partitions
        children = dev.get("children", [])
        parts_info = []
        for ch in children:
            if ch.get("type") in ("part", "crypt"):
                parts_info.append({
                    "name": ch.get("name", ""),
                    "size": ch.get("size") or 0,
                    "fstype": ch.get("fstype") or "",
                    "mountpoint": ch.get("mountpoint") or "",
                })

        devices.append({
            "name": name,
            "device": dev_path,
            "model": model,
            "size": size,
            "size_gb": size_gb,
            "transport": conn,
            "removable": removable,
            "smart": smart,
            "temp": temp,
            "eligible_os": size_gb >= _MIN_SYSTEM_DISK_GB,
            "eligible_data": size_gb >= 1,
            "partitions": parts_info,
        })

    devices.sort(key=lambda d: (d["removable"], -d["size"]))

    return {
        "devices": devices,
        "boot_disk": boot_disk,
    }


def _get_uuid(dev_path):
    try:
        r = _sp_run(["blkid", "-s", "UUID", "-o", "value", dev_path], timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


def install_worker(os_disk, data_disk):
    """Background thread: install EthOS from USB to target disk."""
    try:
        _install_state["status"] = "running"
        _install_state["os_disk"] = os_disk
        _install_state["data_disk"] = data_disk or ""
        _install_state["error"] = ""

        target = f"/dev/{os_disk}"

        # Safety: don't destroy current root
        r = _sp_run(["findmnt", "-no", "SOURCE", "/"], timeout=5)
        root_src = r.stdout.strip()
        root_disk = re.sub(r"p?\d+$", "", root_src)
        if target == root_disk:
            raise RuntimeError("Cannot install to the boot USB!")

        # 1. Unmount all partitions on target
        _set_install("partitioning", 2, f"Odmontowuję {target}...")
        r = _sp_run(["lsblk", "-nlo", "NAME,MOUNTPOINT", target], timeout=5)
        for line in r.stdout.strip().splitlines():
            parts = line.split(None, 1)
            if len(parts) >= 2 and parts[1].strip():
                _sp_run(["umount", "-f", parts[1].strip()], timeout=10)

        # 2. Wipe + partition (GPT: ESP + BIOS boot + rootfs)
        _set_install("partitioning", 5, f"Partycjonuję {target} (GPT)...")
        _sp_run(["wipefs", "-af", target], timeout=30)
        cmds = [
            ["parted", "-s", target, "mklabel", "gpt"],
            ["parted", "-s", target, "mkpart", "ESP", "fat32", "1MiB", "257MiB"],
            ["parted", "-s", target, "set", "1", "esp", "on"],
            ["parted", "-s", target, "mkpart", "primary", "257MiB", "258MiB"],
            ["parted", "-s", target, "set", "2", "bios_grub", "on"],
            ["parted", "-s", target, "mkpart", "primary", "ext4", "258MiB", "100%"],
        ]
        for cmd in cmds:
            r = _sp_run(cmd, timeout=15)
            if r.returncode != 0:
                raise RuntimeError(f"Partition error: {' '.join(cmd)} → {r.stderr.strip()}")

        _sp_run(["udevadm", "settle", "--timeout=10"], timeout=15)
        time.sleep(1)

        # Determine partition device names
        if "mmcblk" in target or "nvme" in target:
            p1, p3 = f"{target}p1", f"{target}p3"
        else:
            p1, p3 = f"{target}1", f"{target}3"

        if not os.path.exists(p1):
            _sp_run(["partprobe", target], timeout=10)
            _sp_run(["udevadm", "settle", "--timeout=5"], timeout=10)
            time.sleep(1)
        for pdev in (p1, p3):
            if not os.path.exists(pdev):
                raise RuntimeError(f"Partition {pdev} did not appear")

        # 3. Format
        _set_install("partitioning", 12, "Formatuję partycje...")
        r = _sp_run(["mkfs.vfat", "-F32", p1], timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"ESP format error: {r.stderr.strip()}")
        r = _sp_run(["mkfs.ext4", "-F", "-L", "ethos-root", p3], timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"Rootfs format error: {r.stderr.strip()}")

        # 4. Mount target
        _set_install("copying", 15, "Montuję partycje docelowe...")
        target_root = "/mnt/installer_target"
        os.makedirs(target_root, mode=0o755, exist_ok=True)
        r = _sp_run(["mount", p3, target_root], timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f"Mount rootfs error: {r.stderr.strip()}")

        efi_mount = os.path.join(target_root, "boot/efi")
        os.makedirs(efi_mount, mode=0o755, exist_ok=True)
        r = _sp_run(["mount", p1, efi_mount], timeout=15)
        if r.returncode != 0:
            raise RuntimeError(f"Mount ESP error: {r.stderr.strip()}")

        try:
            # 5. rsync from USB rootfs to target
            _set_install("copying", 18, "Kopiuję system (rsync)... to potrwa kilka minut")

            exclude_args = []
            for ex in ["/proc", "/sys", "/dev", "/run", "/tmp",
                       "/mnt", "/media", "/lost+found", "/boot/efi/*",
                       "/swap*", "/swapfile", target_root,
                       "/opt/ethos/.installer-mode"]:
                exclude_args.extend(["--exclude", ex])

            rsync_cmd = [
                "rsync", "-aAXH", "--info=progress2", "--no-inc-recursive",
            ] + exclude_args + ["/", target_root + "/"]

            proc = subprocess.Popen(
                rsync_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)

            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                m = re.search(r"(\d+)%", line.strip())
                if m:
                    rsync_pct = int(m.group(1))
                    pct = 18 + int(rsync_pct * 0.50)  # Map 0-100 → 18-68
                    _install_state["percent"] = pct
                    _install_state["message"] = f"Kopiowanie systemu... {rsync_pct}%"

            proc.wait(timeout=1800)
            if proc.returncode != 0:
                raise RuntimeError(f"rsync exited with code {proc.returncode}")

            # Ensure critical dirs exist
            for d in ["proc", "sys", "dev", "run", "tmp", "mnt", "media"]:
                os.makedirs(os.path.join(target_root, d), mode=0o755, exist_ok=True)

            # 6. Fix fstab
            _set_install("grub", 70, "Konfiguruję fstab...")
            root_uuid = _get_uuid(p3)
            efi_uuid = _get_uuid(p1)
            if not root_uuid:
                raise RuntimeError(f"Cannot read UUID for {p3}")

            fstab_lines = [
                "# EthOS System Disk (auto-generated by installer)",
                f"UUID={root_uuid}  /         ext4  defaults,noatime,errors=remount-ro  0  1",
            ]
            if efi_uuid:
                fstab_lines.append(f"UUID={efi_uuid}   /boot/efi vfat  umask=0077                 0  1")
            fstab_lines.extend([
                "tmpfs        /tmp      tmpfs defaults,noatime,nosuid      0  0",
                "",
            ])
            fstab_path = os.path.join(target_root, "etc/fstab")
            with open(fstab_path, "w") as f:
                f.write("\n".join(fstab_lines))

            # 7. Install GRUB (BIOS + UEFI)
            _set_install("grub", 74, "Instaluję bootloader (GRUB)...")

            # Bind-mount required filesystem trees
            for fs in ["proc", "sys", "dev", "run"]:
                src = f"/{fs}"
                dst = os.path.join(target_root, fs)
                os.makedirs(dst, exist_ok=True)
                _sp_run(["mount", "--bind", src, dst], timeout=10)

            try:
                # BIOS (i386-pc)
                r = _sp_run(
                    ["chroot", target_root, "grub-install",
                     "--target=i386-pc", "--boot-directory=/boot", target],
                    timeout=60)
                if r.returncode != 0:
                    print(f"[installer] GRUB BIOS warning: {r.stderr.strip()}")

                # UEFI (x86_64-efi)
                r = _sp_run(
                    ["chroot", target_root, "grub-install",
                     "--target=x86_64-efi", "--efi-directory=/boot/efi",
                     "--boot-directory=/boot", "--removable", "--no-nvram"],
                    timeout=60)
                if r.returncode != 0:
                    print(f"[installer] GRUB UEFI warning: {r.stderr.strip()}")

                # Generate grub.cfg
                r = _sp_run(["chroot", target_root, "update-grub"], timeout=60)
                if r.returncode != 0:
                    # Fallback: write minimal grub.cfg
                    kern = ""
                    initrd = ""
                    boot_dir = os.path.join(target_root, "boot")
                    for f in sorted(os.listdir(boot_dir)):
                        if f.startswith("vmlinuz-"):
                            kern = f"/boot/{f}"
                        elif f.startswith("initrd.img-"):
                            initrd = f"/boot/{f}"
                    grub_cfg = os.path.join(target_root, "boot/grub/grub.cfg")
                    os.makedirs(os.path.dirname(grub_cfg), exist_ok=True)
                    with open(grub_cfg, "w") as f:
                        f.write(f"""set timeout=3
set default=0
menuentry "EthOS" {{
    search --no-floppy --fs-uuid --set=root {root_uuid}
    linux {kern} root=UUID={root_uuid} ro quiet
    initrd {initrd}
}}
""")
            finally:
                for fs in reversed(["proc", "sys", "dev", "run"]):
                    _sp_run(["umount", "-l", os.path.join(target_root, fs)], timeout=10)

            # 8. Prepare data disk (if separate)
            if data_disk and data_disk != os_disk:
                _set_install("data_disk", 82, f"Przygotowuję dysk danych /dev/{data_disk}...")
                data_dev = f"/dev/{data_disk}"
                # Unmount
                r = _sp_run(["lsblk", "-nlo", "NAME,MOUNTPOINT", data_dev], timeout=5)
                for line in r.stdout.strip().splitlines():
                    parts = line.split(None, 1)
                    if len(parts) >= 2 and parts[1].strip():
                        _sp_run(["umount", "-f", parts[1].strip()], timeout=10)
                # Wipe + partition
                _sp_run(["wipefs", "-af", data_dev], timeout=30)
                _sp_run(["parted", "-s", data_dev, "mklabel", "gpt"], timeout=15)
                _sp_run(["parted", "-s", data_dev, "mkpart", "primary", "ext4", "1MiB", "100%"], timeout=15)
                _sp_run(["udevadm", "settle", "--timeout=10"], timeout=15)
                time.sleep(1)
                if "mmcblk" in data_dev or "nvme" in data_dev:
                    data_part = f"{data_dev}p1"
                else:
                    data_part = f"{data_dev}1"
                if not os.path.exists(data_part):
                    _sp_run(["partprobe", data_dev], timeout=10)
                    time.sleep(1)
                r = _sp_run(["mkfs.ext4", "-F", "-L", "EthOS-Data-1", data_part], timeout=300)
                if r.returncode != 0:
                    print(f"[installer] Data disk format warning: {r.stderr.strip()}")

                # Write data disk info to installer_result.json on target
                result_file = os.path.join(target_root, "opt/ethos/data/installer_result.json")
                os.makedirs(os.path.dirname(result_file), exist_ok=True)
                with open(result_file, "w") as f:
                    json.dump({
                        "version": 2,
                        "strategy": "internal",
                        "system_device": target,
                        "data_devices": [{"device": data_dev, "label": "EthOS-Data-1"}],
                    }, f, indent=2)

        finally:
            # Cleanup mounts
            _sp_run(["umount", "-R", target_root], timeout=30)

        _set_install("complete", 100, "Instalacja zakończona!")
        _install_state["status"] = "done"

    except Exception as e:
        _install_state["status"] = "error"
        _install_state["error"] = str(e)
        _install_state["message"] = f"Błąd: {e}"
        print(f"[installer] ERROR: {e}")

LOADING_HTML = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EthOS — Uruchamianie</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.wrap{width:100%;max-width:420px;padding:20px;text-align:center}
h1{font-size:28px;color:#38bdf8;margin-bottom:4px}
.sub{font-size:13px;color:#64748b;margin-bottom:32px}
.spinner{width:48px;height:48px;border:3px solid #334155;border-top-color:#38bdf8;border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 20px}
@keyframes spin{to{transform:rotate(360deg)}}
.status{font-size:14px;color:#94a3b8;margin-bottom:8px}
.detail{font-size:12px;color:#64748b;min-height:18px}
.bar-outer{height:6px;background:#1e293b;border-radius:3px;margin:16px 0;overflow:hidden}
.bar-inner{height:100%;background:linear-gradient(90deg,#38bdf8,#6366f1);border-radius:3px;transition:width .5s;width:0%}
.msg{margin-top:20px;padding:14px;background:#1e293b;border:1px solid #334155;border-radius:10px;font-size:13px;color:#94a3b8;line-height:1.6}
.done{color:#10b981}
a{color:#38bdf8;text-decoration:none;font-weight:600}
</style>
</head>
<body>
<div class="wrap">
  <h1>&#x1F3E0; EthOS</h1>
  <p class="sub">System NAS uruchamia się...</p>
  <div class="spinner" id="spinner"></div>
  <div class="status" id="status">Przygotowywanie systemu...</div>
  <div class="bar-outer"><div class="bar-inner" id="bar"></div></div>
  <div class="detail" id="detail"></div>
  <div class="msg" id="msg">
    Nie wyłączaj urządzenia. Strona odświeży się automatycznie.
  </div>
</div>
<script>
let checkInterval = setInterval(async () => {
  try {
    // Try the real EthOS endpoint
    const r = await fetch('/api/setup/status', {signal: AbortSignal.timeout(3000)});
    if (r.ok) {
      const d = await r.json();
      if (d.needs_setup !== undefined) {
        // Real EthOS is running — reload to get the wizard
        clearInterval(checkInterval);
        document.getElementById('spinner').style.display = 'none';
        document.getElementById('status').className = 'status done';
        document.getElementById('status').textContent = '\u2713 EthOS gotowy!';
        document.getElementById('bar').style.width = '100%';
        document.getElementById('msg').innerHTML = 'Przekierowanie...';
        setTimeout(() => window.location.reload(), 800);
        return;
      }
    }
  } catch(e) {}
  // Still loading — get progress from preboot
  try {
    const r2 = await fetch('/api/progress');
    const p = await r2.json();
    document.getElementById('bar').style.width = p.progress + '%';
    if (p.last_line) document.getElementById('detail').textContent = p.last_line;
    if (p.progress >= 100) {
      document.getElementById('status').textContent = 'Prawie gotowe...';
    }
  } catch(e) {}
}, 2000);
</script>
</body>
</html>"""


WIFI_SETUP_HTML = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EthOS — Konfiguracja WiFi</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.wrap{width:100%;max-width:480px;padding:20px}
h1{font-size:28px;color:#38bdf8;margin-bottom:4px;text-align:center}
.sub{font-size:13px;color:#64748b;margin-bottom:24px;text-align:center}
.card{background:#1e293b;border-radius:12px;padding:24px;margin-bottom:16px;border:1px solid #334155}
.card h2{font-size:16px;margin-bottom:16px;color:#f1f5f9}
.wifi-list{max-height:280px;overflow-y:auto;margin-bottom:12px}
.wifi-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;cursor:pointer;transition:background .2s;border:1px solid transparent}
.wifi-item:hover{background:#334155}
.wifi-item.selected{border-color:#38bdf8;background:rgba(56,189,248,.1)}
.wifi-name{flex:1;font-size:14px}
.wifi-signal{font-size:12px;color:#64748b}
.wifi-lock{font-size:11px;color:#f59e0b}
.inp{width:100%;background:#0f172a;border:1px solid #475569;border-radius:8px;padding:10px 14px;color:#e2e8f0;font-size:14px;margin-bottom:12px}
.inp:focus{outline:none;border-color:#38bdf8}
.btn{width:100%;background:#38bdf8;color:#0f172a;border:none;border-radius:8px;padding:12px;font-size:15px;font-weight:600;cursor:pointer;transition:filter .2s}
.btn:hover{filter:brightness(1.1)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-sm{padding:8px 16px;font-size:13px;width:auto}
.btn-outline{background:transparent;border:1px solid #475569;color:#94a3b8}
.msg{padding:10px;border-radius:8px;font-size:13px;margin-bottom:12px}
.msg-ok{background:rgba(16,185,129,.15);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.msg-err{background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)}
.msg-info{background:rgba(56,189,248,.1);color:#38bdf8;border:1px solid rgba(56,189,248,.2)}
.bar-outer{height:6px;background:#0f172a;border-radius:3px;margin:12px 0;overflow:hidden}
.bar-inner{height:100%;background:linear-gradient(90deg,#38bdf8,#6366f1);border-radius:3px;transition:width .5s;width:0%}
</style>
</head>
<body>
<div class="wrap">
  <h1>&#x1F3E0; EthOS</h1>
  <p class="sub">Konfiguracja początkowa NAS</p>

  <!-- Network status + WiFi -->
  <div class="card" id="card-net">
    <h2>&#x1F4F6; Połączenie z siecią</h2>
    <div id="net-status" class="msg msg-info">Sprawdzanie stanu sieci...</div>
    <div id="wifi-section" style="display:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:13px;color:#94a3b8">Dostępne sieci:</span>
        <button class="btn-outline btn-sm" onclick="scanWifi()">&#x21bb; Odśwież</button>
      </div>
      <div id="wifi-list" class="wifi-list">
        <div style="text-align:center;padding:20px;color:#64748b">Skanowanie...</div>
      </div>
      <input type="password" class="inp" id="wifi-pass" placeholder="Hasło WiFi" style="display:none">
      <div id="wifi-msg"></div>
      <button class="btn" id="btn-connect" onclick="connectWifi()" disabled>Połącz</button>
    </div>
  </div>

  <!-- Install progress -->
  <div class="card" id="card-progress" style="display:none">
    <h2>&#x23F3; Instalacja EthOS</h2>
    <p style="font-size:13px;color:#94a3b8;margin-bottom:12px">
      Trwa instalacja. Strona odświeży się automatycznie po zakończeniu.
    </p>
    <div class="bar-outer"><div class="bar-inner" id="bar"></div></div>
    <div id="progress-detail" style="font-size:12px;color:#64748b;min-height:18px;text-align:center"></div>
  </div>

  <!-- Done -->
  <div class="card" id="card-done" style="display:none;text-align:center">
    <div style="font-size:48px;margin-bottom:8px">&#x1F389;</div>
    <h2>EthOS gotowy!</h2>
    <p id="done-msg" style="font-size:13px;color:#94a3b8;margin-top:8px"></p>
    <a id="done-link" href="" class="btn" style="display:inline-block;text-decoration:none;margin-top:12px">Otwórz EthOS &#x2192;</a>
  </div>
</div>

<script>
let selectedSSID = '';
let progressInterval = null;

async function checkNetwork() {
  try {
    const r = await fetch('/api/wifi/status');
    const d = await r.json();
    const el = document.getElementById('net-status');
    const wifiEl = document.getElementById('wifi-section');
    const wifiDev = d.devices.find(x => x.type === 'wifi');

    if (d.has_internet) {
      el.className = 'msg msg-ok';
      el.innerHTML = '\u2713 Połączenie z internetem aktywne (IP: ' + esc(d.ip) + ')';
      wifiEl.style.display = 'none';
      startProgressWatch();
    } else if (wifiDev) {
      el.className = 'msg msg-info';
      el.textContent = 'Brak internetu \u2014 skonfiguruj WiFi poniżej';
      wifiEl.style.display = '';
      scanWifi();
    } else {
      el.className = 'msg msg-info';
      el.textContent = 'Brak połączenia \u2014 podłącz kabel sieciowy';
      wifiEl.style.display = 'none';
    }
  } catch(e) {
    document.getElementById('net-status').className = 'msg msg-err';
    document.getElementById('net-status').textContent = 'Błąd sprawdzania sieci';
  }
}

async function scanWifi() {
  const list = document.getElementById('wifi-list');
  list.innerHTML = '<div style="text-align:center;padding:20px;color:#64748b">Skanowanie...</div>';
  try {
    const r = await fetch('/api/wifi/scan');
    const d = await r.json();
    if (!d.networks || !d.networks.length) {
      list.innerHTML = '<div style="text-align:center;padding:20px;color:#64748b">Nie znaleziono sieci</div>';
      return;
    }
    list.innerHTML = '';
    for (const n of d.networks) {
      const div = document.createElement('div');
      div.className = 'wifi-item' + (n.ssid === selectedSSID ? ' selected' : '');
      const bars = n.signal > 70 ? '\u2582\u2585\u2587\u2588' : n.signal > 40 ? '\u2582\u2585\u2587\u2591' : '\u2582\u2585\u2591\u2591';
      const lock = n.security ? '\uD83D\uDD12' : '';
      div.innerHTML = '<span>' + bars + '</span><span class="wifi-name">' + esc(n.ssid) + '</span>'
        + '<span class="wifi-lock">' + lock + '</span><span class="wifi-signal">' + n.signal + '%</span>';
      div.onclick = () => selectWifi(n.ssid, div);
      list.appendChild(div);
    }
  } catch(e) {
    list.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444">Błąd skanowania</div>';
  }
}

function selectWifi(ssid, el) {
  selectedSSID = ssid;
  document.querySelectorAll('.wifi-item').forEach(i => i.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('wifi-pass').style.display = '';
  document.getElementById('wifi-pass').focus();
  document.getElementById('btn-connect').disabled = false;
}

async function connectWifi() {
  if (!selectedSSID) return;
  const pass = document.getElementById('wifi-pass').value;
  const msg = document.getElementById('wifi-msg');
  const btn = document.getElementById('btn-connect');
  btn.disabled = true;
  btn.textContent = 'Łączenie...';
  msg.innerHTML = '<div class="msg msg-info">Łączenie z ' + esc(selectedSSID) + '...</div>';

  try {
    const r = await fetch('/api/wifi/connect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ssid: selectedSSID, password: pass}),
    });
    const d = await r.json();
    if (d.ok) {
      msg.innerHTML = '';
      document.getElementById('card-net').innerHTML =
        '<div style="text-align:center;padding:24px;">'
        + '<div style="font-size:48px;margin-bottom:12px">&#x1F504;</div>'
        + '<div style="font-size:18px;font-weight:600;color:#10b981;margin-bottom:12px">WiFi skonfigurowane!</div>'
        + '<div style="font-size:14px;color:#94a3b8;line-height:1.8;margin-bottom:16px">'
        + 'System zostanie automatycznie zrestartowany.<br>Po restarcie:'
        + '</div>'
        + '<div style="text-align:left;font-size:14px;color:#e2e8f0;line-height:2;margin-bottom:16px;padding-left:20px">'
        + '1. Połącz się z siecią <b>' + esc(selectedSSID) + '</b><br>'
        + '2. Otwórz <b style="color:#38bdf8">http://ethos.local:9000</b>'
        + '</div>'
        + '<div style="font-size:13px;color:#64748b">'
        + 'Konfiguracja zajmie ok. 2 minuty.'
        + '</div>'
        + '</div>';
    } else {
      msg.innerHTML = '<div class="msg msg-err">\u2717 ' + esc(d.error || 'Błąd') + '</div>';
      btn.disabled = false;
      btn.textContent = 'Połącz';
    }
  } catch(e) {
    msg.innerHTML = '<div class="msg msg-err">Błąd: ' + esc(e.message) + '</div>';
    btn.disabled = false;
    btn.textContent = 'Połącz';
  }
}

function startProgressWatch() {
  document.getElementById('card-progress').style.display = '';
  if (progressInterval) return;
  progressInterval = setInterval(async () => {
    try {
      const r = await fetch('/api/setup/status', {signal: AbortSignal.timeout(3000)});
      if (r.ok) {
        const d = await r.json();
        if (d.needs_setup !== undefined) {
          clearInterval(progressInterval);
          showDone();
          return;
        }
      }
    } catch(e) {}
    try {
      const r2 = await fetch('/api/progress');
      const p = await r2.json();
      document.getElementById('bar').style.width = p.progress + '%';
      if (p.last_line) document.getElementById('progress-detail').textContent = p.last_line;
    } catch(e) {}
  }, 3000);
}

function showDone() {
  document.getElementById('card-progress').style.display = 'none';
  const done = document.getElementById('card-done');
  done.style.display = '';
  fetch('/api/wifi/status').then(r => r.json()).then(d => {
    const ip = d.ip || '192.168.42.1';
    const link = 'http://' + ip + ':9000';
    document.getElementById('done-link').href = link;
    document.getElementById('done-msg').innerHTML = 'Panel: <b>' + link + '</b>';
  }).catch(() => {});
  setTimeout(() => window.location.reload(), 3000);
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// Periodic network re-check: if WiFi auto-connects (boot 2) or user
// configured WiFi without reboot, transition to progress view.
let netCheckInterval = setInterval(async () => {
  try {
    const r = await fetch('/api/wifi/status');
    const d = await r.json();
    if (d.has_internet) {
      clearInterval(netCheckInterval);
      document.getElementById('net-status').className = 'msg msg-ok';
      document.getElementById('net-status').innerHTML = '\u2713 Połączenie z internetem aktywne (IP: ' + esc(d.ip) + ')';
      document.getElementById('wifi-section').style.display = 'none';
      startProgressWatch();
    }
  } catch(e) {}
}, 5000);

// Init
checkNetwork();
fetch('/api/progress').then(r => r.json()).then(p => {
  if (p.ready) showDone();
  else if (p.progress > 10) startProgressWatch();
}).catch(() => {});
</script>
</body>
</html>"""


INSTALLER_HTML = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EthOS — Instalator</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.wrap{width:100%;max-width:560px;padding:20px}
h1{font-size:28px;color:#38bdf8;margin-bottom:4px;text-align:center}
.sub{font-size:13px;color:#64748b;margin-bottom:24px;text-align:center}
.card{background:#1e293b;border-radius:12px;padding:24px;margin-bottom:16px;border:1px solid #334155}
.card h2{font-size:16px;margin-bottom:16px;color:#f1f5f9}
.disk-list{max-height:320px;overflow-y:auto;margin-bottom:12px}
.disk-item{display:flex;align-items:center;gap:12px;padding:12px;border-radius:8px;cursor:pointer;border:1px solid transparent;transition:all .2s;margin-bottom:6px}
.disk-item:hover{background:#334155}
.disk-item.selected{border-color:#38bdf8;background:rgba(56,189,248,.1)}
.disk-item.disabled{opacity:.4;cursor:not-allowed}
.disk-radio{width:18px;height:18px;border:2px solid #475569;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center}
.disk-item.selected .disk-radio{border-color:#38bdf8}
.disk-item.selected .disk-radio::after{content:'';width:10px;height:10px;background:#38bdf8;border-radius:50%}
.disk-info{flex:1;min-width:0}
.disk-model{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.disk-meta{font-size:12px;color:#94a3b8;margin-top:2px}
.disk-badges{display:flex;gap:6px;margin-top:4px;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 8px;border-radius:4px;font-weight:500}
.badge-ok{background:rgba(16,185,129,.15);color:#10b981}
.badge-warn{background:rgba(245,158,11,.15);color:#f59e0b}
.badge-fail{background:rgba(239,68,68,.15);color:#ef4444}
.badge-type{background:rgba(99,102,241,.15);color:#818cf8}
.select-wrap{position:relative}
.select-wrap select{width:100%;background:#0f172a;border:1px solid #475569;border-radius:8px;padding:10px 14px;color:#e2e8f0;font-size:14px;appearance:none;cursor:pointer}
.select-wrap select:focus{outline:none;border-color:#38bdf8}
.select-wrap::after{content:'\25BC';position:absolute;right:14px;top:50%;transform:translateY(-50%);font-size:10px;color:#64748b;pointer-events:none}
.inp{width:100%;background:#0f172a;border:1px solid #475569;border-radius:8px;padding:10px 14px;color:#e2e8f0;font-size:14px;margin-bottom:12px;text-align:center;letter-spacing:2px;font-weight:600}
.inp:focus{outline:none;border-color:#38bdf8}
.btn{width:100%;background:#38bdf8;color:#0f172a;border:none;border-radius:8px;padding:12px;font-size:15px;font-weight:600;cursor:pointer;transition:filter .2s}
.btn:hover{filter:brightness(1.1)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-danger{background:#ef4444;color:#fff}
.btn-outline{background:transparent;border:1px solid #475569;color:#94a3b8;width:auto;padding:8px 16px;font-size:13px}
.msg{padding:10px;border-radius:8px;font-size:13px;margin-bottom:12px}
.msg-warn{background:rgba(245,158,11,.1);color:#f59e0b;border:1px solid rgba(245,158,11,.2)}
.msg-err{background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)}
.msg-ok{background:rgba(16,185,129,.15);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.msg-info{background:rgba(56,189,248,.1);color:#38bdf8;border:1px solid rgba(56,189,248,.2)}
.summary{font-size:13px;color:#94a3b8;line-height:1.8;margin-bottom:16px}
.summary b{color:#e2e8f0}
.bar-outer{height:8px;background:#0f172a;border-radius:4px;margin:16px 0;overflow:hidden}
.bar-inner{height:100%;background:linear-gradient(90deg,#38bdf8,#6366f1);border-radius:4px;transition:width .5s;width:0%}
.steps{display:flex;gap:8px;margin-bottom:24px;justify-content:center}
.step{width:10px;height:10px;border-radius:50%;background:#334155;transition:background .3s}
.step.active{background:#38bdf8}
.step.done{background:#10b981}
.hidden{display:none}
.hint{font-size:12px;color:#64748b;margin-top:6px}
label{font-size:13px;color:#94a3b8;display:block;margin-bottom:8px}
</style>
</head>
<body>
<div class="wrap">
  <h1>&#x1F4BF; EthOS Installer</h1>
  <p class="sub">Zainstaluj system na wybranym dysku</p>
  <div class="steps">
    <div class="step active" id="s1"></div>
    <div class="step" id="s2"></div>
    <div class="step" id="s3"></div>
    <div class="step" id="s4"></div>
  </div>

  <!-- Step 1: Disk Selection -->
  <div id="step-disks">
    <div class="card">
      <h2>&#x1F4BD; Wybierz dysk systemowy</h2>
      <div id="disk-loading" style="text-align:center;padding:20px;color:#64748b">
        Wykrywanie dysków...
      </div>
      <div id="disk-list" class="disk-list hidden"></div>
      <div id="disk-empty" class="msg msg-warn hidden">
        Nie znaleziono dysków. Podłącz dysk i odśwież.
      </div>
      <button class="btn-outline" onclick="loadDisks()" style="margin-top:8px">&#x21bb; Odśwież</button>
    </div>

    <div class="card">
      <h2>&#x1F4BE; Dysk danych</h2>
      <p class="hint" style="margin-bottom:12px">Gdzie przechowywać pliki użytkowników (zdjęcia, dokumenty, kopie zapasowe).</p>
      <div class="select-wrap">
        <select id="data-select">
          <option value="">Ten sam co systemowy</option>
        </select>
      </div>
      <p class="hint">Oddzielny dysk danych ułatwia reinstalację systemu bez utraty plików.</p>
    </div>

    <div id="disk-warn" class="msg msg-warn hidden">
      &#x26A0; Wszystkie dane na wybranych dyskach zostaną <b>nieodwracalnie usunięte</b>!
    </div>

    <button class="btn" id="btn-next" onclick="goToConfirm()" disabled>Dalej &#x2192;</button>
  </div>

  <!-- Step 2: Confirmation -->
  <div id="step-confirm" class="hidden">
    <div class="card">
      <h2>&#x1F4CB; Plan instalacji</h2>
      <div id="plan-summary" class="summary"></div>
      <div class="msg msg-warn">
        &#x26A0; Ta operacja jest <b>nieodwracalna</b>. Upewnij się, że wybrałeś właściwe dyski.
      </div>
    </div>
    <div class="card">
      <label>Wpisz <b style="color:#ef4444">INSTALUJ</b> aby potwierdzić:</label>
      <input type="text" class="inp" id="confirm-input" placeholder="INSTALUJ" autocomplete="off" spellcheck="false" oninput="checkConfirm()">
      <button class="btn btn-danger" id="btn-install" onclick="startInstall()" disabled>&#x1F4BF; Zainstaluj EthOS</button>
    </div>
    <button class="btn-outline" onclick="goToDisks()" style="margin-top:8px">&#x2190; Wróć</button>
  </div>

  <!-- Step 3: Progress -->
  <div id="step-progress" class="hidden">
    <div class="card" style="text-align:center">
      <h2>&#x23F3; Instalacja EthOS</h2>
      <p style="font-size:13px;color:#94a3b8;margin-bottom:16px">Nie wyłączaj komputera ani nie odłączaj dysków.</p>
      <div class="bar-outer"><div class="bar-inner" id="prog-bar"></div></div>
      <div id="prog-pct" style="font-size:24px;font-weight:700;color:#38bdf8;margin:8px 0">0%</div>
      <div id="prog-msg" style="font-size:13px;color:#94a3b8;min-height:20px"></div>
    </div>
  </div>

  <!-- Step 4: Done -->
  <div id="step-done" class="hidden">
    <div class="card" style="text-align:center">
      <div style="font-size:56px;margin-bottom:12px">&#x1F389;</div>
      <h2 style="color:#10b981;font-size:20px">Instalacja zakończona!</h2>
      <div class="summary" style="margin-top:16px;text-align:left">
        <b>Następne kroki:</b><br>
        1. Wyjmij nośnik USB z komputera<br>
        2. Kliknij "Uruchom ponownie"<br>
        3. System uruchomi się z dysku <span id="done-disk"></span><br>
        4. Kreator pomoże Ci skonfigurować konto i sieć
      </div>
      <button class="btn" onclick="doReboot()" style="margin-top:12px">&#x1F504; Uruchom ponownie</button>
    </div>
  </div>

  <!-- Step: Error -->
  <div id="step-error" class="hidden">
    <div class="card" style="text-align:center">
      <div style="font-size:56px;margin-bottom:12px">&#x274C;</div>
      <h2 style="color:#ef4444">Błąd instalacji</h2>
      <div id="error-msg" class="msg msg-err" style="margin-top:12px;text-align:left"></div>
      <button class="btn-outline" onclick="location.reload()" style="margin-top:16px">Spróbuj ponownie</button>
    </div>
  </div>
</div>

<script>
let allDisks = [];
let selectedOS = '';
let progressInterval = null;

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }

function setStep(n) {
  for (let i = 1; i <= 4; i++) {
    const el = document.getElementById('s' + i);
    el.className = 'step' + (i < n ? ' done' : i === n ? ' active' : '');
  }
}

async function loadDisks() {
  show('disk-loading'); hide('disk-list'); hide('disk-empty');
  try {
    const r = await fetch('/api/disks');
    const d = await r.json();
    allDisks = d.devices || [];
    renderDisks();
  } catch(e) {
    document.getElementById('disk-loading').textContent = 'Błąd wykrywania dysków';
  }
}

function renderDisks() {
  hide('disk-loading');
  const list = document.getElementById('disk-list');
  const sel = document.getElementById('data-select');

  if (!allDisks.length) {
    show('disk-empty');
    return;
  }
  show('disk-list');

  list.innerHTML = '';
  // Rebuild data select (keep "same as OS" default)
  sel.innerHTML = '<option value="">Ten sam co systemowy</option>';

  for (const disk of allDisks) {
    // OS disk item
    const div = document.createElement('div');
    const eligible = disk.eligible_os;
    div.className = 'disk-item' + (selectedOS === disk.name ? ' selected' : '') + (!eligible ? ' disabled' : '');
    const smartBadge = disk.smart === 'ok' ? '<span class="badge badge-ok">SMART OK</span>'
      : disk.smart === 'fail' ? '<span class="badge badge-fail">SMART FAIL</span>'
      : '';
    const tempBadge = disk.temp ? '<span class="badge badge-type">' + disk.temp + '°C</span>' : '';
    const sizeTxt = disk.size_gb >= 1000 ? (disk.size_gb / 1000).toFixed(1) + ' TB' : disk.size_gb + ' GB';
    div.innerHTML = '<div class="disk-radio"></div><div class="disk-info">'
      + '<div class="disk-model">' + esc(disk.model) + '</div>'
      + '<div class="disk-meta">' + esc(disk.device) + ' &mdash; ' + sizeTxt + ' &mdash; ' + esc(disk.transport) + '</div>'
      + '<div class="disk-badges">' + smartBadge + tempBadge
      + (!eligible ? '<span class="badge badge-warn">Za mały (min. 16 GB)</span>' : '')
      + '</div></div>';
    if (eligible) {
      div.onclick = () => selectOS(disk.name, div);
    }
    list.appendChild(div);

    // Add to data select dropdown (if eligible for data and not selected as OS)
    if (disk.eligible_data) {
      const opt = document.createElement('option');
      opt.value = disk.name;
      opt.textContent = disk.model + ' (' + sizeTxt + ')';
      sel.appendChild(opt);
    }
  }
}

function selectOS(name, el) {
  selectedOS = name;
  document.querySelectorAll('.disk-item').forEach(i => i.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('btn-next').disabled = false;
  show('disk-warn');

  // If data select has same disk selected, reset to "same as OS"
  const dataSel = document.getElementById('data-select');
  if (dataSel.value === name) dataSel.value = '';
}

function goToConfirm() {
  if (!selectedOS) return;
  const osDisk = allDisks.find(d => d.name === selectedOS);
  const dataVal = document.getElementById('data-select').value;
  const dataDisk = dataVal ? allDisks.find(d => d.name === dataVal) : null;
  const sameData = !dataVal || dataVal === selectedOS;

  const osSizeTxt = osDisk.size_gb >= 1000 ? (osDisk.size_gb / 1000).toFixed(1) + ' TB' : osDisk.size_gb + ' GB';

  let html = '<b>Dysk systemowy:</b> ' + esc(osDisk.model) + ' (' + osSizeTxt + ')<br>';
  html += 'Partycje: EFI (256 MB) + BIOS (1 MB) + rootfs (reszta)<br><br>';

  if (sameData) {
    html += '<b>Dane:</b> Na dysku systemowym<br>';
  } else {
    const dSizeTxt = dataDisk.size_gb >= 1000 ? (dataDisk.size_gb / 1000).toFixed(1) + ' TB' : dataDisk.size_gb + ' GB';
    html += '<b>Dysk danych:</b> ' + esc(dataDisk.model) + ' (' + dSizeTxt + ')<br>';
    html += 'Partycja: ext4 (EthOS-Data-1)<br><br>';
    html += '<span style="color:#ef4444">UWAGA: Oba dyski zostaną całkowicie wyczyszczone!</span>';
  }

  document.getElementById('plan-summary').innerHTML = html;

  hide('step-disks'); show('step-confirm'); setStep(2);
  document.getElementById('confirm-input').value = '';
  document.getElementById('btn-install').disabled = true;
  document.getElementById('confirm-input').focus();
}

function goToDisks() {
  hide('step-confirm'); show('step-disks'); setStep(1);
}

function checkConfirm() {
  const v = document.getElementById('confirm-input').value.trim();
  document.getElementById('btn-install').disabled = (v !== 'INSTALUJ');
}

async function startInstall() {
  const dataVal = document.getElementById('data-select').value;
  const btn = document.getElementById('btn-install');
  btn.disabled = true;
  btn.textContent = 'Rozpoczynam...';

  try {
    const r = await fetch('/api/install/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        os_disk: selectedOS,
        data_disk: (dataVal && dataVal !== selectedOS) ? dataVal : '',
        confirmation: document.getElementById('confirm-input').value.trim(),
      }),
    });
    const d = await r.json();
    if (!r.ok || d.error) {
      alert(d.error || 'Błąd');
      btn.disabled = false;
      btn.textContent = '\uD83D\uDCBF Zainstaluj EthOS';
      return;
    }
    // Switch to progress view
    hide('step-confirm'); show('step-progress'); setStep(3);
    startProgressWatch();
  } catch(e) {
    alert('Błąd: ' + e.message);
    btn.disabled = false;
    btn.textContent = '\uD83D\uDCBF Zainstaluj EthOS';
  }
}

function startProgressWatch() {
  if (progressInterval) clearInterval(progressInterval);
  progressInterval = setInterval(async () => {
    try {
      const r = await fetch('/api/install/progress');
      const d = await r.json();
      document.getElementById('prog-bar').style.width = d.percent + '%';
      document.getElementById('prog-pct').textContent = d.percent + '%';
      document.getElementById('prog-msg').textContent = d.message || '';

      if (d.status === 'done') {
        clearInterval(progressInterval);
        hide('step-progress'); show('step-done'); setStep(4);
        const osDisk = allDisks.find(x => x.name === selectedOS);
        document.getElementById('done-disk').textContent = osDisk ? osDisk.model : selectedOS;
      } else if (d.status === 'error') {
        clearInterval(progressInterval);
        hide('step-progress'); show('step-error');
        document.getElementById('error-msg').textContent = d.error || d.message || 'Nieznany błąd';
      }
    } catch(e) {}
  }, 1500);
}

async function doReboot() {
  try { await fetch('/api/reboot', {method: 'POST'}); } catch(e) {}
  document.querySelector('#step-done .card h2').textContent = 'Restartowanie...';
}

// Init: load disks
loadDisks();
</script>
</body>
</html>"""


# ── HTTP Handler ──

class PrebootHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content):
        body = content.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            if IS_INSTALLER_MODE:
                # Installer USB: show disk wizard or WiFi setup first
                if has_network() or not has_wifi_device():
                    self._html(INSTALLER_HTML)
                else:
                    self._html(WIFI_SETUP_HTML)
            else:
                # Legacy live-USB mode: show progress or WiFi
                if has_network():
                    self._html(LOADING_HTML)
                else:
                    self._html(WIFI_SETUP_HTML)

        elif path == "/api/wifi/scan":
            self._json({"networks": wifi_scan()})

        elif path == "/api/wifi/status":
            self._json(wifi_status())

        elif path == "/api/progress":
            self._json(install_progress())

        elif path == "/api/disks":
            self._json(discover_disks())

        elif path == "/api/install/progress":
            self._json({
                "status": _install_state["status"],
                "phase": _install_state["phase"],
                "percent": _install_state["percent"],
                "message": _install_state["message"],
                "error": _install_state["error"],
            })

        elif path == "/api/setup/status":
            # Return 404 so the JS can distinguish preboot from real EthOS
            self.send_error(404)

        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/wifi/connect":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            ssid = data.get("ssid", "")
            password = data.get("password", "")
            if not ssid:
                self._json({"ok": False, "error": "Brak SSID"}, 400)
                return
            # Send response first, then connect (hotspot drops on WiFi connect)
            self._json({"ok": True, "message": "Łączenie...", "new_ip": ""})
            threading.Thread(
                target=self._deferred_wifi, args=(ssid, password), daemon=True
            ).start()

        elif path == "/api/wifi/scan":
            self._json({"networks": wifi_scan()})

        elif path == "/api/install/start":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            os_disk = data.get("os_disk", "")
            data_disk = data.get("data_disk", "")
            confirmation = data.get("confirmation", "")

            if not os_disk:
                self._json({"error": "Nie wybrano dysku systemowego"}, 400)
                return
            if confirmation != _CONFIRMATION_TOKEN:
                self._json({"error": f'Wpisz "{_CONFIRMATION_TOKEN}" aby potwierdzić'}, 400)
                return
            if _install_state["status"] == "running":
                self._json({"error": "Instalacja już trwa"}, 409)
                return
            # Validate disk exists
            disc = discover_disks()
            valid_names = {d["name"] for d in disc["devices"]}
            if os_disk not in valid_names:
                self._json({"error": f"Dysk {os_disk} nie znaleziony"}, 400)
                return
            if data_disk and data_disk not in valid_names:
                self._json({"error": f"Dysk danych {data_disk} nie znaleziony"}, 400)
                return
            if data_disk == os_disk:
                data_disk = ""  # Same as OS = no separate data disk

            # Reset state and start worker
            _install_state.update({
                "status": "running", "phase": "", "percent": 0,
                "message": "Rozpoczynam...", "error": "",
                "os_disk": os_disk, "data_disk": data_disk,
            })
            threading.Thread(
                target=install_worker, args=(os_disk, data_disk), daemon=True
            ).start()
            self._json({"ok": True})

        elif path == "/api/reboot":
            self._json({"ok": True, "message": "Restarting..."})
            threading.Thread(target=self._do_reboot, daemon=True).start()

        else:
            self.send_error(404)

    def _deferred_wifi(self, ssid, password):
        """Connect to WiFi after a short delay so HTTP response reaches client.
        In installer mode: don't reboot (installer page will reload).
        In legacy mode: reboot so firstboot can run with network."""
        time.sleep(3)
        ok, msg, new_ip = wifi_connect(ssid, password)
        if ok:
            print(f"[preboot] WiFi connected: {ssid}, IP: {new_ip}")
            run("nmcli connection delete ethos-hotspot 2>/dev/null", timeout=5)
            if IS_INSTALLER_MODE:
                # In installer mode, don't reboot — user proceeds to disk wizard
                print(f"[preboot] Installer mode — skipping reboot, new IP: {new_ip}")
            else:
                # Legacy mode: reboot so firstboot runs with network
                print(f"[preboot] Syncing and rebooting...")
                try:
                    subprocess.run(["sync"], timeout=10)
                    subprocess.run(["sync"], timeout=10)
                except Exception:
                    pass
                time.sleep(5)
                self._do_reboot()
        else:
            print(f"[preboot] WiFi failed: {msg}")

    @staticmethod
    def _do_reboot():
        """Reboot the system."""
        try:
            subprocess.run(["sync"], timeout=10)
        except Exception:
            pass
        time.sleep(3)
        try:
            subprocess.Popen(
                ["systemctl", "reboot"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            try:
                subprocess.Popen(
                    ["/sbin/reboot"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                print(f"[preboot] Reboot failed: {e}")


# ── Auto-exit watcher ──

def watch_for_ethos():
    """Exit when real EthOS is running on :9000."""
    while True:
        time.sleep(15)
        try:
            out, rc = run("curl -sf http://localhost:9000/api/system/info 2>/dev/null", timeout=5)
            if rc == 0 and "version" in out.lower():
                print("[preboot] EthOS detected, exiting in 5s...")
                time.sleep(5)
                os._exit(0)
        except Exception:
            pass


def main():
    print(f"[preboot] Pre-Boot Server starting on :{PORT}")
    mode_label = "INSTALLER USB" if IS_INSTALLER_MODE else ("prepackaged" if IS_PREPACKAGED else "installer")
    print(f"[preboot] Mode: {mode_label}")

    # Already running? Exit immediately.
    out, rc = run("curl -sf http://localhost:9000/api/system/info 2>/dev/null", timeout=5)
    if rc == 0 and out:
        print("[preboot] EthOS already running, exiting.")
        sys.exit(0)

    threading.Thread(target=watch_for_ethos, daemon=True).start()
    server = http.server.HTTPServer(("0.0.0.0", PORT), PrebootHandler)
    print(f"[preboot] Listening on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[preboot] Stopped.")


if __name__ == "__main__":
    main()
