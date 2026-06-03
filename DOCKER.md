# 🐳 EthOS Docker Deployment Guide

Kompletny przewodnik po uruchomieniu systemu EthOS w kontenerze Docker.

---

## 📋 Spis Treści

1. [Wymagania](#-wymagania)
2. [Szybki Start](#-szybki-start)
3. [Konfiguracja Produkcji](#-konfiguracja-produkcji)
4. [Tryb Deweloperski](#-tryb-deweloperski)
5. [Struktura Plików](#-struktura-plików)
6. [Zmienne Środowiskowe](#-zmienne-środowiskowe)
7. [Volumetry i Dane](#-volumetry-i-dane)
8. [Sieć i Porty](#-sieć-i-porty)
9. [Bezpieczeństwo](#-bezpieczeństwo)
10. [Monitorowanie i Logi](#-monitorowanie-i-logi)
11. [Rozwiązywanie Problemów](#-rozwiązywanie-problemów)
12. [Aktualizacja](#-aktualizacja)

---

## 🔧 Wymagania

### Minimalne wymagania sprzętowe:
| Zasób | Minimum | Rekomendowane |
|-------|---------|---------------|
| CPU   | 2 rdzenie | 4+ rdzeni |
| RAM   | 2 GB    | 4+ GB |
| Dysk  | 10 GB   | 50+ GB (dane) |

### Wymagania systemowe:
- Docker Engine 20.10+
- Docker Compose v2+
- Linux host (zalecany dla pełnej funkcjonalności NAS)
- Dostęp do urządzeń (/dev/bus/usb, dyski)

---

## 🚀 Szybki Start

### 1. Przygotowanie konfiguracji

```bash
# Sklonuj repozytorium
git clone https://github.com/your-user/ethos.git
cd ethos

# Stwórz plik konfiguracyjny
cp .env.example .env

# Edytuj .env według potrzeb
nano .env
```

### 2. Budowa i uruchomienie

```bash
# Budowa obrazu Docker
docker-compose build

# Uruchomienie w tle
docker-compose up -d

# Sprawdzenie statusu
docker-compose ps

# Podgląd logów
docker-compose logs -f ethos
```

### 3. Pierwsze uruchomienie

Otwórz przeglądarkę i przejdź do:
- **Web UI:** http://localhost:9000
- **SSH:** ssh root@localhost -p 2222 (jeśli włączone)

---

## 🏭 Konfiguracja Produkcji

### Pełna konfiguracja `docker-compose.yml`

```yaml
version: '3.8'

services:
  ethos:
    build: .
    container_name: ethos-server
    restart: unless-stopped
    
    ports:
      - "9000:9000"     # Web UI
      - "2222:22"       # SSH
      - "445:445"       # Samba
      - "139:139"       # NetBIOS
    
    volumes:
      - ethos_data:/opt/ethos/data
      - ethos_logs:/opt/ethos/logs
      - /home:/home:rw
      - /media:/media:rw
      - /var/run/docker.sock:/var/run/docker.sock:ro
    
    environment:
      - NAS_NAME=MyNAS
      - TZ=Europe/Warsaw
    
    devices:
      - /dev/bus/usb:/dev/bus/usb
    
    privileged: true
    
    networks:
      - ethos-network

volumes:
  ethos_data:
  ethos_logs:

networks:
  ethos-network:
    driver: bridge
```

### Uruchomienie z gotowego obrazu

```bash
# Jeśli obraz jest już zbudowany
docker-compose up -d

# Lub bezpośrednio przez Docker
docker run -d \
  --name ethos-server \
  --restart unless-stopped \
  -p 9000:9000 \
  -v ethos_data:/opt/ethos/data \
  -e NAS_NAME=MyNAS \
  ethos-nas:latest
```

---

## 💻 Tryb Deweloperski

### Uruchomienie z hot reload

```bash
# Uruchom tryb deweloperski
docker-compose -f docker-compose.dev.yml up --build

# Hot reload działa automatycznie przy zmianach w kodzie
```

### Struktura deweloperska

```
ethos/
├── backend/              # Kod Python (mounted)
│   ├── app.py           # Główna aplikacja
│   └── blueprints/      # Moduły API
├── frontend/            # Statyczne pliki HTML/CSS/JS
├── Dockerfile.dev       # Obraz deweloperski
└── docker-compose.dev.yml
```

### Korzystanie z kontenera deweloperskiego

```bash
# Wejdź do shell kontenera
docker-compose -f docker-compose.dev.yml exec ethos-backend-dev bash

# Sprawdź logi w czasie rzeczywistym
docker-compose -f docker-compose.dev.yml logs -f ethos-backend-dev

# Zatrzymaj i usuń kontenery deweloperskie
docker-compose -f docker-compose.dev.yml down
```

---

## 📁 Struktura Plików

### Wewnątrz kontenera:

```
/opt/ethos/
├── backend/              # Kod aplikacji Python
│   ├── app.py           # Główny plik Flask
│   ├── requirements.txt # Zależności Python
│   └── blueprints/      # Moduły API
├── frontend/            # Interfejs webowy
│   ├── index.html       # Strona główna
│   ├── js/              # JavaScript
│   └── css/             # Style CSS
├── data/                # Dane aplikacji (volume)
│   ├── setup_done       # Marker konfiguracji
│   ├── users.json       # Użytkownicy
│   └── .thumb_cache/    # Miniaturki
└── logs/               # Logi systemu (volume)
    ├── auth.log        # Logi autoryzacji
    ├── access.log      # Logi dostępu
    └── ethos.log       # Główny log
```

### Volumetry hosta:

| Volume | Ścieżka w kontenerze | Opis |
|--------|---------------------|------|
| `ethos_data` | `/opt/ethos/data` | Dane aplikacji, ustawienia, miniaturki |
| `ethos_logs` | `/opt/ethos/logs` | Logi systemu |
| `ethos_backups` | `/opt/ethos/backups` | Kopie zapasowe |
| Host `/home` | `/home` | Katalogi domowe użytkowników |

---

## 🔧 Zmienne Środowiskowe

### Podstawowe zmienne:

```bash
# Nazwa systemu (widoczna w UI)
NAS_NAME=EthOS

# Strefa czasowa
TZ=Europe/Warsaw

# Port głównego interfejsu
PORT=9000

# Ścieżki wewnętrzne
ETHOS_ROOT=/opt/ethos
DATA_DIR=/opt/ethos/data
LOG_DIR=/opt/ethos/logs
```

### Opcjonalne zmienne:

```bash
# Dysk danych (zewnętrzny storage)
DATA_DISK=/mnt/data

# Tryb debugowania
DEBUG=false
FLASK_ENV=production

# Logowanie
LOG_LEVEL=INFO
LOG_FORMAT=json
```

---

## 💾 Volumetry i Dane

### Zarządzanie volumetrami

```bash
# Lista wszystkich volumetrów
docker volume ls | grep ethos

# Sprawdzenie zawartości volumetra
docker run --rm -v ethos_data:/data alpine ls -la /data

# Eksport danych
docker run --rm -v ethos_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/ethos-data-backup.tar.gz -C /data .

# Import danych
docker run --rm -v ethos_data:/data -v $(pwd):/backup alpine \
  tar xzf /backup/ethos-data-backup.tar.gz -C /data

# Usunięcie volumetra (UWAGA: trwale usuwa dane!)
docker volume rm ethos_data
```

### Backup i restore

```bash
# Pełny backup
docker-compose down
docker run --rm \
  -v ethos_data:/source/data \
  -v ethos_logs:/source/logs \
  -v $(pwd)/backup:/backup \
  alpine sh -c "tar czf /backup/ethos-full-backup.tar.gz -C /source ."

# Restore z backupu
docker run --rm \
  -v ethos_data:/dest/data \
  -v ethos_logs:/dest/logs \
  -v $(pwd)/backup:/backup \
  alpine sh -c "tar xzf /backup/ethos-full-backup.tar.gz -C /dest"
```

---

## 🌐 Sieć i Porty

### Domyślne porty:

| Port | Protokół | Usługa | Opis |
|------|----------|--------|------|
| 9000 | TCP | HTTP | Główny interfejs webowy |
| 22 | TCP | SSH | Zdalny dostęp (opcjonalnie) |
| 445 | TCP | Samba | Udostępnianie plików Windows |
| 139 | TCP | NetBIOS | Sieć Windows (legacy) |
| 80 | TCP | HTTP | Redirect do portu 9000 |
| 443 | TCP | HTTPS | Bezpieczny dostęp (jeśli skonfigurowane SSL) |

### Konfiguracja sieci:

```yaml
# docker-compose.yml
networks:
  ethos-network:
    driver: bridge
    ipam:
      config:
        - subnet: 172.20.0.0/16
  
  host-network:
    driver: host  # Dostęp do sieci hosta
```

### Dostęp z zewnątrz:

```bash
# Sprawdź otwarte porty
docker port ethos-server

# Test połączenia
curl http://localhost:9000/api/health

# SSH (jeśli włączone)
ssh -p 2222 root@localhost
```

---

## 🔒 Bezpieczeństwo

### Uprawnienia kontenera:

EthOS wymaga rozszerzonych uprawnień dla pełnej funkcjonalności NAS:

```yaml
cap_add:
  - SYS_ADMIN      # Montowanie/odmontowanie, loop devices
  - NET_ADMIN      # Zarządzanie siecią
  - DAC_OVERRIDE   # Ominięcie uprawnień plików
  - FOWNER         # Własność plików
  - SETUID         # Manipulacja UID
  - SETGID         # Manipulacja GID

security_opt:
  - apparmor:unconfined
  - seccomp:unconfined

privileged: true  # Tryb uprzywilejowany
```

### Rekomendacje bezpieczeństwa:

1. **Zmieniaj domyślne hasła** po pierwszym uruchomieniu
2. **Konfiguruj firewall** (UFW) przez interfejs webowy
3. **Włącz Fail2Ban** dla ochrony przed brute-force
4. **Regularnie aktualizuj** obraz Docker
5. **Rób backupy** danych regularnie

### Przykładowa konfiguracja UFW:

```bash
# Wewnątrz kontenera lub przez API EthOS
ufw allow 9000/tcp   # Web UI
ufw allow 22/tcp     # SSH (opcjonalnie)
ufw enable
```

---

## 📊 Monitorowanie i Logi

### Podgląd logów:

```bash
# Live logi kontenera
docker-compose logs -f ethos

# Ostatnie 100 linii
docker-compose logs --tail=100 ethos

# Logi z filtrem czasu
docker-compose logs --since=1h ethos

# Logi do pliku
docker-compose logs ethos > ethos-logs.txt
```

### Monitorowanie zasobów:

```bash
# Status kontenera
docker stats ethos-server

# Szczegóły kontenera
docker inspect ethos-server

# Health check status
docker inspect --format='{{.State.Health.Status}}' ethos-server
```

### Logi wewnątrz systemu:

```bash
# Wejdź do kontenera
docker-compose exec ethos bash

# Sprawdź logi
cat /opt/ethos/logs/auth.log      # Autoryzacja
cat /opt/ethos/logs/access.log    # Dostęp
tail -f /opt/ethos/logs/ethos.log # Główny log
```

---

## 🔧 Rozwiązywanie Problemów

### Kontener nie startuje:

```bash
# Sprawdź logi błędów
docker-compose logs ethos

# Sprawdź czy port jest zajęty
netstat -tulpn | grep 9000

# Zmień port w .env
ETHOS_PORT=9001
```

### Brak dostępu do dysków:

```bash
# Sprawdź czy urządzenia są zamontowane
docker exec ethos-server ls /dev/disk/by-id/

# Dodaj urządzenie do docker-compose.yml
devices:
  - /dev/sda:/dev/sda
```

### Problemy z USB:

```bash
# Sprawdź dostępność USB
docker exec ethos-server lsusb

# Upewnij się że /dev/bus/usb jest zamontowane
docker inspect ethos-server | grep -A5 Devices
```

### Reset konfiguracji:

```bash
# Zatrzymaj kontener
docker-compose down

# Usuń volumetry (UWAGA: usuwa wszystkie dane!)
docker-compose down -v

# Uruchom ponownie
docker-compose up -d
```

### Błędy uprawnień:

```bash
# Sprawdź właściciela plików na hoście
ls -la /home/

# Zmień uprawnienia (jeśli potrzebne)
sudo chown -R 1000:1000 /home/user-data
```

---

## 🔄 Aktualizacja

### Aktualizacja z kodu źródłowego:

```bash
# Pobierz najnowsze zmiany
git pull origin main

# Zbuduj nowy obraz
docker-compose build --no-cache

# Zatrzymaj stary kontener
docker-compose down

# Uruchom nowy kontener
docker-compose up -d

# Sprawdź czy wszystko działa
docker-compose logs -f ethos
```

### Aktualizacja z gotowego obrazu:

```bash
# Pobierz najnowszy obraz
docker pull ethos-nas:latest

# Zatrzymaj i usuń stary kontener
docker-compose down

# Uruchom z nowym obrazem
docker-compose up -d
```

### Backup przed aktualizacją:

```bash
# Zrób backup danych
docker run --rm \
  -v ethos_data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/pre-update-backup.tar.gz -C /data .

# Aktualizuj...
# Jeśli coś pójdzie nie tak, przywróć:
docker run --rm \
  -v ethos_data:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/pre-update-backup.tar.gz -C /data
```

---

## 📞 Wsparcie

### Przydatne komendy:

| Komenda | Opis |
|---------|------|
| `docker-compose up -d` | Uruchom w tle |
| `docker-compose down` | Zatrzymaj i usuń kontenery |
| `docker-compose restart` | Restartuj kontener |
| `docker-compose logs -f` | Podgląd logów |
| `docker-compose exec ethos bash` | Wejdź do shell |
| `docker-compose ps` | Status kontenerów |

### Linki:

- 📖 [Dokumentacja Docker](https://docs.docker.com/)
- 🐙 [Repozytorium EthOS](https://github.com/your-user/ethos)
- 💬 [Forum wsparcia](https://forum.ethos-nas.org)

---

## 📝 Licencja

MIT License - zobacz plik [LICENSE](./LICENSE) dla szczegółów.
