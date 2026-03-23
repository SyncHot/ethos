"""Disk discovery & validation API routes."""

from flask import Blueprint, jsonify, request, current_app
import disk_ops

disks_bp = Blueprint("disks", __name__, url_prefix="/api/disks")


@disks_bp.route("/discover", methods=["GET"])
def discover():
    result = disk_ops.discover()
    return jsonify(result)


@disks_bp.route("/validate", methods=["POST"])
def validate():
    data = request.get_json(silent=True) or {}
    os_disk = data.get("os_disk", "").strip()
    data_disk = data.get("data_disk")  # None or "same" or device name
    boot_device = data.get("boot_device", "")

    ok, errors, warnings = disk_ops.validate(os_disk, data_disk, boot_device)
    return jsonify({"ok": ok, "errors": errors, "warnings": warnings})
