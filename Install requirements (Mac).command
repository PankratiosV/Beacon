#!/bin/bash
# Installs everything Beacon needs on macOS. Double-click once.
cd "$(dirname "$0")"
python3 -m pip install --upgrade -r installer/requirements-mac.txt
echo
echo 'Done. Double-click "Start Beacon (Mac).command" to run the app.'
read -n 1 -s -r -p "Press any key to close."
