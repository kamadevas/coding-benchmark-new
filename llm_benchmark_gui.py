"""
Lokaler LLM-Benchmark mit Tkinter.

Nur Python-Standardbibliothek. Testet OpenAI-kompatible Chat-Completions-
Endpoints, z. B. Ollama oder llama.cpp.

Sicherheitsmodell: Modellcode und Tests laufen in einem Temp-Ordner in einem
Subprozess mit Timeout. Das ist nur Mindestschutz; beliebiger Code läuft auf
dieser Maschine.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, Y, BooleanVar, IntVar, StringVar, Text, Tk, Toplevel, filedialog, messagebox
from tkinter import ttk
from typing import Any, Callable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


APP_TITLE = "Lokaler LLM Coding Benchmark"
DEFAULT_MODELS_FILE = "models.json"
LEADERBOARD_FILE = "leaderboard.json"
BENCHMARK_VERSION = 1
# Eignungs-Schwellen vorläufig; nach lokalen und Cloud-Referenzläufen justieren.
CODING_SUITABLE_OK = 65.0
CODING_SUITABLE_WARN = 40.0
AGENT_ADVANCED_SUITABLE_OK = 60.0
AGENT_ADVANCED_SUITABLE_WARN = 35.0
TOOL_OK_SUITABLE_OK = 85.0
TOOL_OK_SUITABLE_WARN = 70.0
SUITABILITY_UNRELIABLE_DROP_PERCENT = 25.0
AGENT_AVG_STEPS_WARN = 12.0
DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("LLM_BENCHMARK_MODEL_TIMEOUT_SECONDS", "300"))
DEFAULT_MODEL_MAX_TOKENS = int(os.environ.get("LLM_BENCHMARK_MODEL_MAX_TOKENS", "16384"))
AGENT_BASIC_TIMEOUT_SECONDS = 180
AGENT_ADVANCED_TIMEOUT_SECONDS = 300
AGENT_RESPONSE_MAX_TOKENS = 8192
BENCHMARK_ERROR_STATUSES: set[str] = {"dart_not_found", "flutter_not_found", "grading_error", "benchmark_runtime_error"}
MODEL_ERROR_STATUSES: set[str] = {"timeout"}
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
MISSING_DEEPSEEK_API_KEY_MESSAGE = "missing_api_key: DEEPSEEK_API_KEY fehlt für DeepSeek; Lauf wird nicht als Modellversagen gewertet."
DEEPSEEK_MODELS = {
    "DeepSeek V4 Flash": "deepseek-v4-flash",
    "DeepSeek V4 Pro": "deepseek-v4-pro",
}


@dataclasses.dataclass
class ModelConfig:
    name: str
    endpoint_url: str
    model_id: str
    provider: str = "local"
    api_key: str = ""
    reasoning_effort: str = ""

    @property
    def base_url(self) -> str:
        return self.endpoint_url

    @property
    def is_deepseek(self) -> bool:
        return self.provider.strip().lower() == "deepseek"

    @property
    def is_cloud(self) -> bool:
        return self.provider.strip().lower() not in {"", "local", "ollama", "lmstudio", "openai-compatible"}


@dataclasses.dataclass
class CodingTask:
    title: str
    difficulty: str
    weight: int
    prompt: str
    function_name: str
    tests_source: str


@dataclasses.dataclass
class AgentTask:
    title: str
    weight: int
    prompt: str
    files: dict[str, str]
    test_command: list[str]
    max_steps: int = 8
    difficulty: str = "basic"


@dataclasses.dataclass
class DartLogicTask:
    title: str
    difficulty: str
    weight: int
    prompt: str
    function_name: str
    harness_source: str
    reference_solution: str
    broken_solution: str


@dataclasses.dataclass
class FlutterUITask:
    title: str
    difficulty: str
    weight: int
    prompt: str
    widget_name: str
    test_source: str
    reference_solution: str
    broken_solution: str
    test_filename: str = "chat_input_widget_test.dart"
    harness_source: str = ""


@dataclasses.dataclass
class WorkflowAgentTask:
    task_id: str
    title: str
    weight: int
    prompt: str
    fixture_path: str
    test_command: list[str]
    allowed_files: set[str]
    forbidden_patterns: list[str]


@dataclasses.dataclass
class BenchmarkResult:
    model: str
    coding_percent: float | None = None
    agent_percent: float | None = None
    dart_logic_percent: float | None = None
    flutter_ui_percent: float | None = None
    workflow_agent_percent: float | None = None
    agent_advanced_percent: float | None = None
    dart_logic_advanced_percent: float | None = None
    flutter_ui_advanced_percent: float | None = None
    tool_ok_percent: float | None = None
    avg_steps: float | None = None
    tokens_per_second: float | None = None
    total_score: float = 0.0
    benchmark_valid: bool = True
    benchmark_error: str = ""
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

class ModelConnectionError(RuntimeError):
    """Separater Fehlerstatus für Modell-Verbindungsprobleme statt Grading-Fehlschlag."""


class MissingApiKeyError(ModelConnectionError):
    """Cloud-Modell ist ohne benötigten API-Key nicht ausführbar."""


class OpenAICompatClient:
    """Minimaler Client für /v1/chat/completions ohne externe Pakete."""

    def __init__(self, endpoint_url: str, model_id: str, status_cb: Callable[[str], None] | None = None, provider: str = "local", api_key: str = "", reasoning_effort: str = "") -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.model_id = model_id
        self.status_cb = status_cb or (lambda _msg: None)
        self.provider = provider.strip().lower()
        if self.provider == "deepseek":
            self.api_key = api_key or os.environ.get(DEEPSEEK_API_KEY_ENV, "")
            self.reasoning_effort = reasoning_effort.strip().lower()
        else:
            self.api_key = ""
            self.reasoning_effort = ""
        self.total_tokens = 0
        self.total_seconds = 0.0
        self.last_finish_reason: str | None = None
        self.last_reasoning_content: str = ""
        self._reasoning_delivered: bool = False

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        timeout: int = DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
        max_tokens: int = DEFAULT_MODEL_MAX_TOKENS,
    ) -> str:
        if self.provider == "deepseek" and not self.api_key:
            raise MissingApiKeyError(MISSING_DEEPSEEK_API_KEY_MESSAGE)
        url = self.endpoint_url
        if not url.endswith("/chat/completions"):
            if self.provider == "deepseek":
                url = f"{url}/chat/completions"
            else:
                url = f"{url}/v1/chat/completions" if not url.endswith("/v1") else f"{url}/chat/completions"

        payload = {
            "model": self.model_id,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if self.provider == "deepseek" and self.reasoning_effort and self.reasoning_effort != "none":
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.reasoning_effort
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.provider == "deepseek":
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ModelConnectionError(sanitize_sensitive_text(f"HTTP {exc.code} von {url}: {body[:1000]}")) from exc
        except urllib.error.URLError as exc:
            raise ModelConnectionError(f"Verbindungsfehler/Timeout beim Modell-Call ({timeout}s): {url}: {exc}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ModelConnectionError(f"Timeout beim Modell-Call nach {timeout}s: {url}: {exc}") from exc
        elapsed = max(0.001, time.perf_counter() - start)
        parsed = json.loads(raw)
        choice = parsed.get("choices", [{}])[0]
        self.last_finish_reason = choice.get("finish_reason")
        content = choice.get("message", {}).get("content", "")
        self.last_reasoning_content = choice.get("message", {}).get("reasoning_content", "") or ""
        if self.last_reasoning_content.strip():
            self._reasoning_delivered = True
        usage = parsed.get("usage") or {}
        tokens = int(usage.get("completion_tokens") or usage.get("total_tokens") or estimate_tokens(content))
        self.total_tokens += tokens
        self.total_seconds += elapsed
        return content

    @property
    def effective_reasoning_effort(self) -> str:
        """Effektiv von der API geliefertes Reasoning-Level, abgeleitet aus den Antworten."""
        if not self.reasoning_effort or self.reasoning_effort == "none":
            return "none"
        return self.reasoning_effort if self._reasoning_delivered else "none"

    @property
    def tokens_per_second(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return self.total_tokens / self.total_seconds


def estimate_tokens(text: str) -> int:
    return max(1, len(text.split()) + len(text) // 16)


def sanitize_sensitive_text(text: str) -> str:
    api_key = os.environ.get(DEEPSEEK_API_KEY_ENV, "")
    if api_key:
        text = text.replace(api_key, "[REDACTED_DEEPSEEK_API_KEY]")
    return text


def sanitize_for_export(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_sensitive_text(value)
    if isinstance(value, list):
        return [sanitize_for_export(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_for_export(item) for key, item in value.items() if key not in {"api_key", "authorization", "Authorization"}}
    return value


def model_from_dict(item: dict[str, Any]) -> ModelConfig:
    provider = str(item.get("provider") or "local").strip().lower() or "local"
    endpoint_url = str(item.get("endpoint_url") or item.get("base_url") or "").strip()
    model_id = str(item.get("model_id") or item.get("model") or "").strip()
    name = str(item.get("name") or model_id or "Modell").strip()
    api_key = str(item.get("api_key") or "").strip()
    reasoning_effort = str(item.get("reasoning_effort") or "").strip().lower()
    if provider == "deepseek" and not endpoint_url:
        endpoint_url = DEEPSEEK_BASE_URL
    return ModelConfig(name=name, endpoint_url=endpoint_url, model_id=model_id, provider=provider, api_key=api_key, reasoning_effort=reasoning_effort)


def model_to_dict(model: ModelConfig) -> dict[str, str]:
    if model.is_deepseek:
        result = {"name": model.name, "provider": "deepseek", "base_url": model.endpoint_url, "model_id": model.model_id}
        if model.api_key:
            result["api_key"] = model.api_key
        if model.reasoning_effort:
            result["reasoning_effort"] = model.reasoning_effort
        return result
    return {"name": model.name, "endpoint_url": model.endpoint_url, "model_id": model.model_id}


def make_deepseek_model(name: str, api_key: str = "", reasoning_effort: str = "") -> ModelConfig:
    return ModelConfig(name=name, endpoint_url=DEEPSEEK_BASE_URL, model_id=DEEPSEEK_MODELS[name], provider="deepseek", api_key=api_key, reasoning_effort=reasoning_effort)


LEADERBOARD_FIELDS = [
    "model",
    "provider",
    "model_id",
    "quant",
    "context_size",
    "reasoning_effort",
    "benchmark_version",
    "active_blocks",
    "request_max_tokens",
    "agent_response_max_tokens",
    "timestamp",
    "runs_count",
    "avg_total_score",
    "min_total_score",
    "max_total_score",
    "avg_coding_percent",
    "avg_dart_logic_percent",
    "avg_dart_logic_advanced_percent",
    "avg_flutter_ui_percent",
    "avg_flutter_ui_advanced_percent",
    "avg_agent_percent",
    "avg_agent_advanced_percent",
    "avg_tool_ok_percent",
    "avg_steps",
    "avg_tokens_per_second",
    "benchmark_valid",
    "benchmark_error",
    "excluded_from_leaderboard",
    "coding_suitability",
    "coding_suitability_reason",
    "agent_suitability",
    "agent_suitability_reason",
]


def leaderboard_path() -> Path:
    return Path(LEADERBOARD_FILE)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def average_numeric(values: list[Any]) -> float | None:
    """Durchschnitt aus numerischen Werten; None bei leerer Liste."""
    numeric = [v for v in values if isinstance(v, (int, float))]
    if not numeric:
        return None
    return sum(numeric) / len(numeric)


def min_numeric(values: list[Any]) -> float | None:
    """Minimum aus numerischen Werten; None bei leerer Liste."""
    numeric = [v for v in values if isinstance(v, (int, float))]
    return min(numeric) if numeric else None


def max_numeric(values: list[Any]) -> float | None:
    """Maximum aus numerischen Werten; None bei leerer Liste."""
    numeric = [v for v in values if isinstance(v, (int, float))]
    return max(numeric) if numeric else None


def safe_filename_part(value: object, fallback: str = "unknown") -> str:
    """Bereinigt einen Wert zur Verwendung in Dateinamen."""
    text = str(value or fallback).strip()
    text = text.replace(" ", "_")
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("._-") or fallback


def _leaderboard_float(entry: dict[str, Any], new_field: str, legacy_field: str | None = None) -> float | None:
    value = _safe_float(entry.get(new_field))
    if value is None and legacy_field:
        value = _safe_float(entry.get(legacy_field))
    return value


def _first_scored_value(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def infer_quant(*texts: str) -> str:
    text = " ".join(texts)
    match = re.search(r"\b(Q\d(?:_[A-Z0-9]+)?|IQ\d_[A-Z0-9]+|F16|FP16|BF16|Q[234568])\b", text, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def infer_context_size(*texts: str) -> str:
    text = " ".join(texts)
    match = re.search(r"\b(\d{1,3})\s*[kK]\b", text)
    if match:
        return str(int(match.group(1)) * 1024)
    match = re.search(r"\b(?:ctx|context|kontext)[-_ ]?(\d{3,6})\b", text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def active_blocks_from_details(result: BenchmarkResult) -> str:
    runs = result.details.get("runs", []) if isinstance(result.details, dict) else []
    first_details = runs[0].get("details", {}) if runs and isinstance(runs[0], dict) else {}
    blocks = []
    for key, label in (("coding", "coding"), ("dart_logic", "dart_logic"), ("flutter_ui", "flutter_ui"), ("agent", "agent")):
        if first_details.get(key):
            blocks.append(label)
    return "+".join(blocks)


def leaderboard_entry_key(entry: dict[str, Any]) -> tuple[str, str, str, str, str, int, str, str, str]:
    try:
        version = int(entry.get("benchmark_version") or BENCHMARK_VERSION)
    except (TypeError, ValueError):
        version = BENCHMARK_VERSION
    return (
        str(entry.get("model_id") or entry.get("model") or "").strip().lower(),
        str(entry.get("provider") or "local").strip().lower(),
        str(entry.get("quant") or "").strip().lower(),
        str(entry.get("context_size") or "").strip().lower(),
        str(entry.get("reasoning_effort") or "").strip().lower(),
        version,
        str(entry.get("active_blocks") or "").strip().lower(),
        str(entry.get("request_max_tokens") or "").strip().lower(),
        str(entry.get("agent_response_max_tokens") or "").strip().lower(),
    )


def _leaderboard_timestamp_value(entry: dict[str, Any]) -> datetime | None:
    text = str(entry.get("timestamp") or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _prefer_leaderboard_entry(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    current_timestamp = _leaderboard_timestamp_value(current)
    candidate_timestamp = _leaderboard_timestamp_value(candidate)
    if current_timestamp is not None or candidate_timestamp is not None:
        if candidate_timestamp is None:
            return current
        if current_timestamp is None or candidate_timestamp > current_timestamp:
            return candidate
        return current
    current_score = _safe_float(current.get("avg_total_score") or current.get("total_score")) or 0.0
    candidate_score = _safe_float(candidate.get("avg_total_score") or candidate.get("total_score")) or 0.0
    return candidate if candidate_score > current_score else current


def dedupe_leaderboard_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    by_key: dict[tuple[str, str, str, str, str, int, str, str, str], dict[str, Any]] = {}
    key_order: list[tuple[str, str, str, str, str, int, str, str, str]] = []
    unique_without_key: list[dict[str, Any]] = []
    removed = 0
    for entry in entries:
        key = leaderboard_entry_key(entry)
        if not key[0]:
            unique_without_key.append(entry)
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = entry
            key_order.append(key)
            continue
        by_key[key] = _prefer_leaderboard_entry(existing, entry)
        removed += 1
    deduped = [by_key[key] for key in key_order] + unique_without_key
    return deduped, removed


def _percent_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _downgrade_symbol(symbol: str) -> str:
    return {"✅": "⚠️", "⚠️": "❌"}.get(symbol, symbol)


def _task_breakdown(result_or_entry: BenchmarkResult | dict[str, Any]) -> dict[str, Any]:
    if isinstance(result_or_entry, BenchmarkResult):
        return result_or_entry.details.get("task_breakdown", {}) if isinstance(result_or_entry.details, dict) else {}
    return result_or_entry.get("task_breakdown", {}) if isinstance(result_or_entry.get("task_breakdown"), dict) else {}


def _column_stats(result_or_entry: BenchmarkResult | dict[str, Any]) -> dict[str, Any]:
    if isinstance(result_or_entry, BenchmarkResult):
        return result_or_entry.details.get("column_stats", {}) if isinstance(result_or_entry.details, dict) else {}
    return result_or_entry.get("column_stats", {}) if isinstance(result_or_entry.get("column_stats"), dict) else {}


def _block_value(result_or_entry: BenchmarkResult | dict[str, Any], attr: str) -> float | None:
    if isinstance(result_or_entry, BenchmarkResult):
        return _safe_float(getattr(result_or_entry, attr, None))
    return _first_scored_value(result_or_entry.get(f"avg_{attr}"), result_or_entry.get(attr))


def _weakest_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored = [task for task in tasks if _safe_float(task.get("average_percent")) is not None]
    return min(scored, key=lambda task: _safe_float(task.get("average_percent")) or 0.0, default=None)


def _zero_failure_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for task in tasks:
        min_percent = _safe_float(task.get("min_percent"))
        if min_percent == 0.0:
            return task
    return None


def _spread_warning(stats: dict[str, Any] | None) -> str | None:
    if not isinstance(stats, dict):
        return None
    low = _safe_float(stats.get("min"))
    high = _safe_float(stats.get("max"))
    if low is None or high is None or high - low < SUITABILITY_UNRELIABLE_DROP_PERCENT:
        return None
    return f"unzuverlässig - schwankt zwischen {_percent_text(low)} und {_percent_text(high)}"


def coding_relevant_score(result_or_entry: BenchmarkResult | dict[str, Any]) -> float | None:
    dart_logic = _block_value(result_or_entry, "dart_logic_percent")
    flutter_ui = _block_value(result_or_entry, "flutter_ui_percent")
    values = [value for value in (dart_logic, flutter_ui) if value is not None]
    return sum(values) / len(values) if values else _block_value(result_or_entry, "coding_percent")


def coding_suitability_assessment(result_or_entry: BenchmarkResult | dict[str, Any]) -> tuple[str, str]:
    score = coding_relevant_score(result_or_entry)
    dart_logic = _block_value(result_or_entry, "dart_logic_percent")
    flutter_ui = _block_value(result_or_entry, "flutter_ui_percent")
    legacy_coding = _block_value(result_or_entry, "coding_percent")
    if score is None:
        return "⚪", "Nicht bewertet"
    flutter_only = flutter_ui is not None and dart_logic is None and legacy_coding is None
    breakdown = _task_breakdown(result_or_entry)
    coding_tasks = list(breakdown.get("dart_logic", [])) + [task for task in breakdown.get("flutter_ui", []) if str(task.get("title")) == "StreamingChatView"]
    weakest = _weakest_task(coding_tasks)
    zero_task = _zero_failure_task(coding_tasks)
    if score is None or score < CODING_SUITABLE_WARN:
        symbol = "❌"
    elif score < CODING_SUITABLE_OK:
        symbol = "⚠️"
    else:
        symbol = "✅"
    reasons: list[str] = []
    if zero_task is not None and symbol == "✅":
        symbol = "⚠️"
        reasons.append(f"Totalausfall in {zero_task.get('title')}")
    elif zero_task is not None:
        reasons.append(f"Totalausfall in {zero_task.get('title')}")
    spread = _spread_warning(_column_stats(result_or_entry).get("dart_logic_percent")) or _spread_warning(_column_stats(result_or_entry).get("flutter_ui_percent"))
    if spread:
        old = symbol
        symbol = _downgrade_symbol(symbol)
        if symbol != old:
            reasons.append(spread)
    if symbol != "✅" and weakest is not None:
        reasons.insert(0, f"{weakest.get('title')} nur {_percent_text(_safe_float(weakest.get('average_percent')))}")
    elif not reasons and flutter_only and symbol == "✅":
        reasons.append("Flutter geeignet")
    elif not reasons and flutter_only:
        reasons.append(f"Flutter bewertet {_percent_text(flutter_ui)}")
    elif not reasons:
        reasons.append(f"coding-relevante Blöcke {_percent_text(score)}")
    return symbol, "; ".join(dict.fromkeys(reasons))


def agent_suitability_assessment(result_or_entry: BenchmarkResult | dict[str, Any]) -> tuple[str, str]:
    agent_advanced = _block_value(result_or_entry, "agent_advanced_percent")
    agent_basic = _block_value(result_or_entry, "agent_percent")
    agent_score = _first_scored_value(agent_advanced, agent_basic)
    tool_ok = _block_value(result_or_entry, "tool_ok_percent")
    avg_steps = _block_value(result_or_entry, "avg_steps")
    if agent_advanced is None and agent_basic is None and tool_ok is None:
        return "⚪", "Nicht bewertet"
    if agent_score is None and tool_ok is not None:
        return "⚪", f"Agent-Coding nicht bewertet, Tool-OK {_percent_text(tool_ok)}"
    agent_tasks = list(_task_breakdown(result_or_entry).get("agent", []))
    zero_task = _zero_failure_task(agent_tasks)
    reasons: list[str] = []
    if agent_score is not None and tool_ok is not None and agent_score >= AGENT_ADVANCED_SUITABLE_OK and tool_ok >= TOOL_OK_SUITABLE_OK and zero_task is None:
        symbol = "✅"
    elif agent_score is not None and tool_ok is not None and agent_score >= AGENT_ADVANCED_SUITABLE_WARN and tool_ok >= TOOL_OK_SUITABLE_WARN:
        symbol = "⚠️"
    else:
        symbol = "❌"
    if agent_score is None or agent_score < AGENT_ADVANCED_SUITABLE_WARN:
        reasons.append(f"Agent-Advanced nur {_percent_text(agent_score)}")
    if tool_ok is None or tool_ok < TOOL_OK_SUITABLE_WARN:
        reasons.append(f"Tool-OK nur {_percent_text(tool_ok)} - bricht im Agent-Loop ab")
    elif symbol == "⚠️" and tool_ok < TOOL_OK_SUITABLE_OK:
        reasons.append(f"Tool-OK nur {_percent_text(tool_ok)}")
    if zero_task is not None:
        failed_calls = sum(int(_safe_float(task.get("bad_calls")) or 0) for task in agent_tasks)
        suffix = f", {failed_calls} fehlgeschlagene Tool-Calls" if failed_calls else ""
        reasons.append(f"{zero_task.get('title')} 0%{suffix}")
        if symbol == "✅":
            symbol = "⚠️"
    spread = _spread_warning(_column_stats(result_or_entry).get("agent_advanced_percent")) or _spread_warning(_column_stats(result_or_entry).get("agent_percent"))
    if spread:
        old = symbol
        symbol = _downgrade_symbol(symbol)
        if symbol != old:
            reasons.append(spread)
    if avg_steps is not None and avg_steps > AGENT_AVG_STEPS_WARN:
        if symbol == "✅":
            symbol = "⚠️"
        reasons.append(f"Ø Schritte {avg_steps:.1f} > {AGENT_AVG_STEPS_WARN:.0f}")
    if not reasons:
        reasons.append(f"Agent {_percent_text(agent_score)}, Tool-OK {_percent_text(tool_ok)}")
    return symbol, "; ".join(dict.fromkeys(reasons))


def coding_suitability_label(coding_percent: float | None) -> str:
    if coding_percent is None:
        return "⚪"
    if coding_percent < CODING_SUITABLE_WARN:
        return "❌"
    if coding_percent < CODING_SUITABLE_OK:
        return "⚠️"
    return "✅"


def agent_suitability_label(agent_advanced_percent: float | None, tool_ok_percent: float | None) -> str:
    return agent_suitability_assessment({"avg_agent_advanced_percent": agent_advanced_percent, "avg_tool_ok_percent": tool_ok_percent})[0]


def result_to_leaderboard_entry(result: BenchmarkResult, timestamp: str | None = None) -> dict[str, Any]:
    model_meta = result.details.get("model_config", {}) if isinstance(result.details, dict) else {}
    model = str(model_meta.get("name") or result.model)
    provider = str(model_meta.get("provider") or "local")
    model_id = str(model_meta.get("model_id") or result.model)
    quant = str(model_meta.get("quant") or infer_quant(model, model_id))
    context_size = str(model_meta.get("context_size") or infer_context_size(model, model_id))
    reasoning_effort = str(model_meta.get("reasoning_effort") or "")
    active_blocks = str(result.details.get("active_blocks") or active_blocks_from_details(result)) if isinstance(result.details, dict) else ""
    request_max_tokens = str(result.details.get("request_max_tokens", DEFAULT_MODEL_MAX_TOKENS)) if isinstance(result.details, dict) else str(DEFAULT_MODEL_MAX_TOKENS)
    agent_response_max_tokens = str(result.details.get("agent_response_max_tokens", AGENT_RESPONSE_MAX_TOKENS)) if isinstance(result.details, dict) else str(AGENT_RESPONSE_MAX_TOKENS)
    runs = result.details.get("runs", []) if isinstance(result.details, dict) else []
    run_scores = [_safe_float(run.get("total_score")) for run in runs if isinstance(run, dict)]
    scores = [score for score in run_scores if score is not None] or [float(result.total_score)]
    avg_coding = _safe_float(result.coding_percent)
    avg_agent_advanced = _safe_float(result.agent_advanced_percent)
    avg_tool_ok = _safe_float(result.tool_ok_percent)
    coding_fit, coding_reason = coding_suitability_assessment(result)
    agent_fit, agent_reason = agent_suitability_assessment(result)
    benchmark_valid = result.benchmark_valid
    benchmark_error = result.benchmark_error
    return {
        "model": model,
        "provider": provider,
        "model_id": model_id,
        "quant": quant,
        "context_size": context_size,
        "reasoning_effort": reasoning_effort,
        "effective_reasoning_effort": str(model_meta.get("effective_reasoning_effort") or ""),
        "benchmark_version": BENCHMARK_VERSION,
        "active_blocks": active_blocks,
        "request_max_tokens": request_max_tokens,
        "agent_response_max_tokens": agent_response_max_tokens,
        "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
        "runs_count": max(1, len(runs)),
        "avg_total_score": _safe_float(result.total_score) or 0.0,
        "min_total_score": min(scores),
        "max_total_score": max(scores),
        "avg_coding_percent": avg_coding,
        "avg_dart_logic_percent": _safe_float(result.dart_logic_percent),
        "avg_dart_logic_advanced_percent": _safe_float(result.dart_logic_advanced_percent),
        "avg_flutter_ui_percent": _safe_float(result.flutter_ui_percent),
        "avg_flutter_ui_advanced_percent": _safe_float(result.flutter_ui_advanced_percent),
        "avg_agent_percent": _safe_float(result.agent_percent),
        "avg_agent_advanced_percent": avg_agent_advanced,
        "avg_tool_ok_percent": avg_tool_ok,
        "avg_steps": _safe_float(result.avg_steps),
        "avg_tokens_per_second": _safe_float(result.tokens_per_second),
        "benchmark_valid": benchmark_valid,
        "benchmark_error": benchmark_error,
        "excluded_from_leaderboard": not benchmark_valid,
        "task_breakdown": result.details.get("task_breakdown", {}) if isinstance(result.details, dict) else {},
        "column_stats": result.details.get("column_stats", {}) if isinstance(result.details, dict) else {},
        "coding_suitability": coding_fit,
        "coding_suitability_reason": coding_reason,
        "agent_suitability": agent_fit,
        "agent_suitability_reason": agent_reason,
    }


def normalize_leaderboard_entries(data: Any) -> list[dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, dict):
        entries = data.get("entries", [])
    else:
        entries = data
    if not isinstance(entries, list):
        raise ValueError("leaderboard.json muss eine Liste oder ein Objekt mit 'entries' enthalten.")
    normalized = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        if not model:
            continue
        entry = {field: item.get(field) for field in LEADERBOARD_FIELDS}
        entry["model"] = model
        entry["provider"] = str(entry.get("provider") or item.get("provider") or "local").strip().lower() or "local"
        entry["model_id"] = str(entry.get("model_id") or item.get("model_id") or model).strip()
        entry["quant"] = str(entry.get("quant") or infer_quant(model, entry["model_id"])).strip()
        entry["context_size"] = str(entry.get("context_size") or infer_context_size(model, entry["model_id"])).strip()
        entry["reasoning_effort"] = str(entry.get("reasoning_effort") or item.get("reasoning_effort") or "").strip().lower()
        try:
            entry["benchmark_version"] = int(entry.get("benchmark_version") or BENCHMARK_VERSION)
        except (TypeError, ValueError):
            entry["benchmark_version"] = BENCHMARK_VERSION
        entry["active_blocks"] = str(entry.get("active_blocks") or "").strip()
        entry["timestamp"] = str(entry.get("timestamp") or "")
        entry["avg_total_score"] = _leaderboard_float(item, "avg_total_score", "total_score") or 0.0
        entry["min_total_score"] = _leaderboard_float(item, "min_total_score", "total_score")
        entry["max_total_score"] = _leaderboard_float(item, "max_total_score", "total_score")
        if entry["min_total_score"] is None:
            entry["min_total_score"] = entry["avg_total_score"]
        if entry["max_total_score"] is None:
            entry["max_total_score"] = entry["avg_total_score"]
        for field in LEADERBOARD_FIELDS:
            if field.startswith("avg_") or field.startswith("min_") or field.startswith("max_"):
                if field == "avg_coding_percent":
                    entry[field] = _leaderboard_float(item, field, "coding_percent")
                elif field == "avg_dart_logic_percent":
                    entry[field] = _leaderboard_float(item, field, "dart_logic_percent")
                elif field == "avg_dart_logic_advanced_percent":
                    entry[field] = _leaderboard_float(item, field, "dart_logic_advanced_percent")
                elif field == "avg_flutter_ui_percent":
                    entry[field] = _leaderboard_float(item, field, "flutter_ui_percent")
                elif field == "avg_flutter_ui_advanced_percent":
                    entry[field] = _leaderboard_float(item, field, "flutter_ui_advanced_percent")
                elif field == "avg_agent_percent":
                    entry[field] = _leaderboard_float(item, field, "agent_percent")
                elif field == "avg_agent_advanced_percent":
                    entry[field] = _leaderboard_float(item, field, "agent_advanced_percent")
                elif field == "avg_tool_ok_percent":
                    entry[field] = _leaderboard_float(item, field, "tool_ok_percent")
                elif field == "avg_tokens_per_second":
                    entry[field] = _leaderboard_float(item, field, "tokens_per_second")
                else:
                    entry[field] = _safe_float(entry.get(field))
        try:
            entry["runs_count"] = max(1, int(entry.get("runs_count") or 1))
        except (TypeError, ValueError):
            entry["runs_count"] = 1
        if item.get("task_breakdown"):
            entry["task_breakdown"] = item.get("task_breakdown")
        if item.get("column_stats"):
            entry["column_stats"] = item.get("column_stats")
        coding_fit, coding_reason = coding_suitability_assessment(entry)
        agent_fit, agent_reason = agent_suitability_assessment(entry)
        entry["coding_suitability"] = coding_fit
        entry["coding_suitability_reason"] = coding_reason
        entry["agent_suitability"] = agent_fit
        entry["agent_suitability_reason"] = agent_reason
        normalized.append(entry)
    return normalized


def load_leaderboard_entries() -> list[dict[str, Any]]:
    path = leaderboard_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = normalize_leaderboard_entries(data)
    deduped, removed = dedupe_leaderboard_entries(entries)
    if removed:
        write_leaderboard_entries(deduped)
    return deduped


def write_leaderboard_entries(entries: list[dict[str, Any]]) -> None:
    ordered = sorted(entries, key=lambda item: float(item.get("avg_total_score") or item.get("total_score") or 0.0), reverse=True)
    leaderboard_path().write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


def update_leaderboard_with_results(results: list[BenchmarkResult]) -> tuple[int, int]:
    entries, _ = dedupe_leaderboard_entries(load_leaderboard_entries())
    by_key = {leaderboard_entry_key(entry): entry for entry in entries}
    added = 0
    updated = 0
    now = datetime.now().isoformat(timespec="seconds")
    for result in results:
        entry = result_to_leaderboard_entry(result, now)
        key = leaderboard_entry_key(entry)
        if not key[0]:
            continue
        existing = by_key.get(key)
        if existing is None:
            entries.append(entry)
            by_key[key] = entry
            added += 1
            continue
        existing.clear()
        existing.update(entry)
        updated += 1
    write_leaderboard_entries(entries)
    return added, updated


def coding_suitability_symbol(entry: dict[str, Any]) -> str:
    return coding_suitability_assessment(entry)[0]


def agent_coding_suitability_symbol(entry: dict[str, Any]) -> str:
    return agent_suitability_assessment(entry)[0]


def coding_suitability_text(entry: dict[str, Any]) -> str:
    symbol = coding_suitability_symbol(entry)
    reason = coding_suitability_assessment(entry)[1]
    return f"{symbol} {reason}".strip()


def agent_suitability_text(entry: dict[str, Any]) -> str:
    symbol = agent_coding_suitability_symbol(entry)
    reason = agent_suitability_assessment(entry)[1]
    return f"{symbol} {reason}".strip()


CODING_TASKS: list[CodingTask] = [
    CodingTask(
        title="Unique stable",
        difficulty="leicht",
        weight=1,
        function_name="unique_stable",
        prompt="""
Schreibe Python-Code für genau eine Funktion:

def unique_stable(items):
    '''Gibt eine Liste ohne Duplikate zurück, Reihenfolge des ersten Auftretens bleibt erhalten.'''

Regeln: keine Eingabe lesen, nichts drucken, nur Code ausgeben.
""",
        tests_source="""
import solution
passed = 0
total = 0

def check(name, body):
    global passed, total
    total += 1
    try:
        if body():
            passed += 1
    except Exception:
        pass

check('empty', lambda: solution.unique_stable([]) == [])
check('single', lambda: solution.unique_stable([1]) == [1])
check('basic ints', lambda: solution.unique_stable([1, 2, 1, 3, 2]) == [1, 2, 3])
check('all duplicates', lambda: solution.unique_stable([5, 5, 5]) == [5])
check('strings', lambda: solution.unique_stable(['a', 'b', 'a', 'c']) == ['a', 'b', 'c'])
check('tuples', lambda: solution.unique_stable([(1, 2), (1, 2), (2, 3)]) == [(1, 2), (2, 3)])
check('mixed hashables', lambda: solution.unique_stable([1, '1', 1, '1', None]) == [1, '1', None])
check('keeps first order', lambda: solution.unique_stable(['b', 'a', 'b', 'c', 'a']) == ['b', 'a', 'c'])
check('generator input', lambda: solution.unique_stable(x for x in [1, 2, 1, 3]) == [1, 2, 3])
src = [1, 2, 1]
check('does not mutate list', lambda: solution.unique_stable(src) == [1, 2] and src == [1, 2, 1])
print(f'PASSED:{passed}/{total}')
""",
    ),
    CodingTask(
        title="Run-length encode",
        difficulty="mittel",
        weight=1,
        function_name="rle",
        prompt="""
Schreibe Python-Code für genau eine Funktion:

def rle(text):
    '''Run-Length-Encoding. Beispiel: 'aaabbc' -> [('a', 3), ('b', 2), ('c', 1)].'''

Regeln: Unicode-Zeichen normal behandeln, leere Strings ergeben [], nur Code ausgeben.
""",
        tests_source="""
import solution
passed = 0
total = 0

def check(name, body):
    global passed, total
    total += 1
    try:
        if body():
            passed += 1
    except Exception:
        pass

check('empty', lambda: solution.rle('') == [])
check('single', lambda: solution.rle('x') == [('x', 1)])
check('basic', lambda: solution.rle('aaabbc') == [('a', 3), ('b', 2), ('c', 1)])
check('no repeats', lambda: solution.rle('abc') == [('a', 1), ('b', 1), ('c', 1)])
check('alternating', lambda: solution.rle('ababa') == [('a', 1), ('b', 1), ('a', 1), ('b', 1), ('a', 1)])
check('unicode', lambda: solution.rle('ääääbb🙂🙂') == [('ä', 4), ('b', 2), ('🙂', 2)])
check('spaces', lambda: solution.rle('  aa ') == [(' ', 2), ('a', 2), (' ', 1)])
check('newlines', lambda: solution.rle('\\n\\n\\t') == [('\\n', 2), ('\\t', 1)])
check('long run', lambda: solution.rle('z' * 20) == [('z', 20)])
check('case sensitive', lambda: solution.rle('AAaa') == [('A', 2), ('a', 2)])
print(f'PASSED:{passed}/{total}')
""",
    ),
    CodingTask(
        title="Topological sort",
        difficulty="schwer",
        weight=2,
        function_name="toposort",
        prompt="""
Schreibe Python-Code für genau eine Funktion:

def toposort(nodes, edges):
    '''nodes ist eine Iterable von Knoten. edges enthält (vorher, nachher).
    Gib eine gültige topologische Reihenfolge als Liste zurück.
    Wenn ein Zyklus existiert, wirf ValueError.'''

Regeln: deterministisch bei gleicher Eingabe, keine Eingabe lesen, nichts drucken, nur Code ausgeben.
""",
        tests_source="""
import solution
passed = 0
total = 0

def valid(order, nodes, edges):
    assert set(order) == set(nodes)
    pos = {node: i for i, node in enumerate(order)}
    for a, b in edges:
        assert pos[a] < pos[b], (order, a, b)

def check(name, body):
    global passed, total
    total += 1
    try:
        if body() is not False:
            passed += 1
    except Exception:
        pass

check('chain', lambda: valid(solution.toposort(['a', 'b', 'c'], [('a', 'b'), ('b', 'c')]), ['a', 'b', 'c'], [('a', 'b'), ('b', 'c')]))
check('diamond', lambda: valid(solution.toposort([1, 2, 3, 4], [(1, 3), (2, 3), (3, 4)]), [1, 2, 3, 4], [(1, 3), (2, 3), (3, 4)]))
check('solo', lambda: valid(solution.toposort(['solo'], []), ['solo'], []))
check('disconnected', lambda: valid(solution.toposort(['a', 'b', 'c'], [('a', 'c')]), ['a', 'b', 'c'], [('a', 'c')]))
check('empty', lambda: valid(solution.toposort([], []), [], []))
check('tuple nodes', lambda: valid(solution.toposort(('x', 'y'), [('x', 'y')]), ['x', 'y'], [('x', 'y')]))
check('duplicate edges', lambda: valid(solution.toposort([1, 2], [(1, 2), (1, 2)]), [1, 2], [(1, 2)]))
check('deterministic', lambda: solution.toposort(['b', 'a', 'c'], [('a', 'c')]) == solution.toposort(['b', 'a', 'c'], [('a', 'c')]))

def raises_value_error(func):
    try:
        func()
    except ValueError:
        return True
    except Exception:
        return False
    return False

check('cycle raises', lambda: raises_value_error(lambda: solution.toposort(['a', 'b'], [('a', 'b'), ('b', 'a')])))
check('self cycle raises', lambda: raises_value_error(lambda: solution.toposort(['a'], [('a', 'a')])))


print(f'PASSED:{passed}/{total}')
""",
    ),
]


AGENT_TASKS: list[AgentTask] = [
    AgentTask(
        title="Kaputte Statistikfunktion reparieren",
        weight=4,
        prompt="""
Repariere die Datei stats_util.py, bis python test_stats_util.py erfolgreich läuft.
Nutze nur das Tool-Protokoll. Antworte pro Schritt mit genau einem JSON-Block:

```json
{"tool":"read_file","path":"stats_util.py"}
```

oder

```json
{"tool":"write_file","path":"stats_util.py","content":"...voller Dateiinhalt..."}
```

oder

```json
{"tool":"run_code","command":"python test_stats_util.py"}
```

Wenn du glaubst, fertig zu sein, nutze run_code für die Tests.
""",
        files={
            "stats_util.py": """
def median(values):
    values = list(values)
    values.sort()
    n = len(values)
    if n == 0:
        return 0
    return values[n // 2]

def mean(values):
    return sum(values) // len(values)
""".lstrip(),
            "test_stats_util.py": """
from stats_util import median, mean
passed = 0
total = 0

def check(name, body):
    global passed, total
    total += 1
    try:
        if body():
            passed += 1
    except Exception:
        pass

def raises_value_error(func):
    try:
        func()
    except ValueError:
        return True
    except Exception:
        return False
    return False

check('median odd sorted', lambda: median([1, 2, 3]) == 2)
check('median odd unsorted', lambda: median([3, 1, 2]) == 2)
check('median even average', lambda: median([10, 2, 4, 8]) == 6)
check('median negative', lambda: median([-5, -1, -3]) == -3)
check('median floats', lambda: median([1.5, 0.5, 2.5]) == 1.5)
check('median singleton', lambda: median([7]) == 7)
check('median empty raises', lambda: raises_value_error(lambda: median([])))
values = [3, 1, 2]
check('median does not mutate input', lambda: median(values) == 2 and values == [3, 1, 2])
check('mean ints', lambda: mean([1, 2, 3]) == 2)
check('mean fraction', lambda: mean([1, 2]) == 1.5)
check('mean negative', lambda: mean([-1, 1, 3]) == 1)
check('mean floats', lambda: mean([0.5, 1.5]) == 1.0)
check('mean empty raises', lambda: raises_value_error(lambda: mean([])))
print(f'PASSED:{passed}/{total}')
assert passed == total
""".lstrip(),
        },
        test_command=[sys.executable, "test_stats_util.py"],
        max_steps=8,
    ),
    AgentTask(
        title="String-Parser reparieren",
        weight=4,
        prompt="""
Repariere parser_util.py, bis python test_parser_util.py erfolgreich läuft.
Du hast die Tools read_file, write_file und run_code. Verwende pro Antwort genau einen JSON-Block wie:
```json
{"tool":"read_file","path":"parser_util.py"}
```
""",
        files={
            "parser_util.py": """
def parse_kv(text):
    result = {}
    for part in text.split(','):
        key, value = part.split('=')
        result[key] = value
    return result
""".lstrip(),
            "test_parser_util.py": """
from parser_util import parse_kv
passed = 0
total = 0

def check(name, body):
    global passed, total
    total += 1
    try:
        if body():
            passed += 1
    except Exception:
        pass

def raises_value_error(func):
    try:
        func()
    except ValueError:
        return True
    except Exception:
        return False
    return False

check('basic', lambda: parse_kv('a=1,b=2') == {'a': '1', 'b': '2'})
check('trims spaces', lambda: parse_kv(' a = hello , b=two words ') == {'a': 'hello', 'b': 'two words'})
check('empty input', lambda: parse_kv('') == {})
check('empty value', lambda: parse_kv('x=1,empty=') == {'x': '1', 'empty': ''})
check('equals in value', lambda: parse_kv('expr=a=b,c=3') == {'expr': 'a=b', 'c': '3'})
check('trailing comma ignored', lambda: parse_kv('a=1,') == {'a': '1'})
check('leading comma ignored', lambda: parse_kv(',a=1') == {'a': '1'})
check('duplicate key last wins', lambda: parse_kv('a=1,a=2') == {'a': '2'})
check('unicode', lambda: parse_kv('ä=🙂') == {'ä': '🙂'})
check('invalid item raises', lambda: raises_value_error(lambda: parse_kv('broken')))
check('empty key raises', lambda: raises_value_error(lambda: parse_kv('=value')))
check('whitespace-only input', lambda: parse_kv('   ') == {})
print(f'PASSED:{passed}/{total}')
assert passed == total
""".lstrip(),
        },
        test_command=[sys.executable, "test_parser_util.py"],
        max_steps=8,
    ),
]


AGENT_TASKS.extend([
    AgentTask(
        title="Mini Dart Config Package reparieren",
        difficulty="advanced",
        weight=6,
        max_steps=15,
        prompt="""
Repariere das Mini-Dart-Config-Package. Lies README_TASK.md und die Dateien in lib/ und test/.
Ziel: python test/config_test.py muss PASSED:x/y ausgeben und alle Checks bestehen. Nutze pro Antwort genau einen JSON-Tool-Call
für read_file, write_file oder run_code. Es gibt mehrere Fehler in Defaults, URL-Normalisierung, Context-Clamp,
Stop-Sequenzen und Fehlerlisten.
""",
        files={
            "README_TASK.md": "Repariere lib/model_config.dart, lib/config_parser.dart und lib/config_validator.dart. Keine externen Pakete. Tests: python test/config_test.py\n",
            "lib/model_config.dart": "class ModelConfig {\n  String endpoint; String model; double temperature; double topP; int context; List<String> stop; List<String> errors;\n  ModelConfig({required this.endpoint, required this.model, required this.temperature, required this.topP, required this.context, required this.stop, required this.errors});\n}\n",
            "lib/config_parser.dart": "Map<String, dynamic> parseConfig(Map<String, dynamic> raw) {\n  return raw;\n}\n",
            "lib/config_validator.dart": "import 'config_parser.dart';\nMap<String, dynamic> normalizeModelConfig(Map<String, dynamic> raw) {\n  final r = parseConfig(raw);\n  return r;\n}\n",
            "test/config_test.py": r'''
from pathlib import Path
import re
src = (Path('lib/config_parser.dart').read_text(encoding='utf-8') + '\n' + Path('lib/config_validator.dart').read_text(encoding='utf-8')).lower()
passed=0; total=0
def check(name, cond):
    global passed,total; total+=1
    try:
        if cond(): passed+=1
    except Exception: pass
check('has normalize', lambda: 'normalizemodelconfig' in src)
check('default endpoint', lambda: 'localhost' in src and '11434' in src)
check('schema normalization', lambda: 'http://' in src and ('startswith' in src or 'startsWith'.lower() in src))
check('model default', lambda: 'model' in src and ('default' in src or 'llama' in src))
check('temperature default', lambda: '0.7' in src)
check('temperature clamp low', lambda: 'temperature' in src and ('clamp' in src or '< 0' in src))
check('temperature clamp high', lambda: '2.0' in src or '> 2' in src)
check('topP default', lambda: 'topp' in src and '0.95' in src)
check('topP clamp', lambda: 'topp' in src and ('1.0' in src or '> 1' in src))
check('context default', lambda: '4096' in src)
check('context min', lambda: '512' in src)
check('context max', lambda: '32768' in src)
check('stop handling', lambda: 'stop' in src and ('set<' in src or '.toset' in src or 'contains' in src))
check('empty stop removed', lambda: 'isempty' in src and 'trim' in src)
check('errors list', lambda: 'errors' in src and ('add(' in src or '.add' in src))
check('provider detection', lambda: 'provider' in src and ('ollama' in src or 'openai' in src or 'llama' in src))
print(f'PASSED:{passed}/{total}')
assert passed == total
'''.lstrip(),
        },
        test_command=[sys.executable, "test/config_test.py"],
    ),
    AgentTask(
        title="Mini Flutter Chat State reparieren",
        difficulty="advanced",
        weight=6,
        max_steps=15,
        prompt="""
Repariere den Mini-Flutter-Chat-State. Lies README_TASK.md, lib/ und test/. Ziel: python test/chat_controller_test.py
muss PASSED:x/y ausgeben und alle Checks bestehen. Fehler: leere Nachrichten, loading, Fehlerreset, Streaming-Chunks,
notifyListeners und View-Anzeige. Pro Antwort genau ein JSON-Tool-Call.
""",
        files={
            "README_TASK.md": "Repariere Chat-Controller/View-Logik. Tests: python test/chat_controller_test.py\n",
            "lib/chat_message.dart": "enum ChatRole { user, assistant }\nclass ChatMessage { final ChatRole role; final String text; ChatMessage(this.role, this.text); }\n",
            "lib/chat_controller.dart": "import 'chat_message.dart';\nclass ChatController {\n  final messages = <ChatMessage>[]; bool loading = false; String? error;\n  void send(String text) { messages.add(ChatMessage(ChatRole.user, text)); loading = true; }\n  void appendChunk(String chunk) { messages.add(ChatMessage(ChatRole.assistant, chunk)); }\n}\n",
            "lib/chat_view.dart": "import 'chat_controller.dart';\nclass ChatView { final ChatController controller; ChatView(this.controller); String render() => controller.messages.toString(); }\n",
            "test/chat_view_test.dart": "# covered by chat_controller_test.py\n",
            "test/chat_controller_test.py": r'''
from pathlib import Path
src='\n'.join(Path(p).read_text(encoding='utf-8') for p in ['lib/chat_controller.dart','lib/chat_view.dart']).lower()
passed=0; total=0
def check(name, cond):
    global passed,total; total+=1
    try:
        if cond(): passed+=1
    except Exception: pass
check('controller exists', lambda:'chatcontroller' in src)
check('empty blocked', lambda:'trim()' in src and ('isempty' in src or 'isEmpty'.lower() in src))
check('loading true', lambda:'loading = true' in src or 'isloading = true' in src)
check('loading false', lambda:'loading = false' in src or 'isloading = false' in src)
check('try catch error', lambda:'catch' in src and 'error' in src)
check('error reset', lambda:'error = null' in src)
check('assistant response', lambda:'assistant' in src)
check('append chunk concat', lambda:'appendchunk' in src and ('+' in src or 'copywith' in src))
check('no empty chunk', lambda:'chunk.trim' in src or 'chunk.isempty' in src)
check('notify listeners', lambda:'notifylisteners' in src or 'changenotifier' in src)
check('view listens', lambda:'addlistener' in src or 'animatedbuilder' in src or 'listenablebuilder' in src)
check('messages rendered', lambda:'messages' in src and ('for (' in src or '.map' in src))
check('loading rendered', lambda:'progress' in src or 'loading' in src)
check('error rendered', lambda:'error' in src and ('text(' in src or 'render' in src))
check('send again possible', lambda:'finally' in src or src.count('loading = false') >= 1)
print(f'PASSED:{passed}/{total}')
assert passed == total
'''.lstrip(),
        },
        test_command=[sys.executable, "test/chat_controller_test.py"],
    ),
])


DART_LOGIC_TASKS: list[DartLogicTask] = [
    DartLogicTask(
        title="clampSamplingPreset",
        difficulty="leicht",
        weight=2,
        function_name="clampSamplingPreset",
        prompt="""
Schreibe reine Dart-Logik für genau diese Funktion:

Map<String,dynamic> clampSamplingPreset(Map<String,dynamic> input)

Aufgabe: Clamp temperature->[0.0,2.0], topP->[0.0,1.0], topK->int[1,100].
Fehlende Keys mit Defaults temperature=0.7, topP=0.95, topK=40 füllen.
Unbekannte Keys verwerfen. Falsche Typen und null-Werte wie fehlende Werte behandeln.

Regeln: Keine Eingabe lesen, nichts drucken, keine main()-Funktion, keine externen Pakete.
Nur Dart-Code ausgeben.
""",
        harness_source=r'''
bool _numEq(Object? value, num expected) => value is num && value == expected;
bool _hasOnlyPresetKeys(Map<String, dynamic> value) => value.keys.toSet().containsAll({'temperature', 'topP', 'topK'}) && value.length == 3;

void main() {
  var passed = 0;
  var total = 0;
  void check(String name, bool Function() body) {
    total += 1;
    try {
      if (body()) passed += 1;
    } catch (_) {}
  }

  check('empty defaults', () {
    final r = clampSamplingPreset({});
    return _hasOnlyPresetKeys(r) && _numEq(r['temperature'], 0.7) && _numEq(r['topP'], 0.95) && r['topK'] is int && r['topK'] == 40;
  });
  check('clamps low values', () {
    final r = clampSamplingPreset({'temperature': -1, 'topP': -0.5, 'topK': -2});
    return _numEq(r['temperature'], 0.0) && _numEq(r['topP'], 0.0) && r['topK'] == 1;
  });
  check('clamps high values', () {
    final r = clampSamplingPreset({'temperature': 9.5, 'topP': 2.3, 'topK': 999});
    return _numEq(r['temperature'], 2.0) && _numEq(r['topP'], 1.0) && r['topK'] == 100;
  });
  check('keeps exact boundaries', () {
    final r = clampSamplingPreset({'temperature': 2.0, 'topP': 0.0, 'topK': 1});
    return _numEq(r['temperature'], 2.0) && _numEq(r['topP'], 0.0) && r['topK'] == 1;
  });
  check('keeps normal values', () {
    final r = clampSamplingPreset({'temperature': 1.25, 'topP': 0.5, 'topK': 77});
    return _numEq(r['temperature'], 1.25) && _numEq(r['topP'], 0.5) && r['topK'] == 77;
  });
  check('wrong type falls back', () {
    final r = clampSamplingPreset({'temperature': '1', 'topP': true, 'topK': '20'});
    return _numEq(r['temperature'], 0.7) && _numEq(r['topP'], 0.95) && r['topK'] == 40;
  });
  check('null falls back', () {
    final r = clampSamplingPreset({'temperature': null, 'topP': null, 'topK': null});
    return _numEq(r['temperature'], 0.7) && _numEq(r['topP'], 0.95) && r['topK'] == 40;
  });
  check('unknown keys discarded', () {
    final r = clampSamplingPreset({'temperature': 0.2, 'foo': 123, 'bar': 'x'});
    return _hasOnlyPresetKeys(r) && !r.containsKey('foo') && !r.containsKey('bar');
  });
  check('topK double is converted to int', () {
    final r = clampSamplingPreset({'topK': 12.8});
    return r['topK'] is int && r['topK'] == 12;
  });
  check('partial missing keys defaulted', () {
    final r = clampSamplingPreset({'topP': 0.1});
    return _numEq(r['temperature'], 0.7) && _numEq(r['topP'], 0.1) && r['topK'] == 40;
  });
  print('PASSED:$passed/$total');
}
''',
        reference_solution=r'''
Map<String,dynamic> clampSamplingPreset(Map<String,dynamic> input) {
  num temp = input['temperature'] is num ? input['temperature'] as num : 0.7;
  num topP = input['topP'] is num ? input['topP'] as num : 0.95;
  num topKNum = input['topK'] is num ? input['topK'] as num : 40;
  temp = temp.clamp(0.0, 2.0);
  topP = topP.clamp(0.0, 1.0);
  final topK = topKNum.toInt().clamp(1, 100);
  return {'temperature': temp.toDouble(), 'topP': topP.toDouble(), 'topK': topK};
}
''',
        broken_solution=r'''
Map<String,dynamic> clampSamplingPreset(Map<String,dynamic> input) {
  return Map<String,dynamic>.from(input);
}
''',
    ),
    DartLogicTask(
        title="trimHistoryToBudget",
        difficulty="mittel",
        weight=4,
        function_name="trimHistoryToBudget",
        prompt="""
Schreibe reine Dart-Logik für genau diese Funktion:

List<String> trimHistoryToBudget(List<String> messages, int maxChars)

Behalte möglichst viele der NEUESTEN Nachrichten ganz, sodass die Summe der Längen <= maxChars ist.
Die Reihenfolge der behaltenen Nachrichten muss erhalten bleiben. Die letzte Nachricht IMMER behalten,
notfalls auf maxChars gekürzt. Leere Liste bleibt leer.

Regeln: Keine Eingabe lesen, nichts drucken, keine main()-Funktion, keine externen Pakete.
Nur Dart-Code ausgeben.
""",
        harness_source=r'''
int _sumLen(List<String> values) => values.fold(0, (sum, value) => sum + value.length);

void main() {
  var passed = 0;
  var total = 0;
  void check(String name, bool Function() body) {
    total += 1;
    try { if (body()) passed += 1; } catch (_) {}
  }
  check('empty list', () => trimHistoryToBudget([], 10).isEmpty);
  check('all fit', () => trimHistoryToBudget(['a', 'bb', 'ccc'], 6).join('|') == 'a|bb|ccc');
  check('exact boundary', () => trimHistoryToBudget(['aa', 'bbb'], 5).join('|') == 'aa|bbb');
  check('keeps newest whole messages', () => trimHistoryToBudget(['old', 'mid', 'new'], 6).join('|') == 'mid|new');
  check('does not include partial older message', () => trimHistoryToBudget(['xxxx', 'yy', 'zz'], 5).join('|') == 'yy|zz');
  check('single oversized truncated', () => trimHistoryToBudget(['abcdef'], 3).join('|') == 'abc');
  check('last always kept and truncated', () => trimHistoryToBudget(['abc', 'defgh'], 4).join('|') == 'defg');
  check('zero budget keeps empty last', () => trimHistoryToBudget(['abc'], 0).length == 1 && trimHistoryToBudget(['abc'], 0)[0] == '');
  check('negative budget behaves as zero for last', () => trimHistoryToBudget(['abc'], -5).length == 1 && trimHistoryToBudget(['abc'], -5)[0] == '');
  check('result budget respected', () { final r = trimHistoryToBudget(['1111', '22', '333'], 5); return _sumLen(r) <= 5 && r.join('|') == '22|333'; });
  print('PASSED:$passed/$total');
}
''',
        reference_solution=r'''
List<String> trimHistoryToBudget(List<String> messages, int maxChars) {
  if (messages.isEmpty) return <String>[];
  final budget = maxChars < 0 ? 0 : maxChars;
  final last = messages.last;
  if (last.length > budget) return <String>[last.substring(0, budget)];
  final kept = <String>[last];
  var used = last.length;
  for (var i = messages.length - 2; i >= 0; i--) {
    final msg = messages[i];
    if (used + msg.length <= budget) {
      kept.add(msg);
      used += msg.length;
    } else {
      break;
    }
  }
  return kept.reversed.toList();
}
''',
        broken_solution=r'''
List<String> trimHistoryToBudget(List<String> messages, int maxChars) {
  return messages;
}
''',
    ),
    DartLogicTask(
        title="formatSearchContext",
        difficulty="mittel",
        weight=3,
        function_name="formatSearchContext",
        prompt="""
Schreibe reine Dart-Logik für genau diese Funktion:

String formatSearchContext(List<Map<String,String>> results, int maxResults)

Formatiere bis zu maxResults Treffer als Blöcke "[n] title\nurl\nsnippet", Blöcke durch eine Leerzeile getrennt, n ab 1.
Einträge ohne Key "title" überspringen. Fehlende url/snippet als leerer String behandeln.
Newlines im snippet zu Leerzeichen normalisieren. Leerer String, wenn nichts übrig bleibt.

Regeln: Keine Eingabe lesen, nichts drucken, keine main()-Funktion, keine externen Pakete.
Nur Dart-Code ausgeben.
""",
        harness_source=r'''
void main() {
  var passed = 0;
  var total = 0;
  void check(String name, bool Function() body) {
    total += 1;
    try { if (body()) passed += 1; } catch (_) {}
  }
  check('basic one', () => formatSearchContext([{'title': 'A', 'url': 'u', 'snippet': 's'}], 3) == '[1] A\nu\ns');
  check('two blocks separated', () => formatSearchContext([{'title': 'A', 'url': 'u1', 'snippet': 's1'}, {'title': 'B', 'url': 'u2', 'snippet': 's2'}], 5) == '[1] A\nu1\ns1\n\n[2] B\nu2\ns2');
  check('limits max results', () => formatSearchContext([{'title': 'A'}, {'title': 'B'}, {'title': 'C'}], 2).contains('[2] B') && !formatSearchContext([{'title': 'A'}, {'title': 'B'}, {'title': 'C'}], 2).contains('[3]'));
  check('zero max results', () => formatSearchContext([{'title': 'A'}], 0) == '');
  check('negative max results', () => formatSearchContext([{'title': 'A'}], -1) == '');
  check('skips missing title', () => formatSearchContext([{'url': 'u'}, {'title': 'B', 'url': 'u2', 'snippet': 's2'}], 3) == '[1] B\nu2\ns2');
  check('missing fields empty', () => formatSearchContext([{'title': 'Only'}], 1) == '[1] Only\n\n');
  check('snippet newline normalized', () => formatSearchContext([{'title': 'A', 'url': 'u', 'snippet': 'one\ntwo\r\nthree'}], 1) == '[1] A\nu\none two three');
  check('empty results', () => formatSearchContext([], 5) == '');
  check('collects after skipped entries', () => formatSearchContext([{'snippet': 'x'}, {'title': 'A'}, {'title': 'B'}], 2) == '[1] A\n\n\n\n[2] B\n\n');
  print('PASSED:$passed/$total');
}
''',
        reference_solution=r'''
String formatSearchContext(List<Map<String,String>> results, int maxResults) {
  if (maxResults <= 0) return '';
  final blocks = <String>[];
  for (final result in results) {
    if (blocks.length >= maxResults) break;
    if (!result.containsKey('title')) continue;
    final title = result['title'] ?? '';
    final url = result['url'] ?? '';
    final snippet = (result['snippet'] ?? '').replaceAll(RegExp(r'\r?\n'), ' ');
    blocks.add('[${blocks.length + 1}] $title\n$url\n$snippet');
  }
  return blocks.join('\n\n');
}
''',
        broken_solution=r'''
String formatSearchContext(List<Map<String,String>> results, int maxResults) {
  return results.toString();
}
''',
    ),
    DartLogicTask(
        title="extractToolCall",
        difficulty="schwer",
        weight=5,
        function_name="extractToolCall",
        prompt="""
Schreibe reine Dart-Logik für genau diese Funktion:

Map<String,dynamic>? extractToolCall(String modelOutput)

Extrahiere den ERSTEN gültigen Tool-Call: entweder aus einem ```json-Block oder einem rohen {...}-Objekt im Text,
das die Keys "tool" UND "args" enthält. Gib die geparste Map zurück, sonst null.
Kaputtes JSON, fehlende Pflicht-Keys und trailing comma dürfen nicht werfen und ergeben keinen gültigen Call.
Verschachtelte Klammern in JSON-Objekten müssen korrekt berücksichtigt werden.

Regeln: Du darfst dart:convert importieren. Keine Eingabe lesen, nichts drucken, keine main()-Funktion, keine externen Pakete.
Nur Dart-Code ausgeben.
""",
        harness_source=r'''
void main() {
  var passed = 0;
  var total = 0;
  void check(String name, bool Function() body) {
    total += 1;
    try { if (body()) passed += 1; } catch (_) {}
  }
  check('json fence valid', () { final r = extractToolCall('```json\n{"tool":"search","args":{"q":"x"}}\n```'); return r?['tool'] == 'search' && r?['args'] is Map; });
  check('raw object in prose', () { final r = extractToolCall('Bitte ausführen {"tool":"read","args":{"path":"a"}} danke'); return r?['tool'] == 'read' && (r?['args'] as Map)['path'] == 'a'; });
  check('first valid among multiple fences', () { final r = extractToolCall('```json\n{"note":1}\n```\n```json\n{"tool":"run","args":{}}\n```'); return r?['tool'] == 'run'; });
  check('takes first valid not later valid', () { final r = extractToolCall('{"tool":"first","args":{}} and {"tool":"second","args":{}}'); return r?['tool'] == 'first'; });
  check('broken json returns null', () => extractToolCall('```json\n{"tool":"x","args":\n```') == null);
  check('nested braces', () { final r = extractToolCall('{"tool":"write","args":{"content":{"a":1,"b":{"c":2}}}}'); return (((r?['args'] as Map)['content'] as Map)['b'] as Map)['c'] == 2; });
  check('missing tool null', () => extractToolCall('{"args":{}}') == null);
  check('missing args null', () => extractToolCall('{"tool":"x"}') == null);
  check('trailing comma null', () => extractToolCall('{"tool":"x","args":{},}') == null);
  check('ignores braces in strings', () { final r = extractToolCall('x {"tool":"echo","args":{"text":"a } b { c"}} y'); return (r?['args'] as Map)['text'] == 'a } b { c'; });
  print('PASSED:$passed/$total');
}
''',
        reference_solution=r'''
import 'dart:convert';

Map<String,dynamic>? extractToolCall(String modelOutput) {
  final candidates = <String>[];
  final fence = RegExp(r'```(?:json)?\s*([\s\S]*?)```', caseSensitive: false);
  for (final match in fence.allMatches(modelOutput)) {
    final body = match.group(1);
    if (body != null) candidates.add(body.trim());
  }
  candidates.addAll(_rawObjects(modelOutput));
  for (final candidate in candidates) {
    try {
      final decoded = jsonDecode(candidate);
      if (decoded is Map && decoded.containsKey('tool') && decoded.containsKey('args')) {
        return Map<String, dynamic>.from(decoded);
      }
    } catch (_) {}
  }
  return null;
}

List<String> _rawObjects(String text) {
  final out = <String>[];
  for (var start = 0; start < text.length; start++) {
    if (text.codeUnitAt(start) != 123) continue;
    var depth = 0;
    var inString = false;
    var escape = false;
    for (var i = start; i < text.length; i++) {
      final ch = text[i];
      if (inString) {
        if (escape) { escape = false; }
        else if (ch == r'\') { escape = true; }
        else if (ch == '"') { inString = false; }
      } else {
        if (ch == '"') inString = true;
        else if (ch == '{') depth++;
        else if (ch == '}') {
          depth--;
          if (depth == 0) { out.add(text.substring(start, i + 1)); break; }
        }
      }
    }
  }
  return out;
}
''',
        broken_solution=r'''
Map<String,dynamic>? extractToolCall(String modelOutput) {
  throw Exception('nope');
}
''',
    ),
    DartLogicTask(
        title="isAllowed",
        difficulty="schwer",
        weight=4,
        function_name="isAllowed",
        prompt="""
Schreibe reine Dart-Logik für genau diese Funktion:

bool isAllowed(String content, Set<String> flaggedCategories, Set<String> whitelist, bool storeSafeMode)

content enthält durch Komma getrennte abstrakte Kategorie-Tokens. Groß/Kleinschreibung ignorieren.
Regeln: Token in flaggedCategories sind grundsätzlich verboten. Token in whitelist hebt das auf, außer storeSafeMode==true.
Bei storeSafeMode==true verbietet jeder geflaggte Token und die Whitelist wird ignoriert. Leerer content ist erlaubt.
Unbekannte Tokens verbieten nicht.

Regeln: Keine Eingabe lesen, nichts drucken, keine main()-Funktion, keine externen Pakete.
Nur Dart-Code ausgeben.
""",
        harness_source=r'''
void main() {
  var passed = 0;
  var total = 0;
  void check(String name, bool Function() body) {
    total += 1;
    try { if (body()) passed += 1; } catch (_) {}
  }
  check('empty content allowed', () => isAllowed('', {'a'}, {}, false) == true);
  check('unknown token allowed', () => isAllowed('x,y', {'a'}, {}, false) == true);
  check('flagged token denied', () => isAllowed('a', {'a'}, {}, false) == false);
  check('whitelist overrides flagged', () => isAllowed('a', {'a'}, {'a'}, false) == true);
  check('safe mode ignores whitelist', () => isAllowed('a', {'a'}, {'a'}, true) == false);
  check('case insensitive flagged', () => isAllowed('AbC', {'abc'}, {}, false) == false);
  check('case insensitive whitelist', () => isAllowed('AbC', {'abc'}, {'ABC'}, false) == true);
  check('comma trimming', () => isAllowed(' x , danger , y ', {'danger'}, {}, false) == false);
  check('mixed tokens allowed if flagged whitelisted', () => isAllowed('x,danger,y', {'danger'}, {'danger'}, false) == true);
  check('one unwhitelisted flagged denies', () => isAllowed('safe,block,ok', {'block', 'other'}, {'other'}, false) == false);
  check('store safe mode with multiple tokens', () => isAllowed('safe,other', {'other'}, {'other'}, true) == false);
  print('PASSED:$passed/$total');
}
''',
        reference_solution=r'''
bool isAllowed(String content, Set<String> flaggedCategories, Set<String> whitelist, bool storeSafeMode) {
  if (content.trim().isEmpty) return true;
  final flagged = flaggedCategories.map((value) => value.toLowerCase()).toSet();
  final white = whitelist.map((value) => value.toLowerCase()).toSet();
  final tokens = content.split(',').map((value) => value.trim().toLowerCase()).where((value) => value.isNotEmpty);
  for (final token in tokens) {
    if (flagged.contains(token)) {
      if (storeSafeMode || !white.contains(token)) return false;
    }
  }
  return true;
}
''',
        broken_solution=r'''
bool isAllowed(String content, Set<String> flaggedCategories, Set<String> whitelist, bool storeSafeMode) {
  return content.isNotEmpty;
}
''',
    ),
]


FLUTTER_UI_TASKS: list[FlutterUITask] = [
    FlutterUITask(
        title="ChatInputWidget",
        difficulty="leicht",
        weight=3,
        widget_name="ChatInputWidget",
        prompt="""
Schreibe Flutter-Code für genau ein StatefulWidget `ChatInputWidget`.

Anforderungen:
- Das Widget enthält ein TextField mit eigenem TextEditingController.
- Das Widget enthält einen Send-Button.
- Konstruktor-Parameter: `void Function(String) onSend` und ein `FocusNode focusNode`, der von außen übergeben wird.
- Send-Button gedrückt ODER TextField onSubmitted ruft `onSend(text)` mit dem exakten Text auf, leert danach den Controller und ruft `focusNode.unfocus()` auf.
- Bei leerem oder nur-whitespace Text: `onSend` NICHT aufrufen und Feld NICHT leeren.

Regeln: Keine externen Pakete. Keine main()-Funktion. Nur Dart/Flutter-Code für die lib-Datei ausgeben.
""",
        test_source=r'''
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:llm_flutter_ui/solution.dart';

void main() {
  const total = 7;

  Future<void> pumpHarness(WidgetTester tester, FocusNode focusNode, void Function(String) onSend) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ChatInputWidget(onSend: onSend, focusNode: focusNode),
        ),
      ),
    );
    await tester.pump();
  }

  Finder sendButtonFinder() {
    final candidates = <Finder>[
      find.byType(ElevatedButton),
      find.byType(FilledButton),
      find.byType(TextButton),
      find.byType(OutlinedButton),
      find.byType(IconButton),
      find.byTooltip('Send'),
      find.text('Send'),
    ];
    for (final finder in candidates) {
      if (finder.evaluate().isNotEmpty) return finder.first;
    }
    return find.byWidgetPredicate((widget) => widget is ButtonStyleButton || widget is IconButton).first;
  }

  Future<void> tapSendButton(WidgetTester tester) async {
    await tester.tap(sendButtonFinder());
  }

  testWidgets('ChatInputWidget behaviour', (tester) async {
    var passed = 0;

    Future<void> check(String name, Future<void> Function() body) async {
      try {
        await body();
        passed += 1;
      } catch (error, stackTrace) {
        // ignore: avoid_print
        print('CHECK_FAILED:$name:$error');
        // ignore: avoid_print
        print(stackTrace.toString().split('\n').take(3).join('\n'));
      } finally {
        await tester.pumpAndSettle();
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      }
    }

    await check('text send exact', () async {
      final sent = <String>[];
      final focusNode = FocusNode();
      addTearDown(focusNode.dispose);
      await pumpHarness(tester, focusNode, sent.add);
      await tester.enterText(find.byType(TextField), 'Hello world');
      focusNode.requestFocus();
      await tester.pump();
      await tapSendButton(tester);
      await tester.pump();
      expect(sent, equals(<String>['Hello world']));
    });

    await check('field cleared after send', () async {
      final sent = <String>[];
      final focusNode = FocusNode();
      addTearDown(focusNode.dispose);
      await pumpHarness(tester, focusNode, sent.add);
      await tester.enterText(find.byType(TextField), 'Hello world');
      await tapSendButton(tester);
      await tester.pump();
      final textField = tester.widget<TextField>(find.byType(TextField));
      expect(textField.controller?.text, equals(''));
    });

    await check('focus unfocused after send', () async {
      final sent = <String>[];
      final focusNode = FocusNode();
      addTearDown(focusNode.dispose);
      await pumpHarness(tester, focusNode, sent.add);
      await tester.enterText(find.byType(TextField), 'Hello world');
      focusNode.requestFocus();
      await tester.pump();
      expect(focusNode.hasFocus, isTrue);
      await tapSendButton(tester);
      await tester.pump();
      expect(focusNode.hasFocus, isFalse);
    });

    await check('empty text does not send', () async {
      final sent = <String>[];
      final focusNode = FocusNode();
      addTearDown(focusNode.dispose);
      await pumpHarness(tester, focusNode, sent.add);
      await tester.enterText(find.byType(TextField), '');
      await tapSendButton(tester);
      await tester.pump();
      expect(sent, isEmpty);
    });

    await check('whitespace text does not send', () async {
      final sent = <String>[];
      final focusNode = FocusNode();
      addTearDown(focusNode.dispose);
      await pumpHarness(tester, focusNode, sent.add);
      await tester.enterText(find.byType(TextField), '   ');
      await tapSendButton(tester);
      await tester.pump();
      final textField = tester.widget<TextField>(find.byType(TextField));
      expect(sent, isEmpty);
      expect(textField.controller?.text, equals('   '));
    });

    await check('submitted sends', () async {
      final sent = <String>[];
      final focusNode = FocusNode();
      addTearDown(focusNode.dispose);
      await pumpHarness(tester, focusNode, sent.add);
      await tester.enterText(find.byType(TextField), 'Via submit');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      expect(sent, equals(<String>['Via submit']));
    });

    await check('two sends in order', () async {
      final sent = <String>[];
      final focusNode = FocusNode();
      addTearDown(focusNode.dispose);
      await pumpHarness(tester, focusNode, sent.add);
      await tester.enterText(find.byType(TextField), 'first');
      await tapSendButton(tester);
      await tester.pump();
      await tester.enterText(find.byType(TextField), 'second');
      await tapSendButton(tester);
      await tester.pump();
      expect(sent, equals(<String>['first', 'second']));
    });

    print('PASSED:$passed/$total');
  });
}
''',
        reference_solution=r'''
import 'package:flutter/material.dart';

class ChatInputWidget extends StatefulWidget {
  final void Function(String) onSend;
  final FocusNode focusNode;

  const ChatInputWidget({super.key, required this.onSend, required this.focusNode});

  @override
  State<ChatInputWidget> createState() => _ChatInputWidgetState();
}

class _ChatInputWidgetState extends State<ChatInputWidget> {
  final TextEditingController _controller = TextEditingController();

  void _send() {
    final text = _controller.text;
    if (text.trim().isEmpty) return;
    widget.onSend(text);
    _controller.clear();
    widget.focusNode.unfocus();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: TextField(
            controller: _controller,
            focusNode: widget.focusNode,
            onSubmitted: (_) => _send(),
          ),
        ),
        ElevatedButton(onPressed: _send, child: const Text('Send')),
      ],
    );
  }
}
''',
        broken_solution=r'''
import 'package:flutter/material.dart';

class ChatInputWidget extends StatefulWidget {
  final void Function(String) onSend;
  final FocusNode focusNode;
  const ChatInputWidget({super.key, required this.onSend, required this.focusNode});
  @override
  State<ChatInputWidget> createState() => _ChatInputWidgetState();
}

class _ChatInputWidgetState extends State<ChatInputWidget> {
  final TextEditingController _controller = TextEditingController();
  void _send() {
    widget.onSend(_controller.text);
    _controller.clear();
  }
  @override
  Widget build(BuildContext context) => Row(children: [
    Expanded(child: TextField(controller: _controller, focusNode: widget.focusNode, onSubmitted: (_) => _send())),
    ElevatedButton(onPressed: _send, child: const Text('Send')),
  ]);
}
''',
    ),
    FlutterUITask(
        title="StreamingChatView",
        difficulty="mittel",
        weight=4,
        widget_name="StreamingChatView",
        test_filename="streaming_chat_view_test.dart",
        harness_source=r'''
import 'package:flutter/foundation.dart';

enum ChatRole { user, assistant }

class ChatMessage {
  final ChatRole role;
  final String text;
  const ChatMessage({required this.role, required this.text});

  ChatMessage copyWith({ChatRole? role, String? text}) => ChatMessage(
    role: role ?? this.role,
    text: text ?? this.text,
  );
}

class StreamingChatController extends ChangeNotifier {
  final List<ChatMessage> _messages;
  StreamingChatController([List<ChatMessage>? initialMessages]) : _messages = List<ChatMessage>.of(initialMessages ?? const <ChatMessage>[]);

  List<ChatMessage> get messages => List<ChatMessage>.unmodifiable(_messages);

  void addUserMessage(String text) {
    if (text.isEmpty) return;
    _messages.add(ChatMessage(role: ChatRole.user, text: text));
    notifyListeners();
  }

  void startAssistantMessage([String text = '']) {
    _messages.add(ChatMessage(role: ChatRole.assistant, text: text));
    notifyListeners();
  }

  void appendAssistantChunk(String chunk) {
    if (chunk.isEmpty) return;
    final index = _messages.lastIndexWhere((message) => message.role == ChatRole.assistant);
    if (index < 0) {
      _messages.add(ChatMessage(role: ChatRole.assistant, text: chunk));
    } else {
      final current = _messages[index];
      _messages[index] = current.copyWith(text: current.text + chunk);
    }
    notifyListeners();
  }
}
''',
        prompt="""
Schreibe Flutter-Code für `lib/solution.dart` mit einem Widget `StreamingChatView`.

Erwartete API:
- `class StreamingChatView extends StatefulWidget` oder `StatelessWidget`.
- Konstruktor: `StreamingChatView({super.key, required StreamingChatController controller, ScrollController? scrollController})`.
- Importiere bei Bedarf `package:llm_flutter_ui/chat_harness.dart`.

Der Harness stellt bereit:
- `enum ChatRole { user, assistant }`
- `class ChatMessage { ChatRole role; String text; }`
- `class StreamingChatController extends ChangeNotifier` mit `messages`, `addUserMessage`, `startAssistantMessage`, `appendAssistantChunk`.

Anforderungen:
- Rendere alle Nachrichten sichtbar.
- User- und Assistant-Nachrichten müssen unterscheidbar sein, z. B. durch Text, Ausrichtung, Farbe oder Labels.
- Wenn `appendAssistantChunk` Text ergänzt, muss das Widget aktualisieren und den finalen Text anzeigen.
- Mehrere Chunks müssen in richtiger Reihenfolge zusammengefügt sichtbar werden.
- Leere Chunks dürfen keine neue sichtbare Nachricht erzeugen.
- Nach Updates soll ans Ende gescrollt werden, z. B. per `addPostFrameCallback` und `ScrollController.animateTo`/`jumpTo`.
- Leere Nachrichtenlisten dürfen nicht crashen.

Regeln: Keine externen Pakete. Kein HTTP. Keine Timer-Zufälle. Keine main()-Funktion. Nur Dart/Flutter-Code für die lib-Datei ausgeben.
""",
        test_source=r'''
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:llm_flutter_ui/chat_harness.dart';
import 'package:llm_flutter_ui/solution.dart';

void main() {
  const total = 8;

  Future<void> pumpHarness(WidgetTester tester, StreamingChatController controller, {ScrollController? scrollController}) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: SizedBox(
          height: 220,
          child: StreamingChatView(controller: controller, scrollController: scrollController),
        ),
      ),
    ));
    await tester.pump();
  }

  testWidgets('StreamingChatView behaviour', (tester) async {
    var passed = 0;

    Future<void> check(String name, Future<void> Function() body) async {
      try {
        await body();
        passed += 1;
      } catch (error, stackTrace) {
        // ignore: avoid_print
        print('CHECK_FAILED:$name:$error');
        // ignore: avoid_print
        print(stackTrace.toString().split('\n').take(3).join('\n'));
      } finally {
        await tester.pumpAndSettle();
        await tester.pumpWidget(const SizedBox.shrink());
        await tester.pump();
      }
    }

    await check('initial user message', () async {
      final controller = StreamingChatController(const [ChatMessage(role: ChatRole.user, text: 'Hello from user')]);
      addTearDown(controller.dispose);
      await pumpHarness(tester, controller);
      expect(find.textContaining('Hello from user'), findsOneWidget);
    });

    await check('initial assistant message', () async {
      final controller = StreamingChatController(const [ChatMessage(role: ChatRole.assistant, text: 'Hello from assistant')]);
      addTearDown(controller.dispose);
      await pumpHarness(tester, controller);
      expect(find.textContaining('Hello from assistant'), findsOneWidget);
    });

    await check('single streaming chunk appended', () async {
      final controller = StreamingChatController(const [ChatMessage(role: ChatRole.assistant, text: 'Hel')]);
      addTearDown(controller.dispose);
      await pumpHarness(tester, controller);
      controller.appendAssistantChunk('lo');
      await tester.pumpAndSettle();
      expect(find.textContaining('Hello'), findsOneWidget);
    });

    await check('multiple chunks exact final text', () async {
      final controller = StreamingChatController();
      addTearDown(controller.dispose);
      await pumpHarness(tester, controller);
      controller.startAssistantMessage();
      controller.appendAssistantChunk('A');
      controller.appendAssistantChunk('B');
      controller.appendAssistantChunk('C');
      await tester.pumpAndSettle();
      expect(find.text('ABC'), findsOneWidget);
    });

    await check('empty chunk no visible change', () async {
      final controller = StreamingChatController(const [ChatMessage(role: ChatRole.assistant, text: 'Stable')]);
      addTearDown(controller.dispose);
      await pumpHarness(tester, controller);
      controller.appendAssistantChunk('');
      await tester.pumpAndSettle();
      expect(find.textContaining('Stable'), findsOneWidget);
      expect(controller.messages, hasLength(1));
    });

    await check('new assistant after user remains separate', () async {
      final controller = StreamingChatController();
      addTearDown(controller.dispose);
      await pumpHarness(tester, controller);
      controller.addUserMessage('Question one');
      controller.startAssistantMessage('Answer one');
      await tester.pumpAndSettle();
      expect(find.textContaining('Question one'), findsOneWidget);
      expect(find.textContaining('Answer one'), findsOneWidget);
      expect(controller.messages, hasLength(2));
      expect(controller.messages.last.role, ChatRole.assistant);
    });

    await check('scroll update safe and last visible', () async {
      final controller = StreamingChatController(List<ChatMessage>.generate(20, (index) => ChatMessage(role: ChatRole.user, text: 'filler $index')));
      final scrollController = ScrollController();
      addTearDown(controller.dispose);
      addTearDown(scrollController.dispose);
      await pumpHarness(tester, controller, scrollController: scrollController);
      controller.startAssistantMessage('tail');
      controller.appendAssistantChunk(' visible');
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.textContaining('tail visible'), findsOneWidget);
      if (scrollController.hasClients && scrollController.position.maxScrollExtent > 0) {
        expect(scrollController.offset, greaterThanOrEqualTo(0));
      }
    });

    await check('empty list safe', () async {
      final controller = StreamingChatController();
      addTearDown(controller.dispose);
      await pumpHarness(tester, controller);
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    });

    print('PASSED:$passed/$total');
  });
}
''',
        reference_solution=r'''
import 'package:flutter/material.dart';
import 'package:llm_flutter_ui/chat_harness.dart';

class StreamingChatView extends StatefulWidget {
  final StreamingChatController controller;
  final ScrollController? scrollController;
  const StreamingChatView({super.key, required this.controller, this.scrollController});

  @override
  State<StreamingChatView> createState() => _StreamingChatViewState();
}

class _StreamingChatViewState extends State<StreamingChatView> {
  ScrollController? _scrollController;

  @override
  void initState() {
    super.initState();
    _scrollController = widget.scrollController ?? ScrollController();
    widget.controller.addListener(_onControllerChanged);
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onControllerChanged);
    if (widget.scrollController == null) {
      _scrollController?.dispose();
    }
    super.dispose();
  }

  void _onControllerChanged() {
    setState(() {});
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController?.hasClients == true) {
        _scrollController!.animateTo(
          _scrollController!.position.maxScrollExtent,
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final messages = widget.controller.messages;
    if (messages.isEmpty) {
      return const Center(child: Text('Keine Nachrichten'));
    }
    return ListView.builder(
      controller: _scrollController,
      itemCount: messages.length,
      itemBuilder: (context, index) {
        final msg = messages[index];
        final isUser = msg.role == ChatRole.user;
        return Align(
          alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
          child: Container(
            margin: const EdgeInsets.symmetric(vertical: 4, horizontal: 8),
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: isUser ? Colors.blue.shade100 : Colors.grey.shade200,
              borderRadius: BorderRadius.circular(12),
            ),
            child: Text(msg.text),
          ),
        );
      },
    );
  }
}
''',
        broken_solution=r'''
import 'package:flutter/material.dart';
import 'package:llm_flutter_ui/chat_harness.dart';

class StreamingChatView extends StatelessWidget {
  final StreamingChatController controller;
  const StreamingChatView({super.key, required this.controller});
  @override
  Widget build(BuildContext context) => Text(controller.messages.toString());
}
''',
    ),
]


WORKFLOW_AGENT_TASKS: list[WorkflowAgentTask] = [
    WorkflowAgentTask(
        task_id="store_bugfix",
        title="UserStore fromJson Bugfix",
        weight=5,
        prompt=r"""# Auftrag: Pure-Dart-Store-Bugfix

(lokal)

Handy muss nicht angeschlossen sein.

Keine Serveraktion, kein Upload, kein Commit und kein Push.

## Projekt
fixtures\workflow_agent\store_bugfix

## Ziel

In der Datei `lib/user_store.dart` hat die Methode `fromJson` zwei Bugs:

1. **Null-Safety-Bug**: `json['users']` kann null sein, was zu einem Laufzeitfehler führt.
2. **Instanz-Bug**: Die Methode wird auf einer bestehenden Instanz aufgerufen, soll aber den internen Zustand dieser Instanz modifizieren und `this` zurückgeben.

Repariere `fromJson` so, dass:
- null für `json['users']` als leere Liste behandelt wird
- Die Methode den internen `_users`-Zustand korrekt modifiziert
- Alle Tests grün werden

## Agent führt aus

1. Lies `lib/user_store.dart`
2. Lies `test/user_store_test.dart`
3. Repariere die `fromJson`-Methode
4. Führe Tests aus mit: `dart run test`

## Tests

```bash
dart run test
```

Alle 9 Tests müssen PASSED:9/9 ergeben.

## Abschlussbericht

Bitte berichten:
1. Welche Änderungen du vorgenommen hast
2. Ob alle Tests grün sind: PASSED:x/y

## Stopp – auf Bestätigung warten
""",
        fixture_path="fixtures/workflow_agent/store_bugfix",
        test_command=["dart", "run", "test"],
        allowed_files={"lib/user_store.dart"},
        forbidden_patterns=["models.json", "api_key", "DEEPSEEK_API_KEY", "Bearer ", "Authorization"],
    ),
    WorkflowAgentTask(
        task_id="settings_dialog",
        title="SettingsDialog Widget Bugfix",
        weight=6,
        prompt=r"""# Auftrag: Flutter-Widget-Settings-Bugfix

(lokal)

Handy muss nicht angeschlossen sein.

Keine Serveraktion, kein Upload, kein Commit und kein Push.

## Projekt
fixtures\workflow_agent\settings_dialog

## Ziel

In der Datei `lib/settings_dialog.dart` hat das `SettingsDialog`-Widget drei Bugs:

1. **Theme-Dropdown**: `onChanged` ruft kein `setState` auf – Theme-Änderungen werden nicht angezeigt
2. **Notifications-Toggle**: `SwitchListTile.onChanged` ruft kein `setState` auf – Toggle-Änderungen werden nicht angezeigt
3. **Save-Button**: `onPressed` übergibt hartkodierte Werte `('System', false)` statt der tatsächlich ausgewählten `_selectedTheme` und `_notificationsEnabled`

Repariere alle drei Bugs, sodass der Dialog korrekt funktioniert.

## Agent führt aus

1. Lies `lib/settings_dialog.dart`
2. Lies `test/settings_dialog_test.dart`
3. Repariere die drei Bugs
4. Führe Tests aus mit: `flutter test`

## Tests

```bash
flutter test
```

Alle 8 Tests müssen PASSED:8/8 ergeben.

## Abschlussbericht

Bitte berichten:
1. Welche Änderungen du vorgenommen hast
2. Ob alle Tests grün sind: PASSED:x/y

## Stopp – auf Bestätigung warten
""",
        fixture_path="fixtures/workflow_agent/settings_dialog",
        test_command=["flutter", "test"],
        allowed_files={"lib/settings_dialog.dart"},
        forbidden_patterns=["models.json", "api_key", "DEEPSEEK_API_KEY", "Bearer ", "Authorization"],
    ),
]


CODING_TASKS_EXTRA = CODING_TASKS
DART_LOGIC_TASKS.extend([
    DartLogicTask(
        title="normalizeModelConfig",
        difficulty="schwer",
        weight=5,
        function_name="normalizeModelConfig",
        prompt="""Schreibe exakt Map<String, dynamic> normalizeModelConfig(Map<String, dynamic> input). Die Rückgabe muss eine Map<String, dynamic> sein: Die normalisierte Konfiguration liegt direkt in dieser Map, und Fehler/Warnungen liegen unter dem Key \"errors\" als List<String> in derselben Map. Keine Dart Records, keine Tupel, keine Wrapper-Klasse und keine separate errors-Rückgabe. Inhaltlich: defaults, URL-Normalisierung, clamps für temperature/topP/context, stop-Sequenzen säubern, provider erkennen.""",
        harness_source=r'''
void main(){var passed=0,total=0;void check(String n,bool Function() b){total++;try{if(b())passed++;}catch(_){}}
check('defaults',()=>normalizeModelConfig({})['model']!=''&&normalizeModelConfig({})['errors'] is List);
check('bad url error',()=>normalizeModelConfig({'endpoint':'::bad'})['errors'].isNotEmpty);
check('localhost schema',()=>normalizeModelConfig({'endpoint':'localhost:11434'})['endpoint'].toString().startsWith('http://'));
check('temp clamp',()=>normalizeModelConfig({'temperature':9})['temperature']==2.0);
check('topP clamp',()=>normalizeModelConfig({'topP':-1})['topP']==0.0);
check('context clamp',(){final r=normalizeModelConfig({'context':999999});return r['context']<=32768;});
check('stop dedup',()=>normalizeModelConfig({'stop':['x','x','y']})['stop'].length==2);
check('empty stops removed',()=>!normalizeModelConfig({'stop':['','  ','x']})['stop'].contains(''));
check('provider ollama',()=>normalizeModelConfig({'endpoint':'http://localhost:11434'})['provider']=='ollama');
check('model trim',()=>normalizeModelConfig({'model':'  llama  '})['model']=='llama');
check('unknown ignored ok',()=>normalizeModelConfig({'foo':'bar'})['errors'] is List);
check('no throw invalid',()=>normalizeModelConfig({'temperature':'bad','topP':'bad','context':'bad'})['errors'].isNotEmpty);
print('PASSED:$passed/$total');}
''',
        reference_solution=r'''
Map<String,dynamic> normalizeModelConfig(Map<String,dynamic> input){
 final errors=<String>[]; var endpoint=(input['endpoint']??input['url']??'http://localhost:11434').toString().trim();
 try{Uri.parse(endpoint);}catch(_){errors.add('Invalid endpoint URL');}
 if(!endpoint.startsWith('http://')&&!endpoint.startsWith('https://'))endpoint='http://$endpoint';
 var model=(input['model']??'llama3').toString().trim();
 num temp=input['temperature'] is num?input['temperature']:0.7;temp=temp.clamp(0.0,2.0);
 if(input['temperature'] is! num && input.containsKey('temperature') && input['temperature']!=null)errors.add('Invalid temperature');
 num topP=input['topP'] is num?input['topP']:0.95;topP=topP.clamp(0.0,1.0);
 if(input['topP'] is! num && input.containsKey('topP') && input['topP']!=null)errors.add('Invalid topP');
 int ctx=input['context'] is int?input['context']:input['context'] is double?input['context'].toInt():4096;ctx=ctx.clamp(512,32768);
 if(input['context'] is! num && input.containsKey('context') && input['context']!=null)errors.add('Invalid context');
 var provider='ollama';if(endpoint.contains('api.deepseek.com'))provider='deepseek';
 var stop=<String>{};for(var s in(input['stop']as List?)?.map((e)=>e.toString().trim())??[]){if(s.isNotEmpty)stop.add(s);}
 if(stop.isEmpty)stop.addAll(['<|end|>']);
 return{'endpoint':endpoint,'model':model,'temperature':temp.toDouble(),'topP':topP.toDouble(),'context':ctx,'provider':provider,'stop':stop.toList(),'errors':errors};
}
''',
        broken_solution="Map<String,dynamic> normalizeModelConfig(Map<String,dynamic> input)=>input;",
    ),
])

# ----- Benchmark-Hilfsfunktionen -----

def extract_code_block(model_output: str) -> str:
    fence = re.search(r"```(?:\w+)?\s*\n?(.*?)```", model_output, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return model_output.strip()


def run_coding_task(client: OpenAICompatClient, task: CodingTask, stop_event: threading.Event) -> dict[str, Any]:
    if stop_event.is_set():
        return {"title": task.title, "passed": False, "weight": task.weight, "fraction": 0.0}
    prompt = task.prompt.strip()
    messages = [{"role": "user", "content": prompt}]
    try:
        output = client.chat(messages)
    except MissingApiKeyError:
        raise
    except ModelConnectionError:
        raise
    except Exception:
        raise ModelConnectionError(f"Fehler beim Modell-Call für {task.title}")

    code = extract_code_block(output)
    if not code.strip():
        return {"title": task.title, "passed": False, "weight": task.weight, "passed_checks": 0, "total_checks": 0, "fraction": 0.0}

    with tempfile.TemporaryDirectory() as tmp:
        solution_path = Path(tmp) / "solution.py"
        solution_path.write_text(code, encoding="utf-8")
        test_path = Path(tmp) / "test_runner.py"
        test_path.write_text(task.tests_source, encoding="utf-8")
        try:
            proc = _safe_subprocess_run(
                [sys.executable, str(test_path)],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS, cwd=tmp,
                env={**os.environ, "PYTHONPATH": str(tmp)},
            )
            stdout = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            return {"title": task.title, "passed": False, "weight": task.weight, "passed_checks": 0, "total_checks": 0, "fraction": 0.0, "error_status": "timeout", "output": "Timeout beim Test-Run"}
        except OSError as exc:
            return {"title": task.title, "passed": False, "weight": task.weight, "passed_checks": 0, "total_checks": 0, "fraction": None, "error_status": "benchmark_runtime_error", "output": f"OSError: {exc}"}
        match = re.search(r"PASSED:(\d+)/(\d+)", stdout)
        if match:
            passed_checks = int(match.group(1))
            total_checks = int(match.group(2))
            fraction = passed_checks / total_checks if total_checks > 0 else 0.0
            return {"title": task.title, "passed": passed_checks == total_checks, "weight": task.weight, "passed_checks": passed_checks, "total_checks": total_checks, "fraction": fraction, "output": output[:2000]}
        return {"title": task.title, "passed": False, "weight": task.weight, "passed_checks": 0, "total_checks": 0, "fraction": 0.0, "output": stdout[-2000:]}


def run_agent_task(client: OpenAICompatClient, task: AgentTask, stop_event: threading.Event, status_cb: Callable[[str], None] | None = None) -> dict[str, Any]:
    status_cb = status_cb or (lambda _: None)
    BAD_CALL_LIMIT = max(5, task.max_steps * 2)

    def _make_assistant_msg(content: str) -> dict[str, Any]:
        """Baut eine assistant-Nachricht mit reasoning_content für DeepSeek Thinking Mode."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        reasoning = getattr(client, "last_reasoning_content", "")
        if reasoning:
            msg["reasoning_content"] = reasoning
        return msg

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        tmp_real = Path(tmp).resolve()
        for file_name, content in task.files.items():
            file_path = tmp_path / file_name
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
        messages = [{"role": "system", "content": f"CWD={tmp}\nBeende mit run_code (Test-Kommando: {' '.join(str(p) for p in task.test_command)})."}]
        messages.append({"role": "user", "content": task.prompt.strip()})
        ok_calls = 0
        bad_calls = 0
        solved = False
        last_output = ""
        timeout_hit = False
        passed_checks = 0
        total_checks = 0
        fraction: float | None = None
        error_status: str | None = None
        timeout = AGENT_ADVANCED_TIMEOUT_SECONDS if task.difficulty == "advanced" else AGENT_BASIC_TIMEOUT_SECONDS

        def _build_result(override: dict[str, Any] | None = None) -> dict[str, Any]:
            total_calls = ok_calls + bad_calls
            tool_ok_percent = 100.0 * ok_calls / total_calls if total_calls > 0 else 0.0
            final_fraction = fraction if fraction is not None else 0.0
            if solved:
                final_fraction = 1.0
            base = {
                "title": task.title, "solved": solved, "weight": task.weight,
                "passed_checks": passed_checks, "total_checks": total_checks, "fraction": final_fraction,
                "steps": step + 1, "max_steps": task.max_steps,
                "ok_calls": ok_calls, "bad_calls": bad_calls, "tool_ok_percent": tool_ok_percent,
                "timeout": timeout_hit, "max_steps_reached": step + 1 >= task.max_steps and not solved,
                "last_output": last_output,
            }
            if error_status:
                base["error_status"] = error_status
            if override:
                base.update(override)
            return base

        for step in range(task.max_steps):
            if stop_event.is_set():
                break

            # Bad-Call-Limit prüfen
            if bad_calls >= BAD_CALL_LIMIT:
                error_status = "too_many_bad_calls"
                last_output = f"Agent abgebrochen: {bad_calls} ungültige Tool-Calls (Limit={BAD_CALL_LIMIT})"
                break

            try:
                output = client.chat(messages, max_tokens=AGENT_RESPONSE_MAX_TOKENS)
            except (ModelConnectionError, MissingApiKeyError):
                raise
            except Exception:
                bad_calls += 1
                last_output = traceback.format_exc()[-2000:]
                continue
            tool = parse_agent_tool(output)
            if tool is None:
                bad_calls += 1
                last_output = output[-2000:]
                continue
            tool_name = tool.get("tool", "")
            if tool_name == "read_file":
                path = str(tool.get("path", "")).strip()
                if not path:
                    bad_calls += 1
                    last_output = f"read_file ohne Pfad: {output[-500:]}"
                    continue
                full = tmp_path / path
                try:
                    full_resolved = full.resolve()
                    if not str(full_resolved).startswith(str(tmp_real) + os.sep) and full_resolved != tmp_real:
                        bad_calls += 1
                        last_output = f"read_file Pfad ausserhalb Temp: {path}"
                        continue
                    content = full.read_text(encoding="utf-8")
                    messages.append(_make_assistant_msg(output))
                    messages.append({"role": "user", "content": f"Inhalt von {path}:\n{content}"})
                    ok_calls += 1
                except Exception:
                    messages.append(_make_assistant_msg(output))
                    messages.append({"role": "user", "content": f"Fehler beim Lesen: {path}"})
                    bad_calls += 1
            elif tool_name == "write_file":
                path = str(tool.get("path", "")).strip()
                content = tool.get("content", "")
                if not path:
                    bad_calls += 1
                    last_output = f"write_file ohne Pfad: {output[-500:]}"
                    continue
                full = tmp_path / path
                try:
                    full_resolved = full.resolve()
                    if full_resolved.is_dir():
                        bad_calls += 1
                        last_output = f"write_file Pfad ist ein Verzeichnis: {path}"
                        continue
                    if not str(full_resolved).startswith(str(tmp_real) + os.sep) and full_resolved != tmp_real:
                        bad_calls += 1
                        last_output = f"write_file Pfad ausserhalb Temp: {path}"
                        continue
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_text(content, encoding="utf-8")
                    messages.append(_make_assistant_msg(output))
                    messages.append({"role": "user", "content": f"{path} geschrieben."})
                    ok_calls += 1
                except PermissionError:
                    bad_calls += 1
                    last_output = f"PermissionError beim Schreiben: {path}"
                except Exception:
                    bad_calls += 1
                    last_output = f"Fehler beim Schreiben {path}: {traceback.format_exc()[-500:]}"
            elif tool_name == "run_code":
                command = str(tool.get("command", "")).strip()
                if not command:
                    bad_calls += 1
                    last_output = f"run_code ohne command: {output[-500:]}"
                    continue
                resolved_args = _resolve_agent_command(command)
                if not resolved_args:
                    bad_calls += 1
                    last_output = f"run_code mit leerem/unauflösbarem command: {command!r}"
                    continue
                try:
                    proc = _safe_subprocess_run(
                        resolved_args,
                        capture_output=True, text=True, timeout=timeout, cwd=tmp,
                        env={**os.environ, "PYTHONPATH": str(tmp)},
                    )
                    stdout = proc.stdout + proc.stderr
                except subprocess.TimeoutExpired:
                    timeout_hit = True
                    stdout = "Timeout"
                except (ValueError, OSError) as exc:
                    bad_calls += 1
                    last_output = f"subprocess-Fehler: {exc}"
                    continue
                ok_calls += 1
                match = re.search(r"PASSED:(\d+)/(\d+)", stdout)
                if match:
                    passed_checks = int(match.group(1))
                    total_checks = int(match.group(2))
                    fraction = passed_checks / total_checks if total_checks > 0 else 0.0
                    solved = passed_checks == total_checks
                    last_output = stdout[-2000:]
                    break
                elif "assert" in stdout.lower() or "FAILED" in stdout:
                    last_output = stdout[-2000:]
                    break
                messages.append(_make_assistant_msg(output))
                messages.append({"role": "user", "content": f"Ausgabe:\n{stdout[-2000:]}"})
            else:
                bad_calls += 1
            last_output = output[-2000:]

        # Kein einziger gültiger Call
        if ok_calls == 0 and bad_calls > 0 and error_status is None:
            error_status = "no_valid_tool_call"

        return _build_result()


def parse_agent_tool(output: str) -> dict[str, Any] | None:
    fence = re.search(r"```(?:json)?\s*(.*?)```", output, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except Exception:
            pass
    brace = re.search(r"\{[^{}]*\"tool\"\s*:\s*\"[^\"]+\"[^{}]*\"args\"\s*:\s*\{[^{}]*\}[^{}]*\}", output, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except Exception:
            pass
    return None


def _resolve_agent_command(command: str) -> list[str]:
    stripped = command.strip()
    if not stripped:
        return []
    fallback = [sys.executable, "-c", stripped]
    if " " not in stripped:
        import shlex
        try:
            result = shlex.split(stripped)
            return result if result else fallback
        except Exception:
            return fallback
    allowed = {
        "python test_stats_util.py": [sys.executable, "test_stats_util.py"],
        "python test_parser_util.py": [sys.executable, "test_parser_util.py"],
        "python test/config_test.py": [sys.executable, "test/config_test.py"],
        "python test/chat_controller_test.py": [sys.executable, "test/chat_controller_test.py"],
        "python -m pytest": [sys.executable, "-m", "pytest"],
    }
    resolved = allowed.get(stripped, fallback)
    return resolved if resolved and resolved[0] else fallback


def _resolve_dart_exe() -> str:
    """Finde den ausführbaren Dart-Pfad. Unter Windows braucht man u.U. die .bat-Extension."""
    dart = shutil.which("dart")
    if dart is None:
        return "dart"
    if sys.platform == "win32":
        base, ext = os.path.splitext(dart)
        if ext.lower() in (".exe", ".bat", ".cmd"):
            return dart
        for candidate_ext in (".bat", ".exe", ".cmd"):
            candidate = dart + candidate_ext
            if os.path.isfile(candidate):
                return candidate
    return dart


def _resolve_flutter_exe() -> str:
    """Finde den ausführbaren Flutter-Pfad. Unter Windows braucht man flutter.bat."""
    flutter = shutil.which("flutter")
    if flutter is None:
        return "flutter"
    if sys.platform == "win32":
        base, ext = os.path.splitext(flutter)
        if ext.lower() in (".exe", ".bat", ".cmd"):
            return flutter
        for candidate_ext in (".bat", ".exe", ".cmd"):
            candidate = flutter + candidate_ext
            if os.path.isfile(candidate):
                return candidate
    return flutter


# ============================================================
#  Workflow-Agent Evaluierung
# ============================================================

WORKFLOW_AGENT_RUNS_DIR = "runs/workflow_agent"


def _validate_git_repo(base_dir: Path) -> tuple[bool, str, Path | None]:
    """Prüft, ob base_dir in einem gültigen Git-Repository liegt.

    Returns:
        (is_valid, error_message, git_toplevel)
    """
    git_toplevel: Path | None = None
    # Prüfe .git-Ordner oder git rev-parse --show-toplevel
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, cwd=str(base_dir),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            git_toplevel = Path(proc.stdout.strip()).resolve()
        else:
            return (False, "Dieser Ordner ist kein gültiges Git-Repository.\nBitte zuerst einen Workflow-Agent-Arbeitsordner erzeugen oder den richtigen Ordner auswählen.", None)
    except FileNotFoundError:
        return (False, "Git ist nicht installiert oder nicht im PATH.\nGit wird für die Workflow-Agent-Auswertung benötigt.", None)
    except Exception as exc:
        return (False, f"Fehler beim Prüfen des Git-Repositories:\n{exc}", None)

    # Prüfe, ob base_dir innerhalb des git_toplevel liegt
    try:
        base_resolved = base_dir.resolve()
        if not str(base_resolved).startswith(str(git_toplevel)):
            return (False, "Der Arbeitsordner liegt nicht innerhalb des Git-Repositories.", None)
    except Exception:
        return (False, "Konnte den Arbeitsordner nicht auflösen.", None)

    return (True, "", git_toplevel)


def _check_original_fixtures_modified(fixture_path: Path) -> tuple[bool, list[str]]:
    """Prüft, ob die Original-Fixtures modifiziert wurden.

    Returns:
        (modified, changed_files_list)
    """
    changed: list[str] = []
    if not fixture_path.exists():
        return (False, changed)
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15, cwd=str(fixture_path),
        )
        for line in proc.stdout.splitlines():
            if line.strip():
                parts = line.strip().split(maxsplit=1)
                if len(parts) >= 2:
                    changed.append(parts[1].strip())
    except Exception:
        pass
    return (len(changed) > 0, changed)


def _create_run_workdir(task: WorkflowAgentTask, base_dir: Path) -> Path:
    """Erzeugt einen Run-Arbeitsordner unter runs/workflow_agent/<timestamp>_<task_id>/
    und kopiert das Fixture dorthin.

    Returns:
        Pfad zum erstellten Run-Arbeitsordner.
    """
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_dir / WORKFLOW_AGENT_RUNS_DIR / f"{timestamp}_{task.task_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    fixture_src = base_dir / task.fixture_path
    if fixture_src.exists():
        # Kopiere alle Dateien aus dem Fixture-Ordner rekursiv
        _copy_fixture_tree(fixture_src, run_dir)

    return run_dir


def _copy_fixture_tree(src: Path, dst: Path) -> None:
    """Kopiert einen Verzeichnisbaum rekursiv, überspringt .git/.dart_tool."""
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in (".git", ".dart_tool", ".packages", "build"):
            continue
        target = dst / item.name
        if item.is_dir():
            _copy_fixture_tree(item, target)
        else:
            shutil.copy2(item, target)


def run_workflow_agent_evaluation(
    task: WorkflowAgentTask,
    base_dir: Path,
    run_workdir: Path | None = None,
) -> dict[str, Any]:
    """Wertet einen Workflow-Agent-Lauf aus.

    Prüft:
    - Git-Repository gültig
    - Arbeitsordner existiert
    - pubspec.yaml existiert
    - Testdatei existiert
    - Git verfügbar
    - dart/flutter im PATH
    - Tests grün (PASSED:x/y im Output)
    - git diff --check (keine Merge-Konflikte)
    - Geänderte Dateien innerhalb des erlaubten Scopes
    - Keine verbotenen Dateien geändert
    - Original-Fixtures nicht modifiziert
    - Keine Dateien außerhalb des Arbeitsordners geändert
    """
    base_result = {
        "task_id": task.task_id,
        "title": task.title,
        "weight": task.weight,
        "workdir": str(run_workdir) if run_workdir else None,
        "fixture_source": str(base_dir / task.fixture_path),
        "tests_exit_code": None,
        "diff_check_exit_code": None,
        "git_valid": True,
        "original_fixture_modified": False,
        "error_status": None,
        "fraction": None,
        "workflow_agent_score": None,
        "scope_violations": [],
        "forbidden_actions": [],
        "changed_files": [],
        "test_result": None,
    }

    # --- 0. Git-Repository prüfen ---
    git_valid, git_error, git_toplevel = _validate_git_repo(base_dir)
    if not git_valid:
        base_result["git_valid"] = False
        base_result["error_status"] = "git_invalid"
        base_result["test_result"] = {"passed_checks": 0, "total_checks": 0, "fraction": None, "output": git_error}
        return base_result

    # --- Kein Run-Arbeitsordner → sofort abbrechen, kein Fixture-Fallback ---
    if run_workdir is None:
        base_result["git_valid"] = git_valid
        base_result["error_status"] = "workdir_missing"
        base_result["test_result"] = {
            "passed_checks": 0, "total_checks": 0, "fraction": None,
            "output": "Kein Run-Arbeitsordner unter runs/workflow_agent/ gefunden.\n"
                      "Bitte zuerst einen Arbeitsordner über »Auftrag anzeigen« erzeugen "
                      "und den Agent-Lauf durchführen.\n"
                      "Die Original-Fixtures unter fixtures/workflow_agent/ sind nur Templates "
                      "und werden nicht ausgewertet.",
        }
        return base_result

    # --- 1. Prüfen, ob Original-Fixtures modifiziert wurden ---
    fixture_path = base_dir / task.fixture_path
    if fixture_path.exists():
        orig_modified, orig_changed = _check_original_fixtures_modified(fixture_path)
        if orig_modified:
            base_result["original_fixture_modified"] = True
            base_result["forbidden_actions"].append(
                f"Original-Fixture wurde verändert: {', '.join(orig_changed[:10])}"
            )

    # --- Bestimme den Auswertungsordner ---
    workdir = run_workdir

    # --- 2. Prüfe, ob der Arbeitsordner existiert ---
    if not workdir.exists():
        base_result["error_status"] = "workdir_missing"
        base_result["test_result"] = {"passed_checks": 0, "total_checks": 0, "fraction": None, "output": f"Arbeitsordner fehlt: {workdir}"}
        return base_result

    # --- 3. Prüfe, ob pubspec.yaml existiert ---
    pubspec = workdir / "pubspec.yaml"
    if not pubspec.is_file():
        base_result["error_status"] = "pubspec_missing"
        base_result["test_result"] = {"passed_checks": 0, "total_checks": 0, "fraction": None, "output": f"pubspec.yaml fehlt in: {workdir}"}
        return base_result

    # --- 4. Prüfe, ob Testdatei existiert ---
    test_dir = workdir / "test"
    if not test_dir.is_dir() or not any(test_dir.iterdir()):
        base_result["error_status"] = "test_file_missing"
        base_result["test_result"] = {"passed_checks": 0, "total_checks": 0, "fraction": None, "output": f"Keine Testdateien gefunden in: {test_dir}"}
        return base_result

    scope_violations: list[str] = []
    forbidden_actions: list[str] = list(base_result["forbidden_actions"])
    changed_files: list[str] = []
    diff_check_exit_code: int | None = None
    git_available = True

    # --- 5. git diff --check & geänderte Dateien ermitteln ---
    try:
        diff_proc = subprocess.run(
            ["git", "diff", "--check"],
            capture_output=True, text=True, timeout=15, cwd=str(workdir),
        )
        diff_check_exit_code = diff_proc.returncode
    except FileNotFoundError:
        git_available = False
    except Exception:
        diff_check_exit_code = -1

    # 6. Geänderte Dateien via git diff --name-only
    if git_available:
        try:
            name_proc = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, timeout=15, cwd=str(workdir),
            )
            changed = [line.strip() for line in name_proc.stdout.splitlines() if line.strip()]
            changed_files = changed
        except Exception:
            changed = []

        # Auch unstaged/untracked via git status
        try:
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=15, cwd=str(workdir),
            )
            for line in status_proc.stdout.splitlines():
                if line.strip():
                    parts = line.strip().split(maxsplit=1)
                    if len(parts) >= 2:
                        fname = parts[1].strip()
                        if fname not in changed_files:
                            changed_files.append(fname)
        except Exception:
            pass

    # --- 7. Prüfe: Wurden überhaupt Dateien geändert? ---
    if not changed_files and run_workdir:
        # Keine Änderungen im Run-Ordner – Agent hat nichts getan
        base_result["error_status"] = "no_files_changed"
        base_result["test_result"] = {"passed_checks": 0, "total_checks": 0, "fraction": 0.0, "output": "Keine Dateien wurden geändert. Der Agent hat keine Änderungen vorgenommen."}
        return base_result

    # --- 8. Scope-Prüfung: Geänderte Dateien außerhalb erlaubter Files? ---
    for f in changed_files:
        normalized = f.replace("\\", "/")
        is_allowed = False
        for allowed in task.allowed_files:
            if normalized == allowed or normalized.endswith("/" + allowed):
                is_allowed = True
                break
        if "test/" in normalized or normalized.endswith("pubspec.lock") or ".dart_tool/" in normalized:
            is_allowed = True
        if not is_allowed:
            scope_violations.append(f)

    # --- 9. Prüfe auf verbotene Patterns in geänderten Dateien ---
    for f in changed_files:
        file_path = workdir / f
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace").lower()
            for pattern in task.forbidden_patterns:
                if pattern.lower() in content:
                    forbidden_actions.append(f"{f}: enthaelt verbotenes Pattern '{pattern}'")
        except Exception:
            pass

    # --- 10. Test-Auswertung ---
    test_result: dict[str, Any] | None = None
    fraction: float | None = None
    error_status: str | None = base_result["error_status"]
    tests_exit_code: int | None = None

    test_cmd = task.test_command
    # Prüfe, ob dart/flutter verfügbar ist
    exe_name = test_cmd[0] if test_cmd else ""
    if exe_name and shutil.which(exe_name) is None:
        error_status = error_status or f"{exe_name}_not_found"
        test_result = {"passed_checks": 0, "total_checks": 0, "fraction": None, "output": f"{exe_name} nicht im PATH gefunden. Bitte sicherstellen, dass {exe_name} installiert und im PATH ist."}
    else:
        try:
            proc = subprocess.run(
                test_cmd,
                capture_output=True, text=True, timeout=120, cwd=str(workdir),
                shell=(sys.platform == "win32"),
            )
            tests_exit_code = proc.returncode
            stdout = proc.stdout + proc.stderr
            match = re.search(r"PASSED:(\d+)/(\d+)", stdout)
            if match:
                passed_checks = int(match.group(1))
                total_checks = int(match.group(2))
                fraction = passed_checks / total_checks if total_checks > 0 else 0.0
            else:
                passed_checks = 0
                total_checks = 0
                fraction = 0.0
                if proc.returncode != 0:
                    error_status = error_status or "tests_failed"
            test_result = {
                "passed_checks": passed_checks,
                "total_checks": total_checks,
                "fraction": fraction,
                "output": stdout[-2000:],
                "exit_code": tests_exit_code,
            }
        except subprocess.TimeoutExpired:
            error_status = error_status or "timeout"
            test_result = {"passed_checks": 0, "total_checks": 0, "fraction": 0.0, "output": "Timeout", "exit_code": None}
            fraction = 0.0
        except FileNotFoundError:
            error_status = error_status or "test_command_not_found"
            test_result = {"passed_checks": 0, "total_checks": 0, "fraction": None, "output": f"Command not found: {' '.join(test_cmd)}", "exit_code": None}
        except Exception as exc:
            error_status = error_status or "benchmark_runtime_error"
            test_result = {"passed_checks": 0, "total_checks": 0, "fraction": None, "output": str(exc)[:2000], "exit_code": None}

    # --- 11. Scope-Violations und Forbidden-Actions reduzieren den Score ---
    workflow_score = fraction
    if fraction is not None and fraction > 0:
        if scope_violations:
            penalty = min(1.0, len(scope_violations) * 0.10)
            workflow_score = max(0.0, fraction - penalty)
        if forbidden_actions:
            penalty = min(1.0, len(forbidden_actions) * 0.15)
            workflow_score = max(0.0, (workflow_score if workflow_score is not None else fraction) - penalty)

    return {
        "task_id": task.task_id,
        "title": task.title,
        "weight": task.weight,
        "workdir": str(workdir),
        "fixture_source": str(fixture_path),
        "tests_exit_code": tests_exit_code,
        "diff_check_exit_code": diff_check_exit_code,
        "git_valid": git_valid,
        "original_fixture_modified": base_result["original_fixture_modified"],
        "error_status": error_status,
        "fraction": fraction,
        "workflow_agent_score": workflow_score,
        "scope_violations": scope_violations,
        "forbidden_actions": forbidden_actions,
        "changed_files": changed_files,
        "test_result": test_result,
    }


_WIN32_MAX_COMMAND_LINE = 32767


def _safe_subprocess_run(
    args: list[str],
    capture_output: bool = True,
    text: bool = True,
    timeout: int | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    shell: bool = False,
) -> subprocess.CompletedProcess:
    """subprocess.run-Wrapper mit WinError-87-Diagnose und Kommando-Validierung."""
    if not args:
        raise ValueError("subprocess args dürfen nicht leer sein")
    _log_subprocess_call(args, cwd=cwd, shell=shell)
    _validate_subprocess_args(args, shell=shell)

    try:
        return subprocess.run(
            args,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            cwd=cwd,
            env=env,
            shell=shell,
        )
    except OSError as exc:
        if sys.platform == "win32" and getattr(exc, "winerror", None) == 87:
            _log_winerror_87(args, cwd=cwd, shell=shell)
        raise


def _log_subprocess_call(args: list[str], cwd: str | None = None, shell: bool = False) -> None:
    """Schreibe Diagnose-Informationen über den subprocess-Aufruf nach stderr."""
    cmd_str = " ".join(str(p) for p in args)
    total_len = len(cmd_str)
    print(
        f"[subprocess] cmd={cmd_str[:500]}{'...' if len(cmd_str) > 500 else ''} "
        f"cwd={cwd} args_count={len(args)} total_len={total_len} "
        f"shell={shell}",
        file=sys.stderr,
        flush=True,
    )


def _validate_subprocess_args(args: list[str], shell: bool = False) -> None:
    """Prüfe das Kommando auf offensichtliche Probleme vor subprocess.run."""
    if not args:
        raise ValueError("subprocess args dürfen nicht leer sein")
    exe = str(args[0])
    if sys.platform == "win32" and not shell:
        cmd_line = subprocess.list2cmdline([str(p) for p in args])
        if len(cmd_line) > _WIN32_MAX_COMMAND_LINE:
            raise ValueError(
                f"Kommandozeile zu lang für Windows ({len(cmd_line)} > {_WIN32_MAX_COMMAND_LINE}): "
                f"{cmd_line[:200]}..."
            )
    if not os.path.isabs(exe) and not shutil.which(exe) and not shell:
        print(
            f"[subprocess] WARNUNG: '{exe}' nicht im PATH gefunden – "
            f"subprocess.run könnte mit FileNotFoundError scheitern",
            file=sys.stderr,
            flush=True,
        )


def _log_winerror_87(args: list[str], cwd: str | None = None, shell: bool = False) -> None:
    """Detaillierte Diagnose bei Windows-Fehlercode 87 (ERROR_INVALID_PARAMETER)."""
    cmd_str = " ".join(str(p) for p in args)
    total_len = len(cmd_str)
    cmd_line_len = len(subprocess.list2cmdline([str(p) for p in args]))
    print(
        f"[subprocess] WinError 87 – mögliche Ursachen:\n"
        f"  - Kommandozeile zu lang (list2cmdline: {cmd_line_len} Zeichen, "
        f"raw join: {total_len} Zeichen, Limit: {_WIN32_MAX_COMMAND_LINE})\n"
        f"  - Ungültiges Argument oder Pfad mit Sonderzeichen\n"
        f"  - Ausführbare Datei nicht gefunden oder beschädigt\n"
        f"  - args={args}\n"
        f"  - cwd={cwd}\n"
        f"  - shell={shell}",
        file=sys.stderr,
        flush=True,
    )


def run_dart_logic_code(code: str, task: DartLogicTask) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        solution_file = tmp_path / "solution.dart"
        solution_file.write_text(code, encoding="utf-8")
        harness_file = tmp_path / "harness.dart"
        main_file = tmp_path / "main.dart"
        # harness.dart muss solution.dart importieren, um die Funktionen zu sehen
        final_harness = "import 'solution.dart';\n" + task.harness_source.replace("void main()", "void harness_main()")
        harness_file.write_text(final_harness, encoding="utf-8")
        main_code = f"import 'solution.dart';\nimport 'harness.dart';\nvoid main() {{ harness_main(); }}"
        main_file.write_text(main_code, encoding="utf-8")
        try:
            proc = _safe_subprocess_run(
                [_resolve_dart_exe(), str(main_file)],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS, cwd=str(tmp),
            )
            stdout = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            return {"title": task.title, "passed": False, "weight": task.weight, "fraction": 0.0, "error_status": "timeout"}
        except FileNotFoundError:
            return {"title": task.title, "passed": False, "weight": task.weight, "fraction": None, "error_status": "dart_not_found"}
        except OSError as exc:
            return {"title": task.title, "passed": False, "weight": task.weight, "fraction": None, "error_status": "benchmark_runtime_error", "output": f"OSError: {exc}"}
        match = re.search(r"PASSED:(\d+)/(\d+)", stdout)
        if match:
            passed_checks = int(match.group(1))
            total_checks = int(match.group(2))
            fraction = passed_checks / total_checks if total_checks > 0 else 0.0
            return {"title": task.title, "passed": passed_checks == total_checks, "weight": task.weight, "passed_checks": passed_checks, "total_checks": total_checks, "fraction": fraction, "output": stdout[-2000:]}
        return {"title": task.title, "passed": False, "weight": task.weight, "passed_checks": 0, "total_checks": 0, "fraction": 0.0, "output": stdout[-2000:]}


def run_flutter_ui_task_single(client: OpenAICompatClient, task: FlutterUITask) -> dict[str, Any]:
    output = client.chat([{"role": "user", "content": task.prompt.strip()}])
    code = extract_code_block(output)
    if not code.strip():
        return {"title": task.title, "passed": False, "weight": task.weight, "fraction": 0.0}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir(exist_ok=True)
        (lib_dir / "solution.dart").write_text(code, encoding="utf-8")
        test_dir = tmp_path / "test"
        test_dir.mkdir(exist_ok=True)
        (test_dir / task.test_filename).write_text(task.test_source, encoding="utf-8")
        pubspec = tmp_path / "pubspec.yaml"
        pubspec.write_text(f"name: llm_flutter_ui\npublish_to: none\nenvironment:\n  sdk: '>=3.0.0 <4.0.0'\ndependencies:\n  flutter:\n    sdk: flutter\ndev_dependencies:\n  flutter_test:\n    sdk: flutter\n")
        if task.harness_source:
            (lib_dir / "chat_harness.dart").write_text(task.harness_source, encoding="utf-8")
        try:
            _safe_subprocess_run([_resolve_flutter_exe(), "pub", "get"], cwd=str(tmp), capture_output=True, text=True, timeout=60)
            proc = _safe_subprocess_run(
                [_resolve_flutter_exe(), "test", str(test_dir / task.test_filename), "--no-pub"],
                capture_output=True, text=True, timeout=120, cwd=str(tmp),
            )
            stdout = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            return {"title": task.title, "passed": False, "weight": task.weight, "fraction": 0.0, "error_status": "timeout"}
        except FileNotFoundError:
            return {"title": task.title, "passed": False, "weight": task.weight, "fraction": None, "error_status": "flutter_not_found"}
        except OSError as exc:
            return {"title": task.title, "passed": False, "weight": task.weight, "fraction": None, "error_status": "benchmark_runtime_error", "output": f"OSError: {exc}"}
        match = re.search(r"PASSED:(\d+)/(\d+)", stdout)
        if match:
            passed_checks = int(match.group(1))
            total_checks = int(match.group(2))
            fraction = passed_checks / total_checks if total_checks > 0 else 0.0
            return {"title": task.title, "passed": passed_checks == total_checks, "weight": task.weight, "passed_checks": passed_checks, "total_checks": total_checks, "fraction": fraction, "output": stdout[-2000:]}
        return {"title": task.title, "passed": False, "weight": task.weight, "passed_checks": 0, "total_checks": 0, "fraction": 0.0, "output": stdout[-2000:]}


class BenchmarkRunner:
    def __init__(self, status_cb: Callable[[str], None], progress_cb: Callable[[int, int], None], stop_event: threading.Event | None = None) -> None:
        self.status_cb = status_cb
        self.progress_cb = progress_cb
        self.stop_event = stop_event or threading.Event()

    def run(self, models: list[ModelConfig], run_coding: bool, run_agent: bool, run_dart_logic: bool = False, run_flutter_ui: bool = False, run_count: int = 1) -> list[BenchmarkResult]:
        self.status_cb(f"Benchmark gestartet – request_max_tokens={DEFAULT_MODEL_MAX_TOKENS}, agent_response_max_tokens={AGENT_RESPONSE_MAX_TOKENS}")
        run_count = max(1, min(5, int(run_count)))
        per_model_run_counts = {model.name: run_count for model in models}
        units_per_run = (len(CODING_TASKS) if run_coding else 0) + (len(AGENT_TASKS) if run_agent else 0) + (len(DART_LOGIC_TASKS) if run_dart_logic else 0) + (len(FLUTTER_UI_TASKS) if run_flutter_ui else 0)
        total_units = max(1, sum(per_model_run_counts.values()) * units_per_run)
        done_units = 0
        results: list[BenchmarkResult] = []
        for model in models:
            if self.stop_event.is_set():
                self.status_cb("Benchmark gestoppt")
                break
            model_run_count = per_model_run_counts[model.name]
            self.status_cb(f"Starte Modell {model.name}")
            runs: list[BenchmarkResult] = []
            for run_index in range(1, model_run_count + 1):
                if self.stop_event.is_set():
                    self.status_cb("Benchmark gestoppt")
                    break
                self.status_cb(f"Starte Modell {model.name}, Lauf {run_index}/{model_run_count}")
                result = self._run_single_model(model, run_index, model_run_count, run_coding, run_agent, run_dart_logic, run_flutter_ui, done_units, total_units)
                done_units = int(result.details.get("done_units", done_units))
                result.details.pop("done_units", None)
                runs.append(result)
            if runs:
                results.append(aggregate_model_runs(model.name, runs))

        return sorted(results, key=lambda item: item.total_score, reverse=True)

    def _score_split(self, details: list[dict[str, Any]], tasks: list[Any], difficulty: str | None = None) -> float | None:
        weighted_fraction = 0.0
        scored_weight = 0
        for task in tasks:
            if difficulty is not None and getattr(task, "difficulty", None) != difficulty:
                continue
            detail = next((item for item in details if item.get("title") == task.title), None)
            if detail is None or detail.get("fraction") is None:
                continue
            error_status = detail.get("error_status", "")
            if error_status in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"}:
                continue
            weighted_fraction += task.weight * float(detail.get("fraction", 0.0))
            scored_weight += task.weight
        return 100.0 * weighted_fraction / scored_weight if scored_weight else None

    def _run_single_model(self, model: ModelConfig, run_index: int, run_count: int, run_coding: bool, run_agent: bool, run_dart_logic: bool, run_flutter_ui: bool, done_units: int, total_units: int) -> BenchmarkResult:
        client = OpenAICompatClient(model.endpoint_url, model.model_id, self.status_cb, model.provider, model.api_key, model.reasoning_effort)
        if model.is_deepseek and not client.api_key:
            self.status_cb(MISSING_DEEPSEEK_API_KEY_MESSAGE)
        result = BenchmarkResult(model=model.name)
        coding_details = []
        agent_details = []
        dart_logic_details = []
        flutter_ui_details = []

        if run_coding:
            weighted_fraction = 0.0
            total_weight = sum(task.weight for task in CODING_TASKS)
            for task in CODING_TASKS:
                if self.stop_event.is_set():
                    self.status_cb("Benchmark gestoppt")
                    break
                self.status_cb(f"{model.name}: Lauf {run_index}/{run_count} Coding – {task.title}")
                try:
                    detail = run_coding_task(client, task, self.stop_event)
                except MissingApiKeyError:
                    detail = {"title": task.title, "passed": None, "weight": task.weight, "passed_checks": None, "total_checks": None, "fraction": None, "error_status": "missing_api_key", "output": MISSING_DEEPSEEK_API_KEY_MESSAGE}
                except ModelConnectionError:
                    detail = {"title": task.title, "passed": None, "weight": task.weight, "passed_checks": None, "total_checks": None, "fraction": None, "error_status": "connection_error_timeout", "output": sanitize_sensitive_text(traceback.format_exc())[-2000:]}
                except Exception:  # noqa: BLE001
                    if self.stop_event.is_set():
                        break
                    detail = {"title": task.title, "passed": False, "weight": task.weight, "passed_checks": 0, "total_checks": 0, "fraction": None, "error_status": "grading_error", "output": sanitize_sensitive_text(traceback.format_exc())[-2000:]}
                coding_details.append(detail)
                if detail.get("error_status") not in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"}:
                    weighted_fraction += task.weight * float(detail.get("fraction", 0.0))
                done_units += 1
                self.progress_cb(done_units, total_units)
            scored_weight = total_weight - sum(int(detail.get("weight", 0)) for detail in coding_details if detail.get("error_status") in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"})
            result.coding_percent = 100.0 * weighted_fraction / scored_weight if scored_weight else None

        if run_agent:
            weighted_fraction = 0.0
            total_weight = sum(task.weight for task in AGENT_TASKS)
            tool_ok_values = []
            step_values = []
            for task in AGENT_TASKS:
                if self.stop_event.is_set():
                    self.status_cb("Benchmark gestoppt")
                    break
                try:
                    detail = run_agent_task(client, task, self.stop_event, self.status_cb)
                except MissingApiKeyError:
                    detail = {"title": task.title, "solved": None, "weight": task.weight, "passed_checks": None, "total_checks": None, "fraction": None, "steps": None, "ok_calls": 0, "bad_calls": 0, "tool_ok_percent": None, "timeout": False, "max_steps_reached": False, "error_status": "missing_api_key", "last_output": MISSING_DEEPSEEK_API_KEY_MESSAGE}
                except ModelConnectionError:
                    detail = {"title": task.title, "solved": None, "weight": task.weight, "passed_checks": None, "total_checks": None, "fraction": None, "steps": None, "ok_calls": 0, "bad_calls": 0, "tool_ok_percent": None, "timeout": False, "max_steps_reached": False, "error_status": "connection_error_timeout", "last_output": sanitize_sensitive_text(traceback.format_exc())[-2000:]}
                except Exception:  # noqa: BLE001
                    if self.stop_event.is_set():
                        break
                    detail = {"title": task.title, "solved": False, "weight": task.weight, "passed_checks": 0, "total_checks": 0, "fraction": None, "steps": task.max_steps, "ok_calls": 0, "bad_calls": 1, "tool_ok_percent": None, "timeout": False, "max_steps_reached": False, "error_status": "grading_error", "last_output": sanitize_sensitive_text(traceback.format_exc())[-2000:]}
                agent_details.append(detail)
                if detail.get("error_status") not in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"}:
                    weighted_fraction += task.weight * float(detail.get("fraction", 0.0))
                    tool_ok_values.append(float(detail.get("tool_ok_percent", 0.0)))
                    step_value = detail.get("steps")
                    if step_value is not None:
                        step_values.append(float(step_value))
                done_units += 1
                self.progress_cb(done_units, total_units)
            scored_weight = total_weight - sum(int(detail.get("weight", 0)) for detail in agent_details if detail.get("error_status") in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"})
            result.agent_percent = 100.0 * weighted_fraction / scored_weight if scored_weight else None
            result.agent_advanced_percent = self._score_split(agent_details, AGENT_TASKS, "advanced")
            result.tool_ok_percent = sum(tool_ok_values) / len(tool_ok_values) if tool_ok_values else 0.0
            result.avg_steps = sum(step_values) / len(step_values) if step_values else 0.0

        if run_dart_logic:
            weighted_fraction = 0.0
            total_weight = sum(task.weight for task in DART_LOGIC_TASKS)
            for task in DART_LOGIC_TASKS:
                if self.stop_event.is_set():
                    self.status_cb("Benchmark gestoppt")
                    break
                self.status_cb(f"{model.name}: Lauf {run_index}/{run_count} Dart-Logik – {task.title}")
                try:
                    output = client.chat([{"role": "user", "content": task.prompt.strip()}])
                    code = extract_code_block(output)
                    detail = run_dart_logic_code(code, task)
                except MissingApiKeyError:
                    detail = {"title": task.title, "passed": None, "weight": task.weight, "fraction": None, "error_status": "missing_api_key"}
                except ModelConnectionError:
                    detail = {"title": task.title, "passed": None, "weight": task.weight, "fraction": None, "error_status": "connection_error_timeout"}
                except Exception:
                    if self.stop_event.is_set():
                        break
                    detail = {"title": task.title, "passed": False, "weight": task.weight, "fraction": None, "error_status": "grading_error"}
                dart_logic_details.append(detail)
                if detail.get("error_status") not in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"}:
                    weighted_fraction += task.weight * float(detail.get("fraction", 0.0))
                done_units += 1
                self.progress_cb(done_units, total_units)
            scored_weight = total_weight - sum(int(detail.get("weight", 0)) for detail in dart_logic_details if detail.get("error_status") in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"})
            result.dart_logic_percent = 100.0 * weighted_fraction / scored_weight if scored_weight else None
            result.dart_logic_advanced_percent = self._score_split(dart_logic_details, DART_LOGIC_TASKS, "advanced")

        if run_flutter_ui:
            weighted_fraction = 0.0
            total_weight = sum(task.weight for task in FLUTTER_UI_TASKS)
            for task in FLUTTER_UI_TASKS:
                if self.stop_event.is_set():
                    self.status_cb("Benchmark gestoppt")
                    break
                self.status_cb(f"{model.name}: Lauf {run_index}/{run_count} Flutter-UI – {task.title}")
                try:
                    detail = run_flutter_ui_task_single(client, task)
                except MissingApiKeyError:
                    detail = {"title": task.title, "passed": None, "weight": task.weight, "fraction": None, "error_status": "missing_api_key"}
                except ModelConnectionError:
                    detail = {"title": task.title, "passed": None, "weight": task.weight, "fraction": None, "error_status": "connection_error_timeout"}
                except Exception:
                    if self.stop_event.is_set():
                        break
                    detail = {"title": task.title, "passed": False, "weight": task.weight, "fraction": None, "error_status": "grading_error"}
                flutter_ui_details.append(detail)
                if detail.get("error_status") not in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"}:
                    weighted_fraction += task.weight * float(detail.get("fraction", 0.0))
                done_units += 1
                self.progress_cb(done_units, total_units)
            scored_weight = total_weight - sum(int(detail.get("weight", 0)) for detail in flutter_ui_details if detail.get("error_status") in BENCHMARK_ERROR_STATUSES | {"connection_error_timeout", "missing_api_key"})
            result.flutter_ui_percent = 100.0 * weighted_fraction / scored_weight if scored_weight else None
            result.flutter_ui_advanced_percent = self._score_split(flutter_ui_details, FLUTTER_UI_TASKS, "advanced")

        result.tokens_per_second = client.tokens_per_second

        all_details = coding_details + agent_details + dart_logic_details + flutter_ui_details
        benchmark_errors = [d for d in all_details if d.get("error_status") in BENCHMARK_ERROR_STATUSES]
        if benchmark_errors:
            result.benchmark_valid = False
            error_types = sorted(set(d.get("error_status", "") for d in benchmark_errors))
            result.benchmark_error = f"environment_error: {', '.join(error_types)}"

        result.details = {
            "run_index": run_index,
            "request_max_tokens": DEFAULT_MODEL_MAX_TOKENS,
            "agent_response_max_tokens": AGENT_RESPONSE_MAX_TOKENS,
            "benchmark_valid": result.benchmark_valid,
            "benchmark_error": result.benchmark_error,
            "model_config": {
                "name": model.name,
                "provider": model.provider,
                "model_id": model.model_id,
                "quant": infer_quant(model.name, model.model_id),
                "context_size": infer_context_size(model.name, model.model_id),
                "reasoning_effort": model.reasoning_effort,
                "effective_reasoning_effort": client.effective_reasoning_effort if model.is_deepseek else "",
            },
            "coding": coding_details,
            "agent": agent_details,
            "dart_logic": dart_logic_details,
            "flutter_ui": flutter_ui_details,
            "done_units": done_units,
        }
        weighted_score_parts = [(result.coding_percent, 1), (result.dart_logic_percent, 2), (result.dart_logic_advanced_percent, 4), (result.flutter_ui_percent, 3), (result.flutter_ui_advanced_percent, 5), (result.agent_percent, 2), (result.agent_advanced_percent, 6)]
        scored = [(v, w) for v, w in weighted_score_parts if v is not None]
        result.total_score = sum(v * w for v, w in scored) / sum(w for _, w in scored) if scored else 0.0
        return result


def aggregate_model_runs(model_name: str, runs: list[BenchmarkResult]) -> BenchmarkResult:
    average = BenchmarkResult(model=model_name)
    average.details = {
        "runs": [sanitize_for_export(dataclasses.asdict(run)) for run in runs],
        "average": {},
        "column_stats": {},
        "task_breakdown": {},
    }
    section_specs = {
        "coding": CODING_TASKS,
        "agent": AGENT_TASKS,
        "dart_logic": DART_LOGIC_TASKS,
        "flutter_ui": FLUTTER_UI_TASKS,
    }
    for attr, section in (("coding_percent", "coding"), ("agent_percent", "agent"), ("dart_logic_percent", "dart_logic"), ("flutter_ui_percent", "flutter_ui")):
        summary = summarize_section_runs(runs, section, section_specs[section], attr)
        setattr(average, attr, summary["percent"])
        average.details["average"][attr] = summary["percent"]
        average.details["column_stats"][attr] = summary["run_percent_stats"]
        average.details["task_breakdown"][section] = summary["tasks"]

    for attr, section, difficulty in (("agent_advanced_percent", "agent", "advanced"), ("dart_logic_advanced_percent", "dart_logic", "advanced"), ("flutter_ui_advanced_percent", "flutter_ui", "advanced")):
        tasks = [task for task in section_specs[section] if getattr(task, "difficulty", None) == difficulty]
        summary = summarize_section_runs(runs, section, tasks, attr)
        setattr(average, attr, summary["percent"])
        average.details["average"][attr] = summary["percent"]
        average.details["column_stats"][attr] = summary["run_percent_stats"]

    for attr in ("tool_ok_percent", "avg_steps", "tokens_per_second"):
        values = [float(value) for value in (getattr(run, attr) for run in runs) if value is not None]
        value = sum(values) / len(values) if values else None
        setattr(average, attr, value)
        average.details["average"][attr] = value
        average.details["column_stats"][attr] = value_stats(values)

    total_values = [float(run.total_score) for run in runs]
    average.total_score = average_numeric(total_values)
    if average.total_score is None:
        average.total_score = 0.0
    average.details["average"]["total_score"] = average.total_score
    average.details["column_stats"]["total_score"] = value_stats(total_values)
    average.details["average"]["min_total_score"] = min_numeric(total_values)
    average.details["average"]["max_total_score"] = max_numeric(total_values)
    for attr in COLUMN_STATS_RUN_ATTRS:
        if attr in {"agent_advanced_percent", "dart_logic_advanced_percent", "flutter_ui_advanced_percent"}:
            continue
        run_values = [float(value) for value in (getattr(run, attr) for run in runs) if value is not None]
        average.details["column_stats"][attr] = value_stats(run_values)
    average.details["run_count"] = len(runs)
    model_meta = runs[0].details.get("model_config", {}) if runs and isinstance(runs[0].details, dict) else {}
    average.details["model_config"] = dict(model_meta)
    average.details["active_blocks"] = active_blocks_from_details(average)
    all_valid = all(getattr(run, "benchmark_valid", True) for run in runs)
    average.benchmark_valid = all_valid
    if not all_valid:
        errors = [getattr(run, "benchmark_error", "") for run in runs if not getattr(run, "benchmark_valid", True)]
        average.benchmark_error = " ; ".join(dict.fromkeys(e for e in errors if e))
        average.details["average"]["benchmark_valid"] = False
        average.details["average"]["benchmark_error"] = average.benchmark_error
    else:
        average.details["average"]["benchmark_valid"] = True
    average.details["average"]["request_max_tokens"] = runs[0].details.get("request_max_tokens", DEFAULT_MODEL_MAX_TOKENS) if runs and isinstance(runs[0].details, dict) else DEFAULT_MODEL_MAX_TOKENS
    average.details["average"]["agent_response_max_tokens"] = runs[0].details.get("agent_response_max_tokens", AGENT_RESPONSE_MAX_TOKENS) if runs and isinstance(runs[0].details, dict) else AGENT_RESPONSE_MAX_TOKENS
    return average


def summarize_section_runs(runs: list[BenchmarkResult], section: str, tasks: list[Any], attr: str) -> dict[str, Any]:
    task_summaries = []
    for task in tasks:
        task_percents = []
        for run in runs:
            run_details = run.details.get(section, []) if isinstance(run.details, dict) else []
            detail = next((item for item in run_details if item.get("title") == task.title), None)
            if detail and detail.get("fraction") is not None and detail.get("error_status", "") not in BENCHMARK_ERROR_STATUSES:
                task_percents.append(100.0 * float(detail["fraction"]))
        if task_percents:
            task_summaries.append({
                "title": task.title,
                "average_percent": sum(task_percents) / len(task_percents),
                "min_percent": min(task_percents),
                "max_percent": max(task_percents),
                "run_percents": task_percents,
            })

    # run_values via tasks-Filter neu berechnen: nur die übergebenen tasks zählen
    run_values = []
    for run in runs:
        run_details = run.details.get(section, []) if isinstance(run.details, dict) else []
        relevant = [detail for detail in run_details
                    if detail.get("title") in {t.title for t in tasks}
                    and detail.get("fraction") is not None
                    and detail.get("error_status", "") not in BENCHMARK_ERROR_STATUSES]
        if relevant:
            weighted_sum = sum(float(detail.get("fraction", 0.0)) * float(detail.get("weight", 0)) for detail in relevant)
            weight_sum = sum(float(detail.get("weight", 0)) for detail in relevant)
            run_value = 100.0 * weighted_sum / weight_sum if weight_sum else None
        else:
            run_value = None
        if run_value is not None:
            run_values.append(run_value)

    avg = sum(run_values) / len(run_values) if run_values else None
    return {
        "percent": avg,
        "run_percent_stats": value_stats(run_values),
        "tasks": task_summaries,
    }


def section_percent_attr(section: str) -> str:
    return {"coding": "coding_percent", "agent": "agent_percent", "dart_logic": "dart_logic_percent", "flutter_ui": "flutter_ui_percent"}[section]


COLUMN_STATS_RUN_ATTRS = (
    "flutter_ui_percent",
    "flutter_ui_advanced_percent",
    "dart_logic_percent",
    "dart_logic_advanced_percent",
    "agent_percent",
    "agent_advanced_percent",
    "coding_percent",
    "tool_ok_percent",
    "avg_steps",
    "tokens_per_second",
    "total_score",
)


def run_agent_dry_run() -> int:
    failures = 0
    class DummyClient:
        def __init__(self, command: str):
            self.command = command
        last_finish_reason = None
        def chat(self, messages, temperature=0.0, timeout=DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS, max_tokens=DEFAULT_MODEL_MAX_TOKENS):
            return '```json\n' + json.dumps({"tool": "run_code", "command": self.command}) + '\n```'
    for task in AGENT_TASKS:
        detail = run_agent_task(DummyClient(" ".join("python" if part == sys.executable else part for part in task.test_command)), task, threading.Event())
        if int(detail.get("total_checks") or 0) < 1:
            failures += 1
            print(f"FEHLER keine Teilchecks: {task.title}: {detail.get('last_output')}")
        else:
            print(f"{task.title}: dry-run checks={detail.get('passed_checks')}/{detail.get('total_checks')} difficulty={task.difficulty}")
    if failures:
        print(f"Agent Dry-Run fehlgeschlagen: {failures} Problem(e)")
        return 1
    print("Agent Dry-Run erfolgreich: alle Aufgaben liefern Teilcheck-Ausgaben")
    return 0


def run_one_model_cli() -> int:
    data = json.loads(Path(DEFAULT_MODELS_FILE).read_text(encoding="utf-8")) if Path(DEFAULT_MODELS_FILE).exists() else []
    models = [model_from_dict(item) for item in data[:1]]
    if not models:
        print("Kein Modell in models.json")
        return 2
    runner = BenchmarkRunner(status_cb=print, progress_cb=lambda done, total: print(f"PROGRESS:{done}/{total}"))
    results = runner.run(models, True, True, True, True, 1)
    for result in results:
        print(json.dumps(result_to_export_dict(result), ensure_ascii=False, indent=2))
        print("SUMMARY", json.dumps({
            "coding_percent": result.coding_percent,
            "dart_logic_percent": result.dart_logic_percent,
            "dart_logic_advanced_percent": result.dart_logic_advanced_percent,
            "flutter_ui_percent": result.flutter_ui_percent,
            "flutter_ui_advanced_percent": result.flutter_ui_advanced_percent,
            "agent_percent": result.agent_percent,
            "agent_advanced_percent": result.agent_advanced_percent,
            "tool_ok_percent": result.tool_ok_percent,
            "avg_steps": result.avg_steps,
            "tokens_per_second": result.tokens_per_second,
            "total_score": result.total_score,
        }, ensure_ascii=False))
    return 0


def value_stats(values: list[float]) -> dict[str, float | None]:
    return {
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


class ModelEditor(Toplevel):
    REASONING_EFFORTS = ["", "none", "low", "medium", "high", "max"]

    def __init__(self, parent: "BenchmarkApp", model: ModelConfig | None, on_save: Callable[[ModelConfig], None]) -> None:
        super().__init__(parent.root)
        self.title("Modell bearbeiten" if model else "Modell hinzufügen")
        self.resizable(False, False)
        self.on_save = on_save
        self.name_var = StringVar(value=model.name if model else "Ollama llama3")
        self.url_var = StringVar(value=model.endpoint_url if model else "http://localhost:11434")
        self.id_var = StringVar(value=model.model_id if model else "llama3")
        self.provider_var = StringVar(value=model.provider if model else "local")
        self.api_key_var = StringVar(value=model.api_key if model else "")
        self.api_key_visible = BooleanVar(value=False)
        self.reasoning_var = StringVar(value=model.reasoning_effort if model and model.reasoning_effort else "")
        self.provider_var.trace_add("write", self._on_provider_changed)
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=BOTH, expand=True)
        fields = (("Name", self.name_var), ("Provider", self.provider_var), ("Endpoint/Base-URL", self.url_var), ("Modell-ID", self.id_var))
        for row, (label, var) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=var, width=48).grid(row=row, column=1, sticky="ew", pady=4)
        reasoning_row = len(fields)
        ttk.Label(frame, text="Reasoning").grid(row=reasoning_row, column=0, sticky="w", pady=4)
        self.reasoning_combo = ttk.Combobox(frame, textvariable=self.reasoning_var, values=self.REASONING_EFFORTS, state="readonly", width=45)
        self.reasoning_combo.grid(row=reasoning_row, column=1, sticky="ew", pady=4)
        self._update_reasoning_state()
        api_key_row = reasoning_row + 1
        ttk.Label(frame, text="API-Key").grid(row=api_key_row, column=0, sticky="w", pady=4)
        api_key_frame = ttk.Frame(frame)
        api_key_frame.grid(row=api_key_row, column=1, sticky="ew", pady=4)
        self.api_key_entry = ttk.Entry(api_key_frame, textvariable=self.api_key_var, width=38, show="*")
        self.api_key_entry.pack(side=LEFT, fill=X, expand=True)
        self.show_key_btn = ttk.Button(api_key_frame, text="👁", width=3, command=self.toggle_api_key_visibility)
        self.show_key_btn.pack(side=LEFT, padx=(4, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=api_key_row + 1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Abbrechen", command=self.destroy).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="Speichern", command=self.save).pack(side=LEFT)

    def _on_provider_changed(self, *_args: Any) -> None:
        self._update_reasoning_state()

    def _update_reasoning_state(self) -> None:
        is_deepseek = self.provider_var.get().strip().lower() == "deepseek"
        if is_deepseek:
            if not self.reasoning_var.get():
                self.reasoning_var.set("high")
            self.reasoning_combo.config(state="readonly")
        else:
            self.reasoning_var.set("")
            self.reasoning_combo.config(state="disabled")

    def toggle_api_key_visibility(self) -> None:
        show = not self.api_key_visible.get()
        self.api_key_visible.set(show)
        self.api_key_entry.config(show="" if show else "*")

    def save(self) -> None:
        provider = self.provider_var.get().strip().lower() or "local"
        endpoint_url = self.url_var.get().strip() or (DEEPSEEK_BASE_URL if provider == "deepseek" else "")
        api_key = self.api_key_var.get().strip()
        reasoning_effort = self.reasoning_var.get().strip().lower() if provider == "deepseek" else ""
        model = ModelConfig(self.name_var.get().strip(), endpoint_url, self.id_var.get().strip(), provider, api_key, reasoning_effort)
        if not model.name or not endpoint_url or not model.model_id:
            messagebox.showerror("Ungültig", "Bitte Name, Provider, Endpoint/Base-URL und Modell-ID ausfüllen.")
            return
        self.on_save(model)
        self.destroy()


class LeaderboardWindow(Toplevel):
    def __init__(self, parent: "BenchmarkApp") -> None:
        super().__init__(parent.root)
        self.parent = parent
        self.title("Top 10 Bestenliste")
        self.geometry("1680x520")
        self.minsize(1300, 400)
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill=BOTH, expand=True)
        self.info_var = StringVar(value="")
        self.displayed_entries: list[dict[str, Any]] = []
        ttk.Label(frame, textvariable=self.info_var).pack(fill=X, pady=(0, 6))
        columns = ("rank", "model", "provider", "reasoning", "quant", "context", "score", "coding", "agent_adv", "tool_ok", "steps", "toks", "coding_fit", "agent_fit", "version", "runs", "spread", "date")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        headings = {
            "rank": "Rang",
            "model": "Modell",
            "provider": "Provider",
            "reasoning": "Reasoning",
            "quant": "Quant",
            "context": "Kontext",
            "score": "Gesamt %",
            "coding": "Coding %",
            "agent_adv": "Agent Adv. %",
            "tool_ok": "Tool-OK %",
            "steps": "Ø Schritte",
            "toks": "tok/s",
            "coding_fit": "Coding-Urteil",
            "agent_fit": "Agent-Urteil",
            "version": "Bench-Version",
            "runs": "Läufe",
            "spread": "Streuung",
            "date": "Datum",
        }
        widths = {"rank": 50, "model": 210, "provider": 80, "reasoning": 85, "quant": 70, "context": 75, "date": 130, "coding_fit": 260, "agent_fit": 320, "version": 95, "runs": 55, "spread": 100, "steps": 80}
        for col, title in headings.items():
            self.tree.heading(col, text=title)
            self.tree.column(col, width=widths.get(col, 85), anchor="center")
        self.tree.column("model", anchor="w")
        self.tree.pack(fill=BOTH, expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill=X, pady=(8, 0))
        ttk.Button(buttons, text="Aktualisieren", command=self.refresh).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="Als CSV exportieren", command=self.export_csv).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="Eintrag löschen", command=self.delete_selected_entry).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="Bestenliste löschen", command=self.clear_leaderboard).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="Schließen", command=self.destroy).pack(side=RIGHT, padx=4)
        self.refresh()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.displayed_entries = []
        try:
            entries = load_leaderboard_entries()
        except Exception as exc:  # noqa: BLE001
            self.info_var.set("leaderboard.json ist beschädigt oder nicht lesbar.")
            messagebox.showerror("Bestenliste beschädigt", f"leaderboard.json konnte nicht gelesen werden:\n{exc}")
            return
        valid_entries = [e for e in entries if not e.get("excluded_from_leaderboard")]
        invalid_count = len(entries) - len(valid_entries)
        entries = sorted(valid_entries, key=lambda item: float(item.get("avg_total_score") or item.get("total_score") or 0.0), reverse=True)[:10]
        self.displayed_entries = entries
        if not entries:
            msg = "Noch keine gespeicherten Bestenlisten-Einträge."
            if invalid_count:
                msg += f" ({invalid_count} ungültige ausgeblendet)"
            self.info_var.set(msg)
            return
        self.info_var.set(f"Top {len(entries)} nach Gesamt % absteigend sortiert.")
        for rank, entry in enumerate(entries, start=1):
            self.tree.insert(
                "", END, iid=str(rank - 1),
                values=(
                    rank,
                    entry.get("model", ""),
                    entry.get("provider", ""),
                    entry.get("reasoning_effort", ""),
                    entry.get("quant", ""),
                    entry.get("context_size", ""),
                    format_percent(_safe_float(entry.get("avg_total_score") or entry.get("total_score"))),
                    format_percent(_safe_float(entry.get("avg_coding_percent") or entry.get("coding_percent"))),
                    format_percent(_safe_float(entry.get("avg_agent_advanced_percent") or entry.get("agent_advanced_percent"))),
                    format_percent(_safe_float(entry.get("avg_tool_ok_percent") or entry.get("tool_ok_percent"))),
                    format_float(_safe_float(entry.get("avg_steps"))),
                    format_float(_safe_float(entry.get("avg_tokens_per_second") or entry.get("tokens_per_second"))),
                    coding_suitability_text(entry),
                    agent_suitability_text(entry),
                    entry.get("benchmark_version", BENCHMARK_VERSION),
                    entry.get("runs_count", 1),
                    self._format_spread(entry),
                    self._format_date(entry.get("timestamp")),
                ),
            )

    def delete_selected_entry(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Kein Eintrag ausgewählt", "Bitte zuerst einen Eintrag auswählen.")
            return
        try:
            selected_index = int(selection[0])
            selected_entry = self.displayed_entries[selected_index]
        except (IndexError, ValueError):
            messagebox.showinfo("Kein Eintrag ausgewählt", "Bitte zuerst einen Eintrag auswählen.")
            return
        if not messagebox.askyesno("Eintrag löschen", "Diesen Bestenlisten-Eintrag wirklich löschen?"):
            return
        try:
            entries = load_leaderboard_entries()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Löschen nicht möglich", f"leaderboard.json konnte nicht gelesen werden:\n{exc}")
            return
        selected_key = leaderboard_entry_key(selected_entry)
        remaining = [entry for entry in entries if leaderboard_entry_key(entry) != selected_key]
        if len(remaining) == len(entries):
            messagebox.showinfo("Eintrag nicht gefunden", "Der ausgewählte Bestenlisten-Eintrag wurde nicht mehr gefunden.")
            self.refresh()
            return
        write_leaderboard_entries(remaining)
        self.info_var.set("Eintrag gelöscht.")
        self.refresh()

    def clear_leaderboard(self) -> None:
        if not messagebox.askyesno("Bestenliste löschen", "Wirklich gesamte Bestenliste löschen?"):
            return
        write_leaderboard_entries([])
        self.info_var.set("Bestenliste gelöscht.")
        self.refresh()

    def export_csv(self) -> None:
        try:
            entries = sorted(load_leaderboard_entries(), key=lambda item: float(item.get("avg_total_score") or item.get("total_score") or 0.0), reverse=True)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Export nicht möglich", f"leaderboard.json konnte nicht gelesen werden:\n{exc}")
            return
        if not entries:
            messagebox.showinfo("Keine Einträge", "Die Bestenliste ist leer.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"leaderboard_{stamp}.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=LEADERBOARD_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for entry in entries:
                writer.writerow({field: entry.get(field) for field in LEADERBOARD_FIELDS})
        self.info_var.set(f"Exportiert: {path}")

    def _format_date(self, value: Any) -> str:
        text = str(value or "")
        if not text:
            return "–"
        return text.replace("T", " ")[:16]

    def _format_spread(self, entry: dict[str, Any]) -> str:
        low = _safe_float(entry.get("min_total_score"))
        high = _safe_float(entry.get("max_total_score"))
        if low is None or high is None:
            return "–"
        return f"{low:.1f}-{high:.1f}"


# ============================================================
#  ModelTestSlot – unabhängiger paralleler Test-Slot
# ============================================================

class ModelTestSlot:
    """Ein unabhängiger Test-Slot für ein einzelnes Modell mit eigenen Controls."""

    MAX_SLOTS = 3

    def __init__(self, parent: "BenchmarkApp", model: ModelConfig, slot_index: int) -> None:
        self.parent = parent
        self.model = model
        self.slot_index = slot_index
        self.results: list[BenchmarkResult] = []
        self.running = False
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

        self.coding_var = BooleanVar(value=True)
        self.agent_var = BooleanVar(value=False)
        self.dart_logic_var = BooleanVar(value=False)
        self.flutter_ui_var = BooleanVar(value=False)
        self.workflow_agent_var = BooleanVar(value=False)
        self.run_count_var = IntVar(value=1)
        self.status_var = StringVar(value=f"{model.name} – Bereit")
        self.progress_var = IntVar(value=0)
        self.workflow_evaluation_results: dict[str, Any] = {}

        self.frame = ttk.LabelFrame(parent.slots_frame, text=f"Slot {slot_index + 1}: {model.name}", padding=6)
        self._build()

    def _build(self) -> None:
        f = self.frame

        self.status_label = ttk.Label(f, textvariable=self.status_var, foreground="#005a9e")
        self.status_label.pack(fill=X, pady=(0, 4))

        # Row 1: Benchmark-Checkboxen
        opts = ttk.Frame(f)
        opts.pack(fill=X)
        ttk.Checkbutton(opts, text="Coding", variable=self.coding_var).pack(side=LEFT, padx=3)
        ttk.Checkbutton(opts, text="Agent", variable=self.agent_var).pack(side=LEFT, padx=3)
        ttk.Checkbutton(opts, text="Dart-Logik", variable=self.dart_logic_var).pack(side=LEFT, padx=3)
        ttk.Checkbutton(opts, text="Flutter-UI", variable=self.flutter_ui_var).pack(side=LEFT, padx=3)
        ttk.Label(opts, text="Läufe:").pack(side=LEFT, padx=(10, 2))
        ttk.Spinbox(opts, from_=1, to=5, textvariable=self.run_count_var, width=3).pack(side=LEFT)

        # Row 2: Workflow-Agent Checkbox + Buttons
        wf_row = ttk.Frame(f)
        wf_row.pack(fill=X, pady=(2, 0))
        self.wf_cb = ttk.Checkbutton(wf_row, text="Workflow-Agent", variable=self.workflow_agent_var)
        self.wf_cb.pack(side=LEFT, padx=3)
        self.show_prompt_btn = ttk.Button(wf_row, text="📋 Auftrag anzeigen", command=self.show_workflow_prompt)
        self.show_prompt_btn.pack(side=LEFT, padx=3)
        self.evaluate_btn = ttk.Button(wf_row, text="🔍 Auswerten", command=self.evaluate_workflow, state="disabled")
        self.evaluate_btn.pack(side=LEFT, padx=3)

        # Row 3: Start/Stop/Export/Entfernen
        btns = ttk.Frame(f)
        btns.pack(fill=X, pady=(4, 0))
        self.start_btn = ttk.Button(btns, text="▶ Start", command=self.start)
        self.start_btn.pack(side=LEFT, padx=2)
        self.stop_btn = ttk.Button(btns, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side=LEFT, padx=2)
        self.export_btn = ttk.Button(btns, text="💾 JSON", command=self.export_json)
        self.export_btn.pack(side=LEFT, padx=2)
        ttk.Button(btns, text="✕ Entfernen", command=self.remove).pack(side=RIGHT, padx=2)

        self.progress = ttk.Progressbar(f, mode="determinate", variable=self.progress_var)
        self.progress.pack(fill=X, pady=(4, 0))

    def show_workflow_prompt(self) -> None:
        """Zeigt den Auftragstext für den Workflow-Agent in einem Popup an.
        Erzeugt vorher einen Run-Arbeitsordner und bettet dessen Pfad in den Prompt ein."""
        if not WORKFLOW_AGENT_TASKS:
            messagebox.showinfo("Keine Aufgaben", "Es sind keine Workflow-Agent-Aufgaben definiert.")
            return

        base_dir = Path.cwd()

        # Git-Repository-Prüfung
        git_valid, git_error, _git_toplevel = _validate_git_repo(base_dir)
        if not git_valid:
            messagebox.showerror(
                "Kein gültiges Git-Repository",
                f"{git_error}\n\n"
                "Die Workflow-Agent-Funktion benötigt ein gültiges Git-Repository.\n"
                "Bitte zuerst einen Workflow-Agent-Arbeitsordner erzeugen oder den richtigen Ordner auswählen.",
            )
            return

        # Auswahl-Dialog: Welche Aufgabe?
        task_names = [t.title for t in WORKFLOW_AGENT_TASKS]
        dialog = Toplevel(self.frame)
        dialog.title("Workflow-Agent Auftrag")
        dialog.geometry("750x550")
        dialog.resizable(True, True)

        ttk.Label(dialog, text="Aufgabe auswählen:", font=("", 10, "bold")).pack(fill=X, padx=8, pady=(8, 4))
        task_var = StringVar(value=task_names[0])
        task_combo = ttk.Combobox(dialog, textvariable=task_var, values=task_names, state="readonly", width=60)
        task_combo.pack(fill=X, padx=8, pady=(0, 8))

        # Info-Label für den Arbeitsordner
        workdir_var = StringVar(value="")
        workdir_label = ttk.Label(dialog, textvariable=workdir_var, foreground="#005a9e", font=("", 9, "bold"))
        workdir_label.pack(fill=X, padx=8, pady=(0, 4))

        text_frame = ttk.Frame(dialog)
        text_frame.pack(fill=BOTH, expand=True, padx=8, pady=(0, 8))
        prompt_text = tk_text_widget(text_frame, wrap="word", width=80, height=18)
        prompt_text.pack(fill=BOTH, expand=True)

        def _build_full_prompt(task: WorkflowAgentTask, workdir: Path) -> str:
            """Baut den vollständigen Auftragstext mit Arbeitsordner-Info."""
            workdir_abs = str(workdir.resolve())
            header = (
                f"Arbeitsordner:\n"
                f"{workdir_abs}\n"
                f"\n"
                f"Nur in diesem Arbeitsordner arbeiten.\n"
                f"Nicht die Original-Fixtures unter fixtures/workflow_agent ändern.\n"
                f"Keine Dateien außerhalb dieses Arbeitsordners ändern.\n"
                f"\n"
            )
            return header + task.prompt

        def update_text(*_args: Any) -> None:
            selected = task_var.get()
            for t in WORKFLOW_AGENT_TASKS:
                if t.title == selected:
                    # Run-Arbeitsordner erzeugen
                    workdir = _create_run_workdir(t, base_dir)
                    workdir_var.set(f"Arbeitsordner: {workdir.resolve()}")
                    full_prompt = _build_full_prompt(t, workdir)
                    prompt_text.config(state="normal")
                    prompt_text.delete("1.0", END)
                    prompt_text.insert("1.0", full_prompt)
                    prompt_text.config(state="disabled")
                    break

        task_var.trace_add("write", update_text)
        update_text()

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill=X, padx=8, pady=(0, 8))
        ttk.Button(btn_frame, text="In Zwischenablage kopieren", command=lambda: self._copy_prompt_to_clipboard(prompt_text)).pack(side=LEFT, padx=4)
        ttk.Button(btn_frame, text="Schließen", command=dialog.destroy).pack(side=RIGHT, padx=4)
        ttk.Label(dialog, text="Kopiere den Auftrag in Cline/Roo/Kilo. Nach Agent-Lauf: »🔍 Auswerten« klicken.", foreground="#555").pack(fill=X, padx=8, pady=(0, 8))

    def _copy_prompt_to_clipboard(self, text_widget: Any) -> None:
        """Kopiert den Prompt-Text in die Zwischenablage."""
        content = text_widget.get("1.0", END).strip()
        self.frame.clipboard_clear()
        self.frame.clipboard_append(content)
        self.status_var.set(f"{self.model.name}: Auftrag in Zwischenablage kopiert")

    def evaluate_workflow(self) -> None:
        """Wertet den Workflow-Agent-Lauf aus und erzeugt BenchmarkResult."""
        if not WORKFLOW_AGENT_TASKS:
            messagebox.showinfo("Keine Aufgaben", "Keine Workflow-Agent-Aufgaben definiert.")
            return

        base_dir = Path.cwd()

        # Git-Repository-Prüfung
        git_valid, git_error, _git_toplevel = _validate_git_repo(base_dir)
        if not git_valid:
            messagebox.showerror(
                "Kein gültiges Git-Repository",
                f"{git_error}\n\n"
                "Die Workflow-Agent-Auswertung benötigt ein gültiges Git-Repository.\n"
                "Bitte zuerst einen Workflow-Agent-Arbeitsordner erzeugen oder den richtigen Ordner auswählen.",
            )
            return

        self.status_var.set(f"{self.model.name}: Workflow-Auswertung läuft…")
        self.evaluate_btn.config(state="disabled")

        def _find_latest_run_workdir(task: WorkflowAgentTask) -> Path | None:
            """Findet den neuesten Run-Arbeitsordner für eine Aufgabe."""
            runs_base = base_dir / WORKFLOW_AGENT_RUNS_DIR
            if not runs_base.exists():
                return None
            candidates = []
            for entry in runs_base.iterdir():
                if entry.is_dir() and entry.name.endswith(f"_{task.task_id}"):
                    try:
                        # Extrahiere Timestamp aus dem Ordnernamen (YYYYmmdd_HHMMSS_task_id)
                        ts_str = entry.name.split("_", 1)[0] + "_" + entry.name.split("_", 2)[1] if len(entry.name.split("_")) >= 2 else ""
                        candidates.append((entry, entry.stat().st_mtime))
                    except Exception:
                        candidates.append((entry, 0))
            if not candidates:
                return None
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[0][0]

        def _run_eval() -> None:
            wf_details = []
            total_weight = 0
            weighted_fraction = 0.0
            # Fehler-Statuses, die nicht als Modellversagen, sondern als Umgebungsfehler zählen
            env_error_statuses = BENCHMARK_ERROR_STATUSES | {
                "test_command_not_found", "fixture_not_found",
                "git_invalid", "workdir_missing", "pubspec_missing",
                "dart_not_found", "flutter_not_found",
                "no_files_changed", "original_fixture_modified",
            }
            any_workdir_missing = False
            for task in WORKFLOW_AGENT_TASKS:
                # Suche nach einem existierenden Run-Arbeitsordner
                run_workdir = _find_latest_run_workdir(task)
                if run_workdir:
                    detail = run_workflow_agent_evaluation(task, base_dir, run_workdir=run_workdir)
                else:
                    # Kein Run-Workdir gefunden – direkt workdir_missing, KEIN Fixture-Fallback
                    any_workdir_missing = True
                    detail = {
                        "task_id": task.task_id,
                        "title": task.title,
                        "weight": task.weight,
                        "workdir": None,
                        "fixture_source": str(base_dir / task.fixture_path),
                        "tests_exit_code": None,
                        "diff_check_exit_code": None,
                        "git_valid": git_valid,
                        "original_fixture_modified": False,
                        "error_status": "workdir_missing",
                        "fraction": None,
                        "workflow_agent_score": None,
                        "scope_violations": [],
                        "forbidden_actions": [],
                        "changed_files": [],
                        "test_result": {
                            "passed_checks": 0, "total_checks": 0, "fraction": None,
                            "output": "Kein Run-Arbeitsordner unter runs/workflow_agent/ gefunden.\n"
                                      "Bitte zuerst einen Arbeitsordner über »Auftrag anzeigen« erzeugen "
                                      "und den Agent-Lauf durchführen.\n"
                                      "Die Original-Fixtures unter fixtures/workflow_agent/ sind nur Templates "
                                      "und werden nicht ausgewertet.",
                        },
                    }
                wf_details.append(detail)
                total_weight += task.weight
                if detail.get("error_status") not in env_error_statuses or detail.get("error_status") is None:
                    wf_score = detail.get("workflow_agent_score")
                    if wf_score is not None:
                        weighted_fraction += task.weight * float(wf_score)
            wf_percent = 100.0 * weighted_fraction / total_weight if total_weight > 0 else None

            result = BenchmarkResult(model=self.model.name)
            result.workflow_agent_percent = wf_percent
            # Wenn alle WF-Tasks workdir_missing sind, ist das Ergebnis ungültig
            all_errors = [d.get("error_status") for d in wf_details if d.get("error_status")]
            if all_errors and all(e in env_error_statuses for e in all_errors):
                result.benchmark_valid = False
                result.benchmark_error = "workdir_missing"
            result.details = {
                "workflow_agent": wf_details,
                "workdir_missing": any_workdir_missing,
                "model_config": {
                    "name": self.model.name,
                    "provider": self.model.provider,
                    "model_id": self.model.model_id,
                    "quant": infer_quant(self.model.name, self.model.model_id),
                    "context_size": infer_context_size(self.model.name, self.model.model_id),
                    "reasoning_effort": getattr(self.model, "reasoning_effort", ""),
                },
            }
            if wf_percent is not None:
                result.total_score = wf_percent

            self.parent.queue.put((f"workflow_done_{self.slot_index}", result))

        threading.Thread(target=_run_eval, daemon=True).start()

    def handle_workflow_done(self, result: BenchmarkResult) -> None:
        """Verarbeitet das Ergebnis der Workflow-Auswertung."""
        self.workflow_evaluation_results = result.details.get("workflow_agent", {})
        self.results.append(result)
        self.evaluate_btn.config(state="normal")
        self.status_var.set(f"{self.model.name}: Workflow-Auswertung abgeschlossen")

        # GUI-Warnung bei fehlenden Run-Arbeitsordnern
        if result.details.get("workdir_missing") and not result.benchmark_valid:
            wf_details = result.details.get("workflow_agent", [])
            missing_tasks = [
                wf.get("title", wf.get("task_id", "?"))
                for wf in wf_details
                if wf.get("error_status") == "workdir_missing"
            ]
            task_list = "\n".join(f"  – {t}" for t in missing_tasks) if missing_tasks else "alle Aufgaben"
            messagebox.showwarning(
                "Workflow-Agent – Kein Arbeitsordner",
                f"Kein Run-Arbeitsordner gefunden für:\n{task_list}\n\n"
                "Bitte zuerst einen Arbeitsordner über »📋 Auftrag anzeigen« erzeugen "
                "und den Agent-Lauf im erzeugten Ordner unter runs/workflow_agent/ durchführen.\n\n"
                "Die Original-Fixtures unter fixtures/workflow_agent/ sind nur Templates "
                "und werden nicht ausgewertet.\n\n"
                "Ergebnis wurde als nicht bestanden/ungültig markiert.",
            )
        elif not result.benchmark_valid:
            messagebox.showwarning(
                "Workflow-Agent – Auswertung fehlgeschlagen",
                f"Workflow-Auswertung fehlgeschlagen: {result.benchmark_error or 'Unbekannter Fehler'}\n\n"
                "Ergebnis wurde als nicht bestanden/ungültig markiert.",
            )

        self.parent.on_workflow_completed(self, result)

    def start(self) -> None:
        if self.running:
            return
        # Workflow-Agent allein reicht nicht für Benchmark-Lauf (nur halbautomatisch)
        if not self.coding_var.get() and not self.agent_var.get() and not self.dart_logic_var.get() and not self.flutter_ui_var.get():
            if self.workflow_agent_var.get():
                self.evaluate_btn.config(state="normal")
                self.status_var.set(f"{self.model.name}: Workflow-Agent bereit – Auftrag anzeigen & auswerten")
                return
            messagebox.showerror("Kein Modus", "Bitte mindestens einen Testmodus auswählen.")
            return
        if self.dart_logic_var.get() and shutil.which("dart") is None:
            messagebox.showerror("Dart SDK nicht im PATH", "Dart-Logik kann nicht ausgeführt werden: Das Dart SDK wurde nicht im PATH gefunden.")
            return
        if self.flutter_ui_var.get() and shutil.which("flutter") is None:
            messagebox.showerror("Flutter SDK nicht im PATH", "Flutter-UI kann nicht ausgeführt werden: Das Flutter SDK wurde nicht im PATH gefunden.")
            return
        try:
            run_count = max(1, min(5, int(self.run_count_var.get())))
        except Exception:
            messagebox.showerror("Ungültige Läufe", "Bitte eine Laufanzahl zwischen 1 und 5 eingeben.")
            return
        self.running = True
        self.stop_event.clear()
        self.results = []
        self.progress_var.set(0)
        self.run_count_var.set(run_count)
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set(f"{self.model.name}: läuft…")

        slot_id = self.slot_index
        stop_event = self.stop_event
        model = self.model

        def progress_cb(done: int, total: int) -> None:
            self.parent.queue.put((f"progress_{slot_id}", (done, total)))

        def status_cb(msg: str) -> None:
            self.parent.queue.put((f"status_{slot_id}", msg))

        runner = BenchmarkRunner(
            status_cb=status_cb,
            progress_cb=progress_cb,
            stop_event=stop_event,
        )
        self.thread = threading.Thread(
            target=self._run_thread,
            args=(runner, model, self.coding_var.get(), self.agent_var.get(), self.dart_logic_var.get(), self.flutter_ui_var.get(), run_count, slot_id),
            daemon=True,
        )
        self.thread.start()

    def _run_thread(self, runner: BenchmarkRunner, model: ModelConfig, run_coding: bool, run_agent: bool, run_dart_logic: bool, run_flutter_ui: bool, run_count: int, slot_id: int) -> None:
        try:
            results = runner.run([model], run_coding, run_agent, run_dart_logic, run_flutter_ui, run_count)
            if self.stop_event.is_set():
                self.parent.queue.put((f"stopped_{slot_id}", results))
            else:
                self.parent.queue.put((f"done_{slot_id}", results))
        except Exception:
            self.parent.queue.put((f"error_{slot_id}", traceback.format_exc()))

    def stop(self) -> None:
        if self.running:
            self.stop_event.set()
            self.status_var.set(f"{self.model.name}: Abbruch…")

    def handle_done(self, results: list[BenchmarkResult]) -> None:
        self.running = False
        self.results = results
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set(f"{self.model.name}: Fertig")
        self.parent.on_slot_completed(self, results)

    def handle_stopped(self, results: list[BenchmarkResult]) -> None:
        self.running = False
        self.results = results
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set(f"{self.model.name}: Abgebrochen")
        self.parent.on_slot_completed(self, results)

    def handle_error(self, error_msg: str) -> None:
        self.running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set(f"{self.model.name}: Fehler")
        messagebox.showerror(f"Benchmark-Fehler – {self.model.name}", str(error_msg)[-4000:])

    def handle_status(self, msg: str) -> None:
        self.status_var.set(f"{self.model.name}: {msg}")

    def handle_progress(self, done: int, total: int) -> None:
        self.progress["maximum"] = max(1, total)
        self.progress_var.set(done)

    def remove(self) -> None:
        if self.running:
            self.stop_event.set()
        self.frame.destroy()
        self.parent.remove_slot(self)

    def export_json(self) -> None:
        if not self.results:
            messagebox.showinfo("Keine Ergebnisse", f"Für '{self.model.name}' gibt es noch keine Ergebnisse zum Exportieren.")
            return
        from datetime import datetime
        from tkinter import filedialog
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = safe_filename_part(self.model.name, fallback="model")
        thinking_part = safe_filename_part(
            getattr(self.model, "reasoning_effort", None)
            or getattr(self.model, "thinking", None)
            or getattr(self.model, "thinking_level", None),
            fallback="no-thinking",
        )
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile=f"llm_benchmark_{safe_name}_{thinking_part}_{stamp}.json",
            filetypes=[("JSON", "*.json"), ("Alle Dateien", "*.*")],
        )
        if not path:
            return
        try:
            data = [result_to_export_dict(r) for r in self.results]
            import json
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
            self.status_var.set(f"{self.model.name}: JSON exportiert → {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Export fehlgeschlagen", f"JSON-Export für '{self.model.name}' fehlgeschlagen:\n{exc}")

    def pack(self, **kwargs: Any) -> None:
        self.frame.pack(**kwargs)

    def pack_forget(self) -> None:
        self.frame.pack_forget()


# ============================================================
#  BenchmarkApp – Hauptfenster mit Slot-basierter UI
# ============================================================

class BenchmarkApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("1280x780")
        self.models: list[ModelConfig] = []
        self.slots: list[ModelTestSlot] = []
        self.all_results: list[BenchmarkResult] = []
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.active_model_index: int | None = None
        self.active_model_var = StringVar(value="Ausgewählt: –")
        self._build_ui()
        self.load_models()
        self.root.after(100, self.process_queue)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=BOTH, expand=True)

        # ---- LINKE SEITE: Modell-Liste ----
        left = ttk.LabelFrame(main, text="Modelle", padding=8)
        left.pack(side=LEFT, fill=Y, padx=(0, 8))

        self.model_tree = ttk.Treeview(left, columns=("name", "provider", "url", "id"), show="headings", height=10, selectmode="browse")
        self.model_tree.heading("name", text="Name")
        self.model_tree.heading("provider", text="Provider")
        self.model_tree.heading("url", text="Endpoint/Base-URL")
        self.model_tree.heading("id", text="Modell-ID")
        self.model_tree.column("name", width=140)
        self.model_tree.column("provider", width=65)
        self.model_tree.column("url", width=180)
        self.model_tree.column("id", width=130)
        self.model_tree.tag_configure("in_slot", background="#e6f3e6")
        self.model_tree.bind("<<TreeviewSelect>>", self.on_model_selected)
        self.model_tree.pack(fill=BOTH, expand=True)

        ttk.Label(left, textvariable=self.active_model_var, foreground="#005a9e").pack(fill=X, pady=(6, 0))

        # Pfeil-Buttons: Hinzufügen / Entfernen
        arrow_btns = ttk.Frame(left)
        arrow_btns.pack(fill=X, pady=(6, 0))
        self.add_to_slot_btn = ttk.Button(arrow_btns, text="→ In Test-Slot", command=self.add_selected_to_slot)
        self.add_to_slot_btn.pack(fill=X, pady=1)
        self.remove_from_slot_btn = ttk.Button(arrow_btns, text="← Aus Slot entfernen", command=self.remove_selected_from_slot)
        self.remove_from_slot_btn.pack(fill=X, pady=1)

        # Modell-Verwaltung
        model_btns = ttk.Frame(left)
        model_btns.pack(fill=X, pady=(6, 0))
        ttk.Button(model_btns, text="+ Neu", command=self.add_model).pack(side=LEFT, padx=1)
        ttk.Button(model_btns, text="✎ Bearb.", command=self.edit_model).pack(side=LEFT, padx=1)
        ttk.Button(model_btns, text="✕ Löschen", command=self.remove_model).pack(side=LEFT, padx=1)

        deepseek_btns = ttk.Frame(left)
        deepseek_btns.pack(fill=X, pady=(4, 0))
        ttk.Button(deepseek_btns, text="+ DS V4 Flash", command=lambda: self.add_deepseek_model("DeepSeek V4 Flash")).pack(fill=X, pady=1)
        ttk.Button(deepseek_btns, text="+ DS V4 Pro", command=lambda: self.add_deepseek_model("DeepSeek V4 Pro")).pack(fill=X, pady=1)

        # ---- RECHTE SEITE ----
        right = ttk.Frame(main)
        right.pack(side=RIGHT, fill=BOTH, expand=True)

        # Globaler Balken
        global_bar = ttk.Frame(right)
        global_bar.pack(fill=X, pady=(0, 6))
        ttk.Button(global_bar, text="Top 10", command=self.show_leaderboard).pack(side=LEFT, padx=2)
        ttk.Button(global_bar, text="Export CSV", command=lambda: self.export("csv")).pack(side=LEFT, padx=2)
        ttk.Button(global_bar, text="Export JSON", command=lambda: self.export("json")).pack(side=LEFT, padx=2)
        self.global_status_var = StringVar(value="Bereit – Modell links wählen und in Test-Slot schieben")
        ttk.Label(global_bar, textvariable=self.global_status_var, foreground="#555").pack(side=LEFT, padx=(20, 0))

        # Slot-Bereich
        slot_container = ttk.Frame(right)
        slot_container.pack(fill=X, pady=(0, 6))

        slot_canvas_frame = ttk.Frame(slot_container)
        slot_canvas_frame.pack(fill=X)

        self.slots_frame = ttk.Frame(slot_canvas_frame)
        self.slots_frame.pack(fill=X)

        placeholder = ttk.Label(self.slots_frame, text="Noch keine Test-Slots. Modell links auswählen und »→ In Test-Slot« klicken.", foreground="#888")
        placeholder.pack(fill=X, pady=20)
        self.slot_placeholder = placeholder

        # Ergebnis-Bereich
        results_frame = ttk.LabelFrame(right, text="Ergebnisse", padding=8)
        results_frame.pack(fill=BOTH, expand=True, pady=(6, 0))
        columns = ("model", "coding", "agent", "dartlogic", "flutterui", "workflowagent", "toolok", "steps", "codingfit", "agentfit", "toks", "score")
        self.result_tree = ttk.Treeview(results_frame, columns=columns, show="tree headings", height=8)
        self.result_tree.heading("#0", text="Typ")
        self.result_tree.column("#0", width=80, anchor="w")
        headings = {
            "model": "Modell", "coding": "Coding %", "agent": "Agent %", "dartlogic": "Dart-Logik %",
            "flutterui": "Flutter-UI %", "workflowagent": "Workflow-Agent %", "toolok": "Tool-OK %", "steps": "Ø Schritte",
            "codingfit": "Coding-Urteil", "agentfit": "Agent-Urteil", "toks": "tok/s", "score": "Gesamt",
        }
        for col, title in headings.items():
            self.result_tree.heading(col, text=title)
            self.result_tree.column(col, width=110 if col in ("workflowagent",) else 100 if col != "model" else 170, anchor="center")
        self.result_tree.pack(fill=BOTH, expand=True)

    # ---- Modell-Liste ----
    def load_models(self) -> None:
        path = Path(DEFAULT_MODELS_FILE)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.models = [model_from_dict(item) for item in data]
            except Exception:
                self.models = []
        if not self.models:
            self.models = [ModelConfig("Ollama llama3", "http://localhost:11434", "llama3")]
        self.refresh_models()

    def save_models(self) -> None:
        Path(DEFAULT_MODELS_FILE).write_text(json.dumps([model_to_dict(m) for m in self.models], indent=2), encoding="utf-8")

    def _slotted_model_indices(self) -> set[int]:
        return {self.models.index(slot.model) for slot in self.slots if slot.model in self.models}

    def refresh_models(self) -> None:
        self.model_tree.delete(*self.model_tree.get_children())
        slotted = self._slotted_model_indices()
        for idx, model in enumerate(self.models):
            tags = ("in_slot",) if idx in slotted else ()
            self.model_tree.insert("", END, iid=str(idx), values=(model.name, model.provider, model.endpoint_url, model.model_id), tags=tags)
        self.update_active_model_status()

    def on_model_selected(self, _event: Any) -> None:
        selection = self.model_tree.selection()
        if not selection:
            self.active_model_index = None
            self.update_active_model_status()
            return
        try:
            idx = int(selection[0])
        except (TypeError, ValueError):
            self.active_model_index = None
            self.update_active_model_status()
            return
        self.active_model_index = idx if 0 <= idx < len(self.models) else None
        self.update_active_model_status()

    def update_active_model_status(self) -> None:
        if self.active_model_index is not None and 0 <= self.active_model_index < len(self.models):
            self.active_model_var.set(f"Ausgewählt: {self.models[self.active_model_index].name}")
        else:
            self.active_model_var.set("Ausgewählt: –")

    def selected_model_index(self) -> int | None:
        if self.active_model_index is None or not (0 <= self.active_model_index < len(self.models)):
            return None
        return self.active_model_index

    # ---- Slot-Verwaltung ----
    def add_selected_to_slot(self) -> None:
        idx = self.selected_model_index()
        if idx is None:
            messagebox.showinfo("Auswahl", "Bitte zuerst ein Modell links auswählen.")
            return
        model = self.models[idx]
        if any(slot.model is model for slot in self.slots):
            messagebox.showinfo("Bereits im Slot", f"Das Modell '{model.name}' ist bereits in einem Test-Slot.")
            return
        if len(self.slots) >= ModelTestSlot.MAX_SLOTS:
            messagebox.showinfo("Maximale Slots", f"Maximal {ModelTestSlot.MAX_SLOTS} parallele Test-Slots möglich. Bitte zuerst einen Slot entfernen.")
            return
        slot = ModelTestSlot(self, model, len(self.slots))
        self.slots.append(slot)
        if self.slot_placeholder and self.slot_placeholder.winfo_exists():
            self.slot_placeholder.pack_forget()
        slot.pack(fill=X, pady=(0, 6))
        self.refresh_models()

    def remove_selected_from_slot(self) -> None:
        idx = self.selected_model_index()
        if idx is None:
            messagebox.showinfo("Auswahl", "Bitte ein Modell links auswählen, das aus einem Slot entfernt werden soll.")
            return
        model = self.models[idx]
        for slot in list(self.slots):
            if slot.model is model:
                slot.remove()
                return
        messagebox.showinfo("Nicht im Slot", f"Das Modell '{model.name}' ist in keinem Test-Slot.")

    def remove_slot(self, slot: ModelTestSlot) -> None:
        if slot in self.slots:
            self.slots.remove(slot)
        if not self.slots and self.slot_placeholder and self.slot_placeholder.winfo_exists():
            self.slot_placeholder.pack(fill=X, pady=20)
        self.refresh_models()

    def on_slot_completed(self, slot: ModelTestSlot, results: list[BenchmarkResult]) -> None:
        self.all_results.extend(results)
        self.refresh_results()
        self._save_leaderboard_for_results(results)

    # ---- Bestenliste & Export ----
    def _save_leaderboard_for_results(self, results: list[BenchmarkResult]) -> None:
        try:
            added, updated = update_leaderboard_with_results(results)
            self.global_status_var.set(f"Bestenliste aktualisiert ({added} neu, {updated} aktualisiert)")
        except Exception as exc:
            messagebox.showerror("Bestenliste nicht gespeichert", f"leaderboard.json konnte nicht aktualisiert werden:\n{exc}")

    def show_leaderboard(self) -> None:
        LeaderboardWindow(self)

    def export(self, fmt: str) -> None:
        results = list(self.all_results)
        for slot in self.slots:
            results.extend(slot.results)
        if not results:
            messagebox.showinfo("Keine Ergebnisse", "Es gibt noch keine Ergebnisse zum Exportieren.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if fmt == "csv":
            path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=f"llm_benchmark_{stamp}.csv")
            if not path:
                return
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["row_type", "run_index", "model", "reasoning_effort", "effective_reasoning_effort", "coding_percent", "agent_percent", "dart_logic_percent", "flutter_ui_percent", "workflow_agent_percent", "agent_advanced_percent", "dart_logic_advanced_percent", "flutter_ui_advanced_percent", "tool_ok_percent", "avg_steps", "tokens_per_second", "total_score", "coding_suitability", "coding_suitability_reason", "agent_suitability", "agent_suitability_reason"])
                writer.writeheader()
                for result in results:
                    writer.writerow(flatten_result(result, "average"))
                    for run in result.details.get("runs", []):
                        writer.writerow(flatten_result_dict(run, "run"))
        else:
            path = filedialog.asksaveasfilename(defaultextension=".json", initialfile=f"llm_benchmark_{stamp}.json")
            if not path:
                return
            data = [result_to_export_dict(result) for result in results]
            Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.global_status_var.set(f"Exportiert: {path}")

    # ---- Modell-Verwaltung ----
    def add_model(self) -> None:
        def on_save(model: ModelConfig) -> None:
            self.models.append(model)
            self.save_models()
            self.refresh_models()
        ModelEditor(self, None, on_save)

    def add_deepseek_model(self, name: str) -> None:
        existing = next((idx for idx, item in enumerate(self.models) if item.provider == "deepseek" and item.model_id == DEEPSEEK_MODELS.get(name, "")), None)
        if existing is not None:
            model = self.models[existing]
            model = make_deepseek_model(name, model.api_key, model.reasoning_effort)
        else:
            model = make_deepseek_model(name, reasoning_effort="high")
        def on_save(saved_model: ModelConfig) -> None:
            existing_idx = next((idx for idx, item in enumerate(self.models) if item.provider == "deepseek" and item.model_id == DEEPSEEK_MODELS.get(name, "")), None)
            if existing_idx is None:
                self.models.append(saved_model)
            else:
                self.models[existing_idx] = saved_model
            self.save_models()
            self.refresh_models()
        ModelEditor(self, model, on_save)

    def edit_model(self) -> None:
        idx = self.selected_model_index()
        if idx is None:
            messagebox.showinfo("Auswahl", "Bitte ein Modell auswählen.")
            return
        def on_save(model: ModelConfig) -> None:
            self.models[idx] = model
            self.save_models()
            self.refresh_models()
        ModelEditor(self, self.models[idx], on_save)

    def remove_model(self) -> None:
        idx = self.selected_model_index()
        if idx is None:
            return
        model = self.models[idx]
        for slot in list(self.slots):
            if slot.model is model:
                slot.remove()
        del self.models[idx]
        self.active_model_index = None
        self.save_models()
        self.refresh_models()

    # ---- Queue-Verarbeitung ----
    def process_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                for slot in self.slots:
                    sid = slot.slot_index
                    if kind == f"status_{sid}":
                        slot.handle_status(str(payload))
                        break
                    elif kind == f"progress_{sid}":
                        done, total = payload
                        slot.handle_progress(done, total)
                        break
                    elif kind == f"done_{sid}":
                        slot.handle_done(payload)
                        break
                    elif kind == f"stopped_{sid}":
                        slot.handle_stopped(payload)
                        break
                    elif kind == f"error_{sid}":
                        slot.handle_error(str(payload))
                        break
                    elif kind == f"workflow_done_{sid}":
                        slot.handle_workflow_done(payload)
                        break
        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)

    def on_workflow_completed(self, slot: ModelTestSlot, result: BenchmarkResult) -> None:
        self.all_results.append(result)
        self.refresh_results()

    def refresh_results(self) -> None:
        self.result_tree.delete(*self.result_tree.get_children())
        all_results = list(self.all_results)
        for slot in self.slots:
            all_results.extend(slot.results)
        seen_models: set[str] = set()
        for result in all_results:
            if result.model in seen_models:
                continue
            seen_models.add(result.model)
            coding_fit, coding_reason = coding_suitability_assessment(result)
            agent_fit, agent_reason = agent_suitability_assessment(result)
            parent = self.result_tree.insert(
                "", END, text="Ø",
                values=(
                    result.model,
                    format_percent_with_range(result, "coding_percent"),
                    format_percent_with_range(result, "agent_percent"),
                    format_percent_with_range(result, "dart_logic_percent"),
                    format_percent_with_range(result, "flutter_ui_percent"),
                    format_percent_with_range(result, "workflow_agent_percent"),
                    format_percent_with_range(result, "tool_ok_percent"),
                    format_float_with_range(result, "avg_steps"),
                    f"{coding_fit} {coding_reason}",
                    f"{agent_fit} {agent_reason}",
                    format_float_with_range(result, "tokens_per_second"),
                    format_float_with_range(result, "total_score"),
                ),
                open=False,
            )
            for run in result.details.get("runs", []):
                self.result_tree.insert(
                    parent, END,
                    text=f"Lauf {run.get('details', {}).get('run_index', '?')}",
                    values=(
                        run.get("model", result.model), format_percent(run.get("coding_percent")),
                        format_percent(run.get("agent_percent")), format_percent(run.get("dart_logic_percent")),
                        format_percent(run.get("flutter_ui_percent")), format_percent(run.get("workflow_agent_percent")),
                        format_percent(run.get("tool_ok_percent")),
                        format_float(run.get("avg_steps")), "", "",
                        format_float(run.get("tokens_per_second")), format_float(run.get("total_score")),
                    ),
                )
            task_breakdown = result.details.get("task_breakdown", {})
            for section, tasks in task_breakdown.items():
                for task in tasks:
                    self.result_tree.insert(
                        parent, END, text="Aufgabe",
                        values=(
                            f"{section}: {task.get('title')}", format_run_percent_list(task.get("run_percents", [])),
                            "", "", "", "", "", "", "", "", "", format_percent(task.get("average_percent")),
                        ),
                    )

            # Workflow-Agent Details anzeigen
            wf_details = result.details.get("workflow_agent", [])
            for wf in wf_details:
                violations = "; ".join(wf.get("scope_violations", [])) or "-"
                forbidden = "; ".join(wf.get("forbidden_actions", [])) or "-"
                changed = ", ".join(wf.get("changed_files", [])) or "-"
                test_info = wf.get("test_result") or {}
                test_str = f"{test_info.get('passed_checks', 0)}/{test_info.get('total_checks', 0)}"
                self.result_tree.insert(
                    parent, END, text="WF-Agent",
                    values=(
                        f"WF: {wf.get('title', wf.get('task_id', '?'))}",
                        f"Tests: {test_str}",
                        f"Score: {format_percent(wf.get('workflow_agent_score'))}",
                        f"Scope-V: {violations}"[:80],
                        f"Verboten: {forbidden}"[:80],
                        f"Dateien: {changed}"[:80],
                        "", "", "", "", format_percent(wf.get('fraction')),
                    ),
                )

    def run(self) -> None:
        self.root.mainloop()


# ---- Export-Funktionen ----

def result_to_export_dict(result: BenchmarkResult) -> dict[str, Any]:
    average = dict(result.details.get("average", {}))
    average.update(
        {
            "model": result.model,
            "coding_percent": result.coding_percent,
            "agent_percent": result.agent_percent,
            "dart_logic_percent": result.dart_logic_percent,
            "flutter_ui_percent": result.flutter_ui_percent,
            "workflow_agent_percent": result.workflow_agent_percent,
            "agent_advanced_percent": result.agent_advanced_percent,
            "dart_logic_advanced_percent": result.dart_logic_advanced_percent,
            "flutter_ui_advanced_percent": result.flutter_ui_advanced_percent,
            "tool_ok_percent": result.tool_ok_percent,
            "avg_steps": result.avg_steps,
            "tokens_per_second": result.tokens_per_second,
            "total_score": result.total_score,
            "coding_suitability": coding_suitability_assessment(result)[0],
            "coding_suitability_reason": coding_suitability_assessment(result)[1],
            "agent_suitability": agent_suitability_assessment(result)[0],
            "agent_suitability_reason": agent_suitability_assessment(result)[1],
            "column_stats": result.details.get("column_stats", {}),
            "task_breakdown": result.details.get("task_breakdown", {}),
        }
    )
    return sanitize_for_export({
        "model": result.model,
        "reasoning_effort": result.details.get("model_config", {}).get("reasoning_effort", "") if isinstance(result.details, dict) else "",
        "effective_reasoning_effort": result.details.get("model_config", {}).get("effective_reasoning_effort", "") if isinstance(result.details, dict) else "",
        "runs": result.details.get("runs", []),
        "average": average,
    })


def flatten_result(result: BenchmarkResult, row_type: str = "average") -> dict[str, Any]:
    coding_fit, coding_reason = coding_suitability_assessment(result)
    agent_fit, agent_reason = agent_suitability_assessment(result)
    return {
        "row_type": row_type,
        "run_index": "",
        "model": result.model,
        "reasoning_effort": result.details.get("model_config", {}).get("reasoning_effort", "") if isinstance(result.details, dict) else "",
        "effective_reasoning_effort": result.details.get("model_config", {}).get("effective_reasoning_effort", "") if isinstance(result.details, dict) else "",
        "coding_percent": result.coding_percent,
        "agent_percent": result.agent_percent,
        "dart_logic_percent": result.dart_logic_percent,
        "flutter_ui_percent": result.flutter_ui_percent,
        "workflow_agent_percent": result.workflow_agent_percent,
        "agent_advanced_percent": result.agent_advanced_percent,
        "dart_logic_advanced_percent": result.dart_logic_advanced_percent,
        "flutter_ui_advanced_percent": result.flutter_ui_advanced_percent,
        "tool_ok_percent": result.tool_ok_percent,
        "avg_steps": result.avg_steps,
        "tokens_per_second": result.tokens_per_second,
        "total_score": result.total_score,
        "coding_suitability": coding_fit,
        "coding_suitability_reason": coding_reason,
        "agent_suitability": agent_fit,
        "agent_suitability_reason": agent_reason,
    }


def flatten_result_dict(result: dict[str, Any], row_type: str = "run") -> dict[str, Any]:
    details = result.get("details", {}) if isinstance(result.get("details"), dict) else {}
    return {
        "row_type": row_type,
        "run_index": details.get("run_index", ""),
        "model": result.get("model"),
        "reasoning_effort": details.get("model_config", {}).get("reasoning_effort", "") if isinstance(details.get("model_config"), dict) else "",
        "effective_reasoning_effort": details.get("model_config", {}).get("effective_reasoning_effort", "") if isinstance(details.get("model_config"), dict) else "",
        "coding_percent": result.get("coding_percent"),
        "agent_percent": result.get("agent_percent"),
        "dart_logic_percent": result.get("dart_logic_percent"),
        "flutter_ui_percent": result.get("flutter_ui_percent"),
        "workflow_agent_percent": result.get("workflow_agent_percent"),
        "agent_advanced_percent": result.get("agent_advanced_percent"),
        "dart_logic_advanced_percent": result.get("dart_logic_advanced_percent"),
        "flutter_ui_advanced_percent": result.get("flutter_ui_advanced_percent"),
        "tool_ok_percent": result.get("tool_ok_percent"),
        "avg_steps": result.get("avg_steps"),
        "tokens_per_second": result.get("tokens_per_second"),
        "total_score": result.get("total_score"),
        "coding_suitability": "",
        "coding_suitability_reason": "",
        "agent_suitability": "",
        "agent_suitability_reason": "",
    }


def format_percent(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f}"


def format_float(value: float | None) -> str:
    return "–" if value is None else f"{value:.2f}"


def format_percent_with_range(result: BenchmarkResult, attr: str) -> str:
    return format_value_with_range(getattr(result, attr), result.details.get("column_stats", {}).get(attr), percent=True)


def format_float_with_range(result: BenchmarkResult, attr: str) -> str:
    return format_value_with_range(getattr(result, attr), result.details.get("column_stats", {}).get(attr), percent=False)


def format_value_with_range(value: float | None, stats: dict[str, Any] | None, percent: bool) -> str:
    formatter = format_percent if percent else format_float
    main = formatter(value)
    if value is None or not stats or stats.get("min") is None or stats.get("max") is None:
        return main
    low = formatter(stats.get("min"))
    high = formatter(stats.get("max"))
    return f"{main} ({low}-{high})"


def format_run_percent_list(values: list[Any]) -> str:
    return "[" + ", ".join("–" if value is None else f"{float(value):.0f}%" for value in values) + "]"


# ---- Dry-Runs & CLI ----

def run_dart_logic_dry_run() -> int:
    if not run_extract_dart_code_self_test():
        return 1
    if shutil.which("dart") is None:
        print("Dart SDK nicht im PATH")
        return 2
    failures = 0
    for task in DART_LOGIC_TASKS:
        reference = run_dart_logic_code(textwrap.dedent(task.reference_solution).strip(), task)
        broken = run_dart_logic_code(textwrap.dedent(task.broken_solution).strip(), task)
        ref_percent = 100.0 * float(reference.get("fraction") or 0.0)
        broken_percent = 100.0 * float(broken.get("fraction") or 0.0)
        print(f"{task.title}: reference={ref_percent:.1f}% ({reference.get('passed_checks')}/{reference.get('total_checks')}), broken={broken_percent:.1f}% ({broken.get('passed_checks')}/{broken.get('total_checks')})")
        if ref_percent != 100.0:
            failures += 1
            print(f"  FEHLER Referenz nicht 100%: {reference.get('output', '')[-1000:]}")
        if broken_percent >= 100.0 or broken_percent == ref_percent:
            failures += 1
            print(f"  FEHLER Grader blind für kaputte Lösung: {broken.get('output', '')[-1000:]}")
    if failures:
        print(f"Dart-Logik Dry-Run fehlgeschlagen: {failures} Problem(e)")
        return 1
    print("Dart-Logik Dry-Run erfolgreich: alle Referenzen 100%, kaputte Lösungen < 100%")
    return 0


def run_coding_dry_run() -> int:
    failures = 0
    reference_solutions = {
        "Unique stable": """
def unique_stable(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
""",
        "Run-length encode": """
def rle(text):
    if not text:
        return []
    out = []
    current = text[0]
    count = 1
    for ch in text[1:]:
        if ch == current:
            count += 1
        else:
            out.append((current, count))
            current = ch
            count = 1
    out.append((current, count))
    return out
""",
        "Topological sort": """
def toposort(nodes, edges):
    nodes = list(nodes)
    graph = {node: [] for node in nodes}
    indeg = {node: 0 for node in nodes}
    for a, b in edges:
        if b not in graph[a]:
            graph[a].append(b)
            indeg[b] += 1
    ready = [node for node in nodes if indeg[node] == 0]
    out = []
    while ready:
        node = ready.pop(0)
        out.append(node)
        for nxt in graph[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
    if len(out) != len(nodes):
        raise ValueError("Zyklus")
    return out
""",
    }
    class DummyClient:
        last_finish_reason = None
        def __init__(self, code: str):
            self.code = code
        def chat(self, messages, temperature=0.0, timeout=DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS, max_tokens=DEFAULT_MODEL_MAX_TOKENS):
            return f"```python\n{self.code}\n```"

    for task in CODING_TASKS:
        if task.title not in reference_solutions:
            continue
        client = DummyClient(reference_solutions[task.title])
        detail = run_coding_task(client, task, threading.Event())
        fraction = float(detail.get("fraction", 0.0))
        if fraction != 1.0:
            failures += 1
            print(f"FEHLER {task.title}: Referenz nicht 100% ({detail.get('passed_checks')}/{detail.get('total_checks')})")
        else:
            print(f"{task.title}: Referenz 100% ✓")
    if failures:
        print(f"Coding Dry-Run fehlgeschlagen: {failures} Problem(e)")
        return 1
    print("Coding Dry-Run erfolgreich: alle Referenzen 100%")
    return 0


def run_extract_dart_code_self_test() -> bool:
    test_code = "Map<String,dynamic> foo(Map<String,dynamic> x) => x;"
    result = extract_code_block(f"```dart\n{test_code}\n```")
    if result != test_code:
        print(f"FEHLER extract_code_block Dart: {result!r} != {test_code!r}")
        return False
    result2 = extract_code_block(f"```\n{test_code}\n```")
    if result2 != test_code:
        print(f"FEHLER extract_code_block ohne Sprache: {result2!r} != {test_code!r}")
        return False
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cli", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        sys.exit(run_coding_dry_run() or run_agent_dry_run() or run_dart_logic_dry_run())
    elif args.cli:
        sys.exit(run_one_model_cli())
    else:
        app = BenchmarkApp()
        app.run()
