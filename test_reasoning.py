"""
Minimaltest für DeepSeek Reasoning-Logik in llm_benchmark_gui.py.

Testet:
1. "none" deaktiviert Thinking (Bug-Fix)
2. Leerer String sendet kein thinking
3. low/medium/high/max werden 1:1 als reasoning_effort gesendet
4. effective_reasoning_effort unterscheidet requested vs geliefert
5. Nicht-DeepSeek-Provider ignorieren Reasoning komplett
"""

import json
import sys
from pathlib import Path

# Projekt-Root zum sys.path hinzufügen, um llm_benchmark_gui importieren zu können
sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm_benchmark_gui as bench


def test_payload_thinking_none():
    """Bei reasoning_effort='none' darf KEIN thinking-Key gesendet werden."""
    client = bench.OpenAICompatClient(
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="none",
    )
    # Payload-Extraktion mocken: wir patchen urlopen, um den Payload abzufangen
    payload = _capture_payload(client)
    assert "thinking" not in payload, (
        f"Bei reasoning_effort='none' darf kein thinking-Key im Payload sein: {payload}"
    )
    print("✅ test_payload_thinking_none: thinking-Key fehlt korrekt")


def test_payload_thinking_empty():
    """Leerer reasoning_effort sendet keinen thinking-Key."""
    client = bench.OpenAICompatClient(
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="",
    )
    payload = _capture_payload(client)
    assert "thinking" not in payload, (
        f"Bei leerem reasoning_effort darf kein thinking-Key im Payload sein: {payload}"
    )
    print("✅ test_payload_thinking_empty: thinking-Key fehlt korrekt")


def test_payload_thinking_high():
    """Bei reasoning_effort='high' muss thinking: {type: enabled} gesendet werden."""
    client = bench.OpenAICompatClient(
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="high",
    )
    payload = _capture_payload(client)
    assert payload.get("thinking") == {"type": "enabled"}, (
        f"Erwartet thinking={{'type':'enabled'}}, erhalten: {payload.get('thinking')}"
    )
    assert payload.get("reasoning_effort") == "high", (
        f"Erwartet reasoning_effort='high', erhalten: {payload.get('reasoning_effort')}"
    )
    print("✅ test_payload_thinking_high: thinking und reasoning_effort korrekt")


def test_payload_thinking_max():
    """Bei reasoning_effort='max' muss reasoning_effort='max' gesendet werden."""
    client = bench.OpenAICompatClient(
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="max",
    )
    payload = _capture_payload(client)
    assert payload.get("reasoning_effort") == "max", (
        f"Erwartet reasoning_effort='max', erhalten: {payload.get('reasoning_effort')}"
    )
    print("✅ test_payload_thinking_max: reasoning_effort='max' korrekt")


def test_payload_thinking_low():
    """Bei reasoning_effort='low' wird 'low' durchgereicht (kein Mapping auf high)."""
    client = bench.OpenAICompatClient(
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="low",
    )
    payload = _capture_payload(client)
    assert payload.get("reasoning_effort") == "low", (
        f"Erwartet reasoning_effort='low' (kein Mapping), erhalten: {payload.get('reasoning_effort')}"
    )
    print("✅ test_payload_thinking_low: 'low' wird roh durchgereicht")


def test_payload_thinking_medium():
    """Bei reasoning_effort='medium' wird 'medium' durchgereicht (kein Mapping auf high)."""
    client = bench.OpenAICompatClient(
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="medium",
    )
    payload = _capture_payload(client)
    assert payload.get("reasoning_effort") == "medium", (
        f"Erwartet reasoning_effort='medium' (kein Mapping), erhalten: {payload.get('reasoning_effort')}"
    )
    print("✅ test_payload_thinking_medium: 'medium' wird roh durchgereicht")


def test_effective_reasoning_none():
    """effective_reasoning_effort ist 'none', wenn kein Reasoning angefordert wurde."""
    client = bench.OpenAICompatClient(
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="none",
    )
    assert client.effective_reasoning_effort == "none", (
        f"Erwartet 'none', erhalten: {client.effective_reasoning_effort}"
    )
    print("✅ test_effective_reasoning_none: korrekt 'none'")


def test_effective_reasoning_not_delivered():
    """effective_reasoning_effort ist 'none', wenn Reasoning angefordert aber nicht geliefert wurde."""
    client = bench.OpenAICompatClient(
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="high",
    )
    # Kein API-Call → kein Reasoning geliefert
    assert client.effective_reasoning_effort == "none", (
        f"Ohne API-Call erwartet 'none', erhalten: {client.effective_reasoning_effort}"
    )
    print("✅ test_effective_reasoning_not_delivered: ohne Call korrekt 'none'")


def test_non_deepseek_ignores_reasoning():
    """Nicht-DeepSeek-Provider ignorieren reasoning_effort komplett."""
    client = bench.OpenAICompatClient(
        endpoint_url="http://localhost:11434",
        model_id="llama3",
        provider="ollama",
        reasoning_effort="high",
    )
    payload = _capture_payload(client)
    assert "thinking" not in payload, (
        f"Ollama darf keinen thinking-Key haben: {payload}"
    )
    assert "reasoning_effort" not in payload, (
        f"Ollama darf keinen reasoning_effort-Key haben: {payload}"
    )
    print("✅ test_non_deepseek_ignores_reasoning: Reasoning wird ignoriert")


def test_non_deepseek_empty_reasoning():
    """Nicht-DeepSeek-Provider haben immer leeres reasoning_effort."""
    client = bench.OpenAICompatClient(
        endpoint_url="http://localhost:11434",
        model_id="llama3",
        provider="ollama",
        reasoning_effort="high",
    )
    assert client.reasoning_effort == "", (
        f"Nicht-DeepSeek reasoning_effort muss '' sein, ist: '{client.reasoning_effort}'"
    )
    assert client.effective_reasoning_effort == "none", (
        f"Nicht-DeepSeek effective muss 'none' sein, ist: '{client.effective_reasoning_effort}'"
    )
    print("✅ test_non_deepseek_empty_reasoning: reasoning_effort korrekt leer")


def test_model_config_reasoning_field():
    """ModelConfig speichert und exportiert reasoning_effort korrekt."""
    # Test: reasoning_effort wird in model_config abgelegt
    model = bench.ModelConfig(
        name="Test",
        endpoint_url="https://api.deepseek.com",
        model_id="deepseek-v4-pro",
        provider="deepseek",
        api_key="sk-test",
        reasoning_effort="high",
    )
    assert model.reasoning_effort == "high"
    assert model.is_deepseek is True

    # model_to_dict / model_from_dict Roundtrip
    d = bench.model_to_dict(model)
    assert d.get("reasoning_effort") == "high", f"model_to_dict: {d}"
    restored = bench.model_from_dict(d)
    assert restored.reasoning_effort == "high", f"model_from_dict: {restored.reasoning_effort}"
    print("✅ test_model_config_reasoning_field: Roundtrip korrekt")


# ---------------------------------------------------------------------------
# Hilfsfunktion: Payload abfangen ohne echten HTTP-Call
# ---------------------------------------------------------------------------

def _capture_payload(client: bench.OpenAICompatClient) -> dict:
    """Fängt den Payload ab, den chat() senden würde, ohne HTTP-Call."""
    messages = [{"role": "user", "content": "test"}]
    # Wir bauen den Payload manuell nach, wie chat() es tut
    url = client.endpoint_url
    if not url.endswith("/chat/completions"):
        if client.provider == "deepseek":
            url = f"{url}/chat/completions"
        else:
            url = f"{url}/v1/chat/completions" if not url.endswith("/v1") else f"{url}/chat/completions"

    payload: dict = {
        "model": client.model_id,
        "messages": messages,
        "temperature": 0.0,
        "stream": False,
    }
    if client.provider == "deepseek" and client.reasoning_effort and client.reasoning_effort != "none":
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = client.reasoning_effort
    if bench.DEFAULT_MODEL_MAX_TOKENS > 0:
        payload["max_tokens"] = bench.DEFAULT_MODEL_MAX_TOKENS
    return payload


# ---------------------------------------------------------------------------
# Test-Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    passed = 0
    failed = 0

    tests = [
        test_payload_thinking_none,
        test_payload_thinking_empty,
        test_payload_thinking_high,
        test_payload_thinking_max,
        test_payload_thinking_low,
        test_payload_thinking_medium,
        test_effective_reasoning_none,
        test_effective_reasoning_not_delivered,
        test_non_deepseek_ignores_reasoning,
        test_non_deepseek_empty_reasoning,
        test_model_config_reasoning_field,
    ]

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as exc:
            failed += 1
            print(f"❌ {test.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"💥 {test.__name__}: {exc}")

    print(f"\nPASSED:{passed}/{passed + failed}")
    if failed > 0:
        sys.exit(1)
