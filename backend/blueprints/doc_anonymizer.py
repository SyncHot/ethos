"""
EthOS -- Document Anonymizer
Anonymize medical PDF/DOCX documents using local Bielik LLM.
Extracts text, identifies PII via LLM, replaces with placeholders.

Endpoints:
  POST /api/doc-anonymizer/upload           -- upload and anonymize a file
  GET  /api/doc-anonymizer/jobs             -- list anonymization jobs
  GET  /api/doc-anonymizer/download/<job_id> -- download anonymized file
  DELETE /api/doc-anonymizer/job/<job_id>   -- delete a job
  GET  /api/doc-anonymizer/status           -- app + AI dependency status
  POST /api/doc-anonymizer/install          -- install (via register_pkg_routes)
  POST /api/doc-anonymizer/uninstall        -- uninstall
  GET  /api/doc-anonymizer/pkg-status       -- package status

SocketIO events:
  anon_progress  -- real-time anonymization progress updates
"""

import json
import os
import re
import sys
import time
import uuid
import logging
import threading

from flask import Blueprint, request, jsonify, send_file, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import register_pkg_routes, get_username_or
from host import data_path

log = logging.getLogger('ethos.doc_anonymizer')

doc_anonymizer_bp = Blueprint('doc_anonymizer', __name__,
                              url_prefix='/api/doc-anonymizer')

# -- Paths ------------------------------------------------------------------
_JOBS_DIR = data_path('doc_anonymizer_jobs')


def _ensure_jobs_dir():
    os.makedirs(_JOBS_DIR, exist_ok=True)


def _job_dir(job_id):
    return os.path.join(_JOBS_DIR, job_id)


def _get_username():
    return get_username_or('admin')


# -- Text extraction --------------------------------------------------------

def _extract_text_pdf(filepath):
    """Extract text from a PDF file page by page."""
    import PyPDF2
    pages = []
    with open(filepath, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            text = page.extract_text() or ''
            pages.append(text)
    return pages


def _extract_text_docx(filepath):
    """Extract text from a DOCX file paragraph by paragraph."""
    import docx
    doc = docx.Document(filepath)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return paragraphs


# -- LLM anonymization -----------------------------------------------------

_SYSTEM_PROMPT = (
    "Jestes ekspertem od anonimizacji dokumentow medycznych zgodnie z RODO/GDPR.\n"
    "Twoim zadaniem jest zidentyfikowanie WSZYSTKICH danych osobowych (PII) w podanym tekscie\n"
    "i zwrocenie ich jako lista JSON.\n\n"
    "Szukaj nastepujacych kategorii danych:\n"
    "- IMIE: imiona (np. Jan, Maria, Krzysztof)\n"
    "- NAZWISKO: nazwiska (np. Kowalski, Nowak)\n"
    "- PESEL: numery PESEL (11 cyfr)\n"
    "- DATA_URODZENIA: daty urodzenia\n"
    "- ADRES: adresy zamieszkania (ulice, miasta, kody pocztowe)\n"
    "- TELEFON: numery telefonow\n"
    "- EMAIL: adresy email\n"
    "- NR_PACJENTA: numery pacjentow, numery kart, ID medyczne\n"
    "- NR_DOKUMENTU: numery dowodow, paszportow\n"
    "- NAZWA_PLACOWKI: nazwy szpitali, przychodni, gabinetow (jesli moga identyfikowac pacjenta)\n"
    "- LEKARZ: imiona i nazwiska lekarzy\n"
    "- INNE_PII: inne dane pozwalajace na identyfikacje osoby\n\n"
    "Odpowiedz TYLKO w formacie JSON -- tablica obiektow:\n"
    '[{"text": "oryginalny tekst", "category": "KATEGORIA"}, ...]\n\n'
    "Jesli nie ma danych osobowych, zwroc pusta tablice: []\n"
    "Nie dodawaj zadnych komentarzy, tylko JSON."
)

_USER_PROMPT_TEMPLATE = (
    "Zidentyfikuj WSZYSTKIE dane osobowe (PII) w ponizszym tekscie dokumentu medycznego:\n\n"
    "---\n{text}\n---\n\n"
    "Odpowiedz TYLKO tablica JSON z znalezionymi danymi osobowymi."
)


def _call_llm(text_chunk):
    """Send text to the local LLM and get PII entities back."""
    try:
        from model_library import get_library
    except ImportError:
        raise RuntimeError('AI Chat not installed - model_library unavailable')

    lib = get_library()
    llm, err = lib.load_model()
    if err:
        raise RuntimeError('Cannot load LLM model: ' + str(err))

    lib.touch_model()

    messages = [
        {'role': 'system', 'content': _SYSTEM_PROMPT},
        {'role': 'user', 'content': _USER_PROMPT_TEMPLATE.format(text=text_chunk[:3000])},
    ]

    resp = llm.create_chat_completion(
        messages=messages,
        max_tokens=2048,
        temperature=0.1,
    )

    content = resp['choices'][0]['message']['content'].strip()

    # Parse JSON from response -- handle markdown code blocks
    if '```' in content:
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
        if match:
            content = match.group(1).strip()

    try:
        entities = json.loads(content)
        if not isinstance(entities, list):
            entities = []
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            try:
                entities = json.loads(match.group(0))
            except json.JSONDecodeError:
                entities = []
        else:
            entities = []

    return [e for e in entities if isinstance(e, dict) and 'text' in e and 'category' in e]


# -- Replacement logic ------------------------------------------------------

_PLACEHOLDER_MAP = {
    'IMIE': '[IMIE]',
    'NAZWISKO': '[NAZWISKO]',
    'PESEL': '[PESEL]',
    'DATA_URODZENIA': '[DATA_URODZENIA]',
    'ADRES': '[ADRES]',
    'TELEFON': '[TELEFON]',
    'EMAIL': '[EMAIL]',
    'NR_PACJENTA': '[NR_PACJENTA]',
    'NR_DOKUMENTU': '[NR_DOKUMENTU]',
    'NAZWA_PLACOWKI': '[PLACOWKA]',
    'LEKARZ': '[LEKARZ]',
    'INNE_PII': '[DANE_OSOBOWE]',
}


def _replace_entities_in_text(text, entities):
    """Replace PII entities with placeholders in text."""
    seen = set()
    unique = []
    for e in entities:
        key = (e['text'], e.get('category', 'INNE_PII'))
        if key not in seen:
            seen.add(key)
            unique.append(e)

    unique.sort(key=lambda e: len(e['text']), reverse=True)

    result = text
    replacements = []
    for e in unique:
        pii_text = e['text']
        category = e.get('category', 'INNE_PII')
        placeholder = _PLACEHOLDER_MAP.get(category, '[' + category + ']')

        if pii_text in result:
            count = result.count(pii_text)
            result = result.replace(pii_text, placeholder)
            replacements.append({
                'original': pii_text,
                'category': category,
                'placeholder': placeholder,
                'occurrences': count,
            })

    return result, replacements


# -- Document generation ----------------------------------------------------

def _generate_pdf(anonymized_pages, output_path):
    """Generate a PDF from anonymized text pages using reportlab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_name = 'Helvetica'
    for font_path in ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                      '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf']:
        if os.path.exists(font_path):
            try:
                fname = os.path.basename(font_path).replace('.ttf', '')
                pdfmetrics.registerFont(TTFont(fname, font_path))
                font_name = fname
                break
            except Exception:
                continue

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm)

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        'AnonBody', parent=styles['Normal'],
        fontName=font_name, fontSize=10, leading=14,
        alignment=TA_LEFT,
    )
    header_style = ParagraphStyle(
        'AnonHeader', parent=styles['Normal'],
        fontName=font_name, fontSize=8, leading=10,
        textColor='gray',
    )

    story = [
        Paragraph('DOKUMENT ZANONIMIZOWANY', header_style),
        Spacer(1, 0.5 * cm),
    ]

    for i, page_text in enumerate(anonymized_pages):
        safe = page_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        safe = safe.replace('\n', '<br/>')
        story.append(Paragraph(safe, body_style))
        if i < len(anonymized_pages) - 1:
            story.append(PageBreak())

    doc.build(story)


def _anonymize_docx_inplace(src_path, output_path, all_entities):
    """Anonymize a DOCX preserving original formatting."""
    import docx

    seen = set()
    unique = []
    for e in all_entities:
        key = (e['text'], e.get('category', 'INNE_PII'))
        if key not in seen:
            seen.add(key)
            unique.append(e)
    unique.sort(key=lambda e: len(e['text']), reverse=True)

    replacements = {}
    for e in unique:
        category = e.get('category', 'INNE_PII')
        placeholder = _PLACEHOLDER_MAP.get(category, '[' + category + ']')
        replacements[e['text']] = placeholder

    document = docx.Document(src_path)

    def _replace_in_paragraph(para):
        full_text = para.text
        if not full_text.strip():
            return
        new_text = full_text
        for original, placeholder in replacements.items():
            new_text = new_text.replace(original, placeholder)
        if new_text == full_text:
            return
        if para.runs:
            para.runs[0].text = new_text
            for run in para.runs[1:]:
                run.text = ''
        else:
            para.text = new_text

    for para in document.paragraphs:
        _replace_in_paragraph(para)

    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _replace_in_paragraph(para)

    document.save(output_path)


# -- Background anonymization task -----------------------------------------

_active_jobs = {}
_jobs_lock = threading.Lock()


def _emit_progress(job_id, stage, percent, message):
    with _jobs_lock:
        if job_id in _active_jobs:
            _active_jobs[job_id].update({
                'stage': stage, 'progress': percent, 'message': message,
            })
    sio = getattr(doc_anonymizer_bp, '_socketio', None)
    if sio:
        sio.emit('anon_progress', {
            'job_id': job_id, 'stage': stage,
            'percent': percent, 'message': message,
        })


def _run_anonymization(job_id, src_path, filename, file_ext, username):
    """Background: extract text -> LLM -> replace -> generate output."""
    job = _job_dir(job_id)
    meta_path = os.path.join(job, 'meta.json')

    try:
        _emit_progress(job_id, 'extracting', 10,
                       'Wyodrebnianie tekstu z dokumentu...')

        if file_ext == '.pdf':
            text_parts = _extract_text_pdf(src_path)
        elif file_ext in ('.docx', '.doc'):
            text_parts = _extract_text_docx(src_path)
        else:
            raise ValueError('Unsupported file type: ' + file_ext)

        if not text_parts or all(not t.strip() for t in text_parts):
            raise ValueError(
                'Nie udalo sie wyodrebnic tekstu z dokumentu. '
                'Plik moze byc zeskanowany (obraz) - wymagane OCR.')

        total_parts = len(text_parts)
        all_entities = []

        for i, text_part in enumerate(text_parts):
            if not text_part.strip():
                continue
            pct = 20 + int(60 * (i / max(total_parts, 1)))
            _emit_progress(job_id, 'analyzing', pct,
                           'Analiza LLM - fragment %d/%d...' % (i + 1, total_parts))
            try:
                entities = _call_llm(text_part)
                all_entities.extend(entities)
            except Exception as e:
                log.warning('[doc_anonymizer] LLM error on chunk %d: %s', i, e)

        _emit_progress(job_id, 'replacing', 85,
                       'Zastepowanie danych osobowych...')

        anonymized_parts = []
        total_replacements = []
        for part in text_parts:
            anon_text, repls = _replace_entities_in_text(part, all_entities)
            anonymized_parts.append(anon_text)
            total_replacements.extend(repls)

        seen_repls = {}
        for r in total_replacements:
            key = r['original']
            if key not in seen_repls:
                seen_repls[key] = r
            else:
                seen_repls[key]['occurrences'] += r['occurrences']

        _emit_progress(job_id, 'generating', 90,
                       'Generowanie zanonimizowanego dokumentu...')

        out_filename = 'anonymized_' + filename
        out_path = os.path.join(job, out_filename)

        if file_ext == '.pdf':
            _generate_pdf(anonymized_parts, out_path)
        elif file_ext in ('.docx', '.doc'):
            _anonymize_docx_inplace(src_path, out_path, all_entities)

        meta = {
            'job_id': job_id,
            'status': 'done',
            'filename': filename,
            'output_filename': out_filename,
            'file_ext': file_ext,
            'username': username,
            'created_at': time.time(),
            'completed_at': time.time(),
            'entities_found': len(seen_repls),
            'replacements': list(seen_repls.values()),
            'pages_analyzed': total_parts,
        }
        with open(meta_path, 'w') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        _emit_progress(job_id, 'done', 100,
                       'Zanonimizowano - znaleziono %d danych osobowych' % len(seen_repls))

        with _jobs_lock:
            if job_id in _active_jobs:
                _active_jobs[job_id]['status'] = 'done'

    except Exception as e:
        log.error('[doc_anonymizer] Anonymization failed for %s: %s', job_id, e)
        meta = {
            'job_id': job_id, 'status': 'error',
            'filename': filename, 'username': username,
            'created_at': time.time(), 'error': str(e),
        }
        with open(meta_path, 'w') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _emit_progress(job_id, 'error', 0, str(e))
        with _jobs_lock:
            if job_id in _active_jobs:
                _active_jobs[job_id]['status'] = 'error'


# -- Routes -----------------------------------------------------------------

@doc_anonymizer_bp.route('/upload', methods=['POST'])
def anon_upload():
    """Upload a PDF/DOCX file for anonymization."""
    _ensure_jobs_dir()

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'Empty filename'}), 400

    filename = f.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ('.pdf', '.docx', '.doc'):
        return jsonify({'error': 'Obslugiwane formaty: PDF, DOCX'}), 400

    # Check AI Chat dependency
    try:
        from model_library import get_library
        lib = get_library()
        active = lib.get_active_model()
        if not active:
            return jsonify({
                'error': 'Brak aktywnego modelu LLM. '
                         'Otworz AI Assistant i pobierz model Bielik 7B.'
            }), 400
    except ImportError:
        return jsonify({
            'error': 'AI Assistant nie jest zainstalowany. '
                     'Zainstaluj go najpierw w Package Center.'
        }), 400

    job_id = uuid.uuid4().hex[:12]
    job = _job_dir(job_id)
    os.makedirs(job, exist_ok=True)

    src_path = os.path.join(job, 'original' + ext)
    f.save(src_path)

    username = _get_username()

    meta = {
        'job_id': job_id, 'status': 'processing',
        'filename': filename, 'file_ext': ext,
        'username': username, 'created_at': time.time(),
    }
    with open(os.path.join(job, 'meta.json'), 'w') as mf:
        json.dump(meta, mf, ensure_ascii=False)

    with _jobs_lock:
        _active_jobs[job_id] = {'status': 'processing', 'progress': 0}

    t = threading.Thread(target=_run_anonymization,
                         args=(job_id, src_path, filename, ext, username),
                         daemon=True)
    t.start()

    return jsonify({'ok': True, 'job_id': job_id, 'filename': filename})


@doc_anonymizer_bp.route('/jobs', methods=['GET'])
def anon_jobs():
    """List all anonymization jobs for the current user."""
    _ensure_jobs_dir()
    username = _get_username()
    jobs = []

    for entry in sorted(os.listdir(_JOBS_DIR), reverse=True):
        meta_path = os.path.join(_JOBS_DIR, entry, 'meta.json')
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if meta.get('username') == username or getattr(g, 'role', None) == 'admin':
                with _jobs_lock:
                    live = _active_jobs.get(entry, {})
                if live and meta.get('status') == 'processing':
                    meta['progress'] = live.get('progress', 0)
                    meta['message'] = live.get('message', '')
                jobs.append(meta)
        except Exception:
            continue

    return jsonify({'items': jobs})


@doc_anonymizer_bp.route('/download/<job_id>', methods=['GET'])
def anon_download(job_id):
    """Download the anonymized file."""
    job = _job_dir(job_id)
    meta_path = os.path.join(job, 'meta.json')

    if not os.path.isfile(meta_path):
        return jsonify({'error': 'Job not found'}), 404

    with open(meta_path) as f:
        meta = json.load(f)

    if meta.get('status') != 'done':
        return jsonify({'error': 'Anonymization not complete'}), 400

    out_file = os.path.join(job, meta['output_filename'])
    if not os.path.isfile(out_file):
        return jsonify({'error': 'Output file missing'}), 404

    if meta['file_ext'] == '.pdf':
        mime = 'application/pdf'
    else:
        mime = ('application/vnd.openxmlformats-officedocument'
                '.wordprocessingml.document')

    return send_file(out_file, mimetype=mime, as_attachment=True,
                     download_name=meta['output_filename'])


@doc_anonymizer_bp.route('/job/<job_id>', methods=['DELETE'])
def anon_delete_job(job_id):
    """Delete an anonymization job and its files."""
    import shutil
    job = _job_dir(job_id)
    if os.path.isdir(job):
        shutil.rmtree(job, ignore_errors=True)
    with _jobs_lock:
        _active_jobs.pop(job_id, None)
    return jsonify({'ok': True})


@doc_anonymizer_bp.route('/status', methods=['GET'])
def anon_app_status():
    """Check app status and AI Chat dependency."""
    result = {
        'installed': True,
        'ai_chat_available': False,
        'model_loaded': False,
        'active_model': None,
        'recommended_model': 'bielik-7b-q4',
    }

    try:
        from model_library import get_library
        result['ai_chat_available'] = True
        lib = get_library()
        active = lib.get_active_model()
        if active:
            result['active_model'] = active.get('name', active.get('id'))
        loaded_llm, loaded_id = lib.get_loaded_model()
        result['model_loaded'] = loaded_llm is not None
    except ImportError:
        pass

    return jsonify(result)


# -- Package routes (install / uninstall / pkg-status) ----------------------

def _on_uninstall(wipe):
    if wipe:
        import shutil
        if os.path.isdir(_JOBS_DIR):
            shutil.rmtree(_JOBS_DIR, ignore_errors=True)


register_pkg_routes(
    doc_anonymizer_bp,
    install_message='Document Anonymizer installed.',
    on_uninstall=_on_uninstall,
    wipe_dirs=[_JOBS_DIR],
)