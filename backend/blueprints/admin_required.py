from flask import g, jsonify, request
from functools import wraps
import logging

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = getattr(g, 'username', None)
        role = getattr(g, 'role', None)
        groups = getattr(g, 'groups', [])
        
        # Check role first (set by app.py based on groups), then fallback to direct checks
        is_admin = (role == 'admin') or (user == 'admin') or ('sudo' in groups) or ('root' in groups)
        
        if not is_admin:
            try:
                from blueprints.eventlog import log
                log('security', 'warning', f'Unauthorized access attempt by user: {user}', details={'user': user, 'path': request.path})
            except Exception:
                pass
            return jsonify({'error': 'Admin privileges required'}), 403
        return f(*args, **kwargs)
    return decorated
