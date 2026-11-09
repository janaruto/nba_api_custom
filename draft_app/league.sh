#!/usr/bin/env bash
# Liga-Werkzeuge (die Kader werden vor jeder Auswertung live von ESPN geholt, sobald `login` einmal eingerichtet ist):
#   ./draft_app/league.sh login                                 einmalig: ESPN-Zugang für den automatischen Kader-Abruf einrichten
#   ./draft_app/league.sh waiver [--top 3] [--keep "A;B"]      die besten freien Spieler (Waiver) und wen du dafür droppen solltest
#   ./draft_app/league.sh waiver --drop "A"                    A soll gehen: wer ist der beste freie Ersatz?
#   ./draft_app/league.sh waiver --add "B"                     B will ich holen: wen soll ich droppen?
#   ./draft_app/league.sh waiver --add "B" --drop "A"          genau dieser Wechsel: wie ändert sich die Gewinnchance?
#   ./draft_app/league.sh plan                                  was jetzt am meisten bringt: Kategorien stärken, wen loswerden, Waiver und Trades
#   ./draft_app/league.sh all                                   Rosters aktualisieren + Rangliste + Plan + Trade-Ideen
#   ./draft_app/league.sh refresh | rank | suggest
#   ./draft_app/league.sh offer --team "Brick City" --asks "Jamal Murray"    jemand fragt nach deinem Spieler: was kannst du zurückverlangen?
#   ./draft_app/league.sh want "Alperen Sengun"                              du willst einen Spieler: Angebote, Stat-Veränderung, Nachricht
#   ./draft_app/league.sh want "Alperen Sengun" --give 2 --get 2             wie viele Spieler im Trade (Zahl oder Bereich 1-3; --get zählt den Wunschspieler mit),
#                                                                            --keep "A;B" nie anbieten, --skip "C" nicht mitverlangen, --top N Optionen je Kategorie
set -euo pipefail
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
if [[ "${1:-}" == "all" || "${1:-}" == "rank" || "${1:-}" == "suggest" || "${1:-}" == "offer" || "${1:-}" == "want" || "${1:-}" == "waiver" || "${1:-}" == "plan" ]]; then
  "$PY" draft_app/refresh_injuries.py > /dev/null 2>&1 || echo "(Verletzungsstatus nicht aktualisiert)"
fi
exec "$PY" draft_app/league_tools.py "$@"
