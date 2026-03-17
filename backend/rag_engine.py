"""
EthOS — RAG Engine (Retrieval-Augmented Generation)
Lightweight TF-IDF vector search for NAS documents and gallery metadata.
Zero external dependencies beyond numpy (already installed).
Designed for Intel N150 / low-power NAS hardware.

Index types:
  - 'documents'  — text files (.txt, .md, .py, .json, etc.)
  - 'gallery'    — image metadata (EXIF, filename, tags, descriptions)

All operations are sandbox-aware: users only access their own indexed files.
"""

import hashlib
import json
import math
import os
import re
import time
import threading
from collections import Counter
from pathlib import Path

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False

# Use cooperative yielding under gevent to avoid blocking event loop
try:
    import gevent
    _HAS_GEVENT = True
except ImportError:
    _HAS_GEVENT = False

try:
    from PIL import Image
    from PIL.ExifTags import TAGS as EXIF_TAGS
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

from host import data_path


# ═══════════════════════════════════════════════════════════════════
#  Text processing / tokenization
# ═══════════════════════════════════════════════════════════════════

_STOP_WORDS_PL = frozenset([
    'i', 'w', 'z', 'na', 'do', 'się', 'nie', 'to', 'że', 'o', 'jak',
    'ale', 'jest', 'za', 'co', 'po', 'a', 'od', 'tak', 'tego', 'ten',
    'dla', 'czy', 'tym', 'już', 'je', 'go', 'był', 'ze', 'są', 'ma',
    'tylko', 'by', 'te', 'bo', 'ta', 'też', 'jego', 'jej', 'jako',
    'przy', 'tu', 'lub', 'które', 'który', 'która', 'może', 'być',
    'jeszcze', 'gdy', 'mi', 'bardzo', 'gdzie', 'nawet', 'jednak',
])

_STOP_WORDS_EN = frozenset([
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'need', 'dare', 'to', 'of',
    'in', 'for', 'on', 'with', 'at', 'by', 'from', 'or', 'and', 'not',
    'but', 'if', 'that', 'this', 'it', 'as', 'so', 'than', 'its', 'my',
    'we', 'they', 'them', 'your', 'our', 'his', 'her', 'their', 'what',
    'which', 'who', 'when', 'where', 'how', 'all', 'each', 'every',
    'no', 'nor', 'too', 'very', 'just', 'about', 'up', 'out', 'then',
])

_STOP_WORDS = _STOP_WORDS_PL | _STOP_WORDS_EN

_TOKEN_RE = re.compile(r'[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ0-9_]+', re.UNICODE)


def tokenize(text):
    """Tokenize text into lowercase word tokens, remove stop words."""
    tokens = _TOKEN_RE.findall(text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


# ═══════════════════════════════════════════════════════════════════
#  File chunking
# ═══════════════════════════════════════════════════════════════════

CHUNK_SIZE = 400       # ~400 words per chunk (good for small context windows)
CHUNK_OVERLAP = 60     # 60 word overlap between chunks

_TEXT_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.htm', '.css', '.scss',
    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
    '.md', '.txt', '.rst', '.log', '.csv',
    '.sh', '.bash', '.zsh', '.fish',
    '.c', '.h', '.cpp', '.hpp', '.java', '.go', '.rs', '.rb', '.php',
    '.sql', '.xml', '.env',
    '.vue', '.svelte', '.astro',
}

_IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif',
    '.heic', '.heif', '.avif', '.svg',
}

_MAX_INDEX_FILE_SIZE = 1024 * 1024    # 1 MB — skip very large files
_MAX_IMAGE_META_SIZE = 10 * 1024 * 1024  # 10 MB for EXIF reading


def _is_text(path):
    ext = os.path.splitext(path.lower())[1]
    return ext in _TEXT_EXTENSIONS


def _is_image(path):
    ext = os.path.splitext(path.lower())[1]
    return ext in _IMAGE_EXTENSIONS


def chunk_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """Split text into overlapping word-based chunks."""
    words = text.split()
    if len(words) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = ' '.join(words[start:end])
        chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def extract_image_metadata(path):
    """Extract metadata from image file: EXIF, filename, dimensions."""
    meta_parts = []
    basename = os.path.basename(path)
    name_no_ext = os.path.splitext(basename)[0]

    # Filename as semantic info (replace separators with spaces)
    clean_name = re.sub(r'[-_.]', ' ', name_no_ext)
    meta_parts.append(f"Zdjęcie: {clean_name}")
    meta_parts.append(f"Plik: {basename}")

    # Directory info (often meaningful: "Wakacje 2024", "Urodziny")
    parent = os.path.basename(os.path.dirname(path))
    if parent:
        meta_parts.append(f"Folder: {parent}")

    # File date
    try:
        mtime = os.path.getmtime(path)
        meta_parts.append(f"Data pliku: {time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}")
    except Exception:
        pass

    if _HAS_PIL:
        try:
            with Image.open(path) as img:
                w, h = img.size
                meta_parts.append(f"Wymiary: {w}x{h}")

                # EXIF data
                exif_data = img.getexif()
                if exif_data:
                    for tag_id, value in exif_data.items():
                        tag_name = EXIF_TAGS.get(tag_id, str(tag_id))
                        if tag_name in ('ImageDescription', 'Make', 'Model',
                                        'DateTime', 'DateTimeOriginal',
                                        'GPSInfo', 'Artist', 'Copyright',
                                        'Software', 'XPTitle', 'XPComment',
                                        'XPSubject', 'XPKeywords'):
                            val_str = str(value).strip()
                            if val_str and len(val_str) < 200:
                                meta_parts.append(f"{tag_name}: {val_str}")
        except Exception:
            pass

    return '\n'.join(meta_parts)


# ═══════════════════════════════════════════════════════════════════
#  TF-IDF Vector Store
# ═══════════════════════════════════════════════════════════════════

class VectorStore:
    """Lightweight TF-IDF based vector store with numpy.
    Each user gets their own index stored in data/rag/<username>/."""

    def __init__(self, username):
        self.username = username
        self._store_dir = data_path(os.path.join('rag', username))
        os.makedirs(self._store_dir, exist_ok=True)

        # In-memory index
        self._chunks = []       # list of dicts: {id, path, text, index_type, tokens, mtime}
        self._vocabulary = {}   # term → idx in vocabulary
        self._idf = None        # numpy array of IDF values
        self._sparse_rows = None  # list of list of (col_idx, value) tuples — sparse TF-IDF
        self._lock = threading.Lock()
        self._dirty = False

        # Load persisted index
        self._load()

    def _meta_path(self):
        return os.path.join(self._store_dir, 'index_meta.json')

    def _matrix_path(self):
        return os.path.join(self._store_dir, 'tfidf_matrix.npz')

    def _load(self):
        """Load persisted index from disk."""
        mp = self._meta_path()
        if not os.path.isfile(mp):
            return
        try:
            with open(mp, 'r') as f:
                meta = json.load(f)
            self._chunks = meta.get('chunks', [])
            self._vocabulary = meta.get('vocabulary', {})

            mxp = self._matrix_path()
            if os.path.isfile(mxp) and self._chunks:
                loaded = np.load(mxp, allow_pickle=False)
                self._idf = loaded['idf']
                # Load sparse CSR data
                if 'row_ptr' in loaded:
                    row_ptr = loaded['row_ptr']
                    col_idx = loaded['col_idx']
                    values = loaded['values']
                    self._sparse_rows = []
                    for r in range(len(row_ptr) - 1):
                        start, end = int(row_ptr[r]), int(row_ptr[r + 1])
                        self._sparse_rows.append(
                            list(zip(col_idx[start:end].tolist(), values[start:end].tolist()))
                        )
                elif 'matrix' in loaded:
                    # Legacy dense matrix — convert to sparse
                    matrix = loaded['matrix']
                    self._sparse_rows = []
                    for i in range(matrix.shape[0]):
                        row = []
                        for j in range(matrix.shape[1]):
                            if matrix[i, j] != 0:
                                row.append((j, float(matrix[i, j])))
                        self._sparse_rows.append(row)
        except Exception:
            self._chunks = []
            self._vocabulary = {}
            self._sparse_rows = None
            self._idf = None

    def _save(self):
        """Persist index to disk."""
        try:
            # Strip tokens from chunks before saving (they can be re-derived)
            chunks_for_save = []
            for c in self._chunks:
                sc = dict(c)
                sc.pop('tokens', None)
                chunks_for_save.append(sc)

            meta = {
                'chunks': chunks_for_save,
                'vocabulary': self._vocabulary,
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'stats': self.get_stats(),
            }
            with open(self._meta_path(), 'w') as f:
                json.dump(meta, f, ensure_ascii=False)

            # Save sparse matrix in CSR-like format
            if self._sparse_rows is not None and self._idf is not None:
                row_ptr = [0]
                col_indices = []
                values = []
                for row in self._sparse_rows:
                    for ci, val in row:
                        col_indices.append(ci)
                        values.append(val)
                    row_ptr.append(len(col_indices))
                np.savez_compressed(
                    self._matrix_path(),
                    idf=self._idf,
                    row_ptr=np.array(row_ptr, dtype=np.int32),
                    col_idx=np.array(col_indices, dtype=np.int32),
                    values=np.array(values, dtype=np.float32),
                )
        except Exception:
            pass

    def _rebuild_tfidf(self):
        """Rebuild TF-IDF sparse representation from current chunks."""
        if not self._chunks:
            self._vocabulary = {}
            self._idf = None
            self._sparse_rows = None
            return

        # Build vocabulary from all chunks
        doc_freq = Counter()
        all_token_lists = []
        for i, chunk in enumerate(self._chunks):
            tokens = chunk.get('tokens') or tokenize(chunk['text'])
            chunk['tokens'] = tokens
            unique_tokens = set(tokens)
            for t in unique_tokens:
                doc_freq[t] += 1
            all_token_lists.append(tokens)
            if _HAS_GEVENT and i % 50 == 0:
                gevent.sleep(0)

        # Sort vocab for consistent ordering
        vocab_terms = sorted(doc_freq.keys())
        self._vocabulary = {term: idx for idx, term in enumerate(vocab_terms)}
        n_docs = len(self._chunks)

        if not vocab_terms:
            self._idf = np.array([], dtype=np.float32)
            self._sparse_rows = [[] for _ in range(n_docs)]
            return

        # Compute IDF: log(N / df) + 1
        self._idf = np.array(
            [math.log(n_docs / doc_freq[t]) + 1.0 for t in vocab_terms],
            dtype=np.float32,
        )

        # Build sparse TF-IDF rows
        sparse_rows = []
        for i, tokens in enumerate(all_token_lists):
            if not tokens:
                sparse_rows.append([])
                continue
            tf = Counter(tokens)
            row = []
            norm_sq = 0.0
            for term, count in tf.items():
                j = self._vocabulary.get(term)
                if j is not None:
                    v = (count / len(tokens)) * self._idf[j]
                    if v > 0:
                        row.append((j, v))
                        norm_sq += v * v
            # L2 normalize
            if norm_sq > 0:
                norm = math.sqrt(norm_sq)
                row = [(ci, val / norm) for ci, val in row]
            sparse_rows.append(row)
            if _HAS_GEVENT and i % 50 == 0:
                gevent.sleep(0)

        self._sparse_rows = sparse_rows

    def add_document(self, path, content, index_type='documents', mtime=None):
        """Add a text document (possibly in chunks) to the index."""
        with self._lock:
            # Remove existing entries for this path
            self._chunks = [c for c in self._chunks if c['path'] != path]

            if not content or not content.strip():
                return 0

            chunks = chunk_text(content)
            for i, chunk_text_str in enumerate(chunks):
                tokens = tokenize(chunk_text_str)
                if not tokens:
                    continue
                chunk_id = hashlib.md5(f"{path}:{i}".encode()).hexdigest()[:12]
                self._chunks.append({
                    'id': chunk_id,
                    'path': path,
                    'chunk_idx': i,
                    'text': chunk_text_str[:2000],   # cap storage
                    'index_type': index_type,
                    'tokens': tokens,
                    'mtime': mtime or time.time(),
                })
            self._dirty = True
            return len(chunks)

    def add_image(self, path, metadata_text=None, mtime=None):
        """Add image metadata to the gallery index."""
        with self._lock:
            self._chunks = [c for c in self._chunks if c['path'] != path]

            if metadata_text is None:
                metadata_text = extract_image_metadata(path)

            if not metadata_text:
                return 0

            tokens = tokenize(metadata_text)
            if not tokens:
                return 0

            chunk_id = hashlib.md5(path.encode()).hexdigest()[:12]
            self._chunks.append({
                'id': chunk_id,
                'path': path,
                'chunk_idx': 0,
                'text': metadata_text[:2000],
                'index_type': 'gallery',
                'tokens': tokens,
                'mtime': mtime or time.time(),
            })
            self._dirty = True
            return 1

    def remove_path(self, path):
        """Remove all chunks for a given file path."""
        with self._lock:
            before = len(self._chunks)
            self._chunks = [c for c in self._chunks if c['path'] != path]
            if len(self._chunks) != before:
                self._dirty = True

    def commit(self):
        """Rebuild TF-IDF matrix and persist."""
        with self._lock:
            if self._dirty:
                self._rebuild_tfidf()
                self._save()
                self._dirty = False

    def search(self, query, index_type=None, sandbox_root=None, top_k=5, min_score=0.05):
        """Search for chunks matching query using sparse dot product.
        Returns list of {path, text, score, index_type, chunk_idx}."""
        with self._lock:
            if not self._chunks or self._sparse_rows is None:
                return []
            if self._idf is None or len(self._idf) == 0:
                return []

            # Vectorize query (sparse)
            q_tokens = tokenize(query)
            if not q_tokens:
                return []

            q_tf = Counter(q_tokens)
            q_sparse = {}  # col_idx → value
            for term, count in q_tf.items():
                j = self._vocabulary.get(term)
                if j is not None:
                    q_sparse[j] = (count / len(q_tokens)) * self._idf[j]

            if not q_sparse:
                return []

            # L2 normalize query
            q_norm = math.sqrt(sum(v * v for v in q_sparse.values()))
            if q_norm == 0:
                return []
            q_sparse = {k: v / q_norm for k, v in q_sparse.items()}

            # Sparse dot product for each document
            scores = []
            for row in self._sparse_rows:
                score = 0.0
                for ci, val in row:
                    if ci in q_sparse:
                        score += val * q_sparse[ci]
                scores.append(score)

            # Get top-k indices
            scores_arr = np.array(scores, dtype=np.float32)
            top_indices = np.argsort(scores_arr)[::-1]

            results = []
            for i in top_indices:
                if scores_arr[i] < min_score:
                    break
                chunk = self._chunks[i]

                if index_type and chunk['index_type'] != index_type:
                    continue

                if sandbox_root:
                    rpath = os.path.realpath(chunk['path'])
                    if not (rpath.startswith(sandbox_root + '/') or rpath == sandbox_root):
                        continue

                results.append({
                    'path': chunk['path'],
                    'text': chunk['text'],
                    'score': float(scores_arr[i]),
                    'index_type': chunk['index_type'],
                    'chunk_idx': chunk.get('chunk_idx', 0),
                })

                if len(results) >= top_k:
                    break

            return results

    def get_stats(self):
        """Return index statistics."""
        doc_paths = set()
        gallery_paths = set()
        for c in self._chunks:
            if c['index_type'] == 'gallery':
                gallery_paths.add(c['path'])
            else:
                doc_paths.add(c['path'])
        return {
            'total_chunks': len(self._chunks),
            'document_files': len(doc_paths),
            'gallery_files': len(gallery_paths),
            'vocabulary_size': len(self._vocabulary),
        }

    def get_indexed_paths(self):
        """Return set of all indexed file paths."""
        return {c['path'] for c in self._chunks}

    def clear(self):
        """Remove entire index."""
        with self._lock:
            self._chunks = []
            self._vocabulary = {}
            self._idf = None
            self._sparse_rows = None
            self._dirty = False
            self._save()


# ═══════════════════════════════════════════════════════════════════
#  Indexer — scans directories and builds vector index
# ═══════════════════════════════════════════════════════════════════

# Safe base paths — RAG indexer will ONLY scan under these prefixes
_SAFE_INDEX_ROOTS = ('/home/',)

# Throttle: sleep this many seconds after every N files to reduce CPU/IO load
_THROTTLE_EVERY_N = 20
_THROTTLE_SLEEP = 0.05   # 50ms pause

# Maximum total files to index per run (safety valve for N150)
_MAX_INDEX_FILES = 10000


class RAGIndexer:
    """Manages indexing of user files into the vector store."""

    def __init__(self, username, sandbox_root=None):
        self.username = username
        self.sandbox_root = sandbox_root  # None = admin (unrestricted)
        self.store = VectorStore(username)
        self._indexing = False
        self._progress = {'status': 'idle', 'indexed': 0, 'total': 0, 'current': ''}
        self._lock = threading.Lock()

    def get_status(self):
        """Return indexing status and stats."""
        stats = self.store.get_stats()
        return {
            'indexing': self._indexing,
            'progress': dict(self._progress),
            'stats': stats,
        }

    def index_directory(self, directory, recursive=True, socketio=None):
        """Index all eligible files in a directory. Runs synchronously.
        Call in a thread for background indexing."""
        if self._indexing:
            return {'error': 'Indeksowanie już trwa'}

        directory = os.path.realpath(directory)

        # Safety: only index under allowed base paths (never /, /etc, /sys, etc.)
        if not any(directory.startswith(prefix) or directory == prefix.rstrip('/') for prefix in _SAFE_INDEX_ROOTS):
            return {'error': f'Indeksowanie dozwolone tylko w: {", ".join(_SAFE_INDEX_ROOTS)}'}

        # Sandbox check for non-admin users
        if self.sandbox_root:
            if not (directory.startswith(self.sandbox_root + '/') or directory == self.sandbox_root):
                return {'error': 'Dostęp ograniczony do katalogu domowego'}

        if not os.path.isdir(directory):
            return {'error': 'Katalog nie istnieje'}

        self._indexing = True
        self._progress = {'status': 'scanning', 'indexed': 0, 'total': 0, 'current': ''}

        try:
            # Collect eligible files
            eligible = []
            for root_dir, dirs, files in os.walk(directory) if recursive else [(directory, [], os.listdir(directory))]:
                # Skip hidden dirs and common noise
                if not recursive:
                    files_only = [f for f in files if os.path.isfile(os.path.join(directory, f))]
                    root_dir = directory
                    files = files_only
                else:
                    dirs[:] = [d for d in dirs if not d.startswith('.') and d not in (
                        'node_modules', '__pycache__', '.git', '.venv', 'venv',
                        '.cache', '.local', '.config', 'snap',
                    )]

                for fname in files:
                    if fname.startswith('.'):
                        continue
                    fpath = os.path.join(root_dir, fname)
                    try:
                        fsize = os.path.getsize(fpath)
                    except OSError:
                        continue

                    if _is_text(fpath) and fsize <= _MAX_INDEX_FILE_SIZE:
                        eligible.append(('text', fpath, fsize))
                    elif _is_image(fpath) and fsize <= _MAX_IMAGE_META_SIZE:
                        eligible.append(('image', fpath, fsize))

            # Limit total files to prevent overloading N150
            if len(eligible) > _MAX_INDEX_FILES:
                eligible = eligible[:_MAX_INDEX_FILES]

            self._progress['total'] = len(eligible)
            self._progress['status'] = 'indexing'

            indexed_paths = self.store.get_indexed_paths()
            added = 0

            for idx, (ftype, fpath, fsize) in enumerate(eligible):
                self._progress['current'] = os.path.basename(fpath)
                self._progress['indexed'] = idx

                try:
                    # Check if file changed since last index
                    mtime = os.path.getmtime(fpath)
                    existing = [c for c in self.store._chunks if c['path'] == fpath]
                    if existing and existing[0].get('mtime', 0) >= mtime:
                        continue  # unchanged

                    if ftype == 'text':
                        with open(fpath, 'r', errors='replace') as f:
                            content = f.read()
                        n = self.store.add_document(fpath, content, 'documents', mtime)
                        added += n
                    elif ftype == 'image':
                        meta = extract_image_metadata(fpath)
                        n = self.store.add_image(fpath, meta, mtime)
                        added += n
                except Exception:
                    continue

                # Throttle: pause every N files to reduce CPU/IO pressure
                if idx > 0 and idx % _THROTTLE_EVERY_N == 0:
                    import time as _time
                    _time.sleep(_THROTTLE_SLEEP)
                    # Yield to gevent event loop
                    if _HAS_GEVENT:
                        gevent.sleep(0)
                elif _HAS_GEVENT and idx % 5 == 0:
                    gevent.sleep(0)  # yield frequently

                # Emit progress periodically
                if socketio and idx % 50 == 0:
                    socketio.emit('rag_progress', {
                        'username': self.username,
                        'indexed': idx,
                        'total': len(eligible),
                        'current': os.path.basename(fpath),
                    })

            # Remove paths that no longer exist
            for old_path in list(indexed_paths):
                if not os.path.exists(old_path):
                    self.store.remove_path(old_path)

            # Rebuild and persist
            self.store.commit()

            self._progress = {
                'status': 'done',
                'indexed': len(eligible),
                'total': len(eligible),
                'current': '',
                'added_chunks': added,
            }
            return {
                'ok': True,
                'files_scanned': len(eligible),
                'chunks_added': added,
                'stats': self.store.get_stats(),
            }

        except Exception as e:
            self._progress = {'status': 'error', 'indexed': 0, 'total': 0, 'current': str(e)}
            return {'error': str(e)}
        finally:
            self._indexing = False

    def search(self, query, index_type=None, top_k=5):
        """Search the index with sandbox enforcement."""
        # Ensure sparse matrix is built
        if self.store._sparse_rows is None and self.store._chunks:
            self.store.commit()

        return self.store.search(
            query,
            index_type=index_type,
            sandbox_root=self.sandbox_root,
            top_k=top_k,
        )

    def clear(self):
        """Clear the entire index for this user."""
        self.store.clear()


# ═══════════════════════════════════════════════════════════════════
#  Query classifier — detect if user asks about images or documents
# ═══════════════════════════════════════════════════════════════════

_GALLERY_KEYWORDS = {
    # Polish
    'zdjęcie', 'zdjęcia', 'zdjęć', 'foto', 'fotka', 'fotki', 'fotografii',
    'obraz', 'obrazek', 'obrazy', 'galeria', 'album',
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'heic',
    'aparat', 'kamera',
    # English
    'photo', 'photos', 'picture', 'pictures', 'image', 'images',
    'gallery', 'album', 'photograph', 'snapshot', 'camera',
}

_DOCUMENT_KEYWORDS = {
    # Polish
    'dokument', 'dokumenty', 'plik', 'pliki', 'tekst', 'notatka', 'notatki',
    'kod', 'skrypt', 'config', 'konfiguracja', 'log', 'logi', 'raport',
    # English
    'document', 'documents', 'file', 'files', 'text', 'note', 'notes',
    'code', 'script', 'config', 'configuration', 'log', 'logs', 'report',
}


def classify_query(query):
    """Classify query as 'gallery', 'documents', or None (both).
    Returns the detected index_type or None for mixed/ambiguous."""
    tokens = set(tokenize(query))
    gallery_score = len(tokens & _GALLERY_KEYWORDS)
    doc_score = len(tokens & _DOCUMENT_KEYWORDS)

    if gallery_score > 0 and doc_score == 0:
        return 'gallery'
    if doc_score > 0 and gallery_score == 0:
        return 'documents'
    # Ambiguous or no keywords — search both
    return None


# ═══════════════════════════════════════════════════════════════════
#  RAG context builder — formats search results for LLM injection
# ═══════════════════════════════════════════════════════════════════

def build_rag_context(results, query):
    """Format RAG search results into a context string for the LLM.
    Includes source attribution."""
    if not results:
        return '', []

    parts = []
    sources = []
    seen_paths = set()

    for r in results:
        path = r['path']
        basename = os.path.basename(path)
        idx_type = r['index_type']
        # Cap each chunk to ~500 chars to keep total prompt within n_ctx=2048
        text = r['text'][:500]

        if path not in seen_paths:
            seen_paths.add(path)
            sources.append({
                'path': path,
                'name': basename,
                'type': idx_type,
                'score': r['score'],
            })

        type_label = '📷 Galeria' if idx_type == 'gallery' else '📄 Dokument'
        parts.append(f"[{type_label}: {basename}]\n{text}")

    context = (
        "KONTEKST Z BAZY WIEDZY UŻYTKOWNIKA:\n"
        "Poniższe fragmenty zostały automatycznie wyszukane z plików użytkownika "
        "na podstawie jego pytania. Wykorzystaj je w odpowiedzi i powołuj się na źródła.\n"
        "---\n" +
        "\n---\n".join(parts) +
        "\n---\n"
        "INSTRUKCJA: Odpowiedz na pytanie użytkownika na podstawie powyższego kontekstu. "
        "Na końcu odpowiedzi wymień źródła w formacie: 📎 Źródło: [nazwa_pliku]"
    )

    return context, sources


# ═══════════════════════════════════════════════════════════════════
#  Module-level indexer cache (per-user)
# ═══════════════════════════════════════════════════════════════════

_indexer_cache = {}
_indexer_cache_lock = threading.Lock()


def get_indexer(username, sandbox_root=None):
    """Get or create RAG indexer for a user."""
    with _indexer_cache_lock:
        key = username
        if key not in _indexer_cache:
            _indexer_cache[key] = RAGIndexer(username, sandbox_root)
        else:
            # Update sandbox root in case role changed
            _indexer_cache[key].sandbox_root = sandbox_root
        return _indexer_cache[key]
