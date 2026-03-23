"""
EthOS — Sticky Notes Blueprint
Simple persistent notes – like Windows Sticky Notes.
All routes under /api/notes.
"""

import os, uuid, time, logging
from flask import Blueprint, jsonify, request

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path as _data_path
from utils import load_json as _load_json, save_json as _save_json

log = logging.getLogger('stickynotes')

notes_bp = Blueprint('stickynotes', __name__, url_prefix='/api/notes')

NOTES_FILE = _data_path('stickynotes.json')

COLORS = [
    {'id': 'yellow',  'bg': '#fef08a', 'text': '#713f12', 'header': '#fde047'},
    {'id': 'green',   'bg': '#bbf7d0', 'text': '#14532d', 'header': '#86efac'},
    {'id': 'blue',    'bg': '#bfdbfe', 'text': '#1e3a5f', 'header': '#93c5fd'},
    {'id': 'pink',    'bg': '#fecdd3', 'text': '#881337', 'header': '#fda4af'},
    {'id': 'purple',  'bg': '#e9d5ff', 'text': '#581c87', 'header': '#d8b4fe'},
    {'id': 'orange',  'bg': '#fed7aa', 'text': '#7c2d12', 'header': '#fdba74'},
    {'id': 'gray',    'bg': '#e2e8f0', 'text': '#1e293b', 'header': '#cbd5e1'},
]


def _load_notes():
    data = _load_json(NOTES_FILE, {'notes': [], 'order': []})
    if 'notes' not in data:
        data = {'notes': [], 'order': []}
    return data


def _save_notes(data):
    _save_json(NOTES_FILE, data)


# ── GET all notes ──
@notes_bp.route('', methods=['GET'])
def get_notes():
    data = _load_notes()
    return jsonify(data)


# ── POST create note ──
@notes_bp.route('', methods=['POST'])
def create_note():
    body = request.json or {}
    note = {
        'id': uuid.uuid4().hex[:12],
        'title': body.get('title', '').strip()[:100] or '',
        'content': body.get('content', '').strip()[:5000] or '',
        'color': body.get('color', 'yellow'),
        'pinned': bool(body.get('pinned', False)),
        'created': time.time(),
        'updated': time.time(),
    }
    data = _load_notes()
    data['notes'].insert(0, note)
    data['order'].insert(0, note['id'])
    _save_notes(data)
    return jsonify({'ok': True, 'note': note})


# ── PUT update note ──
@notes_bp.route('/<note_id>', methods=['PUT'])
def update_note(note_id):
    data = _load_notes()
    note = next((n for n in data['notes'] if n['id'] == note_id), None)
    if not note:
        return jsonify({'error': 'Note not found'}), 404
    body = request.json or {}
    if 'title' in body:
        note['title'] = str(body['title']).strip()[:100]
    if 'content' in body:
        note['content'] = str(body['content']).strip()[:5000]
    if 'color' in body:
        note['color'] = body['color']
    if 'pinned' in body:
        note['pinned'] = bool(body['pinned'])
    note['updated'] = time.time()
    _save_notes(data)
    return jsonify({'ok': True, 'note': note})


# ── DELETE note ──
@notes_bp.route('/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    data = _load_notes()
    data['notes'] = [n for n in data['notes'] if n['id'] != note_id]
    data['order'] = [i for i in data['order'] if i != note_id]
    _save_notes(data)
    return jsonify({'ok': True})


# ── POST reorder ──
@notes_bp.route('/reorder', methods=['POST'])
def reorder_notes():
    body = request.json or {}
    order = body.get('order', [])
    data = _load_notes()
    data['order'] = order
    _save_notes(data)
    return jsonify({'ok': True})


# ── GET colors ──
@notes_bp.route('/colors', methods=['GET'])
def get_colors():
    return jsonify(COLORS)
