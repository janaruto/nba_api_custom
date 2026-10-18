#!/usr/bin/env bash
# Aufstellung für eine Matchup-Woche:   ./draft_app/weekly.sh 1      (oder: ./draft_app/weekly.sh --opponent "Brick City")
# Aktualisiert zuerst den ESPN-Verletzungsstatus, nutzt dann die native Python-Umgebung .venv im Repo.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
"$PY" draft_app/refresh_injuries.py > /dev/null 2>&1 || echo "(Verletzungsstatus nicht aktualisiert, nehme den letzten Stand)"
if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
  week="$1"; shift
  exec "$PY" draft_app/weekly_lineup.py --week "$week" "$@"
fi
exec "$PY" draft_app/weekly_lineup.py "$@"
