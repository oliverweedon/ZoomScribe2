"""ZoomScribe 2 persistent configuration — stored at ~/.zoomscribe/config.json.

Shares the same config file as ZoomScribe 1 so API keys, template IDs,
and cost totals carry over.
"""

import json
from pathlib import Path

_DIR  = Path.home() / ".zoomscribe"
_PATH = _DIR / "config.json"

_DEFAULTS: dict = {
    # ── Google Docs ──────────────────────────────────────────────────────────
    "template_doc_id": "",

    # ── AI model ─────────────────────────────────────────────────────────────
    "api_key":        "",
    "model_provider": "anthropic",
    "model_name":     "claude-haiku-4-5",

    # ── Cost tracking ────────────────────────────────────────────────────────
    "session_tokens_in":  0,
    "session_tokens_out": 0,
    "session_cost":       0.0,
    "total_cost":         0.0,

    # ── Haiku 4.5 pricing ($/token) ──────────────────────────────────────────
    "price_input_per_token":        0.0000008,
    "price_input_cached_per_token": 0.000000064,
    "price_output_per_token":       0.000004,
}

_data: dict = {}


def _load() -> None:
    global _data
    _data = dict(_DEFAULTS)
    if _PATH.exists():
        try:
            with open(_PATH) as f:
                saved = json.load(f)
            _data.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    _data["session_tokens_in"]  = 0
    _data["session_tokens_out"] = 0
    _data["session_cost"]       = 0.0


def get(key: str, default=None):
    return _data.get(key, _DEFAULTS.get(key, default))


def set_value(key: str, value) -> None:
    _data[key] = value
    _save()


def _save() -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    with open(_PATH, "w") as f:
        json.dump(_data, f, indent=2)


def add_cost(tokens_in: int, tokens_out: int, cached_in: int = 0) -> float:
    """Record token usage; return cost of this call in USD."""
    cost = (
        (tokens_in - cached_in) * _data["price_input_per_token"]
        + cached_in             * _data["price_input_cached_per_token"]
        + tokens_out            * _data["price_output_per_token"]
    )
    _data["session_tokens_in"]  += tokens_in
    _data["session_tokens_out"] += tokens_out
    _data["session_cost"]       += cost
    _data["total_cost"]         += cost
    _save()
    return cost


def session_summary() -> str:
    c = _data["session_cost"]
    if c < 0.001:
        return "Session: <$0.001"
    return f"Session: ${c:.4f}"


_load()
