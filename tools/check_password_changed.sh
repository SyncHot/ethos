#!/bin/bash
# Check if password change marker exists
MARKER="/opt/ethos/.password_changed"

if [ ! -f "$MARKER" ]; then
    echo "======================================================================"
    echo " CRITICAL SECURITY WARNING: Default password in use."
    echo " Access denied."
    echo ""
    echo " You MUST change the password via the Web UI first."
    echo " Open http://$(hostname).local/ or http://<IP>:9000/"
    echo "======================================================================"
    exit 1
fi
exit 0
