"""Env config — defaults match prod 9router constants + qoder-patch prefer_host."""
from __future__ import annotations

import os

BIND_HOST = os.getenv("QODER_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.getenv("QODER_BIND_PORT", "18091"))

# Prod shim: QODER_PREFER_HOST=api2.qoder.sh (stock 9router defaults to api3)
PREFER_HOST = (os.getenv("QODER_PREFER_HOST") or "api2.qoder.sh").strip()
OPENAPI_BASE = (os.getenv("QODER_OPENAPI_BASE") or "https://openapi.qoder.sh").rstrip("/")

UPSTREAM_TIMEOUT = float(os.getenv("QODER_TIMEOUT", "180"))
CONNECT_TIMEOUT = float(os.getenv("QODER_CONNECT_TIMEOUT", "15"))
CONCURRENCY = int(os.getenv("QODER_CONCURRENCY", "32"))
KEY_COOLDOWN_S = max(1.0, float(os.getenv("QODER_KEY_COOLDOWN_S", "30")))
JT_REFRESH_WINDOW_S = max(60, int(os.getenv("QODER_JT_REFRESH_WINDOW_S", str(2 * 3600))))
JT_REFRESH_INTERVAL_S = max(30, int(os.getenv("QODER_JT_REFRESH_INTERVAL_S", "300")))
# Soft-disable fill-first head when trial credits remaining/total < pct.
QUOTA_GUARD_INTERVAL_S = max(15, int(os.getenv("QODER_QUOTA_GUARD_INTERVAL_S", "60")))
QUOTA_SOFT_DISABLE_PCT = float(os.getenv("QODER_QUOTA_SOFT_DISABLE_PCT", "5.0"))

# From prod chunk 69704: cT / Ty / RB / pM / qk / xG
COSY_VERSION = os.getenv("QODER_COSY_VERSION", "1.0.0")
# Seller verify guide for PAT exchange (not chat Cosy)
VERIFY_COSY_VERSION = os.getenv("QODER_VERIFY_COSY_VERSION", "1.1.13")

QODER_KEYS = os.getenv("QODER_KEYS", "").strip()

FALLBACK_MODELS = [
    "qmodel_38max",
    "qmodel_latest",
    "qmodel",
    "cmodel",
    "kmodel_latest",
    "kmodel",
    "gm51model",
    "dmodel",
    "dfmodel",
    "mmodel",
    "auto",
]


def chat_url() -> str:
    # Prod KC with Encode=1, host overridden to prefer_host (shim parity)
    return (
        f"https://{PREFER_HOST}/algo/api/v2/service/pro/sse/agent_chat_generation"
        f"?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
    )


def model_list_url() -> str:
    return f"https://{PREFER_HOST}/algo/api/v2/model/list"
