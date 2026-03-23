"""
Pixel-diff comparison — compares a test screenshot against a baseline.
Uses Pillow for fast, free, local comparison.
"""

import logging
import os
from PIL import Image, ImageChops

log = logging.getLogger("visual_qa.pixel_diff")

DEFAULT_THRESHOLD = 2.0  # percentage of changed pixels


def compare(test_path, baseline_path, diff_output_path=None, threshold=DEFAULT_THRESHOLD):
    """
    Compare two screenshots pixel-by-pixel.
    Returns dict: changed (bool), diff_pct, diff_image path, pixel counts.
    """
    if not os.path.exists(baseline_path):
        log.warning("No baseline found: %s — skipping diff", baseline_path)
        return {
            "changed": True, "diff_pct": 100.0, "diff_image": None,
            "total_pixels": 0, "changed_pixels": 0, "reason": "no_baseline",
        }

    try:
        img_test = Image.open(test_path).convert("RGB")
        img_base = Image.open(baseline_path).convert("RGB")

        if img_test.size != img_base.size:
            img_test = img_test.resize(img_base.size, Image.LANCZOS)

        diff = ImageChops.difference(img_test, img_base)
        gray = diff.convert("L")
        changed_pixels = sum(1 for p in gray.getdata() if p > 20)
        total_pixels = gray.size[0] * gray.size[1]
        diff_pct = (changed_pixels / total_pixels * 100) if total_pixels > 0 else 0.0

        diff_image = None
        if diff_output_path and diff_pct > 0:
            from PIL import ImageEnhance
            enhanced = ImageEnhance.Brightness(diff).enhance(5.0)
            enhanced.save(diff_output_path)
            diff_image = diff_output_path

        result = {
            "changed": diff_pct > threshold,
            "diff_pct": round(diff_pct, 2),
            "diff_image": diff_image,
            "total_pixels": total_pixels,
            "changed_pixels": changed_pixels,
        }
        log.info("Pixel diff: %.2f%% changed (%d/%d px) — %s",
                 diff_pct, changed_pixels, total_pixels,
                 "CHANGED" if result["changed"] else "OK")
        return result

    except Exception as e:
        log.error("Pixel diff error: %s", e)
        return {
            "changed": True, "diff_pct": -1, "diff_image": None,
            "total_pixels": 0, "changed_pixels": 0, "reason": str(e),
        }
