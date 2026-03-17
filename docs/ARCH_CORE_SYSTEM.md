# EthOS — Architektura Systemu Rdzeniowego

> Przygotowane przez: **Senior Linux Kernel & Backend Dev** | Model referencyjny: OpenAI Codex / DeepSeek Coder
> Data: 2026-03-17

---

## Spis Treści

1. [Przegląd Architektury Systemu](#1-przegląd-architektury-systemu)
2. [Warstwa Abstrakcji Sprzętowej (HAL)](#2-warstwa-abstrakcji-sprzętowej-hal)
3. [Zarządzanie Procesami i Współbieżność](#3-zarządzanie-procesami-i-współbieżność)
4. [Zarządzanie Kontenerami: Docker & LXC](#4-zarządzanie-kontenerami-docker--lxc)
5. [Komunikacja: REST API + WebSocket](#5-komunikacja-rest-api--websocket)
6. [System Przechowywania Danych](#6-system-przechowywania-danych)
7. [System Bezpieczeństwa i Izolacja](#7-system-bezpieczeństwa-i-izolacja)
8. [Mapowanie Modułów (37 Blueprintów)](#8-mapowanie-modułów-37-blueprintów)

---

## 1. Przegląd Architektury Systemu

EthOS to samodzielny system operacyjny typu home-server, zbudowany na stosie
**Python 3 + Flask 3.1.0 + Gevent 24.11.1**. Cała logika backendowa rezyduje
w katalogu `/opt/ethos/backend/`, a frontend to klasyczne **Vanilla JS SPA**
serwowane przez Flask jako pliki statyczne.

### Diagram warstw

```
┌─────────────────────────────────────────────────────────────┐
│                     FRONTEND (Vanilla JS SPA)               │
│   desktop.js · apps.js · setup.js · i18n · Socket.IO client │
├──────────────────────┬──────────────────────────────────────┤
│    REST API (JSON)   │   WebSocket (Socket.IO / Gevent)     │
├──────────────────────┴──────────────────────────────────────┤
│               Flask 3.1.0 + Flask-CORS 5.0.1                │
│          Flask-SocketIO 5.4.1 (async_mode='gevent')         │
├─────────────────────────────────────────────────────────────┤
│             37 Flask Blueprints (blueprints/)                │
│  storage · backup · downloads · docker_manager · builder …  │
├─────────────────────────────────────────────────────────────┤
│            host.py — Warstwa Abstrakcji Sprzętowej           │
│   host_run() · host_run_stream() · host_run_stream_raw()    │
│          q() · app_path() · data_path() · safe_path()       │
├─────────────────────────────────────────────────────────────┤
│            Biblioteki systemowe i narzędzia                  │
│  psutil · py-cpuinfo · GPUtil · pyudev · paramiko · scp     │
├─────────────────────────────────────────────────────────────┤
│            systemd (ethos.service) → start.sh                │
├─────────────────────────────────────────────────────────────┤
│               Jądro Linux (kernel ≥ 5.x)                    │
│    cgroups · namespaces · netfilter · block I/O · udev      │
├─────────────────────────────────────────────────────────────┤
│                    SPRZĘT (Hardware)                         │
│       CPU · RAM · dyski · GPU · NIC · USB · kamery          │
└─────────────────────────────────────────────────────────────┘
```

### Kluczowe stałe konfiguracyjne

| Stała              | Wartość domyślna         | Opis                              |
|---------------------|--------------------------|-----------------------------------|
| `ETHOS_ROOT`        | `/opt/ethos`             | Korzeń instalacji (lub env var)   |
| `DATA_DIR`          | `ETHOS_ROOT/data`        | Konfiguracje JSON, bazy SQLite    |
| `LOG_DIR`           | `ETHOS_ROOT/logs`        | Logi aplikacji                    |
| `NATIVE_MODE`       | `True`                   | Zawsze true — komendy via bash -c |

### Punkt wejścia

Serwer uruchamiany jest przez `start.sh`, który aktywuje virtualenv (`venv/`)
i wykonuje:

```bash
cd /opt/ethos/backend
source ../venv/bin/activate
python app.py
```

`app.py` (268KB, ~7122 linii) inicjalizuje Flask, rejestruje 37 blueprintów,
konfiguruje Socket.IO i nasłuchuje na porcie **9000** z automatycznym
przekierowaniem HTTP → HTTPS.

---

## 2. Warstwa Abstrakcji Sprzętowej (HAL)

Plik `backend/host.py` (21KB) stanowi **jedyny punkt styku** między logiką
aplikacyjną a systemem operacyjnym hosta. Żaden blueprint nie powinien
wywoływać `subprocess` bezpośrednio — zawsze przez funkcje z `host.py`.

### 2.1 host_run() — Wykonanie synchroniczne

```python
def host_run(cmd: str, timeout: int = 30, env: dict = None) -> subprocess.CompletedProcess:
    """
    Wykonuje komendę shell synchronicznie przez bash -c.
    Zwraca CompletedProcess z stdout/stderr jako str.
    """
    return subprocess.run(
        ['bash', '-c', cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})}
    )
```

**Dlaczego `bash -c`?** Ponieważ:
- Pozwala na pełną składnię bash (pipe `|`, `&&`, `||`, redirection `>`)
- Ujednolica środowisko wykonania (zawsze bash, nigdy sh/dash)
- `NATIVE_MODE = True` oznacza, że komendy zawsze lecą bezpośrednio
  na hoście (nie w kontenerze) — to kluczowa decyzja architektoniczna
  dla systemu, który **jest** systemem operacyjnym

**Wzorzec użycia w blueprintach:**

```python
from host import host_run, q

# Bezpieczne polecenie z cytowaniem argumentów
result = host_run(f"lsblk -J -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE {q(device)}")
if result.returncode == 0:
    data = json.loads(result.stdout)
    return jsonify(ok=True, devices=data['blockdevices'])
else:
    return jsonify(error=result.stderr.strip()), 500
```

### 2.2 host_run_stream() — Streaming linia po linii

```python
def host_run_stream(cmd: str, env: dict = None):
    """
    Generator — yield'uje linie stdout w czasie rzeczywistym.
    Używany do śledzenia postępu operacji (np. rsync, debootstrap).
    Kompatybilny z gevent dzięki monkey-patchowaniu.
    """
    proc = subprocess.Popen(
        ['bash', '-c', cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, **(env or {})}
    )
    for line in proc.stdout:
        yield line.rstrip('\n')
    proc.wait()
```

**Zastosowanie:** Blueprinty takie jak `builder.py` i `backup.py` wykorzystują
ten generator do wysyłania postępu przez Socket.IO:

```python
@socketio.on('build_start')
def handle_build(data):
    for line in host_run_stream(f"debootstrap --arch=amd64 bookworm {q(target)}"):
        emit('build_progress', {'line': line})
    emit('build_complete', {'ok': True})
```

### 2.3 host_run_stream_raw() — Surowe bajty

```python
def host_run_stream_raw(cmd: str):
    """
    Generator bajtów — dla operacji binarnych jak dd, gdzie
    parsowanie tekstu nie ma sensu, a potrzebujemy surowego
    stderr do odczytu postępu.
    """
    proc = subprocess.Popen(
        ['bash', '-c', cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    while True:
        chunk = proc.stderr.read(4096)
        if not chunk:
            break
        yield chunk
    proc.wait()
```

**Przypadek użycia:** Klonowanie dysków w `builder.py` i `installer.py` —
`dd if=/dev/sda of=/dev/sdb bs=4M status=progress` wysyła postęp na stderr.

### 2.4 q() — Cytowanie argumentów shell

```python
def q(s: str) -> str:
    """Wrapper na shlex.quote() — zapobiega shell injection."""
    return shlex.quote(str(s))
```

**Zasada:** Każda zmienna użytkownika wstawiana do komendy shell **MUSI**
przejść przez `q()`. Bez wyjątków.

```python
# ✅ POPRAWNIE
host_run(f"mount {q(device)} {q(mountpoint)}")

# ❌ NIGDY TAK — Shell injection!
host_run(f"mount {device} {mountpoint}")
```

### 2.5 Helpery ścieżek

```python
def app_path(*parts) -> str:
    """Ścieżka względem ETHOS_ROOT: app_path('data', 'config.json') → /opt/ethos/data/config.json"""
    return os.path.join(ETHOS_ROOT, *parts)

def data_path(*parts) -> str:
    """Ścieżka w DATA_DIR: data_path('users.json') → /opt/ethos/data/users.json"""
    return os.path.join(DATA_DIR, *parts)

def user_data_path(username, *parts) -> str:
    """Dane użytkownika: user_data_path('admin', 'config') → /opt/ethos/data/users/admin/config"""
    return os.path.join(DATA_DIR, 'users', username, *parts)
```

### 2.6 Integracja z bibliotekami sprzętowymi

| Biblioteka   | Wersja  | Zastosowanie                                       |
|--------------|---------|-----------------------------------------------------|
| `psutil`     | 6.1.1   | CPU, RAM, dyski, procesy, sieć — `monitor.py`      |
| `py-cpuinfo` | 9.0.0   | Szczegółowe info CPU (model, flagi, cache)          |
| `GPUtil`     | 1.4.0   | Monitoring GPU NVIDIA (temp, VRAM, load)            |
| `pyudev`     | 0.24.3  | Hotplug USB — `storage.py` nasłuchuje zdarzeń udev |
| `paramiko`   | 3.5.0   | Klient SSH do zdalnych operacji                     |
| `scp`        | 0.15.0  | Transfer plików via SCP (na bazie paramiko)         |

---

## 3. Zarządzanie Procesami i Współbieżność

### 3.1 Dlaczego Gevent, a nie threading/asyncio?

EthOS używa **Gevent 24.11.1** z kilku powodów:

1. **Monkey-patching** — Gevent podmienia standardowe moduły (`socket`,
   `subprocess`, `time`, `threading`) tak, by były nieblokujące. To oznacza,
   że `host_run()` z `subprocess.run()` **nie blokuje** event loop'a — Gevent
   automatycznie przełącza kontekst na inny greenlet, gdy Popen czeka na I/O.

2. **Flask-SocketIO** wymaga async backendu. Gevent to najstabilniejsza opcja
   dla Flask (alternatywa to eventlet, ale gevent jest szybszy i lepiej
   wspierany).

3. **gevent-websocket 0.10.1** zapewnia natywne WebSocket (nie long-polling),
   co jest krytyczne dla terminala, streamingu logów i aktualizacji w czasie
   rzeczywistym.

4. **Prostota** — Nie trzeba pisać `async/await` wszędzie. Zwykły synchroniczny
   Python z `subprocess` działa asynchronicznie dzięki monkey-patch.

### 3.2 Model greenletów w EthOS

```
┌──────────────────────────────────────────┐
│           Gevent Hub (event loop)        │
├──────────────────────────────────────────┤
│  Greenlet: Flask HTTP handler            │
│  Greenlet: Socket.IO event handler       │
│  Greenlet: USB hotplug monitor (pyudev)  │
│  Greenlet: Background task (builder)     │
│  Greenlet: Torrent download worker       │
│  Greenlet: Backup snapshot worker        │
│  Greenlet: Terminal PTY reader           │
└──────────────────────────────────────────┘
```

Każde żądanie HTTP i każde zdarzenie Socket.IO jest obsługiwane w oddzielnym
greenlecie. Greenlet jest lekki (~4KB stosu vs ~8MB dla wątku OS), więc system
może obsłużyć tysiące równoczesnych połączeń bez problemów z pamięcią.

### 3.3 Wzorzec workera w builder.py

`builder.py` (59KB) implementuje budowanie obrazów x86 za pomocą `debootstrap`
i konfiguracji GRUB. Proces budowania trwa 10-30 minut, więc wykorzystuje
dedykowany greenlet z raportowaniem postępu:

```python
def build_worker(build_id, config, sid):
    """Greenlet budujący obraz — uruchamiany przez socketio.start_background_task()"""
    try:
        emit_to = lambda event, data: socketio.emit(event, data, room=sid)
        emit_to('build_status', {'phase': 'debootstrap', 'progress': 0})

        for line in host_run_stream(f"debootstrap --arch=amd64 bookworm {q(rootfs_dir)}"):
            if 'Retrieving' in line:
                emit_to('build_progress', {'line': line, 'phase': 'packages'})

        emit_to('build_status', {'phase': 'grub', 'progress': 80})
        host_run(f"grub-install --root-directory={q(rootfs_dir)} {q(device)}")

        emit_to('build_complete', {'ok': True, 'build_id': build_id})
    except Exception as e:
        emit_to('build_error', {'error': str(e)})

# Uruchomienie workera
socketio.start_background_task(build_worker, build_id, config, request.sid)
```

### 3.4 Streaming w czasie rzeczywistym

Wzorzec streamingu jest używany przez wiele blueprintów:

| Blueprint        | Operacja             | Metoda streamingu      |
|------------------|----------------------|------------------------|
| `builder.py`     | Budowanie obrazu     | `host_run_stream()`    |
| `backup.py`      | rsync / snapshot     | `host_run_stream()`    |
| `installer.py`   | Instalacja systemu   | `host_run_stream()`    |
| `downloads.py`   | Pobieranie plików    | `host_run_stream_raw()`|
| `docker_manager` | Pull obrazu          | Docker API + emit      |
| `surveillance`   | Stream kamery        | Dedykowany stream      |

---

## 4. Zarządzanie Kontenerami: Docker & LXC

### 4.1 docker_manager.py — Przegląd

Blueprint `docker_manager.py` to centralny moduł zarządzania kontenerami Docker
i Docker Compose. Komunikuje się z Docker Engine przez socket `/var/run/docker.sock`
(biblioteka Docker SDK for Python lub bezpośrednie komendy `docker`/`docker compose`).

### 4.2 Kluczowe endpointy

```
GET  /api/docker/containers          — lista kontenerów (all=true/false)
POST /api/docker/containers/start    — uruchom kontener {id}
POST /api/docker/containers/stop     — zatrzymaj kontener {id}
POST /api/docker/containers/remove   — usuń kontener {id, force}
GET  /api/docker/images              — lista obrazów
POST /api/docker/images/pull         — pobierz obraz {image, tag}
GET  /api/docker/compose/projects    — lista projektów Compose
POST /api/docker/compose/up          — docker compose up {project_path}
POST /api/docker/compose/down        — docker compose down {project_path}
GET  /api/docker/stats               — statystyki kontenerów (CPU, RAM)
```

### 4.3 Integracja z CasaOS App Store

EthOS integruje się z rejestrem aplikacji CasaOS, pozwalając użytkownikom
instalować popularne self-hosted aplikacje jednym kliknięciem:

```
1. Frontend → GET /api/docker/appstore/list
2. Backend → pobiera index z CasaOS GitHub repo
3. Użytkownik wybiera aplikację (np. Nextcloud)
4. Frontend → POST /api/docker/appstore/install {app_id: "nextcloud"}
5. Backend:
   a) Generuje docker-compose.yml z szablonu
   b) host_run("docker compose -f {compose_file} up -d")
   c) Emituje postęp przez Socket.IO
6. Frontend aktualizuje listę zainstalowanych aplikacji
```

### 4.4 Backup wolumenów Docker

Blueprint `backup.py` (134KB) obsługuje backup wolumenów Docker:

```python
def backup_docker_volume(volume_name, backup_path):
    """Backup wolumenu Docker do archiwum tar.gz"""
    cmd = (
        f"docker run --rm "
        f"-v {q(volume_name)}:/source:ro "
        f"-v {q(os.path.dirname(backup_path))}:/backup "
        f"alpine tar czf /backup/{q(os.path.basename(backup_path))} -C /source ."
    )
    result = host_run(cmd, timeout=300)
    return result.returncode == 0
```

### 4.5 Orkiestracja cyklu życia

```
  PULL ──→ CREATE ──→ START ──→ RUNNING
                                    │
               REMOVED ←── STOPPED ←┘
```

---

## 5. Komunikacja: REST API + WebSocket

### 5.1 REST API — Wzorzec

Każdy blueprint rejestruje endpointy pod prefiksem `/api/{blueprint_name}/`:

```python
storage_bp = Blueprint('storage', __name__)

@storage_bp.route('/api/storage/list', methods=['GET'])
@require_auth
def storage_list():
    """Lista zamontowanych urządzeń"""
    result = host_run("lsblk -J -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,LABEL,UUID")
    if result.returncode != 0:
        return jsonify(error="Failed to list devices"), 500
    data = json.loads(result.stdout)
    return jsonify(ok=True, devices=data.get('blockdevices', []))
```

### 5.2 Format odpowiedzi

**Sukces:**
```json
{
    "ok": true,
    "devices": [],
    "message": "Operation completed"
}
```

**Błąd:**
```json
{
    "error": "Device not found",
    "details": "No block device at /dev/sdb1"
}
```

### 5.3 Autentykacja — Token Flow

```
1. POST /api/auth/login { "username": "admin", "password": "..." }
2. Backend → weryfikacja hasła (crypto_utils.py) → generacja tokena (64 hex chars)
3. Response: { "ok": true, "token": "a3f8c9..." }
4. Token przechowywany w pamięci: auth_tokens[token] = {user, expires, created}
5. Każdy request: Authorization: Bearer a3f8c9...
6. @require_auth sprawdza token, 7-dniowy expiry
```

**Implementacja dekoratora:**

```python
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token or token not in auth_tokens:
            return jsonify(error="Unauthorized"), 401
        token_data = auth_tokens[token]
        if datetime.now() > token_data['expires']:
            del auth_tokens[token]
            return jsonify(error="Token expired"), 401
        request.current_user = token_data['user']
        return f(*args, **kwargs)
    return decorated
```

### 5.4 Socket.IO — Zdarzenia czasu rzeczywistego

**Inicjalizacja:**
```python
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')
```

**Kluczowe zdarzenia:**

| Zdarzenie           | Kierunek        | Opis                                     |
|---------------------|-----------------|-------------------------------------------|
| `terminal_open`     | Client → Server | Otwarcie sesji terminala PTY              |
| `terminal_input`    | Client → Server | Wysłanie danych wejściowych do PTY        |
| `terminal_output`   | Server → Client | Dane wyjściowe z PTY                      |
| `terminal_resize`   | Client → Server | Zmiana rozmiaru terminala {cols, rows}     |
| `terminal_close`    | Client → Server | Zamknięcie sesji PTY                      |
| `build_progress`    | Server → Client | Postęp budowania obrazu                   |
| `build_complete`    | Server → Client | Zakończenie budowania                     |
| `download_progress` | Server → Client | Postęp pobierania                         |
| `backup_progress`   | Server → Client | Postęp backupu                            |
| `storage_event`     | Server → Client | Zdarzenie USB hotplug (podłączenie/odłącz)|
| `monitor_update`    | Server → Client | Aktualizacja zasobów systemu              |

### 5.5 Przykład pełnego flow — Terminal WebSocket

```
Client                          Server
  │                                │
  ├─ emit('terminal_open', {})  ──→│ → PTY fork (pty.openpty())
  │                                │ → Greenlet: odczyt z PTY
  │←── emit('terminal_output', d) ─┤
  │                                │
  ├─ emit('terminal_input', {      │
  │    data: 'ls -la\n'}) ───────→│ → os.write(master_fd, data)
  │                                │
  │←── emit('terminal_output', d) ─┤ → yield stdout z PTY
  │                                │
  ├─ emit('terminal_resize', {     │
  │    cols: 120, rows: 40}) ────→│ → fcntl.ioctl(TIOCSWINSZ)
  │                                │
  ├─ emit('terminal_close') ─────→│ → kill(pid), close(fd)
  │                                │
```

---

## 6. System Przechowywania Danych

### 6.1 storage.py — Największy moduł (103KB)

`storage.py` to największy blueprint pod względem złożoności, obsługujący:

- **Montowanie/odmontowywanie** dysków i partycji
- **USB Hotplug** — automatyczne wykrywanie podłączonych urządzeń
- **Udostępnianie sieciowe** — Samba, NFS, WebDAV, SFTP, DLNA
- **RAID** — tworzenie i zarządzanie macierzami (mdadm)
- **S.M.A.R.T.** — monitoring zdrowia dysków

### 6.2 USB Hotplug via pyudev

```python
import pyudev

def start_usb_monitor():
    """Uruchamiany jako background task przy starcie aplikacji"""
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem='block', device_type='partition')

    for device in iter(monitor.poll, None):
        if device.action == 'add':
            socketio.emit('storage_event', {
                'action': 'connected',
                'device': device.device_node,       # /dev/sdb1
                'label': device.get('ID_FS_LABEL', ''),
                'fstype': device.get('ID_FS_TYPE', ''),
                'size': device.attributes.get('size', 0)
            })
        elif device.action == 'remove':
            socketio.emit('storage_event', {
                'action': 'disconnected',
                'device': device.device_node
            })

# Uruchomienie monitora jako greenlet
socketio.start_background_task(start_usb_monitor)
```

### 6.3 Protokoły udostępniania

| Protokół | Implementacja                    | Port(y)     | Przypadek użycia        |
|----------|----------------------------------|-------------|--------------------------|
| Samba    | smbd/nmbd via smb.conf           | 445, 139    | Udziały Windows          |
| NFS      | nfs-kernel-server, /etc/exports  | 2049        | Udziały Linux/macOS      |
| WebDAV   | Wbudowany serwer lub Apache mod  | 8080        | Dostęp webowy            |
| SFTP     | OpenSSH sftp-server              | 22          | Bezpieczny transfer      |
| DLNA     | minidlna                         | 8200        | Streaming multimediów    |

**Wzorzec tworzenia udziału Samba:**

```python
@storage_bp.route('/api/storage/share/create', methods=['POST'])
@require_auth
def create_share():
    data = request.json
    share_name = data['name']
    share_path = data['path']

    if not safe_path(share_path):
        return jsonify(error="Path outside allowed scope"), 403

    smb_config = f"""
[{share_name}]
    path = {share_path}
    browseable = yes
    writable = {'yes' if data.get('writable') else 'no'}
    guest ok = {'yes' if data.get('public') else 'no'}
    create mask = 0664
    directory mask = 0775
"""
    host_run(f"echo {q(smb_config)} | sudo tee -a /etc/samba/smb.conf")
    host_run("sudo systemctl restart smbd")
    return jsonify(ok=True, message=f"Share '{share_name}' created")
```

### 6.4 RAID via mdadm

```python
def create_raid(level, devices, name):
    """Tworzenie macierzy RAID"""
    device_str = ' '.join(q(d) for d in devices)
    cmd = (
        f"mdadm --create /dev/md/{q(name)} "
        f"--level={q(str(level))} "
        f"--raid-devices={len(devices)} "
        f"{device_str}"
    )
    result = host_run(cmd, timeout=120)
    return result.returncode == 0
```

### 6.5 safe_path() — Walidacja ścieżek

Każda operacja na plikach użytkownika przechodzi przez `safe_path()`:

```python
ALLOWED_ROOTS = ('/home', '/media', '/run/media', '/mnt')

def safe_path(path: str) -> bool:
    """
    Sprawdza czy ścieżka jest w dozwolonym zakresie.
    Zapobiega path traversal (../../etc/passwd).
    """
    resolved = os.path.realpath(path)
    return any(resolved.startswith(root) for root in ALLOWED_ROOTS)
```

---

## 7. System Bezpieczeństwa i Izolacja

### 7.1 Wielowarstwowy model bezpieczeństwa

```
┌─────────────────────────────────────────────────┐
│  Warstwa 1: Autentykacja (token 64-hex, 7 dni)  │
├─────────────────────────────────────────────────┤
│  Warstwa 2: safe_path() — ALLOWED_ROOTS         │
├─────────────────────────────────────────────────┤
│  Warstwa 3: q() — shell injection prevention    │
├─────────────────────────────────────────────────┤
│  Warstwa 4: HTTPS + CORS                        │
├─────────────────────────────────────────────────┤
│  Warstwa 5: Izolacja użytkowników (/home/X)     │
├─────────────────────────────────────────────────┤
│  Warstwa 6: Linux permissions + sudo_mode       │
└─────────────────────────────────────────────────┘
```

### 7.2 Token Auth — Szczegóły

Tokeny przechowywane są **w pamięci** (dict), nie w bazie danych:

```python
auth_tokens = {}  # {token_string: {user, expires, created}}

def generate_token(username: str) -> str:
    token = secrets.token_hex(32)  # 64 znaki hex
    auth_tokens[token] = {
        'user': username,
        'expires': datetime.now() + timedelta(days=7),
        'created': datetime.now()
    }
    return token
```

**Konsekwencje:**
- Restart serwera = wylogowanie wszystkich użytkowników
- Brak persistencji = brak wycieku tokenów z pliku/bazy
- Prosty model = mniejsza powierzchnia ataku

### 7.3 safe_path() — Obrona przed Path Traversal

Kluczowa logika walidacji:

```python
ALLOWED_ROOTS = ('/home', '/media', '/run/media', '/mnt')

def safe_path(path: str) -> bool:
    """
    KRYTYCZNA FUNKCJA BEZPIECZEŃSTWA.
    1. Rozwiązuje symlinki przez os.path.realpath()
    2. Sprawdza czy wynikowa ścieżka zaczyna się od dozwolonego roota
    3. Blokuje: ../../etc/passwd, /etc/shadow, /opt/ethos/backend/...
    """
    try:
        resolved = os.path.realpath(os.path.expanduser(path))
        return any(resolved.startswith(root + '/') or resolved == root
                    for root in ALLOWED_ROOTS)
    except (ValueError, OSError):
        return False
```

**Przykłady:**
```
safe_path("/home/user/docs")             → True  (w /home)
safe_path("/media/usb/files")            → True  (w /media)
safe_path("/etc/passwd")                 → False (poza ALLOWED_ROOTS)
safe_path("/home/../etc/passwd")         → False (realpath → /etc/passwd)
safe_path("/home/user/../../etc/shadow") → False (realpath → /etc/shadow)
```

### 7.4 Izolacja użytkowników

Każdy użytkownik EthOS ma:
- Katalog domowy: `/home/{username}` — jedyny katalog z pełnym dostępem
- Dane aplikacji: `/opt/ethos/data/users/{username}/` — konfiguracja per-user
- Uprawnienia Linux: `chown {username}:{username} /home/{username}`

### 7.5 sudo_mode

Niektóre operacje wymagają uprawnień root (montowanie, RAID, sieć).
EthOS uruchamiany jest jako root (via systemd), więc `host_run()` ma pełne
uprawnienia. Blueprinty implementują dodatkowe sprawdzenia:

```python
@require_auth
def dangerous_operation():
    if not current_user_is_admin():
        return jsonify(error="Admin privileges required"), 403
    # ... operacja wymagająca root ...
```

### 7.6 HTTPS i CORS

- Automatyczne przekierowanie HTTP → HTTPS na porcie 9000
- Flask-CORS 5.0.1 z konfiguracją `origins="*"` (sieć lokalna)
- Certyfikaty SSL generowane automatycznie (self-signed) lub dostarczane
  przez użytkownika

---

## 8. Mapowanie Modułów (37 Blueprintów)

### 8.1 Kategoryzacja domen

#### 💾 Przechowywanie i Pliki
| Blueprint          | Rozmiar | Opis                                                |
|--------------------|---------|------------------------------------------------------|
| `storage.py`       | 103KB   | Montowanie, udostępnianie, USB hotplug, RAID         |
| `backup.py`        | 134KB   | Snapshoty, wersjonowanie, backup wolumenów Docker    |
| `filemanager.py`   | —       | Przeglądarka plików, operacje CRUD                   |

#### 🌐 Sieć i Pobieranie
| Blueprint          | Rozmiar | Opis                                                |
|--------------------|---------|------------------------------------------------------|
| `downloads.py`     | 98KB    | Torrenty, DDL, debrid (Real-Debrid, AllDebrid)       |
| `network.py`       | 20KB    | Konfiguracja sieci, DNS, DHCP                        |
| `vpn.py`           | —       | Zarządzanie WireGuard/OpenVPN                        |

#### 🐳 Kontenery i Wirtualizacja
| Blueprint          | Rozmiar | Opis                                                |
|--------------------|---------|------------------------------------------------------|
| `docker_manager.py`| —       | Docker, Compose, CasaOS app store                    |
| `vm_manager.py`    | 40KB    | Maszyny wirtualne (QEMU/KVM)                        |

#### 🔧 System
| Blueprint          | Rozmiar | Opis                                                |
|--------------------|---------|------------------------------------------------------|
| `monitor.py`       | 29KB    | Zasoby systemowe (CPU, RAM, dyski, GPU, temperatura) |
| `settings.py`      | 50KB    | Ustawienia systemu, aktualizacje, motywy             |
| `users.py`         | 14KB    | Zarządzanie użytkownikami, uprawnienia                |
| `installer.py`     | 49KB    | Instalacja systemu na dysk, wykrywanie RAID          |
| `builder.py`       | 59KB    | Budowanie obrazów x86 (debootstrap + GRUB)           |

#### 📹 Multimedia i IoT
| Blueprint          | Rozmiar | Opis                                                |
|--------------------|---------|------------------------------------------------------|
| `surveillance.py`  | 74KB    | Zarządzanie kamerami, nagrywanie, detekcja ruchu     |
| `media.py`         | —       | Streaming multimediów                                |

#### 🤖 AI i Automatyzacja
| Blueprint          | Rozmiar | Opis                                                |
|--------------------|---------|------------------------------------------------------|
| `rag_engine.py`    | —       | Silnik RAG (Retrieval-Augmented Generation)          |
| `model_library.py` | —       | Biblioteka modeli ML/AI                              |

#### 🔐 Bezpieczeństwo i Auth
| Blueprint          | Rozmiar | Opis                                                |
|--------------------|---------|------------------------------------------------------|
| `auth.py`          | —       | Endpointy logowania/wylogowania                      |
| `crypto_utils.py`  | —       | Hashowanie haseł (bcrypt/scrypt)                     |

### 8.2 Mapa zależności między blueprintami

```
                          ┌─────────────┐
                          │   auth.py   │
                          │  (tokeny)   │
                          └──────┬──────┘
                                 │ @require_auth
                 ┌───────────────┼───────────────┐
                 ▼               ▼               ▼
          ┌────────────┐  ┌──────────┐   ┌────────────┐
          │ storage.py │  │backup.py │   │docker_mgr  │
          └──────┬─────┘  └────┬─────┘   └──────┬─────┘
                 │             │                │
                 │      ┌──────┘                │
                 ▼      ▼                       ▼
          ┌──────────────────┐           ┌──────────┐
          │ host.py (HAL)    │           │ host.py  │
          │ host_run/stream  │           │          │
          │ q(), safe_path() │           │          │
          └──────────────────┘           └──────────┘
                 │                             │
                 ▼                             ▼
          ┌─────────────────────────────────────────┐
          │          Linux Kernel / systemd           │
          │   block devices · cgroups · namespaces    │
          └─────────────────────────────────────────┘
```

### 8.3 Wspólne zależności

Wszystkie blueprinty zależą od:
1. **`host.py`** — abstrakcja systemu operacyjnego
2. **`utils.py`** — `safe_path()`, helpery formatowania, logowanie
3. **`auth` / `@require_auth`** — autentykacja tokenowa
4. **Flask `request`/`jsonify`** — framework HTTP
5. **Socket.IO `emit`** — komunikacja real-time

---

*Dokument wygenerowany na podstawie analizy kodu źródłowego EthOS v1.x.
Ostatnia aktualizacja: 2026-03-17.*
