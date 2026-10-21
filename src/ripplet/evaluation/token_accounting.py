"""Token and cost accounting for the agent-economy benchmark (Phase 16).

Two strictly separated kinds of numbers:

- provider-reported usage (``source="provider"``): token counts returned by
  the model API for a call (e.g. Ollama ``prompt_eval_count`` /
  ``eval_count``). These are MEASURED.
- tokenizer estimates (``source="estimate"``): tiktoken counts of tool
  inputs/outputs. These are ESTIMATES and must never be presented as
  provider usage; they are stored under separate keys.

Costs come from a versioned rate card (JSON), never from hardcoded prices:
``{"version": ..., "currency": ..., "models": {model: {"input_per_1k": ..,
"output_per_1k": .., "cached_per_1k": ..}}}``. Missing prices or missing
usage are explicit nulls, never zeros-filled silently.
"""
from __future__ import annotations
import json


def estimate_tokens_tiktoken(text: str, model: str = "gpt-4o") -> int:
    """Tokenizer ESTIMATE (not provider usage). Returns -1 when the
    tokenizer backend is unavailable, so callers can mark it missing."""
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text or ""))
    except Exception:
        return -1


def new_trace(task_id: str, condition: str) -> dict:
    return {"task_id": task_id, "condition": condition, "calls": [],
            "provider_totals": {"input": 0, "output": 0, "cached": 0,
                                "measured_calls": 0},
            "estimate_totals": {"input": 0, "output": 0},
            "missing": []}


def record_model_call(trace: dict, usage: dict | None, est_in: int = 0,
                      est_out: int = 0) -> None:
    """usage: provider-reported dict with any of input/output/cached ints
    (ollama: prompt_eval_count/eval_count). None means unreported."""
    call = {"est_input": est_in, "est_output": est_out, "usage": usage}
    trace["calls"].append(call)
    trace["estimate_totals"]["input"] += max(est_in, 0)
    trace["estimate_totals"]["output"] += max(est_out, 0)
    if not usage:
        trace["missing"].append(f"call{len(trace['calls'])}:no-provider-usage")
        return
    # input/output are required measurements; a missing cached count only
    # means the provider does not expose that category (e.g. Ollama) and
    # is not flagged as a gap.
    for key, dest in (("input", "input"), ("output", "output")):
        v = usage.get(key)
        if isinstance(v, int) and v >= 0:
            trace["provider_totals"][dest] += v
        else:
            trace["missing"].append(f"call{len(trace['calls'])}:no-{key}")
    v = (usage or {}).get("cached")
    if isinstance(v, int) and v >= 0:
        trace["provider_totals"]["cached"] += v
    trace["provider_totals"]["measured_calls"] += 1


def record_tool_call(trace: dict, tool: str, input_text: str = "",
                     output_text: str = "", keep_text: bool = False,
                     text_cap: int = 8000) -> dict:
    """Tool I/O is always an ESTIMATE (tiktoken on the serialized text).
    keep_text additionally retains truncated I/O text for live-trial
    completeness auditing (off by default; offline artifacts stay small)."""
    est_in = estimate_tokens_tiktoken(input_text)
    est_out = estimate_tokens_tiktoken(output_text)
    rec = {"tool": tool, "in_bytes": len(input_text or ""),
           "out_bytes": len(output_text or ""), "est_in": est_in,
           "est_out": est_out}
    if keep_text:
        rec["input"] = (input_text or "")[:text_cap]
        rec["output"] = (output_text or "")[:text_cap]
    if est_in < 0 or est_out < 0:
        trace["missing"].append(f"tool:{tool}:tokenizer-unavailable")
    trace["calls"].append(rec)
    trace["estimate_totals"]["input"] += max(est_in, 0)
    trace["estimate_totals"]["output"] += max(est_out, 0)
    return rec


def load_rate_card(path: str) -> dict:
    card = json.load(open(path, encoding="utf-8"))
    assert "version" in card and "models" in card, "bad rate card"
    return card


def cost_of_trace(trace: dict, card: dict, model: str) -> dict:
    """Cost from PROVIDER-REPORTED tokens only. Returns nulls (not zeros)
    when usage or prices are missing."""
    entry = (card.get("models") or {}).get(model)
    if entry is None:
        return {"model": model, "card_version": card.get("version"),
                "cost": None, "reason": "no-rate-for-model"}
    totals = trace["provider_totals"]
    if totals["measured_calls"] == 0:
        return {"model": model, "card_version": card.get("version"),
                "cost": None, "reason": "no-provider-usage"}
    try:
        per_in = float(entry["input_per_1k"])
        per_out = float(entry["output_per_1k"])
        per_cached = float(entry.get("cached_per_1k", entry["input_per_1k"]))
    except (KeyError, TypeError, ValueError):
        return {"model": model, "card_version": card.get("version"),
                "cost": None, "reason": "incomplete-rate"}
    uncached_in = max(totals["input"] - totals["cached"], 0)
    cost = (uncached_in / 1000.0) * per_in + \
           (totals["cached"] / 1000.0) * per_cached + \
           (totals["output"] / 1000.0) * per_out
    return {"model": model, "card_version": card.get("version"),
            "cost": round(cost, 6), "currency": card.get("currency", "?"),
            "reason": None}


def amortized_index_cost(index_seconds: float, tasks: int,
                         hourly_compute_usd: float = 0.0) -> dict:
    """Local compute cost of indexing spread over N task reuses. Subscription
    and API spend are reported separately (never merged here)."""
    if tasks <= 0:
        return {"per_task_s": None, "per_task_usd": None,
                "reason": "no-tasks"}
    per_s = index_seconds / tasks
    return {"per_task_s": round(per_s, 4),
            "per_task_usd": round(per_s / 3600.0 * hourly_compute_usd, 6)
            if hourly_compute_usd else None,
            "reason": None}
