import os
import re
import json
import time
import shutil
import subprocess
from flask import request, jsonify
from blueprints.admin_required import admin_required
from blueprints.vm_manager import (
    vm_bp, _require_qemu,
    _iso_root, _app_path, _allowed_image_roots, _is_allowed_image_path,
    _load_vms, _save_vms,
    _sanitize_name, _vm_dir, _default_network, _human_size,
    host_run, require_tools,
)

# ═══════════════════════════════════════════════════════════
#  ISO / IMAGE MANAGEMENT
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/images')
@admin_required
@_require_qemu
def list_images():
    """List available ISO/IMG/QCOW2 images for booting VMs."""
    iso_dir = _iso_root()
    images = []
    valid_exts = {'.iso', '.img', '.raw', '.qcow2', '.vdi', '.vmdk'}

    for entry in sorted(os.listdir(iso_dir)):
        fpath = os.path.join(iso_dir, entry)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(entry)[1].lower()
        if ext not in valid_exts:
            continue
        stat = os.stat(fpath)
        images.append({
            'name': entry,
            'path': fpath,
            'size': stat.st_size,
            'size_human': _human_size(stat.st_size),
            'type': ext.lstrip('.').upper(),
            'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)),
        })
    return jsonify(images)


@vm_bp.route('/builder-images')
@admin_required
def list_builder_images():
    """List images built by the EthOS Builder (installer/images/)."""
    images_dir = _app_path('installer/images')
    if not os.path.isdir(images_dir):
        return jsonify([])
    valid_exts = {'.iso', '.img', '.raw', '.qcow2'}
    result = []
    for entry in sorted(os.listdir(images_dir)):
        fpath = os.path.join(images_dir, entry)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(entry)[1].lower()
        if ext not in valid_exts:
            continue
        stat = os.stat(fpath)
        result.append({
            'name': entry,
            'path': fpath,
            'size': stat.st_size,
            'size_human': _human_size(stat.st_size),
            'type': ext.lstrip('.').upper(),
            'modified': time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime)),
        })
    return jsonify(result)


@vm_bp.route('/builder-images/copy', methods=['POST'])
@admin_required
@_require_qemu
def copy_builder_image():
    """Copy a builder image into the VM images directory."""
    import shutil as _shutil
    data = request.get_json(force=True) if request.data else {}
    src = data.get('path', '')
    if not src or not os.path.isfile(src):
        return jsonify({'error': 'Source file not found'}), 404
    # Security: only allow files from the builder images directory
    images_dir = os.path.realpath(_app_path('installer/images'))
    real_src = os.path.realpath(src)
    if not real_src.startswith(images_dir + '/'):
        return jsonify({'error': 'Path not allowed'}), 403
    dest = os.path.join(_iso_root(), os.path.basename(src))
    if os.path.exists(dest):
        return jsonify({'error': f'File "{os.path.basename(src)}" already exists in VM images'}), 409
    try:
        _shutil.copy2(real_src, dest)
        return jsonify({'status': 'ok', 'name': os.path.basename(src)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@vm_bp.route('/images', methods=['POST'])
@admin_required
@_require_qemu
def upload_image():
    """Upload an ISO/IMG/QCOW2 image."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No filename provided'}), 400

    valid_exts = {'.iso', '.img', '.raw', '.qcow2', '.vdi', '.vmdk'}
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in valid_exts:
        return jsonify({'error': f'Unsupported format: {ext}. Allowed: {", ".join(valid_exts)}'}), 400

    safe_name = re.sub(r'[^\w\s\-.]', '', f.filename)
    dest = os.path.join(_iso_root(), safe_name)

    f.save(dest)
    return jsonify({'ok': True, 'name': safe_name, 'path': dest})


@vm_bp.route('/images/<path:filename>', methods=['DELETE'])
@admin_required
@_require_qemu
def delete_image(filename):
    """Delete an image file."""
    fpath = os.path.join(_iso_root(), os.path.basename(filename))
    if not os.path.isfile(fpath):
        return jsonify({'error': 'File not found'}), 404

    # Check if any VM uses this image
    vms = _load_vms()
    for vm_id, vm in vms.items():
        if vm.get('boot_image') == fpath:
            return jsonify({'error': f'Image in use by VM "{vm.get("name", vm_id)}"'}), 409

    os.remove(fpath)
    return jsonify({'ok': True})


@vm_bp.route('/import-disk', methods=['POST'])
@admin_required
@_require_qemu
def import_disk():
    """Create a VM from an uploaded or server-path disk image.

    Accepts multipart/form-data (file upload) or JSON with src_path.
    Converts vmdk/vdi/raw/img → qcow2 via qemu-img convert.
    """
    import shutil

    err = require_tools('qemu-img')
    if err:
        return err

    is_upload = 'file' in request.files

    if is_upload:
        f      = request.files['file']
        name   = (request.form.get('name') or '').strip()
        cpu    = int(request.form.get('cpu', 2))
        ram    = int(request.form.get('ram', 2048))
        os_type= request.form.get('os_type', 'linux')
        desc   = request.form.get('description', '')
        do_convert = request.form.get('convert', 'true') == 'true'
    else:
        data   = request.get_json(force=True) if request.data else {}
        name   = (data.get('name') or '').strip()
        cpu    = int(data.get('cpu', 2))
        ram    = int(data.get('ram', 2048))
        os_type= data.get('os_type', 'linux')
        desc   = data.get('description', '')
        do_convert = data.get('convert', True)
        src_path   = (data.get('src_path') or '').strip()

    if not name:
        return jsonify({'error': 'Podaj nazwę VM'}), 400

    # Build VM id/dir
    vm_id  = re.sub(r'-+', '-', _sanitize_name(name).lower().replace(' ', '-'))
    vm_id  = f'{vm_id}-{str(int(time.time()))[-6:]}'
    vm_path = _vm_dir(vm_id)
    os.makedirs(vm_path, exist_ok=True)

    try:
        if is_upload:
            fname = f.filename or 'disk'
            ext   = os.path.splitext(fname)[1].lower()
            valid = {'.qcow2', '.raw', '.vmdk', '.vdi', '.img', '.vhd', '.vhdx'}
            if ext not in valid:
                shutil.rmtree(vm_path, ignore_errors=True)
                return jsonify({'error': f'Nieobsługiwany format: {ext}. Dozwolone: {", ".join(sorted(valid))}'}), 400
            tmp = os.path.join(vm_path, f'import_tmp{ext}')
            f.save(tmp)
            src_path = tmp
        else:
            if not src_path:
                shutil.rmtree(vm_path, ignore_errors=True)
                return jsonify({'error': 'src_path wymagany'}), 400
            real = os.path.realpath(src_path)
            allowed_roots = _allowed_image_roots() + [os.path.realpath(_iso_root())]
            if not any(real.startswith(r + '/') or real == r for r in allowed_roots):
                shutil.rmtree(vm_path, ignore_errors=True)
                return jsonify({'error': 'Ścieżka niedozwolona'}), 403
            src_path = real

        ext = os.path.splitext(src_path)[1].lower()

        if do_convert and ext != '.qcow2':
            disk_file   = os.path.join(vm_path, 'disk.qcow2')
            disk_format = 'qcow2'
            r = host_run(f'qemu-img convert -O qcow2 "{src_path}" "{disk_file}"', timeout=7200)
            if r.returncode != 0:
                raise Exception(f'Konwersja nie powiodła się: {r.stderr[:300]}')
            if is_upload:
                os.remove(src_path)
        else:
            disk_format = {'img': 'raw', 'vhd': 'vpc', 'vhdx': 'vhdx'}.get(ext.lstrip('.'), ext.lstrip('.'))
            disk_file   = os.path.join(vm_path, f'disk{ext}')
            if is_upload:
                os.rename(src_path, disk_file)
            else:
                shutil.copy2(src_path, disk_file)

        # Read actual disk size from image metadata
        try:
            ir = host_run(f'qemu-img info --output=json "{disk_file}"', timeout=30)
            info = json.loads(ir.stdout) if ir.returncode == 0 else {}
            disk_size = _human_size(info.get('virtual-size', 0))
        except Exception:
            disk_size = 'imported'

        vms = _load_vms()
        vms[vm_id] = {
            'name':        name,
            'cpu':         max(1, min(32, cpu)),
            'ram':         max(256, min(65536, ram)),
            'disk_size':   disk_size,
            'disk_format': disk_format,
            'disk_file':   disk_file,
            'disks':       [{'id': 'disk0', 'file': disk_file,
                             'format': disk_format, 'size': disk_size,
                             'bus': 'virtio'}],
            'os_type':     os_type,
            'boot_image':  '',
            'description': desc,
            'network':     _default_network(os_type),
            'created':     time.strftime('%Y-%m-%d %H:%M:%S'),
            'imported':    True,
        }
        _save_vms(vms)
        return jsonify({'status': 'ok', 'id': vm_id, 'name': name, 'disk_size': disk_size})

    except Exception as e:
        shutil.rmtree(vm_path, ignore_errors=True)
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════
#  CONVERT DISK IMAGES
# ═══════════════════════════════════════════════════════════

@vm_bp.route('/convert', methods=['POST'])
@admin_required
@_require_qemu
def convert_image():
    """Convert a disk image between formats (raw, qcow2, vdi, vmdk)."""
    err = require_tools('qemu-img')
    if err:
        return err
    data = request.get_json(force=True) if request.data else {}
    source = data.get('source', '')
    target_format = data.get('format', 'qcow2')

    if target_format not in ('raw', 'qcow2', 'vdi', 'vmdk'):
        return jsonify({'error': 'Unsupported target format'}), 400

    if not source or not os.path.exists(source):
        return jsonify({'error': 'Source file not found'}), 404

    source_real = os.path.realpath(source)
    if not _is_allowed_image_path(source_real):
        return jsonify({'error': 'Source path not allowed'}), 403

    base, _ = os.path.splitext(source_real)
    dest = os.path.realpath(f'{base}.{target_format}')
    if not _is_allowed_image_path(dest):
        return jsonify({'error': 'Target path not allowed'}), 403

    try:
        r = host_run(f'qemu-img convert -O {target_format} "{source_real}" "{dest}"', timeout=600)
        if r.returncode == 0:
            return jsonify({'status': 'ok', 'output': dest, 'target_format': target_format})
        return jsonify({'error': r.stderr}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Conversion timed out (10 min)'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
