"""WiFi API routes."""

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


@wifi_bp.route("/status", methods=["GET"])
def wifi_status():
    return jsonify(wifi_ops.status())
