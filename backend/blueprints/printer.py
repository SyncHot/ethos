"""
EthOS — Printer Blueprint
Print server with CUPS integration, document conversion, printer keepalive,
network printer discovery and full printer management (add/remove/enable/disable).
"""

import os
import re
import socket
import subprocess
import threading
import time
import uuid
import json as _json
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import app_path, NATIVE_MODE, ensure_dep, check_dep
from utils import register_pkg_routes

printer_bp = Blueprint('printer', __name__, url_prefix='/api/printer')

UPLOAD_FOLDER = os.environ.get('PRINTER_UPLOAD_FOLDER', app_path('uploads'))
CUPS_SERVER = os.environ.get('CUPS_SERVER', 'localhost:631')
os.environ['CUPS_SERVER'] = CUPS_SERVER

KEEPALIVE_INTERVAL = int(os.environ.get('KEEPALIVE_INTERVAL', '300'))

ALLOWED_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'odt', 'ods', 'odp', 'txt', 'rtf',
    'jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff'
}

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# =====================================================================
# Helpers — low-level CUPS & network utilities
# =====================================================================

def get_printer_uris():
    """Map printer-name → device URI from lpstat -v."""
    uris = {}
    try:
        result = subprocess.run(['lpstat', '-v'], capture_output=True, text=True, timeout=10)
        for line in result.stdout.strip().split('\n'):
            m = re.match(r'device for (.+?):\s+(.+)', line)
            if m:
                uris[m.group(1)] = m.group(2).strip()
    except Exception as e:
        print(f"[printer] Error getting printer URIs: {e}")
    return uris


def parse_socket_uri(uri):
    m = re.match(r'socket://([^:/]+)(?::(\d+))?', uri or '')
    if m:
        return m.group(1), int(m.group(2) or 9100)
    return None


def _tcp_probe(ip, port, timeout=5):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


CUPS_CONTAINER = os.environ.get('CUPS_CONTAINER', 'cups')

from host import NATIVE_MODE


def _cups_installed():
    """Check if CUPS daemon is available on the system."""
    return check_dep('lpadmin')


def _cups_admin(cmd_args, timeout=15, stdin_data=None):
    """Run a CUPS admin command natively."""
    return subprocess.run(cmd_args, capture_output=True, text=True,
                          timeout=timeout, input=stdin_data)


def _cups_enable(printer_name):
    try:
        _cups_admin(['cupsenable', printer_name])
        _cups_admin(['cupsaccept', printer_name])
    except Exception:
        pass


def wake_printer(printer_name=None):
    uris = get_printer_uris()
    results = {}
    targets = {printer_name: uris.get(printer_name)} if printer_name else uris
    for name, uri in targets.items():
        if not uri:
            results[name] = {'ok': False, 'reason': 'no URI'}
            continue
        addr = parse_socket_uri(uri)
        if not addr:
            results[name] = {'ok': False, 'reason': f'unsupported URI: {uri}'}
            continue
        ip, port = addr
        tcp_ok = _tcp_probe(ip, port)
        _cups_enable(name)
        results[name] = {'ok': tcp_ok, 'ip': ip, 'port': port}
    return results


# ---------- Keepalive thread ----------
def _keepalive_loop():
    print(f"[printer] Keepalive thread started (interval={KEEPALIVE_INTERVAL}s)")
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            wake_printer()
        except Exception as e:
            print(f"[printer] Keepalive error: {e}")


_keepalive_thread = threading.Thread(target=_keepalive_loop, daemon=True)
_keepalive_thread.start()


# =====================================================================
# Printer listing / status
# =====================================================================

def get_printers_detailed():
    """Return rich printer list with URI, status, reachability, default flag."""
    printers = []
    default = get_default_printer()
    uris = get_printer_uris()

    try:
        result = subprocess.run(['lpstat', '-p'], capture_output=True, text=True, timeout=10)
        for line in result.stdout.strip().split('\n'):
            if not line.startswith('printer '):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1]
            idle = 'idle' in line.lower()
            printing = 'printing' in line.lower()
            uri = uris.get(name, '')
            addr = parse_socket_uri(uri)
            reachable = None
            ip = None
            if addr:
                ip, port = addr
                reachable = _tcp_probe(ip, port, timeout=2)
            printers.append({
                'name': name,
                'status': 'printing' if printing else ('idle' if idle else 'disabled'),
                'is_default': name == default,
                'uri': uri,
                'ip': ip,
                'reachable': reachable,
                'description': line,
            })
    except Exception:
        pass
    return printers


def get_default_printer():
    try:
        result = subprocess.run(['lpstat', '-d'], capture_output=True, text=True, timeout=10)
        if 'system default destination:' in result.stdout:
            return result.stdout.split(':')[1].strip()
    except Exception:
        pass
    return None


# =====================================================================
# Network discovery
# =====================================================================

def discover_network_printers():
    """
    Discover printers via:
      1. CUPS lpinfo -v (backends: socket://, ipp://, ipps://, hp, dnssd, …)
      2. Subnet scan for JetDirect (9100), IPP (631)
      3. Web panel detection (80, 443, 8080) — identifies printers by HTTP headers
    Returns de-duped list of {uri, protocol, ip, info}.
    """
    found = {}  # uri → info dict

    # --- 1. lpinfo -v (run inside CUPS container for best results) ---
    try:
        r = _cups_admin(['lpinfo', '-v', '--timeout', '10'], timeout=20)
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            kind, uri = parts
            if uri in ('file:///dev/null', 'cups-brf:/'):
                continue
            if uri.startswith('serial:'):
                continue
            # Skip generic backends (no IP)
            if '://' not in uri and ':' not in uri:
                continue
            ip = _extract_ip_from_uri(uri)
            found[uri] = {
                'uri': uri,
                'protocol': kind.replace('network', 'network').replace('direct', 'direct'),
                'ip': ip,
                'info': _guess_label(uri),
            }
    except Exception as e:
        print(f"[printer] lpinfo error: {e}")

    # --- 2 & 3. Subnet scan: JetDirect, IPP, and web panels ---
    try:
        my_ip = _get_local_ip()
        if my_ip:
            subnet = '.'.join(my_ip.split('.')[:3]) + '.'
            threads = []
            lock = threading.Lock()

            def probe_port(ip, port, proto):
                """Probe a single port and register if open."""
                if not _tcp_probe(ip, port, timeout=1):
                    return
                if port == 9100:
                    uri = f'socket://{ip}:9100'
                    label = f'JetDirect @ {ip}'
                elif port == 631:
                    uri = f'ipp://{ip}:631/ipp/print'
                    label = f'IPP @ {ip}'
                elif port in (80, 443, 8080):
                    # Web panel — try to identify as printer via HTTP
                    info = _http_identify_printer(ip, port)
                    if not info:
                        return  # Not a printer, skip
                    # Build best URI: try common print ports, fallback to socket
                    if _tcp_probe(ip, 631, timeout=1):
                        uri = f'ipp://{ip}:631/ipp/print'
                    elif _tcp_probe(ip, 9100, timeout=1):
                        uri = f'socket://{ip}:9100'
                    else:
                        # No standard print port — use socket as default
                        # (user may need to enable JetDirect/IPP on the printer)
                        uri = f'socket://{ip}:9100'
                    label = info
                    proto = 'web'
                else:
                    return
                with lock:
                    if uri not in found:
                        found[uri] = {
                            'uri': uri,
                            'protocol': proto,
                            'ip': ip,
                            'info': label,
                        }

            for i in range(1, 255):
                ip = subnet + str(i)
                if ip == my_ip:
                    continue
                threads.append(threading.Thread(target=probe_port, args=(ip, 9100, 'socket')))
                threads.append(threading.Thread(target=probe_port, args=(ip, 631, 'ipp')))
                threads.append(threading.Thread(target=probe_port, args=(ip, 80, 'http')))
                threads.append(threading.Thread(target=probe_port, args=(ip, 8080, 'http')))

            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=8)
    except Exception as e:
        print(f"[printer] Subnet scan error: {e}")

    # Filter out already-installed URIs
    installed_uris = set(get_printer_uris().values())
    results = []
    for uri, info in found.items():
        info['installed'] = uri in installed_uris
        results.append(info)

    return sorted(results, key=lambda x: (x.get('installed', False), x.get('uri', '')))


def _http_identify_printer(ip, port):
    """Try to identify a device as a printer via HTTP headers/content.
    Returns a label string if printer detected, None otherwise.
    """
    import urllib.request
    scheme = 'https' if port == 443 else 'http'
    url = f'{scheme}://{ip}:{port}/'
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={'User-Agent': 'EthOS/1.0'})
        resp = urllib.request.urlopen(req, timeout=3, context=ctx)
        server = resp.headers.get('Server', '').lower()
        content = resp.read(4096).decode('utf-8', errors='ignore').lower()
        resp.close()
    except Exception:
        # Even 403/404 responses can identify a printer
        try:
            import http.client
            if port == 443:
                conn = http.client.HTTPSConnection(ip, port, timeout=3,
                                                    context=ctx if 'ctx' in dir() else None)
            else:
                conn = http.client.HTTPConnection(ip, port, timeout=3)
            conn.request('GET', '/')
            resp = conn.getresponse()
            server = resp.getheader('Server', '').lower()
            content = resp.read(4096).decode('utf-8', errors='ignore').lower()
            conn.close()
        except Exception:
            return None

    # Known printer server signatures
    printer_signatures = [
        'webserver',      # Samsung SyncThru
        'printer',
        'cups',
        'epson',
        'hp-httpd',       # HP printers
        'canon',
        'brother',
        'xerox',
        'lexmark',
        'konica',
        'ricoh',
        'syncthru',
        'ews',            # Embedded Web Server (HP)
    ]

    # Check server header
    for sig in printer_signatures:
        if sig in server:
            return f'Drukarka @ {ip} ({server})'

    # Check page content
    content_hints = ['printer', 'drukark', 'syncthru', 'samsung', 'toner',
                     'cartridge', 'print', 'fuser', 'ipp', 'jet direct']
    for hint in content_hints:
        if hint in content:
            return f'Drukarka @ {ip} (web:{port})'

    return None


def _extract_ip_from_uri(uri):
    m = re.search(r'://([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', uri)
    return m.group(1) if m else None


def _guess_label(uri):
    ip = _extract_ip_from_uri(uri)
    if 'dnssd' in uri:
        # dnssd://Printer%20Name._ipp._tcp.local/…
        m = re.search(r'dnssd://(.+?)\._', uri)
        name = m.group(1).replace('%20', ' ') if m else uri
        return f'{name} (mDNS/AirPrint)'
    if uri.startswith('socket://'):
        return f'JetDirect @ {ip or uri}'
    if uri.startswith('ipp://') or uri.startswith('ipps://'):
        return f'IPP @ {ip or uri}'
    if uri.startswith('hp:'):
        return 'HP printer (HPLIP)'
    return uri


def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


# =====================================================================
# PPD / driver listing
# =====================================================================

def get_available_drivers(make_filter=None):
    """List PPDs known to CUPS (lpinfo -m via CUPS container). Optionally filter by make."""
    drivers = []
    try:
        r = _cups_admin(['lpinfo', '-m'], timeout=30)
        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            ppd, desc = parts
            if make_filter and make_filter.lower() not in desc.lower():
                continue
            drivers.append({'ppd': ppd, 'description': desc})
    except Exception as e:
        print(f"[printer] lpinfo -m error: {e}")
    return drivers


# =====================================================================
# Printer management (add / remove / configure)
# =====================================================================

def _query_ipp_make_model(uri):
    """Query printer make-and-model via ipptool (inside CUPS container)."""
    if not uri or not (uri.startswith('ipp://') or uri.startswith('ipps://')):
        return None
    try:
        r = _cups_admin([
            'ipptool', '-tv', uri,
            '-d', 'uri=' + uri,
            '/dev/stdin'
        ], timeout=10, stdin_data='{\n  OPERATION Get-Printer-Attributes\n  GROUP operation-attributes-tag\n  ATTR charset attributes-charset utf-8\n  ATTR naturalLanguage attributes-natural-language en\n  ATTR uri printer-uri $uri\n  ATTR keyword requested-attributes printer-make-and-model\n  STATUS successful-ok\n  DISPLAY printer-make-and-model\n}')
        if r.returncode == 0:
            for line in r.stdout.split('\n'):
                if 'printer-make-and-model' in line:
                    # Format: "  printer-make-and-model (nameWithoutLanguage) = Samsung M283x Series"
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        val = parts[1].strip().strip('"')
                        if val:
                            print(f"[printer] IPP reports make-model: {val}")
                            return val
    except Exception as e:
        print(f"[printer] IPP make-model query error: {e}")
    return None


def _find_best_driver(make_model):
    """Search lpinfo -m for the best PPD matching a make/model string.
    Prefer pxlmono (PCL-XL) > PXL > foomatic > anything else.
    """
    if not make_model:
        return None
    try:
        # Try multiple search strategies
        words = re.split(r'[\s_-]+', make_model.strip())
        words = [w for w in words if w]
        if not words:
            return None

        # First filter by make (first word), then try broader search
        candidates = get_available_drivers(words[0])
        if not candidates and len(words) > 1:
            candidates = get_available_drivers(words[1])

        # Extract model tokens (e.g. "M283x" from "Samsung M283x Series")
        model_tokens = [w.lower().replace('-', '') for w in words if len(w) > 1]

        scored = []
        for d in candidates:
            desc_norm = d['description'].lower().replace('-', '').replace(' ', '')
            # Check if model part matches (skip generic words like "series", "printer")
            significant_tokens = [t for t in model_tokens
                                  if t not in ('series', 'printer', 'samsung', 'hp', 'brother',
                                               'canon', 'epson', 'lexmark', 'xerox', 'ricoh')]
            if not significant_tokens:
                significant_tokens = model_tokens[1:2] if len(model_tokens) > 1 else model_tokens

            if not any(t in desc_norm for t in significant_tokens):
                continue

            score = 0
            if 'pxlmono' in desc_norm:
                score = 3
            elif 'pxl' in desc_norm:
                score = 2
            elif 'foomatic' in desc_norm:
                score = 1
            scored.append((score, d))
        if scored:
            scored.sort(key=lambda x: -x[0])
            best = scored[0][1]
            print(f"[printer] Best driver match: {best['ppd']} ({best['description']})")
            return best['ppd']
    except Exception as e:
        print(f"[printer] _find_best_driver error: {e}")
    return None


def _read_ppd_model(printer_name):
    """Read ModelName from a printer's PPD inside the CUPS container."""
    try:
        r = _cups_admin(['cat', f'/etc/cups/ppd/{printer_name}.ppd'], timeout=5)
        if r.returncode == 0:
            for line in r.stdout.split('\n'):
                if line.strip().startswith('*ModelName:'):
                    return line.split('"')[1] if '"' in line else None
    except Exception:
        pass
    return None


def add_printer(name, uri, ppd=None, info=None, location=None, shared=True, set_default=False):
    """Add a printer via lpadmin (executed inside CUPS container).
    If no PPD is specified, tries to auto-detect the best driver.
    For IPP printers: adds with 'everywhere' first to get make/model,
    then upgrades to a proper driver if available.
    """
    name = re.sub(r'[^A-Za-z0-9_-]', '_', name)
    cmd = ['lpadmin', '-p', name, '-v', uri, '-E']

    used_everywhere = False
    if ppd:
        if ppd.startswith('/'):
            cmd.extend(['-P', ppd])
        else:
            cmd.extend(['-m', ppd])
    else:
        # Try auto-detecting from IPP query, user info, or name
        ipp_model = _query_ipp_make_model(uri)
        auto_ppd = (_find_best_driver(ipp_model)
                    or _find_best_driver(info)
                    or _find_best_driver(name.replace('-', ' ').replace('_', ' ')))
        if auto_ppd:
            print(f"[printer] Auto-selected driver: {auto_ppd}")
            cmd.extend(['-m', auto_ppd])
        elif uri.startswith('ipp://') or uri.startswith('ipps://') or uri.startswith('dnssd://'):
            cmd.extend(['-m', 'everywhere'])
            used_everywhere = True
        else:
            cmd.extend(['-m', 'raw'])

    if info:
        cmd.extend(['-D', info])
    if location:
        cmd.extend(['-L', location])
    if shared:
        cmd.extend(['-o', 'printer-is-shared=true'])

    r = _cups_admin(cmd, timeout=30)
    if r.returncode != 0:
        return {'success': False, 'error': r.stderr.strip() or 'lpadmin failed'}

    # Post-add driver upgrade: if we used 'everywhere', read the PPD to get
    # the real model and try to find a proper native driver (pxlmono, etc.)
    if used_everywhere:
        import time
        time.sleep(1)  # give CUPS a moment to write the PPD
        model = _read_ppd_model(name)
        if model:
            print(f"[printer] PPD reports model: {model}")
            better_ppd = _find_best_driver(model)
            if better_ppd:
                print(f"[printer] Upgrading driver from 'everywhere' to: {better_ppd}")
                upgrade = _cups_admin(['lpadmin', '-p', name, '-m', better_ppd], timeout=15)
                if upgrade.returncode != 0:
                    print(f"[printer] Driver upgrade failed: {upgrade.stderr}")

    _cups_enable(name)

    if set_default:
        _cups_admin(['lpadmin', '-d', name])

    return {'success': True, 'name': name}


def remove_printer(name):
    r = _cups_admin(['lpadmin', '-x', name])
    if r.returncode != 0:
        return {'success': False, 'error': r.stderr.strip() or 'lpadmin -x failed'}
    return {'success': True}


def set_default(name):
    r = _cups_admin(['lpadmin', '-d', name])
    return {'success': r.returncode == 0, 'error': r.stderr.strip() if r.returncode else None}


def enable_printer(name):
    _cups_enable(name)
    return {'success': True}


def disable_printer(name):
    _cups_admin(['cupsdisable', name])
    _cups_admin(['cupsreject', name])
    return {'success': True}


# =====================================================================
# Document conversion & printing
# =====================================================================

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def convert_to_printable(filepath):
    ext = filepath.rsplit('.', 1)[1].lower()

    if ext == 'pdf':
        ps_path = filepath.rsplit('.', 1)[0] + '.ps'
        try:
            subprocess.run(['pdftops', filepath, ps_path], timeout=120, check=True)
            if os.path.exists(ps_path):
                return ps_path
        except Exception:
            pass
        return filepath

    pdf_path = filepath.rsplit('.', 1)[0] + '.pdf'

    if ext in ['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp', 'rtf']:
        try:
            ensure_dep('libreoffice', install=True)
            subprocess.run([
                'libreoffice', '--headless', '--convert-to', 'pdf',
                '--outdir', os.path.dirname(filepath), filepath
            ], timeout=120, check=True)
            if os.path.exists(pdf_path):
                return pdf_path
        except Exception:
            pass

    elif ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff']:
        try:
            subprocess.run(['convert', filepath, pdf_path], timeout=60, check=True)
            if os.path.exists(pdf_path):
                return pdf_path
        except Exception:
            pass

    elif ext == 'txt':
        try:
            ps_path = filepath.rsplit('.', 1)[0] + '.ps'
            subprocess.run(['enscript', '-p', ps_path, filepath], timeout=30, check=True)
            if os.path.exists(ps_path):
                return ps_path
        except Exception:
            pass

    return filepath


def print_document(filepath, printer, copies=1, duplex=False):
    try:
        print_path = convert_to_printable(filepath)
        cmd = ['lp', '-d', printer, '-n', str(copies)]
        if duplex:
            cmd.extend(['-o', 'sides=two-sided-long-edge'])
        else:
            cmd.extend(['-o', 'sides=one-sided'])
        cmd.append(print_path)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            job_id = None
            if 'request id is' in result.stdout:
                job_id = result.stdout.split('request id is')[1].split()[0]
            return {'success': True, 'job_id': job_id, 'message': result.stdout}
        else:
            return {'success': False, 'error': result.stderr or 'Unknown error'}
    except subprocess.TimeoutExpired:
        return {'success': False, 'error': 'Print command timed out'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


# =====================================================================
# API Routes
# =====================================================================

# ----- Printer listing & status -----

@printer_bp.route('/printers')
def api_get_printers():
    printers = get_printers_detailed()
    default = get_default_printer()
    return jsonify({'printers': printers, 'default': default})


@printer_bp.route('/status')
def api_printer_status():
    """Quick aggregate status for the status bar."""
    printers = get_printers_detailed()
    online = [p for p in printers if p.get('reachable')]
    if online:
        return jsonify({
            'printer_online': True,
            'printer_name': ', '.join(p['name'] for p in online),
            'count': len(printers),
            'online_count': len(online),
        })
    return jsonify({
        'printer_online': False,
        'printer_name': None,
        'count': len(printers),
        'online_count': 0,
    })


# ----- Wake -----

def _get_body():
    """Safely parse JSON body, handling double-encoded strings."""
    data = request.get_json(silent=True)
    if isinstance(data, str):
        try:
            data = _json.loads(data)
        except Exception:
            pass
    return data if isinstance(data, dict) else {}


@printer_bp.route('/wake', methods=['POST'])
def api_wake():
    data = _get_body()
    printer_name = data.get('printer')
    results = wake_printer(printer_name)
    return jsonify({'results': results})


# ----- Discovery -----

@printer_bp.route('/discover')
def api_discover():
    printers = discover_network_printers()
    cups_ok = _cups_installed()
    return jsonify({'printers': printers, 'cups_available': cups_ok})


@printer_bp.route('/cups-status')
def api_cups_status():
    """Check if CUPS is installed and running."""
    installed = _cups_installed()
    return jsonify({
        'installed': installed,
        'message': None if installed else 'CUPS is not installed. Install it to add and manage printers.'
    })


@printer_bp.route('/cups-install', methods=['POST'])
def api_cups_install():
    """Install CUPS natively via ensure_dep."""
    if _cups_installed():
        return jsonify({'status': 'ok', 'installed': True})
    ok, msg = ensure_dep('lpadmin', install=True)
    if ok:
        from host import host_run
        host_run("systemctl enable cups && systemctl start cups", timeout=30)
        return jsonify({'status': 'ok'})
    return jsonify({'error': msg}), 500


# ----- Drivers -----

@printer_bp.route('/drivers')
def api_drivers():
    err = _require_cups()
    if err: return err
    make = request.args.get('make', None)
    drivers = get_available_drivers(make)
    return jsonify({'drivers': drivers})


# ----- Management (add / remove / default / enable / disable) -----

def _require_cups():
    """Return error response if CUPS is not installed, else None."""
    if not _cups_installed():
        return jsonify({'success': False, 'error': 'CUPS is not installed. Install CUPS in printer settings.'}), 503
    return None


@printer_bp.route('/add', methods=['POST'])
def api_add_printer():
    err = _require_cups()
    if err: return err
    data = _get_body()
    name = data.get('name', '').strip()
    uri = data.get('uri', '').strip()
    if not name or not uri:
        return jsonify({'success': False, 'error': 'Name and URI are required'}), 400
    result = add_printer(
        name, uri,
        ppd=data.get('ppd'),
        info=data.get('info'),
        location=data.get('location'),
        shared=data.get('shared', True),
        set_default=data.get('set_default', False),
    )
    if result['success']:
        return jsonify(result)
    return jsonify(result), 500


@printer_bp.route('/remove', methods=['POST'])
def api_remove_printer():
    err = _require_cups()
    if err: return err
    data = _get_body()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name required'}), 400
    result = remove_printer(name)
    if result['success']:
        return jsonify(result)
    return jsonify(result), 500


@printer_bp.route('/default', methods=['POST'])
def api_set_default():
    err = _require_cups()
    if err: return err
    data = _get_body()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name required'}), 400
    result = set_default(name)
    return jsonify(result)


@printer_bp.route('/enable', methods=['POST'])
def api_enable():
    err = _require_cups()
    if err: return err
    data = _get_body()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name required'}), 400
    enable_printer(name)
    return jsonify({'success': True})


@printer_bp.route('/disable', methods=['POST'])
def api_disable():
    err = _require_cups()
    if err: return err
    data = _get_body()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name required'}), 400
    disable_printer(name)
    return jsonify({'success': True})


# ----- Print -----

@printer_bp.route('/print', methods=['POST'])
def api_print():
    err = _require_cups()
    if err: return err
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    if not allowed_file(file.filename):
        return jsonify({'success': False, 'error': f'Unsupported format. Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}'}), 400

    printer = request.form.get('printer')
    if not printer:
        return jsonify({'success': False, 'error': 'No printer selected'}), 400

    wake_printer(printer)

    try:
        copies = int(request.form.get('copies', 1))
    except (ValueError, TypeError):
        copies = 1
    duplex = request.form.get('duplex', 'false').lower() == 'true'

    filename = secure_filename(file.filename)
    unique_filename = f"{uuid.uuid4()}_{filename}"
    filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
    file.save(filepath)

    result = print_document(filepath, printer, copies, duplex)

    try:
        os.remove(filepath)
        pdf_path = filepath.rsplit('.', 1)[0] + '.pdf'
        if os.path.exists(pdf_path) and pdf_path != filepath:
            os.remove(pdf_path)
    except Exception:
        pass

    if result['success']:
        return jsonify(result)
    return jsonify(result), 500


# ----- Jobs -----

@printer_bp.route('/jobs')
def api_get_jobs():
    try:
        result = subprocess.run(['lpstat', '-o'], capture_output=True, text=True, timeout=10)
        jobs = []
        for line in result.stdout.strip().split('\n'):
            if line:
                parts = line.split()
                if len(parts) >= 4:
                    jobs.append({
                        'id': parts[0],
                        'user': parts[1] if len(parts) > 1 else 'unknown',
                        'size': parts[2] if len(parts) > 2 else 'unknown',
                        'status': ' '.join(parts[3:]) if len(parts) > 3 else 'pending'
                    })
        return jsonify({'jobs': jobs})
    except Exception as e:
        return jsonify({'jobs': [], 'error': str(e)})


@printer_bp.route('/cancel/<job_id>', methods=['POST'])
def api_cancel_job(job_id):
    try:
        result = subprocess.run(['cancel', job_id], capture_output=True, text=True, timeout=10)
        return jsonify({'success': result.returncode == 0})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ── Package: install / uninstall / status ──

def _printer_on_uninstall(wipe):
    if wipe:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

register_pkg_routes(
    printer_bp,
    install_message='Print server ready.',
    install_deps=['lpadmin'],
    wipe_dirs=[UPLOAD_FOLDER],
    on_uninstall=_printer_on_uninstall,
    status_extras=lambda: {'cups': check_dep('lpadmin')},
    ufw_ports=[(631, 'tcp', 'CUPS Print Server')],
)
