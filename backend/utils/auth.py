"""
EthOS Standard Authentication Utilities

This module provides standardized authentication patterns for all blueprints
to improve consistency and AI readability.
"""

from functools import wraps
from flask import g, jsonify
from blueprints.admin_required import admin_required as _admin_required

def standard_auth_required(f):
    """
    Standard authentication decorator that checks if user is authenticated.

    This decorator should be used for all endpoints that require authentication.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if user is authenticated
        if not hasattr(g, 'username') or g.username is None:
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function

def standard_admin_required(f):
    """
    Standard admin authentication decorator.

    This decorator should be used for all endpoints that require admin privileges.
    """
    return _admin_required(f)

# Export the decorators for use in blueprints
require_auth = standard_auth_required
require_admin = standard_admin_required