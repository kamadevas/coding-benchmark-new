# Local LLM Coding Benchmark

Lokales Tool zum Vergleichen von Modellen für Coding und Agent-Coding. Es nutzt `Tkinter` und ausschließlich die Python-Standardbibliothek, testet lokale LLMs über OpenAI-kompatible Endpoints wie Ollama oder llama.cpp und optional DeepSeek-Modelle über die Cloud-API.

## Start

```powershell
python llm_benchmark_gui.py
```

Optional kann unter Windows die vorhandene Batch-Datei gestartet werden:

```powershell
.\"Benchmark Start.bat"
```

## DeepSeek

Der DeepSeek API-Key wird ausschließlich über eine Umgebungsvariable gesetzt:

```powershell
setx DEEPSEEK_API_KEY "dein_key"
```

Den API-Key niemals in `models.json`, Export-Dateien, Logs oder die README schreiben. Cloud-Läufe über DeepSeek können Tokens verbrauchen und Kosten verursachen.

`leaderboard.json` ist lokaler Zustand und wird nicht committed.

## Funktionen

- Editierbare Modellliste mit Name, Endpoint-URL und Modell-ID.
- Checkboxen für „Coding testen“, „Agent-Coding testen“ und „Dart-Logik testen“.
- Hintergrund-Thread für Benchmarks, damit das Fenster bedienbar bleibt.
- Subprozess, Timeout und Temp-Ordner für Modellcode und Tests.
- Textbasiertes Tool-Protokoll im Agent-Modus per JSON-Block.
- Ergebnistabelle: Modell, Coding %, Agent %, Dart-Logik %, Tool-OK %, Ø Schritte, tok/s.
- Export als CSV oder JSON.

## Bewertung

Benchmark-Werte sind nur innerhalb dieses Tools und mit gleicher Tool-Version sinnvoll vergleichbar. Sie sind nicht direkt mit Internet-Benchmarks oder anderen Benchmark-Suiten vergleichbar.

- Es gibt keine subjektiven Strafstufen.
- Die Fehlerschwere ergibt sich automatisch aus bestandenen Checks pro Aufgabe. Die Aufgaben enthalten mehrere Testfälle, sodass kleine Fehler nur wenige Checks verlieren und schwere Fehler viele oder alle Checks verlieren.
- Compile-Fail, Endlosschleife beziehungsweise Tokenlimit-Abbruch, Laufzeit-Crash und Ausführungs-Timeout durch das Modell zählen für die jeweilige Aufgabe als `0.0`.
- Nur echte Transport- oder Netzwerkfehler zum Modell werden aus der Wertung ausgeschlossen.
- Aufgaben-Gewichte bilden Prioritäten ab, nicht Fehlerschwere. Tool-Call-/Agent-relevante Aufgaben sind höher gewichtet als reine Logikaufgaben.
- Die Score-Untergrenze pro Aufgabe ist `0.0`. Es gibt keine negativen Werte und keine aufgabenübergreifende Verrechnung von Fehlern.

## Endpoint-Format

Die App ruft standardmäßig `ENDPOINT/v1/chat/completions` auf. Wenn die eingetragene URL bereits auf `/v1` endet, wird `/chat/completions` ergänzt. Wenn sie direkt auf `/chat/completions` endet, wird sie unverändert genutzt.

Beispiele:

- Ollama: `http://localhost:11434`, Modell-ID z. B. `llama3`
- llama.cpp Server: `http://localhost:8080`, Modell-ID je nach Server-Konfiguration

## Sicherheitshinweis

Die Benchmarks führen vom Modell erzeugten Code lokal aus. Die App nutzt Temp-Ordner, Subprozesse und Timeouts als Mindestschutz. Das ist kein vollständiges Sandbox-System gegen beliebigen oder bösartigen Code.
