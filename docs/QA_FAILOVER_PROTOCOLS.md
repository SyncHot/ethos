# EthOS — Protokoły QA i Failover

> Przygotowane przez: **UX/UI Designer & QA Lead** | Model referencyjny: Claude Sonnet / GPT-4o-mini
> Data: 2026-03-17

---

## Spis Treści

1. [Strategia Testowania](#1-strategia-testowania)
2. [Scenariusze Krytyczne: Awaria Dysku](#2-scenariusze-krytyczne-awaria-dysku)
3. [Scenariusze Krytyczne: Sieć](#3-scenariusze-krytyczne-sieć)
4. [Scenariusze Krytyczne: Kontener Docker](#4-scenariusze-krytyczne-kontener-docker)
5. [Protokoły Failover dla Usług](#5-protokoły-failover-dla-usług)
6. [Test Matrix: 37 Modułów](#6-test-matrix-37-modułów)
7. [Procedury Odzyskiwania Danych](#7-procedury-odzyskiwania-danych)
8. [Monitoring i Alerty](#8-monitoring-i-alerty)
9. [Checklist Przed Wydaniem](#9-checklist-przed-wydaniem)
10. [Znane Wzorce Błędów](#10-znane-wzorce-błędów)

---

## 1. Strategia Testowania

### Piramida Testów EthOS

```
         /\
        / E2E\             <- 10% — pełne scenariusze użytkownika
       /------\               (Playwright/Puppeteer)
      /Integration\        <- 30% — API + Socket.IO + Service
     /--------------\         (pytest, supertest)
    /   Unit Tests    \    <- 60% — logika biznesowa, parsery, utils
   /--------------------\     (pytest, jest)
  /    Manual / Explor.   \  <- ad-hoc — edge cases, UX review
 /--------------------------\
```

### Kategorie Testów

#### Testy Integralności Danych (Priorytet: KRYTYCZNY)

- **Operacje plikowe:** kopiowanie, przenoszenie, usuwanie, zmiana nazwy — weryfikacja że dane nie zostały uszkodzone
- **Backup/Restore:** cykl zapisu → przywrócenia → porównania checksumów
- **RAID rebuild:** odbudowa macierzy bez utraty danych
- **Docker volumes:** persystencja danych po restart/update kontenera

#### Testy Dostępności Usług (Priorytet: WYSOKI)

- **ethos.service** — główna usługa HTTP na porcie 9000
- **Socket.IO** — real-time połączenia WebSocket
- **Samba/NFS** — udostępnienia sieciowe
- **Docker daemon** — zarządzanie kontenerami
- **Surveillance recording** — ciągłość nagrywania

#### Testy Autentykacji (Priorytet: WYSOKI)

- **Token generation** — 64-char hex, unikalność
- **Bearer header** — `Authorization: Bearer <token>`
- **Token persistence** — przetrwanie restartu usługi
- **Session timeout** — automatyczne wylogowanie
- **Brute-force protection** — rate limiting na endpoint auth

#### Testy Operacji Plikowych (Priorytet: WYSOKI)

- **Upload** — duże pliki (>4GB), wiele plików jednocześnie
- **Download** — resumable download, serwowanie zakresów (Range header)
- **Share** — publiczne linki via share.html, wygasanie, hasło
- **Thumbnail generation** — Gallery thumbnails dla zdjęć/wideo
- **Search** — wyszukiwanie plików po nazwie, rozszerzeniu

### Środowiska Testowe

| Środowisko | Opis                           | Dane           | Cel                    |
|-----------|--------------------------------|----------------|------------------------|
| DEV       | Lokalna maszyna, 1 dysk       | Syntetyczne    | Unit + Integration     |
| STAGING   | VM z 4 dyskami, RAID           | Kopia prod     | E2E + Performance      |
| PROD-LIKE | Hardware NAS, pełna konfig     | Anonimizowane  | Acceptance + Stress    |

---

## 2. Scenariusze Krytyczne: Awaria Dysku

### Scenariusz 2.1: Awaria Dysku Podczas Rebuild RAID

**Kontekst:** RAID 5 z 4 dyskami. Dysk 1 awariuje, rozpoczyna się rebuild na spare.
Podczas rebuild awariuje Dysk 3.

**Procedura testowa:**

```
KROK 1: Skonfiguruj RAID 5 z 4 dyskami + 1 spare
         mdadm --create /dev/md0 --level=5 --raid-devices=4 --spare-devices=1

KROK 2: Zapisz testowe dane (10GB zróżnicowanych plików)
         Oblicz SHA256 każdego pliku -> zapisz checksums.txt

KROK 3: Symuluj awarię Dysku 1
         mdadm --manage /dev/md0 --fail /dev/sdb

KROK 4: Potwierdź rozpoczęcie rebuild
         cat /proc/mdstat -> verify [UU_U] rebuilding

KROK 5: Podczas rebuild (30-50%) symuluj awarię Dysku 3
         mdadm --manage /dev/md0 --fail /dev/sdd

OCZEKIWANY REZULTAT:
  [OK] EthOS Storage Manager wyświetla alert KRYTYCZNY
  [OK] Toast notification: "RAID degraded — risk of data loss"
  [OK] Dane nadal dostępne w trybie read-only (RAID 5 toleruje 1 awarię)
  [!!] Po 2. awarii: RAID failed, dane niedostępne
  [OK] EthOS proponuje procedurę recovery
```

**Weryfikacja UI:**

- [ ] Storage Manager wyświetla czerwony status dysku
- [ ] Taskbar notification badge
- [ ] Email/webhook alert (jeśli skonfigurowany)
- [ ] Backup Manager proponuje natychmiastowy backup zdrowych danych

### Scenariusz 2.2: Odłączenie USB Podczas Kopiowania

**Kontekst:** Użytkownik kopiuje 50GB danych z dysku USB na NAS. USB zostaje odłączony.

```
KROK 1: Podłącz dysk USB z 50GB danych testowych
KROK 2: Rozpocznij kopiowanie przez File Manager (drag & drop)
KROK 3: Po skopiowaniu ~20GB fizycznie odłącz USB
KROK 4: Obserwuj zachowanie systemu

OCZEKIWANY REZULTAT:
  [OK] File Manager wyświetla błąd: "Urządzenie źródłowe odłączone"
  [OK] Pliki już skopiowane są kompletne i nienaruszone
  [OK] Plik w trakcie kopiowania jest usunięty (częściowy)
  [OK] Operacja anulowana — brak zombie procesów kopiowania
  [OK] Storage Manager aktualizuje listę zamontowanych urządzeń
```

### Scenariusz 2.3: Zapełnienie Dysku Podczas Operacji

**Kontekst:** Dysk docelowy ma 500MB wolnego miejsca. Operacja wymaga 2GB.

```
OCZEKIWANY REZULTAT:
  [OK] Operacja zatrzymana z komunikatem "Brak miejsca na dysku"
  [OK] Częściowe dane usunięte (cleanup)
  [OK] Download Manager pausuje aktywne pobieranie
  [OK] Docker blokuje pull nowych obrazów
  [OK] Backup Manager przerywa backup z alertem
  [OK] Monitor pokazuje 100% disk usage z alertem
```

### Scenariusz 2.4: Utrata Zasilania Podczas Backup

**Kontekst:** Backup Manager wykonuje pełny backup. Zasilanie zostaje przerwane.

```
KROK 1: Rozpocznij pełny backup (200GB)
KROK 2: Po 40% symuluj utratę zasilania (hard power off)
KROK 3: Uruchom system ponownie
KROK 4: Sprawdź stan backup

OCZEKIWANY REZULTAT:
  [OK] Poprzedni kompletny backup nienaruszony
  [OK] Przerwany backup oznaczony jako "incomplete"
  [OK] Możliwość wznowienia (resume) od punktu przerwania
  [OK] Brak corruption na systemie plików (journaling)
  [OK] Backup Manager wyświetla historię z oznaczeniem błędu
```

---

## 3. Scenariusze Krytyczne: Sieć

### Scenariusz 3.1: Utrata Połączenia Sieciowego

**Kontekst:** Użytkownik aktywnie korzysta z WebUI. Kabel sieciowy zostaje odłączony.

```
OCZEKIWANY REZULTAT — Frontend:
  [OK] Socket.IO wykrywa disconnect w ~5s
  [OK] Toast notification: "Utracono połączenie z serwerem"
  [OK] Wskaźnik statusu w taskbar zmienia się na czerwony
  [OK] Retry z exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (max)
  [OK] Po przywróceniu: auto-reconnect + sync stanu
  [OK] Operacje lokalne (sortowanie, przeglądanie cache) nadal działają

OCZEKIWANY REZULTAT — Backend:
  [OK] Usługi NAS (Samba, NFS) działają niezależnie od WebUI
  [OK] Backup job kontynuuje się bez WebUI
  [OK] Surveillance recording kontynuuje lokalne zapisywanie
  [OK] Docker kontenery działają niezależnie
```

### Scenariusz 3.2: Awaria DNS

```
PROCEDURA:
  1. Skonfiguruj DNS na nieistniejący serwer (192.168.1.254)
  2. Obserwuj zachowanie systemu

OCZEKIWANY REZULTAT:
  [OK] WebUI dostępne po IP (nie wymaga DNS)
  [OK] Docker pull — timeout z czytelnym komunikatem
  [OK] Download Manager — "DNS resolution failed"
  [OK] AI Chat — degraded mode, jasny komunikat o braku internetu
  [OK] Aktualizacje systemu — pauza z alertem
```

### Scenariusz 3.3: Odłączenie Samba/NFS Share

```
PROCEDURA:
  1. Udostępnij folder przez SMB
  2. Podłącz na kliencie Windows/Mac
  3. Rozpocznij kopiowanie dużego pliku
  4. Wyłącz usługę Samba na NAS

OCZEKIWANY REZULTAT:
  [OK] Klient dostaje "Network error" — plik częściowy do usunięcia po stronie klienta
  [OK] EthOS loguje wymuszony disconnect klienta
  [OK] Po restarcie Samba — klient reconnectuje automatycznie (SMB3)
  [OK] NFS: stale file handle -> wymaga remount po stronie klienta
```

### Scenariusz 3.4: WebSocket Reconnection

```
PROCEDURA:
  1. Otwórz WebUI, zweryfikuj Socket.IO connection
  2. Wykonaj: sudo iptables -A INPUT -p tcp --dport 9000 -j DROP
  3. Odczekaj 30 sekund
  4. Wykonaj: sudo iptables -D INPUT -p tcp --dport 9000 -j DROP

OCZEKIWANY REZULTAT:
  [OK] Frontend wykrywa brak heartbeat po 5-10s
  [OK] Banner "Ponowne łączenie..." widoczny u góry ekranu
  [OK] Po przywróceniu — Socket.IO reconnect + state sync
  [OK] Otwarte okna zachowują swój stan (nie resetują się)
  [OK] Aktywne operacje (upload, download) — resume lub czytelny błąd
```

### Scenariusz 3.5: Wygaśnięcie Certyfikatu HTTPS

```
OCZEKIWANY REZULTAT:
  [OK] Przeglądarka wyświetla ostrzeżenie (NET::ERR_CERT_DATE_INVALID)
  [OK] Port 9000 (HTTP) nadal działa jako fallback
  [OK] Monitor alertuje o zbliżającym się wygaśnięciu (30/14/7/1 dzień przed)
  [OK] Settings umożliwia wgranie nowego certyfikatu
  [OK] Let's Encrypt auto-renewal (jeśli skonfigurowane) — retry + alert przy failure
```

---

## 4. Scenariusze Krytyczne: Kontener Docker

### Scenariusz 4.1: Crash Kontenera

```
PROCEDURA:
  1. Uruchom kontener (np. Plex) przez Docker Manager
  2. docker kill <container_id> (symulacja crash)
  3. Obserwuj zachowanie

OCZEKIWANY REZULTAT:
  [OK] Docker Manager wykrywa zmianę stanu w ~5s (Socket.IO event)
  [OK] Status kontenera: "Exited (137)" wyświetlony w UI
  [OK] Jeśli restart_policy=always -> auto-restart + log w historii
  [OK] Jeśli restart_policy=no -> przycisk "Restart" aktywny
  [OK] Toast notification: "Kontener Plex uległ awarii"
  [OK] Logi kontenera dostępne do diagnozy
```

### Scenariusz 4.2: Awaria Compose Project

```
PROCEDURA:
  1. Uruchom compose project z 3+ serwisami
  2. Wymuś OOM kill na jednym serwisie
  3. Obserwuj propagację błędu

OCZEKIWANY REZULTAT:
  [OK] Docker Manager oznacza konkretny serwis jako "unhealthy"
  [OK] Zależne serwisy (depends_on) wyświetlają degraded status
  [OK] Opcja "Restart Service" obok "Restart Stack"
  [OK] Logi serwisu z OOM marker widoczne w Docker Manager
```

### Scenariusz 4.3: Uszkodzenie Docker Volume

```
PROCEDURA:
  1. Utwórz named volume z danymi
  2. Usuń fizycznie pliki w /var/lib/docker/volumes/<name>/_data/
  3. Uruchom kontener korzystający z tego volume

OCZEKIWANY REZULTAT:
  [OK] Kontener startuje (volume istnieje, ale puste pliki)
  [OK] Docker Manager wyświetla volume z ostrzeżeniem
  [OK] Rekomendacja: przywróć z backup (integracja z Backup Manager)
  [OK] Opcja eksportu volume -> tar.gz przed naprawą
```

### Scenariusz 4.4: Awaria App Store Deployment

```
PROCEDURA:
  1. Rozpocznij instalację aplikacji z App Store (Docker)
  2. Symuluj brak połączenia z Docker Hub (iptables block)

OCZEKIWANY REZULTAT:
  [OK] Timeout po 30s z czytelnym komunikatem
  [OK] "Nie można pobrać obrazu — sprawdź połączenie internetowe"
  [OK] Brak osieroconych kontenerów/sieci po failed deployment
  [OK] Opcja ponowienia próby po przywróceniu łączności
  [OK] Cleanup: usunięcie częściowo pobranych warstw
```

### Scenariusz 4.5: Wyczerpanie Zasobów

```
PROCEDURA:
  1. Uruchom kontener z limitem pamięci 256MB
  2. Wymuś alokację >256MB wewnątrz kontenera
  3. Obserwuj zachowanie systemu

OCZEKIWANY REZULTAT:
  [OK] Docker OOM killer terminuje kontener
  [OK] Monitor wyświetla spike pamięci przed kill
  [OK] Docker Manager loguje przyczynę: "OOMKilled: true"
  [OK] Rekomendacja: zwiększ limit pamięci w compose/settings
```

---

## 5. Protokoły Failover dla Usług

### 5.1. Crash ethos.service

**ethos.service** to główny proces — serwer HTTP (port 9000), API, Socket.IO hub.

```
+---------------------------------------------+
|         ethos.service CRASH FLOW             |
+---------------------------------------------+
|                                              |
|  ethos.service crash                         |
|       |                                      |
|       v                                      |
|  systemd wykrywa exit                        |
|       |                                      |
|       v                                      |
|  Restart=on-failure (5s delay)               |
|       |                                      |
|       +-- Token storage <- plik/DB przetrwa  |
|       |   (persystentny na dysku)            |
|       |                                      |
|       +-- WebSocket connections <- zerwane   |
|       |   Klienci: reconnect po 5s           |
|       |                                      |
|       +-- Active uploads <- przerwane        |
|       |   Frontend: retry lub error          |
|       |                                      |
|       +-- Cron jobs (backup, surveillance)   |
|           <- wznowione po restart            |
|                                              |
|  ethos.service restart complete              |
|       |                                      |
|       v                                      |
|  Klienci: Socket.IO auto-reconnect           |
|  Stan: pełna operacyjność w ~10s             |
+---------------------------------------------+
```

### 5.2. Token Storage Loss

```
SCENARIUSZ: Plik/baza z tokenami uszkodzona lub usunięta po restart

WPŁYW:
  [!!] Wszyscy użytkownicy wylogowani
  [!!] Aktywne sesje nieważne
  [!!] API requests z starymi tokenami -> 401 Unauthorized

PROCEDURA RECOVERY:
  1. ethos.service regeneruje token storage przy starcie
  2. Użytkownicy muszą zalogować się ponownie
  3. Automatyczny redirect na stronę logowania
  4. Setup wizard: jeśli brak kont -> uruchomienie setup.js
```

### 5.3. Przerwanie Backup Job

```
SCENARIUSZ: ethos.service crash podczas aktywnego backup

ZABEZPIECZENIA:
  [OK] Backup atomiczny — snapshot stanu, potem kopiowanie
  [OK] Incomplete backup oznaczony flagą w metadanych
  [OK] Poprzedni kompletny backup NIGDY nie usuwany przed weryfikacją nowego
  [OK] Rsync/cp --reflink — resume po przerwaniu
  [OK] Backup Manager loguje checkpoint co 1000 plików

RECOVERY:
  1. ethos.service restart
  2. Backup Manager wykrywa incomplete backup
  3. Opcja: "Wznów" lub "Zacznij od nowa"
  4. Weryfikacja checksum po ukończeniu
```

### 5.4. Przerwa w Nagrywaniu Surveillance

```
SCENARIUSZ: Restart usługi podczas aktywnego nagrywania z kamer

WPŁYW:
  [!!] Przerwa w nagrywaniu: ~10-15s (czas restartu usługi)
  [!!] Segment wideo przed crash: może być ucięty

ZABEZPIECZENIA:
  [OK] Segmentowane nagrywanie (pliki 5-minutowe) — utrata max 1 segmentu
  [OK] Kamera z lokalnym buforem — nagranie zachowane na karcie SD kamery
  [OK] Po restart: automatyczne wznowienie nagrywania
  [OK] Timeline w Surveillance app wyświetla przerwę jako "gap"
  [OK] Alert: "Wykryto przerwę w nagrywaniu: 14:32:05 — 14:32:18"
```

---

## 6. Test Matrix: 37 Modułów

### Legenda Priorytetów

| Priorytet | Opis                        | Częstotliwość testów    |
|-----------|-----------------------------|------------------------|
| **P1**    | Krytyczny — utrata danych   | Każdy release + nightly|
| **P2**    | Wysoki — dostępność usługi  | Każdy release          |
| **P3**    | Średni — degradacja UX      | Co 2 release           |
| **P4**    | Niski — kosmetyczny         | Kwartalnie             |

### Matrix

| # | Moduł                | Operacje Krytyczne                   | Tryby Awarii                      | Recovery                        | P  |
|---|----------------------|--------------------------------------|-----------------------------------|---------------------------------|----|
| 1 | File Manager         | Upload, download, delete, share      | Disk full, permission denied      | Retry, cleanup partial          | P1 |
| 2 | Storage Manager      | Mount, unmount, RAID create          | Disk failure, RAID degraded       | Spare rebuild, alert            | P1 |
| 3 | Backup Manager       | Backup, restore, schedule            | Interrupted backup, corrupt snap  | Resume, rollback                | P1 |
| 4 | Download Manager     | Add torrent/URL, pause, resume       | Connection loss, disk full        | Auto-pause, retry               | P2 |
| 5 | Docker Manager       | Start, stop, create, compose up      | Container crash, OOM, pull fail   | Auto-restart, cleanup           | P2 |
| 6 | Surveillance         | Record, playback, motion detect      | Camera disconnect, storage full   | Reconnect, rotate old files     | P2 |
| 7 | Gallery              | View, thumbnail gen, organize        | Corrupt file, missing thumb       | Skip corrupt, regen thumbs      | P3 |
| 8 | Document Editor      | Create, edit, export DOCX/PDF        | Save failure, format corruption   | Auto-save, local storage        | P2 |
| 9 | Builder              | Create image, configure, build       | Build failure, missing dir        | Cleanup, retry, create dirs     | P3 |
| 10| Monitor              | CPU/RAM/disk/GPU/net/temp display    | Sensor unavailable, high load     | Graceful degrade, cache         | P2 |
| 11| Settings             | System config, theme, language       | Config corruption, invalid input  | Default fallback, validation    | P2 |
| 12| Network              | Interface config, WiFi, DNS          | Interface down, DHCP failure      | Fallback static IP              | P2 |
| 13| Users                | Create, delete, permissions          | Auth DB corruption                | Admin recovery mode             | P1 |
| 14| AI Chat              | RAG query, conversation              | API timeout, model unavailable    | Graceful error, offline mode    | P4 |
| 15| Terminal             | PTY session, command execution       | WebSocket disconnect              | Reconnect, session restore      | P3 |
| 16| Setup Wizard         | First-time configuration             | Interrupted setup, invalid input  | Resume from step, validation    | P2 |
| 17| Share Page           | Public file sharing (share.html)     | Expired link, file deleted        | 404 with message                | P3 |
| 18| Auth Module          | Login, token gen, session mgmt       | Token corruption, brute force     | Regen tokens, rate limit        | P1 |
| 19| i18n                 | Translation loading, locale switch   | Missing locale, fallback          | Fallback to EN                  | P4 |
| 20| Toast System         | User notifications                   | Queue overflow, rapid fire        | Throttle, stack limit           | P4 |
| 21| Socket.IO Hub        | Real-time events, state sync         | Disconnect, server restart        | Auto-reconnect, state resync    | P2 |
| 22| Desktop/Window Mgr   | Window create/move/resize/close      | Memory leak, z-index overflow     | Cleanup, reset stacking         | P3 |
| 23| App Launcher         | App listing, search, open            | Registry corruption               | Reload from source              | P3 |
| 24| Taskbar              | Running apps, system tray, clock     | Render failure                    | Force re-render                 | P4 |
| 25| CSS Theme System     | Dark/light toggle, variable cascade  | Inconsistent theme, white-on-white| Force CSS reload                | P3 |
| 26| Disk Analysis        | Space usage, file distribution       | Slow scan, permission errors      | Timeout + partial results       | P3 |
| 27| RAID Management      | Create, expand, repair, monitor      | Multiple disk failure             | Alert + guided recovery         | P1 |
| 28| Sharing Protocols    | SMB, NFS, WebDAV config              | Service crash, port conflict      | Restart service, port check     | P2 |
| 29| Snapshot Engine      | Create, list, restore, delete snaps  | Insufficient space, corrupt snap  | Cleanup old, verify integrity   | P1 |
| 30| Torrent Engine       | Seeding, leeching, DHT/tracker       | Port blocked, tracker down        | DHT fallback, port forwarding   | P3 |
| 31| Debrid Integration   | Premium link resolution              | API key expired, service down     | Graceful error, key refresh     | P4 |
| 32| Compose Manager      | Stack deploy, env config, logs       | Dependency failure, invalid YAML  | Validation, rollback            | P2 |
| 33| App Store            | Browse, install, update apps         | Registry unreachable, pull fail   | Cache, retry, offline browse    | P3 |
| 34| Camera Manager       | ONVIF discovery, stream config       | Camera offline, codec incompatible| Reconnect loop, transcode       | P3 |
| 35| Motion Detection     | Zone config, sensitivity tuning      | False positives, CPU overload     | Throttle, zone adjustment       | P4 |
| 36| GPU Monitor          | GPU stats, temp, utilization         | No GPU, driver mismatch           | Hide section, driver alert      | P4 |
| 37| Image Builder        | x86 image creation, customization    | Build env missing, disk space     | Create dirs, space check        | P3 |

---

## 7. Procedury Odzyskiwania Danych

### 7.1. Przywracanie z Backup

```
+--------------------------------------------------+
|         PROCEDURA RESTORE Z BACKUP                |
+--------------------------------------------------+
|                                                   |
|  1. Otwórz Backup Manager                         |
|  2. Wybierz zakładkę "Historia"                   |
|  3. Znajdź backup do przywrócenia                 |
|     +-- Sprawdź status: [OK] Complete             |
|     +-- Sprawdź datę i rozmiar                    |
|  4. Kliknij "Przywróć" (Restore)                  |
|  5. Wybierz tryb:                                 |
|     +-- [A] Pełne przywrócenie (nadpisz)          |
|     +-- [B] Przywróć do nowej lokalizacji         |
|     +-- [C] Wybierz pliki do przywrócenia         |
|  6. Potwierdź w modal                             |
|  7. Monitoruj postęp (progress bar)               |
|  8. Weryfikacja checksumów (automatyczna)         |
|  9. Toast: "Przywrócono X plików (Y GB)"          |
|                                                   |
|  CZAS: ~50 MB/s dla HDD, ~200 MB/s dla SSD       |
+--------------------------------------------------+
```

### 7.2. Snapshot Rollback

```
PROCEDURA:
  1. Storage Manager -> zakładka "Snapshots"
  2. Lista snapshotów z datami i opisami
  3. Wybierz snapshot -> "Preview" (lista zmian)
  4. "Rollback" -> modal z ostrzeżeniem:
     "Wszystkie zmiany po [data] zostaną cofnięte"
  5. Potwierdź -> rollback atomiczny
  6. Weryfikacja integralności

UWAGI:
  [!!] Snapshot rollback jest DESTRUKCYJNY — zmiany po snapshot są tracone
  [!!] Przed rollback: automatyczny snapshot "pre-rollback-TIMESTAMP"
  [!!] Wspierane tylko na ZFS/Btrfs, nie na ext4/XFS
```

### 7.3. Docker Volume Recovery

```
PROCEDURA:
  1. Docker Manager -> Volumes -> wybierz uszkodzony volume
  2. Sprawdź powiązane kontenery — zatrzymaj je
  3. Opcje recovery:
     +-- [A] Przywróć z backup volume (Backup Manager)
     +-- [B] Importuj z tar.gz (ręcznie wyeksportowany)
     +-- [C] Re-create (utrata danych, czyste volume)
  4. Restart kontenerów
  5. Weryfikacja — sprawdź logi kontenerów

PREWENCJA:
  [OK] Regularne backup volumes (Backup Manager -> Docker Volumes)
  [OK] Monitorowanie health checks kontenerów
  [OK] Named volumes zamiast bind mounts (łatwiejsze backup/restore)
```

### 7.4. Configuration Backup/Restore

```
KONFIGURACJA ETHOS OBEJMUJE:
  /etc/ethos/             — główna konfiguracja
  /etc/samba/smb.conf     — Samba config
  /etc/exports            — NFS exports
  Docker compose files    — stack definitions
  Crontab                 — scheduled tasks
  User database           — konta + tokeny

BACKUP KONFIGURACJI:
  Settings -> Backup -> "Eksportuj konfigurację"
  -> Generuje: ethos-config-YYYY-MM-DD.tar.gz

RESTORE KONFIGURACJI:
  Settings -> Backup -> "Importuj konfigurację"
  -> Upload tar.gz
  -> Preview zmian (diff)
  -> Potwierdź -> Restart usług
```

---

## 8. Monitoring i Alerty

### 8.1. Co Monitoruje monitor.py

```
+---------------------------------------------------+
|              METRYKI MONITOROWANE                   |
+---------------------------------------------------+
|                                                    |
|  CPU:     Użycie per rdzeń, load average,          |
|           temperatura, częstotliwość                |
|                                                    |
|  RAM:     Użycie, dostępna, swap, buffers/cache    |
|                                                    |
|  Dyski:   Użycie per partycja, I/O wait,           |
|           prędkość r/w, temperatura, S.M.A.R.T.    |
|                                                    |
|  GPU:     Użycie, temperatura, VRAM, encoder       |
|           (NVIDIA via nvidia-smi)                   |
|                                                    |
|  Sieć:    Przepustowość in/out per interface,       |
|           pakiety, błędy, dropped                   |
|                                                    |
|  Usługi:  ethos.service status, Docker daemon,      |
|           Samba, NFS, SSH                           |
|                                                    |
|  Procesy: Top 10 CPU, Top 10 RAM                   |
|                                                    |
+---------------------------------------------------+
```

### 8.2. Progi Alertów

| Metryka            | Info     | Warning  | Critical | Akcja przy Critical              |
|--------------------|----------|----------|----------|----------------------------------|
| CPU usage          | >50%     | >80%     | >95%     | Identyfikacja procesu, alert     |
| RAM usage          | >60%     | >85%     | >95%     | OOM warning, zwolnij cache       |
| Disk usage         | >70%     | >85%     | >95%     | Auto-cleanup temp, alert admin   |
| Disk temperature   | >40C     | >50C     | >60C     | Throttle I/O, alert              |
| CPU temperature    | >60C     | >80C     | >90C     | Throttle freq, alert             |
| GPU temperature    | >65C     | >80C     | >90C     | Throttle, alert                  |
| Network errors     | >0.1%    | >1%      | >5%      | Interface diagnosis              |
| SMART warnings     | —        | >0       | —        | Natychmiastowy alert             |
| Service down       | —        | —        | dowolna  | Auto-restart, alert              |
| RAID degraded      | —        | —        | dowolna  | Alert + rebuild                  |

### 8.3. Kanały Powiadomień

```
1. WebUI Toast       — natychmiastowy, w przeglądarce
2. Taskbar Badge     — ikonka z licznikiem alertów
3. Email             — konfigurowalny w Settings -> Powiadomienia
4. Webhook           — POST na endpoint URL z JSON payload
5. Push (planowane)  — PWA push notifications
```

### 8.4. Health Check Endpoint

```
GET /api/health

Response 200:
{
  "status": "healthy",
  "uptime": 864000,
  "version": "2.4.1",
  "services": {
    "ethos": "running",
    "docker": "running",
    "samba": "running",
    "nfs": "stopped"
  },
  "storage": {
    "total_gb": 16000,
    "used_gb": 10720,
    "percent": 67
  }
}

Response 503:
{
  "status": "degraded",
  "issues": ["samba: crashed", "raid: degraded"]
}
```

### 8.5. Analiza Logów

```
LOKALIZACJE LOGOW:
  /var/log/ethos/            — logi głównej usługi
  /var/log/ethos/access.log  — HTTP access log
  /var/log/ethos/error.log   — błędy aplikacji
  journalctl -u ethos        — systemd journal

ROTACJA:
  logrotate — 7 dni daily, compress, max 500MB

WZORCE DO MONITOROWANIA:
  "CRITICAL"    — natychmiastowy alert
  "ERROR"       — agregacja, alert po >10/min
  "OOM"         — Docker/system OOM kills
  "RAID"        — jakiekolwiek zdarzenie RAID
  "auth failed" — brute-force detection (>5/min -> ban IP)
```

---

## 9. Checklist Przed Wydaniem

### Pre-Release Verification Checklist

#### Krok 1: Restart Usługi
- [ ] `sudo systemctl restart ethos`
- [ ] Usługa startuje w <10s
- [ ] Port 9000 odpowiada na HTTP GET /
- [ ] Port 443 odpowiada na HTTPS (jeśli skonfigurowany)
- [ ] `systemctl status ethos` -> active (running)
- [ ] Brak ERROR w `journalctl -u ethos --since "5 min ago"`

#### Krok 2: Auth Flow
- [ ] Strona logowania ładuje się poprawnie
- [ ] Logowanie z prawidłowymi credentials -> token w response
- [ ] Token 64-char hex format
- [ ] API request z Bearer token -> 200
- [ ] API request bez tokenu -> 401
- [ ] API request z nieprawidłowym tokenem -> 401
- [ ] Wylogowanie -> token invalidation

#### Krok 3: Operacje Plikowe
- [ ] Upload pliku (1MB, 100MB, 1GB)
- [ ] Download pliku -> porównaj checksum
- [ ] Kopiuj plik -> weryfikacja
- [ ] Przenieś plik -> weryfikacja
- [ ] Usuń plik -> potwierdzenie że usunięty
- [ ] Zmień nazwę pliku
- [ ] Utwórz folder
- [ ] File Manager breadcrumb nawigacja działa
- [ ] Share link -> share.html wyświetla plik

#### Krok 4: Storage
- [ ] Lista dysków w Storage Manager
- [ ] Mount/unmount dysku USB
- [ ] Status RAID (jeśli skonfigurowany)
- [ ] Disk usage wyświetla poprawne wartości
- [ ] S.M.A.R.T. dane dostępne

#### Krok 5: Docker
- [ ] Docker Manager ładuje listę kontenerów
- [ ] Start/stop kontenera
- [ ] Logi kontenera dostępne
- [ ] Compose stack deploy (z testowego YAML)
- [ ] App Store — lista aplikacji ładuje się

#### Krok 6: Backup/Restore
- [ ] Backup manualne -> kompletne
- [ ] Restore z backup -> dane zgodne
- [ ] Scheduled backup -> wykonuje się w czasie
- [ ] Snapshot create/list/delete (jeśli ZFS/Btrfs)

#### Krok 7: UI/UX
- [ ] Dark mode -> brak białego tekstu na białym tle
- [ ] Light mode -> brak ciemnego tekstu na ciemnym tle
- [ ] Wszystkie ikony Font Awesome ładują się
- [ ] Toast notifications wyświetlają się poprawnie
- [ ] Modal overlay zamyka się na Escape i kliknięcie tła
- [ ] Window drag & drop działa
- [ ] Window minimize/maximize/close działa
- [ ] Taskbar wyświetla otwarte aplikacje
- [ ] i18n: przełączenie języka na PL/EN/DE/FR/ES

#### Krok 8: Sieć
- [ ] Network Manager wyświetla interfaces
- [ ] WiFi scan (jeśli adapter dostępny)
- [ ] Samba share dostępny z Windows
- [ ] WebSocket reconnection po krótkim disconnect

#### Krok 9: Monitoring
- [ ] Monitor app wyświetla CPU/RAM/Disk/Net
- [ ] Temperatury wyświetlane (jeśli sensory dostępne)
- [ ] Health check endpoint odpowiada
- [ ] Alerty generują się przy przekroczeniu progów

#### Krok 10: Regression
- [ ] Setup wizard (na czystej instancji lub po reset)
- [ ] AI Chat odpowiada na pytania
- [ ] Terminal otwiera sesję PTY
- [ ] Gallery generuje thumbnails
- [ ] Document Editor -> save -> export DOCX/PDF

---

## 10. Znane Wzorce Błędów

### Bug Pattern #1: Duplikacja Atrybutu HTML Class

**Opis:** Element HTML zawiera dwa oddzielne atrybuty `class=""`. Przeglądarka parsuje
tylko PIERWSZY atrybut class, ignorując drugi. Skutek: brakujące style.

**Przykład:**
```html
<!-- BUG: drugi class ignorowany -->
<div class="container" class="dark-theme-panel">
  <!-- "dark-theme-panel" NIE jest zaaplikowane! -->
</div>

<!-- FIX: połącz klasy w jeden atrybut -->
<div class="container dark-theme-panel">
</div>
```

**Jak wykrywać:**
```bash
# Grep do wykrywania duplikatów
grep -rn 'class="[^"]*"[^>]*class="' *.html js/ --include='*.js' --include='*.html'
```

**Prewencja:**
- Code review: sprawdzaj concatenację stringów HTML w JS
- ESLint custom rule: wykrywaj pattern `class="..." ... class="`
- Testy E2E: sprawdzaj `element.classList.contains()` dla krytycznych klas

---

### Bug Pattern #2: Dark Theme Color Inheritance

**Opis:** Elementy z wymuszonym białym tłem (np. strona dokumentu, preview PDF) dziedziczą
kolor tekstu z dark theme -> jasny tekst na białym tle = nieczytelne.

**Źródło:**
```css
[data-theme="dark"] {
  --text-primary: #e4e4e8;  /* jasny tekst */
}

/* Problem: ten element ma białe tło ale dziedziczy jasny tekst */
.dte-page {
  background: #ffffff;
  /* brak color: ... -> dziedziczy var(--text-primary) = jasny = niewidoczny */
}
```

**Fix:**
```css
.dte-page {
  background: #ffffff;
  color: #1d1d1f;  /* ZAWSZE ustaw kolor gdy bg jest hardcoded */
}
```

**Prewencja:**
- **ZASADA:** Każdy element z hardcoded `background` MUSI mieć hardcoded `color`
- Visual regression tests: screenshot w dark mode, porównaj z baseline
- Checklist w design review: "Czy ten komponent wygląda OK w dark mode?"

---

### Bug Pattern #3: API Key Masking w Testach

**Opis:** Pole API key w Settings wyświetla maskowaną wartość (np. `sk-****...****abcd`).
Przy zapisie formularza maskowana wartość jest wysyłana na serwer jako nowy klucz,
nadpisując prawdziwy klucz.

**Przebieg bugu:**
```
1. Użytkownik otwiera Settings
2. API key field pokazuje: "sk-****...****abcd" (zamaskowany)
3. Użytkownik zmienia INNE ustawienie (np. język)
4. Kliknięcie "Zapisz" wysyła CAŁY formularz
5. Server otrzymuje masked value jako nowy API key
6. API key nadpisany -> integracja przestaje działać
```

**Fix:**
```javascript
// Przy wysyłaniu formularza — pomijaj zamaskowane pola
if (apiKeyField.value === apiKeyField.dataset.maskedValue) {
  delete formData.api_key;  // nie wysyłaj jeśli nie zmienione
}

// LUB: osobny endpoint do aktualizacji klucza
// PUT /api/settings/api-key (tylko gdy user jawnie zmienia)
```

**Prewencja:**
- Sensitive fields: oddzielny flow edycji (kliknij "Zmień" -> pole edytowalne)
- Backend: ignoruj wartości pasujące do maski `****`
- Test: zmień inne ustawienie -> sprawdź że API key nie zmieniony

---

### Bug Pattern #4: Brakujące Katalogi

**Opis:** Feature polega na istnieniu katalogu (np. `installer/images/`).
Katalog nie istnieje na świeżej instalacji -> feature unavailable bez czytelnego błędu.

**Przykład:**
```python
# Builder próbuje listować obrazy
images = os.listdir('/opt/ethos/installer/images/')
# -> FileNotFoundError na świeżej instalacji
```

**Fix:**
```python
import os
IMAGE_DIR = '/opt/ethos/installer/images/'
os.makedirs(IMAGE_DIR, exist_ok=True)  # utwórz jeśli nie istnieje
images = os.listdir(IMAGE_DIR)
```

**Prewencja:**
- `os.makedirs(path, exist_ok=True)` przy pierwszym dostępie
- Instalator: twórz WSZYSTKIE wymagane katalogi
- Test: fresh install -> uruchom każdą aplikację -> zero FileNotFoundError

---

### Bug Pattern #5: Wymóg Restartu Usługi

**Opis:** Po zmianie kodu/konfiguracji wymagany jest restart `ethos.service`.
Brak hot-reload = zmiany nie widoczne bez restart.

**Wpływ:**
- Development: frustracja, wolny cykl iteracji
- Production: krótki downtime przy każdej aktualizacji
- Surveillance: przerwa w nagrywaniu podczas restart

**Obecne Obejście:**
```bash
sudo systemctl restart ethos
```

**Rekomendowane Rozwiązania:**
1. **Konfiguracja hot-reload:** plik config -> watch -> reload bez restart
2. **Graceful restart:** stop accepting new connections -> finish active -> restart
3. **Zero-downtime deploy:** nowy process na innym porcie -> switch proxy -> kill stary
4. **Update notification:** frontend banner "Aktualizacja wymaga odświeżenia strony"

**Prewencja:**
- Oddziel konfigurację od kodu — config reload bez restart
- Implementuj SIGHUP handler — przeładowanie konfiguracji na sygnał
- Planuj okna serwisowe — restart nocą z powiadomieniem

---

### Podsumowanie Wzorców Błędów

| # | Wzorzec                     | Ryzyko   | Wykrywalność | Priorytet Naprawy |
|---|----------------------------|----------|-------------|-------------------|
| 1 | Duplikacja class           | Średnie  | Niska       | P2                |
| 2 | Dark theme inheritance     | Wysokie  | Średnia     | P1                |
| 3 | API key masking            | Krytyczne| Niska       | P1                |
| 4 | Brakujące katalogi         | Średnie  | Wysoka      | P2                |
| 5 | Wymóg restartu             | Niskie   | Wysoka      | P3                |

---

*Dokument jest żywą referencją — aktualizuj przy każdym nowym odkrytym wzorcu błędu.*
