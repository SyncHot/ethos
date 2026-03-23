"""WiFi API routes."""

import os
from flask import Blueprint, jsonify, request
import wifi_ops

wifi_bp = Blueprint("wifi", __name__, url_prefix="/api/wifi")


@wifi_bp.route("/scan", methods=["GET", "POST"])
def wifi_scan():
    networks = wifi_ops.scan()
    return jsonify({"networks": networks})


@wifi_bp.route("/connect", methods=["POST"])
def wifi_connect():
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")

    if not ssid:
        return jsonify({"ok": False, "error": "SSID required"}), 400

    ok, message, ip = wifi_ops.connect(ssid, password)
    if ok:
        wifi_ops.save_wifi_priority(ssid)
    return jsonify({"ok": ok, "message": message, "ip": ip})


@wifi_bp.route("/save", methods=["POST"])
def wifi_save():
    """Save WiFi credentials to installed system (no live connect — hotspot stays)."""
    data = request.get_json(silent=True) or {}
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")
    os_disk = data.get("os_disk", "").strip()

    if not ssid:
        return jsonify({"ok": False, "error": "SSID required"}), 400
    if not os_disk:
        return jsonify({"ok": False, "error": "OS disk required"}), 400

    mount_dir = "/mnt/ethos-target"
    from disk_ops import _run

    try:
        os.makedirs(mount_dir, exist_ok=True)
        _run(f"mount /dev/{os_disk}2 {mount_dir}", timeout=30)
        wifi_ops.save_wifi_config(ssid, password, mount_dir)
        _run(f"umount -R {mount_dir} 2>/dev/null", timeout=30)
        return jsonify({"ok": True, "message": f"WiFi '{ssid}' saved"})
    except Exception as e:
        _run(f"umount -R {mount_dir} 2>/dev/null", timeout=10)
        return jsonify({"ok": False, "error": str(e)}), 500


@wifi_bp.route("/status", methods=["GET"])
def wifi_status():
    return jsonify(wifi_ops.status())
