"""Upstream Qoder chat + model/list (prod Cosy + Encode=1 + soft-error unwrap)."""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from qoder.core import config as config
from qoder.upstream.body import build_agent_chat_body, default_model_config
from qoder.core.cosy import build_cosy_headers
from qoder.core.encoding import encode_body
from qoder.adapters.pat.pool import PatSlot
from qoder.core.usage import normalize_usage

log = logging.getLogger("qoder.client")


class UpstreamError(Exception):
    def __init__(self, message: str, status: int = 502, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def _creds(slot: PatSlot) -> dict[str, str]:
    return {
        "user_id": slot.user_id,
        "auth_token": slot.jt,
        "name": slot.name or "",
        "email": slot.email or "",
        "machine_id": slot.machine_id,
    }


async def fetch_model_list_raw(client: httpx.AsyncClient, slot: PatSlot) -> dict | None:
    url = config.model_list_url()
    c = _creds(slot)
    headers = build_cosy_headers(
        b"",
        url,
        user_id=c["user_id"],
        auth_token=c["auth_token"],
        name=c["name"],
        email=c["email"],
        machine_id=c["machine_id"],
    )
    headers["Accept"] = "application/json"
    headers["Accept-Encoding"] = "identity"
    try:
        r = await client.get(url, headers=headers, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.warning("model/list failed: %s", e)
        return None


async def fetch_model_config(client: httpx.AsyncClient, slot: PatSlot, key: str) -> dict:
    data = await fetch_model_list_raw(client, slot)
    if not data:
        return default_model_config(key)
    for row in data.get("chat") or []:
        if isinstance(row, dict) and row.get("key") == key:
            out = dict(row)
            out["key"] = key
            return out
    return default_model_config(key)


async def list_openai_models(client: httpx.AsyncClient, slot: PatSlot) -> list[dict]:
    """Live Cosy model/list → OpenAI /v1/models rows (+ cmodel inject like prod shim)."""
    data = await fetch_model_list_raw(client, slot)
    rows: list[dict] = []
    seen: set[str] = set()
    chat = list(data.get("chat") or []) if data else []
    # Prod shim injects cmodel if missing
    if not any(isinstance(r, dict) and r.get("key") == "cmodel" for r in chat):
        chat = [
            {
                "key": "cmodel",
                "display_name": "Cantus",
                "max_input_tokens": 200000,
                "max_output_tokens": 65536,
                "enable": True,
            },
            *chat,
        ]
    for row in chat:
        if not isinstance(row, dict):
            continue
        if row.get("enable") is False:
            continue
        mid = row.get("key")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        rows.append(
            {
                "id": mid,
                "object": "model",
                "owned_by": "qoder",
                "context_length": int(row.get("max_input_tokens") or 0) or None,
                "max_output_tokens": int(row.get("max_output_tokens") or 0) or None,
                "display_name": row.get("display_name") or mid,
                "is_reasoning": bool(row.get("is_reasoning")),
                "is_vl": bool(row.get("is_vl")),
            }
        )
    if not rows:
        rows = [
            {"id": m, "object": "model", "owned_by": "qoder"}
            for m in config.FALLBACK_MODELS
        ]
    return rows


async def fetch_quota_usage(client: httpx.AsyncClient, slot: PatSlot) -> dict:
    """Trial credits from openapi (seller guide / 9router usageUrl)."""
    r = await client.get(
        f"{config.OPENAPI_BASE}/api/v2/quota/usage",
        headers={
            "Authorization": f"Bearer {slot.jt}",
            "Accept": "application/json",
            "Cosy-Version": config.VERIFY_COSY_VERSION,
            "Cosy-ClientType": "5",
            "User-Agent": f"qoder/{config.VERIFY_COSY_VERSION}",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _downstream_error(model_key: str, status: int, inner: str) -> str:
    msg = f"qoder upstream status={status}"
    try:
        outer = json.loads(inner) if inner else {}
        if isinstance(outer, dict) and outer.get("message"):
            msg = str(outer["message"])[:500]
    except Exception:
        if inner:
            msg = inner[:500]
    return json.dumps(
        {
            "error": {
                "type": "upstream_error",
                "code": f"qoder_{status}",
                "message": msg,
                "retryable": status in (429, 500, 502, 503, 504, 416),
            },
            "model": model_key,
        },
        ensure_ascii=False,
    )


async def chat_stream(
    client: httpx.AsyncClient,
    slot: PatSlot,
    openai_body: dict,
    model: str,
) -> AsyncIterator[bytes]:
    """Yield OpenAI-shaped SSE bytes (data: ...\\n\\n), soft-error unwrapped like prod patch."""
    cfg_key = model
    for pref in ("qoderkey/", "qoder/", "qd/"):
        if cfg_key.startswith(pref):
            cfg_key = cfg_key[len(pref) :]
    model_cfg = await fetch_model_config(client, slot, cfg_key or "auto")
    qoder_key, payload = build_agent_chat_body(openai_body, model, model_cfg)
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = encode_body(plain)

    url = config.chat_url()
    c = _creds(slot)
    headers = build_cosy_headers(
        encoded,
        url,
        user_id=c["user_id"],
        auth_token=c["auth_token"],
        name=c["name"],
        email=c["email"],
        machine_id=c["machine_id"],
    )
    headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
            "Accept-Encoding": "identity",
            "X-Model-Key": qoder_key,
            "X-Model-Source": str(model_cfg.get("source") or "system"),
        }
    )

    async with client.stream("POST", url, headers=headers, content=encoded, timeout=config.UPSTREAM_TIMEOUT) as resp:
        if resp.status_code >= 400:
            # Keep the existing diagnostic prefix without buffering the entire
            # upstream error response. The stream context closes the remainder.
            prefix = bytearray()
            async for chunk in resp.aiter_bytes():
                prefix.extend(chunk[:500 - len(prefix)])
                if len(prefix) >= 500:
                    break
            body = bytes(prefix)
            raise UpstreamError(f"qoder HTTP {resp.status_code}: {body!r}", status=resp.status_code)

        saw_done = False
        buffer = ""
        async for chunk in resp.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                trimmed = line.replace("\r", "").strip()
                if not trimmed:
                    continue
                if trimmed.startswith("event:"):
                    continue
                if not trimmed.startswith("data:"):
                    continue
                data = trimmed[5:].lstrip()
                if data == "[DONE]":
                    saw_done = True
                    yield b"data: [DONE]\n\n"
                    return
                try:
                    env = json.loads(data)
                except Exception:
                    # already openai-ish
                    yield f"data: {data}\n\n".encode("utf-8")
                    continue
                if not isinstance(env, dict) or "statusCodeValue" not in env:
                    yield f"data: {data}\n\n".encode("utf-8")
                    continue
                status_val = env.get("statusCodeValue")
                if not isinstance(status_val, int):
                    status_val = 200
                inner = env.get("body") if isinstance(env.get("body"), str) else ""
                if status_val != 200:
                    err = _downstream_error(qoder_key, status_val, inner)
                    yield f"data: {err}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    saw_done = True
                    return
                if not inner:
                    continue
                if inner == "[DONE]":
                    saw_done = True
                    yield b"data: [DONE]\n\n"
                    return
                inner = inner.replace("\r", "").replace("\n", "")
                try:
                    chunk_obj = json.loads(inner)
                    if isinstance(chunk_obj, dict) and isinstance(chunk_obj.get("usage"), dict):
                        chunk_obj["usage"] = normalize_usage(chunk_obj["usage"])
                        inner = json.dumps(chunk_obj, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    pass
                yield f"data: {inner}\n\n".encode("utf-8")
        if not saw_done:
            yield b"data: [DONE]\n\n"


def _append_text(parts: list[str], value: Any) -> None:
    if isinstance(value, str) and value:
        parts.append(value)


def _merge_tool_call(target: dict[int, dict], raw: Any, fallback_index: int = 0) -> None:
    if not isinstance(raw, dict):
        return
    try:
        index = int(raw.get("index", fallback_index))
    except (TypeError, ValueError):
        index = fallback_index
    call = target.setdefault(
        index,
        {
            "index": index,
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if raw.get("id"):
        call["id"] = str(raw["id"])
    if raw.get("type"):
        call["type"] = str(raw["type"])
    function = raw.get("function")
    if isinstance(function, dict):
        if isinstance(function.get("name"), str) and function["name"]:
            call["function"]["name"] += function["name"]
        if isinstance(function.get("arguments"), str) and function["arguments"]:
            call["function"]["arguments"] += function["arguments"]


def _merge_function_call(target: dict[str, str], raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    if isinstance(raw.get("name"), str) and raw["name"]:
        target["name"] += raw["name"]
    if isinstance(raw.get("arguments"), str) and raw["arguments"]:
        target["arguments"] += raw["arguments"]


def _openai_tool_calls(target: dict[int, dict]) -> list[dict]:
    out = []
    for index in sorted(target):
        call = target[index]
        if not call["id"] or not call["function"]["name"]:
            continue
        out.append(
            {
                "id": call["id"],
                "type": call["type"],
                "function": dict(call["function"]),
            }
        )
    return out


async def chat_nonstream(
    client: httpx.AsyncClient,
    slot: PatSlot,
    openai_body: dict,
    model: str,
) -> dict[str, Any]:
    """Aggregate Qoder SSE without dropping tool calls or reasoning output."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls_by_index: dict[int, dict] = {}
    function_call = {"name": "", "arguments": ""}
    err_obj = None
    last_usage: dict | None = None
    finish_reason = "stop"
    resp_id = "chatcmpl-qoder"
    resp_model = model
    upstream = chat_stream(client, slot, openai_body, model)
    try:
        async for raw in upstream:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].lstrip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("error"):
                err_obj = obj
                break
            if obj.get("id"):
                resp_id = str(obj["id"])
            if obj.get("model"):
                resp_model = str(obj["model"])
            if isinstance(obj.get("usage"), dict):
                last_usage = obj["usage"]
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            ch0 = choices[0] if isinstance(choices[0], dict) else {}
            if ch0.get("finish_reason"):
                finish_reason = str(ch0["finish_reason"])
            delta = ch0.get("delta") if isinstance(ch0.get("delta"), dict) else {}
            _append_text(content_parts, delta.get("content"))
            _append_text(
                reasoning_parts,
                delta.get("reasoning_content") or delta.get("reasoning"),
            )
            for fallback_index, tool_call in enumerate(delta.get("tool_calls") or []):
                _merge_tool_call(tool_calls_by_index, tool_call, fallback_index)
            _merge_function_call(function_call, delta.get("function_call"))
    
            # Rare non-delta message form.
            message = ch0.get("message") if isinstance(ch0.get("message"), dict) else {}
            if message and not delta:
                _append_text(content_parts, message.get("content"))
                _append_text(
                    reasoning_parts,
                    message.get("reasoning_content") or message.get("reasoning"),
                )
                for fallback_index, tool_call in enumerate(message.get("tool_calls") or []):
                    _merge_tool_call(tool_calls_by_index, tool_call, fallback_index)
                _merge_function_call(function_call, message.get("function_call"))
    finally:
        try:
            await upstream.aclose()
        except Exception:
            log.exception("nonstream upstream cleanup failed")
    if err_obj:
        return err_obj

    text = "".join(content_parts)
    reasoning_text = "".join(reasoning_parts)
    tool_calls = _openai_tool_calls(tool_calls_by_index)
    has_function_call = bool(function_call["name"] or function_call["arguments"])

    message: dict[str, Any] = {"role": "assistant", "content": text}
    if reasoning_text:
        message["reasoning_content"] = reasoning_text
    if tool_calls:
        message["tool_calls"] = tool_calls
        if finish_reason == "stop":
            finish_reason = "tool_calls"
    if has_function_call:
        message["function_call"] = function_call
        if finish_reason == "stop":
            finish_reason = "function_call"
    return {
        "id": resp_id,
        "object": "chat.completion",
        "model": resp_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": normalize_usage(last_usage),
    }
