"""
Radio & Music v2 — SQLite schema.
Tables: favorites, history, playlists
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from host import data_path

DB_PATH = data_path('radio_music_v2.db')

def get_db():
    """Get database connection with WAL mode for concurrent reads."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn

def init_db():
    """Initialize database schema."""
    conn = get_db()
    cur = conn.cursor()
    
    # Favorites (radio + podcasts)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            data TEXT NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_fav_type ON favorites(type)')
    
    # Playback history
    cur.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            title TEXT,
            data TEXT NOT NULL,
            played_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_history_played ON history(played_at DESC)')
    
    # Playlists
    cur.execute('''
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tracks TEXT NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s', 'now')),
            updated_at INTEGER DEFAULT (strftime('%s', 'now'))
        )
    ''')
    
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    print(f'Database initialized: {DB_PATH}')
