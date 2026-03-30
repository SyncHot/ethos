# EthOS — Workflow Wdrożeniowy i Commit/Push

> Data: 2026-03-30 | Wersja: 1.0.75

---

## Spis Treści

1. [Dwa Repozytoria](#1-dwa-repozytoria)
2. [Cykl Deweloperski](#2-cykl-deweloperski)
3. [Commit i Push — Główne Repo](#3-commit-i-push--główne-repo)
4. [Sync ethos-apps — Opcjonalne Aplikacje](#4-sync-ethos-apps--opcjonalne-aplikacje)
5. [Builder a Repozytoria](#5-builder-a-repozytoria)
6. [Mapowanie Plików main - ethos-apps](#6-mapowanie-plików-main---ethos-apps)
7. [Testy E2E](#7-testy-e2e)
8. [Checklist Przed Deployem](#8-checklist-przed-deployem)
9. [Dziennik Zmian — Sesja 2026-03-30](#9-dziennik-zmian--sesja-2026-03-30)

---

## 1. Dwa Repozytoria

EthOS używa **dwóch repozytoriów GitHub**:

| Repo | Przeznaczenie | Kiedy używane |
|------|---------------|---------------|
| SyncHot/ethos | Główny OS — backend, frontend, installer, testy, builder | Builder kopiuje z tego repo na obraz. Dev server działa z tego repo. |
| SyncHot/ethos-os-ethos-apps | Pakiety opcjonalnych aplikacji (backend.py + frontend.js) | Package Center na **zdalnych maszynach** (slim images) pobiera apki stąd |

### Dlaczego dwa repo?

Builder tworzy **slim images** — usuwa opcjonalne backend .py (oszczędza miejsce). Gdy użytkownik instaluje apkę przez Package Center na zdalnej maszynie, Package Center pobiera backend.py + frontend.js z ethos-apps.

Jeśli ethos-apps nie jest zsynchronizowane, zdalne maszyny dostaną **stare wersje** apek.

---

## 2. Cykl Deweloperski

```
1. Edytuj pliki
2. tools/agent_helpers/check_syntax.sh        # Walidacja Python & JS
3. sudo systemctl restart ethos               # Restart backendu
4. Sprawdź w przeglądarce (localhost:9000)
5. rsync -av --delete frontend/ frontend_dist/ # Jeśli zmiany w frontend
6. Uruchom testy (pytest)
7. git add + commit + push                    # Do SyncHot/ethos
8. Sync do ethos-apps (jeśli zmienione opcjonalne apki)
```

---

## 3. Commit i Push — Główne Repo

### Konwencja commitów

```
feat:      nowa funkcjonalność
fix:       naprawa buga
refactor:  refaktoryzacja bez zmiany zachowania
security:  poprawki bezpieczeństwa
test:      nowe/zmienione testy
chore:     wersjonowanie, konfiguracja, CI
docs:      dokumentacja
perf:      optymalizacja wydajności
style:     formatowanie, CSS
```

### Przykład

```bash
git add -A
git commit -m "fix: Package Center streaming progress for apt/pip installs

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git push origin main
```

### Pre-commit hook

Hook sprawdza czy w staged plikach **nie ma hardkodowanych haseł/credentiali**. W komentarzach i docstringach używaj myuser/mypass zamiast prawdziwych danych.

---

## 4. Sync ethos-apps — Opcjonalne Aplikacje

### Kiedy syncować?

**Po każdym pushu do main**, jeśli zmieniono pliki należące do **opcjonalnych apek** (nie CORE).

### CORE_APPS (NIE syncować do ethos-apps)

```
dashboard, file-manager, storage-manager, terminal, system-settings,
users, updates, app-store, packages, event-log, network, services,
resource-monitor, backup, power, notifications, ssh-manager, naslink,
firewall, fail2ban
```

### Opcjonalne apki (DO syncowania)

```
ai-chat, antivirus, builder, cloud-backup, code-editor, cron,
disk-repair, doc-editor, docker-manager, domains-manager,
download-manager, duplicates, family-hub, gallery, printer, raid-lvm,
remote-log, rollback, sharing-dlna, sharing-ftp, sharing-nfs,
sharing-samba, sharing-sftp, sharing-webdav, sticky-notes,
surveillance, tickets, ups, usb-flasher, vm-manager, websites,
wireguard
```

### Procedura sync

```bash
# 1. Sklonuj ethos-apps
cd /tmp
git clone git@github.com:SyncHot/ethos-os-ethos-apps.git ethos-apps
cd ethos-apps
git config user.name "Your Name"
git config user.email "your@email"

# 2. Skopiuj zmienione pliki (przykład)
cp /opt/ethos/backend/blueprints/printer.py apps/printer/backend.py
cp /opt/ethos/frontend/js/apps/downloads.js apps/download-manager/frontend.js

# 3. Commit i push
git add -A
git commit -m "fix: sync app updates from main EthOS repo (v1.0.XX)"
git push

# 4. Cleanup
rm -rf /tmp/ethos-apps
```

### Szybkie sprawdzenie różnic

```bash
# Skrypt porównujący main repo z ethos-apps
for app_dir in /tmp/ethos-apps/apps/*/; do
  app=$(basename "$app_dir")
  case "$app" in
    download-manager) be="backend/blueprints/downloads.py";;
    remote-log)       be="backend/blueprints/remote_log_db.py";;
    sharing-*)        suffix="${app#sharing-}"; be="backend/blueprints/sharing_${suffix}.py";;
    vm-manager)       be="backend/blueprints/vm_manager.py";;
    *)                be="backend/blueprints/${app//-/_}.py";;
  esac
  [ -f "/opt/ethos/$be" ] && [ -f "$app_dir/backend.py" ] && \
    ! diff -q "/opt/ethos/$be" "$app_dir/backend.py" >/dev/null 2>&1 && \
    echo "DIFF: $app backend"
done
```

---

## 5. Builder a Repozytoria

| Typ buildu | Źródło plików | Potrzebny sync ethos-apps? |
|------------|--------------|---------------------------|
| **Builder image (VM/ISO)** | Kopiuje z /opt/ethos/ (lokalny dysk) | NIE — builder ma wszystko |
| **OTA update** | Tworzy tarball z /opt/ethos/ | NIE — tarball ma wszystko |
| **Package Center (zdalne)** | Pobiera z ethos-apps GitHub | TAK — musi być aktualne |

Builder (backend/blueprints/builder.py) kopiuje:
- backend/*.py, backend/blueprints/*.py, backend/middleware/*.py
- frontend/index.html, frontend/css/*.css, frontend/js/*.js, frontend/js/apps/*.js
- frontend/vendor/, frontend/mobile/, frontend/img/, frontend/manifest.json

Slim images usuwają opcjonalne backend/blueprints/*.py ale zachowują frontend JS.

---

## 6. Mapowanie Plików main - ethos-apps

| ethos-apps dir | Backend (main repo) | Frontend (main repo) |
|----------------|--------------------|--------------------|
| apps/antivirus/ | backend/blueprints/antivirus.py | frontend/js/apps/antivirus.js |
| apps/builder/ | backend/blueprints/builder.py | frontend/js/apps/builder.js |
| apps/download-manager/ | backend/blueprints/downloads.py | frontend/js/apps/downloads.js |
| apps/remote-log/ | backend/blueprints/remote_log_db.py | frontend/js/apps/remote-log.js |
| apps/vm-manager/ | backend/blueprints/vm_manager.py | frontend/js/apps/vm-manager.js |
| apps/sharing-dlna/ | backend/blueprints/sharing_dlna.py | frontend/js/apps/sharing.js (!) |
| apps/sharing-ftp/ | backend/blueprints/sharing_ftp.py | frontend/js/apps/sharing.js (!) |
| apps/sharing-nfs/ | backend/blueprints/sharing_nfs.py | frontend/js/apps/sharing.js (!) |
| apps/sharing-samba/ | backend/blueprints/sharing_samba.py | frontend/js/apps/sharing.js (!) |
| apps/sharing-sftp/ | backend/blueprints/sharing_sftp.py | frontend/js/apps/sharing.js (!) |
| apps/sharing-webdav/ | backend/blueprints/sharing_webdav.py | frontend/js/apps/sharing.js (!) |
| apps/{name}/ | backend/blueprints/{name_z_underscores}.py | frontend/js/apps/{name}.js |

(!) **Sharing**: Wszystkie warianty sharing-* mają **ten sam** frontend.js = sharing.js. Zmiana sharing.js wymaga skopiowania do WSZYSTKICH 6 katalogów.

---

## 7. Testy E2E

### Pliki testowe

| Plik | Zakres | Ilość testów |
|------|--------|-------------|
| tests/test_apps_health.py | Pulse — po jednym endpoincie z każdego blueprintu | ~61 |
| tests/test_e2e_apps.py | Funkcjonalne — CRUD, real operations | ~120 |
| tests/test_e2e_appstore.py | Package Center — 3 systemy pakietów | ~57 |
| tests/test_e2e_install_cycle.py | Install/uninstall cycle — 32 opcjonalne apki | ~34 |

### Uruchamianie

```bash
# Dev server (localhost:9000)
source venv/bin/activate
pytest tests/test_apps_health.py -v

# VM (localhost:9004)
ETHOS_USER=myuser ETHOS_PASS=mypass \
ETHOS_BASE_URL=http://localhost:9004 \
pytest tests/test_e2e_apps.py -v

# Pojedynczy test
pytest tests/test_e2e_install_cycle.py -k "sharing-nfs" -v

# Wszystkie testy naraz
pytest tests/ -v --timeout=600
```

### Oczekiwane wyniki

| Plik | Dev (9000) | VM (9004) |
|------|-----------|-----------| 
| test_apps_health.py | ~58 pass 3 skip | ~52 pass 9 skip |
| test_e2e_apps.py | ~120 pass 6 skip | ~109 pass 17 skip |
| test_e2e_appstore.py | ~25 pass 32 skip | zależy od zainstalowanych apek |
| test_e2e_install_cycle.py | n/a (wymaga Package Center) | ~33 pass 1 skip |

skip = blueprint nie załadowany lub zależność niedostępna

---

## 8. Checklist Przed Deployem

```
[ ] tools/agent_helpers/check_syntax.sh — brak błędów
[ ] sudo systemctl restart ethos — serwer wstaje
[ ] rsync -av --delete frontend/ frontend_dist/ — jeśli zmieniony frontend
[ ] pytest tests/test_apps_health.py — wszystko green
[ ] pytest tests/test_e2e_apps.py — wszystko green
[ ] git status — nic uncommitted
[ ] git push origin main — pushed
[ ] Jeśli zmienione OPCJONALNE apki: sync do ethos-apps
[ ] Jeśli zmienione version.json: commit chore: version bump
```

---

## 9. Dziennik Zmian — Sesja 2026-03-30

### Repozytoria

- **SyncHot/ethos** — 32 commity (9a82aac do 77198a6)
- **SyncHot/ethos-os-ethos-apps** — 3 commity synchro (84b3cc0, f99f92e, b8ddc38)

### Główne zmiany

| # | Commit | Opis |
|---|--------|------|
| 1 | 9a82aac | ClamAV antivirus — bugi, progress bar, UI |
| 2 | 45ecdf9 | Antivirus scan stuck on Starting (buffered stdout) |
| 3 | 72f33a6 | SSH hardening w builder images |
| 4 | 32fa72f | Samba sharing security (XSS, 0o777, subnet) |
| 5 | f509cdb | VM Manager — browse ISO from drives |
| 6 | 2f7f7f6 | Backup — button sizing, notification dedup |
| 7 | 4a85518 | Persistent notifications |
| 8 | f550d0f | File Manager — extract all archives, i18n |
| 9 | 68af299 | File Manager — disk analysis button |
| 10 | dab1c01 | Builder — UFW firewall + ethos.env permissions |
| 11 | 6243b0e | UFW firewall integration (auto-manage rules) |
| 12 | 14e22b3 | Firewall app — UI/UX rewrite |
| 13 | 0a39a29 | Builder — security scripts, build history |
| 14 | 386f15f | VM Manager — LAN IP |
| 15 | 4762e0c | api() content-type check + firewall error |
| 16 | 667672d | Update restart overlay |
| 17 | d6ff844 | Remove native confirm() dialogs |
| 18 | 75b0620 | Version bump 1.0.69 |
| 19 | ecc002c | Installer removes nasadmin, writes ETHOS_USER |
| 20 | fb1913e | Download manager torrent resume |
| 21 | c27eed7 | Hide root from user list |
| 22 | 5bd900c | Installer groups (ethos-admin, ethos-user) |
| 23 | 238ce2e | Installer import os fix, downloads clear only completed |
| 24 | e9789b1 | Installer chroot bind-mount /dev |
| 25 | d3c164d | Core app backends preserved in builder |
| 26 | e070ee7 | VM autostart, firewall presets LAN-only |
| 27 | 91394ae | E2E health check tests (61 testów) |
| 28 | d5ca340 | E2E functional tests (120 testów) |
| 29 | b88d5f0 | Core apps duplicate in catalog fix |
| 30 | b859a89 | Package Center streaming progress (apt/pip) |
| 31 | b4e41cb | E2E install/uninstall cycle tests (34 testy) |
| 32 | 77198a6 | Version bump 1.0.75 |

### Kluczowe naprawy techniczne

- **Streaming progress**: host_run_stream() zamiast blokującego host_run() w instalacji apt/pip
- **VM autostart**: flask_app=app w vm_autostart_boot() — greenlet context error
- **Catalog duplicate**: firewall/fail2ban w both core+optional — odfiltrowane
- **Buffered stdout**: antivirus i backup stuck na "Starting" — Python 8KB buffer
- **Pre-commit hook**: blokuje commity z prawdziwymi hasłami nawet w komentarzach
