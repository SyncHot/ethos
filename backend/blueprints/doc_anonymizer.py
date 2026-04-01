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
import unicodedata

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
    """Extract text from a PDF using pdftotext (poppler), fallback to PyPDF2."""
    import subprocess
    try:
        result = subprocess.run(
            ['pdftotext', '-layout', filepath, '-'],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            raw = result.stdout
            # Split by form-feed (page separator) if present
            pages = raw.split('\x0c')
            pages = [p for p in pages if p.strip()]
            if pages:
                return [_cleanup_pdf_text(p) for p in pages]
    except Exception as e:
        log.warning('[doc_anonymizer] pdftotext failed, using PyPDF2: %s', e)

    import PyPDF2
    pages = []
    with open(filepath, 'rb') as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            text = page.extract_text() or ''
            pages.append(_cleanup_pdf_text(text))
    return pages


def _cleanup_pdf_text(text):
    """Reassemble fragmented text from PDF extraction.

    Tables in PDFs often produce single-char lines, scattered digits, and
    broken words.  This tries to rejoin them for better PII detection.
    """
    lines = text.split('\n')
    cleaned = []
    digit_buf = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # Flush digit buffer on blank line
            if digit_buf:
                cleaned.append(''.join(digit_buf))
                digit_buf = []
            continue

        # Collect scattered single digits (likely PESEL fragments)
        if re.match(r'^\d{1,2}$', stripped):
            digit_buf.append(stripped)
            continue

        if digit_buf:
            cleaned.append(''.join(digit_buf))
            digit_buf = []

        # Collapse excessive whitespace within a line
        stripped = re.sub(r'\s{3,}', '  ', stripped)
        cleaned.append(stripped)

    if digit_buf:
        cleaned.append(''.join(digit_buf))

    return '\n'.join(cleaned)


def _extract_text_docx(filepath):
    """Extract text from a DOCX file paragraph by paragraph."""
    import docx
    doc = docx.Document(filepath)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return paragraphs


# -- Regex-based PII detection (fast, reliable for structured data) ---------

_MONTHS_PL = ('stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|'
              'wrzesnia|września|pazdziernika|października|listopada|grudnia')

_REGEX_PATTERNS = [
    # PESEL: exactly 11 digits, not part of a longer number
    (re.compile(r'(?<!\d)\d{11}(?!\d)'), 'PESEL'),
    # Polish bank account (IBAN): 2+26 digits with spaces
    (re.compile(r'(?<!\d)\d{2}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}(?!\d)'), 'NR_KONTA'),
    # Document ID: 3 uppercase letters + 6 digits (e.g. CBA 123456)
    (re.compile(r'\b[A-Z]{3}\s?\d{6}\b'), 'NR_DOKUMENTU'),
    # Phone: Polish formats with +48
    (re.compile(r'(?:\+48|0048)[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{3}'), 'TELEFON'),
    # Phone: 9 digits with separators
    (re.compile(r'(?<!\d)\d{3}[\s-]\d{3}[\s-]\d{3}(?!\d)'), 'TELEFON'),
    # Phone: landline (2-digit area + 7 digits, e.g. "22 620 00 00")
    (re.compile(r'(?<!\d)\d{2}\s\d{3}\s\d{2}\s\d{2}(?!\d)'), 'TELEFON'),
    # Phone: landline with dash separators (e.g. "12-654-33-21", "71-344-89-01")
    (re.compile(r'(?<!\d)\d{2}-\d{3}-\d{2}-\d{2}(?!\d)'), 'TELEFON'),
    # Email
    (re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'), 'EMAIL'),
    # Polish postal code + city (e.g. "00-001 Warszawa")
    (re.compile(r'\d{2}-\d{3}\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+'), 'ADRES'),
    # Street address (ul./al./os./pl. + name + optional number) — single line only
    (re.compile(r'(?:ul\.|al\.|os\.|pl\.|Al\.)\s+[A-ZĄ-Ż][a-ząćęłńóśźż]+'
                r'(?:\s[A-ZĄ-Ż]?[a-ząćęłńóśźż]+)*'
                r'(?:\s+\d+[a-zA-Z]?(?:/\d+[a-zA-Z]?)?)'), 'ADRES'),
    # NIP: 10 digits with optional dashes (e.g. "525-12-34-567")
    (re.compile(r'\b\d{3}-\d{2}-\d{2}-\d{3}\b'), 'NR_DOKUMENTU'),
    # PWZ number (e.g. "nr PWZ 4478123" or "PWZ: 1234567")
    (re.compile(r'(?:nr\s+)?PWZ[\s:]*\d{7}'), 'NR_DOKUMENTU'),
    # Dates: DD.MM.YYYY, DD-MM-YYYY, DD/MM/YYYY
    (re.compile(r'(?<!\d)\d{1,2}[./-]\d{1,2}[./-]\d{4}(?!\d)'), 'DATA'),
    # Written dates: "31 marca 2026" / "1 stycznia 2025 r."
    (re.compile(r'\d{1,2}\s+(?:' + _MONTHS_PL + r')\s+\d{4}(?:\s+r\.)?', re.I), 'DATA'),
]

# -- Name detection (regex-based, high recall for Polish documents) ---------

# Common Polish first names used as anchors for detecting name patterns
_PL_FIRST_NAMES = frozenset({
    'Adam', 'Adrian', 'Agata', 'Agnieszka', 'Aleksander', 'Aleksandra',
    'Andrzej', 'Anna', 'Antoni', 'Barbara', 'Bartosz', 'Beata', 'Bogdan',
    'Bozena', 'Celina', 'Cezary', 'Dariusz', 'Danuta', 'Dawid', 'Dorota',
    'Edward', 'Elzbieta', 'Ewa', 'Filip', 'Franciszek', 'Grazyna',
    'Grzegorz', 'Halina', 'Henryk', 'Henryka', 'Hubert', 'Irena',
    'Iwona', 'Jacek', 'Jadwiga', 'Jakub', 'Jan', 'Janina', 'Jaroslaw',
    'Jerzy', 'Joanna', 'Jolanta', 'Jozef', 'Julia', 'Justyna',
    'Kamil', 'Karol', 'Katarzyna', 'Kazimierz', 'Konrad', 'Krystyna',
    'Krzysztof', 'Leszek', 'Lukasz', 'Maciej', 'Magdalena', 'Malgorzata',
    'Marcin', 'Marek', 'Maria', 'Mariusz', 'Marta', 'Michal', 'Miroslawa',
    'Monika', 'Natalia', 'Norbert', 'Olga', 'Patryk', 'Pawel', 'Piotr',
    'Przemyslaw', 'Rafal', 'Renata', 'Robert', 'Roman', 'Ryszard',
    'Sebastian', 'Stanislaw', 'Stefan', 'Sylwia', 'Szymon', 'Tadeusz',
    'Teresa', 'Tomasz', 'Wanda', 'Weronika', 'Wieslaw', 'Wiktoria',
    'Witold', 'Wladyslaw', 'Wojciech', 'Zbigniew', 'Zofia', 'Zygmunt',
})

# Title prefixes that signal a person name follows
_TITLE_PREFIXES = (
    r'dr\s+n\.\s*med\.\s*',
    r'dr\s+hab\.\s*n\.\s*med\.\s*',
    r'prof\.\s*dr\s+hab\.\s*n\.\s*med\.\s*',
    r'prof\.\s*dr\s+hab\.\s*',
    r'prof\.\s*',
    r'dr\s+hab\.\s*',
    r'dr\s+',
    r'lek\.\s*med\.\s*',
    r'lek\.\s*',
    r'mgr\s+',
    r'inz\.\s*',
)

# Capitalized Polish surname (including compound): e.g. "Kowalski", "Kowalska-Nowak"
_SURNAME_RE = r'[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]{2,}(?:-[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]{2,})?'

# Polish name stopwords: words that look like names but aren't
_NAME_STOPWORDS = frozenset({
    'Pacjent', 'Pacjentka', 'Pacjenta', 'Lekarz', 'Konsultanci', 'Klinika',
    'Szpital', 'Centrum', 'Instytut', 'Poradnia', 'Oddzial', 'Pani', 'Pan',
    'Placowka', 'Adres', 'Telefon', 'Email', 'Podpis', 'Data', 'Numer',
    'Rozpoznanie', 'Badanie', 'Wyniki', 'Epikryza', 'Wnioski', 'Opinia',
    'Przebieg', 'Zalecenia', 'Kontrola', 'Osoba', 'Siostra', 'Brat',
    'Matka', 'Ojciec', 'Zona', 'Maz',
})


def _detect_names(text):
    """Detect Polish person names using pattern matching."""
    entities = []
    seen = set()

    # 1) Title + name patterns (dr, prof., lek. etc.) — search full text
    for prefix in _TITLE_PREFIXES:
        # title + FirstName [MiddleName|Initial] Surname[-Compound]
        pat = re.compile(
            r'(?:' + prefix + r')'
            r'([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+'            # first name
            r'(?:[ \t]+[A-ZĄĆĘŁŃÓŚŹŻ]\.?[a-ząćęłńóśźż]*)?' # optional middle/initial
            r'[ \t]+' + _SURNAME_RE + r')'                    # surname
        )
        for m in pat.finditer(text):
            name = m.group(1).strip()
            full = m.group(0).strip()
            if name not in seen and name.split()[0] not in _NAME_STOPWORDS:
                seen.add(name)
                seen.add(full)
                entities.append({'text': full, 'category': 'LEKARZ'})

    # 2) Known first name + surname(s) — search line-by-line to avoid
    #    cross-line false positives
    name_pat = re.compile(
        r'\b([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)'
        r'(?:[ \t]+([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+))?'
        r'[ \t]+(' + _SURNAME_RE + r')\b'
    )
    for line in text.split('\n'):
        for m in name_pat.finditer(line):
            first = m.group(1)
            middle = m.group(2) or ''
            surname = m.group(3)
            full_match = m.group(0).strip()

            if first not in _PL_FIRST_NAMES and middle not in _PL_FIRST_NAMES:
                continue
            if first in _NAME_STOPWORDS:
                continue
            if len(surname) < 3:
                continue
            if full_match not in seen:
                seen.add(full_match)
                entities.append({'text': full_match, 'category': 'IMIE_NAZWISKO'})

    # 3) "K. Surname" abbreviation patterns
    abbrev_pat = re.compile(
        r'\b([A-ZĄĆĘŁŃÓŚŹŻ]\.)[ \t]+(' + _SURNAME_RE + r')\b'
    )
    for m in abbrev_pat.finditer(text):
        abbrev = m.group(0).strip()
        surname = m.group(2)
        if len(surname) >= 4 and abbrev not in seen:
            seen.add(abbrev)
            entities.append({'text': abbrev, 'category': 'IMIE_NAZWISKO'})

    return entities


def _detect_facilities(text):
    """Detect Polish medical facility names using pattern matching."""
    entities = []
    seen = set()

    # Use [^\n]* to stay within single lines
    facility_patterns = [
        re.compile(r'Szpital(?:u|em)?[ \t]+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+'
                   r'(?:[ \t]+(?:im\.\s+)?[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ.]+)*'),
        re.compile(r'Klinik[aięy][ \t]+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+'
                   r'(?:[ \t]+[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]+){0,5}'),
        re.compile(r'Centrum[ \t]+(?:Medyczn[a-z]*|Zdrowia)[ \t]+[A-Za-ząćęłńóśźż]+'
                   r'(?:[ \t]+[A-Za-ząćęłńóśźż.]+)*'),
        re.compile(r'Poradni[aęy][ \t]+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+'
                   r'(?:[ \t]+[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]+){0,4}'),
        re.compile(r'Instytut(?:u)?[ \t]+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+'
                   r'(?:[ \t]+[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]+){0,5}'),
        re.compile(r'(?:Osrodek|Ośrodek)[ \t]+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+'
                   r'(?:[ \t]+[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]+){0,3}'),
        re.compile(r'(?:NZOZ|SPZOZ|ZOZ)[ \t]+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+'
                   r'(?:[ \t]+[a-ząćęłńóśźżA-ZĄĆĘŁŃÓŚŹŻ]+){0,3}'),
    ]

    for pat in facility_patterns:
        for m in pat.finditer(text):
            name = m.group(0).strip()
            # Trim trailing prepositions, articles, and title prefixes
            name = re.sub(r'[ \t]+(?:w|we|z|ze|na|przy|do|i|lub|oraz|dr|prof|lek|mgr)\.?[ \t]*$', '', name)
            if name not in seen and len(name) > 8:
                seen.add(name)
                entities.append({'text': name, 'category': 'NAZWA_PLACOWKI'})

    return entities


def _regex_detect(text):
    """Detect PII using regex patterns. Returns list of entity dicts."""
    # Normalize: collapse whitespace/newlines between digits
    normalized = re.sub(r'(\d)[\s\n]+(\d)', r'\1 \2', text)

    entities = []
    seen = set()
    for pattern, category in _REGEX_PATTERNS:
        for m in pattern.finditer(normalized):
            matched = m.group(0).strip()
            if not matched or len(matched) < 3:
                continue
            # Skip matches containing newlines (broken table fragments)
            if '\n' in matched:
                continue
            if matched not in seen:
                seen.add(matched)
                entities.append({'text': matched, 'category': category})

    # Remove entities that are substrings of a longer entity in same category
    final = []
    for e in entities:
        is_substring = False
        for other in entities:
            if other is not e and e['text'] in other['text'] and e['category'] == other['category']:
                is_substring = True
                break
        if not is_substring:
            final.append(e)
    return final


# -- LLM anonymization (for names, doctor names, facility names) ------------


def _call_llm(text_chunk):
    """Send text to the local LLM in a subprocess to avoid blocking gevent.

    Uses a result file instead of stdout because llama.cpp's C runtime
    writes CUDA/GGML init messages to stdout, polluting the JSON output.
    """
    import subprocess as _sp
    import tempfile

    # Write text to a temp file for the subprocess
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                     dir=_JOBS_DIR) as tf:
        tf.write(text_chunk[:3000])
        text_path = tf.name

    result_path = text_path + '.result.json'

    script = r'''
import sys, json, os, re
sys.path.insert(0, '/opt/ethos/backend')
sys.path.insert(0, '/opt/ethos/backend/blueprints')

text_path = sys.argv[1]
result_path = sys.argv[2]

with open(text_path) as f:
    text = f.read()

from model_library import get_library
lib = get_library()
llm, err = lib.load_model()
if err:
    with open(result_path, 'w') as rf:
        json.dump([], rf)
    sys.exit(0)
lib.touch_model()

system_prompt = (
    "Wypisz imiona i nazwiska osob oraz nazwy placowek medycznych z tekstu.\n"
    "Ignoruj daty, adresy, numery, telefony, email.\n"
    "Format odpowiedzi - TYLKO JSON tablica:\n"
    '[{"text":"Katarzyna Nowak","category":"IMIE_NAZWISKO"},'
    '{"text":"dr Jan Kowalski","category":"LEKARZ"},'
    '{"text":"Szpital Miejski","category":"NAZWA_PLACOWKI"}]\n'
    "Kategorie: IMIE_NAZWISKO, LEKARZ, NAZWA_PLACOWKI.\n"
    "Bez komentarzy, bez markdown."
)
user_prompt = "Tekst:\n" + text + "\n\nJSON:"

resp = llm.create_chat_completion(
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ],
    max_tokens=1024,
    temperature=0.1,
)
content = resp["choices"][0]["message"]["content"].strip()
if "```" in content:
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
    if m:
        content = m.group(1).strip()

# Try to salvage truncated JSON arrays by closing them
def _try_parse_json_array(s):
    try:
        arr = json.loads(s)
        if isinstance(arr, dict) and "text" in arr:
            return [arr]
        if isinstance(arr, list):
            return arr
        return []
    except json.JSONDecodeError:
        pass
    # Truncated array: try closing the last complete element
    # Find last complete }, then close the array
    idx = s.rfind('}')
    if idx > 0:
        candidate = s[:idx+1] + ']'
        try:
            arr = json.loads(candidate)
            if isinstance(arr, list):
                return arr
        except json.JSONDecodeError:
            pass
    # Find embedded array
    m2 = re.search(r'\[.*\}', s, re.DOTALL)
    if m2:
        try:
            return json.loads(m2.group(0) + ']')
        except json.JSONDecodeError:
            pass
    return []

entities = _try_parse_json_array(content)
result = [e for e in entities if isinstance(e, dict) and "text" in e and "category" in e]
with open(result_path, 'w') as rf:
    json.dump(result, rf, ensure_ascii=False)
'''

    try:
        log.info('[doc_anonymizer] Starting LLM subprocess (text=%d chars)', len(text_chunk))
        proc = _sp.run(
            [sys.executable, '-c', script, text_path, result_path],
            capture_output=True, text=True,
            timeout=600,  # 10 min max
            cwd='/opt/ethos/backend',
        )
        log.info('[doc_anonymizer] LLM subprocess finished (rc=%s, stdout=%d, stderr=%d)',
                 proc.returncode, len(proc.stdout), len(proc.stderr))
        if proc.stderr:
            log.debug('[doc_anonymizer] LLM stderr: %s', proc.stderr[:300])
        if proc.returncode != 0:
            log.warning('[doc_anonymizer] LLM subprocess failed (rc=%s): %s',
                        proc.returncode, proc.stderr[:500])
            return []
        if not os.path.exists(result_path):
            log.warning('[doc_anonymizer] LLM result file not created. stdout=%s',
                        proc.stdout[:300])
            return []
        with open(result_path) as rf:
            result = json.load(rf)
        log.info('[doc_anonymizer] LLM returned %d entities', len(result))
        return result
    except _sp.TimeoutExpired:
        log.warning('[doc_anonymizer] LLM subprocess timed out (600s)')
        return []
    except Exception as e:
        log.warning('[doc_anonymizer] LLM subprocess error: %s', e)
        return []
    finally:
        for p in (text_path, result_path):
            if os.path.exists(p):
                os.unlink(p)


def _normalize_category(cat):
    """Strip diacritics and uppercase: IMIĘ -> IMIE, Nazwisko -> NAZWISKO."""
    nfkd = unicodedata.normalize('NFKD', cat)
    ascii_str = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return ascii_str.upper().strip()


# -- Replacement logic ------------------------------------------------------

_PLACEHOLDER_MAP = {
    'IMIE': '[IMIE]',
    'NAZWISKO': '[NAZWISKO]',
    'IMIE_NAZWISKO': '[OSOBA]',
    'PESEL': '[PESEL]',
    'DATA_URODZENIA': '[DATA]',
    'DATA': '[DATA]',
    'ADRES': '[ADRES]',
    'TELEFON': '[TELEFON]',
    'EMAIL': '[EMAIL]',
    'NR_PACJENTA': '[NR_PACJENTA]',
    'NR_DOKUMENTU': '[NR_DOKUMENTU]',
    'NR_KONTA': '[NR_KONTA]',
    'NAZWA_PLACOWKI': '[PLACOWKA]',
    'LEKARZ': '[LEKARZ]',
    'INNE_PII': '[DANE_OSOBOWE]',
}


def _replace_entities_in_text(text, entities):
    """Replace PII entities with placeholders in text."""
    seen_texts = {}
    for e in entities:
        cat = _normalize_category(e.get('category', 'INNE_PII'))
        if e['text'] not in seen_texts:
            seen_texts[e['text']] = cat

    unique = [{'text': t, 'category': c} for t, c in seen_texts.items()]
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

def _redact_pdf(src_path, output_path, entities):
    """Redact PII in a PDF using PyMuPDF — preserves original layout."""
    import fitz

    seen_texts = {}
    for e in entities:
        cat = _normalize_category(e.get('category', 'INNE_PII'))
        txt = e.get('text', '').strip()
        if txt and txt not in seen_texts:
            seen_texts[txt] = _PLACEHOLDER_MAP.get(cat, '[DANE]')

    # Sort by length descending so longer matches take priority
    sorted_items = sorted(seen_texts.items(), key=lambda x: len(x[0]), reverse=True)

    doc = fitz.open(src_path)
    total_redactions = []

    for page in doc:
        for original, placeholder in sorted_items:
            instances = page.search_for(original)
            for inst in instances:
                page.add_redact_annot(
                    inst, text=placeholder, fontsize=0,
                    fill=(0, 0, 0), text_color=(1, 1, 1),
                )
                total_redactions.append({
                    'original': original,
                    'placeholder': placeholder,
                    'category': next(
                        (_normalize_category(e['category'])
                         for e in entities if e.get('text', '').strip() == original),
                        'INNE_PII'),
                    'occurrences': 1,
                })

    for page in doc:
        page.apply_redactions()

    # Add a small "ZANONIMIZOWANO" watermark on first page
    first = doc[0]
    first.insert_text(
        (first.rect.width - 180, 20),
        'DOKUMENT ZANONIMIZOWANY',
        fontsize=8, color=(0.5, 0.5, 0.5),
    )

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

    # Merge redaction counts
    seen = {}
    for r in total_redactions:
        key = r['original']
        if key not in seen:
            seen[key] = r
        else:
            seen[key]['occurrences'] += 1
    return list(seen.values())


def _anonymize_docx_inplace(src_path, output_path, all_entities):
    """Anonymize a DOCX preserving original formatting."""
    import docx

    seen_texts = {}
    for e in all_entities:
        cat = _normalize_category(e.get('category', 'INNE_PII'))
        if e['text'] not in seen_texts:
            seen_texts[e['text']] = cat

    unique = [{'text': t, 'category': c} for t, c in seen_texts.items()]
    unique.sort(key=lambda e: len(e['text']), reverse=True)

    replacements = {}
    for e in unique:
        placeholder = _PLACEHOLDER_MAP.get(e['category'], '[' + e['category'] + ']')
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


def _cleanup_stuck_jobs():
    """Mark any 'processing' jobs as 'error' on startup (server crashed)."""
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
                meta['error'] = 'Serwer zostal zrestartowany w trakcie przetwarzania.'
                with open(meta_path, 'w') as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                log.info('[doc_anonymizer] Marked stuck job %s as error', entry)
        except Exception:
            continue


_cleanup_stuck_jobs()


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

        # For PDFs, normalize text to fix fragmented extraction
        # (digits split across lines, newlines in bank accounts, etc.)
        if file_ext == '.pdf':
            text_parts = [re.sub(r'(\d)[\s\n]+(\d)', r'\1 \2', p) for p in text_parts]

        # Concatenate all text for detection (catches items spanning pages)
        full_text = '\n\n'.join(t for t in text_parts if t.strip())
        # Normalize digit sequences in full text (catches cross-page items)
        full_text = re.sub(r'(\d)[\s\n]+(\d)', r'\1 \2', full_text)

        # Phase 1: Regex-based detection on full text (fast, reliable)
        _emit_progress(job_id, 'regex', 20,
                       'Wykrywanie PESEL, telefonow, adresow (regex)...')
        regex_hits = _regex_detect(full_text)
        all_entities.extend(regex_hits)

        # Phase 2: Name & facility detection (regex-based, high recall)
        _emit_progress(job_id, 'analyzing', 40,
                       'Wykrywanie imion, nazwisk i placowek...')
        name_hits = _detect_names(full_text)
        facility_hits = _detect_facilities(full_text)
        all_entities.extend(name_hits)
        all_entities.extend(facility_hits)

        # Phase 3: LLM-based detection (supplementary, catches unusual names)
        _emit_progress(job_id, 'analyzing', 55,
                       'Analiza LLM (dodatkowe imiona/nazwiska)...')
        try:
            llm_entities = _call_llm(full_text)
            # Only add LLM entities not already found by regex/name detection
            existing_texts = {e['text'].lower() for e in all_entities}
            for le in llm_entities:
                if le.get('text', '').lower() not in existing_texts:
                    all_entities.append(le)
                    existing_texts.add(le['text'].lower())
        except Exception as e:
            log.warning('[doc_anonymizer] LLM error: %s', e)

        _emit_progress(job_id, 'replacing', 85,
                       'Zastepowanie danych osobowych...')

        _emit_progress(job_id, 'generating', 90,
                       'Generowanie zanonimizowanego dokumentu...')

        out_filename = 'anonymized_' + filename
        out_path = os.path.join(job, out_filename)

        if file_ext == '.pdf':
            redact_repls = _redact_pdf(src_path, out_path, all_entities)
            seen_repls = {}
            for r in redact_repls:
                key = r['original']
                if key not in seen_repls:
                    seen_repls[key] = r
                else:
                    seen_repls[key]['occurrences'] += r['occurrences']
        elif file_ext in ('.docx', '.doc'):
            _anonymize_docx_inplace(src_path, out_path, all_entities)
            seen_repls = {}
            for e in all_entities:
                txt = e.get('text', '').strip()
                if not txt or txt in seen_repls:
                    continue
                cat = _normalize_category(e.get('category', 'INNE_PII'))
                seen_repls[txt] = {
                    'original': txt,
                    'placeholder': _PLACEHOLDER_MAP.get(cat, '[DANE]'),
                    'category': cat,
                    'occurrences': 1,
                }
        else:
            seen_repls = {}

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