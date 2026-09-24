#!/usr/bin/env bash
# Installerar säkerhetsnätet: kör testerna och kopierar guard.py till ~/.claude/hooks/.
# Körs av användaren själv. Agenten är spärrad från att köra skriptet.
# Hook-blocket i settings.example.json läggs in i ~/.claude/settings.json en gång.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest -q  # installera aldrig en version med röda tester
mkdir -p ~/.claude/hooks
install -m 755 guard.py ~/.claude/hooks/guard.py
echo "Installerad: ~/.claude/hooks/guard.py"
