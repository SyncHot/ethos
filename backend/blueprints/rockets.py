
from flask import Blueprint, jsonify

rockets_bp = Blueprint('rockets', __name__, url_prefix='/api/rockets')

@rockets_bp.route('/status')
def status():
    return jsonify({'status': 'ok', 'message': 'Rockets app is running'})
