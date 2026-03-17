# APP DEVELOPMENT GUIDE
## Kompletny przewodnik tworzenia aplikacji dla EthOS NAS OS

> Przygotowane przez: **System Architect** | Wersja: 1.0
> Data: 2026-03-17

---

## Spis treści

1. [Przegląd architektury aplikacji](#1-przegląd-architektury-aplikacji)
2. [Struktura plików](#2-struktura-plików)
3. [Krok po kroku: nowa aplikacja](#3-krok-po-kroku-nowa-aplikacja)
4. [Backend: Flask Blueprint](#4-backend-flask-blueprint)
5. [Frontend: JavaScript (AppRegistry + createWindow)](#5-frontend-javascript)
6. [Frontend: CSS](#6-frontend-css)
7. [Rejestracja w systemie](#7-rejestracja-w-systemie)
8. [API Helpers i narzędzia](#8-api-helpers-i-narzędzia)
9. [Kompletny przykład: Sticky Notes](#9-kompletny-przykład-sticky-notes)
10. [Deployment](#10-deployment)

---

## 1. Przegląd architektury aplikacji

Każda aplikacja EthOS składa się z **4 elementów**:

```
┌─────────────────────────────────────────────────────────────┐
│  1. BACKEND BLUEPRINT     backend/blueprints/{app}.py       │
│     └─ Flask Blueprint z REST API                           │
│     └─ Dane: JSON w data/ via load_json/save_json           │
│                                                             │
│  2. FRONTEND JS           frontend/js/apps/{app}.js         │
│     └─ AppRegistry['{app-id}'] = function(appDef, opts)     │
│     └─ createWindow() z onRender callback                   │
│                                                             │
│  3. FRONTEND CSS          frontend/css/apps.css             │
│     └─ Klasy z prefixem .{xx}-  (np. .tk- dla tickets)     │
│                                                             │
│  4. REJESTRACJA           backend/app.py                    │
│     └─ import + register_blueprint                          │
│     └─ Wpis w get_apps() → /api/apps                       │
│     └─ Wpis w _API_TO_APP → uprawnienia                    │
│     └─ <script> tag w frontend/index.html                   │
└─────────────────────────────────────────────────────────────┘
```

### Przepływ danych

```
Użytkownik klika ikonę → desktop.js wywołuje AppRegistry[id]()
  → createWindow(id, { onRender: (body) => render(body) })
    → render(body) ustawia body.innerHTML i binduje eventy
      → api('/app/action') → fetch('/api/app/action', {Bearer token})
        → Flask @blueprint.route('/api/app/action')
          → load_json() / save_json() z data/
            → return jsonify({ok: true, ...})
```

---

## 2. Struktura plików

```
/opt/ethos/
├── backend/
│   ├── app.py                    ← GŁÓWNY plik (7122 linii)
│   │   ├── Linia ~49-89:          import blueprintów
│   │   ├── Linia ~108-141:        register_blueprint()
│   │   ├── Linia ~323-333:        require_auth decorator
│   │   ├── Linia ~360-380:        _API_TO_APP mapping (uprawnienia)
│   │   └── Linia ~5710-6091:      get_apps() → lista aplikacji
│   ├── host.py                   ← Abstrakcja systemowa
│   │   ├── data_path(rel)          → /opt/ethos/data/{rel}
│   │   ├── user_data_path(f, u)    → /opt/ethos/data/{name}_{user}.json
│   │   ├── host_run(cmd)           → subprocess bash -c wrapper
│   │   └── q(s)                    → shell-quote string
│   ├── utils.py                  ← Narzędzia wspólne
│   │   ├── load_json(path, default)
│   │   ├── save_json(path, data)
│   │   └── safe_path(user_path)
│   └── blueprints/
│       ├── stickynotes.py        ← Wzorcowy prosty blueprint (3.5KB)
│       ├── editor.py             ← Blueprint średni (~12KB)
│       ├── downloads.py          ← Blueprint duży (~98KB)
│       └── ... (37 blueprintów)
│
├── frontend/
│   ├── index.html                ← SPA entry point
│   │   └── Linia ~173-210:        <script> tagi dla apps
│   ├── js/
│   │   ├── desktop.js (84KB)     ← Window manager, createWindow()
│   │   ├── apps.js (349KB)       ← Inline apps + AppRegistry{}
│   │   ├── i18n.js               ← Tłumaczenia, t() function
│   │   └── apps/
│   │       ├── stickynotes.js    ← Wzorcowa prosta app
│   │       ├── editor.js         ← App z window (createWindow)
│   │       └── ... (25+ plików)
│   └── css/
│       ├── style.css             ← Główne style + zmienne CSS
│       └── apps.css              ← Style per-app (.tk-, .fm-, .sn-)
│
└── data/                         ← Runtime data (JSON)
    ├── stickynotes.json
    ├── downloads_config_marcin.json  (per-user)
    └── ...
```

---

## 3. Krok po kroku: nowa aplikacja

### Checklist (w tej kolejności):

```
□ 1. Utworzyć  backend/blueprints/{app}.py        ← Blueprint + API
□ 2. Utworzyć  frontend/js/apps/{app}.js           ← UI + logika
□ 3. Dodać    CSS w frontend/css/apps.css          ← Style .xx-*
□ 4. Edytować backend/app.py:
│    □ 4a. Dodać import:    from blueprints.{app} import {app}_bp
│    □ 4b. Dodać register:  app.register_blueprint({app}_bp)
│    □ 4c. Dodać do get_apps(): {'id': '...', 'name': '...', ...}
│    □ 4d. Dodać do _API_TO_APP: '/api/{app}/': '{app-id}'
□ 5. Edytować frontend/index.html:
│    □ 5a. Dodać <script src="js/apps/{app}.js?v=1"></script>
□ 6. Restart: sudo systemctl restart ethos
□ 7. Test w przeglądarce
□ 8. Git commit + push
```

---

## 4. Backend: Flask Blueprint

### Template (skopiuj i dostosuj):

```python
"""
EthOS — {Nazwa Aplikacji}
{Opis co robi}
Wszystkie endpointy pod /api/{app-prefix}.
"""

import os, uuid, time, logging
from flask import Blueprint, jsonify, request, g

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path as _data_path
from utils import load_json as _load_json, save_json as _save_json

log = logging.getLogger('{app_name}')

# ─── Blueprint ───
{app}_bp = Blueprint('{app_name}', __name__, url_prefix='/api/{app-prefix}')

# ─── Data ───
DATA_FILE = _data_path('{app_name}.json')

def _load():
    return _load_json(DATA_FILE, {'items': []})

def _save(data):
    _save_json(DATA_FILE, data)

# ─── Routes ───

@{app}_bp.route('', methods=['GET'])
def list_items():
    """Lista wszystkich elementów."""
    return jsonify(_load())

@{app}_bp.route('', methods=['POST'])
def create_item():
    """Utwórz nowy element."""
    body = request.json or {}
    item = {
        'id': uuid.uuid4().hex[:12],
        'title': str(body.get('title', '')).strip()[:200],
        'created': time.time(),
        'updated': time.time(),
    }
    data = _load()
    data['items'].insert(0, item)
    _save(data)
    return jsonify({'ok': True, 'item': item})

@{app}_bp.route('/<item_id>', methods=['PUT'])
def update_item(item_id):
    """Edytuj element."""
    data = _load()
    item = next((i for i in data['items'] if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Nie znaleziono'}), 404
    body = request.json or {}
    if 'title' in body:
        item['title'] = str(body['title']).strip()[:200]
    item['updated'] = time.time()
    _save(data)
    return jsonify({'ok': True, 'item': item})

@{app}_bp.route('/<item_id>', methods=['DELETE'])
def delete_item(item_id):
    """Usuń element."""
    data = _load()
    data['items'] = [i for i in data['items'] if i['id'] != item_id]
    _save(data)
    return jsonify({'ok': True})
```

### Kluczowe zasady backendu:

| Zasada | Przykład |
|--------|----------|
| **Zwracaj JSON** | `return jsonify({'ok': True, 'item': item})` |
| **Błędy z kodem HTTP** | `return jsonify({'error': 'Nie znaleziono'}), 404` |
| **Waliduj input** | `str(body.get('title', '')).strip()[:200]` |
| **UUID na ID** | `uuid.uuid4().hex[:12]` |
| **Timestamp** | `time.time()` (Unix float) |
| **Per-user dane** | `user_data_path('app.json', g.username)` |
| **Shell commands** | `host_run(f"ls {q(path)}")` — ZAWSZE użyj `q()` |

### Dostęp do kontekstu użytkownika:

```python
from flask import g

# Dostępne w każdym requeście (ustawiane przez before_request w app.py):
g.username   # str: "marcin"
g.role       # str: "admin" | "user"
g.sudo_mode  # bool: True jeśli admin
```

### load_json / save_json — pełne sygnatury:

```python
def load_json(path, default=None):
    """Wczytaj JSON, zwróć default jeśli brak pliku lub błąd.
    Zwraca KOPIĘ default (bezpieczne mutable obiekty)."""

def save_json(path, data, *, ensure_ascii=False, indent=2):
    """Zapisz JSON atomicznie. Tworzy katalogi automatycznie."""
```

### data_path / user_data_path:

```python
from host import data_path, user_data_path

data_path('tickets.json')
# → /opt/ethos/data/tickets.json

user_data_path('tickets_prefs.json', 'marcin')
# → /opt/ethos/data/tickets_prefs_marcin.json
```

---

## 5. Frontend: JavaScript

### AppRegistry — rejestracja aplikacji:

```javascript
/**
 * EthOS — {Nazwa aplikacji}
 * {Opis}
 */

// AppRegistry MUSI być globalny (zdefiniowany w apps.js linia 6)
AppRegistry['{app-id}'] = function (appDef, launchOpts) {
    const winId = '{app-id}';

    createWindow(winId, {
        title: t('{Nazwa}'),                    // i18n
        icon: appDef?.icon || 'fa-{icon}',      // FontAwesome
        iconColor: appDef?.color || '#3b82f6',   // kolor akcentu
        width: 1100,                             // px
        height: 700,
        minWidth: 600,
        minHeight: 400,
        onRender: (body) => renderApp(body, launchOpts),
    });
};
```

### createWindow — pełna sygnatura:

```javascript
createWindow(id, {
    title: string,        // Tytuł okna (użyj t() dla i18n)
    icon: string,         // Klasa FontAwesome: 'fa-folder'
    iconColor: string,    // Kolor hex: '#f59e0b'
    width: number,        // Szerokość startowa w px (default: 900)
    height: number,       // Wysokość startowa w px (default: 600)
    minWidth: number,     // Min szerokość (default: 400)
    minHeight: number,    // Min wysokość (default: 250)
    content: string,      // HTML string (alternatywa dla onRender)
    onRender: function,   // function(bodyElement) — PREFEROWANY sposób
    onClose: function,    // Callback przy zamknięciu
    singleton: boolean,   // Reużyj istniejące okno (default: true)
});
```

### Render function — wzorzec:

```javascript
function renderApp(body, launchOpts) {
    // 1. Stan aplikacji (closure scope)
    let items = [];
    let currentFilter = 'all';

    // 2. Utwórz HTML
    body.innerHTML = `
        <div class="xx-app">
            <div class="xx-toolbar">
                <button class="xx-btn" id="xx-add">
                    <i class="fas fa-plus"></i> ${t('Dodaj')}
                </button>
            </div>
            <div class="xx-content" id="xx-content">
                <div class="xx-loading">
                    <i class="fas fa-spinner fa-spin"></i>
                </div>
            </div>
        </div>
    `;

    // 3. Referencje do elementów
    const content = body.querySelector('#xx-content');
    const addBtn = body.querySelector('#xx-add');

    // 4. Event handlers
    addBtn.addEventListener('click', () => createItem());

    // 5. Funkcje CRUD
    async function loadItems() {
        const data = await api('/app-prefix');
        items = data.items || [];
        renderList();
    }

    async function createItem() {
        const res = await api('/app-prefix', {
            method: 'POST',
            body: { title: 'Nowy element' }
        });
        if (res.ok) {
            toast(t('Utworzono'), 'success');
            loadItems();
        }
    }

    function renderList() {
        content.innerHTML = items.map(item => `
            <div class="xx-item" data-id="${item.id}">
                ${_escHtml(item.title)}
            </div>
        `).join('');
    }

    // 6. Inicjalizacja
    loadItems();
}
```

### Dostępne globalne funkcje:

| Funkcja | Opis | Przykład |
|---------|------|---------|
| `api(path, opts)` | Fetch z auto Bearer token | `api('/notes', {method:'POST', body:{title}})` |
| `toast(msg, type)` | Powiadomienie | `toast('Zapisano', 'success')` |
| `t(key)` | Tłumaczenie i18n | `t('Dodaj')` |
| `createWindow(id, opts)` | Otwórz okno | Zobacz wyżej |
| `closeWindow(id)` | Zamknij okno | `closeWindow('my-app')` |
| `focusWindow(id)` | Fokus na okno | `focusWindow('my-app')` |
| `NAS.token` | Token auth | Auto-dołączany przez `api()` |
| `NAS.username` | Zalogowany user | `NAS.username` |
| `_escHtml(str)` | Escape HTML | Zapobiega XSS w innerHTML |

### api() — pełna implementacja (z desktop.js):

```javascript
async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (NAS.token) headers['Authorization'] = `Bearer ${NAS.token}`;

    // Auto JSON stringify jeśli body nie jest FormData
    if (options.body && !(options.body instanceof FormData)) {
        headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(options.body);
    }

    const resp = await fetch(`/api${path}`, { ...options, headers });
    if (resp.status === 401) {
        showLogin();
        throw new Error('Unauthorized');
    }
    return resp.json();
}
```

> **UWAGA**: `api()` automatycznie dodaje prefix `/api` do ścieżki!
> Czyli `api('/notes')` → `fetch('/api/notes')`.
> Blueprint z `url_prefix='/api/notes'` odpowie na to.

### toast() — typy:

```javascript
toast('Sukces!', 'success');   // ✅ zielony
toast('Błąd!', 'error');       // ❌ czerwony
toast('Uwaga!', 'warning');    // ⚠️ pomarańczowy
toast('Info', 'info');         // ℹ️ niebieski
```

---

## 6. Frontend: CSS

### Konwencja nazewnictwa:

- **Prefix per app**: `.tk-` (tickets), `.fm-` (file-manager), `.sn-` (stickynotes), `.dte-` (doc-editor), `.dl-` (downloads)
- **Komponenty**: `.tk-board`, `.tk-column`, `.tk-card`, `.tk-btn`
- **Stany**: `.tk-active`, `.tk-selected`, `.tk-dragging`
- **Układ**: `.tk-header`, `.tk-body`, `.tk-footer`, `.tk-sidebar`

### Dostępne zmienne CSS (z style.css):

```css
/* ─── Kolory ─── */
--accent            /* Główny akcent (niebieski) */
--accent-green      /* Sukces */
--accent-orange     /* Ostrzeżenie */
--accent-purple     /* Dekoracyjny */
--accent-cyan       /* Informacyjny */
--danger            /* Błąd/usuwanie (czerwony) */
--text              /* Tekst główny */
--text-muted        /* Tekst drugorzędny */
--bg                /* Tło główne */
--bg-primary        /* Tło kart/paneli */
--bg-hover          /* Tło przy hover */
--bg-deep           /* Najciemniejsze tło (inputy) */
--bg-base           /* Tło podstawowe */
--border            /* Kolor obramowań */
--overlay-2         /* Overlay lekki */
--overlay-3         /* Overlay średni */

/* ─── Geometria ─── */
--r-sm              /* border-radius mały (4px) */
--r-md              /* border-radius średni (8px) */
--r-lg              /* border-radius duży (12px) */
--shadow-sm         /* Mały cień */
--shadow-md         /* Średni cień */
--transition-fast   /* Szybka animacja (150ms) */
```

### Dark/Light theme support:

```css
/* Domyślne style działają z obydwoma motywami dzięki zmiennym CSS */
.tk-card {
    background: var(--bg-primary);     /* ← auto dark/light */
    border: 1px solid var(--border);
    color: var(--text);
}

/* TYLKO jeśli potrzeba override dla dark theme: */
[data-theme="dark"] .tk-card {
    background: #1e293b;
}

/* ⚠️ UWAGA: Elementy symulujące papier/dokument
   (edytor, podgląd) powinny mieć STAŁE kolory,
   nie var(--text-primary). Bug: biały tekst na ciemnym tle. */
.tk-document-preview {
    background: #ffffff;
    color: #000000;    /* ← STAŁE, nie var() */
}
```

### Wzorzec sekcji CSS w apps.css:

```css
/* ═══════════════════════════════════════════════
   Tickets — Kanban Board
   ═══════════════════════════════════════════════ */

.tk-app {
    display: flex;
    flex-direction: column;
    height: 100%;
    background: var(--bg-base);
}

/* ... reszta klas .tk-* ... */
```

### Shared utility classes (już dostępne):

```css
.btn-green    /* Zielony przycisk */
.btn-red      /* Czerwony przycisk */
.btn-orange   /* Pomarańczowy przycisk */
.btn-purple   /* Fioletowy przycisk */
.btn-cyan     /* Cyjanowy przycisk */
.fm-badge     /* Badge/etykieta */
.fm-badge-green
.fm-input     /* Stylizowany input */
.fm-table     /* Stylizowana tabela */
.hidden       /* display: none !important */
```

---

## 7. Rejestracja w systemie

### 7a. Import blueprint (app.py ~linia 86):

```python
# Dodaj wśród istniejących importów:
from blueprints.tickets import tickets_bp
```

### 7b. Register blueprint (app.py ~linia 139):

```python
# Dodaj wśród istniejących register:
app.register_blueprint(tickets_bp)
```

### 7c. Dodaj do get_apps() (app.py ~linia 5710+):

```python
# W liście apps = [...] wewnątrz get_apps():
{
    'id': 'tickets',                              # MUSI == AppRegistry key
    'name': 'Tickets',                            # Wyświetlana nazwa
    'icon': 'fa-columns',                         # FontAwesome ikona
    'color': '#8b5cf6',                           # Kolor ikony
    'type': 'builtin',                            # Typ: builtin
    'category': 'Narzędzia',                      # Kategoria w menu
    'description': 'Zarządzanie projektami Kanban' # Opis
},
```

**Dostępne kategorie**: `'System'`, `'Narzędzia'`, `'Sieć'`, `'Multimedia'`

### 7d. Dodaj do _API_TO_APP (app.py ~linia 376):

```python
# W dict _API_TO_APP:
'/api/tickets/': 'tickets',
```

To mapowanie pozwala systemowi uprawnień wiedzieć,
który app odpowiada za które API endpointy.

### 7e. Dodaj script tag (index.html ~linia 208):

```html
<!-- Dodaj wśród istniejących script tags: -->
<script src="js/apps/tickets.js?v=1"></script>
```

> **?v=1** — wersjonowanie cache-bust. Zwiększ przy aktualizacjach.

---

## 8. API Helpers i narzędzia

### Backend: host.py

```python
from host import (
    data_path,          # data_path('file.json') → /opt/ethos/data/file.json
    user_data_path,     # user_data_path('f.json', 'marcin') → data/f_marcin.json
    host_run,           # host_run('ls /tmp') → CompletedProcess
    host_run_stream,    # host_run_stream('cmd') → yields (line, exit_code)
    q,                  # q(user_input) → shell-safe quoted string
    ETHOS_ROOT,         # '/opt/ethos'
    DATA_DIR,           # '/opt/ethos/data'
    LOG_DIR,            # '/opt/ethos/logs'
)
```

### Backend: utils.py

```python
from utils import (
    load_json,          # load_json(path, default={}) → dict/list
    save_json,          # save_json(path, data) → None (tworzy dirs)
    safe_path,          # safe_path('/home/marcin/f') → real path or None
)
```

### Backend: auth (z app.py, importowany automatycznie)

```python
from flask import g

# W request context (po @require_auth):
g.username    # str: zalogowany user
g.role        # str: 'admin' lub 'user'
g.sudo_mode   # bool: True jeśli admin
```

### Frontend: globalne obiekty

```javascript
// NAS — globalny obiekt stanu
NAS.token      // Bearer token
NAS.username   // Zalogowany użytkownik
NAS.apps       // Lista aplikacji z /api/apps

// WM — Window Manager
WM.windows     // Map<id, windowData>

// AppRegistry — rejestr renderów aplikacji
AppRegistry['app-id'] = function(appDef, launchOpts) { ... }
```

---

## 9. Kompletny przykład: Sticky Notes

Najprostsza pełna aplikacja w EthOS — idealny wzorzec.

### Backend: `backend/blueprints/stickynotes.py`

```python
"""
EthOS — Sticky Notes Blueprint
Simple persistent notes – like Windows Sticky Notes.
All routes under /api/notes.
"""
import os, uuid, time, logging
from flask import Blueprint, jsonify, request

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path as _data_path
from utils import load_json as _load_json, save_json as _save_json

log = logging.getLogger('stickynotes')

notes_bp = Blueprint('stickynotes', __name__, url_prefix='/api/notes')

NOTES_FILE = _data_path('stickynotes.json')

def _load_notes():
    return _load_json(NOTES_FILE, {'notes': [], 'order': []})

def _save_notes(data):
    _save_json(NOTES_FILE, data)

@notes_bp.route('', methods=['GET'])
def get_notes():
    return jsonify(_load_notes())

@notes_bp.route('', methods=['POST'])
def create_note():
    body = request.json or {}
    note = {
        'id': uuid.uuid4().hex[:12],
        'title': body.get('title', '').strip()[:100] or '',
        'content': body.get('content', '').strip()[:5000] or '',
        'color': body.get('color', 'yellow'),
        'pinned': bool(body.get('pinned', False)),
        'created': time.time(),
        'updated': time.time(),
    }
    data = _load_notes()
    data['notes'].insert(0, note)
    data['order'].insert(0, note['id'])
    _save_notes(data)
    return jsonify({'ok': True, 'note': note})

@notes_bp.route('/<note_id>', methods=['PUT'])
def update_note(note_id):
    data = _load_notes()
    note = next((n for n in data['notes'] if n['id'] == note_id), None)
    if not note:
        return jsonify({'error': 'Nie znaleziono notatki'}), 404
    body = request.json or {}
    for key in ('title', 'content', 'color', 'pinned'):
        if key in body:
            note[key] = body[key]
    note['updated'] = time.time()
    _save_notes(data)
    return jsonify({'ok': True, 'note': note})

@notes_bp.route('/<note_id>', methods=['DELETE'])
def delete_note(note_id):
    data = _load_notes()
    data['notes'] = [n for n in data['notes'] if n['id'] != note_id]
    data['order'] = [i for i in data['order'] if i != note_id]
    _save_notes(data)
    return jsonify({'ok': True})
```

### Frontend: `frontend/js/apps/stickynotes.js`

```javascript
AppRegistry['sticky-notes'] = function (appDef) {
    const panelId = 'sticky-notes';
    const existing = document.getElementById('sn-panel');
    if (existing) { _stickyClose(existing, panelId); return; }
    _stickyOpenPanel(panelId);
};

// Panel otwiera się jako floating panel (nie createWindow)
// Dla bardziej złożonych apps → użyj createWindow (patrz editor.js)
```

### Rejestracja w app.py:

```python
# Import (linia ~86):
from blueprints.stickynotes import notes_bp

# Register (linia ~139):
app.register_blueprint(notes_bp)

# get_apps() lista (linia ~6042):
{
    'id': 'sticky-notes',
    'name': 'Karteczki',
    'icon': 'fa-sticky-note',
    'color': '#eab308',
    'type': 'builtin',
    'category': 'Narzędzia',
    'description': 'Szybkie notatki'
},

# _API_TO_APP (linia ~376):
'/api/notes/': 'sticky-notes',
```

### Script tag w index.html (linia ~208):

```html
<script src="js/apps/stickynotes.js?v=2"></script>
```

---

## 10. Deployment

### Po utworzeniu/edycji plików:

```bash
# 1. Restart serwisu (WYMAGANY po każdej zmianie backendu)
sudo systemctl restart ethos

# 2. Sprawdź czy działa
sudo systemctl status ethos

# 3. Test API
curl -s -H "Authorization: Bearer TOKEN" https://localhost/api/app-prefix | head

# 4. Test w przeglądarce — wyczyść cache (Ctrl+Shift+R)

# 5. Commit
cd /opt/ethos
sudo git add -A
sudo git commit -m "Add {app-name} module

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"

# 6. Push (wymaga SSH key)
sudo GIT_SSH_COMMAND="ssh -i /home/marcin/.ssh/id_ed25519" git push origin main
```

### Ważne uwagi:

| Uwaga | Szczegóły |
|-------|-----------|
| **Brak hot-reload** | Backend wymaga `systemctl restart ethos` po zmianach |
| **Frontend cache** | Zwiększ `?v=N` w script tagu lub Ctrl+Shift+R |
| **Uprawnienia plików** | Dir owned by UID 501 — użyj `sudo` do edycji |
| **Logi** | `journalctl -u ethos -f` — live logi serwisu |
| **Dane JSON** | Przeglądaj: `cat /opt/ethos/data/{app}.json \| python3 -m json.tool` |
| **Safe directory** | `git config --global --add safe.directory /opt/ethos` |

---

## Podsumowanie — Quick Reference

```
NOWA APLIKACJA = 4 pliki + 4 edycje

Pliki do UTWORZENIA:
  backend/blueprints/{app}.py      ← REST API (Flask Blueprint)
  frontend/js/apps/{app}.js        ← UI (AppRegistry + createWindow)

Pliki do EDYCJI:
  frontend/css/apps.css            ← Dodaj sekcję .xx-*
  backend/app.py                   ← import + register + get_apps + _API_TO_APP
  frontend/index.html              ← <script> tag

RESTART:
  sudo systemctl restart ethos
```
