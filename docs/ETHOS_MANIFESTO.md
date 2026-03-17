# ETHOS MANIFESTO
## Filozofia i Wizja Systemu EthOS NAS OS

> Przygotowane przez: **Główny Architekt & Strateg** | Model referencyjny: Claude Opus / GPT-4o  
> Data: 2026-03-17  
> Wersja dokumentu: 1.0  
> Status: Dokument Fundacyjny — Obowiązujący

---

> *„Dane użytkownika są święte. Każda linia kodu, każda decyzja architektoniczna,*  
> *każdy kompromis musi być rozpatrywany przez pryzmat jednego pytania:*  
> ***czy to chroni integralność danych?"***
>
> — Zasada Zerowa EthOS

---

## Spis treści

1. [Dlaczego budujemy EthOS?](#1-dlaczego-budujemy-ethos)
2. [Fundamenty Bezpieczeństwa Danych](#2-fundamenty-bezpieczeństwa-danych)
3. [Wybór Platformy: Debian + Natywna Warstwa Systemowa](#3-wybór-platformy-debian--natywna-warstwa-systemowa)
4. [System Plików i Strategia Przechowywania](#4-system-plików-i-strategia-przechowywania)
5. [Zasada "Data Integrity over Everything"](#5-zasada-data-integrity-over-everything)
6. [Architektura Modułowa: 37 Blueprintów](#6-architektura-modułowa-37-blueprintów)
7. [Wizja Długoterminowa](#7-wizja-długoterminowa)
8. [Etyka Systemowa](#8-etyka-systemowa)

---

## 1. Dlaczego budujemy EthOS?

### Manifest Niezależności Cyfrowej

Żyjemy w erze, w której korporacje traktują dane użytkowników jako surowiec. Każde
zdjęcie rodzinne przesłane do chmury, każdy dokument zapisany na cudzym serwerze,
każda kopia zapasowa powierzona zewnętrznemu dostawcy — to akt zaufania, który
coraz częściej bywa nadużywany. Ceny subskrypcji rosną, warunki usług zmieniają
się jednostronnie, a dane stają się zakładnikami vendor lock-in.

**EthOS powstał jako odpowiedź na fundamentalne pytanie**: czy zwykły użytkownik
może posiadać system NAS klasy enterprise, nie płacąc podatku od zamkniętego
ekosystemu?

Synology DSM, QNAP QTS, TrueNAS SCALE — każdy z tych systemów oferuje wartość,
ale każdy narzuca kompromisy. Synology wiąże hardware z software'em. QNAP zamyka
kluczowe funkcje za paywallem. TrueNAS wymaga wiedzy eksperckiej z zakresu ZFS.
Żaden z nich nie daje użytkownikowi pełnej kontroli bez ukrytych kosztów.

### Dowód Kompleksowości: 37 Modułów

EthOS to nie prototyp i nie proof-of-concept. To **37 w pełni działających modułów
Flask Blueprint**, obejmujących:

```
┌─────────────────────────────────────────────────────────────────┐
│                    EthOS Module Landscape                       │
├──────────────────┬──────────────────┬───────────────────────────┤
│  STORAGE         │  NETWORK         │  SYSTEM                   │
│  ├─ storage.py   │  ├─ samba.py     │  ├─ system_info.py        │
│  ├─ backup.py    │  ├─ nfs.py       │  ├─ services.py           │
│  ├─ raid.py      │  ├─ webdav.py    │  ├─ users.py              │
│  └─ usb.py       │  ├─ sftp.py      │  ├─ ssh.py                │
│                  │  └─ network.py   │  └─ logs.py               │
├──────────────────┼──────────────────┼───────────────────────────┤
│  MEDIA           │  VIRTUALIZATION  │  AI & MONITORING          │
│  ├─ media.py     │  ├─ docker.py    │  ├─ ai_chat.py            │
│  ├─ gallery.py   │  ├─ compose.py   │  ├─ surveillance.py       │
│  └─ downloads.py │  └─ vm.py        │  └─ notifications.py      │
├──────────────────┼──────────────────┼───────────────────────────┤
│  HARDWARE        │  APPS            │  ADMIN                    │
│  ├─ printers.py  │  ├─ app_store.py │  ├─ settings.py           │
│  ├─ ups.py       │  ├─ plugins.py   │  ├─ auth.py               │
│  └─ hw_monitor.py│  └─ casaos.py    │  └─ image_builder.py      │
└──────────────────┴──────────────────┴───────────────────────────┘
```

Każdy z tych modułów to niezależny Blueprint z własnym routingiem, logiką
biznesową i izolacją błędów. To nie monolit — to **mikroserwisowa architektura
osadzona w jednym procesie**, łącząca elastyczność z prostotą deploymentu.

### Ruch Self-Hosting i Suwerenność Danych

EthOS wpisuje się w globalny ruch **self-hosting**, który zyskuje na sile:

- **Suwerenność danych** — Twoje dane, Twój serwer, Twoje zasady
- **Prywatność by design** — zero telemetrii, zero phone-home
- **Niezależność od chmury** — pełna funkcjonalność offline
- **Transparentność** — otwarty kod, audytowalny przez każdego
- **Ekonomia** — jednorazowy koszt hardware vs. wieczna subskrypcja

---

## 2. Fundamenty Bezpieczeństwa Danych

### Architektura Bezpieczeństwa: Defense in Depth

Bezpieczeństwo EthOS nie polega na jednym mechanizmie — to wielowarstwowy
system ochrony, gdzie każda warstwa działa niezależnie:

```
┌─────────────────────────────────────────────────────────────┐
│                    WARSTWA 1: SIEĆ                          │
│            HTTPS (port 443) + TLS enforcement               │
│            Firewall rules + Port isolation                   │
├─────────────────────────────────────────────────────────────┤
│                    WARSTWA 2: UWIERZYTELNIANIE              │
│            Token-based auth (64-char hex)                    │
│            7-day expiry + in-memory token store              │
│            Session isolation per user                        │
├─────────────────────────────────────────────────────────────┤
│                    WARSTWA 3: AUTORYZACJA                   │
│            ALLOWED_ROOTS path restriction                    │
│            Home directory isolation per user                 │
│            sudo_mode separation for privileged ops           │
├─────────────────────────────────────────────────────────────┤
│                    WARSTWA 4: WALIDACJA ŚCIEŻEK             │
│            safe_path() — kanoniczny resolver ścieżek        │
│            Blokada path traversal (../, symlink escape)      │
│            Whitelist-based directory access                  │
├─────────────────────────────────────────────────────────────┤
│                    WARSTWA 5: IZOLACJA PROCESÓW             │
│            host.py: bash -c wrapper z kontrolą komend       │
│            Rozdzielenie operacji user/root                   │
│            Audyt log każdej operacji systemowej              │
└─────────────────────────────────────────────────────────────┘
```

### Funkcja `safe_path()` — Strażnik Systemu Plików

Centralna funkcja bezpieczeństwa EthOS to `safe_path()`. Każde odwołanie
do systemu plików — bez wyjątku — przechodzi przez tę walidację:

```python
# Pseudokod logiki safe_path()
def safe_path(requested_path, allowed_roots=ALLOWED_ROOTS):
    canonical = os.path.realpath(requested_path)  # Rozwiąż symlinki
    for root in allowed_roots:
        if canonical.startswith(root):
            return canonical  # Ścieżka dozwolona
    raise SecurityError("Path outside allowed roots")
```

**Zasady `safe_path()`:**
- Każda ścieżka jest kanonizowana przez `os.path.realpath()`
- Symlinki są rozwiązywane PRZED sprawdzeniem uprawnień
- Lista `ALLOWED_ROOTS` definiuje jawnie dozwolone katalogi
- Próba wyjścia poza dozwolony zakres = natychmiastowy błąd bezpieczeństwa
- Katalogi domowe użytkowników są izolowane — user A nie widzi danych user B

### Mechanizm Tokenów Autoryzacyjnych

System autoryzacji EthOS opiera się na tokenach kryptograficznych:

| Cecha                  | Implementacja                           |
|------------------------|-----------------------------------------|
| Format tokenu          | 64-znakowy ciąg hexadecymalny           |
| Generacja              | `secrets.token_hex(32)`                 |
| Przechowywanie         | In-memory dictionary (nie na dysku)     |
| Czas życia             | 7 dni od ostatniego użycia              |
| Transmisja             | Header `Authorization: Bearer <token>`  |
| Odświeżanie            | Automatyczne przy aktywnym użyciu       |
| Unieważnienie          | Natychmiastowe przy wylogowaniu         |

**Dlaczego in-memory, a nie baza danych?** Świadomy wybór: restart serwisu =
unieważnienie wszystkich sesji. To feature, nie bug. Wymusza re-autentykację
po każdej aktualizacji systemu, eliminując ryzyko przejęcia starych tokenów.

### Separacja `sudo_mode`

Operacje systemowe w EthOS dzielą się na dwie kategorie:

1. **Operacje użytkownika** — odczyt plików, przeglądanie galerii, konfiguracja
   ustawień osobistych. Wykonywane z uprawnieniami procesu EthOS.

2. **Operacje uprzywilejowane** — montowanie dysków, zarządzanie RAID, instalacja
   pakietów, konfiguracja sieci. Wymagają jawnego przejścia w `sudo_mode`.

Przejście do `sudo_mode` jest logowane, ograniczone czasowo i wymaga
ponownego potwierdzenia tożsamości. Żadna operacja root-level nie może
zostać wykonana „po cichu".

---

## 3. Wybór Platformy: Debian + Natywna Warstwa Systemowa

### Dlaczego Debian?

Wybór Debiana jako bazy EthOS nie był przypadkowy. To decyzja oparta na
dekadach doświadczeń branży:

| Kryterium              | Debian                 | Ubuntu         | Alpine         |
|------------------------|------------------------|----------------|----------------|
| Stabilność             | ★★★★★                  | ★★★★☆          | ★★★☆☆          |
| Cykl wsparcia          | 5+ lat (LTS)           | 5 lat (LTS)    | 2 lata         |
| Ekosystem pakietów     | 59 000+ pakietów       | Fork Debiana   | Ograniczony    |
| Systemd integration    | Natywny                | Natywny        | Brak (OpenRC)  |
| Rozmiar community      | Największe             | Duże           | Rosnące        |
| Podejście do stabilności| Conservative           | Mixed          | Rolling-ish    |

**Debian daje EthOS to, czego NAS potrzebuje najbardziej: przewidywalność.**
Aktualizacja pakietów nie powinna nigdy zepsuć systemu przechowywania danych.

### Integracja z systemd

EthOS działa jako usługa systemd (`ethos.service`), co zapewnia:

```ini
# /etc/systemd/system/ethos.service
[Unit]
Description=EthOS NAS Operating System
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/ethos/host.py
Restart=always
RestartSec=5
User=ethos

[Install]
WantedBy=multi-user.target
```

- **Automatyczny restart** po awarii (Restart=always)
- **Zarządzanie zależnościami** — start po sieci
- **Journald logging** — pełna historia w `journalctl -u ethos`
- **Kontrola zasobów** — opcjonalne cgroups limits

### Natywna Warstwa Wykonawcza: `host.py`

Kluczowa decyzja architektoniczna EthOS: **natywne wykonanie na hoście,
nie w kontenerze**.

```
┌─────────────────────────────────────────────────────┐
│                   EthOS Process                      │
│                                                      │
│  Flask App (port 9000)                               │
│    │                                                 │
│    ├─── Blueprint A ──→ host.py ──→ bash -c "cmd"   │
│    ├─── Blueprint B ──→ host.py ──→ bash -c "cmd"   │
│    └─── Blueprint C ──→ host.py ──→ bash -c "cmd"   │
│                          │                           │
│                          ▼                           │
│                    Linux Kernel                       │
│              (bezpośredni dostęp do hardware)         │
└─────────────────────────────────────────────────────┘
```

**`host.py`** to warstwa abstrakcji systemowej — wrapper, który:
- Przyjmuje komendy od blueprintów Flask
- Wykonuje je przez `bash -c` z odpowiednimi uprawnieniami
- Loguje każdą operację systemową
- Sanityzuje argumenty przed przekazaniem do shella
- Zapewnia timeout dla długotrwałych operacji

**Dlaczego natywnie, a nie w kontenerze?**

System NAS musi mieć bezpośredni dostęp do:
- Urządzeń blokowych (`/dev/sd*`) dla zarządzania dyskami
- Podsystemu udev dla detekcji USB hotplug
- Stosu sieciowego dla konfiguracji Samba/NFS
- mdadm dla zarządzania RAID
- CUPS dla drukarek
- libvirt/KVM dla maszyn wirtualnych

Konteneryzacja core OS wprowadziłaby warstwę abstrakcji, która **szkodzi
niezawodności** systemu zarządzającego sprzętem. Docker jest w EthOS
narzędziem *zarządzanym*, nie narzędziem *zarządzającym*.

### Dlaczego x86_64 Only?

Decyzja o porzuceniu wsparcia dla ARM/Raspberry Pi była trudna, ale
uzasadniona:

1. **Wydajność I/O** — x86_64 oferuje pełną przepustowość SATA/NVMe
2. **Niezawodność ECC** — platformy x86 wspierają pamięć ECC
3. **Wirtualizacja** — pełne wsparcie KVM/VT-x dla VM management
4. **Enterprise hardware** — kompatybilność z kontrolerami RAID HW
5. **Prostota testowania** — jeden target = mniej bugów

Nie zamykamy drogi dla ARM w przyszłości, ale priorytetem jest solidność
na platformie, gdzie dane użytkowników są najcenniejsze.

---

## 4. System Plików i Strategia Przechowywania

### Obecny Stan: Uniwersalne Wsparcie Systemów Plików

EthOS w obecnej wersji implementuje **generyczne podejście do mount**,
wspierając dowolny system plików rozpoznawany przez jądro Linux:

```
Wspierane systemy plików:
├── ext4          — domyślny, najstabilniejszy
├── XFS           — wydajny dla dużych plików
├── NTFS (ntfs3)  — kompatybilność z Windows
├── FAT32/exFAT   — USB drives, karty SD
├── BTRFS         — opcjonalnie, z snapshot support
└── ZFS (FUSE)    — opcjonalnie, experimental
```

### `storage.py` — Serce Systemu (103 KB kodu)

Moduł `storage.py` to **największy pojedynczy blueprint EthOS** (103 KB),
co odzwierciedla fundamentalne założenie: **przechowywanie danych jest
najważniejszą funkcją NAS**.

Zakres odpowiedzialności `storage.py`:

```
storage.py (103 KB)
├── Wykrywanie urządzeń blokowych
│   ├── lsblk parsing
│   ├── /dev/disk/by-id mapping
│   └── SMART health monitoring
├── Operacje mount/unmount
│   ├── Automatyczne wykrywanie filesystem type
│   ├── Mount options per filesystem
│   └── /etc/fstab management
├── USB Hotplug (pyudev)
│   ├── Detekcja podłączenia urządzenia
│   ├── Automatyczny mount (konfigurowalna polityka)
│   ├── Safe eject z sync flush
│   └── Socket.IO powiadomienie do UI
├── Zarządzanie przestrzenią
│   ├── Disk usage monitoring
│   ├── Quota management
│   └── Alerting (próg zapełnienia)
└── RAID Integration
    ├── mdadm array detection
    ├── Status monitoring
    ├── Degraded array alerts
    └── Rebuild progress tracking
```

### `backup.py` — Druga Linia Obrony (134 KB kodu)

Moduł backupu (134 KB) to **największy moduł w całym systemie**, co
potwierdza zasadę „Data Integrity over Everything":

```
backup.py (134 KB)
├── Snapshot Engine
│   ├── Tworzenie migawek stanu danych
│   ├── Wersjonowanie z retention policy
│   ├── Inkrementalne snapshoty (delta)
│   └── Weryfikacja integralności po backup
├── Destinations
│   ├── Lokalny dysk (inny volume)
│   ├── Zewnętrzny USB drive
│   ├── Zdalny serwer (rsync over SSH)
│   └── Kolejny EthOS node (peer backup)
├── Scheduling
│   ├── Cron-based harmonogram
│   ├── Event-triggered backups
│   └── Priority queue przy wielu zadaniach
├── Restore Engine
│   ├── Point-in-time restore
│   ├── Granularny restore (plik/folder)
│   ├── Bare-metal restore (image builder)
│   └── Weryfikacja integralności po restore
└── Monitoring
    ├── Backup health dashboard
    ├── Failure alerting
    ├── Space usage projection
    └── Retention policy enforcement
```

### Strategia RAID: mdadm jako Fundament

EthOS wykorzystuje programowy RAID przez **mdadm**, oferując:

| Poziom RAID | Opis                            | Min. dysków | Redundancja |
|-------------|----------------------------------|-------------|-------------|
| RAID 0      | Striping (wydajność)             | 2           | Brak        |
| RAID 1      | Mirror (bezpieczeństwo)          | 2           | 1 dysk      |
| RAID 5      | Striping + parzystość            | 3           | 1 dysk      |
| RAID 6      | Podwójna parzystość              | 4           | 2 dyski     |
| RAID 10     | Mirror + stripe                  | 4           | 1 na parę   |

**Decyzja o mdadm vs hardware RAID:** Programowy RAID daje pełną
transparentność. Administrator widzi dokładnie, co się dzieje. Hardware
RAID to czarna skrzynka — awaria kontrolera = utrata dostępu do danych.

### Wizja Przyszłości: ZFS/BTRFS Native

Roadmapa przechowywania danych w EthOS:

```
Faza 1 (obecna):  ext4 + mdadm RAID          ✅ Zrealizowane
Faza 2 (Q3 2026): BTRFS z natywnym RAID       🔄 W planach
Faza 3 (Q1 2027): ZFS native (nie FUSE)       📋 Zaplanowane
Faza 4 (Q3 2027): Hybrid storage pools        📋 Zaplanowane
```

Dlaczego ZFS jest celem:
- **Checksumming** — wykrywanie silent data corruption (bit rot)
- **Copy-on-Write** — atomowe operacje zapisu
- **Native snapshots** — migawki bez dodatkowego kosztu I/O
- **Self-healing** — automatyczna naprawa z redundantnych kopii
- **Compression** — transparentna kompresja LZ4/ZSTD
- **Deduplication** — oszczędność przestrzeni na poziomie bloków

---

## 5. Zasada "Data Integrity over Everything"

### Definicja Zasady

**"Data Integrity over Everything"** to naczelna zasada projektowa EthOS.
Oznacza ona, że w przypadku konfliktu między:

- Wydajnością a integralnością danych → **wygrywa integralność**
- Wygodą użytkownika a bezpieczeństwem danych → **wygrywa bezpieczeństwo**
- Szybkością developmentu a stabilnością → **wygrywa stabilność**
- Nowymi funkcjami a niezawodnością → **wygrywa niezawodność**

### Manifestacja Zasady w Kodzie

#### 1. Walidacja Ścieżek: Zero Trust Filesystem Access

```
KAŻDE żądanie do systemu plików:

  User Request
       │
       ▼
  ┌─────────────┐     NIE     ┌──────────────┐
  │ safe_path()  │───────────→│ REJECT + LOG  │
  │ walidacja    │            └──────────────┘
  └──────┬──────┘
         │ TAK
         ▼
  ┌─────────────┐     NIE     ┌──────────────┐
  │ ALLOWED_ROOTS│───────────→│ REJECT + LOG  │
  │ sprawdzenie  │            └──────────────┘
  └──────┬──────┘
         │ TAK
         ▼
  ┌─────────────┐     NIE     ┌──────────────┐
  │ Home isolation│──────────→│ REJECT + LOG  │
  │ sprawdzenie   │           └──────────────┘
  └──────┬───────┘
         │ TAK
         ▼
  ┌─────────────┐
  │ WYKONAJ     │
  │ OPERACJĘ    │
  └─────────────┘
```

Trzy poziomy walidacji — każdy niezależny. Nawet jeśli jeden zawiedzie,
pozostałe nadal chronią dane.

#### 2. Backup Versioning: Nigdy Nie Nadpisuj

System backupu EthOS **nigdy nie nadpisuje** istniejących kopii zapasowych.
Każdy backup tworzy nową wersję. Stare wersje są usuwane dopiero po
przekroczeniu retention policy — i nigdy automatycznie bez potwierdzenia
dla wersji młodszych niż 24h.

#### 3. USB Hotplug: Bezpieczne Odmontowanie

Gdy użytkownik odłącza dysk USB:
1. System wykrywa zdarzenie udev
2. Przed odmontowaniem: `sync` — wymuszenie zapisu buforów
3. Sprawdzenie otwartych file descriptors
4. Graceful unmount z timeout
5. Powiadomienie Socket.IO do frontendu
6. Log operacji z timestampem

#### 4. RAID Monitoring: Proaktywna Ochrona

EthOS nie czeka na awarię. System aktywnie monitoruje:
- Stan tablicy RAID (degraded/rebuilding/healthy)
- SMART attributes każdego dysku
- Temperature dysków
- Reallocated sector count
- Pending sector count

Przekroczenie progu = natychmiastowy alert do użytkownika.

### Zapobieganie Długowi Technicznemu

Zasada DioE (Data Integrity over Everything) nakłada ograniczenia na
rozwój kodu:

1. **Żaden merge bez review modułów storage/backup** — zmiany w tych
   modułach wymagają dodatkowej walidacji
2. **Testy integracyjne dla operacji dyskowych** — każda nowa funkcja
   storage musi mieć testy z rzeczywistymi operacjami I/O
3. **Backward compatibility** — format backupu musi być odczytywalny
   przez wszystkie przyszłe wersje
4. **Graceful degradation** — awaria modułu AI nie może wpłynąć
   na działanie modułu storage

---

## 6. Architektura Modułowa: 37 Blueprintów

### Filozofia Architektury

EthOS implementuje wzorzec **"mikroserwisy w monolicie"** — 37 niezależnych
modułów Flask Blueprint współdzielących jeden proces Python:

```
┌──────────────────────────────────────────────────────────────┐
│                    EthOS Application Layer                     │
│                                                                │
│   Flask App (Gevent WSGI + Flask-SocketIO)                    │
│   │                                                            │
│   ├── /api/storage/*      ──→  storage_bp     (storage.py)    │
│   ├── /api/backup/*       ──→  backup_bp      (backup.py)     │
│   ├── /api/docker/*       ──→  docker_bp      (docker.py)     │
│   ├── /api/compose/*      ──→  compose_bp     (compose.py)    │
│   ├── /api/samba/*        ──→  samba_bp       (samba.py)      │
│   ├── /api/nfs/*          ──→  nfs_bp         (nfs.py)        │
│   ├── /api/webdav/*       ──→  webdav_bp      (webdav.py)     │
│   ├── /api/sftp/*         ──→  sftp_bp        (sftp.py)       │
│   ├── /api/vm/*           ──→  vm_bp          (vm.py)         │
│   ├── /api/surveillance/* ──→  surveillance_bp                 │
│   ├── /api/ai/*           ──→  ai_chat_bp     (ai_chat.py)    │
│   ├── /api/apps/*         ──→  app_store_bp   (app_store.py)  │
│   ├── /api/auth/*         ──→  auth_bp        (auth.py)       │
│   ├── /api/users/*        ──→  users_bp       (users.py)      │
│   ├── /api/ssh/*          ──→  ssh_bp         (ssh.py)        │
│   ├── /api/printers/*     ──→  printers_bp    (printers.py)   │
│   ├── /api/network/*      ──→  network_bp     (network.py)    │
│   ├── /api/system/*       ──→  system_bp      (system_info.py)│
│   ├── /api/services/*     ──→  services_bp    (services.py)   │
│   ├── /api/logs/*         ──→  logs_bp        (logs.py)       │
│   ├── /api/settings/*     ──→  settings_bp    (settings.py)   │
│   ├── /api/notifications/*──→  notify_bp      (notifications) │
│   ├── /api/downloads/*    ──→  downloads_bp   (downloads.py)  │
│   ├── /api/media/*        ──→  media_bp       (media.py)      │
│   ├── /api/gallery/*      ──→  gallery_bp     (gallery.py)    │
│   └── ... (+ pozostałe blueprinty)                             │
│                                                                │
│   Port 9000 (HTTP) ──→ Port 443 (HTTPS via reverse proxy)     │
└──────────────────────────────────────────────────────────────┘
```

### Kategorie Modułów

#### 🗄️ Przechowywanie i Backup (Storage & Backup)

| Moduł          | Rozmiar  | Odpowiedzialność                              |
|----------------|----------|-----------------------------------------------|
| `storage.py`   | 103 KB   | Mount/unmount, disk mgmt, USB hotplug, RAID   |
| `backup.py`    | 134 KB   | Snapshoty, wersjonowanie, scheduling, restore |

Te dwa moduły stanowią **rdzeń EthOS**. Ich łączny rozmiar (237 KB)
to ponad 40% logiki biznesowej całego systemu.

#### 🐳 Wirtualizacja i Kontenery (Virtualization)

| Moduł         | Odpowiedzialność                                     |
|---------------|------------------------------------------------------|
| `docker.py`   | Zarządzanie kontenerami Docker (Portainer-like)      |
| `compose.py`  | Docker Compose stack management                      |
| `vm.py`       | Maszyny wirtualne (KVM/libvirt)                      |
| `app_store.py`| App Store z kompatybilnością CasaOS                  |

EthOS pozwala zarządzać Dockerem **bez opuszczania interfejsu NAS**,
oferując funkcjonalność porównywalną z Portainer, ale zintegrowaną
z ekosystemem przechowywania danych.

#### 🌐 Udostępnianie Sieciowe (Network Sharing)

| Moduł       | Protokół      | Przypadek użycia                       |
|-------------|---------------|----------------------------------------|
| `samba.py`  | SMB/CIFS      | Udostępnianie dla Windows/macOS        |
| `nfs.py`    | NFS v3/v4     | Udostępnianie dla Linux/Unix           |
| `webdav.py` | WebDAV/HTTPS  | Dostęp przez przeglądarkę / mobilne    |
| `sftp.py`   | SFTP/SSH      | Bezpieczny transfer plików             |

Cztery protokoły sieciowe pokrywają **100% scenariuszy udostępniania**:
od domowej sieci Windows, przez cluster Linux, po zdalny dostęp mobilny.

#### 🤖 AI i Inteligentne Funkcje

| Moduł            | Odpowiedzialność                                |
|------------------|-------------------------------------------------|
| `ai_chat.py`     | Chat AI z RAG engine (Retrieval-Augmented Gen.) |
| `surveillance.py`| Zarządzanie kamerami, detekcja ruchu            |

Integracja AI to **nie gimmick** — to praktyczne narzędzie:
- RAG engine indeksuje dokumentację i pomaga w diagnostyce
- AI chat asystuje w konfiguracji systemu
- Surveillance z AI-powered detection

#### 🖥️ Administracja Systemem (System Admin)

| Moduł            | Odpowiedzialność                               |
|------------------|-------------------------------------------------|
| `system_info.py` | CPU, RAM, temperatura, uptime                   |
| `services.py`    | Zarządzanie usługami systemd                    |
| `users.py`       | Zarządzanie kontami użytkowników                |
| `ssh.py`         | Konfiguracja SSH, klucze autoryzacyjne          |
| `logs.py`        | Przeglądarka logów systemowych                  |
| `settings.py`    | Globalne ustawienia EthOS                       |
| `printers.py`    | Zarządzanie drukarkami (CUPS backend)            |
| `network.py`     | Konfiguracja sieci (IP, DNS, routing)           |

#### 🏗️ Narzędzia Budowania (Build Tools)

| Moduł              | Odpowiedzialność                               |
|--------------------|-------------------------------------------------|
| `image_builder.py` | Tworzenie bootowalnych obrazów x86 instalatora  |

Image Builder pozwala **wygenerować obraz ISO/IMG** gotowy do instalacji
EthOS na bare-metal hardware — od zera do działającego NAS.

### Dlaczego Flask Blueprints?

Wybór Flask z Blueprint pattern (zamiast Django, FastAPI, czy microservices):

```
Architektura Flask Blueprint:

Zalety:
  ✅ Izolacja logiki — każdy moduł to osobny namespace
  ✅ Niezależne testowanie — unit testy per blueprint
  ✅ Dynamiczne ładowanie — moduły mogą być wyłączane
  ✅ Shared process — zero overhead komunikacji między-procesowej
  ✅ Gevent compatibility — async I/O bez złożoności asyncio
  ✅ Flask-SocketIO — natywne WebSocket support
  ✅ Prostota — czytelny kod, łatwe onboarding nowych developerów

Kompromisy (świadome):
  ⚠️  Single process — brak horizontal scaling (ale NAS to single-node)
  ⚠️  Python GIL — ale Gevent omija to przez green threads
  ⚠️  Brak type safety — kompensowane przez walidację w runtime
```

### Frontend: Vanilla JavaScript SPA

Świadomy wybór **braku framework'a** (React, Vue, Angular):

- **Zero build step** — pliki JS serwowane bezpośrednio
- **Brak node_modules** — 0 MB zależności frontendowych
- **Pełna kontrola** — żadna abstrakcja nie ukrywa zachowania
- **Długowieczność** — vanilla JS nie wymaga migracji frameworka
- **Lekkość** — minimalne zużycie zasobów na embedded hardware

Stack frontendowy:
- **Font Awesome** — ikony (jeden zestaw, konsystentny UI)
- **Google Fonts (Inter)** — czytelna typografia
- **Socket.IO Client** — real-time updates z backendu
- **Vanilla CSS** — bez preprocessorów, custom properties

### Komunikacja Real-Time: Socket.IO

```
┌──────────┐    WebSocket     ┌──────────────┐
│  Browser  │◄──────────────►│ Flask-SocketIO │
│  (SPA)    │    Socket.IO    │  (Gevent)     │
└──────────┘                  └──────┬───────┘
                                     │
                              Eventy:
                              ├── disk:mounted
                              ├── disk:unmounted
                              ├── usb:detected
                              ├── backup:progress
                              ├── backup:complete
                              ├── raid:degraded
                              ├── docker:status
                              ├── notification:new
                              └── system:alert
```

Real-time communication jest kluczowy dla NAS — użytkownik musi
natychmiast wiedzieć, gdy:
- Dysk USB zostanie podłączony/odłączony
- Backup się zakończy lub nie powiedzie
- Tablica RAID zdegraduje
- Kontener Docker zmieni status
- System wykryje anomalię

### Wielojęzyczność (i18n)

EthOS wspiera pięć języków z możliwością rozszerzenia:

```
locales/
├── pl.json  — Polski (język domyślny)
├── en.json  — English
├── de.json  — Deutsch
├── fr.json  — Français
└── es.json  — Español
```

System i18n jest oparty na plikach JSON z kluczami hierarchicznymi.
Frontend dynamicznie ładuje odpowiedni plik locale na podstawie
preferencji użytkownika.

### Konfiguracja: `install.conf`

```ini
# /opt/ethos/install.conf — centralny plik konfiguracyjny
ETHOS_USER=ethos           # Użytkownik systemowy
ETHOS_HOSTNAME=ethos-nas   # Nazwa hosta
ETHOS_PORT=9000            # Port HTTP backendu
```

Minimalistyczna konfiguracja — zgodna z filozofią:
**sensowne domyślne wartości, minimum wymaganej konfiguracji**.

---

## 7. Wizja Długoterminowa

### Roadmapa Strategiczna

```
2026 Q1-Q2 │ FAZA: STABILIZACJA
            │ ✅ 37 blueprintów w pełni operacyjnych
            │ ✅ Image builder dla x86_64
            │ ✅ Docker + CasaOS app store compatibility
            │ ✅ 5-language i18n support
            │ 🔄 Hardening bezpieczeństwa
            │ 🔄 Performance profiling & optimization
            │
2026 Q3-Q4 │ FAZA: STORAGE EVOLUTION
            │ 📋 BTRFS native RAID support
            │ 📋 ZFS integration (non-FUSE)
            │ 📋 Encrypted volumes (LUKS)
            │ 📋 Storage tiering (SSD cache + HDD bulk)
            │ 📋 Deduplication engine
            │
2027 Q1-Q2 │ FAZA: ENTERPRISE FEATURES
            │ 📋 LDAP/Active Directory integration
            │ 📋 2FA/FIDO2 authentication
            │ 📋 Audit logging (compliance-grade)
            │ 📋 SNMP monitoring support
            │ 📋 API rate limiting & quotas
            │
2027 Q3-Q4 │ FAZA: ECOSYSTEM
            │ 📋 Plugin SDK & marketplace
            │ 📋 Community app submissions
            │ 📋 Theme engine
            │ 📋 Mobile companion app
            │ 📋 Desktop sync client
            │
2028+       │ FAZA: CLUSTERING
            │ 📋 Multi-node EthOS clusters
            │ 📋 Distributed storage (GlusterFS/Ceph)
            │ 📋 Automatic failover
            │ 📋 Cross-node backup replication
            │ 📋 Centralne zarządzanie flotą
```

### Enterprise-Grade NAS OS

Wizja EthOS to system, który może zastąpić Synology DSM w firmie:

| Funkcja               | Synology DSM    | EthOS (obecny)  | EthOS (2028)    |
|------------------------|-----------------|------------------|-----------------|
| Web UI                 | ✅              | ✅               | ✅              |
| Storage management     | ✅              | ✅               | ✅              |
| Docker                 | ✅              | ✅               | ✅              |
| Virtual Machines       | ✅              | ✅               | ✅              |
| Surveillance           | ✅ (licencje)   | ✅ (bez licencji)| ✅              |
| ZFS                    | Btrfs only      | mdadm            | ZFS native      |
| Clustering             | HA (2 nodes)    | ❌               | Multi-node      |
| Plugin ecosystem       | Packages        | App Store        | SDK + Market    |
| Open source            | ❌              | ✅               | ✅              |
| Vendor lock-in         | ✅ (hardware)   | ❌               | ❌              |
| Telemetria             | ✅              | ❌               | ❌              |

### Plugin Ecosystem

Przyszły system pluginów EthOS:

```python
# Przykład struktury pluginu EthOS
# /opt/ethos/plugins/my-plugin/

plugin.json:
{
    "name": "my-custom-plugin",
    "version": "1.0.0",
    "author": "Community",
    "blueprint": "my_plugin_bp",
    "routes_prefix": "/api/my-plugin",
    "requires": ["storage", "notifications"],
    "min_ethos_version": "2.0.0"
}

# Plugin automatycznie rejestrowany jako Flask Blueprint
# z dostępem do API storage i notifications
```

### AI-Assisted Administration

Wizja integracji AI w administracji systemem:

1. **Diagnostyka** — „Dlaczego dysk sdc jest wolny?" → AI analizuje
   SMART data, I/O stats, temperaturę i podaje diagnozę
2. **Optymalizacja** — AI sugeruje optymalną konfigurację RAID
   na podstawie wzorców użycia
3. **Predykcja awarii** — ML model przewidujący failure dysków
   na podstawie trendów SMART
4. **Natural language config** — „Udostępnij folder Photos przez
   Samba tylko dla użytkownika anna" → AI generuje konfigurację

RAG Engine już zintegrowany w `ai_chat.py` stanowi fundament
pod te przyszłe funkcjonalności.

### Multi-Node Clustering (Wizja 2028+)

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│ EthOS    │     │ EthOS    │     │ EthOS    │
│ Node 1   │◄───►│ Node 2   │◄───►│ Node 3   │
│ (Primary)│     │(Secondary)│     │(Tertiary)│
└────┬─────┘     └────┬─────┘     └────┬─────┘
     │                │                │
     ▼                ▼                ▼
┌─────────────────────────────────────────────┐
│           Distributed Storage Pool           │
│     (GlusterFS / Ceph / custom replication) │
└─────────────────────────────────────────────┘

Funkcjonalność:
├── Automatic failover — node 1 pada, node 2 przejmuje
├── Cross-node replication — dane na wielu maszynach
├── Centralne zarządzanie — jeden dashboard dla fleet
├── Load balancing — rozłożenie obciążenia I/O
└── Geographic redundancy — nody w różnych lokalizacjach
```

---

## 8. Etyka Systemowa

### Deklaracja Etyczna EthOS

EthOS opiera się na zestawie nienaruszalnych zasad etycznych,
które definiują, czym system **jest** i czym **nigdy nie będzie**:

### 🔒 Zasada 1: Zero Telemetrii

```
EthOS NIGDY:
  ✗ Nie wysyła danych o użyciu do żadnego serwera
  ✗ Nie zbiera statystyk bez jawnej zgody
  ✗ Nie implementuje "phone home" functionality
  ✗ Nie trackuje zachowań użytkownika
  ✗ Nie wykorzystuje danych użytkownika do treningu AI

EthOS ZAWSZE:
  ✓ Działa w pełni offline
  ✓ Aktualizacje są inicjowane wyłącznie przez użytkownika
  ✓ Każda komunikacja sieciowa jest jawna i udokumentowana
```

### 🏠 Zasada 2: Suwerenność Danych

Użytkownik EthOS jest **właścicielem** swoich danych w każdym sensie:
- **Fizycznie** — dane na jego dyskach, w jego lokalizacji
- **Logicznie** — żaden proces nie modyfikuje danych bez wiedzy użytkownika
- **Prawnie** — brak Terms of Service ograniczających użycie danych
- **Technicznie** — standardowe formaty, zero vendor lock-in

### 🔓 Zasada 3: Otwarta Architektura

```
Transparentność w EthOS:

Kod źródłowy        → Otwarty, audytowalny
Format konfiguracji → JSON/INI, czytelne dla człowieka
Format backupu      → Standard (tar/rsync), przenośny
Protokoły sieciowe  → Standardowe (SMB, NFS, WebDAV, SFTP)
API                 → REST + WebSocket, udokumentowane
Baza danych         → SQLite/pliki, bez zamkniętego engine'u
```

Żaden komponent EthOS nie używa zamkniętego, proprietary formatu.
Użytkownik może w każdej chwili przenieść swoje dane do innego systemu
**bez żadnych narzędzi specjalnych**.

### 🛡️ Zasada 4: Bezpieczeństwo Bez Kompromisów

Bezpieczeństwo nie jest feature'em — to **fundamentalna właściwość systemu**:

- `safe_path()` nie ma trybu bypass — nawet administrator nie może go wyłączyć
- Token auth nie ma "remember forever" — 7-dniowy limit jest sztywny
- HTTPS nie jest opcjonalny w produkcji — HTTP dopuszczalne tylko w development
- Każda operacja root jest logowana — brak "cichych" eskalacji uprawnień

### 🤝 Zasada 5: Społeczność i Współpraca

EthOS wierzy w model rozwoju oparty na społeczności:

- **Kontrybucje** — każdy może zaproponować zmianę
- **Feedback-driven development** — roadmapa kształtowana przez użytkowników
- **Dokumentacja jako obywatel pierwszej klasy** — nie afterthought
- **Inclusive design** — 5 języków, accessibility, responsive UI

### Anty-Wzorce: Czego EthOS Nigdy Nie Zrobi

| Anty-Wzorzec                | Dlaczego NIE                              |
|-----------------------------|-------------------------------------------|
| Freemium model              | Wszystkie funkcje dostępne dla każdego    |
| Licencje per-kamera         | Surveillance bez limitów                  |
| Wymuszony hardware          | Działa na dowolnym x86_64                 |
| Cloud-dependent features    | Pełna funkcjonalność offline              |
| Obfuscated code             | Kod zawsze otwarty i czytelny             |
| Forced updates              | Użytkownik decyduje kiedy aktualizować    |
| Data mining                 | Dane użytkownika to NIE nasz zasób        |

---

## Podsumowanie

EthOS to więcej niż system operacyjny NAS. To **manifest cyfrowej
niezależności** — dowód, że można zbudować system klasy enterprise
bez kompromisów etycznych, bez vendor lock-in i bez traktowania
użytkownika jako produktu.

37 modułów. 237 KB samego kodu storage + backup. Pięć języków.
Cztery protokoły sieciowe. Zero telemetrii. Zero kompromisów
w kwestii integralności danych.

**Data Integrity over Everything.**

```
    ███████╗████████╗██╗  ██╗ ██████╗ ███████╗
    ██╔════╝╚══██╔══╝██║  ██║██╔═══██╗██╔════╝
    █████╗     ██║   ███████║██║   ██║███████╗
    ██╔══╝     ██║   ██╔══██║██║   ██║╚════██║
    ███████╗   ██║   ██║  ██║╚██████╔╝███████║
    ╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚══════╝

         NAS OS — Built on Trust, Not Lock-in
```

---

*Dokument ten jest żywym manifestem — będzie ewoluował wraz z systemem,
ale jego fundamentalne zasady pozostaną niezmienne.*

*— Główny Architekt & Strateg, EthOS Project*
