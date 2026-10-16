"""OpenAI → Qoder agent_chat payload (prod chunk 318) + session rotate (prod patch)."""
from __future__ import annotations

import secrets
import time
import uuid
from typing import Any


def _flatten_content(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        parts = []
        for p in v:
            if isinstance(p, dict):
                if p.get("type") in (None, "text") and isinstance(p.get("text"), str):
                    parts.append(p["text"])
                elif isinstance(p.get("content"), str):
                    parts.append(p["content"])
        return "".join(parts)
    return str(v)


def normalize_messages(raw: Any) -> tuple[list[dict], str]:
    if not isinstance(raw, list):
        return [], ""
    systems: list[str] = []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = _flatten_content(item.get("content"))
        role = item.get("role") or ""
        if role == "system":
            if content:
                systems.append(content)
            continue
        cp = dict(item)
        cp["content"] = content
        out.append(cp)
    return out, "\n\n".join(systems)


def last_user_text(msgs: list[dict]) -> str:
    for m in reversed(msgs):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def default_model_config(key: str) -> dict:
    if key == "cmodel":
        return {
            "key": "cmodel",
            "source": "system",
            "display_name": "Cantus",
            "model": "",
            "format": "openai",
            "max_input_tokens": 200000,
            "max_output_tokens": 65536,
            "price_factor": 1.6,
            "original_price_factor": 3.2,
            "strategies": None,
            "context_config": {"200K": {"token_count": 200000, "is_default": True}},
        }
    return {
        "key": key,
        "source": "system",
        "display_name": key,
        "model": "",
        "format": "openai",
        "max_input_tokens": 131072,
        "max_output_tokens": 32768,
        "is_reasoning": False,
        "is_vl": False,
    }


def build_agent_chat_body(
    openai_body: dict,
    model: str,
    model_config: dict | None = None,
) -> tuple[str, dict]:
    qoder_key = (model or "").strip()
    for pref in ("qoderkey/", "qoder/", "qd/"):
        if qoder_key.startswith(pref):
            qoder_key = qoder_key[len(pref) :]
    if not qoder_key:
        qoder_key = str(openai_body.get("model") or "auto")
        for pref in ("qoderkey/", "qoder/", "qd/"):
            if qoder_key.startswith(pref):
                qoder_key = qoder_key[len(pref) :]
    if not qoder_key:
        qoder_key = "auto"

    cfg = dict(model_config or default_model_config(qoder_key))
    cfg.setdefault("key", qoder_key)

    msgs, system_text = normalize_messages(openai_body.get("messages"))
    tools = openai_body.get("tools") if isinstance(openai_body.get("tools"), list) else []
    is_reasoning = bool(cfg.get("is_reasoning"))
    max_out = int(cfg.get("max_output_tokens") or 32768) or 32768
    for k in ("max_tokens", "max_completion_tokens"):
        v = openai_body.get(k)
        if isinstance(v, (int, float)) and 0 < int(v) < max_out:
            max_out = int(v)

    last_user = last_user_text(msgs)
    # Prod patch: rotate session/record every request (avoid 416 velocity)
    session_id = secrets.token_hex(8)
    record_id = secrets.token_hex(8)

    payload = {
        "request_id": str(uuid.uuid4()),
        "request_set_id": record_id,
        "chat_record_id": record_id,
        "session_id": session_id,
        "stream": True,
        "chat_task": "FREE_INPUT",
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "session_type": "qodercli",
        "agent_id": "agent_common",
        "task_id": "common",
        "code_language": "",
        "chat_prompt": "",
        "image_urls": None,
        "aliyun_user_type": "",
        "system": system_text,
        "messages": msgs,
        "tools": tools,
        "parameters": {"max_tokens": max_out},
        "chat_context": {
            "chatPrompt": "",
            "imageUrls": None,
            "extra": {
                "context": [],
                "modelConfig": {"key": qoder_key, "is_reasoning": is_reasoning},
                "originalContent": last_user,
            },
            "features": [],
            "text": last_user,
        },
        "model_config": cfg,
        "business": {
            "product": "cli",
            "version": "1.0.0",
            "type": "agent",
            "stage": "start",
            "id": str(uuid.uuid4()),
            "name": (last_user or "")[:30],
            "begin_at": int(time.time() * 1000),
        },
    }
    return qoder_key, payload
