from flask import g, jsonify
from functools import wraps
import logging

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = getattr(g, 'username', None)
        groups = getattr(g, 'groups', [])
        if user != 'admin' and 'sudo' not in groups and 'root' not in groups:
            logging.getLogger('ddns').warning(f'Unauthorized access attempt by user: {user}')
            return jsonify({'error': 'Admin privileges required'}), 403
        return f(*args, **kwargs)
    return decorated
