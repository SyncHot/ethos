#!/bin/bash
# Check for hardcoded credentials and personal data (marcin)

ERROR=0

EXCLUDES="--exclude-dir=logs --exclude-dir=backups --exclude-dir=.git --exclude-dir=venv --exclude-dir=__pycache__ --exclude-dir=data --exclude-dir=frontend_dist --exclude=check_credentials.sh --exclude=*.log --exclude=*.md --exclude=*.json --exclude=install.conf --exclude=.env*"

# Check for '/home/marcin' in codebase (hardcoded path)
if grep -rI "/home/marcin" . $EXCLUDES 2>/dev/null; then
    echo "❌ Found '/home/marcin' in codebase (hardcoded path). Please fix."
    ERROR=1
fi

# Check for 'marcin' as username/password value
if grep -rIE "username['\"]?\s*[:=]\s*['\"]?marcin['\"]?|password['\"]?\s*[:=]\s*['\"]?marcin['\"]?" . $EXCLUDES 2>/dev/null; then
    echo "❌ Found 'marcin' as username/password. Please fix."
    ERROR=1
fi

if [ $ERROR -eq 1 ]; then
    echo "Security check FAILED."
    exit 1
else
    echo "Security check PASSED."
    exit 0
fi
