"""
EthOS — Security Audit Logging Helper

Provides audit_log() which writes security-relevant events to the
existing eventlog system with category='security'.

Usage:
    from audit import audit_log
    audit_log('auth.login.success', 'User logged in', username='admin')
"""

from flask import request, g
from blueprints.eventlog import log as elog


def audit_log(action, details, username=None):
    """Write a security audit entry to the event log.

    Parameters
    ----------
    action : str
        Dotted action type, e.g. "auth.login.success", "user.create".
    details : str
        Human-readable description of what happened.
    username : str, optional
        The actor.  Falls back to g.username from the current request context.
    """
    actor = username
    if actor is None:
        try:
            actor = getattr(g, 'username', None)
        except RuntimeError:
            actor = None
    actor = actor or 'unknown'

    try:
        ip = request.remote_addr or '0.0.0.0'
    except RuntimeError:
        ip = '0.0.0.0'

    elog('security', 'info', f'[AUDIT] {action}: {details}', {
        'audit_action': action,
        'actor': actor,
        'ip': ip,
        'details': details,
    })
