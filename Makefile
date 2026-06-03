# =============================================================================
# EthOS - Makefile for Docker Management
# Szybkie komendy do zarządzania kontenerami
# =============================================================================

.PHONY: help build up down restart logs shell clean backup restore dev prod

# Domyślna cecha
.DEFAULT_GOAL := help

# Kolory dla outputu
GREEN  := \033[0;32m
YELLOW := \033[1;33m
BLUE   := \033[0;34m
NC     := \033[0m # No Color

help: ## Pokaż pomoc
	@echo ""
	@echo -e "$(GREEN)╔═══════════════════════════════════════════════════════╗$(NC)"
	@echo -e "$(GREEN)║           EthOS Docker Management                     ║$(NC)"
	@echo -e "$(GREEN)╚═══════════════════════════════════════════════════════╝$(NC)"
	@echo ""
	@echo "Dostępne komendy:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""

build: ## Zbuduj obraz Docker
	@echo -e "$(BLUE)[BUILD]$(NC) Budowanie obrazu..."
	docker-compose build --no-cache
	@echo -e "$(GREEN)[OK]$(NC) Obraz zbudowany pomyślnie"

up: ## Uruchom kontenery w tle
	@echo -e "$(BLUE)[START]$(NC) Uruchamianie EthOS..."
	docker-compose up -d
	@echo -e "$(GREEN)[OK]$(NC) System dostępny pod http://localhost:9000"

down: ## Zatrzymaj kontenery
	@echo -e "$(BLUE][STOP]$(NC) Zatrzymywanie EthOS..."
	docker-compose down
	@echo -e "$(GREEN)[OK]$(NC) Kontenery zatrzymane"

restart: ## Restartuj kontenery
	@echo -e "$(BLUE)[RESTART]$(NC) Restartowanie EthOS..."
	docker-compose restart
	@echo -e "$(GREEN)[OK]$(NC) System zrestartowany"

logs: ## Pokaż logi (live)
	@echo -e "$(BLUE)[LOGS]$(NC) Podgląd logów:"
	docker-compose logs -f ethos

shell: ## Wejdź do shell kontenera
	@echo -e "$(BLUE)[SHELL]$(NC) Łączenie z kontenerem..."
	docker-compose exec ethos bash

status: ## Pokaż status kontenerów
	@echo -e "$(BLUE)[STATUS]$(NC) Status systemu:"
	docker-compose ps

clean: ## Usuń kontenery, obrazy i volumetry (UWAGA!)
	@echo -e "$(YELLOW)[CLEAN]$(NC) Czyszczenie wszystkich danych Docker..."
	@read -p "Czy na pewno chcesz usunąć WSZYSTKIE dane? (t/n): " confirm && \
		if [ "$$confirm" = "t" ]; then \
			docker-compose down -v --rmi all --remove-orphans; \
			echo -e "$(GREEN)[OK]$(NC) Zczyszczono"; \
		else \
			echo "Anulowano"; \
		fi

backup: ## Zrób backup danych
	@echo -e "$(BLUE)[BACKUP]$(NC) Tworzenie kopii zapasowej..."
	@mkdir -p docker-backups
	docker run --rm \
		-v ethos_data:/data \
		-v $(shell pwd)/docker-backups:/backup \
		alpine tar czf /backup/ethos-$(shell date +%Y%m%d_%H%M%S).tar.gz -C /data .
	@echo -e "$(GREEN)[OK]$(NC) Backup zapisany w docker-backups/"

restore: ## Przywróć z backupu (wymaga nazwy pliku)
	@if [ -z "$(FILE)" ]; then \
		echo "Użycie: make restore FILE=nazwa_pliku.tar.gz"; \
		exit 1; \
	fi
	@echo -e "$(BLUE)[RESTORE]$(NC) Przywracanie z $(FILE)..."
	docker run --rm \
		-v ethos_data:/data \
		-v $(shell pwd)/docker-backups:/backup \
		alpine tar xzf /backup/$(FILE) -C /data
	@echo -e "$(GREEN)[OK]$(NC) Dane przywrócone"

dev: ## Uruchom tryb deweloperski (hot reload)
	@echo -e "$(BLUE)[DEV]$(NC) Tryb deweloperski..."
	docker-compose -f docker-compose.dev.yml up --build

prod: ## Uruchom tryb produkcyjny
	@echo -e "$(BLUE)[PROD]$(NC) Tryb produkcyjny..."
	docker-compose up -d --build

inspect: ## Szczegóły kontenera
	@echo -e "$(BLUE)[INSPECT]$(NC) Szczegóły:"
	docker inspect ethos-server | jq '.[0] | {Name, State, Mounts, NetworkSettings}'

stats: ## Monitorowanie zasobów
	@echo -e "$(BLUE)[STATS]$(NC) Zużycie zasobów:"
	docker stats ethos-server --no-stream

health: ## Sprawdź health check
	@echo -e "$(BLUE)[HEALTH]$(NC) Status zdrowia:"
	docker inspect --format='{{.State.Health.Status}}' ethos-server || echo "Brak healthcheck"

pull: ## Pobierz najnowszy obraz z rejestru
	@echo -e "$(BLUE][PULL]$(NC) Pobieranie obrazu..."
	docker-compose pull

update: build down up ## Pełna aktualizacja (build + restart)

# Aliasy dla szybszego wpisywania
s: shell
l: logs
r: restart
b: backup
