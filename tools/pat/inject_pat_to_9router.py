#!/usr/bin/env python3
"""
Inject a Qoder PAT (pt-...) into 9router as a working chat connection.

Flow:
  1. Exchange PAT -> JT (24h) + JRT (48h) via /api/v1/jobToken/exchange
  2. Get userId + email + name via /api/v1/userinfo
  3. INSERT into 9router.providerConnections with:
       provider      = 'qoder'
       authType      = 'pat'                  (new type, distinguish from 'oauth'/'device')
       accessToken   = JT (jt-...)            (9router uses this as authToken in cosy signing)
       refreshToken  = JRT (jrt-...)          (for refresh)
       providerSpecificData = {
         authMethod: 'pat',
         userId,                              (required by cosy signing)
         machineId,                           (random UUID per PAT; 9router uses for X-Machine-Id)
         pat,                                 (PAT retained for re-exchange when JT expires)
         patExpiresAt,                        (when PAT itself dies — refresh from web session)
         jtExpiresAt,                         (when JT dies — re-exchange from PAT)
       }

Because JT only lives 24h, this script also emits a JSON sidecar at
`pat_connections.json` so an external refresher can re-exchange before expiry.

Usage:
  QODER_9ROUTER_DB=/path/to/data.sqlite python3 inject_pat_to_9router.py <PAT> [priority] [email]

If email is omitted, it's fetched from /api/v1/userinfo.
"""
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

OPENAPI = "https://openapi.qoder.sh"
UA = "qodercli/1.0.47"
TIMEOUT = 20


def die(msg, code=2):
    print(f"[!] {msg}", file=sys.stderr)
    sys.exit(code)


def exchange_pat(pat: str) -> dict:
    """PAT -> JT + JRT. Returns {jt, jrt, jt_expires_at_iso, jt_expires_in_ms}."""
    r = requests.post(
        f"{OPENAPI}/api/v1/jobToken/exchange",
        headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json", "User-Agent": UA},
        json={"personal_token": pat},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        die(f"exchange PAT failed: HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    jt = data.get("token")
    if not jt or not jt.startswith("jt-"):
        die(f"exchange PAT: no jt- in response: {data}")
    return {
        "jt": jt,
        "jrt": data.get("refresh_token", ""),
        "jt_expires_at_iso": data.get("expires_at"),
        "jt_expires_in_ms": data.get("expires_in", 86400000),
        "jrt_expires_at_iso": data.get("refresh_token_expires_at"),
        "raw": data,
    }


def fetch_userinfo(jt: str) -> dict:
    r = requests.get(
        f"{OPENAPI}/api/v1/userinfo",
        headers={"Authorization": f"Bearer {jt}", "Accept": "application/json", "User-Agent": UA},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        die(f"userinfo failed: HTTP {r.status_code}: {r.text[:300]}")
    d = r.json()
    return {
        "userId": d.get("id") or "",
        "email": d.get("email") or "",
        "name": d.get("name") or d.get("username") or "",
        "organization_id": d.get("organization_id") or "",
    }


def fetch_plan(jt: str) -> dict:
    """Best-effort: read plan tier so we know if PAT is Pro Trial or Free."""
    try:
        r = requests.get(
            f"{OPENAPI}/api/v2/user/plan",
            headers={"Authorization": f"Bearer {jt}", "Accept": "application/json", "User-Agent": UA},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def inject_to_9router(db_path: str, conn_data: dict, priority: int, is_active: int) -> str:
    """
    INSERT a new providerConnection. Returns the row id.
    Idempotency: if a row with same email + authType='pat' exists, replace it.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    email = conn_data["email"]
    # Replace existing pat row for same email
    cur.execute(
        "SELECT id FROM providerConnections WHERE provider='qoder' AND authType='pat' AND email=?",
        (email,),
    )
    row = cur.fetchone()
    if row:
        conn_id = row[0]
        cur.execute(
            "UPDATE providerConnections SET data=?, priority=?, isActive=?, updatedAt=? WHERE id=?",
            (json.dumps(conn_data["data"]), priority, is_active, int(time.time() * 1000), conn_id),
        )
        print(f"[~] updated existing pat row id={conn_id}", flush=True)
    else:
        conn_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        cur.execute(
            """INSERT INTO providerConnections
               (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                conn_id,
                "qoder",
                "pat",
                conn_data["name"],
                email,
                priority,
                is_active,
                json.dumps(conn_data["data"]),
                now_ms,
                now_ms,
            ),
        )
        print(f"[+] inserted new pat row id={conn_id}", flush=True)

    conn.commit()
    conn.close()
    return conn_id


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    pat = sys.argv[1]
    if not pat.startswith("pt-"):
        die(f"not a PAT: {pat[:20]}...")

    priority = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    override_email = sys.argv[3] if len(sys.argv) > 3 else None

    db_path = os.environ.get("QODER_9ROUTER_DB")
    if not db_path:
        die("QODER_9ROUTER_DB env var not set (path to 9router data.sqlite)")

    print(f"[*] PAT: {pat[:24]}…", flush=True)

    # Step 1: exchange
    print("[*] exchanging PAT -> JT ...", flush=True)
    ex = exchange_pat(pat)
    jt = ex["jt"]
    print(f"    JT  = {jt}", flush=True)
    print(f"    JRT = {ex['jrt']}", flush=True)
    print(f"    JT expires at: {ex['jt_expires_at_iso']} (in {ex['jt_expires_in_ms']/3600000:.1f}h)", flush=True)

    # Step 2: userinfo
    print("[*] fetching userinfo ...", flush=True)
    ui = fetch_userinfo(jt)
    email = override_email or ui["email"]
    if not email:
        die("no email from userinfo and no override given")
    print(f"    userId = {ui['userId']}", flush=True)
    print(f"    email  = {email}", flush=True)
    print(f"    name   = {ui['name']}", flush=True)

    # Step 3: plan (best-effort)
    plan = fetch_plan(jt)
    plan_tier = plan.get("plan_tier_name") or "?"
    is_paid = plan.get("is_paid_plan", False)
    print(f"    plan   = {plan_tier} (paid={is_paid})", flush=True)

    # Step 4: build connection data
    machine_id = str(uuid.uuid4())  # fresh machine_id per PAT (9router uses for X-Machine-Id in cosy signing)
    now_ms = int(time.time() * 1000)
    jt_expires_ms = now_ms + ex["jt_expires_in_ms"]
    data = {
        "displayName": ui["name"] or email.split("@")[0],
        "accessToken": jt,                 # 9router uses this as authToken in cosy signing
        "refreshToken": ex["jrt"],         # JRT (48h)
        "expiresAt": ex["jt_expires_at_iso"],  # ISO for 9router UI display
        "testStatus": "active",
        "expiresIn": ex["jt_expires_in_ms"] // 1000,
        "providerSpecificData": {
            "authMethod": "pat",
            "userId": ui["userId"],
            "machineId": machine_id,
            "organizationId": ui["organization_id"],
            "pat": pat,                    # retained for re-exchange
            "jtExpiresAt": jt_expires_ms,  # ms epoch
            "jtExpiresAtIso": ex["jt_expires_at_iso"],
            "planTier": plan_tier,
            "isPaidPlan": is_paid,
            # Required by 9router quota tracker: guard in /api/usage/[connectionId]/route.js
            # checks `providerSpecificData.usageUrl` for non-oauth connections. Without this,
            # quota UI shows "Usage not available for this connection" for PAT rows.
            # Value is truthy check only; actual fetch URL comes from xq["qoder"].usage.url.
            "usageUrl": "https://openapi.qoder.sh/api/v2/quota/usage",
        },
    }

    conn_data = {"name": ui["name"] or email.split("@")[0], "email": email, "data": data}

    # Step 5: inject
    print(f"[*] injecting to 9router db={db_path} priority={priority} ...", flush=True)
    conn_id = inject_to_9router(db_path, conn_data, priority, 1)

    # Step 6: write sidecar for refresher
    sidecar = {
        "conn_id": conn_id,
        "email": email,
        "userId": ui["userId"],
        "machineId": machine_id,
        "pat": pat,
        "jt": jt,
        "jrt": ex["jrt"],
        "jt_expires_at_iso": ex["jt_expires_at_iso"],
        "jt_expires_ms": jt_expires_ms,
        "plan_tier": plan_tier,
        "is_paid_plan": is_paid,
        "injected_at": now_ms,
    }
    sidecar_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pat_connections.json")
    # Append to list
    existing = []
    if os.path.exists(sidecar_path):
        try:
            existing = json.load(open(sidecar_path))
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
    # Replace if same email
    existing = [e for e in existing if e.get("email") != email]
    existing.append(sidecar)
    with open(sidecar_path, "w") as f:
        json.dump(existing, f, indent=2)
    print(f"[+] sidecar written: {sidecar_path}", flush=True)

    print()
    print("=== SUMMARY ===")
    print(f"  connection id : {conn_id}")
    print(f"  email         : {email}")
    print(f"  userId        : {ui['userId']}")
    print(f"  machineId     : {machine_id}")
    print(f"  JT (24h)      : {jt}")
    print(f"  JRT (48h)     : {ex['jrt']}")
    print(f"  plan          : {plan_tier}")
    print(f"  priority      : {priority}")
    print(f"  isActive      : 1")
    print(f"  expires at    : {ex['jt_expires_at_iso']}")
    print()
    print("Next: test chat via 9router:")
    print(f'  curl -sS http://localhost:20128/v1/chat/completions -H "Authorization: Bearer sk-..." \\')
    print(f'    -d \'{{"model":"qd/auto","messages":[{{"role":"user","content":"hi"}}]}}\'')


if __name__ == "__main__":
    main()
