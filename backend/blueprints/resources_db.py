"""
EthOS — Resources Monitor Database
SQLite persistence for system metrics history.
Migrated from standalone resources app.
"""

import sqlite3
import os
import sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path

DB_PATH = os.environ.get('RESOURCES_DB_PATH', data_path('resources.db'))


def get_db():
    from blueprints.db_pool import get_pooled_db
    return get_pooled_db(DB_PATH)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    cursor = conn.cursor()

    cursor.executescript('''
        CREATE TABLE IF NOT EXISTS cpu_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            usage_percent REAL,
            core_count INTEGER,
            frequency_current REAL,
            frequency_max REAL,
            temperature REAL
        );
        CREATE TABLE IF NOT EXISTS ram_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            total_bytes INTEGER,
            used_bytes INTEGER,
            available_bytes INTEGER,
            usage_percent REAL,
            swap_total INTEGER,
            swap_used INTEGER,
            swap_percent REAL
        );
        CREATE TABLE IF NOT EXISTS gpu_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            gpu_id INTEGER,
            name TEXT,
            load_percent REAL,
            memory_used REAL,
            memory_total REAL,
            temperature REAL
        );
        CREATE TABLE IF NOT EXISTS disk_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            device TEXT,
            mountpoint TEXT,
            fstype TEXT,
            total_bytes INTEGER,
            used_bytes INTEGER,
            free_bytes INTEGER,
            usage_percent REAL,
            read_bytes INTEGER,
            write_bytes INTEGER
        );
        CREATE TABLE IF NOT EXISTS network_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            interface TEXT,
            bytes_sent INTEGER,
            bytes_recv INTEGER,
            packets_sent INTEGER,
            packets_recv INTEGER,
            speed_sent REAL,
            speed_recv REAL
        );
        CREATE TABLE IF NOT EXISTS process_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            pid INTEGER,
            name TEXT,
            cpu_percent REAL,
            memory_percent REAL,
            memory_bytes INTEGER,
            status TEXT,
            username TEXT
        );
        CREATE TABLE IF NOT EXISTS usb_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            bus TEXT,
            device TEXT,
            vendor_id TEXT,
            product_id TEXT,
            manufacturer TEXT,
            product TEXT,
            serial TEXT,
            device_class TEXT
        );
        CREATE TABLE IF NOT EXISTS docker_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            container_id TEXT,
            name TEXT,
            image TEXT,
            status TEXT,
            cpu_percent REAL,
            memory_usage INTEGER,
            memory_limit INTEGER,
            memory_percent REAL,
            net_input INTEGER,
            net_output INTEGER,
            block_read INTEGER,
            block_write INTEGER,
            pids INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_cpu_timestamp ON cpu_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_ram_timestamp ON ram_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_gpu_timestamp ON gpu_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_disk_timestamp ON disk_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_network_timestamp ON network_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_process_timestamp ON process_history(timestamp);
        CREATE INDEX IF NOT EXISTS idx_docker_timestamp ON docker_history(timestamp);
    ''')

    conn.commit()
    conn.close()


def save_cpu_data(data):
    conn = get_db()
    conn.execute(
        'INSERT INTO cpu_history (usage_percent, core_count, frequency_current, frequency_max, temperature) VALUES (?, ?, ?, ?, ?)',
        (data['usage_percent'], data['core_count'], data['frequency_current'], data['frequency_max'], data.get('temperature'))
    )
    conn.commit()
    conn.close()


def save_ram_data(data):
    conn = get_db()
    conn.execute(
        'INSERT INTO ram_history (total_bytes, used_bytes, available_bytes, usage_percent, swap_total, swap_used, swap_percent) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (data['total'], data['used'], data['available'], data['percent'],
         data['swap_total'], data['swap_used'], data['swap_percent'])
    )
    conn.commit()
    conn.close()


def save_gpu_data(gpus):
    conn = get_db()
    for gpu in gpus:
        conn.execute(
            'INSERT INTO gpu_history (gpu_id, name, load_percent, memory_used, memory_total, temperature) VALUES (?, ?, ?, ?, ?, ?)',
            (gpu['id'], gpu['name'], gpu['load'], gpu['memory_used'], gpu['memory_total'], gpu['temperature'])
        )
    conn.commit()
    conn.close()


def save_disk_data(disks):
    conn = get_db()
    for disk in disks:
        conn.execute(
            'INSERT INTO disk_history (device, mountpoint, fstype, total_bytes, used_bytes, free_bytes, usage_percent, read_bytes, write_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (disk['device'], disk['mountpoint'], disk['fstype'], disk['total'],
             disk['used'], disk['free'], disk['percent'], disk.get('read_bytes', 0), disk.get('write_bytes', 0))
        )
    conn.commit()
    conn.close()


def save_network_data(interfaces):
    conn = get_db()
    for iface in interfaces:
        conn.execute(
            'INSERT INTO network_history (interface, bytes_sent, bytes_recv, packets_sent, packets_recv, speed_sent, speed_recv) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (iface['name'], iface['bytes_sent'], iface['bytes_recv'],
             iface['packets_sent'], iface['packets_recv'], iface.get('speed_sent', 0), iface.get('speed_recv', 0))
        )
    conn.commit()
    conn.close()


def save_process_data(processes):
    conn = get_db()
    for proc in processes[:20]:
        conn.execute(
            'INSERT INTO process_history (pid, name, cpu_percent, memory_percent, memory_bytes, status, username) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (proc['pid'], proc['name'], proc['cpu_percent'], proc['memory_percent'],
             proc['memory_bytes'], proc['status'], proc['username'])
        )
    conn.commit()
    conn.close()


def save_usb_data(devices):
    conn = get_db()
    conn.execute('DELETE FROM usb_devices')
    for dev in devices:
        conn.execute(
            'INSERT INTO usb_devices (bus, device, vendor_id, product_id, manufacturer, product, serial, device_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (dev.get('bus'), dev.get('device'), dev.get('vendor_id'), dev.get('product_id'),
             dev.get('manufacturer'), dev.get('product'), dev.get('serial'), dev.get('device_class'))
        )
    conn.commit()
    conn.close()


def save_docker_data(containers):
    conn = get_db()
    for c in containers[:20]:
        conn.execute(
            'INSERT INTO docker_history (container_id, name, image, status, cpu_percent, memory_usage, memory_limit, memory_percent, net_input, net_output, block_read, block_write, pids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (c['id'], c['name'], c['image'], c['status'], c['cpu_percent'],
             c['memory_usage'], c['memory_limit'], c['memory_percent'],
             c['net_input'], c['net_output'], c['block_read'], c['block_write'], c['pids'])
        )
    conn.commit()
    conn.close()


def get_history(table, hours=1, limit=500):
    conn = get_db()
    since = (datetime.utcnow() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute(
        f'SELECT * FROM {table} WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?',
        (since, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cleanup_old_data(days=7):
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    tables = ['cpu_history', 'ram_history', 'gpu_history', 'disk_history', 'network_history', 'process_history', 'docker_history']
    for table in tables:
        conn.execute(f'DELETE FROM {table} WHERE timestamp < ?', (cutoff,))
    conn.commit()
    conn.close()
