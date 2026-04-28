#!/usr/bin/env python3
"""
EthOS Installer — Flask-based pre-boot setup server.

Runs on port 9000 before EthOS is installed.
Serves a step-wizard UI for: language → user → disks → install → reboot.
"""

import os
import sys
import logging

from flask import Flask

from i18n import I18N
from routes_disks import disks_bp
from routes_install import install_bp

PORT = int(os.environ.get("ETHOS_INSTALLER_PORT", 9000))
HOST = "0.0.0.0"

INSTALLED_MARKER = "/opt/ethos/.installed"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/ethos-installer.log", mode="a"),
    ],
)
log = logging.getLogger("ethos-installer")


def create_app():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
        static_url_path="/static",
    )
    app.config["SECRET_KEY"] = os.urandom(24).hex()

    app.config["i18n"] = I18N()

    app.register_blueprint(disks_bp)
    app.register_blueprint(install_bp)

    @app.route("/")
    def index():
        from flask import render_template
        return render_template("installer.html")

    @app.route("/api/i18n/<lang>")
    def get_translations(lang):
        from flask import jsonify
        i18n = app.config["i18n"]
        return jsonify(i18n.get_all(lang))

    @app.route("/api/languages")
    def list_languages():
        from flask import jsonify
        i18n = app.config["i18n"]
        return jsonify(i18n.available_languages())

    @app.route("/health")
    def health():
        from flask import jsonify
        return jsonify({"status": "ok", "service": "ethos-installer"})

    return app


def main():
    if os.path.exists(INSTALLED_MARKER):
        log.info("EthOS already installed (%s exists). Exiting.", INSTALLED_MARKER)
        sys.exit(0)

    app = create_app()

    # Print access info to console (user sees this on the monitor)
    local_ip = _get_local_ip()
    default_user = _get_default_user()
    print(flush=True)
    print("=" * 60, flush=True)
    print(f"  EthOS Installer", flush=True)
    print(f"  Web UI:  http://{local_ip}:{PORT}", flush=True)
    print(f"  SSH:     ssh {default_user}@{local_ip}  (haslo: ethos)", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)

    log.info("Starting EthOS Installer on %s:%d", HOST, PORT)
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


def _get_local_ip():
    """Get the best local IP to display."""
    import subprocess
    try:
        out = subprocess.check_output(
            "hostname -I 2>/dev/null || echo '0.0.0.0'",
            shell=True, text=True, timeout=5
        ).strip()
        ips = out.split()
        return ips[0] if ips else "0.0.0.0"
    except Exception:
        return "0.0.0.0"


def _get_default_user():
    """Get the default system user for SSH hint."""
    import subprocess
    try:
        # Read ETHOS_USER from install.conf if available
        conf = "/opt/ethos/install.conf"
        if os.path.exists(conf):
            with open(conf) as f:
                for line in f:
                    if line.startswith("ETHOS_USER="):
                        return line.split("=", 1)[1].strip().strip('"')
        # Fall back to first non-system user (UID >= 1000)
        out = subprocess.check_output(
            "awk -F: '$3>=1000&&$3<65534{print $1;exit}' /etc/passwd",
            shell=True, text=True, timeout=3
        ).strip()
        return out or "nasadmin"
    except Exception:
        return "nasadmin"


if __name__ == "__main__":
    main()
