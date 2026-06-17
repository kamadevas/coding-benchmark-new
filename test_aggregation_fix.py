"""Regressiontest: Average-/Leaderboard-Berechnung – Bugfix None/null bei Advanced-Werten."""
import sys
import json
from dataclasses import dataclass, field, asdict
from typing import Any

# Projekt importieren
sys.path.insert(0, ".")
from llm_benchmark_gui import (
    average_numeric,
    min_numeric,
    max_numeric,
    aggregate_model_runs,
    summarize_section_runs,
    BenchmarkResult,
    CODING_TASKS,
    DART_LOGIC_TASKS,
    FLUTTER_UI_TASKS,
    AGENT_TASKS,
)


def test_average_numeric():
    """average_numeric ignoriert None und berechnet korrekt."""
    assert average_numeric([1, 2, 3]) == 2.0
    assert average_numeric([1, None, 3]) == 2.0
    assert average_numeric([None, None]) is None
    assert average_numeric([]) is None
    assert average_numeric([0.0]) == 0.0
    assert average_numeric([0.0, None]) == 0.0


def test_min_max_numeric():
    """min_numeric/max_numeric ignorieren None."""
    assert min_numeric([5, 1, 3]) == 1.0
    assert min_numeric([5, None, 1]) == 1.0
    assert min_numeric([None]) is None
    assert min_numeric([]) is None
    assert max_numeric([5, 1, 3]) == 5.0
    assert max_numeric([None, 1]) == 1.0


def test_aggregate_single_run_total_score():
    """Ein Modell mit genau einem Run – total_score muss aus Run-Werten kommen."""
    run = BenchmarkResult(model="TestModel")
    run.coding_percent = 100.0
    run.agent_percent = 20.0
    run.dart_logic_percent = 66.23188405797102
    run.flutter_ui_percent = 100.0
    run.agent_advanced_percent = 0.0
    run.dart_logic_advanced_percent = None
    run.flutter_ui_advanced_percent = None
    run.tool_ok_percent = 53.541666666666664
    run.total_score = 40.89026915113872

    # Simuliere Details für summarize_section_runs
    # Agent: Advanced-Tasks mit fraction=0.0, normale mit 0.2
    agent_details = []
    for t in AGENT_TASKS:
        if getattr(t, "difficulty", None) == "advanced":
            agent_details.append({"title": t.title, "fraction": 0.0, "weight": t.weight, "error_status": ""})
        else:
            agent_details.append({"title": t.title, "fraction": 0.2, "weight": t.weight, "error_status": ""})
    # Dart-Logic: Advanced-Task ohne fraction-Wert (None/null)
    dart_logic_details = []
    for t in DART_LOGIC_TASKS:
        if getattr(t, "difficulty", None) == "advanced":
            dart_logic_details.append({"title": t.title, "fraction": None, "weight": t.weight, "error_status": ""})
        else:
            dart_logic_details.append({"title": t.title, "fraction": 0.6623188405797102, "weight": t.weight, "error_status": ""})
    # Flutter-UI: Advanced-Task ohne fraction-Wert
    flutter_ui_details = []
    for t in FLUTTER_UI_TASKS:
        if getattr(t, "difficulty", None) == "advanced":
            flutter_ui_details.append({"title": t.title, "fraction": None, "weight": t.weight, "error_status": ""})
        else:
            flutter_ui_details.append({"title": t.title, "fraction": 1.0, "weight": t.weight, "error_status": ""})

    run.details = {
        "coding": [
            {"title": t.title, "fraction": 1.0, "weight": t.weight, "error_status": ""}
            for t in CODING_TASKS
        ],
        "agent": agent_details,
        "dart_logic": dart_logic_details,
        "flutter_ui": flutter_ui_details,
        "model_config": {},
        "run_index": 1,
    }

    average = aggregate_model_runs("TestModel", [run])

    # Regel 1+2: average.total_score = Durchschnitt der Run-total_score-Werte
    assert average.total_score == 40.89026915113872, (
        f"average.total_score={average.total_score}, erwartet 40.89026915113872"
    )

    # Regel 3: min/max aus echten Run-Werten
    avg_dict = average.details.get("average", {})
    assert avg_dict.get("min_total_score") == 40.89026915113872, (
        f"min_total_score={avg_dict.get('min_total_score')}"
    )
    assert avg_dict.get("max_total_score") == 40.89026915113872, (
        f"max_total_score={avg_dict.get('max_total_score')}"
    )

    # Regel 4+5: None bleibt None
    assert average.agent_advanced_percent == 0.0, (
        f"agent_advanced_percent={average.agent_advanced_percent}, erwartet 0.0"
    )
    assert average.dart_logic_advanced_percent is None, (
        f"dart_logic_advanced_percent={average.dart_logic_advanced_percent}, erwartet None"
    )
    assert average.flutter_ui_advanced_percent is None, (
        f"flutter_ui_advanced_percent={average.flutter_ui_advanced_percent}, erwartet None"
    )

    # Regel 5: Keine falsche Ersetzung durch normale Werte
    assert average.dart_logic_advanced_percent != average.dart_logic_percent, (
        "dart_logic_advanced_percent darf nicht durch dart_logic_percent ersetzt werden"
    )
    assert average.flutter_ui_advanced_percent != average.flutter_ui_percent, (
        "flutter_ui_advanced_percent darf nicht durch flutter_ui_percent ersetzt werden"
    )

    # Regel 6: column_stats für fehlende Spalten
    col_stats = average.details.get("column_stats", {})
    dart_adv_stats = col_stats.get("dart_logic_advanced_percent", {})
    assert dart_adv_stats.get("min") is None, (
        f"dart_logic_advanced_percent column_stats min={dart_adv_stats.get('min')}, erwartet None"
    )
    assert dart_adv_stats.get("max") is None, (
        f"dart_logic_advanced_percent column_stats max={dart_adv_stats.get('max')}, erwartet None"
    )


def test_summarize_section_runs_advanced_empty():
    """summarize_section_runs mit leeren Advanced-Tasks liefert None."""
    run = BenchmarkResult(model="TestModel")
    run.coding_percent = 100.0
    run.agent_percent = 20.0
    run.dart_logic_percent = 66.23188405797102
    run.flutter_ui_percent = 100.0
    run.agent_advanced_percent = None
    run.dart_logic_advanced_percent = None
    run.flutter_ui_advanced_percent = None
    run.tool_ok_percent = 53.541666666666664
    run.total_score = 40.89026915113872

    run.details = {
        "dart_logic": [
            {"title": t.title, "fraction": 0.6623188405797102, "weight": t.weight, "error_status": ""}
            for t in DART_LOGIC_TASKS
        ],
        "model_config": {},
        "run_index": 1,
    }

    # Nur Advanced-Tasks – es gibt keine in DART_LOGIC_TASKS
    advanced_tasks = [t for t in DART_LOGIC_TASKS if getattr(t, "difficulty", None) == "advanced"]
    summary = summarize_section_runs([run], "dart_logic", advanced_tasks, "dart_logic_advanced_percent")

    assert summary["percent"] is None, (
        f"Advanced percent={summary['percent']}, erwartet None (keine Advanced-Tasks)"
    )
    assert summary["run_percent_stats"]["min"] is None
    assert summary["run_percent_stats"]["max"] is None
    assert summary["tasks"] == []


if __name__ == "__main__":
    print("=== Test: average_numeric ===")
    test_average_numeric()
    print("  OK")

    print("=== Test: min_numeric / max_numeric ===")
    test_min_max_numeric()
    print("  OK")

    print("=== Test: aggregate_single_run_total_score ===")
    test_aggregate_single_run_total_score()
    print("  OK")

    print("=== Test: summarize_section_runs_advanced_empty ===")
    test_summarize_section_runs_advanced_empty()
    print("  OK")

    print("\n✅ Alle Regressionstests bestanden!")
