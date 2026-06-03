#!/bin/bash
# =============================================================================
# EthOS - Stop Script
# Bezpieczne zatrzymanie systemu EthOS w Dockerze
# =============================================================================

set -e

# Kolory
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Opcje z linii komend
FORCE=false
REMOVE_VOLUMES=false
SHOW_HELP=false

for arg in "$@"; do
    case $arg in
        --force|-f) FORCE=true ;;
        --remove-volumes|--volumes|-v) REMOVE_VOLUMES=true ;;
        --help|-h) SHOW_HELP=true ;;
    esac
done

if [ "$SHOW_HELP" = true ]; then
    echo "Użycie: $0 [opcje]"
    echo ""
    echo "Opcje:"
    echo "  --force, -f          Zatrzymaj natychmiast (bez graceful shutdown)"
    echo "  --remove-volumes, -v Usuń volumetry z danymi"
    echo "  --help, -h           Pokaż tę pomoc"
    exit 0
fi

# Sprawdź czy kontenery działają
if ! docker-compose ps --services 2>/dev/null | grep -q ethos; then
    print_warning "Kontenery EthOS nie są uruchomione"
    exit 0
fi

print_info "Zatrzymywanie systemu EthOS..."

# Zrób backup logów przed zatrzymaniem
BACKUP_DIR="./docker-backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

print_info "Tworzenie kopii zapasowej logów..."
docker-compose logs --tail=1000 ethos > "$BACKUP_DIR/ethos-logs.txt" 2>&1 || true
print_success "Logi zapisane w: $BACKUP_DIR/"

# Zatrzymaj kontenery
if [ "$FORCE" = true ]; then
    print_warning "Wymuszanie zatrzymania..."
    docker-compose down --remove-orphans
else
    print_info "Graceful shutdown (może zająć kilka sekund)..."
    docker-compose stop
    docker-compose rm -f
fi

# Usuń volumetry jeśli poproszono
if [ "$REMOVE_VOLUMES" = true ]; then
    print_warning "Usuwanie volumetrów z danymi!"
    read -p "Czy na pewno chcesz usunąć wszystkie dane? (t/n) [n]: " confirm
    if [[ "$confirm" =~ ^[Tt]$ ]]; then
        docker-compose down -v --remove-orphans
        print_success "Volumetry usunięte"
    else
        print_info "Anulowano usuwanie volumetrów"
    fi
fi

print_success "System EthOS został zatrzymany ✓"
echo ""
print_info "Podsumowanie:"
echo "  Logi backupu: $BACKUP_DIR/"
echo "  Dane zachowane w volumetrach Docker"
echo ""
print_info "Aby uruchomić ponownie:"
echo "  docker-compose up -d"
