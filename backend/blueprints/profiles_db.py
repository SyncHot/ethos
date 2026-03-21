"""
EthOS — Backup Profiles Database
SQLite for backup profiles.
Migrated from standalone backuprestore app.
"""

import sqlite3
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path


def get_db_connection():
    db_path = os.environ.get('PROFILES_DB_PATH', data_path('profiles.db'))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')  # Enable Write-Ahead Logging
    return conn


def init_profiles_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            paths TEXT NOT NULL,
            destination TEXT,
            schedule TEXT,
            options TEXT
        )
    ''')
    try:
        c.execute('ALTER TABLE profiles ADD COLUMN retention INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    try:
        c.execute('ALTER TABLE profiles ADD COLUMN incremental INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()
