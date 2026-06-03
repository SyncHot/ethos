#!/bin/bash
# =============================================================================
# EthOS - Quick Start Script
# Automatyczne uruchomienie systemu EthOS w Dockerze
# =============================================================================

set -e

# Kolory dla outputu
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Funkcje pomocnicze
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Sprawdź wymagania
check_requirements() {
    print_info "Sprawdzanie wymagań..."
    
    # Docker
    if ! command -v docker &> /dev/null; then
        print_error "Docker nie jest zainstalowany!"
        echo "Zainstaluj Docker: https://docs.docker.com/get-docker/"
        exit 1
    fi
    
    # Docker Compose
    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        print_error "Docker Compose nie jest zainstalowany!"
        echo "Zainstaluj Docker Compose: https://docs.docker.com/compose/install/"
        exit 1
    fi
    
    # Sprawdź czy Docker działa
    if ! docker info &> /dev/null; then
        print_error "Docker daemon nie działa!"
        echo "Uruchom: sudo systemctl start docker"
        exit 1
    fi
    
    print_success "Wszystkie wymagania spełnione ✓"
}

# Przygotuj konfigurację
setup_config() {
    print_info "Przygotowywanie konfiguracji..."
    
    if [ ! -f .env ]; then
        if [ -f .env.example ]; then
            cp .env.example .env
            print_success "Stworzono plik .env z domyślnymi ustawieniami"
        else
            print_warning "Brak pliku .env.example, używam domyślnych wartości"
        fi
    else
        print_info "Plik .env już istnieje"
    fi
    
    # Sprawdź czy użytkownik chce edytować konfigurację
    read -p "Czy chcesz edytować plik .env? (t/n) [n]: " edit_env
    if [[ "$edit_env" =~ ^[Tt]$ ]]; then
        ${EDITOR:-nano} .env
    fi
}

# Wybierz tryb uruchomienia
select_mode() {
    print_info "Wybierz tryb uruchomienia:"
    echo "  1) Produkcja (domyślne)"
    echo "  2) Deweloperski (hot reload)"
    echo "  3) Minimalny (tylko web UI)"
    
    read -p "Twój wybór [1]: " mode
    
    case $mode in
        2) MODE="dev" ;;
        3) MODE="minimal" ;;
        *) MODE="prod" ;;
    esac
    
    print_info "Wybrano tryb: $MODE"
}

# Buduj i uruchom
build_and_run() {
    print_info "Budowanie obrazu Docker..."
    
    case $MODE in
        prod)
            docker-compose build --no-cache
            ;;
        dev)
            docker-compose -f docker-compose.dev.yml build --no-cache
            ;;
        minimal)
            # Tylko podstawowe usługi
            docker-compose build ethos
            ;;
    esac
    
    print_success "Obraz zbudowany pomyślnie ✓"
}

# Uruchom kontenery
start_containers() {
    print_info "Uruchamianie kontenerów..."
    
    case $MODE in
        prod)
            docker-compose up -d
            ;;
        dev)
            docker-compose -f docker-compose.dev.yml up -d
            ;;
        minimal)
            docker-compose up -d ethos
            ;;
    esac
    
    print_success "Kontenery uruchomione ✓"
}

# Pokaż status i linki
show_status() {
    echo ""
    print_info "Status systemu:"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    case $MODE in
        prod)
            docker-compose ps
            ;;
        dev)
            docker-compose -f docker-compose.dev.yml ps
            ;;
        minimal)
            docker-compose ps ethos
            ;;
    esac
    
    echo ""
    print_success "System EthOS jest dostępny pod adresem:"
    echo "  🌐 Web UI: http://localhost:${ETHOS_PORT:-9000}"
    
    if [ "$MODE" != "minimal" ]; then
        echo "  🔑 SSH: ssh root@localhost -p ${SSH_PORT:-2222}"
    fi
    
    echo ""
    print_info "Przydatne komendy:"
    echo "  Logi:       docker-compose logs -f ethos"
    echo "  Zatrzymaj:  docker-compose down"
    echo "  Restart:    docker-compose restart"
    echo "  Shell:      docker-compose exec ethos bash"
    echo ""
}

# Główna funkcja
main() {
    echo "╔═══════════════════════════════════════════════════════╗"
    echo "║           EthOS - NAS Operating System               ║"
    echo "║              Docker Quick Start                       ║"
    echo "╚═══════════════════════════════════════════════════════╝"
    echo ""
    
    check_requirements
    setup_config
    select_mode
    build_and_run
    start_containers
    show_status
    
    print_success "Gotowe! System EthOS działa. 🎉"
}

# Uruchom skrypt
main "$@"
