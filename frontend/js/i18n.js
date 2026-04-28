/* ═══════════════════════════════════════════════════════════
   EthOS — Internationalization (i18n) Engine
   Must be loaded BEFORE all other scripts.
   ═══════════════════════════════════════════════════════════ */

const I18n = {
    lang: 'pl',               // current language code
    strings: {},               // loaded translations { key: value }
    _cache: {},                // per-language cache of loaded JSON
    // Must stay in sync with backend/app.py -> SUPPORTED_LANGUAGES
    supportedLangs: {
        pl: { name: 'Polski',      flag: '🇵🇱', locale: 'pl-PL' },
        en: { name: 'English',     flag: '🇬🇧', locale: 'en-US' },
        de: { name: 'Deutsch',     flag: '🇩🇪', locale: 'de-DE' },
        fr: { name: 'Français',    flag: '🇫🇷', locale: 'fr-FR' },
        es: { name: 'Español',     flag: '🇪🇸', locale: 'es-ES' },
    },
};

/**
 * Translate a string key.
 * Polish text is used as the key — if language is 'pl' or key not found, returns key as-is.
 * Supports simple {placeholder} interpolation:
 *   t('Dysk danych: {disk}', { disk: '/dev/sda' })
 *
 * @param {string} key     The Polish source string (acts as translation key)
 * @param {Object} [params]  Optional interpolation values
 * @returns {string}
 */
function t(key, params) {
    if (!key) return '';
    let str = (I18n.lang !== 'pl' && I18n.strings[key]) ? I18n.strings[key] : key;
    if (params) {
        for (const [k, v] of Object.entries(params)) {
            str = str.replace(new RegExp(`\\{${k}\\}`, 'g'), v);
        }
    }
    return str;
}

/**
 * Get the locale string for date/time APIs (e.g. 'pl-PL', 'en-US').
 */
function getLocale() {
    return (I18n.supportedLangs[I18n.lang] || I18n.supportedLangs.pl).locale;
}

/**
 * Load a language — fetches the JSON file and switches.
 * @param {string} lang  Language code ('pl', 'en', etc.)
 * @param {boolean} [save=true]  Whether to persist to backend
 */
async function setLanguage(lang, save = true) {
    if (!I18n.supportedLangs[lang]) lang = 'pl';

    if (lang === 'pl') {
        // Polish is the source language — no translation needed
        I18n.strings = {};
        I18n.lang = 'pl';
    } else if (I18n._cache[lang]) {
        I18n.strings = I18n._cache[lang];
        I18n.lang = lang;
    } else {
        try {
            const resp = await fetch(`/locales/${lang}.json?v=${Date.now()}`);
            if (resp.ok) {
                const data = await resp.json();
                I18n._cache[lang] = data;
                I18n.strings = data;
                I18n.lang = lang;
            }
        } catch (e) {
            console.warn(`[i18n] Failed to load locale "${lang}":`, e);
        }
    }

    // Update HTML lang attribute
    document.documentElement.lang = lang;

    // Persist preference
    localStorage.setItem('ethos_lang', lang);
    if (save) {
        try {
            const headers = { 'Content-Type': 'application/json' };
            if (NAS && NAS.token) headers['Authorization'] = `Bearer ${NAS.token}`;
            await fetch('/api/language', { method: 'POST', headers, body: JSON.stringify({ language: lang }) });
        } catch { /* ignore during setup */ }
    }
}

/**
 * Initialize i18n — load saved language preference.
 * Called once before other scripts run.
 */
async function initI18n() {
    // Priority: localStorage > system default (from API)
    let lang = localStorage.getItem('ethos_lang');
    if (!lang) {
        try {
            const resp = await fetch('/api/language');
            if (resp.ok) {
                const data = await resp.json();
                if (Array.isArray(data.supported) && data.supported.length) {
                    const filtered = {};
                    data.supported.forEach(code => {
                        if (I18n.supportedLangs[code]) filtered[code] = I18n.supportedLangs[code];
                    });
                    if (Object.keys(filtered).length) {
                        I18n.supportedLangs = filtered;
                    }
                }
                lang = data.language || 'pl';
            }
        } catch { /* default to pl */ }
    }
    if (!I18n.supportedLangs[lang]) {
        lang = 'pl';
    }
    if (lang && lang !== 'pl') {
        await setLanguage(lang, false);
    } else {
        I18n.lang = 'pl';
        document.documentElement.lang = 'pl';
    }
}


/**
 * Translate all elements with data-i18n attributes in the given root.
 * - data-i18n="key"             → textContent = t(key)
 * - data-i18n-placeholder="key" → placeholder = t(key)
 * - data-i18n-title="key"       → title = t(key)
 */
function translateDOM(root) {
    root = root || document;
    root.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.dataset.i18n);
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    root.querySelectorAll('[data-i18n-title]').forEach(el => {
        el.title = t(el.dataset.i18nTitle);
    });
}
