#!/usr/bin/env python3
"""
Comprehensive VM installation tests.

Tests EthOS image building and installation across different disk configurations:
- Same disk (OS + data on one disk)
- Separate disks (OS on one, data on another)
- Different disk bus types: virtio, scsi, sata, ide
- Different disk formats: qcow2, raw

Each test:
1. Creates a VM with the EthOS boot image + target disk(s)
2. Starts the VM
3. Waits for the installer to become reachable
4. Runs disk discovery
5. Executes installation via the installer REST API
6. Monitors progress to completion
7. Verifies the installation succeeded
8. Cleans up
"""

import json
import os
import time
import sys
import requests
import sqlite3
import subprocess

# ── Configuration ──────────────────────────────────────────────────────────
BASE_URL = os.environ.get('ETHOS_BASE_URL', 'http://localhost:9000')
TOKEN = os.environ.get('ETHOS_TOKEN', '')

# Get token from DB if not provided
if not TOKEN:
    try:
        conn = sqlite3.connect('/opt/ethos/data/tokens.db')
        TOKEN = conn.execute(
            'SELECT token FROM tokens ORDER BY last_active DESC LIMIT 1'
        ).fetchone()[0]
        conn.close()
    except Exception:
        pass

HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
VM_API = f'{BASE_URL}/api/vm'
IMAGE_PATH = '/opt/ethos/data/vms/_images/ethos-x86.img'

# Test configurations: (name, bus_type, disk_format, data_mode, os_disk_size, data_disk_size)
TEST_CONFIGS = [
    ('test-virtio-same', 'virtio', 'qcow2', 'same', '20G', None),
    ('test-virtio-sep',  'virtio', 'qcow2', 'separate', '20G', '10G'),
    ('test-scsi-same',   'scsi',   'qcow2', 'same', '20G', None),
    ('test-sata-sep',    'sata',   'qcow2', 'separate', '20G', '10G'),
    ('test-ide-same',    'ide',    'qcow2', 'same', '20G', None),
    ('test-raw-same',    'virtio', 'raw',   'same', '20G', None),
]


def api(method, path, data=None, timeout=15):
    """Make an API call to the EthOS server."""
    url = f'{BASE_URL}{path}'
    try:
        if method == 'GET':
            r = requests.get(url, headers=HEADERS, timeout=timeout)
        elif method == 'POST':
            r = requests.post(url, headers=HEADERS, json=data, timeout=timeout)
        elif method == 'PUT':
            r = requests.put(url, headers=HEADERS, json=data, timeout=timeout)
        elif method == 'DELETE':
            r = requests.delete(url, headers=HEADERS, timeout=timeout)
        else:
            raise ValueError(f'Unknown method: {method}')
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {'raw': r.text[:200]}
    except requests.exceptions.ConnectionError:
        return 0, {'error': 'Connection refused'}
    except requests.exceptions.Timeout:
        return 0, {'error': 'Timeout'}


def installer_api(port, method, path, data=None, timeout=10):
    """Make an API call to the installer running inside a VM."""
    url = f'http://localhost:{port}{path}'
    try:
        if method == 'GET':
            r = requests.get(url, timeout=timeout)
        elif method == 'POST':
            r = requests.post(url, json=data, timeout=timeout)
        else:
            raise ValueError(f'Unknown method: {method}')
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {'raw': r.text[:200]}
    except requests.exceptions.ConnectionError:
        return 0, {'error': 'Connection refused'}
    except requests.exceptions.Timeout:
        return 0, {'error': 'Timeout'}


def wait_for_installer(port, timeout=180):
    """Wait for the installer to become reachable."""
    start = time.time()
    while time.time() - start < timeout:
        code, data = installer_api(port, 'GET', '/health')
        if code == 200:
            return True
        time.sleep(3)
    return False


def wait_for_install_complete(port, timeout=600):
    """Poll installation progress until complete or error."""
    start = time.time()
    last_pct = -1
    while time.time() - start < timeout:
        code, data = installer_api(port, 'GET', '/api/install/progress')
        if code != 200:
            time.sleep(2)
            continue
        pct = data.get('percent', 0)
        phase = data.get('phase', '?')
        msg = data.get('message', '')
        if pct != last_pct:
            print(f'    [{pct:3d}%] {phase}: {msg}')
            last_pct = pct
        if data.get('done'):
            return True, data
        if data.get('error'):
            return False, data
        time.sleep(2)
    return False, {'error': 'Timeout waiting for install'}


def create_vm(name, bus, disk_format, os_disk_size='20G'):
    """Create a VM with the EthOS boot image."""
    data = {
        'name': name,
        'cpu': 2,
        'ram': 2048,
        'disk_size': os_disk_size,
        'disk_format': disk_format,
        'disk_bus': bus,
        'os_type': 'linux',
        'boot_image': IMAGE_PATH,
        'description': f'Auto-test: bus={bus} fmt={disk_format}',
    }
    code, resp = api('POST', '/api/vm/machines', data)
    if code != 200:
        return None, resp
    return resp.get('id'), resp


def add_disk(vm_id, size, disk_format='qcow2', bus='virtio'):
    """Add an additional disk to a VM."""
    data = {
        'size': size,
        'format': disk_format,
        'bus': bus,
    }
    code, resp = api('POST', f'/api/vm/machines/{vm_id}/disks', data)
    return code, resp


def start_vm(vm_id):
    """Start a VM."""
    code, resp = api('POST', f'/api/vm/machines/{vm_id}/start')
    return code, resp


def stop_vm(vm_id):
    """Stop a VM."""
    code, resp = api('POST', f'/api/vm/machines/{vm_id}/stop')
    return code, resp


def delete_vm(vm_id):
    """Delete a VM and its files."""
    code, resp = api('DELETE', f'/api/vm/machines/{vm_id}')
    return code, resp


def get_vm(vm_id):
    """Get VM details from the list endpoint (no single-VM GET exists)."""
    code, resp = api('GET', '/api/vm/machines')
    if code != 200:
        return code, resp
    vms = resp if isinstance(resp, list) else resp.get('items', [])
    for vm in vms:
        if vm.get('id') == vm_id:
            return 200, vm
    return 404, {'error': f'VM {vm_id} not found'}


def get_ethos_port(vm_id):
    """Get the host port mapped to guest port 9000 (EthOS/Installer)."""
    code, vm = get_vm(vm_id)
    if code != 200:
        return None
    for pf in vm.get('network', {}).get('port_forwards', []):
        if pf.get('guest') == 9000 and pf.get('host'):
            return pf['host']
    return None


def get_ssh_port(vm_id):
    """Get the host port mapped to guest port 22 (SSH)."""
    code, vm = get_vm(vm_id)
    if code != 200:
        return None
    for pf in vm.get('network', {}).get('port_forwards', []):
        if pf.get('guest') == 22 and pf.get('host'):
            return pf['host']
    return None


def run_test(name, bus, disk_format, data_mode, os_size, data_size):
    """Run a single installation test."""
    print(f'\n{"="*60}')
    print(f'TEST: {name}')
    print(f'  Bus: {bus} | Format: {disk_format} | Data: {data_mode}')
    print(f'  OS disk: {os_size} | Data disk: {data_size or "same"}')
    print(f'{"="*60}')

    vm_id = None
    try:
        # Step 1: Create VM
        print('\n[1] Creating VM...')
        vm_id, resp = create_vm(name, bus, disk_format, os_size)
        if not vm_id:
            print(f'  FAIL: {resp}')
            return False, 'VM creation failed'
        print(f'  OK: VM ID = {vm_id}')
        print(f'  Message: {resp.get("message", "")}')

        # Step 1b: Add data disk if separate
        if data_mode == 'separate' and data_size:
            print(f'\n[1b] Adding data disk ({data_size}, {bus})...')
            code, resp = add_disk(vm_id, data_size, disk_format, bus)
            if code != 200:
                print(f'  FAIL: {resp}')
                return False, 'Data disk creation failed'
            print(f'  OK: Data disk added')

        # Step 2: Get port mapping
        print('\n[2] Checking port mapping...')
        port = get_ethos_port(vm_id)
        if not port:
            print('  FAIL: No port 9000 forward found')
            return False, 'No port mapping'
        print(f'  OK: Installer accessible on localhost:{port}')

        # Step 3: Start VM
        print('\n[3] Starting VM...')
        code, resp = start_vm(vm_id)
        if code != 200:
            print(f'  FAIL: {resp}')
            return False, 'VM start failed'
        print(f'  OK: VM started')

        # Step 4: Wait for installer
        print(f'\n[4] Waiting for installer on port {port} (up to 3 min)...')
        if not wait_for_installer(port, timeout=180):
            print('  FAIL: Installer not reachable after 3 minutes')
            # Grab serial output for debugging
            return False, 'Installer not reachable'
        print('  OK: Installer is reachable!')

        # Step 5: Discover disks
        print('\n[5] Discovering disks...')
        code, disks_data = installer_api(port, 'GET', '/api/disks/discover')
        if code != 200:
            print(f'  FAIL: {code} {disks_data}')
            return False, 'Disk discovery failed'

        disks = disks_data.get('disks', [])
        boot_dev = disks_data.get('boot_device', '')
        print(f'  Boot device: {boot_dev}')
        print(f'  Available disks ({len(disks)}):')
        for d in disks:
            print(f'    {d["name"]} - {d.get("size_human","?")} - '
                  f'transport={d.get("transport","?")} boot={d.get("is_boot",False)}')

        # Select target disks (non-boot disks)
        targets = [d for d in disks if not d.get('is_boot')]
        if not targets:
            print('  FAIL: No non-boot disks found')
            return False, 'No target disks'

        os_disk = targets[0]['name']
        data_disk_name = 'same'
        if data_mode == 'separate' and len(targets) > 1:
            data_disk_name = targets[1]['name']
            print(f'  Selected: OS={os_disk}, Data={data_disk_name}')
        else:
            print(f'  Selected: OS={os_disk}, Data=same')

        # Step 6: Validate disk selection
        print('\n[6] Validating disk selection...')
        code, val = installer_api(port, 'POST', '/api/disks/validate', {
            'os_disk': os_disk,
            'data_disk': data_disk_name if data_mode == 'separate' else None,
            'boot_device': boot_dev,
        })
        if code != 200:
            print(f'  FAIL: {code} {val}')
            return False, f'Disk validation failed: {val}'
        if not val.get('ok'):
            errs = val.get('errors', [])
            print(f'  FAIL: Validation errors: {errs}')
            return False, f'Disk validation errors: {errs}'
        warnings = val.get('warnings', [])
        if warnings:
            print(f'  Warnings: {warnings}')
        print('  OK: Disk selection valid')

        # Step 7: Start installation
        print('\n[7] Starting installation...')
        install_data = {
            'os_disk': os_disk,
            'data_disk': data_disk_name if data_mode == 'separate' else 'same',
            'username': 'testadmin',
            'password': 'test1234',
            'hostname': name,
            'lang': 'en',
            'confirmation': 'INSTALL',
        }
        code, resp = installer_api(port, 'POST', '/api/install/start', install_data)
        if code not in (200, 202):
            print(f'  FAIL: {code} {resp}')
            return False, f'Install start failed: {resp}'
        print('  OK: Installation started')

        # Step 8: Monitor progress
        print('\n[8] Monitoring installation progress...')
        ok, result = wait_for_install_complete(port, timeout=600)
        if not ok:
            err = result.get('error', 'Unknown error')
            print(f'  FAIL: {err}')
            # Get logs
            _, logs = installer_api(port, 'GET', '/api/install/logs')
            if logs.get('logs'):
                print('  Last 10 log entries:')
                for entry in logs['logs'][-10:]:
                    print(f'    {entry.get("msg", "")}')
            return False, f'Installation failed: {err}'

        print('  OK: Installation completed successfully!')
        new_ip = result.get('new_ip', 'unknown')
        print(f'  New IP: {new_ip}')

        return True, 'Installation succeeded'

    except Exception as e:
        print(f'\n  EXCEPTION: {e}')
        import traceback
        traceback.print_exc()
        return False, str(e)

    finally:
        # Cleanup
        if vm_id:
            print(f'\n[Cleanup] Stopping and deleting VM {vm_id}...')
            stop_vm(vm_id)
            time.sleep(3)
            delete_vm(vm_id)
            print('  Done.')


def main():
    print('='*60)
    print('EthOS VM Installation Comprehensive Tests')
    print('='*60)

    # Verify prerequisites
    print('\nChecking prerequisites...')
    code, resp = api('GET', '/api/auth/verify')
    if code != 200 or not resp.get('valid'):
        print(f'FATAL: Auth failed: {resp}')
        sys.exit(1)
    print(f'  Auth: OK (user={resp["user"]["username"]})')

    if not os.path.isfile(IMAGE_PATH):
        print(f'FATAL: Boot image not found: {IMAGE_PATH}')
        sys.exit(1)
    print(f'  Image: OK ({IMAGE_PATH})')

    # Check QEMU
    qemu = subprocess.run(['which', 'qemu-system-x86_64'],
                          capture_output=True, text=True)
    if qemu.returncode != 0:
        print('FATAL: qemu-system-x86_64 not found')
        sys.exit(1)
    print('  QEMU: OK')

    kvm = os.path.exists('/dev/kvm')
    print(f'  KVM: {"OK" if kvm else "NOT AVAILABLE (tests will be slow)"}')

    # Select which tests to run
    configs = TEST_CONFIGS
    if len(sys.argv) > 1:
        # Allow filtering by name prefix
        filt = sys.argv[1]
        configs = [c for c in configs if filt in c[0]]
        if not configs:
            print(f'No tests match filter: {filt}')
            sys.exit(1)

    # Run tests
    results = []
    for cfg in configs:
        ok, msg = run_test(*cfg)
        results.append((cfg[0], ok, msg))

    # Summary
    print('\n' + '='*60)
    print('TEST RESULTS SUMMARY')
    print('='*60)
    passed = 0
    for name, ok, msg in results:
        status = 'PASS ✓' if ok else 'FAIL ✗'
        print(f'  {status}  {name}: {msg}')
        if ok:
            passed += 1
    print(f'\n{passed}/{len(results)} tests passed')
    sys.exit(0 if passed == len(results) else 1)


if __name__ == '__main__':
    main()
