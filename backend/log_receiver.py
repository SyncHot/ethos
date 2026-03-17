"""
EthOS — Remote Log Receiver & Dashboard

Full-featured web app to receive, store, and browse device diagnostic logs.
Deploy on your central server (e.g., nas.myserver.pl).

Usage:
    pip install flask
    python log_receiver.py

    # Or behind nginx/gunicorn:
    gunicorn -b 0.0.0.0:5050 log_receiver:app

Logs stored in ./device_logs/{device_id}/*.json
Dashboard at http://localhost:5050
"""

import json
import os
import re
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'device_logs')
os.makedirs(LOG_DIR, exist_ok=True)


# ═══════════════════ API ═══════════════════

@app.route('/api/device-logs', methods=['POST'])
def receive_device_logs():
    """Receive diagnostic report from a EthOS device."""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({'error': 'Invalid JSON'}), 400

    device_id = data.get('device_id', 'unknown')
    reason = data.get('reason', 'unknown')

    device_dir = os.path.join(LOG_DIR, _safe(device_id))
    os.makedirs(device_dir, exist_ok=True)

    ts_slug = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f'{ts_slug}_{reason}.json'
    filepath = os.path.join(device_dir, filename)

    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    latest = os.path.join(device_dir, 'latest.json')
    with open(latest, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    sys_info = data.get('system', {})
    hostname = sys_info.get('hostname', '?')
    version = sys_info.get('ethos_version', '?')
    ip = sys_info.get('ip', '?')
    errors = data.get('errors', [])
    services = data.get('services', {})
    failed = [s for s, v in services.items() if v.get('active') == 'failed']

    print(f'[{ts_slug}] {device_id[:12]}.. | {hostname} | {ip} | v{version} | {reason}'
          + (f' | {len(errors)} err' if errors else '')
          + (f' | FAIL: {",".join(failed)}' if failed else ''))

    return jsonify({'ok': True, 'saved': filename})


@app.route('/api/device-logs', methods=['GET'])
def list_devices():
    """List all devices with latest report summary."""
    devices = []
    for name in sorted(os.listdir(LOG_DIR)):
        device_dir = os.path.join(LOG_DIR, name)
        if not os.path.isdir(device_dir):
            continue
        latest = os.path.join(device_dir, 'latest.json')
        info = {'device_id': name, 'reports': 0}
        try:
            info['reports'] = len([f for f in os.listdir(device_dir)
                                   if f.endswith('.json') and f != 'latest.json'])
        except Exception:
            pass
        if os.path.isfile(latest):
            try:
                with open(latest) as f:
                    data = json.load(f)
                sys_info = data.get('system', {})
                info['hostname'] = sys_info.get('hostname', '?')
                info['ip'] = sys_info.get('ip', '?')
                info['version'] = sys_info.get('ethos_version', '?')
                info['last_seen'] = data.get('timestamp', '?')
                info['reason'] = data.get('reason', '?')
                info['uptime_seconds'] = sys_info.get('uptime_seconds', 0)
                info['disk'] = sys_info.get('disk_usage', '')
                info['ram_mb'] = f"{sys_info.get('ram_used_mb', '?')}/{sys_info.get('ram_total_mb', '?')}"
                info['kernel'] = sys_info.get('kernel', '?')
                info['arch'] = sys_info.get('arch', '?')
                info['installed_at'] = sys_info.get('installed_at', '')
                services = data.get('services', {})
                info['failed_services'] = [s for s, v in services.items()
                                           if v.get('active') == 'failed']
                info['error_count'] = len(data.get('errors', []))
                info['services'] = services
            except Exception:
                pass
        devices.append(info)
    return jsonify({'devices': devices})


@app.route('/api/device-logs/<device_id>')
def device_detail(device_id):
    """Get latest report for a device."""
    device_dir = os.path.join(LOG_DIR, _safe(device_id))
    latest = os.path.join(device_dir, 'latest.json')
    if not os.path.isfile(latest):
        return jsonify({'error': 'Device not found'}), 404
    with open(latest) as f:
        return jsonify(json.load(f))


@app.route('/api/device-logs/<device_id>/history')
def device_history(device_id):
    """List all reports for a device."""
    device_dir = os.path.join(LOG_DIR, _safe(device_id))
    if not os.path.isdir(device_dir):
        return jsonify({'error': 'Device not found'}), 404
    reports = []
    for f in sorted(os.listdir(device_dir), reverse=True):
        if f.endswith('.json') and f != 'latest.json':
            fp = os.path.join(device_dir, f)
            reason = f.rsplit('_', 1)[-1].replace('.json', '') if '_' in f else '?'
            reports.append({
                'filename': f,
                'size': os.path.getsize(fp),
                'reason': reason,
            })
    return jsonify({'device_id': device_id, 'reports': reports})


@app.route('/api/device-logs/<device_id>/<filename>')
def device_report(device_id, filename):
    """Get a specific historical report."""
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'Invalid'}), 400
    filepath = os.path.join(LOG_DIR, _safe(device_id), filename)
    if not os.path.isfile(filepath):
        return jsonify({'error': 'Not found'}), 404
    with open(filepath) as f:
        return jsonify(json.load(f))


@app.route('/api/device-logs/<device_id>', methods=['DELETE'])
def delete_device(device_id):
    """Delete all logs for a device."""
    import shutil
    device_dir = os.path.join(LOG_DIR, _safe(device_id))
    if os.path.isdir(device_dir):
        shutil.rmtree(device_dir)
    return jsonify({'ok': True})


@app.route('/api/device-logs/<device_id>/<filename>', methods=['DELETE'])
def delete_report(device_id, filename):
    """Delete a specific report."""
    if '..' in filename or '/' in filename:
        return jsonify({'error': 'Invalid'}), 400
    filepath = os.path.join(LOG_DIR, _safe(device_id), filename)
    if os.path.isfile(filepath):
        os.remove(filepath)
    return jsonify({'ok': True})


def _safe(s):
    """Sanitize device ID for filesystem use."""
    return re.sub(r'[^a-zA-Z0-9_-]', '', s)[:32]


# ═══════════════════ FRONTEND SPA ═══════════════════

@app.route('/')
def index():
    return DASHBOARD_HTML


DASHBOARD_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EthOS &mdash; Device Logs</title>
<style>
:root{--bg:#0f172a;--bg2:#1e293b;--bg3:#334155;--border:#475569;--text:#e2e8f0;--text2:#94a3b8;--text3:#64748b;--accent:#38bdf8;--green:#10b981;--red:#ef4444;--orange:#f59e0b;--purple:#a78bfa;--radius:8px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* Layout */
.topbar{background:var(--bg2);border-bottom:1px solid var(--bg3);padding:12px 24px;display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100}
.topbar h1{font-size:18px;color:var(--accent);cursor:pointer}
.topbar h1:hover{color:#7dd3fc}
.topbar .crumb{color:var(--text3);font-size:14px}
.topbar .crumb a{color:var(--text2)}
.main{max-width:1200px;margin:0 auto;padding:24px}

/* Cards */
.card{background:var(--bg2);border:1px solid var(--bg3);border-radius:var(--radius);margin-bottom:16px;overflow:hidden}
.card-header{padding:14px 18px;border-bottom:1px solid var(--bg3);display:flex;align-items:center;justify-content:space-between}
.card-header h2{font-size:15px;font-weight:600}
.card-body{padding:18px}

/* Table */
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:8px 12px;font-size:11px;text-transform:uppercase;color:var(--text3);font-weight:600;border-bottom:1px solid var(--bg3)}
td{padding:10px 12px;border-bottom:1px solid rgba(51,65,85,.5);font-size:13px;vertical-align:top}
tr:hover td{background:rgba(56,189,248,.03)}
tr.clickable{cursor:pointer}
tr.clickable:hover td{background:rgba(56,189,248,.06)}

/* Tags / badges */
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.tag-ok{background:rgba(16,185,129,.15);color:var(--green)}
.tag-err{background:rgba(239,68,68,.15);color:var(--red)}
.tag-warn{background:rgba(245,158,11,.15);color:var(--orange)}
.tag-info{background:rgba(56,189,248,.12);color:var(--accent)}
.tag-purple{background:rgba(167,139,250,.15);color:var(--purple)}
.tag-boot{background:rgba(56,189,248,.12);color:var(--accent)}
.tag-periodic{background:rgba(100,116,139,.2);color:var(--text3)}
.tag-error{background:rgba(239,68,68,.15);color:var(--red)}
.tag-manual{background:rgba(167,139,250,.15);color:var(--purple)}

/* Stats */
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.stat{background:var(--bg2);border:1px solid var(--bg3);border-radius:var(--radius);padding:16px 20px;min-width:120px;text-align:center}
.stat .val{font-size:24px;font-weight:700;color:var(--accent)}
.stat .lbl{font-size:11px;color:var(--text3);margin-top:2px;text-transform:uppercase}

/* Buttons */
.btn{padding:6px 14px;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:filter .2s}
.btn-primary{background:var(--accent);color:var(--bg)}
.btn-danger{background:var(--red);color:#fff}
.btn-ghost{background:transparent;color:var(--text2);border:1px solid var(--bg3)}
.btn:hover{filter:brightness(1.15)}
.btn:disabled{opacity:.4;cursor:not-allowed}

/* Log viewer */
.log-block{background:var(--bg);border:1px solid var(--bg3);border-radius:6px;padding:14px;font-family:'JetBrains Mono',Consolas,monospace;font-size:12px;line-height:1.7;max-height:400px;overflow:auto;white-space:pre-wrap;word-break:break-all;color:var(--text2);margin-bottom:12px}
.log-block .line-err{color:var(--red)}
.log-block .line-warn{color:var(--orange)}
.log-block .line-ok{color:var(--green)}
.log-block .line-info{color:var(--accent)}

/* Key-value grid */
.kv{display:grid;grid-template-columns:160px 1fr;gap:6px 12px;font-size:13px}
.kv .k{color:var(--text3);font-weight:500}
.kv .v{color:var(--text)}

/* Service grid */
.svc-grid{display:flex;flex-wrap:wrap;gap:8px}
.svc{padding:8px 14px;border-radius:6px;border:1px solid var(--bg3);font-size:12px;font-weight:500}
.svc-active{border-color:var(--green);color:var(--green)}
.svc-failed{border-color:var(--red);color:var(--red);background:rgba(239,68,68,.08)}
.svc-inactive{border-color:var(--text3);color:var(--text3)}

/* Tabs */
.tabs{display:flex;gap:0;border-bottom:1px solid var(--bg3);margin-bottom:16px;flex-wrap:wrap}
.tab{padding:10px 18px;font-size:13px;font-weight:500;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;transition:all .2s}
.tab:hover{color:var(--text2)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}

/* Error list */
.err-item{padding:10px 14px;border-bottom:1px solid var(--bg3);font-size:13px}
.err-item:last-child{border-bottom:none}
.err-time{font-size:11px;color:var(--text3);margin-right:8px}
.err-cat{font-size:11px;color:var(--text3)}
.err-msg{color:var(--red);margin-top:2px}

/* Search */
.search-box{background:var(--bg);border:1px solid var(--bg3);border-radius:6px;padding:8px 14px;color:var(--text);font-size:13px;width:220px;outline:none}
.search-box:focus{border-color:var(--accent)}

/* Responsive */
@media(max-width:768px){.main{padding:12px}.kv{grid-template-columns:1fr}.stats{flex-direction:column}.search-box{width:100%}}

/* Spinner */
.spinner{display:inline-block;width:18px;height:18px;border:2px solid var(--bg3);border-top-color:var(--accent);border-radius:50%;animation:spin .6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* Empty */
.empty{text-align:center;padding:40px;color:var(--text3)}
</style>
</head>
<body>

<div class="topbar">
    <h1 onclick="navigate('home')">&#x1F4E1; EthOS Logs</h1>
    <div class="crumb" id="breadcrumb"></div>
</div>

<div class="main" id="app">
    <div style="text-align:center;padding:40px"><div class="spinner"></div></div>
</div>

<script>
const $ = s => document.querySelector(s);
const app = $('#app');
const crumb = $('#breadcrumb');
let currentView = 'home';
let currentDevice = null;

function esc(s) { if (s == null) return ''; const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }

function timeAgo(ts) {
    if (!ts) return '?';
    const d = new Date(ts);
    const now = Date.now();
    const diff = Math.floor((now - d.getTime()) / 1000);
    if (isNaN(diff) || diff < 0) return esc(ts);
    if (diff < 60) return diff + 's ago';
    if (diff < 3600) return Math.floor(diff/60) + 'm ago';
    if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
    return Math.floor(diff/86400) + 'd ago';
}

function uptime(s) {
    if (!s) return '?';
    const d = Math.floor(s/86400);
    const h = Math.floor((s%86400)/3600);
    const m = Math.floor((s%3600)/60);
    if (d > 0) return d+'d '+h+'h';
    return h > 0 ? h+'h '+m+'m' : m+'m';
}

function reasonTag(r) {
    const cls = r === 'boot' ? 'tag-boot' : r === 'error' ? 'tag-error' : r === 'manual' ? 'tag-manual' : 'tag-periodic';
    return '<span class="tag '+cls+'">'+esc(r)+'</span>';
}

function statusTag(failed, errCount) {
    if (failed && failed.length) return '<span class="tag tag-err">FAILED</span>';
    if (errCount > 0) return '<span class="tag tag-warn">'+errCount+' errors</span>';
    return '<span class="tag tag-ok">OK</span>';
}

async function api(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
}

async function apiDelete(url) {
    const r = await fetch(url, {method:'DELETE'});
    return r.json();
}

function navigate(view, deviceId, reportFile) {
    currentView = view;
    if (view === 'home') renderDeviceList();
    else if (view === 'device') renderDeviceDetail(deviceId);
    else if (view === 'report') renderReport(deviceId, reportFile);
}

/* ══════════════ Device List ══════════════ */

async function renderDeviceList() {
    crumb.innerHTML = '';
    currentDevice = null;
    app.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';

    try {
        const data = await api('/api/device-logs');
        const devices = data.devices || [];

        if (!devices.length) {
            app.innerHTML = '<div class="empty">' +
                '<div style="font-size:48px;margin-bottom:16px">&#x1F4E1;</div>' +
                '<h3>No devices yet</h3>' +
                '<p style="margin-top:8px;font-size:13px">Devices will appear here when they send their first log report.</p></div>';
            return;
        }

        const totalDevices = devices.length;
        const totalReports = devices.reduce(function(s,d){return s+(d.reports||0)},0);
        const withErrors = devices.filter(function(d){return d.error_count > 0 || (d.failed_services && d.failed_services.length)}).length;
        const healthy = totalDevices - withErrors;

        let html = '<div class="stats">' +
            '<div class="stat"><div class="val">'+totalDevices+'</div><div class="lbl">Devices</div></div>' +
            '<div class="stat"><div class="val">'+totalReports+'</div><div class="lbl">Reports</div></div>' +
            '<div class="stat"><div class="val" style="color:var(--green)">'+healthy+'</div><div class="lbl">Healthy</div></div>' +
            '<div class="stat"><div class="val" style="color:'+(withErrors?'var(--red)':'var(--green)')+'">'+withErrors+'</div><div class="lbl">With Issues</div></div>' +
            '</div>';

        html += '<div class="card"><div class="card-header"><h2>Devices</h2>' +
            '<input class="search-box" id="dev-search" placeholder="Filter devices..." oninput="filterDeviceRows(this.value)">' +
            '</div>';
        html += '<table><thead><tr><th>Hostname</th><th>IP</th><th>Version</th><th>Uptime</th><th>Last Seen</th><th>Reason</th><th>Status</th><th>Reports</th></tr></thead>';
        html += '<tbody id="device-tbody">';

        for (var i = 0; i < devices.length; i++) {
            var d = devices[i];
            html += '<tr class="clickable dev-row" onclick="navigate(\'device\',\''+esc(d.device_id)+'\')" data-search="'
                +esc((d.hostname||'')+(d.ip||'')+(d.device_id||'')+(d.version||'')).toLowerCase()+'">'
                +'<td><strong>'+esc(d.hostname||'?')+'</strong><br><span style="font-size:11px;color:var(--text3)">'+esc(d.device_id).slice(0,16)+'&hellip;</span></td>'
                +'<td>'+esc(d.ip||'?')+'</td>'
                +'<td><span class="tag tag-info">v'+esc(d.version||'?')+'</span></td>'
                +'<td>'+uptime(d.uptime_seconds)+'</td>'
                +'<td>'+timeAgo(d.last_seen)+'<br><span style="font-size:11px;color:var(--text3)">'+esc(d.last_seen||'').slice(0,19)+'</span></td>'
                +'<td>'+reasonTag(d.reason)+'</td>'
                +'<td>'+statusTag(d.failed_services, d.error_count)+'</td>'
                +'<td>'+esc(d.reports||0)+'</td></tr>';
        }
        html += '</tbody></table></div>';
        app.innerHTML = html;
    } catch(e) {
        app.innerHTML = '<div class="empty"><h3 style="color:var(--red)">Error loading devices</h3><p>'+esc(e.message)+'</p></div>';
    }
}

function filterDeviceRows(q) {
    q = q.toLowerCase();
    var rows = document.querySelectorAll('.dev-row');
    for (var i = 0; i < rows.length; i++) {
        rows[i].style.display = (!q || rows[i].getAttribute('data-search').indexOf(q) >= 0) ? '' : 'none';
    }
}

/* ══════════════ Device Detail ══════════════ */

async function renderDeviceDetail(deviceId) {
    currentDevice = deviceId;
    crumb.innerHTML = '<a href="#" onclick="navigate(\'home\');return false">Devices</a> &rsaquo; '+esc(deviceId).slice(0,16)+'&hellip;';
    app.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';

    try {
        var results = await Promise.all([
            api('/api/device-logs/'+deviceId),
            api('/api/device-logs/'+deviceId+'/history'),
        ]);
        var report = results[0];
        var historyData = results[1];

        var sys = report.system || {};
        var services = report.services || {};
        var network = report.network || {};
        var errors = report.errors || [];
        var journals = report.journals || {};
        var history = historyData.reports || [];

        var html = '';

        /* System Info */
        html += '<div class="card"><div class="card-header">'
            +'<h2>&#x1F5A5;&#xFE0F; '+esc(sys.hostname||'?')+' <span style="font-weight:400;color:var(--text3);font-size:13px">'+esc(deviceId).slice(0,20)+'</span></h2>'
            +'<div><button class="btn btn-danger" onclick="deleteDevice(\''+esc(deviceId)+'\')">Delete Device</button></div>'
            +'</div><div class="card-body"><div class="kv">'
            +'<div class="k">IP Address</div><div class="v">'+esc(sys.ip)+'</div>'
            +'<div class="k">EthOS Version</div><div class="v"><span class="tag tag-info">v'+esc(sys.ethos_version)+'</span></div>'
            +'<div class="k">Kernel</div><div class="v">'+esc(sys.kernel)+' ('+esc(sys.arch)+')</div>'
            +'<div class="k">Uptime</div><div class="v">'+uptime(sys.uptime_seconds)+'</div>'
            +'<div class="k">Disk Usage</div><div class="v">'+esc(sys.disk_usage)+'</div>'
            +'<div class="k">RAM</div><div class="v">'+esc(sys.ram_used_mb||'?')+' / '+esc(sys.ram_total_mb||'?')+' MB</div>'
            +'<div class="k">Installed At</div><div class="v">'+esc(sys.installed_at||'?')+'</div>'
            +'<div class="k">Report Reason</div><div class="v">'+reasonTag(report.reason)+'</div>'
            +'<div class="k">Report Time</div><div class="v">'+esc(report.timestamp)+'</div>'
            +'</div></div></div>';

        /* Services */
        html += '<div class="card"><div class="card-header"><h2>&#x2699;&#xFE0F; Services</h2></div><div class="card-body"><div class="svc-grid">';
        var svcEntries = Object.entries(services);
        for (var i = 0; i < svcEntries.length; i++) {
            var name = svcEntries[i][0], info = svcEntries[i][1];
            var active = info.active || '?';
            var cls = active === 'active' ? 'svc-active' : active === 'failed' ? 'svc-failed' : 'svc-inactive';
            html += '<div class="svc '+cls+'"><strong>'+esc(name)+'</strong><br><span style="font-size:11px">'+esc(info.enabled)+'/'+esc(active)+'</span></div>';
        }
        html += '</div></div></div>';

        /* Network */
        html += '<div class="card"><div class="card-header"><h2>&#x1F310; Network</h2></div><div class="card-body"><div class="kv">';
        if (network.interfaces) {
            html += '<div class="k">Interfaces</div><div class="v log-block" style="max-height:100px;margin:0">'+esc(network.interfaces)+'</div>';
        }
        if (network.wifi_saved) {
            html += '<div class="k">Saved WiFi</div><div class="v">'+esc(network.wifi_saved)+'</div>';
        }
        html += '<div class="k">Internet</div><div class="v">'+(network.internet==='OK'?'<span class="tag tag-ok">OK</span>':'<span class="tag tag-err">FAIL</span>')+'</div>';
        html += '<div class="k">DNS</div><div class="v">'+(String(network.dns||'').indexOf('OK')>=0?'<span class="tag tag-ok">OK</span>':'<span class="tag tag-err">FAIL</span>')+'</div>';
        html += '</div></div></div>';

        /* Tabs */
        html += '<div class="tabs" id="detail-tabs">'
            +'<div class="tab active" data-tab="errors">Errors ('+errors.length+')</div>'
            +'<div class="tab" data-tab="boot">Boot Log</div>'
            +'<div class="tab" data-tab="journals">Service Journals</div>'
            +'<div class="tab" data-tab="dmesg">dmesg</div>'
            +'<div class="tab" data-tab="history">History ('+history.length+')</div>'
            +'<div class="tab" data-tab="raw">Raw JSON</div>'
            +'</div><div id="tab-content"></div>';

        app.innerHTML = html;

        /* Tab switching */
        var tabs = app.querySelectorAll('#detail-tabs .tab');
        var tabContent = app.querySelector('#tab-content');

        function showTab(name) {
            for (var j = 0; j < tabs.length; j++) tabs[j].classList.toggle('active', tabs[j].dataset.tab === name);
            if (name === 'errors') renderErrors(tabContent, errors);
            else if (name === 'boot') renderBootLog(tabContent, report.boot_log);
            else if (name === 'journals') renderJournals(tabContent, journals);
            else if (name === 'dmesg') renderDmesg(tabContent, report.dmesg);
            else if (name === 'history') renderHistory(tabContent, deviceId, history);
            else if (name === 'raw') renderRaw(tabContent, report);
        }

        for (var t = 0; t < tabs.length; t++) {
            tabs[t].addEventListener('click', (function(tab){ return function(){ showTab(tab.dataset.tab); }; })(tabs[t]));
        }
        showTab(errors.length ? 'errors' : 'boot');

    } catch(e) {
        app.innerHTML = '<div class="empty"><h3 style="color:var(--red)">Error</h3><p>'+esc(e.message)+'</p></div>';
    }
}

function renderErrors(container, errors) {
    if (!errors || !errors.length) {
        container.innerHTML = '<div class="card"><div class="card-body empty">No errors &#x2705;</div></div>';
        return;
    }
    var h = '<div class="card"><div class="card-body" style="padding:0;max-height:500px;overflow-y:auto">';
    for (var i = errors.length - 1; i >= 0; i--) {
        var e = errors[i];
        h += '<div class="err-item"><span class="err-time">'+esc(e.time||'?')+'</span>'
            +'<span class="tag tag-'+(e.level==='error'?'err':'warn')+'">'+esc(e.level)+'</span> '
            +'<span class="err-cat">'+esc(e.category||'')+'</span>'
            +'<div class="err-msg">'+esc(e.message)+'</div>';
        if (e.details) h += '<div style="font-size:11px;color:var(--text3);margin-top:4px">'+esc(JSON.stringify(e.details))+'</div>';
        h += '</div>';
    }
    h += '</div></div>';
    container.innerHTML = h;
}

function colorizeLog(text) {
    if (!text) return '';
    return text.split('\n').map(function(line) {
        var cls = '';
        if (/\[✗\]|\[!\]|ERROR|failed|FAIL/i.test(line)) cls = 'line-err';
        else if (/warn|WARNING/i.test(line)) cls = 'line-warn';
        else if (/\[✓\]|OK|done|complete/i.test(line)) cls = 'line-ok';
        else if (/\[i\]|INFO|LOG:/i.test(line)) cls = 'line-info';
        return cls ? '<span class="'+cls+'">'+esc(line)+'</span>' : esc(line);
    }).join('\n');
}

function renderBootLog(container, bootLog) {
    if (!bootLog) {
        container.innerHTML = '<div class="card"><div class="card-body empty">No boot log in this report</div></div>';
        return;
    }
    container.innerHTML = '<div class="card"><div class="card-body">'
        +'<div class="log-block" style="max-height:600px">'+colorizeLog(bootLog)+'</div></div></div>';
}

function renderJournals(container, journals) {
    if (!journals || !Object.keys(journals).length) {
        container.innerHTML = '<div class="card"><div class="card-body empty">No journal entries in this report</div></div>';
        return;
    }
    var h = '';
    var entries = Object.entries(journals);
    for (var i = 0; i < entries.length; i++) {
        h += '<div class="card"><div class="card-header"><h2>'+esc(entries[i][0])+'</h2></div>'
            +'<div class="card-body"><div class="log-block" style="max-height:350px">'+colorizeLog(entries[i][1])+'</div></div></div>';
    }
    container.innerHTML = h;
}

function renderDmesg(container, dmesg) {
    if (!dmesg) {
        container.innerHTML = '<div class="card"><div class="card-body empty">No dmesg in this report</div></div>';
        return;
    }
    container.innerHTML = '<div class="card"><div class="card-body">'
        +'<div class="log-block" style="max-height:600px">'+colorizeLog(dmesg)+'</div></div></div>';
}

function renderHistory(container, deviceId, reports) {
    if (!reports.length) {
        container.innerHTML = '<div class="card"><div class="card-body empty">No history</div></div>';
        return;
    }
    var h = '<div class="card"><table><thead><tr><th>Timestamp</th><th>Reason</th><th>Size</th><th></th></tr></thead><tbody>';
    for (var i = 0; i < reports.length; i++) {
        var r = reports[i];
        var ts = r.filename.replace('.json','');
        var parts = ts.split('_');
        var dateStr = parts.length >= 3 ? parts[0].slice(0,4)+'-'+parts[0].slice(4,6)+'-'+parts[0].slice(6,8)+' '+parts[1].slice(0,2)+':'+parts[1].slice(2,4)+':'+parts[1].slice(4,6) : ts;
        var kb = (r.size / 1024).toFixed(1);
        h += '<tr class="clickable" onclick="navigate(\'report\',\''+esc(deviceId)+'\',\''+esc(r.filename)+'\')">'
            +'<td>'+esc(dateStr)+'</td><td>'+reasonTag(r.reason)+'</td><td>'+kb+' KB</td>'
            +'<td><button class="btn btn-ghost" onclick="event.stopPropagation();deleteReport(\''+esc(deviceId)+'\',\''+esc(r.filename)+'\')" title="Delete">&#x1F5D1;</button></td>'
            +'</tr>';
    }
    h += '</tbody></table></div>';
    container.innerHTML = h;
}

function renderRaw(container, report) {
    container.innerHTML = '<div class="card"><div class="card-body">'
        +'<div class="log-block" style="max-height:700px">'+esc(JSON.stringify(report, null, 2))+'</div></div></div>';
}

/* ══════════════ Single Report View ══════════════ */

async function renderReport(deviceId, filename) {
    currentDevice = deviceId;
    crumb.innerHTML = '<a href="#" onclick="navigate(\'home\');return false">Devices</a> &rsaquo; '
        +'<a href="#" onclick="navigate(\'device\',\''+esc(deviceId)+'\');return false">'+esc(deviceId).slice(0,16)+'&hellip;</a> &rsaquo; '+esc(filename);
    app.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';

    try {
        var report = await api('/api/device-logs/'+deviceId+'/'+filename);
        var sys = report.system || {};
        var errors = report.errors || [];
        var journals = report.journals || {};

        var html = '<div class="card"><div class="card-header">'
            +'<h2>&#x1F4C4; '+esc(filename)+'</h2>'
            +'<div><button class="btn btn-ghost" onclick="navigate(\'device\',\''+esc(deviceId)+'\')">&#x2190; Back</button></div>'
            +'</div><div class="card-body"><div class="kv">'
            +'<div class="k">Device</div><div class="v">'+esc(sys.hostname)+' ('+esc(sys.ip)+')</div>'
            +'<div class="k">Version</div><div class="v">v'+esc(sys.ethos_version)+'</div>'
            +'<div class="k">Reason</div><div class="v">'+reasonTag(report.reason)+'</div>'
            +'<div class="k">Timestamp</div><div class="v">'+esc(report.timestamp)+'</div>'
            +'<div class="k">Uptime</div><div class="v">'+uptime(sys.uptime_seconds)+'</div>'
            +'</div></div></div>';

        html += '<div class="tabs" id="report-tabs">'
            +'<div class="tab active" data-tab="errors">Errors ('+errors.length+')</div>'
            +'<div class="tab" data-tab="boot">Boot Log</div>'
            +'<div class="tab" data-tab="journals">Journals</div>'
            +'<div class="tab" data-tab="dmesg">dmesg</div>'
            +'<div class="tab" data-tab="raw">Raw</div>'
            +'</div><div id="report-tab-content"></div>';

        app.innerHTML = html;
        var tabs = app.querySelectorAll('#report-tabs .tab');
        var tc = app.querySelector('#report-tab-content');

        function showTab(name) {
            for (var j = 0; j < tabs.length; j++) tabs[j].classList.toggle('active', tabs[j].dataset.tab === name);
            if (name === 'errors') renderErrors(tc, errors);
            else if (name === 'boot') renderBootLog(tc, report.boot_log);
            else if (name === 'journals') renderJournals(tc, journals);
            else if (name === 'dmesg') renderDmesg(tc, report.dmesg);
            else if (name === 'raw') renderRaw(tc, report);
        }
        for (var t = 0; t < tabs.length; t++) {
            tabs[t].addEventListener('click', (function(tab){ return function(){ showTab(tab.dataset.tab); }; })(tabs[t]));
        }
        showTab(errors.length ? 'errors' : 'boot');
    } catch(e) {
        app.innerHTML = '<div class="empty"><h3 style="color:var(--red)">Error</h3><p>'+esc(e.message)+'</p></div>';
    }
}

/* ══════════════ Actions ══════════════ */

async function deleteDevice(deviceId) {
    if (!confirm('Delete ALL logs for this device?')) return;
    await apiDelete('/api/device-logs/'+deviceId);
    navigate('home');
}

async function deleteReport(deviceId, filename) {
    if (!confirm('Delete this report?')) return;
    await apiDelete('/api/device-logs/'+deviceId+'/'+filename);
    navigate('device', deviceId);
}

/* ══════════════ Auto-refresh (30s on device list) ══════════════ */

setInterval(function() {
    if (currentView === 'home') renderDeviceList();
}, 30000);

/* ══════════════ Init ══════════════ */
navigate('home');
</script>
</body>
</html>'''


if __name__ == '__main__':
    print(f'\n  EthOS Log Receiver & Dashboard')
    print(f'  Logs directory: {LOG_DIR}')
    print(f'  Dashboard:      http://0.0.0.0:5050\n')
    app.run(host='0.0.0.0', port=5050, debug=True)
