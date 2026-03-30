"""
WiFi operations — thin wrapper around nmcli.
"""

import os
import re
import subprocess
import time
import logging

log = logging.getLogger("ethos-installer")


def _run(cmd, timeout=20):
    """Run shell command, return (stdout, returncode)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", 1
    except Exception as e:
        log.warning("cmd failed: %s — %s", cmd, e)
        return "", 1


def _shq(s):
    """Shell-quote a string."""
    return "'" + s.replace("'", "'\\''") + "'"


def has_wifi_device():
    """Check if system has a WiFi adapter."""
    out, rc = _run("nmcli -t -f TYPE device status 2>/dev/null")
    return "wifi" in out


def has_ethernet():
    """Check if any ethernet interface has an IP."""
    out, _ = _run("nmcli -t -f TYPE,STATE device status 2>/dev/null")
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] == "ethernet" and parts[1] == "connected":
            return True
    return False


def get_ethernet_ip():
    """Return IP of connected ethernet, or None."""
    out, _ = _run(
        "nmcli -t -f TYPE,IP4.ADDRESS device show 2>/dev/null | "
        "grep -A1 '^ethernet' | grep IP4 | head -1"
    )
    # Try simpler approach
    out2, _ = _run(
        "ip -4 -o addr show scope global | grep -v 'wl' | "
        "grep -v '192.168.42' | awk '{print $4}' | cut -d/ -f1 | head -1"
    )
    return out2 if out2 else None


def scan():
    """Scan WiFi networks. Returns list of dicts."""
    _run("nmcli device wifi rescan 2>/dev/null", timeout=10)
    time.sleep(2)
    out, rc = _run(
        "nmcli -t -f SSID,SIGNAL,SECURITY,IN-USE device wifi list 2>/dev/null"
    )
    if rc != 0:
        return []
    seen = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        ssid = parts[0].strip()
        if not ssid or ssid == "--":
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security = parts[2] if len(parts) > 2 else ""
        connected = "*" in (parts[3] if len(parts) > 3 else "")
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = {
                "ssid": ssid,
                "signal": signal,
                "security": security,
                "connected": connected,
            }
    return sorted(seen.values(), key=lambda x: -x["signal"])


def connect(ssid, password):
    """Connect to WiFi network. Returns (ok, message, ip)."""
    log.info("WiFi connect: %s", ssid)
    # Delete old connection if exists
    _run(f"nmcli connection delete {_shq(ssid)} 2>/dev/null")
    time.sleep(1)

    cmd = f"nmcli dev wifi connect {_shq(ssid)} password {_shq(password)} 2>&1"
    out, rc = _run(cmd, timeout=30)
    if rc != 0:
        log.warning("WiFi connect failed: %s", out)
        return False, out or "Connection failed", ""

    # Wait for IP
    ip = ""
    for _ in range(10):
        time.sleep(2)
        ip = _get_wifi_ip()
        if ip:
            break

    if ip:
        log.info("WiFi connected: %s → %s", ssid, ip)
        return True, f"Connected to {ssid}", ip
    else:
        log.warning("WiFi connected but no IP")
        return True, f"Connected to {ssid} (no IP yet)", ""


def _get_wifi_ip():
    """Return IP of WiFi interface (excluding hotspot)."""
    out, _ = _run(
        "ip -4 -o addr show scope global | grep 'wl' | "
        "grep -v '192.168.42' | awk '{print $4}' | cut -d/ -f1 | head -1"
    )
    return out if out else None


def status():
    """Return current network status dict."""
    eth = has_ethernet()
    eth_ip = get_ethernet_ip() if eth else None
    wifi_dev = has_wifi_device()

    # Check if WiFi is connected (not hotspot)
    wifi_ssid = None
    wifi_ip = None
    if wifi_dev:
        out, _ = _run(
            "nmcli -t -f NAME,TYPE,DEVICE connection show --active 2>/dev/null"
        )
        for line in out.splitlines():
            parts = line.split(":")
            if (
                len(parts) >= 2
                and "wireless" in parts[1]
                and "ethos-hotspot" not in parts[0]
            ):
                wifi_ssid = parts[0]
                wifi_ip = _get_wifi_ip()
                break

    return {
        "ethernet": eth,
        "ethernet_ip": eth_ip,
        "has_wifi": wifi_dev,
        "wifi_ssid": wifi_ssid,
        "wifi_ip": wifi_ip,
        "has_network": eth or (wifi_ip is not None),
    }


def save_wifi_priority(ssid):
    """Raise priority of a WiFi connection so it auto-connects after reboot."""
    _run(
        f"nmcli connection modify {_shq(ssid)} "
        f"connection.autoconnect yes connection.autoconnect-priority 100 2>/dev/null"
    )
    log.info("WiFi priority set for: %s", ssid)


def save_wifi_config(ssid, password, target_root="/mnt/ethos-target"):
    """Write WiFi credentials as NM connection file to the installed system.

    This does NOT connect live (hotspot stays active).  After reboot,
    NetworkManager picks up the saved file and auto-connects.
    """
    import os
    import uuid as _uuid

    nm_dir = os.path.join(target_root, "etc/NetworkManager/system-connections")
    os.makedirs(nm_dir, exist_ok=True)

    conn_uuid = str(_uuid.uuid4())
    config = (
        "[connection]\n"
        f"id={ssid}\n"
        f"uuid={conn_uuid}\n"
        "type=wifi\n"
        "autoconnect=true\n"
        "autoconnect-priority=100\n"
        "\n"
        "[wifi]\n"
        "mode=infrastructure\n"
        f"ssid={ssid}\n"
        "\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        f"psk={password}\n"
        "\n"
        "[ipv4]\n"
        "method=auto\n"
        "\n"
        "[ipv6]\n"
        "addr-gen-mode=default\n"
        "method=auto\n"
    )
    safe_name = re.sub(r"[^\w\s-]", "_", ssid)
    filepath = os.path.join(nm_dir, f"{safe_name}.nmconnection")
    with open(filepath, "w") as f:
        f.write(config)
    os.chmod(filepath, 0o600)
    log.info("WiFi config saved to: %s", filepath)
    return True
