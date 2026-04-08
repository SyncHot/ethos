# EthOS Builder — Dokumentacja Techniczna

> **Zakres dokumentu:** `backend/blueprints/builder.py` + `builder_spec.py` + `frontend/js/apps/builder.js`
> **Wersja kodu:** main, kwiecien 2026

---

> **WAZNA UWAGA dla nowych programistow**
> Builder to **builder obrazow systemowych**, nie modulu multimedialnego (wbrew opisowi w zadaniu).
> Produkuje:
> - `ethos-x86.img` — rozruchowy obraz dysku instalacyjnego EthOS (UEFI, GPT, SquashFS)
> - `ethos-VERSION.tar.gz` — paczki aktualizacyjne OTA
> - Publikuje opcjonalne aplikacje do repozytorium GitHub (Package Center)

---

## Spis tresci

1. [System Design i Workflow](#1-system-design-i-workflow)
2. [Logika Silnika (Engine Logic)](#2-logika-silnika-engine-logic)
3. [Interfejsy i Komunikacja (Contract)](#3-interfejsy-i-komunikacja-contract)
4. [Specyfikacja Techniczna (Stack)](#4-specyfikacja-techniczna-stack)
5. [Deployment i Utrzymanie (Ops)](#5-deployment-i-utrzymanie-ops)

---

## 1. System Design i Workflow

### 1.1 High-Level Overview

Builder jest **samowystarczalnym Flask Blueprint** uruchomionym w ramach procesu EthOS (Gunicorn + Gevent). Nie jest oddzielnym serwisem — wspoldziela jeden PID z glowna aplikacja, ale ciezkie operacje uruchamia na hoscie przez HAL.

```
+----------------------------------------------------------+
|                     EthOS Process                        |
|  +----------------------------------------------------+  |
|  |          Flask Blueprint /api/builder              |  |
|  |                                                    |  |
|  |  +--------------+    +--------------------------+  |  |
|  |  | Build State  |    |  _x86_wrapper_script()   |  |  |
|  |  | (in-memory   |    |  embedded bash ~1800 LOC |  |  |
|  |  | + JSON disk) |    +--------------------------+  |  |
|  |  +--------------+                |                  |  |
|  +------------------------------- - | -----------------+  |
|                                     | host_run_stream()    |
|                              +------v--------+            |
|                              |  HAL (host.py)|            |
|                              |  bash -c ...  |            |
|                              +------+--------+            |
+-----------------------------------  |  -------------------+
                                      |
                              +-------v--------+
                              |   Host OS      |
                              | debootstrap    |
                              | parted/mkfs    |
                              | mksquashfs     |
                              | veritysetup    |
                              | grub-install   |
                              +----------------+
```

**Granice systemu (boundaries):**

| Wewnetrzne | Zewnetrzne |
|---|---|
| Flask Blueprint — trasy HTTP | Host OS — shell przez HAL |
| Build State (in-memory + JSON) | Debootstrap mirror (debian/ubuntu) |
| Build Spec (YAML) | GitHub API (publish apps) |
| SSE stream | `/var/cache/ethos-builder/` (cache host) |
| Build History (JSON) | `/tmp/ethos-x86-build-web/` (tmpfs/disk) |

**Komunikacja z reszta systemu:**

- **Frontend -> Builder**: HTTP REST + SSE polling (nie WebSocket)
- **Builder -> Host**: `host_run(cmd)` / `host_run_stream(cmd)` — wrappery nad `subprocess` z HAL
- **Builder -> GitHub**: HTTPS REST API (GitHub Trees API) przez `_github_api()` helper
- **Builder -> App Manager**: import `blueprints.app_manager` w czasie wykonania — pobiera liste core/optional apps

### 1.2 Pipeline Architecture

#### Pipeline A: Budowanie Obrazu Dysku (`POST /api/builder/image`)

```
Browser           Flask               Background Thread     Host OS
   |                |                        |                 |
   +--POST /image-->|                        |                 |
   |                +--check debootstrap     |                 |
   |                +--_reset_build()        |                 |
   |                +--Thread.start()------->|                 |
   |<--200 ok-------+                        |                 |
   |                |                   _x86_wrapper_script()  |
   +--GET /status-->|                        +--host_run_stream-->
   |<--{percent}----+                        |  bash script    |
   |  (every 1-2s)  |                   parse STEP:/LOG:       |
   |                |                   _update_build()        |
   +--GET /status-->|                        |  ...completes   |
   |<--{done}-------+                        |                 |
```

**Kroki bash skryptu (`_x86_wrapper_script`):**

| Krok | Procent | Opis | Narzedzia |
|------|---------|------|-----------|
| 1 | 2-14 | Sprawdzenie zaleznosci + tworzenie obrazu dysku | `parted`, `mkfs.vfat`, `mkfs.ext4`, `losetup` |
| 2 | 15-44 | Debootstrap — minimalny install OS | `debootstrap` |
| 3 | 45-52 | Konfiguracja systemu | `chroot`, `locale-gen`, `useradd`, `systemctl` |
| 4 | 53-60 | Bootloader GRUB (UEFI) | `grub-install`, `grub-mkimage` |
| 5 | 61-75 | Instalacja zaleznosci (apt + pip) + firmware | `apt-get`, `pip install` |
| 6 | 76-85 | Inject EthOS (kod + venv + konfiguracje) | `cp -r`, `python3 -m venv` |
| 7 | 86-100 | Cleanup + SquashFS + dm-verity + finalizacja | `mksquashfs`, `veritysetup`, `resize2fs` |

#### Pipeline B: Budowanie Release (`POST /api/builder/release`)

Release dziala **synchronicznie w SSE generator** (blokuje watek Gevent), a nie w osobnym thread — build jest szybki (sekundy, nie minuty):

```
POST /release --> SSE generator --> bash script inline --> tar.gz archive
     |                |                    |
     |         version.json bump           |
     |                |             copy core files
     |                |             strip optional apps
     |                |             create tar.gz
     |                |             write latest.json
     +----------------+------------------------------------> done/error
```

---

## 2. Logika Silnika (Engine Logic)

### 2.1 Konfiguracja Srodowiskowa — Build Spec

Builder uzywa **deklaratywnej konfiguracji** (`data/build-spec.yaml`). Jesli plik nie istnieje — uzywane sa hardkodowane wartosci domyslne z `DEFAULT_SPEC` w `builder_spec.py`.

**Schemat `DEFAULT_SPEC`:**

```yaml
base:
  distro: debian          # 'debian' | 'ubuntu'
  release: bookworm       # Debian: bookworm|trixie; Ubuntu: noble|jammy
  arch: amd64
  img_size_gb: 8
  variant: minbase

identity:
  hostname: ethos
  brand_name: EthOS
  default_user: nasadmin
  default_password: ethos
  nas_port: 9000

partitions:
  esp_mb: 256
  root_mb: 4096
  root_type: ext4
  data_type: btrfs
  squashfs: true
  verity: true

packages:
  debootstrap:
    - systemd, systemd-sysv, linux-image-amd64, efibootmgr, ...
  apt_extra:
    - python3-pip, python3-venv, ufw, samba, ...
  pip:
    - flask==3.1.0, gevent==24.11.1, gunicorn, ...

services:
  enable: [systemd-networkd, ssh, fail2ban, ethos, ...]
  disable: [apt-daily.timer, man-db.timer, ...]

security:
  ssh_password_auth: true
  ufw_default_deny: true
  ufw_allow_ports: [9000, 22]
  fail2ban: true

build:
  use_tmpfs: true
  tmpfs_min_ram_mb: 10000
  cache_debootstrap: true
  cache_apt: true
  compression: zstd
  compression_level: 3
```

**Jak spec trafia do skryptu bash:**

Funkcja `spec_to_shell_vars()` w `builder_spec.py` konwertuje spec na zmienne shell, ktore sa wstrzykiwane na poczatku bash skryptu:

```bash
BASE_DISTRO="debian"
DEBIAN_RELEASE="bookworm"
IMG_SIZE_GB="8"
DEFAULT_HOSTNAME="ethos"
BRAND_NAME="EthOS"
NAS_PORT="9000"
# ... (wszystkie pola spec jako zmienne bash)
```

### 2.2 Adaptacja do Sprzetu

#### RAM — tmpfs vs disk

```bash
# Fragment _x86_wrapper_script (uproszczony):
TOTAL_RAM_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
if [ "$TOTAL_RAM_MB" -gt "$TMPFS_MIN_RAM_MB" ]; then
    mount -t tmpfs -o size=10G tmpfs "$WORK_DIR"
    # build calkowicie w RAM -- ~3x szybszy I/O
else
    mkdir -p "$WORK_DIR"   # build na dysku
fi
```

| Dostepna RAM | Zachowanie | Czas budowania |
|---|---|---|
| > 10 GB | tmpfs w RAM, kopia na dysk po zakonczeniu | ~15-20 min |
| < 10 GB | build bezposrednio na dysku | ~25-40 min |

#### Dystrybucja baza — Debian vs Ubuntu

Skrypt bash rozgalezia sie w 4 kluczowych miejscach:

| Roznica | Debian | Ubuntu |
|---|---|---|
| Keyring | `debian-archive-keyring.gpg` | `--no-check-gpg` (brak paczki ubuntu-keyring na Debian hoscie) |
| Mirror debootstrap | `http://deb.debian.org/debian` | `http://archive.ubuntu.com/ubuntu/` |
| Komponenty debootstrap | (domyslne: main) | `--components=main,restricted,universe` |
| Sources.list | `main contrib non-free non-free-firmware` | `main restricted universe multiverse` |
| GRUB packages | z `-backports` | bezposrednio z main |
| Firmware | `firmware-atheros`, `firmware-realtek`, `firmware-misc-nonfree` | `linux-firmware` |
| Kernel backports | tak (nowszy kernel) | nie (Ubuntu ships nowy kernel w main) |

#### I/O Scheduler — automatyczna adaptacja po instalacji

Obraz zawiera udev rules automatycznie dostosowujace scheduler I/O:

```
HDD (rotational=1) --> BFQ scheduler + readahead 4096 + hdparm spin-down
SSD (rotational=0) --> none scheduler + readahead 256
NVMe               --> none scheduler + readahead 256
```

### 2.3 Algorytmy Decyzyjne

#### Partycjonowanie obrazu

```
GPT Disk (img_size_gb GB)
+-- p1: ESP FAT32   (esp_mb MB, domyslnie 256 MB)
+-- p2: Root ext4   (root_mb MB, domyslnie 4096 MB)
    +-- SquashFS embedded:
        /opt/ethos/installer/images/ethos-root.sqsh

Reszta miejsca = niealokowana (installer rozciaga podczas instalacji na sprzet)
```

#### SquashFS vs Fallback

```
mksquashfs dostepne? --YES--> SquashFS zstd (preferowany)
                |
               NO
                |
                +--> dd | zstd --> ethos-root.img.zst (fallback)
```

#### dm-verity (opcjonalny)

```
veritysetup dostepne? --YES--> generuj hash tree
                             +-- ethos-root.sqsh.verity
                             +-- roothash --> /EFI/ethos/roothash (ESP)

                        NO --> skip (warning w logu)
```

dm-verity zapewnia integralnosc rootfs przy bootowaniu przez sprawdzenie hash tree przed mount.

#### Stripping Optional Apps

```python
# builder.py importuje app_manager na starcie:
_OPTIONAL_JS = _compute_optional_js()   # lista *.js optional apps
_OPTIONAL_PY = _compute_optional_py()   # lista *.py blueprintow optional apps
```

Pliki optional apps sa:
- **wykluczone** z release `.tar.gz` (nie pakowane)
- **usuniete** z base image `.img` po skopiowaniu (krok 6)
- **dostepne do pobrania** przez Package Center z GitHub

### 2.4 Zarzadzanie Zasobami

#### Mutex — tylko jeden build jednoczesnie

```python
_build_lock = threading.Lock()
# /release lub /image zwraca 409 Conflict jesli status == 'building'
```

#### Cykl zycia pamieci tymczasowej

```
BUILD START
  +-- [RAM > 10GB] mount tmpfs /tmp/ethos-x86-build-web/
  |
  +-- Katalog roboczy: /tmp/ethos-x86-build-web/
  |   +-- root/           <- debootstrap rootfs (~2.5 GB)
  |   +-- efi/
  |   +-- ethos-x86.img   <- obraz finalny (8 GB)
  |   +-- ethos-root.sqsh <- SquashFS tymczasowy (~600 MB)
  |   +-- ethos-root.sqsh.verity
  |
  +-- STEP 7 cleanup: apt clean, rm /tmp/* /var/tmp/* w chroot
  +-- rm ethos-root.sqsh (po inject do img)
  |
BUILD END
  +-- [tmpfs] cp img --> installer/images/ + umount tmpfs
  +-- [disk]  mv img --> installer/images/
  +-- rm -rf /tmp/ethos-x86-build-web/
```

Cleanup jest gwarantowany przez `trap cleanup EXIT` w bash skrypcie.

#### Build Cache (persistent)

| Cache dir | Zawartosc | Oszczednosc |
|---|---|---|
| `/var/cache/ethos-builder/debootstrap/` | paczki .deb przez debootstrap | ~3-5 min na rebuild |
| `/var/cache/ethos-builder/apt/` | apt cache (bind-mount do chroot) | ponowne uzycie paczek |

Cache nie jest czyszczony automatycznie. Endpoint `DELETE /api/builder/cache` czysci oba katalogi.

#### Crash Recovery

Stan buildu jest persystowany w `data/builder_state.json` po kazdej zmianie.

Przy restarcie procesu EthOS:
- `status == 'building'` + PID zyje: build kontynuowany normalnie
- `status == 'building'` + PID martwy: automatycznie zmieniony na `error: Build interrupted`

---

## 3. Interfejsy i Komunikacja (Contract)

### 3.1 API Endpoints

| Method | Endpoint | Opis |
|---|---|---|
| GET | `/api/builder/info` | Wersja, lista releases i images |
| GET | `/api/builder/status` | Aktualny stan buildu |
| POST | `/api/builder/cancel` | Przerwij aktywny build |
| POST | `/api/builder/dismiss` | Wyczysc wynik (done/error -> idle) |
| GET | `/api/builder/history` | Historia ostatnich 50 budow |
| POST | `/api/builder/history/clear` | Wyczysc historie |
| GET | `/api/builder/cache` | Rozmiar cache |
| DELETE | `/api/builder/cache` | Wyczysc cache |
| GET | `/api/builder/spec` | Pobierz aktualny build spec |
| PUT | `/api/builder/spec` | Zapisz build spec |
| DELETE | `/api/builder/spec` | Reset do domyslnych |
| GET | `/api/builder/spec/defaults` | Domyslny spec (read-only) |
| POST | `/api/builder/release` | Zbuduj release package (SSE) |
| POST | `/api/builder/image` | Zbuduj obraz dysku (background thread) |
| GET | `/api/builder/logs` | Logi z `logs/builder.log` |
| POST | `/api/builder/logs/clear` | Wyczysc logi |
| POST | `/api/builder/delete` | Usun plik release/image |
| GET | `/api/builder/download` | Pobierz plik (stream) |
| GET | `/api/builder/publish-config` | Config GitHub (token maskowany) |
| PUT | `/api/builder/publish-config` | Zapisz config GitHub |
| GET | `/api/builder/publish-diff` | Porownaj lokalne vs GitHub |
| POST | `/api/builder/publish-apps` | Opublikuj apki do GitHub (SSE) |

Wszystkie endpointy wymagaja `@require_auth`. Brak `@admin_required`.

### 3.2 Input Schema

#### `POST /api/builder/release`

```json
{
  "bump": "patch",
  "changelog_title": "Opcjonalny tytul",
  "changelog_changes": ["Naprawiono X", "Dodano Y"]
}
```

Pole `bump`: `"patch"` | `"minor"` | `"major"` | `""` (bez bump wersji)

#### `POST /api/builder/image`

Brak parametrow — konfiguracja pochodzi z build spec (`data/build-spec.yaml`).

#### `PUT /api/builder/spec` (czesciowa aktualizacja)

Tylko przeslane sekcje sa nadpisywane:

```json
{
  "base": {
    "distro": "debian",
    "release": "bookworm",
    "img_size_gb": 8
  },
  "identity": {
    "hostname": "myhostname",
    "brand_name": "MyNAS",
    "default_user": "admin",
    "nas_port": 9000
  },
  "partitions": {
    "esp_mb": 256,
    "squashfs": true,
    "verity": true
  },
  "build": {
    "compression_level": 3,
    "tmpfs_min_ram_mb": 10000
  },
  "security": {
    "ufw_default_deny": true,
    "fail2ban": true,
    "ssh_password_auth": true,
    "ufw_allow_ports": [9000, 22]
  }
}
```

### 3.3 Status Reporting

#### SSE Events (Release Build)

Format: `data: JSON\n\n` (Server-Sent Events, mimetype `text/event-stream`)

| `type` | Pola dodatkowe | Opis |
|---|---|---|
| `step` | `message`, `percent` (0-100) | Glowny krok pipeline |
| `log` | `message` | Szczegolowy log (nie zmienia paska postepu) |
| `done` | `success`, `message`, opcjonalnie `version`/`img` | Koniec buildu |

#### Polling `/api/builder/status` (Image Build)

```json
{
  "status": "building",
  "build_type": "image",
  "percent": 73,
  "message": "Injecting EthOS...",
  "logs": ["linia1", "linia2"],
  "log_total": 142,
  "elapsed": 847,
  "result": null
}
```

Parametr `?since=N` umozliwia inkrementalne pobieranie logow bez duplikatow (klient zapamietuje `log_total`).

### 3.4 Build State Machine

```
          dismiss()
  +----------------------------------+
  |                                  |
+--+--+  start()  +----------+  done  +------+
|idle |----------->| building |------->| done |
+-----+            +----------+        +------+
                        |                  |
                  error/cancel        dismiss()
                        |                  |
                        v                  |
                    +-------+              |
                    | error |<-------------+
                    +-------+
                        |
                    dismiss()
                        |
                       idle
```

Stan `done` i `error` wymagaja jawnego `POST /dismiss` przed nowym buildem.
Proba startu w stanie `building` zwraca **HTTP 409 Conflict**.

---

## 4. Specyfikacja Techniczna (Stack)

### 4.1 Technologie i Biblioteki

| Warstwa | Technologia | Rola |
|---|---|---|
| Serwer | Python 3.11 + Flask 3.1.0 + Gevent 24.11.1 | Blueprint HTTP |
| Wspolbieznosc | `threading.Thread` (image) + Gevent greenlet (release SSE) | Izolacja budow od HTTP |
| Mutex | `threading.Lock` | Ochrona `_build_state` |
| Serializacja | `json` stdlib | Stan, historia, wyniki |
| Konfiguracja | PyYAML (opcjonalne) | build-spec.yaml |
| Shell exec | `host_run()` / `host_run_stream()` z `backend/host.py` | Izolacja subprocess |
| GitHub API | `urllib.request` przez `_github_api()` | Publish apps |
| Frontend | Vanilla JS + SSE `fetch()` + polling | Builder UI |

### 4.2 Host Tools (wymagane na hoscie budujacym)

| Narzedzie | Pakiet apt | Rola | Wymagane |
|---|---|---|---|
| `debootstrap` | `debootstrap` | Minimalny install OS | TAK |
| `parted` | `parted` | Partycjonowanie obrazu | TAK |
| `mkfs.ext4` | `e2fsprogs` | Formatowanie root | TAK |
| `mkfs.vfat` | `dosfstools` | Formatowanie ESP | TAK |
| `grub-install` | `grub-efi-amd64-bin` | Instalacja bootloadera | TAK |
| `losetup` | `util-linux` | Loop device | TAK |
| `mksquashfs` | `squashfs-tools` | Tworzenie SquashFS | Zalecane |
| `veritysetup` | `cryptsetup-bin` | dm-verity hash tree | Opcjonalne |
| `zstd` | `zstd` | Kompresja + fallback | TAK |

Builder sprawdza obecnosc `debootstrap` przed startem przez `require_tools()`.
Brak narzedzia zwraca HTTP 500 z czytelnym komunikatem.

### 4.3 Sciezki Plikow

| Sciezka | Opis |
|---|---|
| `data/builder_state.json` | Aktualny stan buildu (crash recovery) |
| `data/build_history.json` | Historia ostatnich 50 budow |
| `data/build-spec.yaml` | Uzytkownikowe nadpisania spec (opcjonalne) |
| `data/builder_github.json` | Token + repo GitHub do publish apps |
| `logs/builder.log` | Pelny log wszystkich budow |
| `installer/releases/` | Paczki release + `latest.json` |
| `installer/images/ethos-x86.img` | Gotowy obraz dysku |
| `/var/cache/ethos-builder/debootstrap/` | Persistent cache debootstrap (host) |
| `/var/cache/ethos-builder/apt/` | Persistent cache apt (host) |
| `/tmp/ethos-x86-build-web/` | Katalog roboczy podczas buildu |

### 4.4 Struktura Obrazu Wynikowego

```
ethos-x86.img  (GPT, ~8 GB)
+-- p1: ESP FAT32 (256 MB)
|   +-- /EFI/BOOT/BOOTX64.EFI
|   +-- /EFI/BOOT/grub.cfg
|   +-- /EFI/BOOT/grubenv
|   +-- /EFI/ethos/roothash       <- dm-verity root hash
|   +-- /EFI/recovery/vmlinuz
|   +-- /EFI/recovery/initrd.img
|
+-- p2: ext4 "ethos-root" (4096 MB)
    +-- /boot/grub/grub.cfg
    +-- /opt/ethos/
        +-- backend/              <- kod EthOS (bez optional apps)
        +-- frontend/             <- UI (bez optional app JS)
        +-- installer/
        |   +-- images/
        |   |   +-- ethos-root.sqsh        <- SquashFS (primary)
        |   |   +-- ethos-root.sqsh.verity <- dm-verity hash tree
        |   |   +-- ethos-root.img.zst     <- dd+zstd (fallback)
        |   +-- preboot/                   <- Flask installer
        +-- ethos.env
        +-- install.conf
        +-- .installer-mode               <- marker: USB w trybie instalatora
```

### 4.5 Python f-string vs bash variables

Skrypt bash jest zwracany jako **Python f-string** przez `_x86_wrapper_script()`.

| W Python f-string | W wynikowym bash |
|---|---|
| `{PYTHON_VAR}` | wartosc zmiennej Python |
| `{{` | `{` (literalny nawias klamrowy) |
| `}}` | `}` |
| `$BASH_VAR` | `$BASH_VAR` (bez zmian) |
| literal bash: `${BASH_VAR}` | pisz jako `${{BASH_VAR}}` w f-stringu |

Przyklad:
```python
# W _x86_wrapper_script() (Python f-string):
f"echo {q(some_python_var)} > ${{ROOT}}/etc/hostname"
#       ^-- Python interpolacja   ^-- bash variable (zapisane jako ${{...}})
```

---

## 5. Deployment i Utrzymanie (Ops)

### 5.1 Zaleznosci Systemowe

#### Na hoscie budujacym

```bash
# Minimalne:
apt-get install -y debootstrap parted dosfstools e2fsprogs \
    grub-efi-amd64-bin grub-common squashfs-tools zstd \
    cryptsetup-bin mtools xorriso

# Dla Debian targets:
apt-get install -y debian-archive-keyring
```

Brakujace narzedzia sa **automatycznie instalowane** na poczatku bash skryptu budowania.

### 5.2 Logika Logowania

Builder uzywa **wlasnego Logger** oddzielonego od glownego Flask logger:

```python
_logger = logging.getLogger('builder')
_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler('logs/builder.log')
```

**Dwa strumienie logow:**

| Strumien | Lokalizacja | Zawartosc | Retencja |
|---|---|---|---|
| `logs/builder.log` | Dysk, persystentny | Wszystkie logi (INFO/ERROR) | weekly rotate, 4 kopie, max 50MB |
| `_build_state['logs']` | RAM + JSON disk | Ostatnie 500 linii aktywnego buildu | Czyszczone przy `_reset_build()` |

**Format linii logu w bash skrypcie:**

| Prefix | Obsluga w Python | Efekt w UI |
|---|---|---|
| `STEP:NN:message` | `_update_build(percent=NN, message=...)` | Aktualizacja paska postepu |
| `LOG:message` | `_update_build(log=...)` | Linia w logu |
| `RESULT_IMG:path:size` | parsowanie `result_info` | Wynik koncowy |
| `__EXIT_CODE__:N` | logika done/error | Finalizacja buildu |
| (inne) | `_update_build(log=line)` | Linia w logu |

### 5.3 Monitoring Wydajnosci

**Build History** (`GET /api/builder/history`):

```json
{
  "build_type": "image",
  "status": "done",
  "start_time": 1712500000,
  "end_time": 1712501800,
  "duration": 1800,
  "message": "Image ready! IMG: 8.0 GB (czas: 30min 0s)"
}
```

**Wbudowane metryki w logach:**
- Dostepna RAM (decyzja tmpfs vs disk)
- Zuzycie dysku w obrazie (per katalog)
- Rozmiar SquashFS i dm-verity hash tree
- Czas kazdego glownego kroku

### 5.4 Typowe Problemy i Rozwiazania

| Problem | Symptom | Rozwiazanie |
|---|---|---|
| `debootstrap` nie znaleziony | HTTP 500 przy `/image` | `apt-get install debootstrap` na hoscie |
| Build utkniety w `building` | Status nie zmienia sie | Crash recovery przy restarcie serwisu; lub `POST /dismiss` |
| Brak miejsca na dysku | Error w kroku SquashFS | Min. 20 GB wolnych na dysku hosta |
| Za malo RAM na tmpfs | Log: `not enough for tmpfs` | Normalny fallback na disk; obnizyc `tmpfs_min_ram_mb` w spec |
| Optional app w base image | App pojawia sie w swiezej instalacji | Sprawdz czy plik jest w `app_manager.OPTIONAL_APPS` |
| `ubuntu-keyring` nieznaleziony | Warning `--no-check-gpg` w logu | Oczekiwane dla Ubuntu builds na Debian hoscie — bezpieczne |

### 5.5 Modyfikacja Logiki Budowania — Przewodnik

#### Dodanie nowego pakietu do base image

```python
# Edytuj DEFAULT_SPEC w builder_spec.py:
'packages': {
    'apt_extra': [
        'nowy-pakiet',   # <-- dodaj tutaj
        ...
    ]
}
# LUB dodaj do data/build-spec.yaml bez edycji kodu
```

#### Zmiana konfiguracji systemowej w obrazie

Wszystkie konfiguracje (sysctl, udev, samba, NM, fail2ban, UFW, SSH) sa generowane **inline** w `_x86_wrapper_script()` jako heredoc.

Kluczowe lokalizacje w `backend/blueprints/builder.py`:

| Sekcja | Marker w kodzie | Linia approx. |
|---|---|---|
| I/O tuning (sysctl, udev) | `# -- I/O tuning for low-power NAS hardware --` | ~1011 |
| Locale / hosts / sources.list | po `echo "STEP:45:..."` | ~1064 |
| User creation + sudo | `echo "LOG:Creating user..."` | ~1077 |
| NetworkManager | `# -- NetworkManager Configuration --` | ~1230 |
| SSH hardening | `# -- SSH Hardening --` | ~1236 |
| UFW firewall | `# -- UFW Firewall --` | ~1221 |
| Fail2Ban | `# -- Fail2Ban Configuration --` | ~1173 |
| GRUB install | `# -- Step 4: GRUB --` | ~1326 |
| SquashFS initramfs hooks | `# -- SquashFS + OverlayFS initramfs hooks --` | ~1496 |
| Inject EthOS | `# -- Step 6: Inject EthOS --` | ~1635 |
| Python venv + pip | `# -- Python venv + environment file --` | ~1711 |
| ethos.service | `# -- ethos.service --` | ~1880 |
| Cleanup + SquashFS | `# -- Step 7: Cleanup & finalize --` | ~1963 |

#### Dodanie nowego kroku do pipeline

Kroki numerowane `STEP:NN:message` (0-100). Wolne sloty: 55, 56, 65, 70, 80.

```bash
echo "STEP:65:Moj nowy krok..."
# ... logika ...
echo "LOG:Szczegoly operacji..."
```

---

## Appendix A — Pelny schemat Image Build Pipeline

```
POST /api/builder/image
         |
         v
require_tools('debootstrap') --FAIL--> HTTP 500
         | OK
         v
status == 'building'? --YES--> HTTP 409
         | NO
         v
_reset_build('image') + threading.Thread.start()
HTTP 200 {status: 'ok'} (natychmiastowe)

=== Background Thread ===

_x86_wrapper_script() przez host_run_stream()
         |
         +-- [STEP 2-14]  check deps + create GPT image
         |                losetup + parted + mkfs.vfat + mkfs.ext4
         |
         +-- [STEP 15-44] debootstrap (Debian/Ubuntu)
         |                cache: /var/cache/ethos-builder/debootstrap/
         |
         +-- [STEP 45-52] chroot: locale, users, NM, SSH, fail2ban, UFW
         |
         +-- [STEP 53-60] chroot: grub-install (UEFI, --removable)
         |
         +-- [STEP 61-75] chroot: apt-extra + firmware + builder tools
         |                cache: /var/cache/ethos-builder/apt/ (bind-mount)
         |
         +-- [STEP 73-75] kernel upgrade (Debian: backports; Ubuntu: main)
         |
         +-- [STEP 76-85] inject EthOS: cp backend/ frontend/ tools/ installer/
         |                python3 -m venv + pip install requirements.txt
         |                ethos.service + firstboot.sh + install.conf
         |
         +-- [STEP 86]    cleanup: apt clean, rm /tmp/* w chroot, truncate machine-id
         |
         +-- [STEP 87]    mksquashfs (zstd-3) + veritysetup
         |                OR dd + zstd (fallback)
         |
         +-- [STEP 88]    inject SquashFS do p2 rootfs
         |
         +-- [STEP 90]    umount + losetup detach
         |                [tmpfs] cp --> installer/images/ + umount tmpfs
         |
         +-- [STEP 100]   RESULT_IMG:path:size
                          _update_build(status='done')
```

## Appendix B — Pelny schemat Release Build Pipeline

```
POST /api/builder/release {bump, changelog_title, changelog_changes}
         |
         v
SSE generator starts (blokuje greenlet Gevent)
         |
         +-- read installer/releases/version.json
         +-- bump version (patch/minor/major)
         +-- write updated version.json
         |
         +-- compute _OPTIONAL_JS / _OPTIONAL_PY lists
         |
         +-- bash inline script:
         |   mkdir /tmp/ethos-release-web-TIMESTAMP/
         |   cp backend/ (bez optional .py blueprintow)
         |   cp frontend/ (bez optional .js)
         |   find -name __pycache__ | xargs rm -rf
         |   tar -czf installer/releases/ethos-VERSION.tar.gz
         |   sha256sum + stat --> latest.json
         |   rm -rf /tmp/ethos-release-web-TIMESTAMP/
         |
         +-- SSE event: done {success: true, version: "X.Y.Z"}
```
