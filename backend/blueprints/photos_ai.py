"""
EthOS - Photos AI blueprint.
Face recognition, object detection, and smart albums for the Gallery.
"""

import os, io, json, time, struct, sqlite3, hashlib, threading, logging
from pathlib import Path
from flask import Blueprint, request, jsonify, send_file, g

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import app_path, data_path, host_run, q
from utils import load_json as _load_json, save_json as _save_json, \
    safe_path as _safe_path_util, get_username as _get_username, DATA_ROOT, \
    ALLOWED_ROOTS
from blueprints.admin_required import admin_required

log = logging.getLogger('photos_ai')
photos_ai_bp = Blueprint('photos_ai', __name__, url_prefix='/api/photos-ai')

_DB_PATH = data_path('gallery_ai.db')
_MODELS_DIR = data_path('models/photos_ai')
_YOLO_MODEL = os.path.join(_MODELS_DIR, 'yolov8n.onnx')
_FACE_THUMBS_DIR = data_path('.thumb_cache/faces')

IMAGE_EXTS = {'.jpg','.jpeg','.png','.gif','.bmp','.webp','.tiff','.tif','.heic','.heif','.avif'}
_YOLO_URL = 'https://huggingface.co/Kalray/yolov8/resolve/main/yolov8n.onnx'
_YOLO_CLASSES = [
    'person','bicycle','car','motorcycle','airplane','bus','train','truck','boat',
    'traffic light','fire hydrant','stop sign','parking meter','bench','bird','cat',
    'dog','horse','sheep','cow','elephant','bear','zebra','giraffe','backpack',
    'umbrella','handbag','tie','suitcase','frisbee','skis','snowboard','sports ball',
    'kite','baseball bat','baseball glove','skateboard','surfboard','tennis racket',
    'bottle','wine glass','cup','fork','knife','spoon','bowl','banana','apple',
    'sandwich','orange','broccoli','carrot','hot dog','pizza','donut','cake',
    'chair','couch','potted plant','bed','dining table','toilet','tv','laptop',
    'mouse','remote','keyboard','cell phone','microwave','oven','toaster','sink',
    'refrigerator','book','clock','vase','scissors','teddy bear','hair drier','toothbrush'
]
_TAG_PL = {
    'person':'osoba','bicycle':'rower','car':'samochod','motorcycle':'motocykl',
    'airplane':'samolot','bus':'autobus','train':'pociag','truck':'ciezarowka',
    'boat':'lodz','bird':'ptak','cat':'kot','dog':'pies','horse':'kon',
    'sheep':'owca','cow':'krowa','elephant':'slon','bear':'niedzwiedz',
    'zebra':'zebra','giraffe':'zyrafa','backpack':'plecak','umbrella':'parasol',
    'handbag':'torebka','tie':'krawat','suitcase':'walizka','bottle':'butelka',
    'wine glass':'kieliszek','cup':'kubek','fork':'widelec','knife':'noz',
    'spoon':'lyzka','bowl':'miska','banana':'banan','apple':'jablko',
    'sandwich':'kanapka','orange':'pomarancza','broccoli':'brokul',
    'carrot':'marchewka','pizza':'pizza','donut':'paczek','cake':'ciasto',
    'chair':'krzeslo','couch':'kanapa','potted plant':'roslina','bed':'lozko',
    'dining table':'stol','tv':'telewizor','laptop':'laptop','cell phone':'telefon',
    'book':'ksiazka','clock':'zegar','vase':'wazon','scissors':'nozyczki',
    'teddy bear':'mis','toothbrush':'szczoteczka',
}
_MIN_YOLO_CONFIDENCE = 0.35
_CLUSTER_THRESHOLD = 0.6

_scan_lock = threading.Lock()
_scan_state = {'running':False,'stop_requested':False,'paused':False,'total':0,'processed':0,
               'faces_found':0,'tags_found':0,'current_file':'','started_at':0}

_AI_SETTINGS_FILE = os.path.join(DATA_ROOT, 'photos_ai_settings.json')

def _load_ai_settings():
    try:
        with open(_AI_SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_ai_settings(d):
    with open(_AI_SETTINGS_FILE, 'w') as f:
        json.dump(d, f)


def _persist_scan_state(state_dict):
    """Save scan progress to DB so it survives restarts."""
    try:
        conn = _get_db()
        conn.execute('INSERT OR REPLACE INTO scan_state (key, value) VALUES (?,?)',
                     ('progress', json.dumps(state_dict)))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _clear_persisted_scan():
    """Remove persisted scan state (scan finished or stopped)."""
    try:
        conn = _get_db()
        conn.execute("DELETE FROM scan_state WHERE key='progress'")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _load_persisted_scan():
    """Load interrupted scan state from DB. Returns dict or None."""
    try:
        conn = _get_db()
        row = conn.execute("SELECT value FROM scan_state WHERE key='progress'").fetchone()
        conn.close()
        if row:
            return json.loads(row['value'])
    except Exception:
        pass
    return None


def _get_db():
    conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def _init_db():
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_path TEXT NOT NULL, x INTEGER, y INTEGER, w INTEGER, h INTEGER,
            embedding BLOB NOT NULL, person_id INTEGER, confidence REAL DEFAULT 1.0,
            created_at REAL DEFAULT (strftime('%s','now')),
            FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '', cover_face_id INTEGER,
            photo_count INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0,
            created_at REAL DEFAULT (strftime('%s','now')));
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_path TEXT NOT NULL, tag TEXT NOT NULL, tag_pl TEXT DEFAULT '',
            confidence REAL DEFAULT 1.0, source TEXT DEFAULT 'yolo',
            created_at REAL DEFAULT (strftime('%s','now')));
        CREATE TABLE IF NOT EXISTS scan_log (
            photo_path TEXT PRIMARY KEY, file_mtime REAL NOT NULL,
            scanned_at REAL DEFAULT (strftime('%s','now')));
        CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_path);
        CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
        CREATE INDEX IF NOT EXISTS idx_tags_photo ON tags(photo_path);
        CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
        CREATE TABLE IF NOT EXISTS scan_state (
            key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.commit()
    conn.close()

try:
    _init_db()
except Exception:
    pass


def resume_interrupted_scan():
    """Called on app startup to resume a scan that was interrupted by a restart.
    Uses gevent.spawn_later to wait for full app init before starting."""
    settings = _load_ai_settings()
    if settings.get('auto_scan') is False:
        log.info('AI auto-scan disabled by user setting, skipping resume')
        return
    saved = _load_persisted_scan()
    if not saved:
        return
    folders = saved.get('folders', [])
    if not folders:
        _clear_persisted_scan()
        return
    deps = _check_deps()
    if not deps.get('ready'):
        _clear_persisted_scan()
        return
    log.info('Resuming interrupted scan (%d processed before restart), folders: %s',
             saved.get('processed', 0), folders)
    with _scan_lock:
        if _scan_state['running']:
            return
        _scan_state['running'] = True
        _scan_state['stop_requested'] = False
        _scan_state['paused'] = False
    _launch_scan(folders)


def _safe_path(user_path):
    if not user_path:
        return None
    if os.path.isabs(user_path):
        target = os.path.realpath(user_path)
    else:
        target = os.path.realpath(os.path.join(os.path.realpath(DATA_ROOT), user_path))
    if any(target == r or target.startswith(r + '/') for r in ALLOWED_ROOTS):
        return target
    return None

def _emb2blob(emb):
    return struct.pack(f'{len(emb)}f', *emb)

def _blob2emb(blob):
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))

def _sio():
    try:
        return photos_ai_bp._socketio
    except AttributeError:
        return None

def _emit_progress():
    s = _sio()
    if s:
        s.emit('photos_ai_progress', {
            'running': _scan_state['running'],
            'total': _scan_state['total'],
            'processed': _scan_state['processed'],
            'faces_found': _scan_state['faces_found'],
            'tags_found': _scan_state['tags_found'],
            'current_file': os.path.basename(_scan_state['current_file']),
        })

def _gallery_folders():
    u = _get_username()
    if u:
        from host import user_data_path
        p = user_data_path('gallery_folders.json', u)
    else:
        p = data_path('gallery_folders.json')
    folders = _load_json(p, [])
    return [f.get('path', f) if isinstance(f, dict) else f for f in folders]

def _collect_images(folders):
    imgs = []
    for folder in folders:
        fp = _safe_path(folder)
        if not fp or not os.path.isdir(fp):
            continue
        for root, _, files in os.walk(fp):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in IMAGE_EXTS:
                    imgs.append(os.path.join(root, fn))
    return imgs

def _needs_scan(conn, path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False
    row = conn.execute('SELECT file_mtime FROM scan_log WHERE photo_path=?', (path,)).fetchone()
    return not row or abs(mtime - row['file_mtime']) > 1.0

def _check_deps():
    st = {}
    for mod in ('face_recognition', 'onnxruntime', 'scipy', 'numpy'):
        try:
            __import__(mod)
            st[mod] = True
        except ImportError:
            st[mod] = False
    try:
        from PIL import Image
        st['PIL'] = True
    except ImportError:
        st['PIL'] = False
    hm = os.path.isfile(_YOLO_MODEL)
    return {'ready': all(st.values()) and hm, 'deps': st, 'yolo_model': hm}


# -- ML Pipeline --

def _detect_faces(path):
    import face_recognition
    img = face_recognition.load_image_file(path)
    locs = face_recognition.face_locations(img, model='hog')
    if not locs:
        return []
    encs = face_recognition.face_encodings(img, locs)
    return [{'x': l, 'y': t, 'w': r - l, 'h': b - t, 'embedding': list(e)}
            for (t, r, b, l), e in zip(locs, encs)]

def _load_yolo():
    import onnxruntime as ort
    return ort.InferenceSession(_YOLO_MODEL, providers=['CPUExecutionProvider'])

def _detect_objects(sess, path):
    import numpy as np
    from PIL import Image
    img = Image.open(path).convert('RGB').resize((640, 640))
    arr = np.expand_dims(np.array(img, dtype=np.float32).transpose(2, 0, 1) / 255.0, 0)
    preds = sess.run(None, {sess.get_inputs()[0].name: arr})[0][0].T
    tags = {}
    for det in preds:
        scores = det[4:]
        cid = int(np.argmax(scores))
        conf = float(scores[cid])
        if conf < _MIN_YOLO_CONFIDENCE:
            continue
        tag = _YOLO_CLASSES[cid]
        if tag not in tags or conf > tags[tag]:
            tags[tag] = conf
    return [{'tag': t, 'confidence': c, 'tag_pl': _TAG_PL.get(t, t)} for t, c in tags.items()]

def _exif_tags(path, conn):
    from PIL import Image
    from PIL.ExifTags import TAGS as ET
    try:
        exif = Image.open(path)._getexif()
        if not exif:
            return
    except Exception:
        return
    for tid, val in exif.items():
        if ET.get(tid, '') == 'Model' and val:
            cam = str(val).replace('\x00', '').strip()
            if cam:
                conn.execute(
                    'INSERT INTO tags (photo_path,tag,tag_pl,confidence,source) VALUES (?,?,?,?,?)',
                    (path, 'camera:' + cam, 'aparat:' + cam, 1.0, 'exif'))

def _detect_faces_and_objects(path, yolo):
    """CPU-heavy ML work — runs in threadpool to avoid blocking gevent event loop."""
    faces = []
    tags = []
    try:
        faces = _detect_faces(path)
    except Exception as e:
        log.debug('Face fail %s: %s', path, e)
    if yolo:
        try:
            tags = _detect_objects(yolo, path)
        except Exception as e:
            log.debug('YOLO fail %s: %s', path, e)
    return faces, tags


def _process_image(path, conn, yolo):
    from gevent.threadpool import ThreadPool
    if not hasattr(_process_image, '_pool'):
        _process_image._pool = ThreadPool(1)
    ff = tf = 0
    faces, tags = _process_image._pool.apply(_detect_faces_and_objects, (path, yolo))
    for f in faces:
        conn.execute(
            'INSERT INTO faces (photo_path,x,y,w,h,embedding) VALUES (?,?,?,?,?,?)',
            (path, f['x'], f['y'], f['w'], f['h'], _emb2blob(f['embedding'])))
        face_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        _try_assign_face(conn, face_id, f['embedding'])
        ff += 1
    for obj in tags:
        conn.execute(
            'INSERT INTO tags (photo_path,tag,tag_pl,confidence,source) VALUES (?,?,?,?,?)',
            (path, obj['tag'], obj['tag_pl'], obj['confidence'], 'yolo'))
        tf += 1
    try:
        _exif_tags(path, conn)
    except Exception:
        pass
    try:
        conn.execute(
            'INSERT OR REPLACE INTO scan_log (photo_path,file_mtime,scanned_at) VALUES (?,?,?)',
            (path, os.path.getmtime(path), time.time()))
        conn.commit()
    except Exception:
        pass
    return ff, tf


def _try_assign_face(conn, face_id, embedding):
    """Incrementally assign a new face to an existing person by embedding similarity."""
    import numpy as np
    people = conn.execute(
        'SELECT id FROM people WHERE hidden=0').fetchall()
    if not people:
        return
    target = np.array(embedding)
    best_pid, best_dist = None, _CLUSTER_THRESHOLD
    for p in people:
        rows = conn.execute(
            'SELECT embedding FROM faces WHERE person_id=? LIMIT 30', (p['id'],)
        ).fetchall()
        if not rows:
            continue
        embs = np.array([_blob2emb(r['embedding']) for r in rows])
        centroid = embs.mean(axis=0)
        dist = float(np.linalg.norm(target - centroid))
        if dist < best_dist:
            best_dist = dist
            best_pid = p['id']
    if best_pid is not None:
        conn.execute('UPDATE faces SET person_id=? WHERE id=?', (best_pid, face_id))
        cnt = conn.execute(
            'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
            (best_pid,)).fetchone()[0]
        conn.execute('UPDATE people SET photo_count=? WHERE id=?', (cnt, best_pid))


# -- Clustering --

def _run_clustering(conn):
    import numpy as np
    rows = conn.execute('SELECT id, embedding FROM faces').fetchall()
    if not rows:
        return 0
    fids = [r['id'] for r in rows]
    embs = np.array([_blob2emb(r['embedding']) for r in rows])
    if len(embs) < 2:
        conn.execute('DELETE FROM people')
        conn.execute('INSERT INTO people (name,photo_count) VALUES ("",1)')
        pid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute('UPDATE faces SET person_id=? WHERE id=?', (pid, fids[0]))
        conn.commit()
        return 1
    try:
        from scipy.cluster.hierarchy import fcluster, linkage
        Z = linkage(embs, method='average', metric='euclidean')
        labels = list(fcluster(Z, t=_CLUSTER_THRESHOLD, criterion='distance'))
    except ImportError:
        labels = _greedy_cluster(embs, _CLUSTER_THRESHOLD)
    old_names = {}
    for row in conn.execute(
            'SELECT p.id,p.name,f.id as fid FROM people p '
            'JOIN faces f ON f.person_id=p.id WHERE p.name!=""'):
        old_names[row['fid']] = row['name']
    conn.execute('DELETE FROM people')
    fid2lbl = dict(zip(fids, labels))
    nc = int(max(labels))
    for cid in range(1, nc + 1):
        cfids = [fid for fid, lbl in fid2lbl.items() if lbl == cid]
        if not cfids:
            continue
        name = ''
        for fid in cfids:
            if fid in old_names:
                name = old_names[fid]
                break
        pcnt = len(set(
            r['photo_path'] for r in conn.execute(
                'SELECT photo_path FROM faces WHERE id IN (%s)' % ','.join(['?'] * len(cfids)),
                cfids).fetchall()))
        conn.execute(
            'INSERT INTO people (name,photo_count,cover_face_id) VALUES (?,?,?)',
            (name, pcnt, cfids[0]))
        pid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute(
            'UPDATE faces SET person_id=? WHERE id IN (%s)' % ','.join(['?'] * len(cfids)),
            [pid] + cfids)
    conn.commit()
    return nc

def _greedy_cluster(embs, thr):
    import numpy as np
    n = len(embs)
    labels = [0] * n
    cl = 0
    for i in range(n):
        if labels[i]:
            continue
        cl += 1
        labels[i] = cl
        for j in range(i + 1, n):
            if not labels[j] and np.linalg.norm(embs[i] - embs[j]) < thr:
                labels[j] = cl
    return labels


# -- Background scan --

def _scan_worker(folders):
    import gevent
    t0 = time.time()
    imgs = _collect_images(folders)
    total_all = len(imgs)
    conn = _get_db()
    todo = [p for p in imgs if _needs_scan(conn, p)]
    already_done = total_all - len(todo)
    _scan_state.update(total=total_all, processed=already_done,
                       faces_found=0, tags_found=0, started_at=t0,
                       already_scanned=already_done)
    _persist_scan_state({'folders': folders, 'total': total_all,
                         'processed': already_done,
                         'faces_found': 0, 'tags_found': 0, 'started_at': t0})
    _emit_progress()
    if not todo:
        _scan_state['running'] = False
        _clear_persisted_scan()
        _emit_progress()
        s = _sio()
        if s:
            s.emit('photos_ai_done', {
                'total_processed': 0, 'faces': 0, 'tags': 0, 'people': 0,
                'duration': 0, 'message': 'Brak nowych zdjec do skanowania.'})
        conn.close()
        return
    if already_done:
        log.info('Scan: skipping %d already-scanned, %d remaining of %d total',
                 already_done, len(todo), total_all)
    yolo = None
    if os.path.isfile(_YOLO_MODEL):
        try:
            yolo = _load_yolo()
        except Exception as e:
            log.warning('YOLO load: %s', e)
    for i, path in enumerate(todo):
        if _scan_state.get('stop_requested'):
            break
        # Pause support: spin-wait while paused
        while _scan_state.get('paused') and not _scan_state.get('stop_requested'):
            gevent.sleep(1)
        if _scan_state.get('stop_requested'):
            break
        _scan_state['current_file'] = path
        gevent.sleep(0.3)  # throttle: yield CPU between images so server stays responsive
        ff, tf = _process_image(path, conn, yolo)
        _scan_state['processed'] = already_done + i + 1
        _scan_state['faces_found'] += ff
        _scan_state['tags_found'] += tf
        if (i + 1) % 5 == 0 or i == 0:
            _emit_progress()
            _persist_scan_state({'folders': folders, 'total': total_all,
                                 'processed': already_done + i + 1,
                                 'faces_found': _scan_state['faces_found'],
                                 'tags_found': _scan_state['tags_found'], 'started_at': t0})
            gevent.sleep(0.1)
        # Run full clustering every 500 photos so new faces get grouped
        if (i + 1) % 500 == 0:
            try:
                _run_clustering(conn)
                log.info('Interim clustering at %d/%d photos', already_done + i + 1, total_all)
            except Exception as e:
                log.warning('Interim cluster: %s', e)
    np_ = 0
    try:
        np_ = _run_clustering(conn)
    except Exception as e:
        log.warning('Cluster: %s', e)
    conn.close()
    dur = time.time() - t0
    _scan_state.update(running=False, stop_requested=False, paused=False, current_file='')
    _clear_persisted_scan()
    s = _sio()
    if s:
        s.emit('photos_ai_done', {
            'total_processed': _scan_state['processed'],
            'faces': _scan_state['faces_found'],
            'tags': _scan_state['tags_found'],
            'people': np_, 'duration': round(dur, 1)})


# -- Routes --

@photos_ai_bp.route('/pkg-status', methods=['GET'])
def pkg_status():
    st = _check_deps()
    stats = {}
    if os.path.isfile(_DB_PATH):
        try:
            c = _get_db()
            stats = {
                'faces': c.execute('SELECT COUNT(*) FROM faces').fetchone()[0],
                'people': c.execute('SELECT COUNT(*) FROM people WHERE hidden=0').fetchone()[0],
                'tags': c.execute('SELECT COUNT(*) FROM tags').fetchone()[0],
                'scanned': c.execute('SELECT COUNT(*) FROM scan_log').fetchone()[0],
            }
            c.close()
        except Exception:
            pass
    st['stats'] = stats
    st['installed'] = st['ready']
    st['scanning'] = _scan_state['running']
    return jsonify(st)

@photos_ai_bp.route('/install', methods=['POST'])
@admin_required
def install_deps():
    import gevent
    def _do():
        steps = []
        try:
            host_run('apt-get update -qq && apt-get install -y -qq cmake libopenblas-dev liblapack-dev && apt-get clean', timeout=120)
            steps.append('system_deps')
            pip = os.path.join(app_path(), 'venv', 'bin', 'pip')
            host_run(f'{q(pip)} install --quiet face_recognition onnxruntime scipy', timeout=600)
            steps.append('pip_packages')
            os.makedirs(_MODELS_DIR, exist_ok=True)
            if not os.path.isfile(_YOLO_MODEL):
                host_run(f'curl -sL -o {q(_YOLO_MODEL)} {q(_YOLO_URL)}', timeout=120)
            steps.append('yolo_model')
            s = _sio()
            if s:
                s.emit('photos_ai_install', {'ok': True, 'steps': steps})
        except Exception as e:
            log.error('Install failed: %s', e)
            s = _sio()
            if s:
                s.emit('photos_ai_install', {'ok': False, 'error': str(e), 'steps': steps})
    gevent.spawn(_do)
    return jsonify({'ok': True, 'message': 'Instalacja rozpoczeta w tle...'})

@photos_ai_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall_deps():
    wipe = (request.json or {}).get('wipe_data', False)
    if wipe:
        try:
            if os.path.isfile(_DB_PATH):
                os.remove(_DB_PATH)
        except Exception:
            pass
        import shutil
        for d in [_MODELS_DIR, _FACE_THUMBS_DIR]:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
    return jsonify({'ok': True})

@photos_ai_bp.route('/scan', methods=['POST'])
@admin_required
def start_scan():
    with _scan_lock:
        if _scan_state['running']:
            return jsonify({'error': 'Skanowanie juz trwa.'}), 409
        deps = _check_deps()
        if not deps['ready']:
            return jsonify({'error': 'Zaleznosci nie zainstalowane.'}), 400
        folders = _gallery_folders()
        if not folders:
            return jsonify({'error': 'Brak folderow zrodlowych w Galerii.'}), 400
        _scan_state['running'] = True
        _scan_state['stop_requested'] = False
    _launch_scan(folders)
    return jsonify({'ok': True, 'message': 'Skanowanie AI rozpoczete.'})


def _launch_scan(folders):
    """Start scan worker in background (used by both manual scan and auto-resume)."""
    s = _sio()
    if s:
        s.start_background_task(_scan_worker, folders)
    else:
        import gevent
        gevent.spawn(_scan_worker, folders)

@photos_ai_bp.route('/stop-scan', methods=['POST'])
@admin_required
def stop_scan():
    _scan_state['stop_requested'] = True
    _scan_state['paused'] = False
    _clear_persisted_scan()
    return jsonify({'ok': True})

@photos_ai_bp.route('/pause-scan', methods=['POST'])
@admin_required
def pause_scan():
    if not _scan_state['running']:
        return jsonify({'error': 'Skanowanie nie jest uruchomione.'}), 400
    _scan_state['paused'] = True
    return jsonify({'ok': True})

@photos_ai_bp.route('/resume-scan', methods=['POST'])
@admin_required
def resume_scan():
    if not _scan_state['running']:
        return jsonify({'error': 'Skanowanie nie jest uruchomione.'}), 400
    _scan_state['paused'] = False
    return jsonify({'ok': True})

@photos_ai_bp.route('/rescan', methods=['POST'])
@admin_required
def rescan_from_scratch():
    """Stop current scan, wipe scan_log, then start fresh scan."""
    with _scan_lock:
        if _scan_state['running']:
            _scan_state['stop_requested'] = True
            _scan_state['paused'] = False
    # Wait for scan to stop (max 10s)
    import gevent
    for _ in range(20):
        if not _scan_state['running']:
            break
        gevent.sleep(0.5)
    deps = _check_deps()
    if not deps.get('ready'):
        return jsonify({'error': 'Zaleznosci nie zainstalowane.'}), 400
    folders = _gallery_folders()
    if not folders:
        return jsonify({'error': 'Brak folderow zrodlowych w Galerii.'}), 400
    # Wipe scan log so everything is rescanned
    try:
        conn = _get_db()
        conn.execute('DELETE FROM scan_log')
        conn.commit()
        conn.close()
        log.info('Wiped scan_log for full rescan')
    except Exception as e:
        log.warning('Failed to wipe scan_log: %s', e)
    _clear_persisted_scan()
    with _scan_lock:
        _scan_state['running'] = True
        _scan_state['stop_requested'] = False
        _scan_state['paused'] = False
    _launch_scan(folders)
    return jsonify({'ok': True, 'message': 'Reskan od poczatku rozpoczety.'})

@photos_ai_bp.route('/ai-settings', methods=['GET'])
def get_ai_settings():
    settings = _load_ai_settings()
    return jsonify({
        'auto_scan': settings.get('auto_scan', True),
    })

@photos_ai_bp.route('/ai-settings', methods=['POST'])
@admin_required
def set_ai_settings():
    d = request.json or {}
    settings = _load_ai_settings()
    if 'auto_scan' in d:
        settings['auto_scan'] = bool(d['auto_scan'])
    _save_ai_settings(settings)
    return jsonify({'ok': True})

@photos_ai_bp.route('/scan-status', methods=['GET'])
def scan_status():
    return jsonify({
        'running': _scan_state['running'],
        'paused': _scan_state.get('paused', False),
        'total': _scan_state['total'],
        'processed': _scan_state['processed'],
        'faces_found': _scan_state['faces_found'],
        'tags_found': _scan_state['tags_found'],
        'current_file': os.path.basename(_scan_state['current_file']),
    })

@photos_ai_bp.route('/people', methods=['GET'])
def list_people():
    conn = _get_db()
    where = '' if request.args.get('hidden', '0') == '1' else 'WHERE hidden=0'
    rows = conn.execute(f'SELECT * FROM people {where} ORDER BY photo_count DESC').fetchall()
    result = []
    for r in rows:
        cnt = conn.execute(
            'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
            (r['id'],)).fetchone()[0]
        result.append({
            'id': r['id'],
            'name': r['name'] or f'Osoba {r["id"]}',
            'photo_count': cnt,
            'cover_face_id': r['cover_face_id'],
            'hidden': bool(r['hidden']),
        })
    conn.close()
    return jsonify({'people': result})

@photos_ai_bp.route('/people/merge', methods=['POST'])
@admin_required
def merge_people():
    d = request.json or {}
    src, tgt = d.get('source_id'), d.get('target_id')
    if not src or not tgt or src == tgt:
        return jsonify({'error': 'Podaj source_id i target_id.'}), 400
    conn = _get_db()
    try:
        conn.execute('UPDATE faces SET person_id=? WHERE person_id=?', (tgt, src))
        cnt = conn.execute(
            'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
            (tgt,)).fetchone()[0]
        conn.execute('UPDATE people SET photo_count=? WHERE id=?', (cnt, tgt))
        conn.execute('DELETE FROM people WHERE id=?', (src,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        if 'locked' in str(e).lower():
            return jsonify({'error': 'Baza jest zajęta (trwa skanowanie). Spróbuj za chwilę.'}), 503
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()
    return jsonify({'ok': True})

@photos_ai_bp.route('/people/<int:pid>/hide', methods=['POST'])
def hide_person(pid):
    h = 1 if (request.json or {}).get('hidden', True) else 0
    conn = _get_db()
    conn.execute('UPDATE people SET hidden=? WHERE id=?', (h, pid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@photos_ai_bp.route('/people/<int:pid>', methods=['DELETE'])
@admin_required
def delete_person(pid):
    conn = _get_db()
    conn.execute('UPDATE faces SET person_id=NULL WHERE person_id=?', (pid,))
    conn.execute('DELETE FROM people WHERE id=?', (pid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@photos_ai_bp.route('/people/<int:pid>/photos', methods=['GET'])
def person_photos(pid):
    conn = _get_db()
    off = int(request.args.get('offset', 0))
    lim = int(request.args.get('limit', 80))
    rows = conn.execute(
        'SELECT DISTINCT photo_path FROM faces WHERE person_id=? ORDER BY photo_path LIMIT ? OFFSET ?',
        (pid, lim, off)).fetchall()
    total = conn.execute(
        'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
        (pid,)).fetchone()[0]
    items = []
    for r in rows:
        p = r['photo_path']
        nm = os.path.basename(p)
        ext = os.path.splitext(nm)[1].lower()
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0
        items.append({
            'path': p, 'name': nm,
            'type': 'image' if ext in IMAGE_EXTS else 'video',
            'mtime': mt,
        })
    conn.close()
    return jsonify({'items': items, 'total': total})

@photos_ai_bp.route('/people/<int:pid>/faces', methods=['GET'])
def person_faces(pid):
    """Return all face thumbnails for a given person (for false-positive management)."""
    conn = _get_db()
    rows = conn.execute(
        'SELECT id, photo_path FROM faces WHERE person_id=? ORDER BY id', (pid,)
    ).fetchall()
    conn.close()
    return jsonify({'faces': [{'id': r['id'], 'photo_path': r['photo_path']} for r in rows]})

@photos_ai_bp.route('/people/<int:pid>/rename', methods=['POST'])
def rename_person(pid):
    d = request.json or {}
    name = d.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Podaj imię.'}), 400
    conn = _get_db()
    conn.execute('UPDATE people SET name=? WHERE id=?', (name, pid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@photos_ai_bp.route('/face-thumb/<int:face_id>', methods=['GET'])
def face_thumbnail(face_id):
    conn = _get_db()
    row = conn.execute(
        'SELECT photo_path,x,y,w,h FROM faces WHERE id=?', (face_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Nie znaleziono.'}), 404
    sp = _safe_path(row['photo_path'])
    if not sp or not os.path.isfile(sp):
        return jsonify({'error': 'Plik nie istnieje.'}), 404
    os.makedirs(_FACE_THUMBS_DIR, exist_ok=True)
    cf = os.path.join(_FACE_THUMBS_DIR, f'{face_id}.jpg')
    if os.path.isfile(cf):
        return send_file(cf, mimetype='image/jpeg')
    try:
        from PIL import Image
        img = Image.open(sp)
        x, y, w, h = row['x'], row['y'], row['w'], row['h']
        pad = int(max(w, h) * 0.3)
        face = img.crop((max(0, x - pad), max(0, y - pad),
                         min(img.width, x + w + pad), min(img.height, y + h + pad)))
        face = face.resize((150, 150))
        face.save(cf, 'JPEG', quality=85)
        return send_file(cf, mimetype='image/jpeg')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@photos_ai_bp.route('/tags', methods=['GET'])
def list_tags():
    conn = _get_db()
    rows = conn.execute(
        'SELECT tag,tag_pl,COUNT(*) as cnt FROM tags GROUP BY tag ORDER BY cnt DESC').fetchall()
    conn.close()
    return jsonify({
        'tags': [{'tag': r['tag'], 'tag_pl': r['tag_pl'], 'count': r['cnt']} for r in rows]
    })

@photos_ai_bp.route('/search', methods=['GET'])
def ai_search():
    qs = request.args.get('q', '').strip().lower()
    if not qs:
        return jsonify({'items': [], 'total': 0})
    conn = _get_db()
    off = int(request.args.get('offset', 0))
    lim = int(request.args.get('limit', 80))
    tp = set(r['photo_path'] for r in conn.execute(
        'SELECT DISTINCT photo_path FROM tags WHERE LOWER(tag) LIKE ? OR LOWER(tag_pl) LIKE ?',
        (f'%{qs}%', f'%{qs}%')).fetchall())
    pp = set(r['photo_path'] for r in conn.execute(
        'SELECT DISTINCT f.photo_path FROM faces f JOIN people p ON f.person_id=p.id '
        'WHERE LOWER(p.name) LIKE ?', (f'%{qs}%',)).fetchall())
    ap = sorted(tp | pp)
    total = len(ap)
    page = ap[off:off + lim]
    items = []
    for p in page:
        nm = os.path.basename(p)
        ext = os.path.splitext(nm)[1].lower()
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0
        items.append({
            'path': p, 'name': nm,
            'type': 'image' if ext in IMAGE_EXTS else 'video',
            'mtime': mt,
        })
    conn.close()
    return jsonify({'items': items, 'total': total})

@photos_ai_bp.route('/smart-albums', methods=['GET'])
def smart_albums():
    conn = _get_db()
    albums = []
    for p in conn.execute(
            'SELECT id,name,cover_face_id FROM people WHERE hidden=0 '
            'ORDER BY photo_count DESC LIMIT 20').fetchall():
        cnt = conn.execute(
            'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
            (p['id'],)).fetchone()[0]
        if cnt > 0:
            albums.append({
                'type': 'person', 'id': str(p['id']),
                'name': p['name'] or f'Osoba {p["id"]}',
                'count': cnt, 'cover_face_id': p['cover_face_id'],
            })
    for t in conn.execute(
            "SELECT tag,tag_pl,COUNT(DISTINCT photo_path) as cnt FROM tags "
            "WHERE source='yolo' GROUP BY tag HAVING cnt>=3 ORDER BY cnt DESC LIMIT 20").fetchall():
        albums.append({
            'type': 'tag', 'id': t['tag'],
            'name': t['tag_pl'] or t['tag'], 'count': t['cnt'],
        })
    for c in conn.execute(
            "SELECT tag,COUNT(DISTINCT photo_path) as cnt FROM tags "
            "WHERE source='exif' AND tag LIKE 'camera:%' GROUP BY tag "
            "HAVING cnt>=3 ORDER BY cnt DESC LIMIT 10").fetchall():
        cam = c['tag'].replace('camera:', '')
        albums.append({'type': 'camera', 'id': cam, 'name': cam, 'count': c['cnt']})
    conn.close()
    return jsonify({'albums': albums})

@photos_ai_bp.route('/photo-ai', methods=['GET'])
def photo_ai_data():
    path = request.args.get('path', '')
    sp = _safe_path(path)
    if not sp:
        return jsonify({'error': 'Nieprawidlowa sciezka.'}), 400
    conn = _get_db()
    faces = [{
        'id': f['id'], 'x': f['x'], 'y': f['y'], 'w': f['w'], 'h': f['h'],
        'person_id': f['person_id'], 'person_name': f['person_name'] or '',
    } for f in conn.execute(
        'SELECT f.id,f.x,f.y,f.w,f.h,f.person_id,p.name as person_name '
        'FROM faces f LEFT JOIN people p ON f.person_id=p.id '
        'WHERE f.photo_path=?', (sp,)).fetchall()]
    tags = [{
        'tag': t['tag'], 'tag_pl': t['tag_pl'],
        'confidence': round(t['confidence'], 2), 'source': t['source'],
    } for t in conn.execute(
        'SELECT tag,tag_pl,confidence,source FROM tags WHERE photo_path=?',
        (sp,)).fetchall()]
    conn.close()
    return jsonify({'faces': faces, 'tags': tags})

@photos_ai_bp.route('/album-photos', methods=['GET'])
def album_photos():
    at = request.args.get('type', '')
    aid = request.args.get('id', '')
    off = int(request.args.get('offset', 0))
    lim = int(request.args.get('limit', 80))
    conn = _get_db()
    if at == 'tag':
        rows = conn.execute(
            'SELECT DISTINCT photo_path FROM tags WHERE tag=? ORDER BY photo_path LIMIT ? OFFSET ?',
            (aid, lim, off)).fetchall()
        total = conn.execute(
            'SELECT COUNT(DISTINCT photo_path) FROM tags WHERE tag=?', (aid,)).fetchone()[0]
    elif at == 'camera':
        ct = 'camera:' + aid
        rows = conn.execute(
            'SELECT DISTINCT photo_path FROM tags WHERE tag=? ORDER BY photo_path LIMIT ? OFFSET ?',
            (ct, lim, off)).fetchall()
        total = conn.execute(
            'SELECT COUNT(DISTINCT photo_path) FROM tags WHERE tag=?', (ct,)).fetchone()[0]
    elif at == 'person':
        pid = int(aid)
        rows = conn.execute(
            'SELECT DISTINCT photo_path FROM faces WHERE person_id=? ORDER BY photo_path LIMIT ? OFFSET ?',
            (pid, lim, off)).fetchall()
        total = conn.execute(
            'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
            (pid,)).fetchone()[0]
    else:
        conn.close()
        return jsonify({'error': 'Nieznany typ albumu.'}), 400
    items = []
    for r in rows:
        p = r['photo_path']
        nm = os.path.basename(p)
        ext = os.path.splitext(nm)[1].lower()
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0
        items.append({
            'path': p, 'name': nm,
            'type': 'image' if ext in IMAGE_EXTS else 'video',
            'mtime': mt,
        })
    conn.close()
    return jsonify({'items': items, 'total': total})

@photos_ai_bp.route('/cluster', methods=['POST'])
@admin_required
def rerun_clustering():
    if not _check_deps()['ready']:
        return jsonify({'error': 'Zaleznosci nie zainstalowane.'}), 400
    conn = _get_db()
    try:
        n = _run_clustering(conn)
        conn.close()
        return jsonify({'ok': True, 'people': n})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@photos_ai_bp.route('/stats', methods=['GET'])
def ai_stats():
    conn = _get_db()
    stats = {
        'faces': conn.execute('SELECT COUNT(*) FROM faces').fetchone()[0],
        'people': conn.execute('SELECT COUNT(*) FROM people WHERE hidden=0').fetchone()[0],
        'tags': conn.execute('SELECT COUNT(*) FROM tags').fetchone()[0],
        'scanned_photos': conn.execute('SELECT COUNT(*) FROM scan_log').fetchone()[0],
        'top_tags': [{
            'tag': r['tag_pl'], 'count': r['cnt'],
        } for r in conn.execute(
            "SELECT tag_pl,COUNT(DISTINCT photo_path) as cnt FROM tags "
            "WHERE source='yolo' GROUP BY tag ORDER BY cnt DESC LIMIT 10").fetchall()],
    }
    conn.close()
    return jsonify(stats)


# ─── Face identification & merge suggestions ────────────────────

@photos_ai_bp.route('/identify-face', methods=['POST'])
def identify_face():
    """Given a face_id, find top matching people by embedding similarity."""
    import numpy as np
    d = request.json or {}
    face_id = d.get('face_id')
    if not face_id:
        return jsonify({'error': 'Podaj face_id.'}), 400
    conn = _get_db()
    row = conn.execute('SELECT embedding, person_id FROM faces WHERE id=?', (face_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Twarz nie znaleziona.'}), 404
    target_emb = np.array(_blob2emb(row['embedding']))
    people_rows = conn.execute(
        'SELECT id, name, cover_face_id, photo_count FROM people WHERE hidden=0'
    ).fetchall()
    results = []
    for p in people_rows:
        face_rows = conn.execute(
            'SELECT embedding FROM faces WHERE person_id=? LIMIT 30', (p['id'],)
        ).fetchall()
        if not face_rows:
            continue
        embs = np.array([_blob2emb(f['embedding']) for f in face_rows])
        centroid = embs.mean(axis=0)
        dist = float(np.linalg.norm(target_emb - centroid))
        results.append({
            'person_id': p['id'],
            'name': p['name'] or f'Osoba {p["id"]}',
            'cover_face_id': p['cover_face_id'],
            'photo_count': p['photo_count'],
            'distance': round(dist, 3),
            'confidence': round(max(0, 1.0 - dist) * 100, 1),
        })
    conn.close()
    results.sort(key=lambda x: x['distance'])
    return jsonify({'matches': results[:10], 'current_person_id': row['person_id']})


@photos_ai_bp.route('/assign-face', methods=['POST'])
def assign_face():
    """Assign a face to an existing person, create a new one, or unassign."""
    d = request.json or {}
    face_id = d.get('face_id')
    person_id = d.get('person_id')
    new_name = d.get('new_name', '').strip()
    unassign = d.get('unassign', False)
    if not face_id:
        return jsonify({'error': 'Podaj face_id.'}), 400
    conn = _get_db()
    face = conn.execute('SELECT id, person_id FROM faces WHERE id=?', (face_id,)).fetchone()
    if not face:
        conn.close()
        return jsonify({'error': 'Twarz nie znaleziona.'}), 404

    old_pid = face['person_id']

    if unassign:
        conn.execute('UPDATE faces SET person_id=NULL WHERE id=?', (face_id,))
        if old_pid:
            cnt = conn.execute(
                'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
                (old_pid,)).fetchone()[0]
            conn.execute('UPDATE people SET photo_count=? WHERE id=?', (cnt, old_pid))
            if cnt == 0:
                conn.execute('DELETE FROM people WHERE id=?', (old_pid,))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})

    if new_name and not person_id:
        cur = conn.execute(
            'INSERT INTO people (name, cover_face_id, photo_count) VALUES (?,?,1)',
            (new_name, face_id))
        person_id = cur.lastrowid
    elif not person_id:
        conn.close()
        return jsonify({'error': 'Podaj person_id lub new_name.'}), 400

    conn.execute('UPDATE faces SET person_id=? WHERE id=?', (person_id, face_id))
    if old_pid:
        cnt = conn.execute(
            'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
            (old_pid,)).fetchone()[0]
        conn.execute('UPDATE people SET photo_count=? WHERE id=?', (cnt, old_pid))
        if cnt == 0:
            conn.execute('DELETE FROM people WHERE id=?', (old_pid,))
    cnt = conn.execute(
        'SELECT COUNT(DISTINCT photo_path) FROM faces WHERE person_id=?',
        (person_id,)).fetchone()[0]
    conn.execute('UPDATE people SET photo_count=? WHERE id=?', (cnt, person_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'person_id': person_id})


@photos_ai_bp.route('/merge-suggestions', methods=['GET'])
def merge_suggestions():
    """Find pairs of people clusters that might be duplicates."""
    import numpy as np
    conn = _get_db()
    people = conn.execute('SELECT id, name FROM people WHERE hidden=0').fetchall()
    if len(people) < 2:
        conn.close()
        return jsonify({'suggestions': []})

    centroids = {}
    cover_faces = {}
    for p in people:
        rows = conn.execute(
            'SELECT embedding FROM faces WHERE person_id=? LIMIT 30', (p['id'],)
        ).fetchall()
        if not rows:
            continue
        embs = np.array([_blob2emb(r['embedding']) for r in rows])
        centroids[p['id']] = embs.mean(axis=0)
        cf = conn.execute(
            'SELECT id FROM faces WHERE person_id=? LIMIT 1', (p['id'],)
        ).fetchone()
        cover_faces[p['id']] = cf['id'] if cf else None

    pids = list(centroids.keys())
    pid_to_name = {p['id']: p['name'] or f'Osoba {p["id"]}' for p in people}
    suggestions = []
    threshold = 0.75
    for i in range(len(pids)):
        for j in range(i + 1, len(pids)):
            dist = float(np.linalg.norm(centroids[pids[i]] - centroids[pids[j]]))
            if dist < threshold:
                suggestions.append({
                    'person_a': {
                        'id': pids[i], 'name': pid_to_name[pids[i]],
                        'cover_face_id': cover_faces.get(pids[i]),
                    },
                    'person_b': {
                        'id': pids[j], 'name': pid_to_name[pids[j]],
                        'cover_face_id': cover_faces.get(pids[j]),
                    },
                    'distance': round(dist, 3),
                    'confidence': round(max(0, 1.0 - dist) * 100, 1),
                })
    conn.close()
    suggestions.sort(key=lambda x: x['distance'])
    return jsonify({'suggestions': suggestions[:20]})
