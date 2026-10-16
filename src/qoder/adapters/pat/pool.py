"""PAT→JT pool. Never touches 9router SQLite."""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field

import httpx

from qoder.core import config as config


@dataclass
class PatSlot:
    pat: str
    email: str = ""
    name: str = ""
    user_id: str = ""
    machine_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    jt: str = ""
    jrt: str = ""
    jt_expires_at: float = 0.0
    plan_status: str = ""
    paused_until: float = 0.0
    exhausted: bool = False
    last_reason: str | None = None
    success_count: int = 0
    fail_count: int = 0

    @property
    def paused(self) -> bool:
        return self.paused_until > time.monotonic()

    @property
    def jt_valid(self) -> bool:
        return bool(self.jt) and self.jt_expires_at > (time.time() + 60)


class PatPool:
    def __init__(self, slots: list[PatSlot], cooldown_s: float = 30.0):
        self._slots = slots
        self._cooldown = cooldown_s
        self._lock = threading.Lock()

    def active_slots(self) -> list[PatSlot]:
        with self._lock:
            now = time.monotonic()
            out: list[PatSlot] = []
            for s in self._slots:
                if s.exhausted:
                    continue
                if s.paused_until and now >= s.paused_until:
                    s.paused_until = 0.0
                if s.paused or not s.jt_valid:
                    continue
                out.append(s)
            return out

    def all_slots(self) -> list[PatSlot]:
        with self._lock:
            return list(self._slots)

    def record_success(self, slot: PatSlot) -> None:
        with self._lock:
            slot.paused_until = 0.0
            slot.last_reason = None
            slot.success_count += 1

    def advance(self, slot: PatSlot, reason: str) -> None:
        with self._lock:
            slot.paused_until = time.monotonic() + self._cooldown
            slot.last_reason = reason
            slot.fail_count += 1

    def mark_exhausted(self, slot: PatSlot, reason: str = "exhausted") -> None:
        with self._lock:
            slot.exhausted = True
            slot.last_reason = reason

    def report(self) -> list[dict]:
        with self._lock:
            now = time.time()
            return [
                {
                    "index": i,
                    "email": s.email,
                    "pat_prefix": (s.pat[:12] + "…") if s.pat else "",
                    "jt_remaining_s": max(0, int(s.jt_expires_at - now)),
                    "status": (
                        "exhausted"
                        if s.exhausted
                        else ("paused" if s.paused else ("no_jt" if not s.jt_valid else "active"))
                    ),
                    "plan_status": s.plan_status,
                    "last_reason": s.last_reason,
                    "success_count": s.success_count,
                    "fail_count": s.fail_count,
                }
                for i, s in enumerate(self._slots)
            ]


def _verify_headers(extra: dict | None = None) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cosy-Version": config.VERIFY_COSY_VERSION,
        "Cosy-ClientType": "5",
        "User-Agent": f"qoder/{config.VERIFY_COSY_VERSION}",
    }
    if extra:
        h.update(extra)
    return h


async def bootstrap_slot(client: httpx.AsyncClient, slot: PatSlot) -> None:
    r = await client.post(
        f"{config.OPENAPI_BASE}/api/v1/jobToken/exchange",
        headers=_verify_headers(),
        json={"personal_token": slot.pat},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    jt = data.get("token") or ""
    if not jt.startswith("jt-"):
        raise RuntimeError(f"exchange: no jt keys={list(data)}")
    slot.jt = jt
    slot.jrt = data.get("refresh_token") or ""
    expires_in_ms = int(data.get("expires_in") or 86400000)
    slot.jt_expires_at = time.time() + expires_in_ms / 1000.0

    ui = await client.get(
        f"{config.OPENAPI_BASE}/api/v1/userinfo",
        headers=_verify_headers({"Authorization": f"Bearer {jt}"}),
        timeout=30,
    )
    ui.raise_for_status()
    u = ui.json()
    slot.user_id = u.get("id") or slot.user_id
    slot.email = u.get("email") or slot.email
    slot.name = (u.get("name") or u.get("username") or slot.name or "").strip()
    if not slot.machine_id:
        slot.machine_id = str(uuid.uuid4())


async def refresh_if_needed(client: httpx.AsyncClient, slot: PatSlot, force: bool = False) -> bool:
    remaining = slot.jt_expires_at - time.time()
    if not force and slot.jt_valid and remaining > config.JT_REFRESH_WINDOW_S:
        return False
    await bootstrap_slot(client, slot)
    return True


def load_pool_from_env() -> PatPool:
    raw = config.QODER_KEYS
    slots: list[PatSlot] = []
    if raw:
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            arr = [{"pat": p} for p in raw.split() if p.startswith("pt-")]
        if isinstance(arr, str):
            arr = [{"pat": arr}]
        for item in arr:
            if isinstance(item, str):
                item = {"pat": item}
            if not isinstance(item, dict):
                continue
            pat = (item.get("pat") or item.get("secret") or item.get("key") or "").strip()
            if not pat.startswith("pt-"):
                continue
            slots.append(
                PatSlot(
                    pat=pat,
                    email=(item.get("email") or "").strip(),
                    name=(item.get("name") or "").strip(),
                    user_id=(item.get("user_id") or item.get("userId") or "").strip(),
                    machine_id=(item.get("machine_id") or item.get("machineId") or str(uuid.uuid4())).strip(),
                    plan_status=(item.get("plan_status") or "").strip(),
                )
            )
    if not slots:
        raise RuntimeError("No Qoder PATs configured (set QODER_KEYS)")
    return PatPool(slots, cooldown_s=config.KEY_COOLDOWN_S)
