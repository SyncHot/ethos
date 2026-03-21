#!/bin/bash
# Check syntax of Python and JS files.
# Usage: check_syntax.sh [file1 file2 ...] — check specific files
#        check_syntax.sh                   — check all backend + tools
set -euo pipefail
ETHOS=/opt/ethos
ERRORS=0

check_py() {
    if python3 -c "import ast; ast.parse(open('$1').read())" 2>&1; then
        echo "  ✅ $1"
    else
        echo "  ❌ $1"
        ERRORS=$((ERRORS + 1))
    fi
}

check_js() {
    if node --check "$1" 2>&1; then
        echo "  ✅ $1"
    else
        echo "  ❌ $1"
        ERRORS=$((ERRORS + 1))
    fi
}

if [[ $# -gt 0 ]]; then
    # Check specific files
    for f in "$@"; do
        if [[ "$f" == *.py ]]; then
            check_py "$f"
        elif [[ "$f" == *.js ]]; then
            check_js "$f"
        else
            echo "  ⏭️ $f (unsupported type)"
        fi
    done
else
    # Check all Python in backend + tools
    echo "=== Python files ==="
    find "$ETHOS/backend" "$ETHOS/tools" -name '*.py' ! -path '*__pycache__*' | sort | while read -r f; do
        check_py "$f"
    done

    echo ""
    echo "=== JavaScript files ==="
    find "$ETHOS/frontend/js" -name '*.js' | sort | while read -r f; do
        check_js "$f"
    done
fi

if [[ $ERRORS -gt 0 ]]; then
    echo ""
    echo "❌ $ERRORS syntax error(s) found"
    exit 1
else
    echo ""
    echo "✅ All files OK"
fi
