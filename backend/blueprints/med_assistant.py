"""
EthOS -- Medical Assistant (Asystent Medyczny)
Analyze medical documents using Bielik LLM + OCR.
Patient folder management with 6 analysis modes.

Endpoints:
  GET  /api/med-assistant/status           -- app + AI dependency status
  GET  /api/med-assistant/patients         -- list patient folders
  POST /api/med-assistant/patients         -- create patient folder
  DELETE /api/med-assistant/patients/<name> -- delete patient folder
  GET  /api/med-assistant/patients/<name>/files -- list files in patient folder
  POST /api/med-assistant/patients/<name>/upload -- upload file to patient folder
  POST /api/med-assistant/analyze          -- run analysis on file(s)
  GET  /api/med-assistant/jobs             -- list analysis jobs
  GET  /api/med-assistant/job/<job_id>     -- get job details/result
  DELETE /api/med-assistant/job/<job_id>   -- delete a job
  GET  /api/med-assistant/job/<job_id>/export -- export result to file
  POST /api/med-assistant/install          -- install (via register_pkg_routes)
  POST /api/med-assistant/uninstall        -- uninstall
  GET  /api/med-assistant/pkg-status       -- package status

SocketIO events:
  med_progress  -- real-time analysis progress updates
"""

import json
import os
import re
import sys
import time
import uuid
import logging
import threading
import shutil

from flask import Blueprint, request, jsonify, send_file, g

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import register_pkg_routes, get_username_or
from host import data_path, host_run, get_user_home, safe_path, q

log = logging.getLogger('ethos.med_assistant')

med_assistant_bp = Blueprint('med_assistant', __name__,
                             url_prefix='/api/med-assistant')

# -- Paths ------------------------------------------------------------------
_JOBS_DIR = data_path('med_assistant_jobs')
_PATIENTS_FOLDER_NAME = 'Pacjenci'

_jobs_lock = threading.Lock()
_active_jobs = {}

# Sequential job queue — process one analysis at a time (LLM loads ~8GB)
_job_queue = []
_queue_lock = threading.Lock()
_worker_running = False


def _ensure_jobs_dir():
    os.makedirs(_JOBS_DIR, exist_ok=True)


def _job_dir(job_id):
    return os.path.join(_JOBS_DIR, job_id)


def _get_username():
    return get_username_or('admin')


def _patients_base(username):
    """Return base path for patient folders: /home/{user}/Pacjenci/"""
    home = get_user_home(username)
    base = os.path.join(home, _PATIENTS_FOLDER_NAME)
    os.makedirs(base, mode=0o750, exist_ok=True)
    return base


# -- Dependencies -----------------------------------------------------------

def _ensure_deps():
    """Install required pip packages and system tools if missing."""
    missing_pip = []
    try:
        import fitz  # noqa: F401
    except ImportError:
        missing_pip.append('PyMuPDF')
    try:
        import PyPDF2  # noqa: F401
    except ImportError:
        missing_pip.append('PyPDF2')
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing_pip.append('Pillow')
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        missing_pip.append('pytesseract')
    try:
        import docx  # noqa: F401
    except ImportError:
        missing_pip.append('python-docx')

    if missing_pip:
        pkgs = ' '.join(missing_pip)
        log.info('[med_assistant] Installing pip deps: %s', pkgs)
        host_run(f'/opt/ethos/venv/bin/pip install --quiet {pkgs}', timeout=120)

    if not shutil.which('pdftotext'):
        log.info('[med_assistant] Installing poppler-utils')
        host_run('apt-get install -y -qq poppler-utils', timeout=60)

    if not shutil.which('tesseract'):
        log.info('[med_assistant] Installing Tesseract OCR + Polish language pack')
        host_run('apt-get install -y -qq tesseract-ocr tesseract-ocr-pol', timeout=120)


# -- Socket.IO helpers ------------------------------------------------------

def _emit_progress(job_id, stage, percent, message):
    with _jobs_lock:
        if job_id in _active_jobs:
            _active_jobs[job_id].update({
                'stage': stage, 'progress': percent, 'message': message,
            })
    sio = getattr(med_assistant_bp, '_socketio', None)
    if sio:
        sio.emit('med_progress', {
            'job_id': job_id, 'stage': stage,
            'percent': percent, 'message': message,
        })


# -- Text extraction --------------------------------------------------------

_OCR_KEEP_UPPER = frozenset({
    'PESEL', 'NIP', 'REGON', 'KRS', 'PWZ', 'NFZ', 'ZUS', 'PIT', 'VAT',
    'KARTA', 'INFORMACYJNA', 'MR', 'CT', 'EKG', 'USG', 'RTG', 'MRI',
    'DNA', 'RNA', 'HIV', 'HCV', 'HBS', 'CRP', 'HDL', 'LDL', 'TSH',
    'BMI', 'EWUS', 'NZOZ', 'SP', 'ZOZ', 'II', 'III', 'IV', 'VI',
    'EF', 'LVEF', 'LVEDD', 'LVESD', 'TAPSE', 'INR', 'APTT', 'BNP',
})


def _normalize_ocr_text(text):
    """Normalize OCR text: ALL CAPS names -> Title Case, preserve abbreviations."""
    lines = text.split('\n')
    normalized = []
    for line in lines:
        words = line.split()
        new_words = []
        for word in words:
            stripped = word.strip('.,;:!?()[]/-')
            if (len(stripped) >= 3
                    and stripped.isupper()
                    and stripped.isalpha()
                    and stripped not in _OCR_KEEP_UPPER):
                new_words.append(word.replace(stripped, stripped.title()))
            else:
                new_words.append(word)
        normalized.append(' '.join(new_words))
    return '\n'.join(normalized)


def _cleanup_text(text):
    """Clean up extracted text."""
    text = text.replace('\x00', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _extract_text_pdf(filepath):
    """Extract text from PDF using pdftotext, fallback to PyPDF2, then OCR."""
    import subprocess
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', filepath, '-'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout
            pages = raw.split('\x0c')
            pages = [p for p in pages if p.strip()]
            if pages:
                return '\n\n--- Strona ---\n\n'.join(_cleanup_text(p) for p in pages)
    except Exception as e:
        log.warning('[med_assistant] pdftotext failed: %s', e)

    try:
        import PyPDF2
        pages = []
        with open(filepath, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                text = page.extract_text() or ''
                pages.append(_cleanup_text(text))
        has_text = any(p.strip() for p in pages)
        if has_text:
            return '\n\n--- Strona ---\n\n'.join(p for p in pages if p.strip())
    except Exception as e:
        log.warning('[med_assistant] PyPDF2 failed: %s', e)

    return _ocr_pdf(filepath)


def _ocr_pdf(filepath):
    """Extract text from scanned PDF using Tesseract OCR."""
    try:
        import fitz
        import pytesseract
        from PIL import Image
        import io
    except ImportError as e:
        log.warning('[med_assistant] OCR deps missing: %s', e)
        return ''

    pages = []
    try:
        doc = fitz.open(filepath)
        for page_num, page in enumerate(doc):
            mat = fitz.Matrix(300 / 72, 300 / 72)
            pix = page.get_pixmap(matrix=mat)
            img_data = pix.tobytes('png')
            img = Image.open(io.BytesIO(img_data))
            text = pytesseract.image_to_string(img, lang='pol+eng', config='--psm 6')
            text = _normalize_ocr_text(text)
            pages.append(_cleanup_text(text))
        doc.close()
    except Exception as e:
        log.error('[med_assistant] OCR failed: %s', e)
        return ''

    return '\n\n--- Strona ---\n\n'.join(p for p in pages if p.strip())


def _extract_text_docx(filepath):
    """Extract text from DOCX."""
    try:
        import docx
        doc = docx.Document(filepath)
        return '\n'.join(para.text for para in doc.paragraphs if para.text.strip())
    except Exception as e:
        log.error('[med_assistant] DOCX extraction failed: %s', e)
        return ''


def _extract_text(filepath):
    """Extract text from any supported file."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == '.pdf':
        return _extract_text_pdf(filepath)
    elif ext in ('.docx', '.doc'):
        return _extract_text_docx(filepath)
    elif ext == '.txt':
        with open(filepath, 'r', errors='replace') as f:
            return f.read()
    return ''


# -- LLM calling -----------------------------------------------------------

_LLM_SCRIPT = r'''
import sys, json, os
sys.path.insert(0, '/opt/ethos/backend')
sys.path.insert(0, '/opt/ethos/backend/blueprints')

sys_path = sys.argv[1]
usr_path = sys.argv[2]
result_path = sys.argv[3]
max_tokens = int(sys.argv[4])

with open(sys_path) as f:
    system_prompt = f.read()
with open(usr_path) as f:
    user_prompt = f.read()

from model_library import get_library
lib = get_library()

bielik_preference = ['bielik-11b-q8', 'bielik-11b-q4', 'bielik-7b-q8', 'bielik-7b-q4']
best_bielik = None
downloaded = lib._config.get('downloaded', {})
for bid in bielik_preference:
    dl = downloaded.get(bid)
    if dl and os.path.isfile(dl.get('path', '')):
        best_bielik = bid
        break

model_id = best_bielik or lib._config.get('active_model_id')
if not model_id:
    with open(result_path, 'w') as rf:
        rf.write('[BLAD] Brak aktywnego modelu LLM')
    sys.exit(0)

dl = downloaded.get(model_id)
if not dl or not os.path.isfile(dl.get('path', '')):
    with open(result_path, 'w') as rf:
        rf.write('[BLAD] Model nie jest pobrany: ' + str(model_id))
    sys.exit(0)

model_path = dl['path']

# Medical docs need larger context — estimate from prompt sizes
total_chars = len(system_prompt) + len(user_prompt)
est_tokens = total_chars // 3 + max_tokens + 256
n_ctx = max(4096, min(est_tokens, 8192))

try:
    from llama_cpp import Llama
    import multiprocessing
    n_threads = max(1, multiprocessing.cpu_count() - 2)
    llm = Llama(model_path=model_path, n_ctx=n_ctx, n_threads=n_threads, n_batch=512, verbose=False)
except Exception as e:
    with open(result_path, 'w') as rf:
        rf.write('[BLAD] Nie udalo sie zaladowac modelu: ' + str(e))
    sys.exit(0)

resp = llm.create_chat_completion(
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ],
    max_tokens=max_tokens,
    temperature=0.2,
)
content = resp["choices"][0]["message"]["content"].strip()
with open(result_path, 'w') as rf:
    rf.write(content)
'''


def _call_llm(system_prompt, user_prompt, max_tokens=2048):
    """Send prompt to Bielik LLM in a subprocess, return text response."""
    import subprocess as _sp
    import tempfile

    _ensure_jobs_dir()

    with tempfile.NamedTemporaryFile(mode='w', suffix='.sys.txt', delete=False,
                                     dir=_JOBS_DIR) as sf:
        sf.write(system_prompt)
        sys_path = sf.name
    with tempfile.NamedTemporaryFile(mode='w', suffix='.usr.txt', delete=False,
                                     dir=_JOBS_DIR) as uf:
        uf.write(user_prompt)
        usr_path = uf.name

    result_path = sys_path + '.result.txt'

    try:
        log.info('[med_assistant] Starting LLM subprocess (sys=%d, usr=%d chars)',
                 len(system_prompt), len(user_prompt))
        proc = _sp.run(
            [sys.executable, '-c', _LLM_SCRIPT, sys_path, usr_path, result_path, str(max_tokens)],
            capture_output=True, text=True,
            timeout=900,
            cwd='/opt/ethos/backend',
        )
        log.info('[med_assistant] LLM subprocess finished (rc=%s)', proc.returncode)
        if proc.stderr:
            log.debug('[med_assistant] LLM stderr: %s', proc.stderr[:300])
        if proc.returncode != 0:
            log.warning('[med_assistant] LLM failed (rc=%s): %s',
                        proc.returncode, proc.stderr[:500])
            return '[BLAD] Model LLM zwrocil blad.'
        if not os.path.exists(result_path):
            log.warning('[med_assistant] LLM result file not created')
            return '[BLAD] Brak odpowiedzi z modelu LLM.'
        with open(result_path) as rf:
            return rf.read()
    except _sp.TimeoutExpired:
        log.warning('[med_assistant] LLM subprocess timed out')
        return '[BLAD] Przekroczono czas oczekiwania na model LLM.'
    except Exception as e:
        log.warning('[med_assistant] LLM error: %s', e)
        return '[BLAD] Wyjatek LLM: ' + str(e)
    finally:
        for p in (sys_path, usr_path, result_path):
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


# -- Analysis modes ---------------------------------------------------------

_ANALYSIS_MODES = {
    'spellcheck': {
        'name': 'Literowki',
        'icon': 'fa-spell-check',
        'description': 'Sprawdz literowki i bledy w dokumentacji medycznej',
    },
    'timeline': {
        'name': 'Chronologia badan',
        'icon': 'fa-clock',
        'description': 'Uloz badania chronologicznie od najstarszych',
    },
    'summary': {
        'name': 'Skrot Holter/ECHO',
        'icon': 'fa-heartbeat',
        'description': 'Skroc opisy badan zachowujac kluczowe wartosci',
    },
    'interactions': {
        'name': 'Interakcje lekowe',
        'icon': 'fa-pills',
        'description': 'Sprawdz potencjalne interakcje miedzy lekami',
    },
    'referral': {
        'name': 'Skierowanie',
        'icon': 'fa-file-medical',
        'description': 'Napisz tresc skierowania do specjalisty',
    },
    'clinical': {
        'name': 'Wskazania kliniczne',
        'icon': 'fa-stethoscope',
        'description': 'Ocena wg wytycznych ESC -- wskazania, badania kontrolne',
    },
}


def _get_system_prompt(mode):
    """Return system prompt for given analysis mode."""
    base = (
        "# ROLA\n"
        "Jestes specjalistycznym modulem AI o nazwie Medical Assistant, "
        "zintegrowanym z systemem EthOS. Wspierasz lekarza kardiologa "
        "w analizie dokumentacji medycznej i automatyzacji pracy biurowej.\n\n"
        "# STYL\n"
        "- Odpowiadaj profesjonalnym jezykiem medycznym, zwieznie i konkretnie.\n"
        "- Uzywaj formatowania Markdown: **pogrubienia** dla kluczowych parametrow, tabele dla wynikow.\n"
        "- Nie stawiaj ostatecznych diagnoz — uzywaj fraz: 'Sugerowane dzialanie:', "
        "'Wyniki moga wskazywac na...', 'Warto rozwazyc...'.\n"
        "- Jesli dane sa niekompletne, wyraznie to zaznacz i napisz jakich danych brakuje.\n"
        "- Na koncu ZAWSZE dodaj:\n"
        "'---\\nWygenerowano automatycznie przez Medical Assistant (EthOS). "
        "Wymaga weryfikacji i podpisu lekarza.'\n\n"
        "# KONTEKST\n"
        "Nadrzednym zrodlem wiedzy medycznej sa wytyczne European Society of Cardiology (ESC).\n\n"
    )

    prompts = {
        'spellcheck': base + (
            "# ZADANIE: PORZADKOWANIE I KOREKTA\n"
            "Znajdz literowki i bledy w dokumentacji medycznej.\n\n"
            "Zasady:\n"
            "- Szukaj literowek, bledow ortograficznych i gramatycznych\n"
            "- NIE poprawiaj terminologii medycznej i lacinskiej (pozostaw nienaruszona)\n"
            "- NIE poprawiaj skrotow medycznych (EKG, RTG, ECHO, MR, CT itp.)\n"
            "- NIE poprawiaj nazw wlasnych lekow i preparatow\n"
            "- Analizuj daty w tresci plikow i zasugeruj czy kolejnosc dokumentow jest chronologiczna\n"
            "- Przy kazdym bledzie podaj: oryginal -> poprawna forma\n"
            "- Jesli brak bledow, napisz wyraznie: 'Nie znaleziono literowek.'\n\n"
            "Format odpowiedzi:\n"
            "## Literowki i bledy\n"
            "| Oryginal | Poprawka | Kontekst |\n"
            "|----------|----------|----------|\n"
            "| ... | ... | ... |\n\n"
            "## Uwagi dotyczace chronologii\n"
            "...\n"
        ),
        'timeline': base + (
            "# ZADANIE: CHRONOLOGIA BADAN\n"
            "Wyciagnij WSZYSTKIE badania, wizyty i zdarzenia z dokumentacji "
            "i uloz je chronologicznie od najstarszych do najswiezszych.\n\n"
            "Zasady:\n"
            "- Wyciagnij date, typ badania/wizyty, kluczowy wynik lub wniosek\n"
            "- Sortuj od **najstarszych** do **najnowszych**\n"
            "- Jesli data nie jest podana wprost, zaznacz **[brak daty]**\n"
            "- Uwzglednij: badania laboratoryjne, obrazowe, konsultacje, zabiegi, hospitalizacje\n"
            "- Wyrozniaj kluczowe momenty (np. rozpoznanie, zmiana leczenia, zabieg)\n\n"
            "Format odpowiedzi:\n"
            "## Chronologia badan i wizyt\n"
            "| Data | Badanie/wizyta | Kluczowy wynik |\n"
            "|------|---------------|----------------|\n"
            "| **DD.MM.RRRR** | Typ badania | Wynik/wniosek |\n"
        ),
        'summary': base + (
            "# ZADANIE: ANALIZA BADAN (ECHO / HOLTER)\n"
            "Streszczaj opisy badan stosujac zasade **istotnosci klinicznej**.\n\n"
            "## Z ECHO serca wyciagaj:\n"
            "- **Frakcje wyrzutowa (EF)** — z metoda pomiaru (Simpson/Teicholz)\n"
            "- **Wymiary jam** serca (LVEDD, LVESD, LA, RV, RA)\n"
            "- **Opis zastawek** — niedomykalnosci, stenozy, gradienty, pole\n"
            "- **Cisnienia** — PASP, gradient E/A, E/e'\n"
            "- **TAPSE**, **S'** (funkcja prawej komory)\n"
            "- Zaburzenia kurczliwosci odcinkowej\n\n"
            "## Z Holtera EKG wyciagaj:\n"
            "- **Rytm wiodacy** (zatokowy, migotanie przedsionkow itp.)\n"
            "- **Pauzy** — czas trwania, okolicznosci\n"
            "- **Max/min HR** z okolicznosciami\n"
            "- Istotne **zaburzenia komorowe** (VT, VE, pary, salwy — ilosc)\n"
            "- Istotne **zaburzenia nadkomorowe** (SVT, AF paroxysms — czas trwania)\n\n"
            "Zasady:\n"
            "- ZACHOWAJ wszystkie wartosci liczbowe\n"
            "- USUN powtorzenia, standardowe opisy normy, fragmenty szablonowe\n"
            "- Skroc do **30-50%** oryginalnej dlugosci\n"
            "- Zachowaj wniosek/podsumowanie w calosci\n\n"
            "Format odpowiedzi:\n"
            "## [Typ badania] — [data]\n"
            "### Kluczowe parametry\n"
            "| Parametr | Wartosc | Norma |\n"
            "|----------|---------|-------|\n"
            "### Patologie\n"
            "- ...\n"
            "### Wniosek\n"
            "...\n"
        ),
        'interactions': base + (
            "# ZADANIE: BEZPIECZENSTWO LEKOWE\n"
            "Skanuj zalecenia pod katem interakcji lekowych (drug-drug interactions).\n\n"
            "Zasady:\n"
            "- Wyciagnij WSZYSTKIE leki z dokumentacji (nazwy handlowe i INN)\n"
            "- Sprawdz znane interakcje miedzy wymienionymi lekami\n"
            "- Ocen kliniczna istotnosc: **WYSOKA** / **SREDNIA** / **NISKA**\n"
            "- Podaj mechanizm interakcji\n"
            "- Jesli wykryjesz ryzyko WYSOKIE, oznacz je wyraznym alertem: ⚠️ ALERT\n"
            "- Jesli brak istotnych interakcji, napisz to wyraznie\n"
            "- Zwroc uwage na: podwojne antyagregacyjne, antykoagulanty+NLPZ, "
            "QT-prolongujace, hipokalemizujace+digoksyna, ACEI+potas\n\n"
            "Format odpowiedzi:\n"
            "## Lista lekow\n"
            "| Nr | Lek | Dawka | Wskazanie |\n"
            "|----|-----|-------|-----------|\n\n"
            "## Interakcje\n"
            "### ⚠️ ALERT — [Lek A] + [Lek B] (istotnosc: WYSOKA)\n"
            "- **Mechanizm:** ...\n"
            "- **Ryzyko:** ...\n"
            "- **Sugerowane dzialanie:** ...\n"
        ),
        'referral': base + (
            "# ZADANIE: GENEROWANIE SKIEROWANIA\n"
            "Tworz tresc skierowania na podstawie analizy dokumentacji. "
            "Skup sie na faktach istotnych dla lekarza konsultujacego.\n\n"
            "Zasady:\n"
            "- Napisz profesjonalne skierowanie w standardowym formacie polskim\n"
            "- Uwzglednij: rozpoznanie glowne (ICD-10), istotne wyniki badan, "
            "dotychczasowe leczenie\n"
            "- Podaj cel skierowania (konsultacja, kwalifikacja do zabiegu, itp.)\n"
            "- Wymien istotne badania zalaczane do skierowania\n"
            "- Ton formalny, medyczny, bez zbednych przymiotnikow\n"
            "- Nie wymyslaj danych — uzywaj tylko tego co jest w dokumentacji\n\n"
            "Format odpowiedzi:\n"
            "## SKIEROWANIE DO: [specjalista]\n\n"
            "**Rozpoznanie:** [ICD-10 + opis]\n\n"
            "**Cel skierowania:** ...\n\n"
            "Szanowna/y Pani/Pan Doktor,\n\n"
            "[tresc skierowania z kluczowymi wynikami badan i leczeniem...]\n\n"
            "**Zalaczniki:** [lista badan]\n"
        ),
        'clinical': base + (
            "# ZADANIE: WNIOSKOWANIE KLINICZNE (wg ESC)\n"
            "Porownuj wyniki pacjenta z wytycznymi European Society of Cardiology.\n\n"
            "Zasady:\n"
            "- Ocen wskazania do operacji/zabiegu na podstawie kryteriow ESC:\n"
            "  * **Wady zastawkowe:** kryteria ciezkosci (EF, gradient, pole, LVESD, EROA)\n"
            "  * **CRT/ICD:** EF<=35%, QRS>=150ms, LBBB, NYHA II-IV\n"
            "  * **Rewaskularyzacja:** ocena niedokrwienia, anatomia tetnic wiencowych\n"
            "  * **TAVI/SAVR:** kryteria STS/EuroSCORE\n"
            "- Wskaz jakie badania warto powtorzyc i kiedy, by zachowac ciaglosc leczenia\n"
            "  (np. kontrola potasu przy ACEI, kontrola ECHO co X miesiecy)\n"
            "- Ocen spojnosc zalecen — czy leczenie jest zgodne z wytycznymi\n"
            "- Wymien wskazania ESC klasy **I** (silne) i **IIa**\n"
            "- Uzywaj fraz: 'Wyniki moga wskazywac na...', 'Sugerowane dzialanie:'\n\n"
            "Format odpowiedzi:\n"
            "## Ocena kliniczna (wg wytycznych ESC)\n\n"
            "### Rozpoznania\n"
            "- ...\n\n"
            "### Wskazania do zabiegu/interwencji\n"
            "| Wskazanie | Klasa ESC | Poziom dowodow | Uzasadnienie |\n"
            "|-----------|-----------|----------------|-------------|\n\n"
            "### Badania do powtorzenia\n"
            "| Badanie | Termin | Powod |\n"
            "|---------|--------|-------|\n\n"
            "### Ciaglosc leczenia\n"
            "- ...\n"
        ),
    }
    return prompts.get(mode, base)


# -- Analysis runner --------------------------------------------------------

def _run_analysis(job_id, mode, files_data, patient_name, username, referral_target=None):
    """Background task: extract text from files -> send to LLM -> save result."""
    _ensure_jobs_dir()
    job = _job_dir(job_id)
    os.makedirs(job, exist_ok=True)
    meta_path = os.path.join(job, 'meta.json')

    meta = {
        'job_id': job_id,
        'mode': mode,
        'mode_name': _ANALYSIS_MODES.get(mode, {}).get('name', mode),
        'patient': patient_name,
        'files': [f['name'] for f in files_data],
        'status': 'processing',
        'created_at': time.time(),
        'username': username,
    }

    try:
        with open(meta_path, 'w') as mf:
            json.dump(meta, mf, ensure_ascii=False)

        _emit_progress(job_id, 'extracting', 5, 'Wyodrebnianie tekstu z dokumentow...')
        all_text_parts = []
        for i, fdata in enumerate(files_data):
            fpath = fdata['path']
            fname = fdata['name']
            pct = 10 + int(30 * (i + 1) / len(files_data))
            _emit_progress(job_id, 'extracting', pct,
                           'Odczytywanie: ' + fname + '...')

            _ensure_deps()
            text = _extract_text(fpath)
            if text.strip():
                all_text_parts.append('=== ' + fname + ' ===\n' + text)
            else:
                all_text_parts.append('=== ' + fname + ' ===\n[Nie udalo sie wyodrebnic tekstu]')

        combined_text = '\n\n'.join(all_text_parts)

        max_chars = 8000
        if len(combined_text) > max_chars:
            combined_text = combined_text[:max_chars] + '\n\n[...tekst skrocony ze wzgledu na limit kontekstu]'

        _emit_progress(job_id, 'analyzing', 50, 'Analiza przez model Bielik...')

        system_prompt = _get_system_prompt(mode)
        user_prompt = 'Dokumentacja pacjenta: ' + patient_name + '\n\n' + combined_text

        if mode == 'referral' and referral_target:
            user_prompt += '\n\nSkierowanie do: ' + referral_target

        result_text = _call_llm(system_prompt, user_prompt, max_tokens=2048)

        _emit_progress(job_id, 'saving', 90, 'Zapisywanie wyniku...')

        result_path = os.path.join(job, 'result.txt')
        with open(result_path, 'w') as rf:
            rf.write(result_text)

        meta['status'] = 'done'
        meta['result_preview'] = result_text[:500]
        meta['result_length'] = len(result_text)
        meta['completed_at'] = time.time()

        with open(meta_path, 'w') as mf:
            json.dump(meta, mf, ensure_ascii=False)

        _emit_progress(job_id, 'done', 100, 'Analiza zakonczona')

    except Exception as e:
        log.error('[med_assistant] Analysis failed: %s', e, exc_info=True)
        meta['status'] = 'error'
        meta['error'] = str(e)
        try:
            with open(meta_path, 'w') as mf:
                json.dump(meta, mf, ensure_ascii=False)
        except Exception:
            pass
        _emit_progress(job_id, 'error', 0, 'Blad: ' + str(e))
    finally:
        with _jobs_lock:
            _active_jobs.pop(job_id, None)


# -- Job queue (sequential processing) -------------------------------------

def _queue_worker():
    """Process queued analysis jobs one at a time."""
    global _worker_running
    while True:
        with _queue_lock:
            if not _job_queue:
                _worker_running = False
                return
            job_args = _job_queue.pop(0)

        job_id = job_args[0]
        with _jobs_lock:
            if job_id in _active_jobs:
                _active_jobs[job_id]['stage'] = 'processing'

        _run_analysis(*job_args)


def _enqueue_job(job_id, mode, files_data, patient, username, referral_target):
    """Add analysis job to sequential queue; start worker if needed."""
    global _worker_running
    with _queue_lock:
        _job_queue.append((job_id, mode, files_data, patient, username, referral_target))
        if not _worker_running:
            _worker_running = True
            t = threading.Thread(target=_queue_worker, daemon=True)
            t.start()


def _cleanup_stuck_jobs():
    """Mark any 'processing' jobs as error on startup (server crash recovery)."""
    if not os.path.isdir(_JOBS_DIR):
        return
    for entry in os.listdir(_JOBS_DIR):
        meta_path = os.path.join(_JOBS_DIR, entry, 'meta.json')
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if meta.get('status') == 'processing':
                meta['status'] = 'error'
                meta['error'] = 'Serwer zrestartowany w trakcie analizy.'
                with open(meta_path, 'w') as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                log.info('[med_assistant] Marked stuck job %s as error', entry)
        except Exception:
            continue


_cleanup_stuck_jobs()


# -- Routes: Status --------------------------------------------------------

@med_assistant_bp.route('/status')
def med_status():
    """Return app status and AI dependency info."""
    ai_available = False
    active_model = None
    try:
        from model_library import get_library
        lib = get_library()
        downloaded = lib._config.get('downloaded', {})
        for mid in ['bielik-11b-q8', 'bielik-11b-q4', 'bielik-7b-q8', 'bielik-7b-q4']:
            dl = downloaded.get(mid)
            if dl and os.path.isfile(dl.get('path', '')):
                active_model = mid
                break
        ai_available = bool(downloaded)
    except Exception:
        pass

    return jsonify(
        ok=True,
        ai_chat_available=ai_available,
        active_model=active_model,
        modes=_ANALYSIS_MODES,
    )


# -- Routes: Patients ------------------------------------------------------

@med_assistant_bp.route('/patients')
def list_patients():
    """List patient folders."""
    username = _get_username()
    base = _patients_base(username)

    patients = []
    try:
        for entry in sorted(os.listdir(base)):
            full = os.path.join(base, entry)
            if os.path.isdir(full) and not entry.startswith('.'):
                file_count = sum(1 for f in os.listdir(full)
                                 if os.path.isfile(os.path.join(full, f))
                                 and not f.startswith('.'))
                stat = os.stat(full)
                patients.append({
                    'name': entry,
                    'file_count': file_count,
                    'modified': stat.st_mtime,
                })
    except Exception as e:
        log.error('[med_assistant] list patients: %s', e)

    patients.sort(key=lambda p: p['modified'], reverse=True)
    return jsonify(ok=True, items=patients)


@med_assistant_bp.route('/patients', methods=['POST'])
def create_patient():
    """Create a new patient folder."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify(error='Podaj nazwe folderu pacjenta'), 400
    name = re.sub(r'[/\\<>:"|?*]', '_', name)
    if not name or name in ('.', '..'):
        return jsonify(error='Nieprawidlowa nazwa'), 400

    username = _get_username()
    base = _patients_base(username)
    folder = os.path.join(base, name)

    if os.path.exists(folder):
        return jsonify(error='Folder juz istnieje'), 400

    os.makedirs(folder, mode=0o750)
    return jsonify(ok=True, name=name)


@med_assistant_bp.route('/patients/<path:name>', methods=['DELETE'])
def delete_patient(name):
    """Delete a patient folder and its contents."""
    username = _get_username()
    base = _patients_base(username)
    folder = safe_path(name, base=base)

    if not os.path.isdir(folder):
        return jsonify(error='Folder nie istnieje'), 404

    shutil.rmtree(folder, ignore_errors=True)
    return jsonify(ok=True)


# -- Routes: Patient files --------------------------------------------------

@med_assistant_bp.route('/patients/<path:name>/files')
def list_patient_files(name):
    """List files in a patient folder."""
    username = _get_username()
    base = _patients_base(username)
    folder = safe_path(name, base=base)

    if not os.path.isdir(folder):
        return jsonify(error='Folder nie istnieje'), 404

    files = []
    try:
        for entry in sorted(os.listdir(folder)):
            full = os.path.join(folder, entry)
            if os.path.isfile(full) and not entry.startswith('.') and not entry.startswith('_analiza_'):
                stat = os.stat(full)
                ext = os.path.splitext(entry)[1].lower()
                files.append({
                    'name': entry,
                    'size': stat.st_size,
                    'modified': stat.st_mtime,
                    'type': ext,
                })
    except Exception as e:
        log.error('[med_assistant] list files: %s', e)

    files.sort(key=lambda f: f['modified'], reverse=True)
    return jsonify(ok=True, items=files, patient=name, path=folder)


@med_assistant_bp.route('/patients/<path:name>/upload', methods=['POST'])
def upload_patient_file(name):
    """Upload file(s) to a patient folder."""
    username = _get_username()
    base = _patients_base(username)
    folder = safe_path(name, base=base)

    if not os.path.isdir(folder):
        return jsonify(error='Folder nie istnieje'), 404

    uploaded = request.files.getlist('files')
    if not uploaded:
        return jsonify(error='Brak plikow'), 400

    saved = []
    for f in uploaded:
        if not f.filename:
            continue
        fname = re.sub(r'[/\\<>:"|?*]', '_', f.filename)
        dest = os.path.join(folder, fname)
        f.save(dest)
        saved.append(fname)

    return jsonify(ok=True, files=saved)


# -- Routes: Analysis ------------------------------------------------------

@med_assistant_bp.route('/analyze', methods=['POST'])
def analyze():
    """Start an analysis job."""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode')
    patient = data.get('patient', '').strip()
    filenames = data.get('files', [])
    referral_target = data.get('referral_target', '')

    if mode not in _ANALYSIS_MODES:
        return jsonify(error='Nieznany tryb analizy: ' + str(mode)), 400
    if not patient:
        return jsonify(error='Nie wybrano pacjenta'), 400
    if not filenames:
        return jsonify(error='Nie wybrano plikow'), 400

    username = _get_username()
    base = _patients_base(username)
    folder = safe_path(patient, base=base)

    if not os.path.isdir(folder):
        return jsonify(error='Folder pacjenta nie istnieje'), 404

    files_data = []
    for fname in filenames:
        fpath = os.path.join(folder, fname)
        if os.path.isfile(fpath):
            files_data.append({'name': fname, 'path': fpath})

    if not files_data:
        return jsonify(error='Zaden z wybranych plikow nie istnieje'), 400

    job_id = str(uuid.uuid4())[:12]
    with _jobs_lock:
        _active_jobs[job_id] = {
            'mode': mode, 'patient': patient,
            'stage': 'queued', 'progress': 0,
        }

    _enqueue_job(job_id, mode, files_data, patient, username, referral_target)

    return jsonify(ok=True, job_id=job_id)


# -- Routes: Jobs -----------------------------------------------------------

@med_assistant_bp.route('/jobs')
def list_jobs():
    """List analysis jobs, newest first."""
    _ensure_jobs_dir()
    username = _get_username()
    items = []

    try:
        for entry in os.listdir(_JOBS_DIR):
            meta_path = os.path.join(_JOBS_DIR, entry, 'meta.json')
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path) as mf:
                    meta = json.load(mf)
                if meta.get('username') != username:
                    continue
                items.append(meta)
            except Exception:
                continue
    except Exception:
        pass

    with _jobs_lock:
        for jid, jdata in _active_jobs.items():
            if not any(it.get('job_id') == jid for it in items):
                items.append({
                    'job_id': jid,
                    'mode': jdata.get('mode'),
                    'mode_name': _ANALYSIS_MODES.get(jdata.get('mode', ''), {}).get('name', ''),
                    'patient': jdata.get('patient'),
                    'status': 'processing',
                    'progress': jdata.get('progress', 0),
                })

    items.sort(key=lambda x: x.get('created_at', 0), reverse=True)
    return jsonify(ok=True, items=items)


@med_assistant_bp.route('/job/<job_id>')
def get_job(job_id):
    """Get full job details including result text."""
    _ensure_jobs_dir()
    job = _job_dir(job_id)
    meta_path = os.path.join(job, 'meta.json')
    result_path = os.path.join(job, 'result.txt')

    if not os.path.isfile(meta_path):
        return jsonify(error='Zadanie nie istnieje'), 404

    with open(meta_path) as mf:
        meta = json.load(mf)

    if os.path.isfile(result_path):
        with open(result_path) as rf:
            meta['result'] = rf.read()

    return jsonify(ok=True, **meta)


@med_assistant_bp.route('/job/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    """Delete a job and its files."""
    _ensure_jobs_dir()
    job = _job_dir(job_id)
    if os.path.isdir(job):
        shutil.rmtree(job, ignore_errors=True)
    return jsonify(ok=True)


@med_assistant_bp.route('/job/<job_id>/export')
def export_job(job_id):
    """Export analysis result to patient folder as a text file."""
    _ensure_jobs_dir()
    job = _job_dir(job_id)
    meta_path = os.path.join(job, 'meta.json')
    result_path = os.path.join(job, 'result.txt')

    if not os.path.isfile(meta_path) or not os.path.isfile(result_path):
        return jsonify(error='Brak wyniku do eksportu'), 404

    with open(meta_path) as mf:
        meta = json.load(mf)

    patient = meta.get('patient', '')
    mode_name = meta.get('mode_name', meta.get('mode', 'analiza'))

    username = _get_username()
    base = _patients_base(username)
    folder = safe_path(patient, base=base)

    if not os.path.isdir(folder):
        return jsonify(error='Folder pacjenta nie istnieje'), 404

    with open(result_path) as rf:
        result_text = rf.read()

    ts = time.strftime('%Y%m%d_%H%M%S')
    safe_mode = re.sub(r'[/\\<>:"|?*\s]', '_', mode_name)
    export_name = '_analiza_' + safe_mode + '_' + ts + '.txt'
    export_path = os.path.join(folder, export_name)

    with open(export_path, 'w') as ef:
        ef.write('Medical Assistant -- ' + mode_name + '\n')
        ef.write('Pacjent: ' + patient + '\n')
        ef.write('Data: ' + time.strftime('%Y-%m-%d %H:%M:%S') + '\n')
        ef.write('Pliki: ' + ', '.join(meta.get('files', [])) + '\n')
        ef.write('=' * 60 + '\n\n')
        ef.write(result_text)

    return jsonify(ok=True, filename=export_name)


# -- Package routes ---------------------------------------------------------

def _on_uninstall(wipe=False):
    if wipe:
        if os.path.isdir(_JOBS_DIR):
            shutil.rmtree(_JOBS_DIR, ignore_errors=True)


register_pkg_routes(
    med_assistant_bp,
    install_message='Medical Assistant installed.',
    on_uninstall=_on_uninstall,
    wipe_dirs=[_JOBS_DIR],
)
