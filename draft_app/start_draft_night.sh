#!/usr/bin/env bash
# Draft-Tag: startet den ESPN-Empfänger (bridge.py) und die App. Strg+C beendet beides.
#
#   ./draft_app/start_draft_night.sh            normal starten (Verletzungsstatus wird aktualisiert)
#   ./draft_app/start_draft_night.sh --reset    zusätzlich das Draft-Log leeren (vor dem echten Draft einmal!)
#
# Voraussetzung: die conda-Umgebung ist aktiv (conda activate nba-env) oder PYTHON=/pfad/zu/python ist gesetzt.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"
APP_PORT="${APP_PORT:-8502}"

if [[ ! -f draft_app/player_pool.csv || ! -f draft_app/league_meta.json ]]; then
  echo "player_pool.csv / league_meta.json fehlen: zuerst fantasy_draft_2026_27_v4_engine.ipynb ausführen." >&2
  exit 1
fi

if [[ "${1:-}" == "--reset" ]]; then
  echo '[]' > draft_app/draft_log.json
  rm -f draft_app/current_nomination.json draft_app/bridge_unresolved.json
  echo "Draft-Log geleert."
fi

"$PY" draft_app/refresh_injuries.py || echo "(Verletzungsstatus nicht geladen: die App nutzt den letzten Stand.)"

"$PY" draft_app/bridge.py &
BRIDGE_PID=$!
trap 'kill "$BRIDGE_PID" 2>/dev/null || true' EXIT INT TERM
sleep 1

echo
echo "App:        http://localhost:${APP_PORT}"
echo "Empfänger:  läuft (Token in draft_app/bridge_token.txt, schon im Tampermonkey-Skript)"
echo "Als Nächstes: ESPN-Draft-Room in Chrome öffnen, unten links muss das Badge 'Draft-Room erkannt' zeigen."
echo

"$PY" -m streamlit run draft_app/app.py --server.port "$APP_PORT"
