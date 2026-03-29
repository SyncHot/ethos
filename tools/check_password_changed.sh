#!/bin/bash
# Gate SSH login until the default password has been changed via the Web UI.
# Used as ForceCommand in sshd_config — must exec the user's intended
# command/shell when the gate passes, or exit 1 to deny access.
MARKER="/opt/ethos/.password_changed"

if [ ! -f "$MARKER" ]; then
    echo "======================================================================"
    echo " Default password is still active — SSH access denied."
    echo " Change your password in the Web UI first:"
    echo "   http://$(hostname -I | awk '{print $1}'):9000/"
    echo "======================================================================"
    exit 1
fi

# Password was changed — allow the session
if [ -n "$SSH_ORIGINAL_COMMAND" ]; then
    exec /bin/bash -c "$SSH_ORIGINAL_COMMAND"
else
    exec /bin/bash --login
fi
