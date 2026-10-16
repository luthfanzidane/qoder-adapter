"""Normalize Qoder usage into OpenAI shape the Amanai gateway understands."""
from __future__ import annotations

from typing import Any


def normalize_usage(raw: dict | None) -> dict[str, Any]:
    """Map Qoder usage chunk → gateway `_apply_openai_usage` fields.

    Gateway reads:
      prompt_tokens / input_tokens
      completion_tokens / output_tokens
      prompt_tokens_details.cached_tokens (or top-level cached_tokens /
        cache_read_input_tokens)
      cache_creation_* variants
      completion_tokens_details.reasoning_tokens
      credit (optional)
    """
    if not isinstance(raw, dict):
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated": True,
        }

    prompt = int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0)
    completion = int(raw.get("completion_tokens") or raw.get("output_tokens") or 0)
    total = int(raw.get("total_tokens") or (prompt + completion))

    prompt_details = raw.get("prompt_tokens_details")
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    # Preserve + fill cache aliases if Qoder uses alternate keys
    cached = (
        prompt_details.get("cached_tokens")
        or raw.get("cached_tokens")
        or raw.get("cache_read_input_tokens")
        or raw.get("cache_read_tokens")
    )
    if cached is not None and "cached_tokens" not in prompt_details:
        prompt_details = {**prompt_details, "cached_tokens": int(cached)}

    creation = (
        prompt_details.get("cache_creation_tokens")
        or prompt_details.get("cache_creation_input_tokens")
        or raw.get("cache_creation_input_tokens")
        or raw.get("cache_creation_tokens")
    )
    if creation is not None and "cache_creation_tokens" not in prompt_details:
        prompt_details = {**prompt_details, "cache_creation_tokens": int(creation)}

    completion_details = raw.get("completion_tokens_details")
    if not isinstance(completion_details, dict):
        completion_details = {}

    out: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "estimated": False,
    }
    if prompt_details:
        out["prompt_tokens_details"] = prompt_details
    if completion_details:
        out["completion_tokens_details"] = completion_details
    # Pass through Qoder billing fields the gateway may sniff
    for k in ("credits", "original_credits", "billable", "credit"):
        if k in raw and raw[k] is not None:
            out[k] = raw[k]
    # Alias credit for gateway result.cb_credit
    if "credit" not in out and raw.get("credits") is not None:
        out["credit"] = raw["credits"]
    return out
