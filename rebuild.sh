#!/bin/bash
cd "$(dirname "$0")"
./venv/bin/pip install --quiet --no-cache-dir -r backend/requirements.txt
sudo systemctl restart ethos
echo "EthOS przebudowany i uruchomiony"
