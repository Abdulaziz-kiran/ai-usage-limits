#!/bin/bash
# <swiftbar.title>AI limits</swiftbar.title>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# Refresh the cached AI limits menu every five minutes.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
for PYTHON in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [[ -x "$PYTHON" ]]; then
        exec "$PYTHON" "$DIR/ai_limits.py" --menubar
    fi
done
exec python3 "$DIR/ai_limits.py" --menubar
