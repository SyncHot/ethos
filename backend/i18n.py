"""
EthOS — Backend Internationalization (i18n) Helper

Usage:
    from i18n import t
    t('auth.invalid_password')  # returns translated string for current request language
    t('setup.username_required', min=2)  # with format arguments
"""

import os
import json
import threading
from flask import request

_translations = {}
_translations_lock = threading.Lock()

SUPPORTED_LANGUAGES = ('en', 'pl', 'de', 'fr', 'es')
DEFAULT_LANGUAGE = 'en'

_I18N_DIR = os.path.join(os.path.dirname(__file__), 'i18n')


def _load_lang(lang):
    path = os.path.join(_I18N_DIR, f'{lang}.json')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def _ensure_loaded(lang):
    if lang not in _translations:
        with _translations_lock:
            if lang not in _translations:
                _translations[lang] = _load_lang(lang)


def _get_request_lang():
    """Detect language from request: query param > env > Accept-Language > default."""
    try:
        lang = request.args.get('lang') or ''
        if lang in SUPPORTED_LANGUAGES:
            return lang
    except RuntimeError:
        pass

    env_lang = os.environ.get('LANGUAGE', '').strip()
    if env_lang in SUPPORTED_LANGUAGES:
        return env_lang

    try:
        accept = request.headers.get('Accept-Language', '')
        for part in accept.split(','):
            code = part.split(';')[0].strip()[:2].lower()
            if code in SUPPORTED_LANGUAGES:
                return code
    except RuntimeError:
        pass

    return DEFAULT_LANGUAGE


def t(key, **kwargs):
    """Translate a key. Falls back to English, then returns key itself."""
    lang = _get_request_lang()
    _ensure_loaded(lang)
    _ensure_loaded('en')

    text = _translations.get(lang, {}).get(key)
    if text is None:
        text = _translations.get('en', {}).get(key)
    if text is None:
        return key

    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def reload_translations():
    """Force reload of all translation files."""
    with _translations_lock:
        _translations.clear()
