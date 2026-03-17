# 🎯 EthOS Ticket Orchestrator — Workflow

> System automatycznej realizacji ticketów przez agentów Copilot.

## Architektura

```
┌─────────────────────────────────────────────────────────┐
│                  KANBAN BOARD (ETHOS)                    │
│                                                         │
│  Backlog → Do zrobienia → W trakcie → Review → Gotowe   │
│              ↑ user        ↑ orchestrator  ↑ user       │
│              dodaje        startuje        ocenia       │
│              tickety       agentów         rezultat     │
└─────────────────────────────────────────────────────────┘

Orchestrator (Copilot) — obserwuje board i:
  1. Bierze najwyższy priorytet z "Do zrobienia"
  2. Rozpoznaje typ agenta (FE, BE, UX, DevOps, QA, Security)
  3. Przesuwa ticket do "W trakcie"
  4. Deleguje pracę do sub-agenta
  5. Po zakończeniu przesuwa do "Review"
  6. User daje feedback → "Gotowe" lub powrót do "W trakcie"
```

## Priorytet = kolejność

| Priorytet  | Kolejność | Opis |
|-----------|-----------|------|
| `critical` | 1 (pierwszy) | Blokujący, natychmiastowa realizacja |
| `high`     | 2 | Ważny, następny w kolejce |
| `medium`   | 3 | Standardowy |
| `low`      | 4 (ostatni) | Nice-to-have |

## Rozpoznawanie typu agenta

Na podstawie prefiksu w tytule ticketu lub labels:

| Prefix / Label | Agent | Lessons Learned |
|---------------|-------|-----------------|
| `[FE]`, `frontend`, `ui`, `ux`, `css` | FE/UX Agent | `FE_LESSONS_LEARNED.md` + `UX_LESSONS_LEARNED.md` |
| `[BE]`, `backend`, `api`, `endpoint` | Backend Agent | `BE_LESSONS_LEARNED.md` |
| `[DevOps]`, `deploy`, `build`, `docker` | DevOps Agent | `DEVOPS_LESSONS_LEARNED.md` |
| `[SEC]`, `security` | Security Agent | `SECURITY_HARDENING.md` |
| `[QA]`, `test` | QA Agent | `QA_FAILOVER_PROTOCOLS.md` |
| `[DOCS]`, `dokumentacja` | Docs Agent | `APP_DEVELOPMENT_GUIDE.md` |

## CLI Tool

```bash
# Pokaż pełny board
python3 /opt/ethos/tools/ticket_orchestrator.py board

# Pokaż następny ticket do realizacji
python3 /opt/ethos/tools/ticket_orchestrator.py next

# Rozpocznij pracę (→ W trakcie)
python3 /opt/ethos/tools/ticket_orchestrator.py start <ticket_id>

# Oddaj do review (→ Review)
python3 /opt/ethos/tools/ticket_orchestrator.py review <ticket_id>

# Zaakceptowane (→ Gotowe)
python3 /opt/ethos/tools/ticket_orchestrator.py done <ticket_id>

# Do poprawy (→ W trakcie)
python3 /opt/ethos/tools/ticket_orchestrator.py rework <ticket_id>

# Dodaj komentarz
python3 /opt/ethos/tools/ticket_orchestrator.py comment <ticket_id> "Opis zmian..."
```

## Workflow agenta — krok po kroku

### Krok 1: Sprawdź board
```
python3 /opt/ethos/tools/ticket_orchestrator.py next
```

### Krok 2: Rozpocznij ticket
```
python3 /opt/ethos/tools/ticket_orchestrator.py start <id>
```

### Krok 3: Przeczytaj lessons-learned
Przed delegacją do sub-agenta, dołącz do promptu:
- Odpowiedni plik `docs/*_LESSONS_LEARNED.md`
- `docs/APP_DEVELOPMENT_GUIDE.md` (jeśli nowa app/feature)
- Opis ticketu + kontekst projektu

### Krok 4: Deleguj do sub-agenta
Prompt template dla sub-agenta:
```
Realizujesz ticket EthOS:
  Tytuł: {title}
  Opis: {description}
  Priorytet: {priority}

PRZED ROZPOCZĘCIEM przeczytaj:
  - /opt/ethos/docs/{ROLE}_LESSONS_LEARNED.md
  - /opt/ethos/docs/APP_DEVELOPMENT_GUIDE.md

Wymagania:
  - Wszystkie pliki wymagają sudo do zapisu
  - Po zmianach backend: sudo systemctl restart ethos
  - Frontend: bez restartu
  - Testuj zmiany przed zakończeniem
```

### Krok 5: Review
```
python3 /opt/ethos/tools/ticket_orchestrator.py review <id>
python3 /opt/ethos/tools/ticket_orchestrator.py comment <id> "Opis wykonanych zmian"
```

### Krok 6: Feedback od użytkownika
- ✅ "OK" → `done <id>`
- 🔄 "Poprawki" → `rework <id>` + komentarz z feedbackiem

## Reguły orchestratora

1. **Jeden ticket na raz** — nie startuj kolejnego zanim bieżący nie jest w Review lub Gotowe
2. **Lessons-learned obowiązkowe** — sub-agent MUSI przeczytać odpowiedni plik
3. **Komentarz po każdej akcji** — co zrobiono, jakie pliki zmieniono
4. **Commit po każdym tickecie** — osobny commit z ID ticketu w message
5. **Nie przenoś z Review** — tylko user decyduje o akceptacji lub odrzuceniu

---

## Ticket Watcher — usługa systemd

`ethos-ticket-watcher.service` działa jako usługa systemd, zapewniając ciągłe działanie po zamknięciu konsoli i automatyczny restart przy awarii.

### Zarządzanie usługą

```bash
# Status
sudo systemctl status ethos-ticket-watcher

# Start / Stop / Restart
sudo systemctl start ethos-ticket-watcher
sudo systemctl stop ethos-ticket-watcher
sudo systemctl restart ethos-ticket-watcher

# Włącz autostart przy bootowaniu
sudo systemctl enable ethos-ticket-watcher

# Wyłącz autostart
sudo systemctl disable ethos-ticket-watcher
```

### Logi usługi

Logi zapisywane są do:
- **Główny log**: `/opt/ethos/logs/ticket_watcher.log`
- **Logi per ticket (Copilot)**: `/opt/ethos/logs/copilot_tickets/<ticket_id>_<ts>.log`
- **Systemd journal**: `journalctl -u ethos-ticket-watcher -f`

```bash
# Podgląd live
tail -f /opt/ethos/logs/ticket_watcher.log

# Systemd journal (ostatnie 50 linii)
journalctl -u ethos-ticket-watcher -n 50 --no-pager
```

### Konfiguracja usługi

Plik jednostki: `/etc/systemd/system/ethos-ticket-watcher.service`

Kluczowe parametry:
- `Restart=on-failure` — automatyczny restart przy błędzie
- `RestartSec=10` — czeka 10s przed restartem
- `User=marcin` — uruchamiany jako użytkownik marcin
- `EnvironmentFile=/opt/ethos/ethos.env` — zmienne środowiskowe

Po zmianie pliku jednostki: `sudo systemctl daemon-reload`
