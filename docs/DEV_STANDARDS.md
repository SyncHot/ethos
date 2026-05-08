# EthOS — Standardy Deweloperskie

> Przygotowane przez: **Senior Linux Kernel & Backend Dev** | Model referencyjny: OpenAI Codex / DeepSeek Coder
> Data: 2026-03-17

---

## Spis Treści

1. [Struktura Repozytorium](#1-struktura-repozytorium)
2. [Konwencje Nazewnictwa](#2-konwencje-nazewnictwa)
3. [Standardy Kodu Python](#3-standardy-kodu-python)
4. [Standardy Kodu JavaScript](#4-standardy-kodu-javascript)
5. [Standardy CSS](#5-standardy-css)
6. [API Design Guidelines](#6-api-design-guidelines)
7. [Dokumentacja Kodu](#7-dokumentacja-kodu)
8. [Git Workflow](#8-git-workflow)
9. [Procedura Wdrożenia Zmian](#9-procedura-wdrożenia-zmian)
10. [Przechowywanie Danych Aplikacji](#10-przechowywanie-danych-aplikacji-data-partition-rules)

---

## 1. Struktura Repozytorium

```
/opt/ethos/
├── backend/                    # Cały kod backendowy (Python)
│   ├── app.py                  # Główny plik aplikacji Flask (268KB, ~7122 linii)
│   │                           # Inicjalizacja, rejestracja blueprintów, Socket.IO
│   ├── host.py                 # Warstwa abstrakcji sprzętowej (21KB)
│   │                           # host_run(), host_run_stream(), q(), ścieżki
│   ├── utils.py                # Wspólne utility: safe_path(), formatowanie, logowanie
│   ├── crypto_utils.py         # Hashowanie haseł, funkcje kryptograficzne
│   ├── rag_engine.py           # Silnik RAG (Retrieval-Augmented Generation)
│   ├── model_library.py        # Zarządzanie modelami ML/AI
│   ├── requirements.txt        # Zależności Python (pip)
│   └── blueprints/             # 37 modułów Flask Blueprint
│       ├── storage.py          # 103KB — montowanie, udostępnianie, USB, RAID
│       ├── backup.py           # 134KB — snapshoty, wersjonowanie, Docker backup
│       ├── downloads.py        # 98KB — torrenty, DDL, debrid
│       ├── docker_manager.py   # Docker/Compose, CasaOS app store
│       ├── surveillance.py     # 74KB — kamery, nagrywanie
│       ├── builder.py          # 59KB — budowanie obrazów x86
│       ├── settings.py         # 50KB — ustawienia systemu
│       ├── installer.py        # 49KB — instalacja systemu
│       ├── vm_manager.py       # 40KB — maszyny wirtualne
│       ├── monitor.py          # 29KB — monitoring zasobów
│       ├── network.py          # 20KB — konfiguracja sieci
│       ├── users.py            # 14KB — zarządzanie użytkownikami
│       └── ...                 # Pozostałe blueprinty
│
├── frontend/                   # Cały kod frontendowy (Vanilla JS)
│   ├── index.html              # SPA entry point — jedyny plik HTML
│   ├── js/
│   │   ├── desktop.js          # 84KB — główny desktop UI, window manager
│   │   ├── apps.js             # 349KB — system aplikacji, AppRegistry
│   │   ├── setup.js            # 57KB — kreator pierwszej konfiguracji
│   │   ├── i18n.js             # System tłumaczeń — funkcja t()
│   │   └── apps/               # 25+ dedykowanych plików JS per aplikacja
│   │       ├── file_manager.js
│   │       ├── docker_app.js
│   │       ├── terminal.js
│   │       └── ...
│   ├── css/
│   │   ├── style.css           # Główne style, zmienne CSS, layout
│   │   └── apps.css            # Style specyficzne dla aplikacji
│   └── locales/                # Pliki tłumaczeń JSON
│       ├── pl.json             # Polski
│       ├── en.json             # Angielski
│       ├── de.json             # Niemiecki
│       ├── fr.json             # Francuski
│       └── es.json             # Hiszpański
│
├── installer/                  # Skrypty budowania obrazów ISO
│   ├── build.sh                # Główny skrypt budowania
│   └── ...                     # Konfiguracje debootstrap, GRUB
│
├── data/                       # Dane runtime (NIE w Git)
│   ├── config.json             # Konfiguracja globalna
│   ├── users.json              # Baza użytkowników
│   └── users/                  # Dane per-user
│       └── {username}/
│
├── logs/                       # Logi aplikacji (NIE w Git)
├── uploads/                    # Pliki tymczasowe uploadów (NIE w Git)
├── venv/                       # Python virtualenv (NIE w Git)
│
├── start.sh                    # Uruchomienie serwera
├── stop.sh                     # Zatrzymanie serwera
├── install.conf                # Konfiguracja instalacji
├── ethos.env                   # Zmienne środowiskowe
├── docs/                       # Dokumentacja projektu
└── .gitignore                  # Wykluczenia z Git
```

### Zasady katalogowe

- **`backend/`** — Tylko Python. Żadnego JS, CSS, HTML.
- **`frontend/`** — Tylko pliki serwowane klientowi. Żadnego Pythona.
- **`data/`**, **`logs/`**, **`uploads/`**, **`venv/`** — Wykluczone z Git.
- **`blueprints/`** — Jeden plik = jeden moduł funkcjonalny.

---

## 2. Konwencje Nazewnictwa

### 2.1 Python (Backend)

| Element              | Konwencja       | Przykład                            |
|----------------------|-----------------|--------------------------------------|
| Pliki                | `snake_case`    | `docker_manager.py`, `vm_manager.py` |
| Funkcje              | `snake_case`    | `get_storage_list()`, `start_vm()`   |
| Zmienne              | `snake_case`    | `device_path`, `mount_point`         |
| Stałe                | `UPPER_SNAKE`   | `ETHOS_ROOT`, `DATA_DIR`            |
| Blueprinty           | `{name}_bp`     | `storage_bp`, `backup_bp`           |
| Klasy (rzadko)       | `PascalCase`    | `BackupScheduler`                    |
| Endpointy            | `snake_case`    | `storage_list`, `docker_start`       |

**Blueprint naming pattern:**
```python
# Plik: blueprints/storage.py
storage_bp = Blueprint('storage', __name__)

# Plik: blueprints/docker_manager.py
docker_bp = Blueprint('docker', __name__)
```

### 2.2 JavaScript (Frontend)

| Element              | Konwencja       | Przykład                            |
|----------------------|-----------------|--------------------------------------|
| Pliki                | `snake_case`    | `file_manager.js`, `docker_app.js`   |
| Funkcje              | `camelCase`     | `loadStorageList()`, `openTerminal()`|
| Zmienne              | `camelCase`     | `currentPath`, `selectedDevice`      |
| Stałe                | `UPPER_SNAKE`   | `API_BASE`, `WS_URL`                |
| Klasy CSS            | `kebab-case`    | `app-container`, `toolbar-btn`       |
| ID elementów         | `camelCase`     | `mainDesktop`, `appWindow`           |
| Rejestr aplikacji    | `PascalCase`    | `AppRegistry.register('FileManager')`|

### 2.3 CSS — Prefiksy aplikacji

Każda aplikacja w EthOS ma dedykowany prefiks CSS, aby uniknąć kolizji:

| Prefiks | Aplikacja         | Przykład klasy                      |
|---------|--------------------|--------------------------------------|
| `dte-`  | Document Editor    | `dte-btn`, `dte-toolbar`, `dte-page` |
| `fm-`   | File Manager       | `fm-tree`, `fm-ana-panel`, `fm-path` |
| `dl-`   | Downloads          | `dl-list`, `dl-progress`, `dl-card`  |
| `trm-`  | Terminal           | `trm-output`, `trm-input`           |
| `mon-`  | Monitor            | `mon-chart`, `mon-cpu-bar`           |
| `dkr-`  | Docker Manager     | `dkr-container`, `dkr-stats`        |
| `cam-`  | Surveillance       | `cam-grid`, `cam-stream`            |
| `set-`  | Settings           | `set-panel`, `set-toggle`           |
| `vm-`   | VM Manager         | `vm-list`, `vm-console`             |
| `bkp-`  | Backup             | `bkp-schedule`, `bkp-history`       |

**Wzorzec BEM-like:**
```css
/* Blok */
.fm-tree { ... }

/* Element */
.fm-tree-item { ... }
.fm-tree-icon { ... }

/* Modyfikator */
.fm-tree-item--selected { ... }
.fm-tree-item--folder { ... }
```

---

## 3. Standardy Kodu Python

### 3.1 Tworzenie nowego Blueprint

Krok po kroku — dodawanie nowego modułu:

**1. Utwórz plik w `blueprints/`:**

```python
# backend/blueprints/my_module.py

from flask import Blueprint, request, jsonify
from host import host_run, q
from utils import safe_path

my_module_bp = Blueprint('my_module', __name__)


@my_module_bp.route('/api/my_module/list', methods=['GET'])
@require_auth
def my_module_list():
    """Lista zasobów modułu"""
    result = host_run("some-command --json")
    if result.returncode != 0:
        return jsonify(error=result.stderr.strip()), 500
    return jsonify(ok=True, data=json.loads(result.stdout))


@my_module_bp.route('/api/my_module/create', methods=['POST'])
@require_auth
def my_module_create():
    """Tworzenie nowego zasobu"""
    data = request.json
    if not data or 'name' not in data:
        return jsonify(error="Missing 'name' field"), 400

    name = data['name']
    result = host_run(f"create-resource {q(name)}", timeout=60)
    if result.returncode != 0:
        return jsonify(error=f"Failed to create: {result.stderr.strip()}"), 500

    return jsonify(ok=True, message=f"Resource '{name}' created")
```

**2. Zarejestruj w `app.py`:**

```python
# W app.py, sekcja importów blueprintów
from blueprints.my_module import my_module_bp

# W sekcji rejestracji
app.register_blueprint(my_module_bp)
```

### 3.2 Wzorzec @require_auth

**Każdy** endpoint publiczny (poza `/api/auth/login` i statycznymi plikami)
**MUSI** mieć dekorator `@require_auth`:

```python
@my_bp.route('/api/my_module/action', methods=['POST'])
@require_auth  # ← OBOWIĄZKOWY
def my_action():
    # request.current_user jest dostępny po walidacji tokena
    username = request.current_user
    return jsonify(ok=True, user=username)
```

### 3.3 Wzorce host_run()

**Proste polecenie:**
```python
result = host_run("systemctl status docker")
is_running = result.returncode == 0
```

**Z argumentami użytkownika (ZAWSZE q()):**
```python
result = host_run(f"ls -la {q(user_path)}")
```

**Z dłuższym timeoutem:**
```python
result = host_run(f"rsync -avz {q(src)} {q(dst)}", timeout=300)
```

**Streaming z postępem:**
```python
for line in host_run_stream(f"apt-get install -y {q(package)}"):
    socketio.emit('install_progress', {'line': line}, room=sid)
```

### 3.4 Obsługa błędów

Wzorzec obsługi błędów jest jednolity w całym projekcie:

```python
@my_bp.route('/api/my_module/action', methods=['POST'])
@require_auth
def my_action():
    try:
        data = request.json
        if not data:
            return jsonify(error="No JSON body"), 400

        # Walidacja
        required = ['name', 'path']
        for field in required:
            if field not in data:
                return jsonify(error=f"Missing field: {field}"), 400

        # Walidacja ścieżki
        if 'path' in data and not safe_path(data['path']):
            return jsonify(error="Path outside allowed scope"), 403

        # Operacja
        result = host_run(f"some-command {q(data['name'])}")
        if result.returncode != 0:
            return jsonify(error=result.stderr.strip()), 500

        return jsonify(ok=True, message="Done")

    except json.JSONDecodeError:
        return jsonify(error="Invalid JSON"), 400
    except subprocess.TimeoutExpired:
        return jsonify(error="Operation timed out"), 504
    except Exception as e:
        return jsonify(error=str(e)), 500
```

### 3.5 Format odpowiedzi JSON

**Sukces — zawsze pole `ok: true`:**
```python
return jsonify(ok=True)
return jsonify(ok=True, message="Created successfully")
return jsonify(ok=True, data=result_list)
return jsonify(ok=True, devices=devices, count=len(devices))
```

**Błąd — zawsze pole `error`:**
```python
return jsonify(error="Not found"), 404
return jsonify(error="Unauthorized"), 401
return jsonify(error="Validation failed", details=errors), 400
```

---

## 4. Standardy Kodu JavaScript

### 4.1 Zasada fundamentalna: Vanilla JS Only

EthOS **nie używa żadnych frameworków** JavaScript. Żadnego React, Vue, Angular,
Svelte, jQuery. Cały frontend to czysty (Vanilla) JavaScript ES6+.

**Powody:**
- Zero zależności = zero supply chain attacks
- Pełna kontrola nad DOM
- Mniejszy rozmiar (brak bundlera, brak node_modules)
- Prostsze debugowanie
- Działa natywnie w przeglądarce bez kompilacji

### 4.2 Rejestracja aplikacji — AppRegistry

Każda aplikacja frontendowa musi się zarejestrować w centralnym rejestrze:

```javascript
// frontend/js/apps/my_app.js

AppRegistry.register('MyApp', {
    name: 'My Application',
    icon: 'icon-my-app',
    category: 'tools',

    init(container, windowId) {
        container.innerHTML = `
            <div class="myapp-container">
                <div class="myapp-toolbar">
                    <button class="myapp-btn" onclick="MyApp.refresh()">
                        ${t('myapp.refresh')}
                    </button>
                </div>
                <div class="myapp-content" id="myapp-content-${windowId}">
                    ${t('myapp.loading')}
                </div>
            </div>
        `;
        this.load(windowId);
    },

    async load(windowId) {
        try {
            const data = await api('/api/my_module/list');
            const content = document.getElementById(`myapp-content-${windowId}`);
            if (data.ok) {
                content.innerHTML = this.renderList(data.items);
            } else {
                content.innerHTML = `<div class="error">${data.error}</div>`;
            }
        } catch (err) {
            toast(t('myapp.load_error'), 'error');
        }
    },

    renderList(items) {
        return items.map(item => `
            <div class="myapp-item" data-id="${item.id}">
                <span class="myapp-item-name">${item.name}</span>
                <span class="myapp-item-status">${item.status}</span>
            </div>
        `).join('');
    }
});
```

### 4.3 Funkcja api() — Wrapper HTTP

Wszystkie requesty do backendu przechodzą przez centralną funkcję `api()`:

```javascript
async function api(url, options = {}) {
    const token = localStorage.getItem('auth_token');
    const response = await fetch(url, {
        ...options,
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`,
            ...options.headers
        },
        body: options.body ? JSON.stringify(options.body) : undefined
    });
    return await response.json();
}

// Przykłady użycia:
const devices = await api('/api/storage/list');
const result = await api('/api/docker/containers/start', {
    method: 'POST',
    body: { id: containerId }
});
```

### 4.4 Socket.IO — Wzorce zdarzeń

```javascript
// Inicjalizacja (w desktop.js)
const socket = io({ transports: ['websocket'] });

// Nasłuchiwanie zdarzeń
socket.on('storage_event', (data) => {
    if (data.action === 'connected') {
        toast(t('storage.device_connected', { device: data.device }), 'info');
        refreshStorageList();
    }
});

socket.on('build_progress', (data) => {
    updateProgressBar(data.progress);
    appendLog(data.line);
});

socket.on('monitor_update', (data) => {
    updateCpuChart(data.cpu);
    updateMemoryBar(data.memory);
});

// Wysyłanie zdarzeń
socket.emit('terminal_open', {});
socket.emit('terminal_input', { data: command + '\n' });
socket.emit('terminal_resize', { cols: 120, rows: 40 });
```

### 4.5 System tłumaczeń — i18n via t()

```javascript
// Użycie w kodzie
const label = t('storage.mount_button');    // "Zamontuj" / "Mount"
const msg = t('backup.completed', {         // "Backup 'daily' zakończony"
    name: backupName
});

// Definicja w locales/pl.json
{
    "storage": {
        "mount_button": "Zamontuj",
        "unmount_button": "Odmontuj",
        "device_connected": "Podłączono urządzenie: {device}"
    },
    "backup": {
        "completed": "Backup '{name}' zakończony",
        "failed": "Backup '{name}' nie powiódł się: {error}"
    }
}

// Definicja w locales/en.json
{
    "storage": {
        "mount_button": "Mount",
        "unmount_button": "Unmount",
        "device_connected": "Device connected: {device}"
    }
}
```

### 4.6 Notyfikacje — toast()

```javascript
// Typy notyfikacji
toast('Operacja zakończona pomyślnie', 'success');
toast('Uwaga: dysk prawie pełny', 'warning');
toast('Błąd: nie można zamontować urządzenia', 'error');
toast('Pobieranie rozpoczęte...', 'info');
```

---

## 5. Standardy CSS

### 5.1 Zmienne CSS — System motywów

EthOS obsługuje motywy dark/light za pomocą CSS Custom Properties.
Wszystkie kolory **MUSZĄ** używać zmiennych — nigdy hardcoded wartości.

```css
/* Definicja zmiennych w :root (motyw domyślny — dark) */
:root {
    /* Tła */
    --bg-primary: #1a1a2e;
    --bg-secondary: #16213e;
    --bg-tertiary: #0f3460;
    --bg-card: #1a1a2e;

    /* Tekst */
    --text-primary: #e0e0e0;
    --text-secondary: #a0a0a0;
    --text-muted: #666666;

    /* Akcent */
    --accent: #e94560;
    --accent-hover: #ff6b81;
    --accent-muted: rgba(233, 69, 96, 0.2);

    /* Obramowania */
    --border: #2a2a4a;
    --border-light: #3a3a5a;

    /* Status */
    --success: #4caf50;
    --warning: #ff9800;
    --error: #f44336;
    --info: #2196f3;

    /* Layout */
    --sidebar-width: 250px;
    --toolbar-height: 48px;
    --border-radius: 8px;
    --transition: 0.2s ease;
}

/* Motyw jasny */
[data-theme="light"] {
    --bg-primary: #ffffff;
    --bg-secondary: #f5f5f5;
    --bg-tertiary: #e8e8e8;
    --bg-card: #ffffff;

    --text-primary: #333333;
    --text-secondary: #666666;
    --text-muted: #999999;

    --border: #e0e0e0;
    --border-light: #f0f0f0;
}
```

### 5.2 Używanie zmiennych

```css
/* ✅ POPRAWNIE — zawsze zmienne */
.fm-tree {
    background: var(--bg-secondary);
    color: var(--text-primary);
    border: 1px solid var(--border);
    border-radius: var(--border-radius);
    transition: background var(--transition);
}

.fm-tree-item:hover {
    background: var(--accent-muted);
}

.fm-tree-item--selected {
    background: var(--accent);
    color: white;
}

/* ❌ NIGDY — hardcoded kolory */
.fm-tree {
    background: #1a1a2e;
    color: #e0e0e0;
}
```

### 5.3 Prefiksy aplikacji

Każda aplikacja **MUSI** używać swojego prefiksu, aby style nie kolidowały
z innymi aplikacjami w tym samym SPA:

```css
/* ✅ POPRAWNIE — z prefiksem aplikacji */
.dl-card {
    background: var(--bg-card);
    padding: 12px;
    border-radius: var(--border-radius);
}

.dl-progress {
    height: 4px;
    background: var(--accent);
    transition: width 0.3s ease;
}

.dl-card-title {
    font-weight: 600;
    color: var(--text-primary);
}

/* ❌ NIGDY — generyczne klasy bez prefiksu */
.card { ... }
.progress { ... }
.title { ... }
```

### 5.4 Responsywność

```css
/* Mobile-first z breakpointami */
.fm-container {
    display: flex;
    flex-direction: column;
}

@media (min-width: 768px) {
    .fm-container {
        flex-direction: row;
    }
    .fm-sidebar {
        width: var(--sidebar-width);
    }
}

@media (min-width: 1200px) {
    .fm-container {
        max-width: 1400px;
    }
}
```

---

## 6. API Design Guidelines

### 6.1 Wzorzec URL

```
/api/{blueprint}/{action}
/api/{blueprint}/{resource}/{id}
/api/{blueprint}/{resource}/{id}/{sub_action}
```

**Przykłady:**
```
GET    /api/storage/list              — lista urządzeń
GET    /api/storage/device/sda        — szczegóły urządzenia
POST   /api/storage/mount             — zamontuj urządzenie
POST   /api/storage/unmount           — odmontuj urządzenie
GET    /api/docker/containers         — lista kontenerów
POST   /api/docker/containers/start   — uruchom kontener
POST   /api/docker/containers/stop    — zatrzymaj kontener
GET    /api/backup/list               — lista backupów
POST   /api/backup/create             — utwórz backup
DELETE /api/backup/delete             — usuń backup
GET    /api/monitor/stats             — statystyki systemu
```

### 6.2 Autentykacja

Każdy request (poza `/api/auth/login`) wymaga tokena:

```
Authorization: Bearer <64-char-hex-token>
```

**Przykład pełnego request:**
```bash
curl -X GET https://ethos-server:9000/api/storage/list \
    -H "Authorization: Bearer a3f8c9b2e1d4f6a8c0b2e4f6a8c0b2e4f6a8c0b2e4f6a8c0b2e4f6a8c0b2e4" \
    -H "Content-Type: application/json"
```

### 6.3 Format request body (POST/PUT)

```json
{
    "device": "/dev/sdb1",
    "mountpoint": "/mnt/data",
    "options": {
        "readonly": false,
        "fstype": "ext4"
    }
}
```

### 6.4 Format response

**Sukces (200):**
```json
{
    "ok": true,
    "message": "Device mounted successfully",
    "data": {
        "device": "/dev/sdb1",
        "mountpoint": "/mnt/data",
        "fstype": "ext4"
    }
}
```

**Błąd klienta (400/403/404):**
```json
{
    "error": "Device not found",
    "details": "No block device at /dev/sdb1"
}
```

**Błąd serwera (500):**
```json
{
    "error": "Mount failed: permission denied"
}
```

### 6.5 Kody HTTP

| Kod | Użycie w EthOS                              |
|-----|----------------------------------------------|
| 200 | Sukces (GET, POST, PUT, DELETE)              |
| 400 | Brakujące/nieprawidłowe parametry            |
| 401 | Brak tokena lub token wygasł                |
| 403 | safe_path() odmowa, brak uprawnień admin     |
| 404 | Zasób nie znaleziony                         |
| 500 | Błąd serwera, host_run() failure             |
| 504 | Timeout operacji (subprocess.TimeoutExpired) |

### 6.6 Paginacja

Dla endpointów zwracających listy:

```
GET /api/backup/list?page=1&per_page=20&sort=created_desc
```

```json
{
    "ok": true,
    "items": [...],
    "total": 142,
    "page": 1,
    "per_page": 20,
    "pages": 8
}
```

---

## 7. Dokumentacja Kodu

### 7.1 Docstringi Python

Każda publiczna funkcja w blueprintach **MUSI** mieć docstring:

```python
def create_raid(level: int, devices: list, name: str) -> bool:
    """
    Tworzy macierz RAID za pomocą mdadm.

    Args:
        level: Poziom RAID (0, 1, 5, 6, 10)
        devices: Lista ścieżek urządzeń ['/dev/sdb', '/dev/sdc']
        name: Nazwa macierzy (bez /dev/md/ prefix)

    Returns:
        True jeśli macierz została utworzona pomyślnie.

    Raises:
        subprocess.TimeoutExpired: Gdy operacja przekroczy 120s.

    Example:
        >>> create_raid(1, ['/dev/sdb', '/dev/sdc'], 'mirror0')
        True
    """
    device_str = ' '.join(q(d) for d in devices)
    cmd = f"mdadm --create /dev/md/{q(name)} --level={level} --raid-devices={len(devices)} {device_str}"
    result = host_run(cmd, timeout=120)
    return result.returncode == 0
```

### 7.2 Komentarze inline

**Kiedy komentować:**
- Nieoczywista logika biznesowa
- Workaroundy dla bugów w zależnościach
- Wyjaśnienie "dlaczego", nie "co"

```python
# ✅ Dobry komentarz — wyjaśnia "dlaczego"
# Gevent monkey-patch zmienia zachowanie subprocess —
# musimy jawnie ustawić bufsize=1 dla line-buffered output
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=1)

# ✅ Dobry komentarz — ostrzeżenie
# UWAGA: mdadm --create wymaga interaktywnego potwierdzenia,
# dlatego używamy --run aby je pominąć
cmd = f"mdadm --create --run /dev/md/{q(name)} ..."

# ❌ Zły komentarz — oczywisty
# Pobierz listę urządzeń
devices = get_devices()

# ❌ Zły komentarz — nie dodaje wartości
# Zwróć wynik
return result
```

### 7.3 README dla nowych modułów

Każdy nowy blueprint powinien mieć komentarz na początku pliku:

```python
"""
blueprints/my_module.py — Zarządzanie moim modułem

Endpointy:
    GET  /api/my_module/list        — lista zasobów
    POST /api/my_module/create      — tworzenie zasobu
    POST /api/my_module/delete      — usunięcie zasobu

Socket.IO:
    my_module_progress  (server → client)  — postęp operacji

Zależności:
    - host.py: host_run(), host_run_stream()
    - utils.py: safe_path()
    - Systemowe: some-system-tool (apt install some-tool)

Autor: Imię Nazwisko
Data: 2026-XX-XX
"""
```

---

## 8. Git Workflow

### 8.1 Nazewnictwo gałęzi

```
main                    — gałąź produkcyjna, stabilna
dev                     — gałąź rozwojowa
feature/{opis}          — nowa funkcjonalność
fix/{opis}              — naprawa błędu
hotfix/{opis}           — krytyczna naprawa na main
refactor/{opis}         — refaktoryzacja bez zmiany funkcjonalności
```

**Przykłady:**
```
feature/docker-compose-v2
feature/raid-monitoring
fix/usb-hotplug-crash
fix/safe-path-symlink-bypass
hotfix/auth-token-expiry
refactor/storage-blueprint-split
```

### 8.2 Format komunikatów commit

```
{typ}: {krótki opis} (max 72 znaki)

{opcjonalny dłuższy opis, jeśli potrzebny}

{opcjonalnie: Co-authored-by, Fixes #issue}
```

**Typy:**
```
feat:     Nowa funkcjonalność
fix:      Naprawa błędu
refactor: Refaktoryzacja (bez zmiany zachowania)
docs:     Dokumentacja
style:    Formatowanie (bez zmiany logiki)
test:     Testy
chore:    Zadania administracyjne (deps, CI, config)
perf:     Optymalizacja wydajności
security: Poprawka bezpieczeństwa
```

**Przykłady:**
```
feat: add NFS sharing support to storage blueprint
fix: resolve USB hotplug crash on rapid plug/unplug
refactor: split storage.py into mount and share modules
docs: update API documentation for docker endpoints
security: fix path traversal via symlink in safe_path()
chore: upgrade Flask to 3.1.0, update requirements.txt
```

### 8.3 .gitignore — Kluczowe wzorce

```gitignore
# Python
venv/
__pycache__/
*.pyc
*.pyo

# Runtime data
data/
logs/
uploads/

# OS
.DS_Store
Thumbs.db

# IDE
.vscode/
.idea/
*.swp
*.swo

# Environment
ethos.env
*.key
*.pem
*.crt
```

### 8.4 Procedura push

```bash
# 1. Upewnij się, że jesteś na właściwej gałęzi
git branch

# 2. Sprawdź status zmian
git status

# 3. Dodaj zmienione pliki
git add backend/blueprints/my_module.py
git add frontend/js/apps/my_app.js
git add frontend/css/apps.css

# 4. Commit z sensownym opisem
git commit -m "feat: add my_module blueprint with CRUD operations"

# 5. Push na remote (SSH key musi być skonfigurowany)
git push origin feature/my-module
```

---

## 9. Procedura Wdrożenia Zmian

### 9.1 Workflow krok po kroku

```
1. EDYCJA      → Zmodyfikuj pliki w /opt/ethos/
2. TEST LOCAL  → Sprawdź składnię Python: python3 -c "import py_compile; py_compile.compile('plik.py')"
3. RESTART     → sudo systemctl restart ethos
4. WERYFIKACJA → Sprawdź w przeglądarce lub curl
5. LOGI        → Sprawdź logi: tail -f /opt/ethos/logs/ethos.log
6. COMMIT      → git add, git commit, git push
```

### 9.2 Restart serwisu

```bash
# Restart serwisu EthOS
sudo systemctl restart ethos

# Sprawdzenie statusu
sudo systemctl status ethos

# Podgląd logów w czasie rzeczywistym
sudo journalctl -u ethos -f

# Alternatywnie — logi aplikacji
tail -f /opt/ethos/logs/ethos.log
```

### 9.3 Weryfikacja zmian

**Backend — curl:**
```bash
# Login (pobierz token)
TOKEN=$(curl -s -X POST https://localhost:9000/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"username":"admin","password":"..."}' \
    -k | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Test endpointu
curl -s https://localhost:9000/api/my_module/list \
    -H "Authorization: Bearer $TOKEN" \
    -k | python3 -m json.tool
```

**Frontend — przeglądarka:**
```
1. Otwórz https://<ip-serwera>:9000
2. Zaloguj się
3. Otwórz nową aplikację z menu
4. Sprawdź konsolę przeglądarki (F12) pod kątem błędów
5. Przetestuj wszystkie interakcje
```

### 9.4 Rollback awaryjny

```bash
# Jeśli coś poszło nie tak — cofnij ostatni commit
git log --oneline -5               # znajdź hash ostatniego dobrego commita
git revert HEAD                     # cofnij ostatni commit
sudo systemctl restart ethos       # restart z cofniętymi zmianami

# Lub przywróć konkretny plik
git checkout HEAD~1 -- backend/blueprints/my_module.py
sudo systemctl restart ethos
```

### 9.5 Checklist przed deployem

- [ ] Wszystkie zmienne użytkownika w `host_run()` przechodzą przez `q()`
- [ ] Ścieżki plików walidowane przez `safe_path()`
- [ ] Endpointy mają `@require_auth` (jeśli nie są publiczne)
- [ ] Odpowiedzi JSON w formacie `{ok: true}` / `{error: "..."}`
- [ ] Tłumaczenia dodane do `locales/pl.json` i `locales/en.json`
- [ ] CSS używa zmiennych (`var(--bg-primary)` itd.)
- [ ] CSS klasy mają prefiks aplikacji (`fm-`, `dl-`, `dkr-` itd.)
- [ ] Brak hardcoded sekretów, haseł, tokenów w kodzie
- [ ] Serwis restartuje się bez błędów (`systemctl status ethos`)
- [ ] Logi nie pokazują tracebacków ani warningów

### 9.6 Debugowanie na produkcji

```bash
# Pełny stack trace w logach
sudo journalctl -u ethos --since "5 min ago" --no-pager

# Interaktywne sprawdzenie modułu Python
cd /opt/ethos/backend
source ../venv/bin/activate
python3 -c "from blueprints.my_module import *; print('Import OK')"

# Test konkretnej funkcji host_run
python3 -c "
from host import host_run, q
r = host_run('lsblk -J')
print(r.returncode)
print(r.stdout[:200])
"

# Sprawdzenie czy port 9000 nasłuchuje
ss -tlnp | grep 9000

# Sprawdzenie procesów EthOS
ps aux | grep app.py
```

---

*Dokument wygenerowany na podstawie analizy kodu źródłowego EthOS v1.x.
Ostatnia aktualizacja: 2026-03-17.*

---

## 10. Przechowywanie Danych Aplikacji (Data Partition Rules)

EthOS działa na **4 GB partycji root (SquashFS A/B)**. Partycja zapełnia się natychmiast, jeśli aplikacje przechowują tam duże dane. Każda nowa aplikacja — corowa i opcjonalna — MUSI przestrzegać poniższych zasad.

### 10.1 Co należy gdzie trzymać

| Na partycji root ✅ | Na partycji danych (/mnt/data) ✅ |
|---------------------|-----------------------------------|
| Binaria apt (/usr, /lib) | Bazy wirusów / modeli AI/ML |
| Konfigi systemowe (/etc/*) | Bazy danych indeksów aplikacji |
| Pakiety pip (venv/) | Obrazy i kontenery Docker |
| Małe pliki stanu (JSON/SQLite) | Obrazy dysków VM |
| Jednostki systemd | Pobrane pliki medialne |
| | Pliki nagrań kamer |
| | Cache / indeks DLNA |

### 10.2 Funkcja `get_data_disk()` — wzorzec obowiązkowy

```python
from host import get_data_disk, data_path, q
import os

def _myapp_data_dir():
    """Zwraca katalog danych — preferuje /mnt/data, fallback do data_path."""
    dd = get_data_disk()      # zwraca '/mnt/data' lub ''
    if dd:
        p = os.path.join(dd, 'myapp')
        os.makedirs(p, exist_ok=True)
        return p
    return data_path('myapp') # fallback: /opt/ethos/data/myapp
```

**Kiedy używać `get_data_disk()` bezpośrednio:**
- Gdy potrzeba ścieżki poza podkatalogiem ethos (np. `/mnt/data/docker`, nie `/mnt/data/ethos/data/docker`)
- Dla aplikacji third-party z konfigurowalnymi katalogami danych

**Kiedy wystarczy `data_path()`:**
- Małe pliki konfiguracyjne, pliki stanu, bazy SQLite aplikacji
- `data/` jest symlinkiem do `/mnt/data/ethos/data/` na instalacjach z osobnym dyskiem danych

### 10.3 Przekierowanie katalogu danych aplikacji apt po instalacji

Gdy pakiet apt przechowuje duże dane w `/var/lib/<pkg>`, trzeba przekierować po instalacji:

```python
def _bg_install():
    r = apt_install('somepkg', timeout=300)
    if r.returncode != 0:
        return

    dd = get_data_disk()
    if dd:
        data_dir = os.path.join(dd, 'somepkg')
        os.makedirs(data_dir, exist_ok=True)
        host_run(f'chown -R somepkg:somepkg {q(data_dir)}', timeout=10)

        import re
        conf_path = '/etc/somepkg/somepkg.conf'
        if os.path.isfile(conf_path):
            txt = open(conf_path).read()
            txt = re.sub(r'^DataDirectory\s.*$', f'DataDirectory {data_dir}',
                         txt, flags=re.MULTILINE)
            if 'DataDirectory' not in txt:
                txt += f'\nDataDirectory {data_dir}\n'
            open(conf_path, 'w').write(txt)
```

### 10.4 Lista kontrolna dla nowej aplikacji

Przed napisaniem kodu blueprintu odpowiedz na te pytania:

1. **Czy aplikacja instaluje pakiety apt?**
   - Tylko binaria (np. `ffmpeg`, `mdadm`) → root OK
   - Pakiety z katalogami danych w `/var/lib/<pkg>` → **obowiązkowy redirect do `get_data_disk()`**

2. **Czy aplikacja pobiera duże pliki?** (modele, bazy wirusów, media, ISO)
   - Zawsze `data_path('myapp/...')` lub `get_data_disk()`

3. **Czy aplikacja uruchamia konteneryzowaną usługę?** (Docker, LXC)
   - Ustaw `data-root` / katalog roboczy na partycję danych

4. **Czy aplikacja zapisuje bazę danych / indeks?**
   - `data_path('myapp/myapp.db')` — nigdy `/var/lib/` ani `/tmp/`

5. **Czy aplikacja ma konfigurowalny katalog danych?**
   - Patch conf po instalacji na ścieżkę z `get_data_disk()`

### 10.5 Istniejące implementacje jako wzorzec

| Aplikacja | Plik | Wzorzec |
|-----------|------|---------|
| Docker Manager | `docker_manager.py` | `get_data_disk()` → `/mnt/data/docker` w `daemon.json` |
| ClamAV | `antivirus.py` | `_clamav_db_dir()` z fallbackiem; patch `freshclam.conf` + `clamd.conf` |
| MiniDLNA | `dlna.py` | `_minidlna_db_dir()` z fallbackiem; patch `minidlna.conf` |
| VM Manager | `vm_manager.py` | `_vm_root()` z `get_data_disk()` |
| AI Chat | `aichat.py` | modele w `data_path('models/')` |
| Photos AI | `photos_ai.py` | YOLO model w `data_path('models/photos_ai/')` |
| Surveillance | `surveillance.py` | nagrania w `data_path('surveillance/recordings/')` |
