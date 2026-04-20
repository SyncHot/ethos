"""Disk discovery, validation & recommendation API routes."""

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


@disks_bp.route("/recommend", methods=["GET"])
def recommend():
    """Analyze available disks and recommend the best installation scenario.

    Returns one of three scenarios:
      - simple:      Single disk for OS + data (1 usable disk)
      - performance: Fast SSD/NVMe for OS, large HDD(s) for data
      - advanced:    Multiple similar disks, potential RAID
    Each scenario includes recommended disk assignments and partition preview.
    """
    result = disk_ops.discover()
    disks = result.get("disks", [])
    boot_dev = result.get("boot_device")

    # Filter usable disks (not boot, >= 9GB for OS or >= 1GB for data)
    MIN_OS = 9 * 1024**3
    MIN_DATA = 1 * 1024**3
    usable = [d for d in disks if not d["is_boot"] and d["size_bytes"] >= MIN_DATA]
    os_capable = [d for d in usable
                  if d["size_bytes"] >= MIN_OS
                  and d.get("transport") != "usb"
                  and not d.get("removable")]
    data_only = [d for d in usable if d not in os_capable]

    scenarios = []

    # --- Scenario: SIMPLE (one disk for everything) ---
    if os_capable:
        best = max(os_capable, key=lambda d: d["size_bytes"])
        data_bytes = best["size_bytes"] - (9 * 1024**3)  # ESP+RootA+RootB ≈ 9GB
        scenarios.append({
            "id": "simple",
            "recommended": len(os_capable) == 1 and not data_only,
            "os_disk": best["name"],
            "data_disk": "same",
            "partition_preview": [
                {"label": "ESP", "size_mb": 512, "fs": "FAT32"},
                {"label": "Root A", "size_mb": 4096, "fs": "ext4"},
                {"label": "Root B", "size_mb": 4096, "fs": "ext4"},
                {"label": "Data", "size_mb": max(1, int(data_bytes / (1024**2))),
                 "fs": "Btrfs"},
            ],
            "os_disk_info": best,
            "data_disk_info": None,
            "total_data_bytes": data_bytes,
        })

    # --- Scenario: PERFORMANCE (SSD for OS, HDD/large for data) ---
    ssds = [d for d in os_capable if not d.get("rotational")
            or d.get("transport") == "nvme"]
    hdds = [d for d in usable if d.get("rotational")
            and d.get("transport") != "usb"]
    large_disks = [d for d in usable if d not in ssds]

    if ssds and (hdds or large_disks):
        os_pick = max(ssds, key=lambda d: d["size_bytes"])
        data_candidates = hdds if hdds else large_disks
        data_pick = max(data_candidates, key=lambda d: d["size_bytes"])
        if os_pick["name"] != data_pick["name"]:
            os_remaining = os_pick["size_bytes"] - (9 * 1024**3)
            scenarios.append({
                "id": "performance",
                "recommended": True,
                "os_disk": os_pick["name"],
                "data_disk": data_pick["name"],
                "partition_preview": [
                    {"label": "ESP", "size_mb": 512, "fs": "FAT32",
                     "disk": "os"},
                    {"label": "Root A", "size_mb": 4096, "fs": "ext4",
                     "disk": "os"},
                    {"label": "Root B", "size_mb": 4096, "fs": "ext4",
                     "disk": "os"},
                    {"label": "NVMe Pool",
                     "size_mb": max(0, int(os_remaining / (1024**2))),
                     "fs": "Btrfs", "disk": "os"},
                    {"label": "Data",
                     "size_mb": int(data_pick["size_bytes"] / (1024**2)),
                     "fs": "Btrfs", "disk": "data"},
                ],
                "os_disk_info": os_pick,
                "data_disk_info": data_pick,
                "total_data_bytes": data_pick["size_bytes"],
            })

    # --- Scenario: ADVANCED (manual, always available) ---
    scenarios.append({
        "id": "advanced",
        "recommended": False,
        "os_disk": None,
        "data_disk": None,
        "partition_preview": [],
        "os_disk_info": None,
        "data_disk_info": None,
        "total_data_bytes": 0,
    })

    # Mark exactly one as recommended (prefer performance > simple > advanced)
    has_rec = any(s["recommended"] for s in scenarios)
    if not has_rec and scenarios:
        for s in scenarios:
            if s["id"] != "advanced":
                s["recommended"] = True
                break

    return jsonify({
        "scenarios": scenarios,
        "disks": disks,
        "boot_device": boot_dev,
        "os_capable": [d["name"] for d in os_capable],
        "data_only": [d["name"] for d in data_only],
    })
