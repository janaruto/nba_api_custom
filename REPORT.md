# Bericht: Draft-Engine v4 für „Swiss See Ballers“ (Saison 2026-27)

Stand: 9. Oktober 2026 · Draft: 17. Oktober 2026, 20:00 · Autor der Analyse: Claude (Sonnet 5.5) im Auftrag von jgudda

Alle Zahlen in diesem Bericht stammen aus dem letzten vollständigen Lauf von
`fantasy_draft_2026_27_v4_engine.ipynb` (Ausgaben im Notebook gespeichert) oder aus den genannten
Hilfsskripten. Verweise stehen in eckigen Klammern und sind in Abschnitt 11 aufgelöst, z. B. **[N11]** = Notebook-Zelle 11.
Wo eine Aussage **nicht** aus dem Repo reproduzierbar ist, steht das dabei.

---

## 1. Zusammenfassung

* **Ergebnis in einem Satz:** Statt eine Kategorie-Strategie vorzugeben (v3: „welche Kategorien soll ich punten?“),
  optimiert v4 direkt die Wahrscheinlichkeit, einen Wochen-Matchup zu gewinnen (mindestens 6 von 10 Kategorien),
  und liefert zu jedem Spieler einen *Break-even-Preis*. Die App rechnet das nach jedem eingetragenen Pick neu.
* **Die Frage zu den negativ korrelierten Kategorien** (FG% gegen 3PM −48, FG% gegen FT% −41 **[N7]**): Die
  Korrelation stimmt, aber der Schluss „also muss man punten“ hielt der Prüfung nicht stand. Der beste Kader ist
  *breit* (rund 7,3–7,4 Kategorien pro Woche) und mischt Guards (3PM, FT%, AST, PTS) mit Bigs (REB, DD, BLK). FG% bleibt
  dabei bewusst etwa ein Münzwurf (48 %) **[N13]**. Abschnitt 6.
* **Was in v3 nicht stimmte** (sieben Punkte, mit Zahlen in Abschnitt 4): Verfügbarkeit wurde falsch geschätzt, der
  Simulator machte FG%/FT% künstlich zufällig, die Preiskurve war ein Annahme-Artefakt, der Teamkollegen-Effekt zählte doppelt,
  der Spielerpool hatte Lücken, und der „Robustheitstest“ konnte nicht scheitern.
* **Ergebnis der Simulationen** (umkämpfte 14-Team-Auktionen, 16 Läufe je Preiskurve **[N11]**): Der Optimierer gewinnt
  einen Wochen-Matchup mit 0,66–0,88, der beste feste Kategorie-Plan mit 0,53–0,67. Gegen schärfere Rivalen schrumpft der
  Vorsprung, ist aber da (0,76 bei 3 von 13 optimierenden Rivalen **[N12]**).
* **Grösste Einschränkung:** Die absoluten Zahlen (87 % Matchup-Sieg, 15,5-2,5 Saisonbilanz) sind **zu optimistisch**,
  weil Prognosefehler nicht in die Wochen-Streuung eingehen. Belastbar ist der *Vergleich* zwischen Kadern und Strategien,
  nicht die Zahl selbst. Abschnitt 9.
* **Status:** Alles ist neu (`draft_app/engine.py`, `auction_sim.py`, `tests/`, v4-Notebook, überarbeitete `app.py`).
  12 Tests laufen durch. Die App wurde im Browser mit den heutigen Daten geprüft.

---

## 2. Ausgangslage und Liga

Liga „Swiss See Ballers“ (ESPN, Saison 2027) **[L1]**: 14 Teams, Salary-Cap-Auktion mit $200 Budget, 13 Kaderplätze
(PG, SG, SF, PF, C, G, F, 3× UTIL = 10 Starter, dazu 3 Bank), maximal 4 Center, kein IR, wöchentlicher Lineup-Lock.
H2H *each category* mit zehn Kategorien: FG%, FT%, 3PM, REB, AST, STL, BLK, DD, PTS, PPM. Ein Matchup ist mit 6 von 10
Kategorien gewonnen, 5:5 zählt als halber Sieg (siehe offene Frage F1). Dein Team: Janartuo Ninjas.

Die ESPN-Regeln stehen an einer einzigen Stelle im Notebook (Zelle 2 **[N2]**) und werden von dort in `league_meta.json`
und damit in die App exportiert. Ändert sich eine Regel, ändert man sie nur dort und lässt das Notebook neu laufen.

## 3. Was gebaut wurde

| Datei | Zweck |
|---|---|
| `draft_app/engine.py` | Analytische Kader-Bewertung (Wochen-Mittel und -Varianz sind Summen über die Starter, P(Kategorie) per Normalverteilung, P(Matchup) per Poisson-Binomial), Kader-Optimierer, Break-even-Gebote, Live-Preisniveau |
| `draft_app/auction_sim.py` | Simulierte Auktion mit 14 Bietern, Bootstrap-Wochensimulator aus echten Spielen, Bieter-Typen (fester Plan oder Optimierer) |
| `draft_app/app.py` | Streamlit-Oberfläche für den Draft-Abend: Pick eintragen, Spieler nachschlagen („Zahle bis …“), Zielkader |
| `draft_app/tests/test_engine.py` | 12 Tests (Wahrscheinlichkeiten, Slot-Zuordnung, Center-Grenze, Optimierer-Nebenbedingungen, Preis-Konsistenz) |
| `fantasy_draft_2026_27_v4_engine.ipynb` | Datenaufbereitung, Projektionen, Validierung, Simulationen, Export für die App |
| `REPORT.md` | dieser Bericht |

Start: `streamlit run draft_app/app.py`, Tests: `pytest draft_app/tests`.

---

## 4. Befunde zu v3, mit Belegen

Jeder Punkt wurde gegen Daten geprüft, nicht gegen Intuition.

| # | v3-Annahme | Messung | Beleg |
|---|---|---|---|
| 1 | Verfügbarkeit = Mittel der Spiele der letzten zwei Saisons, dann hoch 1,35 | Das Mittel sagt die Spiele der Folgesaison *schlechter* voraus (RMSE 0,263) als eine Konstante für alle (0,230). Kalibriert: 0,219. Wer 75+ Spiele hatte, spielt im Folgejahr im Schnitt 63, wer unter 30 hatte, 48. 513 Spieler-Saisons. | **[N6B]** |
| 2 | Simulator zieht jede Boxscore-Grösse unabhängig normalverteilt | Wöchentliche Team-FT%-Streuung 0,223 statt echten 0,047 (4,7×), FG% 2,0×, DD 0,8×. Mittelwerte zu hoch: DD +0,76, BLK +0,69, STL +0,48. | **[N9]** |
| 3 | Markt = ESPN-Werte × 1,58 für alle | ESPNs Kurve gilt für kleinere Ligen. Bei 14 Teams steigt vor allem das Mittelfeld. Jokić: ESPN $85, uniform $128, strukturell $108, Modellwert $67. Der beste Plan wechselt mit der Kurve (Beleg unten). | **[N8]**, **[X1]** |
| 4 | Kader über Summe der z-Werte für einen festen Kategorie-Satz | Direkte Optimierung des echten Ziels gewinnt in denselben Auktionen deutlich häufiger (Abschnitt 5). | **[N11]** |
| 5 | Teamkollegen-Effekt nach *Niveau* des neuen Umfelds | Die *Veränderung* gegenüber dem Vorjahresumfeld erklärt 3,6-mal mehr Varianz (R² 0,067 gegen 0,018). Sind beide im Modell, ist der Koeffizient des Niveaus +0,006, also null. Beispiel: Jokić bekam +12 % für ein Umfeld, das er schon kannte. | **[X2]** (ad hoc, nicht im Repo) |
| 6 | Pool = Spieler mit mindestens 15 Spielen 2025-26 | Sieben verletzte Rückkehrer (u. a. Haliburton, ESPN $39) und fünf Rookies fehlten. Ein aufgerufener Spieler hätte sich nicht eintragen lassen, und die Geldbilanz im Raum wäre verrutscht. | **[N8A]** |
| 7 | „Robustheit“: mein Team wählt zuerst aus dem vollen Pool | Der Test konnte nicht scheitern. Ersetzt durch umkämpfte Auktionen mit Rivalen, schärferen Rivalen und falschem Markt. | **[N11]**, **[N12]** |

**Zu Punkt 3 (der Plan wechselt mit der Preiskurve).** Ranking der v3-Kategorie-Bündel in `exp_e1.log` **[X1]**: Bei der
uniformen Kurve gewinnt der Fünf-Kategorien-Punkt REB+AST+DD+PTS+PPM (0,70). Bei der strukturellen Kurve gewinnt ein
Sechser-Plan 3PM+REB+AST+STL+BLK+PTS (0,84), und bei der Modellkurve REB+AST+STL+BLK+PPM (0,80). Ein „bester Plan“ ist also
ein Artefakt der angenommenen Preise.

**Was geprüft und beibehalten wurde:** Saisonmix 70/30 (das Backtest-Optimum liegt laut Skript bei 0,75–0,8 und ist flach),
Alterskurve (nur ab 35 Jahren −9 % statt −20 %), ESPN-Positionen, Center-Grenze. Diese Backtests liefen als einmalige Skripte
und stehen im Notebook nur als Kommentar in Zelle 6 (siehe Abschnitt 9, Punkt 7).

---

## 5. Methode in Kürze

1. **Wochen-Verteilung.** Alles, was ein Lineup in einer Woche tut, ist eine Summe über zehn Starter. Spiele pro Woche und
   Verfügbarkeit folgen einem Binomialmodell. Für Quoten (FG%, FT%, PPM) zählt das Team-Aggregat ΣFGM/ΣFGA, also gewichten
   Spieler mit vielen Versuchen mehr. Ein 92-%-Schütze mit 1 FTA pro Spiel bewegt Team-FT% kaum.
2. **Matchup-Wahrscheinlichkeit.** P(Kategorie) per Normalverteilung gegen einen Durchschnittsgegner. P(Matchup) aus den
   zehn Einzelwahrscheinlichkeiten, 5:5 mit halbem Sieg. Weil Kategorien in einer Woche zusammen schwanken, ist das Rohprodukt
   zu extrem. Eine Kalibrierung `logit(sim) = 0,66 · logit(roh)` korrigiert das. Der Fit auf die Simulation ergibt 0,655 **[N10]**.
3. **Validierung.** 90 Kader, bewertet von Engine und Bootstrap-Simulator aus echten Spielen: Korrelation der erwarteten Kategorien
   0,9998 (RMSE 0,044), P(Matchup) nach Kalibrierung RMSE 0,017 **[N10]**.
4. **Optimierer.** Lagrange-Preis auf das Budget, danach Austausch-Suche (steilster Anstieg) mit drei Startpunkten.
   Rechtlichkeit (Positionen, höchstens 4 Center) per Zuordnungsproblem geprüft.
5. **Break-even-Gebot.** Der höchste Preis, bei dem der Kader mit dem Spieler noch so gut ist wie der beste Kader ohne ihn.
   Gedeckelt durch Restbudget minus $1 je offenem Slot.
6. **Preisniveau live.** Aus Geldbilanz: Alles Restgeld im Raum wird für die noch offenen Spieler ausgegeben. Eine Anpassung
   der Preisform nach Preisstufe wurde getestet und bringt nichts (0,881 gegen 0,876 und 0,889 gegen 0,879 **[N12]**), also
   bewegt sie die Gebote nicht, sie wird nur angezeigt.
7. **Bank** (`engine.bench_suggestions`): Für die drei Bankplätze nach dem Zielkader. Ohne IR und mit wöchentlicher Aufstellung bleibt der Platz eines
   ausgefallenen Starters leer, wenn niemand einspringt. Für jeden Starter s (Gewicht = 1 − Verfügbarkeit) wird die Matchup-Chance der Aufstellung ohne s mit der
   Aufstellung mit dem Kandidaten c an dessen Stelle verglichen; der Wert von c ist die gewichtete Summe dieser Zuwächse (Positionen und Center-Limit müssen
   passen). Die Bankplätze werden gierig so gewählt, dass sie verschiedene Starter abdecken (ein späterer Spieler zählt, was er über die Hälfte des bereits
   Abgedeckten hinaus bringt, `overlap = 0,5`: eine Annahme). Das ist eine Rangfolge erster Ordnung, keine Gewinnprognose; die App zeigt deshalb einen relativen Index.

---

## 6. Zu den negativ korrelierten Kategorien

Aus der Korrelationstabelle **[N7]** (Werte ×100):

* **Zwei Lager.** *Bigs:* FG%, REB, BLK, DD hängen zusammen (DD/REB 82, REB/BLK 64, FG%/REB 53). *Guards:* PTS, PPM, 3PM, AST,
  FT% hängen zusammen (PTS/PPM 91, PTS/3PM 62, 3PM/FT% 60).
* **Gegenpole.** FG%/3PM −48, FG%/FT% −41, REB/FT% −26, BLK/FT% −28, BLK/3PM −20.
* **Konzentrationstabelle (EDGE_TOP3, GINI).** Sie sagt, wie viele z-Werte die Top-Spieler je Kategorie bringen: BLK 5,3, DD 4,8, STL 4,1
  gegen 3PM 3,0, PTS 3,2, REB 3,6. Das ist ein Kostenmass *in z-Einheiten* und kennt weder Preise noch Korrelationen. Es ist
  deshalb Beschreibung, kein Plan.

**Was der Optimierer daraus macht.** Er baut echte Kader und sieht die Gegenpole automatisch. Der Zielkader bei Board-Preisen
(strukturelle Kurve, $192, 7,34 Kategorien pro Woche **[N13]**) mischt Guards (Murray, Ball) mit Bigs (Towns, Holmgren, Ware).
Die Kategorie-Gewinnchancen: REB 0,88, PTS 0,87, 3PM 0,84, PPM 0,84, DD 0,80, FT% 0,74, AST 0,73, BLK 0,64, STL 0,53, FG% 0,48.
FG% bleibt der Preis dafür, dass man 3PM und FT% mitnimmt. Der Optimierer gibt ihn *teilweise* ab, ohne einen festen Punkt-Plan.

**Verschiedene Strategien im Vergleich** (Wochen-Matchup-Gewinn, strukturelle Kurve, dieselben Auktionen **[N11]**):

| Strategie | P(Matchup) | Kategorien/Woche |
|---|---|---|
| Optimierer (v4) | 0,87 | 7,33 |
| Balanced, alle 10 | 0,67 | 6,00 |
| Punkt auf REB+AST+DD+PTS+PPM (5 Kategorien) | 0,61 | 5,58 |
| 7 Kategorien 3PM+REB+AST+STL+BLK+DD+PTS | 0,53 | 5,14 |
| Guard-Punt PTS+PPM+AST+STL+3PM+FT% | 0,46 | 4,78 |

Feste Pläne verlieren, weil sie im Wettbieten dieselben Stars kaufen wie alle anderen. Der Optimierer weicht dagegen auf
Spieler aus, die der Markt zu billig lässt.

---

## 7. Ergebnisse im Detail

**Umkämpfte Auktionen, P(Wochen-Matchup-Gewinn)** (16 Auktionen je Zelle, Standardfehler 0,004–0,006 beim Optimierer **[N11]**):

| Preiskurve | Optimierer | bester fester Plan |
|---|---|---|
| UNIFORM | 0,883 | 0,637 (balanced) |
| STRUCTURAL | 0,871 | 0,665 (balanced) |
| MODEL | 0,658 | 0,531 (5-Kategorien-Punkt) |

Der „balanced“-Bieter gibt bei der Modellkurve nur $97 aus (0,189) und taugt dort nicht als Vergleich.

**Stresstests** **[N12]**: 0 optimierende Rivalen 0,884, 3 von 13 Rivalen mit demselben Optimierer 0,755. Ich glaube eine falsche Kurve
(Raum zahlt STRUCTURAL, ich glaube UNIFORM oder umgekehrt): 0,88–0,89, das Preisniveau-Lernen genügt.

**Saison-Simulation** **[N14]**: Eine umkämpfte Auktion, danach 18-Wochen-Saisons für alle 14 Kader: Janartuo Ninjas 15,5-2,5
(10.–90. Perzentil 14-4 bis 17-1), nächstbester Kader 11,8-6,2. **Das ist ein Wert für „Projektionen stimmen und der Raum verhält sich wie
die simulierten Bots“, keine Prognose.**

**Kauf-Ziele** (Häufigkeit auf meinem Kader über die simulierten Auktionen, strukturelle Kurve, **[N13]**): Murray 94 % (Ø $37),
Ware 88 % (Ø $3), Towns 81 % (Ø $55), Duren 81 % (Ø $22), J. Brown 75 % (Ø $25), Holmgren 69 % (Ø $9), Bane 63 % (Ø $11).
„Zahle bis“ deutlich über Board: Keyonte George (Board $10, bis $26), Edgecombe (5 → 16), Anunoby (7 → 17), Ware (3 → 13).
Laufenlassen: Giannis ($85, Break-even $0), Sabonis ($60, $0), Tatum ($75, $20), Trae Young. Das ist keine Aussage über ihre
Qualität, sondern darüber, dass sie bei dem erwarteten Preis nicht in den Kader passen. Giannis, Sabonis und Tatum schlagen mit
geringer erwarteter Spielzahl zu Buche (49, 44, 44 Spiele **[N8]**).

---

## 8. Pro und Kontra

### Direkte Optimierung statt Kategorie-Plan

| Pro | Kontra |
|---|---|
| Deckt die Gegenpole der Kategorien automatisch ab, ohne dass jemand den Plan raten muss | Ergebnis hängt an den Projektionen: kleine Fehler können das gesamte Gebotsverhalten verschieben |
| Liefert zu jedem Spieler einen Preis, nicht nur eine Rangliste | Break-even-Preise sind nur so gut wie der angenommene Rest des Marktes |
| Passt sich nach jedem Pick an (Restbudget, Center-Zahl, verbleibende Spieler) | Der Kader kann stark von der Vorab-Skizze abweichen. Das ist gewollt, wirkt aber im Moment des Bietens ungewohnt |
| In Simulationen +13 bis +25 Prozentpunkte gegenüber festen Plänen **[N11]** | Sind 3 von 13 Rivalen ebenfalls Optimierer, fällt der Wert von 0,88 auf 0,76 **[N12]** (kein Vergleich mit festen Plänen im selben Test) |

### Breiter Kader statt Punt

| Pro | Kontra |
|---|---|
| Wöchentliche Varianz sinkt, weil nicht alles an wenigen Kategorien hängt | Mehr Kategorien im Bereich 50–60 % heissen: viele Wochen entscheiden sich knapp |
| Weniger abhängig davon, dass die Konkurrenz bestimmte Kategorien nicht ebenfalls bevorzugt | FG% bleibt ein Münzwurf, auch wenn der Rest sehr gut ist |

### Die Live-App

| Pro | Kontra |
|---|---|
| Ein Blick nach dem Aufruf: Obergrenze, Aktion (BUY/skip), Preisprognose | Du musst jeden Pick von Hand eintragen. Ein vergessener Pick verzerrt Preisniveau und Kader |
| Tests für die Kernlogik (12), Konsistenzprüfung gegen Simulator | Die Oberfläche ist nur manuell im Browser getestet, nicht automatisiert |
| Preisniveau-Lernen reichte in den Stresstests mit falscher Preiskurve | Preisform-Lernen brachte keinen Gewinn, daher nur Anzeige |

---

## 9. Grenzen und Risiken

1. **Absolute Zahlen sind optimistisch.** Der walk-forward-Backtest zeigt einen Prognosefehler pro Spiel von etwa einer halben
   Streuung zwischen Spielern. Er geht nicht in die Wochen-Streuung ein.
2. **Die Rivalen sind Bots.** Sie bieten marktnah mit einem Aufschlag (Abschöpfung 0,6). Echte Manager verhalten sich anders, etwa
   emotionaler bei Stars oder bewusst gegen Konsens. Eine Liga mit mehr scharfen Bietern senkt den Vorsprung (siehe 0,76).
3. **Kalibrierung gegen sich selbst.** Der Faktor 0,66 wurde an meinem Bootstrap-Simulator angepasst, nicht an echten Fantasy-Matchups.
   Das ist nicht unabhängig geprüft.
4. **Verletzungen und Positionen** stammen aus dem Cache vom 9. Oktober. Vor dem Draft prüfen und das Notebook neu laufen lassen.
5. **Nominierungs-Strategie fehlt.** Die App sagt, wie viel ein Spieler wert ist, aber nicht, wen man wann aufrufen soll.
6. **Datenquellen sind inoffiziell.** stats.nba.com drosselt Anfragen (daher gestaffelt, mit Cache und Wiederholung). Die ESPN-API ist
   nicht dokumentiert und kann sich ändern.
7. **Teile der Belege sind nicht aus dem Repo reproduzierbar.** Die Backtests zu Saisonmix, Alterskurve und Teamkollegen-Änderung
   liefen als einmalige Skripte im Scratchpad. Die Zahlen aus dem Teamkollegen-Test stehen in Abschnitt 4, im Repo sind sie nicht enthalten. Wer sie prüfen will, braucht dafür ein eigenes Skript (siehe M1).
8. **Ausgaben nur bis zum Datenstand.** Spieler, die zwischen dem 9. und 17. Oktober verletzt oder getradet werden, fehlen.

---

## 10. Draft-Abend: Vorgehen

**Am Tag vorher (einmalig, rund 15 Minuten):**
1. Notebook neu laufen lassen (rund 9 Minuten): `fantasy_draft_2026_27_v4_engine.ipynb`. Das aktualisiert Rosters, Trades und die Projektionen.
2. Chrome: Tampermonkey mit dem Skript `draft_app/espn_bridge.user.js` (Version 0.2, nur ein Eintrag im Dashboard) und die Chrome-Einstellung „Benutzerskripts zulassen“.

**Am Draft-Abend (zwei Minuten vor Beginn):**
```bash
conda activate nba-env
./draft_app/start_draft_night.sh --reset
```
Das Skript aktualisiert den ESPN-Verletzungsstatus (`refresh_injuries.py`), leert das Draft-Log (`--reset`, nur vor dem echten Draft), startet den Empfänger und die App (`http://localhost:8502`). Strg+C beendet beides. Dann den ESPN-Draft-Room in Chrome öffnen: unten links muss das Badge „Draft-Room erkannt ✓“ zeigen.

**Verletzte Spieler.** Hat ESPN einen Spieler auf OUT gesetzt (aktuell 13, u. a. Ingram), zieht das Modell 25 erwartete Spiele ab (`availability.py`, `OUT_GAMES_LOST`). Das ist eine Annahme, keine Messung. In der App kannst du unter „Verletzung / erwartete Spiele anpassen“ für einzelne Spieler die Spiele selbst festlegen. Wer täglich fraglich ist, wird nicht angepasst. Nachrichten bis kurz vor dem Draft holst du mit `python draft_app/refresh_injuries.py`, die App liest die Datei ohne Neustart.

**Im Draft:**
1. Bei jedem Aufruf zeigt die App oben den Spieler, das aktuelle Gebot, dein Limit und BIETEN / AUSSTEIGEN. Nicht über das Limit gehen. Es enthält bereits $1 pro offenen Slot.
2. Verkäufe kommen automatisch aus dem Raum. Bei Fehlern gilt die manuelle Eingabe: Team antippen, Preis prüfen (vorbelegt mit der Prognose), „Eintragen“. Ruft der Raum einen anderen Spieler zuerst auf, wählst du ihn oben bei „Anderer Spieler aufgerufen?“. „Letzten Pick zurücknehmen“ korrigiert eine Fehleingabe, aber Achtung: Lädt man die Draft-Room-Seite neu, trägt der Empfänger zurückgenommene Picks wieder ein.
3. Hast du den Zielkader zusammen, zeigt der Abschnitt „Bank-Vorschläge“, wen du für die offenen Bankplätze holen sollst (meist \$1). Beim Aufruf eines solchen Spielers steht „Bank-Kandidat“ mit dem Bank-Limit.
4. Meldet der Empfänger einen Verkauf, den er nicht zuordnen kann (Spieler nicht im Pool), erscheint eine Warnung: von Hand eintragen.
5. Preisniveau beobachten: Steht es dauerhaft über 1,0×, zahlt der Raum mehr als das Board. Dann werden die späteren Preise steigen und
   der Optimierer rechnet das ein.
6. Nach rund 20 Picks lohnt ein Blick auf den Zielkader: weicht er stark vom Anfangs-Kader ab, ist das Absicht, nicht ein Fehler.
7. Centers im Auge behalten (Maximum 4). Die App berücksichtigt das, aber ein Center mehr in Reserve engt die Optionen ein.

### Anbindung an den ESPN-Draft-Room (getestet im ESPN-Übungsdraft, 11. Oktober)

Ziel: Die App erfährt den aufgerufenen Spieler, das aktuelle Gebot und jeden Verkauf automatisch, du musst nur noch in ESPN bieten.
Die Anbindung liest nur mit, sie klickt nichts in ESPN und bietet nicht für dich.

* **Bausteine.** `draft_app/espn_bridge.user.js` (Userscript für Tampermonkey, läuft in deinem eingeloggten Browser-Tab) →
  `draft_app/bridge.py` (lokaler Empfänger auf 127.0.0.1:8765, Token in `draft_app/bridge_token.txt`) → Dateien, die die App alle 1,5 Sekunden prüft
  (`draft_log.json`, `current_nomination.json`). Die App zeigt dann unter „Jetzt aufgerufen“ den Spieler aus dem Raum und dazu **BIETEN bis $X**,
  **AUSSTEIGEN** oder **DU FÜHRST**, je nach aktuellem Gebot und deinem Limit.
* **Gemessen an ESPNs echtem Draft-Room.** ESPN bietet in der Mock-Draft-Lobby einen „League Specific Practice Draft“ mit den Einstellungen dieser Liga an.
  Dort habe ich die Selektoren angepasst und geprüft: aufgerufener Spieler (`.pickArea .player-selected .playerinfo__playername`), aktuelles Gebot
  (`.pickArea .current-amount`), Höchstbietender (neuester Eintrag in `.bid-history__list`) und Verkäufe (alle Zellen des Draft Boards mit der Klasse
  `completedPick`; das Team ergibt sich aus der Spalte, in der die Zelle steht). Die Liste „Picks“ von ESPN ist virtualisiert und zeigt nicht alle Zeilen,
  das Board dagegen schon. Beim Abgleich mit ESPNs eigener Pick-Liste stimmten Spieler, Team und Preis bei allen Verkäufen überein.
* **Ende-zu-Ende.** Aufruf, Gebotserhöhungen und Verkauf kamen aus dem ESPN-Raum über das Skript und den Empfänger in der App an, ohne einen Klick
  (z. B. „BIETEN bis $95. Aktuell $20 (Team 15)“, danach der Verkauf im Log mit Quelle `espn`). Dazu 11 Tests für Namens- und Team-Zuordnung und die
  Ereignisverarbeitung (`draft_app/tests/test_bridge.py`).
* **Einrichtung.** Tampermonkey installieren und in Chrome unter Erweiterungen → Details „Benutzerskripts zulassen“ einschalten. `python draft_app/bridge.py`
  starten, die App starten, das Skript mit dem Token aus `bridge_token.txt` installieren. ESPNs Seite darf keine Verbindung zu `127.0.0.1` aufbauen
  (Sicherheitsrichtlinie der Seite); deshalb geht der Weg über Tampermonkey (`GM_xmlhttpRequest`). Zwei Kopien desselben Skripts erzeugen zwei Badges: im
  Tampermonkey-Dashboard sollte nur ein Eintrag stehen.
* **Grenzen.** Das Markup stammt aus dem Übungsdraft; der echte Raum ist am 17. Oktober dieselbe Oberfläche, aber das ist erst dann bestätigt. Ändert ESPN sein Markup,
  muss man nachziehen (`Strg+Shift+D` speichert einen Seiten-Snapshot für die Anpassung). „Letzten Pick zurücknehmen“ gilt nur für Fehleingaben: Lädt man die
  Draft-Room-Seite neu, schickt das Skript die Verkäufe erneut und der Empfänger trägt zurückgenommene Picks wieder ein (ESPN gilt als Wahrheit). Die Token-Prüfung
  schützt nur vor fremden Webseiten, nicht vor anderen Programmen auf deinem Rechner. Aufgerufen wird im ESPN-Draft reihum durch die Teams; die Reihenfolge nach
  ESPN-Rang in der App ist nur die Vorbelegung, solange die Anbindung keinen Spieler meldet.

---

## 11. Offene Fragen

**An dich:**

* **F1.** Zählt in der Tabelle 5:5 wirklich als halber Sieg? Die ESPN-Einstellungen kennen keinen Tie-Breaker, ich habe 5:5 deshalb als halben Sieg modelliert. Das beeinflusst die Kalibrierung geringfügig.
* **F2.** Ist der Draft online mit Timer? Das beeinflusst, wie viel Zeit zum Nachschlagen bleibt, und ob die App auf Handy-Grösse getestet werden sollte.
* **F3.** Kennst du die Manager? Wenn einige regelmässig Stars überbezahlen oder Bank-Plätze verschenken, lässt sich das als Rivalen-Profil einbauen.
* **F4.** Welche Spieler siehst du anders als das Modell? Besonders Giannis, Tatum, Sabonis (Verfügbarkeit) und Keyonte George (Break-even-Preis $26 bei Board $10).
* **F5.** Soll ich eine Nominierungs-Empfehlung ergänzen (wen aufrufen, um Rivalen Geld zu entziehen)?
* **F6.** Ist die Aufruf-Reihenfolge im ESPN-Draft wirklich ESPNs Vorab-Rang? Die Liga-Daten selbst sind ohne Login nicht abrufbar (ESPN antwortet mit 401),
  deshalb verwendet die App den öffentlichen ESPN-Rang für H2H-Kategorien. Wird anders aufgerufen (z. B. nominieren die Manager reihum selbst), greift
  „Anderer Spieler aufgerufen?“. Ist die echte Reihenfolge vorab bekannt, kann man sie als Spalte `ESPN_RANK` in die Spielerliste schreiben.

**Methodisch:**

* **M1.** Wie gut sind die Teamkollegen-Koeffizienten ausserhalb der Stichprobe? Mit rund 130 Teamwechslern ist die Stichprobe klein.
* **M2.** Soll die Prognoseunsicherheit pro Spieler in die Wochen-Streuung? Das senkt die absoluten Zahlen und könnte Rangfolgen verschieben.
* **M3.** Hilft eine Kalibrierung an echten ESPN-Ergebnissen der Vorjahre (falls Zugriff auf die Historie der Liga besteht)?

---

## 12. Quellen und Referenzen

**Notebook** `fantasy_draft_2026_27_v4_engine.ipynb` (Ausgaben gespeichert):

* **[N2]** Zelle 2, Liga-Regeln · **[N6B]** Verfügbarkeit, Backtest über vier Saisons · **[N7]** Kategorie-Korrelation und -Konzentration
* **[N8]** Bewertung und drei Marktkurven · **[N8A]** Pool-Abdeckung (Rückkehrer, Rookies) · **[N9]** Wochen-Simulator, Fehlertabelle zu v3
* **[N10]** Validierung der Engine · **[N11]** umkämpfte Auktion · **[N12]** Stresstests
* **[N13]** Zielkader und Break-even-Preise · **[N14]** Saison-Simulation · **[N15]** Export für die App

**Code:** `draft_app/engine.py`, `draft_app/auction_sim.py`, `draft_app/app.py`, `draft_app/tests/test_engine.py`

**Ad-hoc-Skripte** (nicht im Repo, Scratchpad der Sitzung):

* **[X1]** `exp_e1.log` (Ranking der v3-Bündel je Preiskurve, v3-Simulator)
* **[X2]** Teamkollegen-Regression: Niveau R² 0,0184 (Koeffizient −0,033), Änderung R² 0,0667 (Koeffizient −0,057), beide: Niveau +0,006, Änderung −0,060

**Externe Daten:**

* **[D1]** NBA-Spielprotokolle, Kader, Spielerinfos über das Paket `nba_api` (stats.nba.com): `leaguegamelog`, `commonplayerinfo`, `commonteamroster`
* **[D2]** ESPN-Fantasy-Spielerliste (Auktionswerte, Positions-Berechtigung, Verletzungsstatus), Endpunkt `lm-api-reads.fantasy.espn.com/apis/v3/games/fba/`
* **[L1]** Liga-Einstellungen: https://fantasy.espn.com/basketball/league/settings?leagueId=1015252784&seasonId=2027 (von dir eingefügt)
