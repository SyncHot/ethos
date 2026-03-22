#!/bin/bash
# Block SSH login until the default password has been changed via the Web UI.
# The marker file is created by the backend after a successful password change.
MARKER="/opt/ethos/.password_changed"

if [ ! -f "$MARKER" ]; then
    echo "======================================================================"
    echo " Default password is still active — SSH access denied."
    echo " Change your password in the Web UI first:"
    echo "   http://$(hostname -I | awk '{print $1}'):9000/"
    echo "======================================================================"
    exit 1
fi
exit 0
